"""The opt-in MCP agent control policy with a fake multiplexer and process table.

Launch records, the config file and the audit log are real files in the
private test dirs; no multiplexer, agent or model is started.
"""

import inspect
import json
import os
import stat
import threading

import pytest

from tongs_agents import control, discovery, launches, mcp_control, mcp_server, paths
from tongs_agents.multiplexers.base import (
    SESSION_EXITED,
    SESSION_RUNNING,
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
)
from tongs_agents.profiles import Profile

SESSION = "agents"
SOCKET_DIR = "/run/user/1000/zellij-test"
CALLER_PID = 4000
SERVER_PID = 4100
START = "4242"

CONFIG = """
[profiles.lead]
command = ["fake-lead"]
mcp_control = true

[profiles.worker]
command = ["fake-worker"]
mcp_control = true

[profiles.plain]
command = ["fake-plain"]

[control]
mcp_enabled = {enabled}
send_delay_ms = 0
mcp_any_target = {any_target}
mcp_rate_per_minute = {rate}
"""


class FakeProcess:
    def __init__(self, pid, start_token, ppid=None):
        self.pid = pid
        self.start_token = start_token
        self.ppid = ppid


class FakeMux:
    name = "zellij"

    def __init__(self):
        self.panes = {}
        self.events = []
        self.states = {SESSION: SESSION_RUNNING}
        self.spawned = []
        self.next_pane = 10

    def add_pane(self, pane_id, session=SESSION):
        self.panes[pane_id] = PaneInfo(
            ref=PaneRef("zellij", session, pane_id, SOCKET_DIR),
            title="agent", command="python", exited=False, focused=False,
        )

    def list_panes(self, session):
        return [pane for pane in self.panes.values() if pane.ref.session == session]

    def session_state(self, session):
        return self.states.get(session)

    def spawn(self, session, argv, *, name=None, focus=True, cwd=None):
        pane_id = str(self.next_pane)
        self.next_pane += 1
        self.spawned.append((session, list(argv), name, focus))
        return PaneRef("zellij", session, pane_id, SOCKET_DIR)

    def paste(self, ref, text):
        self.events.append(("paste", ref.pane_id, text))

    def send_keys(self, ref, keys):
        self.events.append(("keys", ref.pane_id, tuple(keys)))

    def capture(self, ref, lines=200):
        return "prompt>"


def write_config(enabled=True, any_target=False, rate=6):
    directory = paths.config_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(CONFIG.format(
        enabled=str(enabled).lower(), any_target=str(any_target).lower(), rate=rate,
    ))
    path.chmod(0o600)


def audit_lines():
    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def world(monkeypatch):
    """A claimed caller launch ``lead`` in pane 1 whose process runs this server."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    write_config()
    mux = FakeMux()
    processes = {}
    hooks = []

    def get_multiplexer(name="zellij", *, socket_dir=None):
        if name != "zellij":
            raise Unsupported(f"no multiplexer adapter named {name!r}")
        return mux

    monkeypatch.setattr(control, "get_multiplexer", get_multiplexer)
    monkeypatch.setattr(mcp_control, "get_multiplexer", get_multiplexer)
    monkeypatch.setattr(discovery, "get_process_info", processes.get)
    monkeypatch.setattr(discovery, "read_session_records", lambda: [dict(h) for h in hooks])

    class World:
        pass

    state = World()
    state.mux = mux
    state.processes = processes

    def launch(pane_id, *, profile="worker", pid=5000, start=START, spawned_by=None):
        pending = launches.create_pending(
            Profile(profile, "other", ("fake",)), SESSION, "zellij",
            spawned_by=spawned_by,
        )
        record = launches.claim(
            pending.launch_id, pid, start, PaneRef("zellij", SESSION, pane_id, SOCKET_DIR),
        )
        processes[pid] = FakeProcess(pid, start)
        mux.add_pane(pane_id)
        return record

    state.launch = launch
    state.caller = launch("1", profile="lead", pid=CALLER_PID)
    processes[SERVER_PID] = FakeProcess(SERVER_PID, "9", ppid=CALLER_PID)
    state.env = {
        control.CALLER_ENV: state.caller.launch_id,
        "ZELLIJ_SESSION_NAME": SESSION,
        "ZELLIJ_SOCKET_DIR": SOCKET_DIR,
    }

    def tools(env=None, pid=SERVER_PID):
        return mcp_control.McpControl(
            state.env if env is None else env, pid=pid, claim_timeout=0
        )

    state.tools = tools
    state.worker = launch("2", spawned_by=state.caller.launch_id)
    return state


def refused(call, outcome="refused", exit_code=control.EXIT_REFUSED):
    with pytest.raises(control.ControlError) as raised:
        call()
    assert (raised.value.outcome, raised.value.exit_code) == (outcome, exit_code)
    return raised.value.detail


def last_audit():
    return audit_lines()[-1]


def test_send_to_a_spawned_opted_in_pane_goes_through_control(world):
    result = world.tools().send(world.worker.launch_id, "hello\nworld")

    assert result == {"ok": True, "launch_id": world.worker.launch_id, "bytes": 11}
    assert world.mux.events == [("paste", "2", "hello\nworld"), ("keys", "2", ("Enter",))]
    entry = last_audit()
    assert entry["action"] == "send"
    assert entry["actor"] == f"mcp:{world.caller.launch_id}"
    assert entry["caller_launch_id"] == world.caller.launch_id
    assert entry["launch_id"] == world.worker.launch_id
    assert (entry["outcome"], entry["exit_code"], entry["bytes"]) == ("ok", 0, 11)
    assert "hello" not in json.dumps(audit_lines())


def test_status_and_wait_of_a_spawned_pane(world):
    status = world.tools().status(world.worker.launch_id)
    assert status["ok"] is True
    assert status["status"]["launch_id"] == world.worker.launch_id
    assert status["status"]["state"] == "quiet"

    world.mux.panes["2"] = PaneInfo(
        ref=PaneRef("zellij", SESSION, "2", SOCKET_DIR), title="agent",
        command="python", exited=True, focused=False, exit_status=0,
    )
    waited = world.tools().wait(world.worker.launch_id, "ended", 1)
    assert waited["ok"] is True and waited["reached"] is True
    assert waited["status"]["state"] == "ended"
    assert [entry["action"] for entry in audit_lines()] == ["status", "wait"]
    assert {entry["actor"] for entry in audit_lines()} == {f"mcp:{world.caller.launch_id}"}


def test_self_targeting_is_refused_and_audited(world):
    detail = refused(lambda: world.tools().send(world.caller.launch_id, "hi"))
    assert "caller's own launch" in detail
    assert world.mux.events == []
    entry = last_audit()
    assert (entry["action"], entry["outcome"], entry["exit_code"]) == ("send", "refused", 5)
    assert entry["launch_id"] == world.caller.launch_id


def test_a_launch_on_the_callers_own_pane_is_refused(world):
    twin = world.launch("1", pid=5100, spawned_by=world.caller.launch_id)
    assert "caller's own pane" in refused(lambda: world.tools().status(twin.launch_id))


def test_a_non_opted_target_profile_is_refused(world):
    plain = world.launch("3", profile="plain", spawned_by=world.caller.launch_id)
    detail = refused(lambda: world.tools().send(plain.launch_id, "hi"))
    assert "'plain' has not opted in" in detail
    assert world.mux.events == []
    assert last_audit()["outcome"] == "refused"


def test_a_non_opted_spawn_profile_is_refused(world):
    detail = refused(lambda: world.tools().spawn("plain"))
    assert "'plain' has not opted in" in detail
    assert world.mux.spawned == []
    entry = last_audit()
    assert (entry["action"], entry["outcome"], entry["launch_id"]) == ("spawn", "refused", None)


def test_an_unknown_or_invalid_spawn_profile_is_refused(world):
    assert "not a named profile" in refused(lambda: world.tools().spawn("missing"))
    assert "not a profile name" in refused(lambda: world.tools().spawn("../x"))
    assert world.mux.spawned == []


def test_a_non_opted_caller_is_refused(world):
    other = world.launch("4", profile="plain", pid=6000)
    world.processes[6100] = FakeProcess(6100, "9", ppid=6000)
    env = dict(world.env, **{control.CALLER_ENV: other.launch_id})
    detail = refused(lambda: world.tools(env, pid=6100).status(world.worker.launch_id))
    assert "caller profile 'plain'" in detail


def test_an_unverified_pane_is_refused_and_audited(world):
    del world.mux.panes["2"]
    detail = refused(lambda: world.tools().send(world.worker.launch_id, "hi"))
    assert "is not in session" in detail
    entry = last_audit()
    assert (entry["action"], entry["outcome"], entry["exit_code"]) == ("send", "refused", 5)
    assert entry["actor"] == f"mcp:{world.caller.launch_id}"


def test_a_launch_not_spawned_by_tongs_agents_is_refused(world):
    assert "not spawned by tongs-agents" in refused(
        lambda: world.tools().status("f" * 32)
    )
    assert "not a tongs-agents launch id" in refused(lambda: world.tools().status("x"))


def test_another_callers_pane_needs_mcp_any_target(world):
    stranger = world.launch("5")
    detail = refused(lambda: world.tools().status(stranger.launch_id))
    assert "not spawned by this caller" in detail

    write_config(any_target=True)
    assert world.tools().status(stranger.launch_id)["ok"] is True


def test_without_a_launch_id_every_tool_is_refused(world):
    env = {key: value for key, value in world.env.items() if key != control.CALLER_ENV}
    detail = refused(lambda: world.tools(env).spawn("worker"))
    assert "does not run in a pane tongs-agents launched" in detail
    entry = last_audit()
    assert (entry["actor"], entry["caller_launch_id"]) == ("mcp", None)


def test_a_copied_launch_id_outside_the_callers_process_is_refused(world):
    world.processes[7000] = FakeProcess(7000, "1", ppid=1)
    detail = refused(lambda: world.tools(pid=7000).send(world.worker.launch_id, "hi"))
    assert "does not run under the process" in detail
    assert world.mux.events == []


def test_a_stale_caller_is_refused(world):
    world.processes[CALLER_PID] = FakeProcess(CALLER_PID, "other")
    assert "caller:" in refused(lambda: world.tools().status(world.worker.launch_id))


def test_disabled_control_refuses_at_call_time(world):
    write_config(enabled=False)
    detail = refused(lambda: world.tools().send(world.worker.launch_id, "hi"))
    assert "mcp_enabled = false" in detail
    assert world.mux.events == []
    assert last_audit()["outcome"] == "refused"


def test_sends_are_rate_limited_per_caller(world):
    write_config(rate=2)
    tools = world.tools()
    tools.send(world.worker.launch_id, "one")
    tools.send(world.worker.launch_id, "two")
    detail = refused(
        lambda: tools.send(world.worker.launch_id, "three"), outcome="rate-limited",
    )
    assert "at most 2 sends per minute" in detail
    assert [event[2] for event in world.mux.events if event[0] == "paste"] == ["one", "two"]
    entry = last_audit()
    assert (entry["action"], entry["outcome"], entry["exit_code"]) == (
        "send", "rate-limited", 5,
    )
    assert world.tools().status(world.worker.launch_id)["ok"] is True


def take_rate(caller, action, limit, now):
    with mcp_control.rate_slot(caller, action, limit, now):
        pass


def test_the_rate_window_slides_and_persists(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    caller = "a" * 32
    take_rate(caller, "send", 1, now=1000.0)
    with pytest.raises(mcp_control.RateLimited):
        take_rate(caller, "send", 1, now=1059.0)
    take_rate(caller, "send", 1, now=1061.0)
    take_rate("b" * 32, "send", 1, now=1061.0)
    take_rate(caller, "spawn", 1, now=1061.0)
    directory = paths.runtime_dir() / mcp_control.RATE_DIR
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / f"{caller}.send.json").stat().st_mode) == 0o600


def test_a_call_that_fails_in_the_rate_slot_is_not_counted(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    caller = "c" * 32
    with pytest.raises(control.Refused):
        with mcp_control.rate_slot(caller, "send", 1, now=1000.0):
            raise control.Refused("refused inside")
    take_rate(caller, "send", 1, now=1000.0)
    with pytest.raises(mcp_control.RateLimited):
        take_rate(caller, "send", 1, now=1001.0)


def test_a_rate_slot_does_not_hold_the_lock_while_the_call_runs(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(control, "LOCK_TIMEOUT", 0.2)
    caller = "d" * 32
    with mcp_control.rate_slot(caller, "send", 2, now=1000.0):
        with pytest.raises(mcp_control.RateLimited):
            with mcp_control.rate_slot(caller, "send", 1, now=1000.0):
                pass
        take_rate(caller, "send", 2, now=1000.0)
        with pytest.raises(mcp_control.RateLimited):
            take_rate(caller, "send", 2, now=1000.0)


def test_parallel_calls_of_one_caller_run_together(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(control, "LOCK_TIMEOUT", 2.0)
    caller = "e" * 32
    both_inside = threading.Barrier(2, timeout=2)
    errors = []

    def call():
        try:
            with mcp_control.rate_slot(caller, "send", 2):
                both_inside.wait()
        except Exception as error:
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with pytest.raises(mcp_control.RateLimited):
        take_rate(caller, "send", 2, now=None)


def test_a_failed_call_gives_back_only_its_own_reservation(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    caller = "f" * 32
    with mcp_control.rate_slot(caller, "send", 3, now=1000.0):
        with pytest.raises(control.Refused):
            with mcp_control.rate_slot(caller, "send", 3, now=1001.0):
                raise control.Refused("refused inside")
    _, path = mcp_control._rate_path(caller, "send")
    assert mcp_control._read_stamps(path) == [1000.0]


def test_a_send_refused_by_control_does_not_use_the_rate_limit(world):
    write_config(rate=1)
    pane = world.mux.panes.pop("2")
    assert "is not in session" in refused(
        lambda: world.tools().send(world.worker.launch_id, "lost")
    )
    assert [entry["outcome"] for entry in audit_lines()] == ["refused"]

    world.mux.panes["2"] = pane
    assert world.tools().send(world.worker.launch_id, "kept")["ok"] is True
    refused(lambda: world.tools().send(world.worker.launch_id, "more"),
            outcome="rate-limited")
    assert [event[2] for event in world.mux.events if event[0] == "paste"] == ["kept"]


def test_a_failed_spawn_does_not_use_the_rate_limit(world, monkeypatch):
    write_config(rate=1)
    spawn = world.mux.spawn

    def broken(*args, **kwargs):
        raise MultiplexerError("pane did not start")

    monkeypatch.setattr(world.mux, "spawn", broken)
    refused(lambda: world.tools().spawn("worker"), outcome="error", exit_code=1)
    monkeypatch.setattr(world.mux, "spawn", spawn)
    assert world.tools().spawn("worker")["ok"] is True
    refused(lambda: world.tools().spawn("worker"), outcome="rate-limited")


def test_a_cancelled_wait_ends_and_is_audited(world, monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(control.time, "sleep", lambda seconds: None)
    refused(lambda: world.tools().wait(world.worker.launch_id, "done", 600, stop=stop),
            outcome="cancelled", exit_code=control.EXIT_ERROR)
    entry = last_audit()
    assert (entry["action"], entry["outcome"]) == ("wait", "cancelled")


def fake_clock(monkeypatch, on_sleep=None):
    clock = [1000.0]
    polls = []

    def sleep(seconds):
        clock[0] += seconds
        polls.append(seconds)
        if on_sleep is not None:
            on_sleep(len(polls))

    monkeypatch.setattr(mcp_control.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(control.time, "sleep", sleep)
    return polls


def count_caller_checks(monkeypatch):
    checks = []
    real = mcp_control.McpControl._caller

    def counted(self, loaded):
        checks.append(1)
        return real(self, loaded)

    monkeypatch.setattr(mcp_control.McpControl, "_caller", counted)
    return checks


def test_a_wait_ends_after_two_failed_caller_verifications(world, monkeypatch):
    def replace_caller(polls):
        if polls == 2:
            world.processes[CALLER_PID] = FakeProcess(CALLER_PID, "replaced")

    polls = fake_clock(monkeypatch, replace_caller)
    detail = refused(lambda: world.tools().wait(world.worker.launch_id, "done", 600))
    assert "caller:" in detail
    per_check = int(mcp_control.VERIFY_INTERVAL / control.WAIT_POLL)
    assert len(polls) == mcp_control.VERIFY_FAILURES * per_check
    assert (last_audit()["action"], last_audit()["outcome"]) == ("wait", "refused")


def test_a_wait_reverifies_the_caller_only_every_interval(world, monkeypatch):
    polls = fake_clock(monkeypatch)
    checks = count_caller_checks(monkeypatch)
    waited = world.tools().wait(world.worker.launch_id, "done", 20)
    assert waited["timed_out"] is True
    assert len(polls) == 40
    assert len(checks) == 1 + 4


def test_one_transient_verification_error_does_not_end_a_wait(world, monkeypatch):
    verify = control.verify
    calls = []

    def flaky(launch_id):
        if launch_id == world.caller.launch_id:
            calls.append(launch_id)
            if len(calls) == 2:
                raise control.Refused("cannot list session 'agents': server busy")
        return verify(launch_id)

    monkeypatch.setattr(control, "verify", flaky)

    def end_worker(polls):
        if polls == 25:
            world.mux.panes["2"] = PaneInfo(
                ref=world.mux.panes["2"].ref, title="agent", command="python",
                exited=True, focused=False,
            )

    fake_clock(monkeypatch, end_worker)
    waited = world.tools().wait(world.worker.launch_id, "ended", 600)
    assert waited["reached"] is True
    assert len(calls) == 3


def test_a_cancelled_wait_ends_at_the_next_poll_without_reverifying(world, monkeypatch):
    stop = threading.Event()
    polls = fake_clock(monkeypatch, lambda polls: polls == 3 and stop.set())
    checks = count_caller_checks(monkeypatch)
    refused(lambda: world.tools().wait(world.worker.launch_id, "done", 600, stop=stop),
            outcome="cancelled", exit_code=control.EXIT_ERROR)
    assert len(polls) == 3
    assert len(checks) == 1


def test_the_same_pane_id_on_another_socket_is_not_the_callers_pane(world, monkeypatch):
    other_socket = "/run/user/1000/zellij-other"
    other = FakeMux()
    other.panes["1"] = PaneInfo(
        ref=PaneRef("zellij", SESSION, "1", other_socket),
        title="agent", command="python", exited=False, focused=False,
    )
    monkeypatch.setattr(
        control, "get_multiplexer",
        lambda name="zellij", *, socket_dir=None: (
            other if socket_dir == other_socket else world.mux
        ),
    )
    pending = launches.create_pending(
        Profile("worker", "other", ("fake",)), SESSION, "zellij",
        spawned_by=world.caller.launch_id,
    )
    twin = launches.claim(
        pending.launch_id, 5200, START, PaneRef("zellij", SESSION, "1", other_socket),
    )
    world.processes[5200] = FakeProcess(5200, START)
    assert world.tools().status(twin.launch_id)["ok"] is True


def test_the_own_pane_check_uses_the_callers_record_not_the_server_env(world):
    twin = world.launch("1", pid=5100, spawned_by=world.caller.launch_id)
    env = {
        **world.env,
        "ZELLIJ_SESSION_NAME": "elsewhere",
        "ZELLIJ_SOCKET_DIR": "/run/user/1000/zellij-other",
        "ZELLIJ_PANE_ID": "9",
    }
    assert "caller's own pane" in refused(lambda: world.tools(env).status(twin.launch_id))


def test_spawn_starts_the_launcher_in_the_running_session(world):
    result = world.tools().spawn("worker")

    assert result["ok"] is True
    assert (result["profile"], result["session"], result["state"]) == (
        "worker", SESSION, "pending",
    )
    record = launches.get(result["launch_id"])
    assert record.spawned_by == world.caller.launch_id
    assert record.to_json()["spawned_by"] == world.caller.launch_id
    [(session, argv, name, focus)] = world.mux.spawned
    assert (session, name, focus) == (SESSION, "worker", False)
    assert argv[-4:] == ["tongs_agents.launch", "worker", "--launch-id", record.launch_id]
    entry = last_audit()
    assert (entry["action"], entry["outcome"], entry["launch_id"]) == (
        "spawn", "ok", record.launch_id,
    )


def test_spawn_returns_placement_notices(world, monkeypatch):
    world.mux.take_notices = lambda: ["the agent stack is gone"]
    result = world.tools().spawn("worker")
    assert result["ok"] is True and result["notices"] == ["the agent stack is gone"]


def test_spawn_uses_the_callers_session_not_the_server_env(world):
    env = {**world.env, "ZELLIJ_SESSION_NAME": "elsewhere", "ZELLIJ_SOCKET_DIR": "/x"}
    world.mux.states = {SESSION: SESSION_RUNNING, "elsewhere": SESSION_RUNNING}
    assert world.tools(env).spawn("worker")["session"] == SESSION
    assert [session for session, *_ in world.mux.spawned] == [SESSION]


def test_spawn_uses_the_callers_multiplexer_session_and_socket(world, monkeypatch):
    """A lead in a tmux workspace with no [workspace] config spawns into tmux."""
    tmux_socket = "/tmp/tmux-1000/work"
    mux = FakeMux()
    mux.name = "tmux"
    mux.states = {"work": SESSION_RUNNING}
    mux.panes["%1"] = PaneInfo(
        ref=PaneRef("tmux", "work", "%1", tmux_socket),
        title="agent", command="python", exited=False, focused=False,
    )
    asked = []

    def get_multiplexer(name="zellij", *, socket_dir=None):
        asked.append((name, socket_dir))
        if name == "tmux":
            return mux
        return world.mux

    monkeypatch.setattr(control, "get_multiplexer", get_multiplexer)
    monkeypatch.setattr(mcp_control, "get_multiplexer", get_multiplexer)
    pending = launches.create_pending(Profile("lead", "other", ("fake",)), "work", "tmux")
    lead = launches.claim(
        pending.launch_id, 7000, START, PaneRef("tmux", "work", "%1", tmux_socket)
    )
    world.processes[7000] = FakeProcess(7000, START)
    world.processes[7100] = FakeProcess(7100, "9", ppid=7000)
    env = {control.CALLER_ENV: lead.launch_id, "TMUX": f"{tmux_socket},1,0"}

    result = world.tools(env, pid=7100).spawn("worker")

    assert result["session"] == "work"
    assert [session for session, *_ in mux.spawned] == ["work"]
    assert world.mux.spawned == []
    assert set(asked) == {("tmux", tmux_socket)}
    assert launches.get(result["launch_id"]).multiplexer == "tmux"


def test_spawn_refuses_a_session_that_is_not_running(world):
    world.mux.states = {SESSION: SESSION_EXITED}
    assert "is not running" in refused(lambda: world.tools().spawn("worker"))
    world.mux.states = {}
    assert "is not running" in refused(lambda: world.tools().spawn("worker"))
    assert world.mux.spawned == []
    assert {r.launch_id for r in launches.list_records()} == {
        world.caller.launch_id, world.worker.launch_id,
    }


def test_a_failed_spawn_discards_its_record_and_is_audited(world, monkeypatch):
    def broken(*args, **kwargs):
        raise MultiplexerError("pane did not start")

    monkeypatch.setattr(world.mux, "spawn", broken)
    detail = refused(lambda: world.tools().spawn("worker"), outcome="error", exit_code=1)
    assert "pane did not start" in detail
    assert {r.launch_id for r in launches.list_records()} == {
        world.caller.launch_id, world.worker.launch_id,
    }
    assert last_audit()["outcome"] == "error"


def test_spawns_are_rate_limited(world):
    write_config(rate=1)
    world.tools().spawn("worker")
    refused(lambda: world.tools().spawn("worker"), outcome="rate-limited")
    assert len(world.mux.spawned) == 1


def test_a_spawned_pane_can_then_be_driven(world):
    launch_id = world.tools().spawn("worker")["launch_id"]
    launches.claim(launch_id, 8000, START, PaneRef("zellij", SESSION, "10", SOCKET_DIR))
    world.processes[8000] = FakeProcess(8000, START)
    world.mux.add_pane("10")
    world.tools().send(launch_id, "go")
    assert world.mux.events[-2:] == [("paste", "10", "go"), ("keys", "10", ("Enter",))]


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_the_audit_file_is_private(world):
    refused(lambda: world.tools().send(world.caller.launch_id, "hi"))
    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_an_invalid_config_is_an_audited_error(world):
    path = paths.config_dir() / "config.toml"
    path.write_text("[control\n")
    refused(lambda: world.tools().status(world.worker.launch_id), outcome="error",
            exit_code=1)
    assert last_audit()["outcome"] == "error"


def test_an_agent_spawned_through_mcp_cannot_spawn_more(world):
    world.processes[6000] = FakeProcess(6000, "9", ppid=5000)
    env = {**world.env, control.CALLER_ENV: world.worker.launch_id}
    detail = refused(lambda: world.tools(env, pid=6000).spawn("worker"))
    assert "mcp_nested_spawn" in detail
    assert world.mux.spawned == []


def test_mcp_spawn_takes_no_folder_or_arguments(world):
    assert list(inspect.signature(mcp_server.agents_spawn).parameters) == ["profile"]
    assert list(inspect.signature(mcp_control.McpControl.spawn).parameters) == [
        "self", "profile_name",
    ]
    result = world.tools().spawn("worker")
    record = launches.get(result["launch_id"])
    assert (record.folder, record.args) == (None, ())
    assert "folder" not in record.to_json() and "args" not in record.to_json()
