"""The opt-in MCP agent control policy with a fake multiplexer and process table.

Launch records, the config file and the audit log are real files in the
private test dirs; no multiplexer, agent or model is started.
"""

import json
import os
import stat

import pytest

from tongs_agents import control, discovery, launches, mcp_control, paths
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


def test_the_rate_window_slides_and_persists(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    caller = "a" * 32
    mcp_control.take_rate(caller, "send", 1, now=1000.0)
    with pytest.raises(mcp_control.RateLimited):
        mcp_control.take_rate(caller, "send", 1, now=1059.0)
    mcp_control.take_rate(caller, "send", 1, now=1061.0)
    mcp_control.take_rate("b" * 32, "send", 1, now=1061.0)
    mcp_control.take_rate(caller, "spawn", 1, now=1061.0)
    directory = paths.runtime_dir() / mcp_control.RATE_DIR
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / f"{caller}.send.json").stat().st_mode) == 0o600


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


def test_spawn_falls_back_to_the_workspace_session(world):
    env = {control.CALLER_ENV: world.caller.launch_id}
    world.mux.states = {"agents": SESSION_RUNNING}
    assert world.tools(env).spawn("worker")["session"] == "agents"


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
