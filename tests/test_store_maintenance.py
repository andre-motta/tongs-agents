"""maintain_store: the one cleanup path, its locks, stamp and owners."""

import functools
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tongs_agents import cli, discovery, mcp_server, paths, session_store
from tongs_agents.commands import cleanup as cleanup_command
from tongs_agents.models import SCHEMA_VERSION, UNRESOLVED_INSTANCE_ID, canonical_session_id
from tongs_agents.session_store import MaintenanceResult, import_session_record, maintain_store
from tongs_agents.sessions_feed import SessionFeed, WorkspaceFeed
from tests.conftest import write_session
from tests.test_harness_contract import make_record


def pidless(session, *, status="running", ts=None, sequence=None):
    """A schema 1 record cleanup judges by age alone (no process lookups)."""
    now = int(time.time()) if ts is None else ts
    record = make_record(session=session, instance=UNRESOLVED_INSTANCE_ID)
    record.update(
        canonical_id=canonical_session_id("claude", session, UNRESOLVED_INSTANCE_ID),
        pid="", process_start="", process_identity="unresolved", status=status,
        ts=now, started=now,
        focus_target={"kind": "unavailable", "terminal": "", "terminal_env": {}},
    )
    if sequence is not None:
        record["sequence"] = sequence
    return record


def stale(session):
    return pidless(session, status="ended", ts=int(time.time()) - 4000)


def stored(session):
    return discovery.read_session_record("claude", session, UNRESOLVED_INSTANCE_ID)


@pytest.fixture
def no_processes(monkeypatch):
    calls = []
    monkeypatch.setattr(discovery, "_find_agent_processes", lambda: calls.append(1) or [])
    return calls


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


# maintain_store


def test_maintain_store_discovers_cleans_and_stamps(fleet_dir, no_processes):
    assert discovery.write_session_record(stale("old"))
    assert discovery.write_session_record(pidless("live"))
    result = maintain_store(clock=Clock())
    assert result == MaintenanceResult(True, removed=1)
    assert no_processes == [1]
    assert stored("old") is None and stored("live") is not None
    assert (fleet_dir / session_store.STAMP_NAME).read_text() == "1000000.000"


def test_maintain_store_skips_within_the_interval(fleet_dir, no_processes):
    clock = Clock()
    assert maintain_store(clock=clock).ran
    clock.now += session_store.MIN_INTERVAL_SECONDS - 0.1
    assert maintain_store(clock=clock) == MaintenanceResult(False, skipped="recent")
    assert maintain_store(clock=clock, force=True).ran
    clock.now += session_store.MIN_INTERVAL_SECONDS
    assert maintain_store(clock=clock).ran
    assert len(no_processes) == 3


@pytest.mark.parametrize("stamp", [b"2000000.0", b"garbage", b"nan", b"9" * 100])
def test_maintain_store_runs_after_a_future_or_bad_stamp(fleet_dir, no_processes, stamp):
    (fleet_dir / session_store.STAMP_NAME).write_bytes(stamp)
    assert maintain_store(clock=Clock()).ran


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_maintain_store_refuses_a_symlinked_stamp(fleet_dir, no_processes, tmp_path):
    target = tmp_path / "elsewhere"
    target.write_text("0")
    (fleet_dir / session_store.STAMP_NAME).symlink_to(target)
    assert discovery.write_session_record(stale("old"))
    assert maintain_store() == MaintenanceResult(False, skipped="unavailable")
    assert target.read_text() == "0"
    assert no_processes == [] and stored("old") is not None


def test_maintain_store_never_waits_for_another_maintainer(fleet_dir, no_processes):
    assert discovery.write_session_record(stale("old"))
    with discovery.store_maintenance_lock():
        started = time.monotonic()
        assert maintain_store(force=True) == MaintenanceResult(False, skipped="busy")
        assert time.monotonic() - started < 0.4
    assert no_processes == [] and stored("old") is not None


def test_a_timeout_during_the_run_is_not_reported_as_busy(fleet_dir, no_processes,
                                                          monkeypatch):
    def timed_out():
        raise TimeoutError("fleet record lock timed out")

    clock = Clock()
    monkeypatch.setattr(discovery, "_cleanup_stale_sessions", timed_out)
    assert maintain_store(force=True, clock=clock) == MaintenanceResult(
        False, skipped="unavailable")
    with discovery.store_maintenance_lock():
        pass
    stamp = (fleet_dir / session_store.STAMP_NAME).read_text()
    monkeypatch.setattr(discovery, "_cleanup_stale_sessions", lambda: 0)
    assert maintain_store(clock=clock) == MaintenanceResult(False, skipped="recent")
    assert (fleet_dir / session_store.STAMP_NAME).read_text() == stamp


def test_maintain_store_reports_an_unusable_store(monkeypatch, no_processes):
    def refuse(create=False):
        raise paths.UnsafePathError("symlinked")

    monkeypatch.setattr(paths, "sessions_dir", refuse)
    assert maintain_store() == MaintenanceResult(False, skipped="unavailable")
    assert no_processes == []


def test_maintain_store_creates_the_store_it_owns(no_processes):
    assert not paths.sessions_dir().exists()
    assert maintain_store().ran
    assert paths.is_private_dir(paths.sessions_dir())


# The per-record lock before each unlink


def test_cleanup_keeps_a_record_rewritten_after_it_was_judged(fleet_dir, monkeypatch):
    assert discovery.write_session_record(stale("racy"))
    judge = discovery._should_remove

    def judge_then_hook_writes(data, now):
        verdict = judge(data, now)
        assert discovery.write_session_record(pidless("racy"))
        return verdict

    monkeypatch.setattr(discovery, "_should_remove", judge_then_hook_writes)
    assert discovery._cleanup_stale_sessions() == 0
    assert stored("racy")["status"] == "running"


def test_cleanup_waits_for_the_record_lock(fleet_dir):
    record = stale("held")
    assert discovery.write_session_record(record)
    with discovery._record_lock(record["canonical_id"]):
        assert discovery._cleanup_stale_sessions() == 0
    assert stored("held") is not None
    assert discovery._cleanup_stale_sessions() == 1


def test_concurrent_hook_writes_and_cleanup_lose_no_records(fleet_dir):
    sessions = [f"stress-{index}" for index in range(12)]
    rounds = 15
    stop = threading.Event()
    errors = []

    def write_until_written(record):
        deadline = time.monotonic() + 10
        while not discovery.write_session_record(dict(record)):
            if time.monotonic() > deadline:
                raise AssertionError(f"could not write {record['session_id']}")

    def writer(session):
        try:
            for _ in range(rounds):
                write_until_written(stale(session))
                write_until_written(pidless(session))
        except Exception as error:  # surfaced below
            errors.append(error)

    def cleaner():
        while not stop.is_set():
            discovery._cleanup_stale_sessions()

    cleaners = [threading.Thread(target=cleaner) for _ in range(2)]
    writers = [threading.Thread(target=writer, args=(session,)) for session in sessions]
    for thread in cleaners + writers:
        thread.start()
    for thread in writers:
        thread.join()
    stop.set()
    for thread in cleaners:
        thread.join()

    assert errors == []
    missing = [session for session in sessions if stored(session) is None]
    assert missing == []
    assert {stored(session)["status"] for session in sessions} == {"running"}


# The writer for imported records


def test_import_session_record_writes_like_the_hook(fleet_dir):
    record = pidless("bridged", sequence=1)
    record["source"] = "sandbox"
    original = json.loads(json.dumps(record))
    assert import_session_record(record)
    assert record == original
    [stored_record] = discovery.read_session_records()
    assert stored_record["source"] == "sandbox"
    assert stored_record["arrival_sequence"] == 1
    assert (fleet_dir / f"fleet-v{SCHEMA_VERSION}-{record['canonical_id'].rsplit(':', 1)[-1]}.json").is_file()


def test_import_session_record_keeps_the_hook_ordering_rules(fleet_dir):
    assert import_session_record(pidless("bridged", sequence=5))
    assert not import_session_record(pidless("bridged", sequence=5))
    assert not import_session_record(pidless("bridged", sequence=4))
    assert import_session_record(pidless("bridged", sequence=6))
    moved = make_record(session="owned", instance="101:20")
    assert import_session_record(moved)
    moved = dict(moved, pid="202")
    assert not import_session_record(moved)


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {"schema_version": 0, "session_id": "s"},
        {**make_record(), "schema_version": 0},
        {**make_record(), "harness_id": "unknown", "agent": "unknown"},
        {**make_record(), "canonical_id": "fleet:v1:claude:forged"},
        {**make_record(), "status": "bogus"},
        {**make_record(), "session_id": 7},
    ],
)
def test_import_session_record_refuses_invalid_records(fleet_dir, record):
    assert import_session_record(record) is False
    assert discovery.read_session_records() == []


def test_import_session_record_reports_a_held_identity_lock(fleet_dir):
    record = pidless("busy")
    with discovery.session_identity_lock("claude", "busy"):
        assert import_session_record(record) is False


# Owners and explicit callers


def test_session_feed_is_the_cleanup_owner(monkeypatch):
    order = []
    monkeypatch.setattr(session_store, "maintain_store", lambda: order.append("maintain"))
    monkeypatch.setattr(discovery, "read_sessions", lambda: order.append("read") or [])
    SessionFeed().snapshot()
    assert order == ["maintain", "read"]


def test_a_custom_reader_does_not_maintain(monkeypatch):
    monkeypatch.setattr(
        session_store, "maintain_store", lambda: pytest.fail("maintained")
    )
    assert SessionFeed(lambda: []).snapshot() == []


def test_an_explicit_maintainer_runs_before_a_custom_reader():
    order = []
    feed = SessionFeed(
        lambda: order.append("read") or [], maintainer=lambda: order.append("maintain")
    )
    feed.snapshot()
    assert order == ["maintain", "read"]


def test_workspace_feed_maintains_like_the_monitor(monkeypatch):
    calls = []
    monkeypatch.setattr(session_store, "maintain_store", lambda: calls.append(1))
    monkeypatch.setattr(discovery, "read_sessions", lambda: [])
    mux = SimpleNamespace(name="zellij", list_panes=lambda session: [])
    WorkspaceFeed("agents", mux, launch_reader=lambda session: []).snapshot()
    assert calls == [1]


def test_sidebar_and_monitor_feeds_both_maintain(monkeypatch, fleet_dir, no_processes):
    clock = Clock()
    monkeypatch.setattr(
        session_store, "maintain_store",
        functools.partial(session_store.maintain_store, clock=clock),
    )
    assert discovery.write_session_record(stale("old"))
    SessionFeed().snapshot()
    assert stored("old") is None
    assert discovery.write_session_record(stale("again"))
    SessionFeed().snapshot()
    assert stored("again") is not None
    clock.now += session_store.MIN_INTERVAL_SECONDS
    SessionFeed().snapshot()
    assert stored("again") is None
    assert len(no_processes) == 2


def test_fleet_cleanup_runs_maintenance_now(fleet_dir, no_processes):
    assert discovery.write_session_record(stale("old"))
    assert maintain_store().ran
    assert discovery.write_session_record(stale("next"))
    assert json.loads(mcp_server.fleet_cleanup()) == {"removed": 1}
    assert stored("next") is None


def test_fleet_cleanup_reports_a_skip(fleet_dir, no_processes):
    with discovery.store_maintenance_lock():
        assert json.loads(mcp_server.fleet_cleanup()) == {"removed": 0, "skipped": "busy"}


def test_cleanup_command_is_registered():
    parser, handlers = cli.build_parser()
    assert "cleanup" in handlers
    assert parser.parse_args(["cleanup"]).command == "cleanup"


def test_cleanup_command_removes_now(fleet_dir, no_processes, capsys):
    assert discovery.write_session_record(stale("old"))
    assert maintain_store().ran
    assert discovery.write_session_record(stale("next"))
    assert cleanup_command.run(SimpleNamespace()) == 0
    assert capsys.readouterr().out == "Removed 1 session record(s).\n"
    assert stored("next") is None


def test_cleanup_command_fails_while_another_maintainer_runs(fleet_dir, no_processes, capsys):
    with discovery.store_maintenance_lock():
        assert cleanup_command.run(SimpleNamespace()) == 1
    assert "try again" in capsys.readouterr().err


def test_legacy_records_are_still_cleaned(fleet_dir, no_processes):
    write_session(fleet_dir, "legacy", "repo", "/tmp/repo", status="ended",
                  ts=int(time.time()) - 4000)
    assert maintain_store().removed == 1
    assert not (fleet_dir / "legacy.json").exists()


def test_hook_import_path_does_not_load_the_maintainer():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, tongs_agents.hook; print('tongs_agents.session_store' in sys.modules)"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    assert result.stdout.strip() == "False"


def test_status_hints_at_cleanup_when_maintenance_is_overdue(fleet_dir, no_processes,
                                                             capsys):
    cli.cmd_status(None)
    assert capsys.readouterr().err.strip() == cli.CLEANUP_HINT
    assert maintain_store(force=True).ran
    cli.cmd_status(None)
    assert capsys.readouterr().err == ""
    stamp = fleet_dir / session_store.STAMP_NAME
    stamp.write_text(f"{time.time() - session_store.OVERDUE_SECONDS - 60:.3f}")
    cli.cmd_status(None)
    assert "tongs-agents cleanup" in capsys.readouterr().err
    stamp.write_text(f"{time.time() + 600:.3f}")
    cli.cmd_status(None)
    assert "tongs-agents cleanup" in capsys.readouterr().err


def test_status_gives_no_hint_without_a_store(capsys):
    assert not paths.sessions_dir().exists()
    cli.cmd_status(None)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "No sessions found." in captured.out
    assert not paths.sessions_dir().exists()
