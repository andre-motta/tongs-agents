"""Grant, revoke and stale grants, with a mocked pane check and process table."""

import json

import pytest

from tests.test_chat_identity import AGENT_PID, LAUNCH, START, FakeProcess, record
from tongs_agents import control, discovery, paths, pi_install
from tongs_agents.chat import grants
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import ChatError, ChatRefused, ChatStore
from tongs_agents.multiplexers.base import PaneRef

OTHER = "f" * 32


@pytest.fixture
def world(monkeypatch):
    table = {AGENT_PID: FakeProcess(AGENT_PID, START, 1), 4001: FakeProcess(4001, "5", 1)}
    verified = {}
    monkeypatch.setattr(discovery, "get_process_info", table.get)

    def verify(launch_id):
        if launch_id not in verified:
            raise control.Refused(f"launch {launch_id}: pane 1 is not in session 'agents'")
        return verified[launch_id]

    monkeypatch.setattr(control, "verify", verify)

    class World:
        processes = table
        panes = verified

    def launch(launch_id=LAUNCH, profile="lead", pid=AGENT_PID, start=START, pane="1",
               harness="claude"):
        made = record(launch_id, profile=profile, pid=pid, start=start, harness=harness)
        verified[launch_id] = PaneRef("zellij", "agents", pane, "/run/zj")
        return made

    World.launch = staticmethod(launch)
    return World


def audit():
    path = paths.state_dir() / "audit" / "chat.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_prepare_shows_the_pane_and_apply_creates_the_file(world):
    world.launch()
    request = grants.prepare(LAUNCH)
    assert (request.handle, request.level, request.pane_id) == ("lead", "chat", "1")
    assert "pane agents:1" in request.describe() and LAUNCH in request.describe()
    assert not chat_store.exists()
    grant = grants.apply(request, via="cli")
    assert (grant.handle, grant.process_start, grant.granted_via, grant.socket_dir) == (
        "lead", START, "cli", "/run/zj",
    )
    assert chat_store.exists()
    assert [(entry["action"], entry["outcome"]) for entry in audit()] == [("grant", "ok")]


def test_wake_and_a_chosen_handle(world):
    world.launch()
    grant = grants.apply(grants.prepare(LAUNCH, wake=True, handle="boss"), via="sidebar")
    assert (grant.handle, grant.level, grant.granted_via) == ("boss", "chat+wake", "sidebar")
    assert grants.prepare(LAUNCH).handle == "boss"


def test_default_handles_count_up_from_the_profile_name(world):
    world.launch()
    world.launch(OTHER, pid=4001, start="5", pane="2")
    grants.apply(grants.prepare(LAUNCH), via="cli")
    second = grants.prepare(OTHER)
    assert second.handle == "lead-2"
    with pytest.raises(ChatRefused, match="already granted"):
        grants.prepare(OTHER, handle="lead")
    for bad in ("maintainer", "Bad", "x" * 33):
        with pytest.raises(ChatError, match="invalid handle"):
            grants.prepare(OTHER, handle=bad)
    assert grants.default_handle("my_profile", {}, LAUNCH) == "my-profile"
    assert grants.default_handle("maintainer", {}, LAUNCH) == "agent"
    long = "p" * 32
    assert grants.default_handle(long, {long: OTHER}, LAUNCH) == "p" * 30 + "-2"


def test_refusals_are_audited(world):
    with pytest.raises(ChatRefused, match="not a tongs-agents launch id"):
        grants.prepare("nope")
    with pytest.raises(ChatRefused, match="not spawned"):
        grants.prepare(LAUNCH)
    record(LAUNCH, start=None)
    with pytest.raises(ChatRefused, match="no process start token"):
        grants.prepare(LAUNCH)
    record(LAUNCH)
    with pytest.raises(ChatRefused, match="pane check failed"):
        grants.prepare(LAUNCH)
    assert {entry["outcome"] for entry in audit()} == {"refused"}
    assert len(audit()) == 4


def test_apply_checks_the_launch_again(world):
    world.launch()
    request = grants.prepare(LAUNCH)
    world.launch(start="9999")
    world.processes[AGENT_PID] = FakeProcess(AGENT_PID, "9999", 1)
    with pytest.raises(ChatRefused, match="changed since"):
        grants.apply(request, via="cli")
    assert not chat_store.exists()


def test_pi_needs_native_mcp(world, monkeypatch):
    config = paths.config_dir()
    config.mkdir(parents=True, mode=0o700)
    (config / "config.toml").write_text('[profiles.pi]\ncommand = ["pi"]\n')
    (config / "config.toml").chmod(0o600)
    world.launch(profile="pi", harness="pi")
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 98, 2))
    with pytest.raises(ChatRefused, match="0.99.0"):
        grants.prepare(LAUNCH)
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: None)
    assert "cannot read the Pi version" in grants.prepare(LAUNCH).notes[0]
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 99, 0))
    assert grants.prepare(LAUNCH).notes == ()


def test_revoke_is_audited_and_reports_absence(world):
    world.launch()
    assert grants.revoke(LAUNCH) is False
    grants.apply(grants.prepare(LAUNCH), via="cli")
    assert grants.revoke(LAUNCH) is True
    with ChatStore.maintainer() as current:
        assert current.grants() == []
    assert [(e["action"], e["outcome"]) for e in audit()] == [
        ("revoke", "absent"), ("grant", "ok"), ("revoke", "ok"),
    ]


def test_grant_state_and_stale_removal(world):
    world.launch()
    world.launch(OTHER, pid=4001, start="5", pane="2")
    first = grants.apply(grants.prepare(LAUNCH), via="cli")
    grants.apply(grants.prepare(OTHER), via="cli")
    assert grants.grant_state(first) == grants.STATE_PENDING
    with ChatStore.agent(LAUNCH) as agent:
        agent.record_tools_listed(now=first.granted_at + 1)
    with ChatStore.maintainer() as current:
        assert grants.grant_state(current.grant_for(LAUNCH)) == grants.STATE_ACTIVE
    del world.processes[4001]
    with ChatStore.maintainer() as current:
        state = grants.grant_state(current.grant_for(OTHER))
    assert state == "stale: the agent process ended"
    removed = grants.remove_stale()
    assert [grant.launch_id for grant in removed] == [OTHER]
    with ChatStore.maintainer() as current:
        assert [grant.launch_id for grant in current.grants()] == [LAUNCH]
    assert audit()[-1]["action"] == "expire"
