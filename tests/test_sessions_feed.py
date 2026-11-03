"""SessionFeed and the pure view helpers."""

import dataclasses

import pytest

from tongs_agents import discovery, hook
from tongs_agents.launches import LaunchRecord
from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.multiplexers.base import PaneInfo, PaneRef
from tongs_agents.sessions_feed import (
    LAUNCH_SOURCE,
    NOTIFY_SOURCE,
    SORT_KEYS,
    SessionFeed,
    WorkspaceFeed,
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
    profiled = dataclasses.replace(SESSIONS[0], profile_id="reviewer")
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


TMUX_SOCKET = "/tmp/tmux-1000/work"
LAUNCH_ID = "a" * 32


class _TmuxMux:
    name = "tmux"

    def list_panes(self, session):
        return [PaneInfo(PaneRef("tmux", session, "%1", TMUX_SOCKET), "agent", "cmd",
                         False, False, None)]


def _tmux_launch(*, state="running", socket_dir=TMUX_SOCKET, session="work"):
    return LaunchRecord(
        launch_id=LAUNCH_ID, profile="tests", harness="claude", multiplexer="tmux",
        session=session, created=100, socket_dir=socket_dir, pane_id="%1",
        pid=4242 if state == "running" else None,
        process_start="77" if state == "running" else None, state=state,
    )


def _tmux_hook(*, pid="4242", start="77", env=None, source="hook", status="idle"):
    return {
        "session_id": "sid", "agent": "claude", "repo": "repo", "cwd": "/w/repo",
        "status": status, "detail": "finished", "ts": 100, "pid": pid,
        "process_start": start, "terminal": "tmux",
        "terminal_env": {"TMUX": f"{TMUX_SOCKET},99,0"} if env is None else env,
        "launch": {"launch_id": LAUNCH_ID, "profile_id": "tests"}, "source": source,
    }


def _tmux_feed(hooks, launch):
    return WorkspaceFeed(
        "work", _TmuxMux(), lambda: [dict(hook) for hook in hooks],
        launch_reader=lambda session: [launch], alive=lambda record: True,
        clock=lambda: 200, socket_dir=TMUX_SOCKET,
    )


@pytest.mark.parametrize("source", ["hook", NOTIFY_SOURCE])
def test_tmux_hook_and_notify_rows_join_their_launch(source):
    rows = _tmux_feed(
        [_tmux_hook(source=source, status="waiting")], _tmux_launch()
    ).snapshot()
    assert [(row.source, row.status, row.launch_id) for row in rows] == [
        (source, SessionStatus.WAITING, LAUNCH_ID)
    ]


def test_tmux_hook_row_from_a_child_process_joins_its_launch(monkeypatch):
    monkeypatch.setattr(
        discovery, "get_process_info",
        lambda pid: discovery.ProcessInfo(pid, 4242, "claude", "claude", (), None, "88"),
    )
    monkeypatch.setattr(hook, "process_descends_from", lambda process, pid, start: True)
    rows = _tmux_feed([_tmux_hook(pid="5000", start="88")], _tmux_launch()).snapshot()
    assert [row.source for row in rows] == ["hook"]


@pytest.mark.parametrize(
    ("hook_fields", "launch_fields"),
    [
        ({"pid": "4242", "start": "99"}, {}),
        ({"pid": "5000"}, {}),
        ({}, {"state": "pending"}),
        ({}, {"socket_dir": "/tmp/tmux-1000/other"}),
        ({"env": {"ZELLIJ_SESSION_NAME": "scratch"}}, {}),
        ({}, {"session": "elsewhere"}),
    ],
    ids=["start-mismatch", "unrelated-pid", "pending", "other-socket",
         "zellij-session", "other-session"],
)
def test_tmux_hook_rows_that_do_not_prove_their_launch_are_dropped(
    monkeypatch, hook_fields, launch_fields
):
    monkeypatch.setattr(discovery, "get_process_info", lambda pid: None)
    rows = _tmux_feed(
        [_tmux_hook(**hook_fields)], _tmux_launch(**launch_fields)
    ).snapshot()
    assert all(row.source == LAUNCH_SOURCE for row in rows)
