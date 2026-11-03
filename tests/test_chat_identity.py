"""Agent and maintainer identity for chat, with a fake process table."""

import time

import pytest

from tongs_agents import discovery, launches
from tongs_agents.chat import identity
from tongs_agents.chat.identity import IdentityError, grant_matches, verify_agent
from tongs_agents.chat.store import Grant

LAUNCH = "0123456789abcdef0123456789abcdef"
AGENT_PID = 4000
SERVER_PID = 4100
START = "4242"


class FakeProcess:
    def __init__(self, pid, start_token, ppid=None):
        self.pid = pid
        self.start_token = start_token
        self.ppid = ppid


def record(launch_id=LAUNCH, *, profile="lead", pid=AGENT_PID, start=START,
           state="running", replaces=None, harness="claude"):
    return launches._write(launches.LaunchRecord(
        launch_id=launch_id, profile=profile, harness=harness, multiplexer="zellij",
        session="agents", created=int(time.time()),
        pane_id="1" if state == "running" else None,
        pid=pid if state == "running" else None,
        process_start=start if state == "running" else None,
        state=state, replaces=replaces,
    ))


@pytest.fixture
def processes(monkeypatch):
    table = {
        1: FakeProcess(1, "1", 0),
        900: FakeProcess(900, "9", 1),
        AGENT_PID: FakeProcess(AGENT_PID, START, 900),
        SERVER_PID: FakeProcess(SERVER_PID, "77", AGENT_PID),
    }
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    return table


ENV = {identity.LAUNCH_ENV: LAUNCH, identity.PROFILE_ENV: "lead"}


def test_a_server_under_the_claimed_process_is_verified(processes):
    record()
    found = verify_agent(ENV, SERVER_PID)
    assert (found.launch_id, found.profile, found.process_start) == (LAUNCH, "lead", START)


@pytest.mark.parametrize("env,match", [
    ({}, "TONGS_AGENTS_LAUNCH_ID"),
    ({identity.LAUNCH_ENV: "X" * 32, identity.PROFILE_ENV: "lead"}, "LAUNCH_ID"),
    ({identity.LAUNCH_ENV: LAUNCH}, "PROFILE"),
    ({identity.LAUNCH_ENV: LAUNCH, identity.PROFILE_ENV: "Bad Name"}, "PROFILE"),
])
def test_step_one_checks_the_env(processes, env, match):
    record()
    with pytest.raises(IdentityError, match=match):
        verify_agent(env, SERVER_PID)


def test_step_two_needs_a_claimed_unreplaced_record_of_that_profile(processes):
    with pytest.raises(IdentityError, match="not spawned"):
        verify_agent(ENV, SERVER_PID)
    record(state="pending")
    with pytest.raises(IdentityError, match="not been claimed"):
        verify_agent(ENV, SERVER_PID)
    record(profile="other")
    with pytest.raises(IdentityError, match="not a launch of profile"):
        verify_agent(ENV, SERVER_PID)
    record()
    record("f" * 32, replaces=LAUNCH)
    with pytest.raises(IdentityError, match="replaced"):
        verify_agent(ENV, SERVER_PID)


def test_a_record_without_a_start_token_is_unverifiable(processes):
    record(start=None)
    with pytest.raises(IdentityError, match="no process start token"):
        verify_agent(ENV, SERVER_PID)


def test_the_claimed_pid_must_be_an_ancestor_with_its_start_token(processes):
    record()
    processes[5000] = FakeProcess(5000, "5", 900)
    with pytest.raises(IdentityError, match="does not run under"):
        verify_agent(ENV, 5000)
    processes[AGENT_PID] = FakeProcess(AGENT_PID, "reused", 900)
    with pytest.raises(IdentityError, match="does not run under"):
        verify_agent(ENV, SERVER_PID)
    del processes[AGENT_PID]
    with pytest.raises(IdentityError):
        verify_agent(ENV, SERVER_PID)


def test_a_grant_matches_only_its_process_start(processes):
    record()
    found = verify_agent(ENV, SERVER_PID)

    def grant(start, launch_id=LAUNCH):
        return Grant(launch_id, "lead", "chat", start, "agents", "1", None, 1, "cli")

    assert grant_matches(grant(START), found)
    assert not grant_matches(grant("older"), found)
    assert not grant_matches(grant(START, "e" * 32), found)
    assert not grant_matches(None, found)


def test_the_maintainer_is_outside_every_agent_tree(processes):
    processes[300] = FakeProcess(300, "3", 1)
    record()
    assert identity.maintainer_refusal({}, 300) is None
    assert "TONGS_AGENTS_LAUNCH_ID" in identity.maintainer_refusal(
        {identity.LAUNCH_ENV: "anything"}, 300
    )
    refusal = identity.maintainer_refusal({}, SERVER_PID)
    assert LAUNCH in refusal and "process tree" in refusal


def test_a_reused_agent_pid_does_not_refuse_the_maintainer(processes):
    record(start="older")
    assert identity.maintainer_refusal({}, SERVER_PID) is None
