"""Unit tests for the control policy layer with a fake multiplexer.

Launch records are real files in the private test state dir; the
multiplexer, process table and hook session records are faked.
"""

import json
import os
import stat
import threading
import time

import pytest

from tongs_agents import control, discovery, launches, paths
from tongs_agents.multiplexers.base import (
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
)
from tongs_agents.profiles import Profile

SESSION = "agents"
SOCKET_DIR = "/run/user/1000/zellij-test"
START = "4242"


class FakeProcess:
    def __init__(self, pid, start_token):
        self.pid = pid
        self.start_token = start_token


class FakeMux:
    """Records pane I/O as (thread, operation, payload) events."""

    name = "zellij"

    def __init__(self):
        self.panes = {}
        self.events = []
        self.lock = threading.Lock()
        self.paste_delay = 0.0
        self.screen = "line 1\nline 2\nprompt>"
        self.list_error = None
        self.io_error = None

    def add_pane(self, pane_id, *, exited=False, exit_status=None, socket_dir=SOCKET_DIR):
        self.panes[pane_id] = PaneInfo(
            ref=PaneRef("zellij", SESSION, pane_id, socket_dir),
            title="r1", command="python", exited=exited, focused=False,
            exit_status=exit_status,
        )

    def list_panes(self, session):
        if self.list_error is not None:
            raise self.list_error
        return [pane for pane in self.panes.values() if pane.ref.session == session]

    def _event(self, operation, payload):
        if self.io_error is not None:
            raise self.io_error
        with self.lock:
            self.events.append((threading.current_thread().name, operation, payload))

    def paste(self, ref, text):
        self._event("paste", text)
        if self.paste_delay:
            time.sleep(self.paste_delay)

    def send_keys(self, ref, keys):
        self._event("keys", tuple(keys))

    def capture(self, ref, lines=200):
        self._event("capture", lines)
        return "\n".join(self.screen.splitlines()[-lines:])


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A claimed launch r1 in pane 3, its live process and an empty hook store."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv(control.CALLER_ENV, raising=False)
    mux = FakeMux()
    mux.add_pane("3")
    processes = {}
    hooks = []
    requested = []

    def get_multiplexer(name, *, socket_dir=None):
        requested.append((name, socket_dir))
        if name != "zellij":
            raise Unsupported(f"no multiplexer adapter named {name!r}")
        return mux

    monkeypatch.setattr(control, "get_multiplexer", get_multiplexer)
    monkeypatch.setattr(discovery, "get_process_info", processes.get)
    monkeypatch.setattr(discovery, "read_session_records", lambda: [dict(h) for h in hooks])

    class World:
        pass

    state = World()
    state.mux = mux
    state.processes = processes
    state.hooks = hooks
    state.requested = requested
    state.settings = control.Settings(send_delay_ms=0)

    def launch(pane_id="3", *, profile="r1", pid=4000, start=START,
               socket_dir=SOCKET_DIR, multiplexer="zellij"):
        pending = launches.create_pending(
            Profile(profile, "other", ("fake",)), SESSION, multiplexer
        )
        record = launches.claim(
            pending.launch_id, pid, start,
            PaneRef(multiplexer, SESSION, pane_id, socket_dir),
        )
        processes[pid] = FakeProcess(pid, start)
        return record

    state.launch = launch
    state.record = launch()
    return state


def audit_lines():
    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def hook_record(record, status="idle", *, pane=None, session=SESSION, ts=None,
                arrival=1, session_id="s1", socket_dir=None):
    env = {"ZELLIJ_SESSION_NAME": session, "ZELLIJ_PANE_ID": pane or record.pane_id}
    if socket_dir:
        env["ZELLIJ_SOCKET_DIR"] = socket_dir
    return {
        "session_id": session_id,
        "canonical_id": f"other:{session_id}",
        "harness_id": "other",
        "status": status,
        "detail": f"{status} detail",
        "ts": int(time.time()) if ts is None else ts,
        "arrival_sequence": arrival,
        "terminal": "zellij",
        "terminal_env": env,
        "launch": {"launch_id": record.launch_id, "profile_id": record.profile},
    }


# verify


def test_verify_returns_the_launch_pane(world):
    ref = control.verify(world.record.launch_id)

    assert ref == PaneRef("zellij", SESSION, "3", SOCKET_DIR)
    assert world.requested == [("zellij", SOCKET_DIR)]


def test_a_hook_record_naming_no_pane_does_not_refuse(world):
    entry = hook_record(world.record)
    entry["terminal_env"] = {}
    world.hooks.append(entry)
    assert control.verify(world.record.launch_id).pane_id == "3"


def _pending(world):
    return launches.create_pending(Profile("r2", "other", ("fake",)), SESSION, "zellij")


def _replaced(world):
    launches.claim(world.record.launch_id, 4001, START, PaneRef("zellij", SESSION, "4"))
    world.processes[4001] = FakeProcess(4001, START)
    world.mux.add_pane("4")
    return world.record


REFUSALS = {
    "not a launch id": lambda world: "not-a-launch-id",
    "never spawned": lambda world: "0" * 32,
    "pending, not claimed": lambda world: _pending(world).launch_id,
    "replaced by a later claim": lambda world: _replaced(world).launch_id,
    "pane not listed": lambda world: world.mux.panes.pop("3") and world.record.launch_id,
    "session cannot be listed": lambda world: setattr(
        world.mux, "list_error", MultiplexerError("unknown or EXITED session")
    ) or world.record.launch_id,
    "pane served from another socket dir": lambda world: world.mux.add_pane(
        "3", socket_dir="/run/user/1000/zellij-other"
    ) or world.record.launch_id,
    "launched process is gone": lambda world: world.processes.pop(4000)
    and world.record.launch_id,
    "pane runs another process (pid reused)": lambda world: world.processes.update(
        {4000: FakeProcess(4000, "9999")}
    ) or world.record.launch_id,
    "claim has no start token": lambda world: world.launch(
        "5", pid=4005, start=None
    ).launch_id if not world.mux.add_pane("5") else None,
    "hook record names another pane": lambda world: world.hooks.append(
        hook_record(world.record, pane="7")
    ) or world.record.launch_id,
    "hook record names another session": lambda world: world.hooks.append(
        hook_record(world.record, session="elsewhere")
    ) or world.record.launch_id,
    "hook record names another socket dir": lambda world: world.hooks.append(
        hook_record(world.record, socket_dir="/run/user/1000/zellij-other")
    ) or world.record.launch_id,
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_every_refusal_path_exits_5_and_touches_no_pane(world, case):
    launch_id = REFUSALS[case](world)

    with pytest.raises(control.Refused) as caught:
        control.verify(launch_id)
    assert caught.value.exit_code == control.EXIT_REFUSED == 5

    with pytest.raises(control.Refused):
        control.send(launch_id, "hello", actor="test", settings=world.settings)
    with pytest.raises(control.Refused):
        control.read(launch_id)
    assert world.mux.events == []
    outcomes = [(line["action"], line["outcome"], line["exit_code"]) for line in audit_lines()]
    assert outcomes == [("send", "refused", 5), ("read", "refused", 5)]


def test_a_tmux_launch_is_unsupported(world):
    record = world.launch("8", pid=4008, multiplexer="tmux")
    with pytest.raises(control.ControlUnsupported) as caught:
        control.send(record.launch_id, "hi", actor="test", settings=world.settings)
    assert caught.value.exit_code == control.EXIT_UNSUPPORTED == 6
    assert audit_lines()[-1]["outcome"] == "unsupported"


def test_an_exited_pane_is_agent_exited(world):
    world.mux.add_pane("3", exited=True, exit_status=2)
    with pytest.raises(control.AgentExited) as caught:
        control.send(world.record.launch_id, "hi", actor="test", settings=world.settings)
    assert caught.value.exit_code == control.EXIT_EXITED == 4
    assert caught.value.exit_status == 2
    assert world.mux.events == []


def test_identity_check_is_one_seam(world, monkeypatch):
    calls = []
    monkeypatch.setattr(control, "_identity_matches", lambda record: calls.append(record) or False)
    with pytest.raises(control.Refused, match="stale launch"):
        control.verify(world.record.launch_id)
    assert [record.launch_id for record in calls] == [world.record.launch_id]


# send


def test_send_pastes_then_waits_then_presses_enter_separately(world, monkeypatch):
    order = []
    monkeypatch.setattr(control.time, "sleep", lambda seconds: order.append(("sleep", seconds)))
    original = world.mux._event
    monkeypatch.setattr(
        world.mux, "_event",
        lambda operation, payload: order.append((operation, payload)) or original(operation, payload),
    )
    text = "first line\nsecond line\n\tthird"

    control.send(world.record.launch_id, text, actor="test",
                 settings=control.Settings(send_delay_ms=150))

    assert order == [("paste", text), ("sleep", 0.15), ("keys", ("Enter",))]


def test_send_delay_comes_from_the_control_config(world, monkeypatch, tmp_path):
    config = paths.config_dir(create=True) / "config.toml"
    config.write_text("[control]\nsend_delay_ms = 40\n")
    sleeps = []
    monkeypatch.setattr(control.time, "sleep", sleeps.append)

    control.send(world.record.launch_id, "hello", actor="test")

    assert sleeps == [0.04]


def test_send_without_submit_only_pastes(world):
    control.send(world.record.launch_id, "draft", actor="test", submit=False,
                 settings=world.settings)
    assert [event[1] for event in world.mux.events] == ["paste"]


def test_empty_text_only_presses_enter(world):
    control.send(world.record.launch_id, "", actor="test", settings=world.settings)
    assert [event[1:] for event in world.mux.events] == [("keys", ("Enter",))]


@pytest.mark.parametrize(
    "text",
    [
        "x" * (control.MAX_SEND_BYTES + 1),
        "escape \x1b[201~ injected",
        "carriage\rreturn",
        "nul\0byte",
        "c1 \x9b control",
        "delete \x7f",
        b"bytes",
        "\ud800",
    ],
)
def test_send_refuses_text_it_cannot_paste_safely(world, text):
    with pytest.raises(control.ControlError) as caught:
        control.send(world.record.launch_id, text, actor="test", settings=world.settings)
    assert caught.value.exit_code == control.EXIT_ERROR
    assert world.mux.events == []
    assert audit_lines()[-1]["outcome"] == "error"


def test_send_accepts_exactly_64_kib(world):
    control.send(world.record.launch_id, "y" * control.MAX_SEND_BYTES, actor="test",
                 settings=world.settings)
    assert len(world.mux.events[0][2]) == control.MAX_SEND_BYTES


def test_a_multiplexer_failure_during_send_is_an_error(world):
    world.mux.io_error = MultiplexerError("zellij action paste failed")
    with pytest.raises(control.ControlError, match="send to launch") as caught:
        control.send(world.record.launch_id, "hello", actor="test", settings=world.settings)
    assert type(caught.value) is control.ControlError
    assert audit_lines()[-1]["outcome"] == "error"


def test_two_threads_sending_to_one_pane_are_serialized(world):
    world.mux.paste_delay = 0.05
    settings = control.Settings(send_delay_ms=30)
    barrier = threading.Barrier(2)
    errors = []

    def sender(text):
        barrier.wait()
        try:
            control.send(world.record.launch_id, text, actor="test", settings=settings)
        except Exception as error:  # pragma: no cover - reported below
            errors.append(error)

    threads = [
        threading.Thread(target=sender, args=(f"prompt {name}\nline two",), name=name)
        for name in ("A", "B")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert errors == []
    events = world.mux.events
    assert len(events) == 4
    first, second = events[0][0], events[2][0]
    assert {first, second} == {"A", "B"}
    assert events == [
        (first, "paste", f"prompt {first}\nline two"),
        (first, "keys", ("Enter",)),
        (second, "paste", f"prompt {second}\nline two"),
        (second, "keys", ("Enter",)),
    ]
    lock = control.lock_path(SESSION, "3")
    assert lock.parent == paths.runtime_dir() / control.LOCKS_DIR
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700


def test_sends_to_different_panes_do_not_share_a_lock(world):
    assert control.lock_path(SESSION, "3") != control.lock_path(SESSION, "4")
    assert control.lock_path(SESSION, "3") != control.lock_path("other", "3")


def test_a_held_lock_times_out_as_an_error(world, monkeypatch):
    monkeypatch.setattr(control, "LOCK_TIMEOUT", 0.05)
    record = world.record
    with control._pane_lock(record):
        def other():
            try:
                with control._pane_lock(record):
                    pass
            except control.ControlError as error:
                results.append(error)

        results = []
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(5)
    assert results and "timed out" in results[0].detail


# audit


def test_audit_lines_carry_no_text(world, monkeypatch):
    monkeypatch.setenv(control.CALLER_ENV, "a" * 32)
    secret = "very secret prompt text\nsecond secret line"

    control.send(world.record.launch_id, secret, actor="mcp:tester",
                 caller=control.caller_launch_id(), settings=world.settings)
    output = control.read(world.record.launch_id, 2, actor="cli")
    with pytest.raises(control.Refused):
        control.send("0" * 32, secret, actor="cli", settings=world.settings)

    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    raw = path.read_text()
    assert "secret" not in raw and "prompt>" not in raw and output not in raw
    lines = audit_lines()
    assert [line["action"] for line in lines] == ["send", "read", "send"]
    sent = lines[0]
    assert set(sent) == {"ts", "actor", "caller_launch_id", "launch_id", "action",
                         "bytes", "sha256", "outcome", "exit_code"}
    assert sent["actor"] == "mcp:tester"
    assert sent["caller_launch_id"] == "a" * 32
    assert sent["launch_id"] == world.record.launch_id
    assert sent["bytes"] == len(secret.encode())
    assert len(sent["sha256"]) == 64 and sent["outcome"] == "ok"
    assert lines[2]["outcome"] == "refused" and lines[2]["launch_id"] == "0" * 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_audit_never_logs_an_invalid_launch_id_or_actor(world):
    with pytest.raises(control.Refused):
        control.send("some prompt text", "x", actor="bad actor\n", settings=world.settings)
    line = audit_lines()[-1]
    assert line["launch_id"] is None and line["actor"] == "unknown"


def test_audit_log_rotates_by_size(world, monkeypatch):
    monkeypatch.setattr(control, "AUDIT_MAX_BYTES", 400)
    for _ in range(6):
        control.status(world.record.launch_id)
    directory = paths.state_dir() / control.AUDIT_DIR
    assert (directory / "control.jsonl.1").exists()
    assert (directory / control.AUDIT_FILE).stat().st_size < 400 + 300


# read


def test_read_returns_the_tail(world):
    assert control.read(world.record.launch_id, 2) == "line 2\nprompt>"
    assert world.mux.events[-1][1:] == ("capture", 2)


@pytest.mark.parametrize("lines", [0, 201, -3, 1.5, "10"])
def test_read_refuses_bad_line_counts(world, lines):
    with pytest.raises(control.ControlError, match="lines"):
        control.read(world.record.launch_id, lines)
    assert world.mux.events == []


# status and wait


def test_status_maps_hook_states(world):
    launch_id = world.record.launch_id
    assert control.status(launch_id).state == "quiet"
    for status, expected in (("running", "working"), ("waiting", "needs-you"),
                             ("idle", "done"), ("error", "failed"), ("ended", "ended")):
        world.hooks[:] = [hook_record(world.record, status)]
        assert control.status(launch_id).state == expected


def test_status_of_an_exited_pane_is_ended(world):
    world.mux.add_pane("3", exited=True, exit_status=0)
    current = control.status(world.record.launch_id)
    assert current.state == "ended" and current.exit_status == 0
    assert current.to_json()["profile"] == "r1"


def test_list_statuses_reports_refusals_as_rows(world):
    other = world.launch("6", pid=4006)
    world.processes.pop(4006)
    world.mux.add_pane("6")
    rows = {row.launch_id: row.state for row in control.list_statuses()}
    assert rows == {world.record.launch_id: "quiet", other.launch_id: "refused"}


@pytest.mark.parametrize(
    "until, state, code",
    [
        ("done", "done", 0), ("done", "needs-you", 3), ("done", "failed", 1),
        ("done", "ended", 4), ("done", "working", None), ("done", "quiet", None),
        ("needs-you", "needs-you", 0), ("needs-you", "done", None),
        ("needs-you", "ended", 4), ("ended", "ended", 0), ("ended", "done", None),
        ("any", "done", 0), ("any", "needs-you", 0), ("any", "failed", 0),
        ("any", "ended", 0), ("any", "working", None),
    ],
)
def test_wait_codes(until, state, code):
    assert control.wait_code(until, state) == code


def test_wait_ignores_the_previous_turn_after_a_send(world, monkeypatch):
    world.hooks[:] = [hook_record(world.record, "idle", arrival=5)]
    control.send(world.record.launch_id, "next task", actor="test", settings=world.settings)
    polls = []

    def sleep(seconds):
        polls.append(seconds)
        if len(polls) == 2:
            world.hooks[:] = [hook_record(world.record, "running", arrival=6)]
        if len(polls) == 4:
            world.hooks[:] = [hook_record(world.record, "idle", arrival=7)]

    monkeypatch.setattr(control.time, "sleep", sleep)
    result = control.wait(world.record.launch_id, "done", 60)

    assert result.reached and result.exit_code == 0
    assert result.status.state == "done"
    assert len(polls) == 4
    assert audit_lines()[-1]["action"] == "wait"


def test_wait_without_a_send_accepts_the_current_state(world):
    world.hooks[:] = [hook_record(world.record, "idle")]
    assert control.wait(world.record.launch_id, "done", 5).exit_code == 0


def test_wait_reports_blocked_and_exited(world, monkeypatch):
    world.hooks[:] = [hook_record(world.record, "waiting")]
    assert control.wait(world.record.launch_id, "done", 5).exit_code == control.EXIT_BLOCKED
    world.mux.add_pane("3", exited=True, exit_status=0)
    assert control.wait(world.record.launch_id, "done", 5).exit_code == control.EXIT_EXITED
    assert control.wait(world.record.launch_id, "ended", 5).exit_code == control.EXIT_OK


def test_wait_times_out(world, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    result = control.wait(world.record.launch_id, "done", 3)
    assert result.timed_out and result.exit_code == control.EXIT_TIMEOUT == 2
    assert audit_lines()[-1]["outcome"] == "timeout"


@pytest.mark.parametrize(
    "until, timeout", [("finished", 5), ("done", 0), ("done", 601), ("done", float("nan"))]
)
def test_wait_refuses_bad_arguments(world, until, timeout):
    with pytest.raises(control.ControlError):
        control.wait(world.record.launch_id, until, timeout)


def test_wait_on_a_refused_launch_exits_5(world):
    with pytest.raises(control.Refused):
        control.wait("0" * 32, "done", 5)


# settings


def test_settings_default_without_a_config():
    assert control.load_settings() == control.Settings(150, False)


@pytest.mark.parametrize(
    "body",
    ["[control]\nsend_delay_ms = -1\n", "[control]\nsend_delay_ms = 9000\n",
     "[control]\nsend_delay_ms = 1.5\n", "[control]\nmcp_enabled = 'yes'\n",
     "control = 3\n", "not toml ="],
)
def test_settings_refuse_bad_values(body):
    path = paths.config_dir(create=True) / "config.toml"
    path.write_text(body)
    with pytest.raises(control.ControlError):
        control.load_settings()


def test_settings_read_the_control_table():
    path = paths.config_dir(create=True) / "config.toml"
    path.write_text("[control]\nsend_delay_ms = 0\nmcp_enabled = true\n")
    assert control.load_settings() == control.Settings(0, True)


def test_caller_launch_id_is_validated():
    assert control.caller_launch_id({control.CALLER_ENV: "b" * 32}) == "b" * 32
    assert control.caller_launch_id({control.CALLER_ENV: "../x"}) is None
    assert control.caller_launch_id({}) is None


def test_control_runtime_files_stay_private(world):
    control.send(world.record.launch_id, "hi", actor="test", settings=world.settings)
    mark = paths.runtime_dir() / control.SENT_DIR / f"{world.record.launch_id}.json"
    assert stat.S_IMODE(mark.stat().st_mode) == 0o600
    assert "hi" not in mark.read_text()
    assert os.path.commonpath([mark, paths.state_dir()]) == str(paths.state_dir())


def test_unusable_audit_dir_only_warns(world, monkeypatch, capsys):
    def broken():
        raise control.ControlError("cannot use the audit log directory: denied")

    monkeypatch.setattr(control, "_audit_dir", broken)
    control.audit("send", actor="cli", launch_id=None, outcome="ok", exit_code=0, data=b"x")
    assert "control audit not written" in capsys.readouterr().err
