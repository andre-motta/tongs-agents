"""SessionFeed and the pure view helpers."""

import dataclasses

from tongs_agents import discovery
from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.sessions_feed import (
    SORT_KEYS,
    SessionFeed,
    attention_rows,
    filter_sessions,
    next_sort_key,
    search_sessions,
    sort_sessions,
    visible_sessions,
)


def _session(sid, repo, status=SessionStatus.RUNNING, *, agent="claude",
             detail="", attention=False, age=0):
    return FleetSession(
        session_id=sid, agent=agent, repo=repo, cwd=f"/tmp/{repo}",
        status=status, detail=detail, ts=0, started=0, pid="", terminal="",
        terminal_env={}, source="hook", tool="", age_seconds=age,
        needs_attention=attention,
    )


SESSIONS = [
    _session("a", "beta", SessionStatus.IDLE, agent="codex", age=10),
    _session("b", "alpha", SessionStatus.WAITING, detail="Approve rm", attention=True, age=5),
    _session("c", "gamma", SessionStatus.RUNNING, agent="pi", age=99),
]


def test_snapshot_parses_reader_records():
    feed = SessionFeed(reader=lambda: [
        {"session_id": "x", "repo": "r", "status": "waiting", "agent": "pi"},
    ])
    [session] = feed.snapshot()
    assert isinstance(session, FleetSession)
    assert session.status is SessionStatus.WAITING
    assert session.agent == "pi"


def test_default_reader_is_discovery_read_sessions_at_call_time(monkeypatch):
    calls = []
    monkeypatch.setattr(
        discovery, "read_sessions", lambda: calls.append(1) or [{"session_id": "y"}]
    )
    assert [s.session_id for s in SessionFeed().snapshot()] == ["y"]
    assert calls == [1]


def test_filter_by_status():
    assert [s.session_id for s in filter_sessions(SESSIONS, "waiting")] == ["b"]
    assert filter_sessions(SESSIONS, "") == SESSIONS
    assert filter_sessions(SESSIONS, "ended") == []


def test_search_matches_agent_repo_detail_and_status_case_insensitive():
    assert [s.session_id for s in search_sessions(SESSIONS, "CODEX")] == ["a"]
    assert [s.session_id for s in search_sessions(SESSIONS, "gam")] == ["c"]
    assert [s.session_id for s in search_sessions(SESSIONS, "approve")] == ["b"]
    assert [s.session_id for s in search_sessions(SESSIONS, "idle")] == ["a"]
    assert search_sessions(SESSIONS, "") == SESSIONS


def test_search_matches_profile_id_when_present():
    @dataclasses.dataclass(frozen=True)
    class Profiled(FleetSession):
        profile_id: str = ""

    profiled = Profiled(**{
        f.name: getattr(SESSIONS[0], f.name) for f in dataclasses.fields(FleetSession)
    }, profile_id="reviewer")
    assert search_sessions([profiled, SESSIONS[1]], "review") == [profiled]


def test_sort_keys():
    assert [s.repo for s in sort_sessions(SESSIONS, "repo")] == ["alpha", "beta", "gamma"]
    assert [s.repo for s in sort_sessions(SESSIONS, "age")] == ["gamma", "beta", "alpha"]
    assert sort_sessions(SESSIONS, "attention")[0].session_id == "b"
    assert sort_sessions(SESSIONS, "status")[0].status is SessionStatus.WAITING


def test_helpers_are_pure():
    before = list(SESSIONS)
    visible_sessions(SESSIONS, status="idle", query="beta", sort_key="age")
    sort_sessions(SESSIONS, "age")
    assert SESSIONS == before


def test_next_sort_key_cycles_and_recovers():
    assert [next_sort_key(k) for k in SORT_KEYS] == SORT_KEYS[1:] + SORT_KEYS[:1]
    assert next_sort_key("bogus") == SORT_KEYS[0]


def test_visible_sessions_combines_filter_search_sort():
    result = visible_sessions(SESSIONS, query="a", sort_key="repo")
    assert [s.repo for s in result] == ["alpha", "beta", "gamma"]
    assert visible_sessions(SESSIONS, status="running", query="beta") == []


def test_attention_rows():
    assert attention_rows(sort_sessions(SESSIONS, "repo")) == [0]
