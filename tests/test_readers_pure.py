"""Every session reader only reads: no writes, unlinks, discovery or cleanup."""

import builtins
import io
import json
import os
import stat
import time
from pathlib import Path

import pytest

from tongs_agents import cli, discovery, focus, mcp_server, paths, session_store
from tongs_agents.discovery import annotate_session
from tongs_agents.sessions_feed import SessionFeed
from tests.conftest import write_session
from tests.test_harness_contract import make_record

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _inside(path, root: Path) -> bool:
    try:
        resolved = os.path.abspath(os.fspath(path))
    except TypeError:
        return False
    return os.path.commonpath([resolved, str(root)]) == str(root)


def _refuse(name):
    def refused(*args, **kwargs):
        raise AssertionError(f"a reader called {name}")
    return refused


@pytest.fixture
def forbid_writes(monkeypatch):
    """Fail any write, unlink, rename or mkdir under the state dir, and any
    process discovery or cleanup, then make the store read-only on disk."""
    root = Path(os.path.abspath(paths.state_dir()))
    real_os_open = os.open
    real_open = builtins.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & WRITE_FLAGS and _inside(path, root):
            raise AssertionError(f"a reader opened {path} for writing")
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+") and _inside(file, root):
            raise AssertionError(f"a reader opened {file} for writing")
        return real_open(file, mode, *args, **kwargs)

    def guarded_path_call(name):
        def guarded(path, *args, **kwargs):
            if _inside(path, root):
                raise AssertionError(f"a reader called {name} on {path}")
            return real(path, *args, **kwargs)
        real = getattr(os, name)
        return guarded

    def apply():
        monkeypatch.setattr(os, "open", guarded_os_open)
        monkeypatch.setattr(builtins, "open", guarded_open)
        monkeypatch.setattr(io, "open", guarded_open)
        for name in ("unlink", "remove", "rmdir", "mkdir", "replace", "rename", "utime"):
            monkeypatch.setattr(os, name, guarded_path_call(name))
        monkeypatch.setattr(discovery, "discover_processes", _refuse("discover_processes"))
        monkeypatch.setattr(
            discovery, "_cleanup_stale_sessions", _refuse("_cleanup_stale_sessions")
        )
        monkeypatch.setattr(discovery.subprocess, "run", _refuse("subprocess.run"))
        monkeypatch.setattr(session_store, "maintain_store", _refuse("maintain_store"))
        monkeypatch.setattr(mcp_server, "maintain_store", _refuse("maintain_store"))
        if os.name != "nt":
            for directory in (paths.sessions_dir(), paths.state_dir()):
                if directory.is_dir():
                    directory.chmod(0o500)

    yield apply
    for directory in (paths.sessions_dir(), paths.state_dir()):
        if directory.is_dir():
            directory.chmod(0o700)


def _snapshot(root: Path) -> dict:
    found = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else b""
        found[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(info.st_mode), info.st_mtime_ns, content
        )
    return found


def _populate(fleet_dir):
    now = int(time.time())
    fresh = make_record(session="fresh", instance="101:20")
    ended = make_record(session="ended", instance="102:20")
    ended.update(status="ended", ts=now - 4000, started=now - 4000)
    dead = make_record(session="dead", instance="999999:1")
    dead.update(pid="999999", process_start="1")
    waiting = make_record(session="waiting", instance="103:20")
    waiting.update(status="waiting", repo="needs-me")
    for record in (fresh, ended, dead, waiting):
        assert discovery.write_session_record(record)
    write_session(fleet_dir, "legacy-old", "legacy", "/tmp/legacy",
                  status="ended", ts=now - 4000)
    write_session(fleet_dir, "legacy-pidless", "legacy", "/tmp/legacy",
                  status="started", ts=now - 4000)
    write_session(fleet_dir, "proc-424242", "proc", "/tmp/proc", source="process")


def test_readers_never_write_or_unlink(fleet_dir, forbid_writes, capsys):
    _populate(fleet_dir)
    state = paths.state_dir()
    before = _snapshot(state)
    forbid_writes()

    sessions = discovery.read_sessions()
    assert {session["session_id"] for session in sessions} == {
        "fresh", "ended", "dead", "waiting", "legacy-old", "legacy-pidless",
        "proc-424242",
    }
    assert discovery.read_session_records()
    assert discovery.read_stored_sessions()
    assert discovery.read_session_record("claude", "fresh", "101:20")["status"] == "running"

    status = json.loads(mcp_server.fleet_status())
    assert status["summary"]["total"] == 7
    assert json.loads(mcp_server.fleet_session("fresh"))["session_id"] == "fresh"
    attention = json.loads(mcp_server.fleet_sessions_needing_attention())
    assert [session["session_id"] for session in attention["sessions"]] == ["waiting"]

    cli.cmd_status(None)
    assert "needs-me" in capsys.readouterr().out
    assert focus.find_session("needs-me")["session_id"] == "waiting"
    assert len(SessionFeed(discovery.read_sessions).snapshot()) == 7

    assert _snapshot(state) == before


def test_readers_do_not_create_a_missing_store(forbid_writes):
    forbid_writes()
    assert not paths.state_dir().exists()
    assert discovery.read_sessions() == []
    assert discovery.read_session_records() == []
    assert json.loads(mcp_server.fleet_status())["sessions"] == []
    assert not paths.state_dir().exists()


def test_cli_status_prints_stored_sessions_only(fleet_dir, forbid_writes, capsys):
    write_session(fleet_dir, "one", "alpha", "/tmp/alpha", status="running")
    forbid_writes()
    cli.cmd_status(None)
    out = capsys.readouterr().out
    assert "alpha" in out and "RUNNING" in out


def test_annotate_session_is_pure_and_the_one_place_for_derived_fields():
    stored = {
        "session_id": "s", "status": "idle", "ts": 1_000, "_legacy": False,
        "_process": False, "launch": {"launch_id": "L", "profile_id": "P"},
    }
    original = json.loads(json.dumps(stored))
    view = annotate_session(stored, 1_120)
    assert stored == original
    assert "_legacy" not in view and "_process" not in view
    assert view["age_seconds"] == 120
    assert view["needs_attention"] is False
    assert annotate_session(stored, 1_121)["needs_attention"] is True
    assert view["launch_id"] == "L" and view["profile_id"] == "P"


@pytest.mark.parametrize(
    ("status", "age", "attention"),
    [("waiting", 0, True), ("running", 9_999, False), ("idle", 5, False),
     ("ended", 9_999, False)],
)
def test_annotate_session_attention(status, age, attention):
    view = annotate_session({"status": status, "ts": 100}, 100 + age)
    assert view["needs_attention"] is attention


def test_annotate_session_clamps_future_timestamps():
    assert annotate_session({"status": "running", "ts": 200}, 100)["age_seconds"] == 0
