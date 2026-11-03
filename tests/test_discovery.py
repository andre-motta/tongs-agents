import json
import sys
import time
from types import SimpleNamespace

import pytest

from tests.conftest import write_session


@pytest.fixture
def no_maintenance(monkeypatch):
    """read_sessions must never discover processes or clean up."""
    def refuse():
        raise AssertionError("read_sessions ran store maintenance")

    monkeypatch.setattr("tongs_agents.discovery.discover_processes", refuse)
    monkeypatch.setattr("tongs_agents.discovery._cleanup_stale_sessions", refuse)


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the pgrep path")
def test_find_agent_processes_includes_claude_and_codex(monkeypatch):
    def fake_run(command, **kwargs):
        pid = "101\n" if command[-1] == "claude" else "202\n"
        return SimpleNamespace(returncode=0, stdout=pid)

    monkeypatch.setattr("tongs_agents.discovery.subprocess.run", fake_run)
    from tongs_agents.discovery import _find_agent_processes
    assert _find_agent_processes() == [("claude", 101), ("codex", 202)]


def test_read_sessions_empty(fleet_dir, no_maintenance):
    from tongs_agents.discovery import read_sessions
    assert read_sessions() == []


def test_read_sessions_returns_hook_sessions(fleet_dir, no_maintenance):
    write_session(fleet_dir, "abc-123", "myrepo", "/tmp/myrepo", pid="99999")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert len(sessions) == 1
    assert sessions[0]["repo"] == "myrepo"
    assert "age_seconds" in sessions[0]


def test_read_sessions_dedup_proc_vs_hook(fleet_dir, no_maintenance):
    write_session(fleet_dir, "abc-123", "myrepo", "/tmp/myrepo", pid="12345")
    write_session(fleet_dir, "proc-12345", "myrepo", "/tmp/myrepo", source="process")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "abc-123"


def test_read_sessions_keeps_distinct_sessions_with_same_pid(fleet_dir, no_maintenance):
    now = int(time.time())
    write_session(fleet_dir, "old-session", "myrepo", "/tmp/myrepo", pid="12345", ts=now - 100)
    write_session(fleet_dir, "new-session", "myrepo", "/tmp/myrepo", pid="12345", ts=now)
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert {session["session_id"] for session in sessions} == {
        "old-session", "new-session"
    }


def test_read_sessions_proc_shows_when_no_hook(fleet_dir, no_maintenance):
    write_session(fleet_dir, "proc-99999", "otherrepo", "/tmp/otherrepo", source="process")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "proc-99999"


def test_read_sessions_keeps_codex_process_with_claude_hook(fleet_dir, no_maintenance):
    write_session(fleet_dir, "claude-1", "myrepo", "/tmp/myrepo",
                  pid="11111", agent="claude")
    write_session(fleet_dir, "proc-22222", "myrepo", "/tmp/myrepo",
                  source="process", agent="codex")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert {session["agent"] for session in sessions} == {"claude", "codex"}


def test_needs_attention_idle_over_2min(fleet_dir, no_maintenance):
    now = int(time.time())
    write_session(fleet_dir, "idle-old", "myrepo", "/tmp/myrepo",
                  status="idle", ts=now - 200, pid="99999")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert sessions[0]["needs_attention"] is True


def test_needs_attention_waiting_immediate(fleet_dir, no_maintenance):
    write_session(fleet_dir, "waiting-sess", "myrepo", "/tmp/myrepo",
                  status="waiting", pid="99999")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert sessions[0]["needs_attention"] is True


def test_needs_attention_running_false(fleet_dir, no_maintenance):
    write_session(fleet_dir, "running-sess", "myrepo", "/tmp/myrepo",
                  status="running", pid="99999")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert sessions[0]["needs_attention"] is False


def test_cleanup_removes_ended_sessions(fleet_dir):
    now = int(time.time())
    write_session(fleet_dir, "ended-old", "myrepo", "/tmp/myrepo",
                  status="ended", ts=now - 400, pid="99999")
    from tongs_agents.discovery import _cleanup_stale_sessions
    _cleanup_stale_sessions()
    assert not (fleet_dir / "ended-old.json").exists()


def test_cleanup_keeps_recent_ended(fleet_dir):
    write_session(fleet_dir, "ended-new", "myrepo", "/tmp/myrepo",
                  status="ended", pid="")
    from tongs_agents.discovery import _cleanup_stale_sessions
    _cleanup_stale_sessions()
    assert (fleet_dir / "ended-new.json").exists()


def test_cleanup_removes_no_pid_old(fleet_dir):
    now = int(time.time())
    write_session(fleet_dir, "nopid-old", "myrepo", "/tmp/myrepo",
                  status="started", ts=now - 700, pid="")
    from tongs_agents.discovery import _cleanup_stale_sessions
    _cleanup_stale_sessions()
    assert not (fleet_dir / "nopid-old.json").exists()


def test_sessions_sorted_by_repo(fleet_dir, no_maintenance):
    write_session(fleet_dir, "z-sess", "zrepo", "/tmp/zrepo", pid="11111")
    write_session(fleet_dir, "a-sess", "arepo", "/tmp/arepo", pid="22222")
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    assert sessions[0]["repo"] == "arepo"
    assert sessions[1]["repo"] == "zrepo"
