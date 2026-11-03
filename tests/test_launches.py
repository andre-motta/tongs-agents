"""Launch records: pending creation, claim-once, fresh replacements and pruning."""

import json
import os
import stat
import threading
import time

import pytest

from tongs_agents import discovery, launches, paths
from tongs_agents.discovery import ProcessInfo
from tongs_agents.launches import LaunchError, LaunchRecord
from tongs_agents.multiplexers.base import PaneRef
from tongs_agents.profiles import Profile


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")

PROFILE = Profile(name="claude-alt", harness="claude", argv=("claude",))
PANE = PaneRef("zellij", "agents", "7", "/run/example/zellij")


def record_file(launch_id: str):
    return paths.launches_dir() / f"{launch_id}.json"


def test_create_pending_writes_a_private_record():
    record = launches.create_pending(PROFILE, "agents", "zellij")
    assert launches.is_launch_id(record.launch_id)
    assert record.state == "pending"
    assert (record.profile, record.harness) == ("claude-alt", "claude")
    assert (record.multiplexer, record.session) == ("zellij", "agents")
    assert record.pid is record.pane_id is record.replaces is None
    data = json.loads(record_file(record.launch_id).read_text())
    assert data == {
        "schema_version": 1,
        "launch_id": record.launch_id,
        "profile": "claude-alt",
        "harness": "claude",
        "multiplexer": "zellij",
        "session": "agents",
        "socket_dir": None,
        "pane_id": None,
        "pid": None,
        "process_start": None,
        "state": "pending",
        "replaces": None,
        "created": record.created,
    }
    assert launches.get(record.launch_id) == record


@posix_only
def test_record_and_directory_modes():
    record = launches.create_pending(PROFILE, "agents", "zellij")
    assert stat.S_IMODE(paths.launches_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE(record_file(record.launch_id).stat().st_mode) == 0o600
    launches.claim(record.launch_id, os.getpid(), "1", PANE)
    assert stat.S_IMODE(record_file(record.launch_id).stat().st_mode) == 0o600


@pytest.mark.parametrize("session, multiplexer", [
    ("", "zellij"), ("agents", ""), ("a\nb", "zellij"), ("x" * 2000, "zellij"),
])
def test_create_pending_rejects_bad_text(session, multiplexer):
    with pytest.raises(LaunchError):
        launches.create_pending(PROFILE, session, multiplexer)


def test_claim_once_records_pid_start_and_pane():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    claimed = launches.claim(pending.launch_id, 4242, "98765", PANE)
    assert claimed.launch_id == pending.launch_id
    assert (claimed.state, claimed.pid, claimed.process_start) == ("running", 4242, "98765")
    assert (claimed.pane_id, claimed.socket_dir) == ("7", "/run/example/zellij")
    assert claimed.replaces is None
    assert launches.get(pending.launch_id) == claimed


def test_second_claim_gets_a_fresh_replacing_record():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    first = launches.claim(pending.launch_id, 4242, "1", PANE)
    other_pane = PaneRef("zellij", "agents", "9", None)
    second = launches.claim(pending.launch_id, 5151, "2", other_pane)
    assert second.launch_id != pending.launch_id
    assert second.replaces == pending.launch_id
    assert (second.state, second.pid, second.pane_id, second.socket_dir) == (
        "running", 5151, "9", None
    )
    assert (second.profile, second.harness, second.session) == ("claude-alt", "claude", "agents")
    assert launches.get(pending.launch_id) == first
    assert launches.get(second.launch_id) == second


def test_concurrent_claims_have_one_winner():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    results = []
    barrier = threading.Barrier(8)

    def worker(pid):
        barrier.wait()
        results.append(launches.claim(pending.launch_id, pid, None, None))

    threads = [threading.Thread(target=worker, args=(1000 + n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [record for record in results if record.launch_id == pending.launch_id]
    assert len(winners) == 1
    assert launches.get(pending.launch_id).pid == winners[0].pid
    assert sorted(r.replaces for r in results if r.replaces) == [pending.launch_id] * 7


def test_tmux_pane_without_session_keeps_the_record_session():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    claimed = launches.claim(pending.launch_id, 4242, None, PaneRef("tmux", "", "%3", "/tmp/tmux-1/default"))
    assert (claimed.multiplexer, claimed.session, claimed.pane_id) == ("tmux", "agents", "%3")


def test_claim_without_pane_keeps_spawner_identity():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    claimed = launches.claim(pending.launch_id, 4242, None, None)
    assert (claimed.multiplexer, claimed.session, claimed.pane_id) == ("zellij", "agents", None)


@pytest.mark.parametrize("launch_id", ["", "abc", "A" * 32, "0" * 31, "../" + "0" * 29])
def test_claim_rejects_bad_ids(launch_id):
    with pytest.raises(LaunchError, match="invalid launch id"):
        launches.claim(launch_id, 1, None, None)
    assert launches.get(launch_id) is None


def test_claim_rejects_unknown_id_and_bad_values():
    with pytest.raises(LaunchError, match="no launch record"):
        launches.claim("0" * 32, 1, None, None)
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    for pid in (0, -1, True, "12"):
        with pytest.raises(LaunchError, match="pid"):
            launches.claim(pending.launch_id, pid, None, None)
    with pytest.raises(LaunchError, match="pane"):
        launches.claim(pending.launch_id, 1, None, PaneRef("zellij", "agents", "", None))
    assert launches.get(pending.launch_id).state == "pending"


@pytest.mark.parametrize("mutate", [
    lambda data: data.update(state="done"),
    lambda data: data.update(pid="12"),
    lambda data: data.update(schema_version=2),
    lambda data: data.update(profile="Bad Name"),
    lambda data: data.update(launch_id="f" * 32),
    lambda data: data.pop("created"),
])
def test_malformed_records_are_ignored(mutate):
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    path = record_file(pending.launch_id)
    data = json.loads(path.read_text())
    mutate(data)
    path.write_text(json.dumps(data))
    assert launches.get(pending.launch_id) is None
    assert launches.list_records() == []
    with pytest.raises(LaunchError, match="no launch record"):
        launches.claim(pending.launch_id, 1, None, None)


def test_oversized_record_is_ignored():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    record_file(pending.launch_id).write_text(" " * (64 * 1024 + 1))
    assert launches.get(pending.launch_id) is None


@posix_only
def test_symlinked_record_is_not_followed(tmp_path):
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    path = record_file(pending.launch_id)
    real = tmp_path / "real.json"
    original = path.read_text()
    real.write_text(original)
    path.unlink()
    path.symlink_to(real)
    assert launches.get(pending.launch_id) is None
    with pytest.raises(LaunchError):
        launches.claim(pending.launch_id, 1, None, None)
    assert real.read_text() == original


@posix_only
def test_foreign_owned_record_is_ignored(monkeypatch):
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    real_getuid = os.getuid
    monkeypatch.setattr(launches.os, "getuid", lambda: real_getuid() + 1)
    assert launches.get(pending.launch_id) is None


@posix_only
def test_symlinked_launches_dir_is_refused(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    paths.state_dir(create=True)
    paths.launches_dir().symlink_to(target, target_is_directory=True)
    with pytest.raises(paths.UnsafePathError, match="symlink"):
        launches.create_pending(PROFILE, "agents", "zellij")
    assert list(target.iterdir()) == []


def test_list_records_filters_by_session_and_sorts():
    first = launches.create_pending(PROFILE, "agents", "zellij")
    second = launches.create_pending(PROFILE, "other", "zellij")
    third = launches.create_pending(PROFILE, "agents", "zellij")
    assert launches.list_records() == sorted(
        [first, second, third], key=lambda r: (r.created, r.launch_id)
    )
    assert {r.launch_id for r in launches.list_records("agents")} == {
        first.launch_id, third.launch_id
    }
    assert launches.list_records("missing") == []


def test_list_records_without_directory():
    assert launches.list_records() == []
    assert launches.prune() == 0


def age(record: LaunchRecord, days: float) -> None:
    path = record_file(record.launch_id)
    data = json.loads(path.read_text())
    data["created"] = int(time.time() - days * 86400)
    path.write_text(json.dumps(data))


def test_prune_removes_only_old_records_of_dead_processes(monkeypatch):
    alive = {111: "start-a", 333: "start-new"}
    monkeypatch.setattr(
        discovery, "get_process_info",
        lambda pid: ProcessInfo(pid, 1, "x", "x", (), None, alive[pid]) if pid in alive else None,
    )
    old_pending = launches.create_pending(PROFILE, "agents", "zellij")
    fresh_pending = launches.create_pending(PROFILE, "agents", "zellij")
    old_alive = launches.claim(launches.create_pending(PROFILE, "agents", "zellij").launch_id,
                               111, "start-a", None)
    old_dead = launches.claim(launches.create_pending(PROFILE, "agents", "zellij").launch_id,
                              222, "start-b", None)
    old_reused = launches.claim(launches.create_pending(PROFILE, "agents", "zellij").launch_id,
                                333, "start-old", None)
    for record in (old_pending, old_alive, old_dead, old_reused):
        age(record, 8)
    assert launches.prune() == 3
    remaining = {record.launch_id for record in launches.list_records()}
    assert remaining == {fresh_pending.launch_id, old_alive.launch_id}
    assert not list(paths.launches_dir().glob(f".{old_dead.launch_id}.claim"))
    age(fresh_pending, 2)
    assert launches.prune(max_age_days=1) == 1
