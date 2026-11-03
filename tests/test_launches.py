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


def _set_mtime(path, days):
    then = time.time() - days * 86400
    os.utime(path, (then, then), follow_symlinks=False)


def test_prune_removes_old_temp_files_and_orphaned_markers(monkeypatch):
    monkeypatch.setattr(discovery, "get_process_info", lambda pid: None)
    directory = paths.launches_dir(create=True)
    kept = launches.create_pending(PROFILE, "agents", "zellij")
    claimed = launches.claim(kept.launch_id, 4242, "s", None)
    assert claimed.launch_id == kept.launch_id
    live_marker = directory / f".{kept.launch_id}.claim"
    old_tmp = directory / ".launch-tmp-abc.tmp"
    new_tmp = directory / ".launch-tmp-def.tmp"
    orphan = directory / f".{'e' * 32}.claim"
    new_orphan = directory / f".{'f' * 32}.claim"
    unrelated = directory / "notes.txt"
    for path in (old_tmp, new_tmp, orphan, new_orphan, unrelated):
        path.write_text("")
    for path in (old_tmp, orphan, unrelated, live_marker):
        _set_mtime(path, 8)
    assert launches.prune() == 0
    assert sorted(entry.name for entry in directory.iterdir()) == sorted([
        f"{kept.launch_id}.json", live_marker.name, new_tmp.name, new_orphan.name,
        unrelated.name,
    ])


@posix_only
def test_prune_never_follows_a_symlinked_leftover(tmp_path):
    directory = paths.launches_dir(create=True)
    target = tmp_path / "target.tmp"
    target.write_text("keep")
    link = directory / ".launch-tmp-link.tmp"
    link.symlink_to(target)
    _set_mtime(link, 8)
    launches.prune()
    assert target.read_text() == "keep"


@posix_only
def test_read_does_not_block_on_a_fifo():
    launch_id = "a" * 32
    directory = paths.launches_dir(create=True)
    os.mkfifo(directory / f"{launch_id}.json")
    result = []
    worker = threading.Thread(target=lambda: result.append(launches.get(launch_id)), daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result == [None]


def test_discard_removes_only_a_pending_record():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    claimed = launches.create_pending(PROFILE, "agents", "zellij")
    launches.claim(claimed.launch_id, 4242, "1", PANE)
    launches.discard(pending.launch_id)
    launches.discard(claimed.launch_id)
    launches.discard(pending.launch_id)
    assert not os.path.lexists(record_file(pending.launch_id))
    assert [record.launch_id for record in launches.list_records()] == [claimed.launch_id]
    with pytest.raises(LaunchError):
        launches.discard("../escape")


@posix_only
def test_discard_never_follows_a_symlinked_record(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    paths.launches_dir(create=True)
    link = record_file("a" * 32)
    link.symlink_to(target)
    launches.discard("a" * 32)
    assert target.read_text() == "{}"


def _folder_profile():
    return Profile(name="p", harness="claude", argv=("claude",))


def test_folder_and_args_round_trip_and_stay_out_of_plain_records(tmp_path):
    plain = launches.create_pending(_folder_profile(), "agents", "zellij")
    assert "folder" not in plain.to_json() and "args" not in plain.to_json()
    assert launches.get(plain.launch_id).args == ()
    record = launches.create_pending(_folder_profile(), "agents", "zellij",
                                     folder=str(tmp_path), args=["--resume", "", "a\nb"])
    data = json.loads((paths.launches_dir() / f"{record.launch_id}.json").read_text())
    assert (data["folder"], data["args"]) == (str(tmp_path), ["--resume", "", "a\nb"])
    stored = launches.get(record.launch_id)
    assert (stored.folder, stored.args) == (str(tmp_path), ("--resume", "", "a\nb"))
    claimed = launches.claim(record.launch_id, os.getpid(), None, None)
    assert (claimed.folder, claimed.args) == (str(tmp_path), ("--resume", "", "a\nb"))


@pytest.mark.parametrize("folder, args, message", [
    ("relative/dir", (), "absolute path"),
    ("/tmp/a\x07b", (), "absolute path"),
    (None, "--resume", "list of strings"),
    (None, ["a\0b"], "NUL"),
    (None, ["x"] * (launches.MAX_ARGS + 1), f"at most {launches.MAX_ARGS}"),
    (None, ["x" * (launches.MAX_ARG_LENGTH + 1)], "longer than"),
    (None, ["x" * launches.MAX_ARG_LENGTH] * 5, "in all"),
])
def test_invalid_folder_or_args_are_refused(folder, args, message):
    with pytest.raises(launches.LaunchError, match=message):
        launches.create_pending(_folder_profile(), "agents", "zellij", folder=folder, args=args)
    assert launches.list_records() == []


@pytest.mark.parametrize("change", [
    {"folder": "relative"}, {"folder": 7}, {"args": "--resume"}, {"args": ["a\0b"]},
    {"args": [1]}, {"args": None},
])
def test_a_tampered_folder_or_args_makes_the_record_invalid(change):
    record = launches.create_pending(_folder_profile(), "agents", "zellij")
    path = paths.launches_dir() / f"{record.launch_id}.json"
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    assert launches.get(record.launch_id) is None


def test_a_label_round_trips_and_plain_records_keep_their_shape():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    assert "label" not in json.loads(record_file(pending.launch_id).read_text())
    claimed = launches.claim(pending.launch_id, 4242, "1", PANE)
    labelled = launches.set_label(pending.launch_id, "  review #130  ")
    assert labelled.label == "review #130"
    assert labelled == launches.get(pending.launch_id)
    assert labelled == LaunchRecord(**{**claimed.__dict__, "label": "review #130"})
    data = json.loads(record_file(pending.launch_id).read_text())
    assert data["label"] == "review #130"
    assert data["schema_version"] == launches.SCHEMA_VERSION
    assert launches.labels() == {pending.launch_id: "review #130"}
    restored = launches.set_label(pending.launch_id, "   ")
    assert restored.label is None
    assert restored == claimed
    assert "label" not in json.loads(record_file(pending.launch_id).read_text())
    assert launches.labels() == {}


def test_a_replacing_claim_keeps_the_label():
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    launches.claim(pending.launch_id, 4242, "1", PANE)
    launches.set_label(pending.launch_id, "reviewer")
    fresh = launches.claim(pending.launch_id, 5151, "2", PANE)
    assert (fresh.replaces, fresh.label) == (pending.launch_id, "reviewer")


@pytest.mark.parametrize("label, message", [
    ("x" * (launches.MAX_LABEL_LENGTH + 1), "at most"),
    ("tab\there", "control"),
    ("bell\x07", "control"),
    ("line\nbreak", "control"),
    ("-flag", "'-'"),
    ("tongs-agents-sidebar", "reserved"),
    ("tongs-agents-home", "reserved"),
    (42, "non-empty"),
])
def test_invalid_labels_are_refused_and_nothing_is_written(label, message):
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    before = record_file(pending.launch_id).read_bytes()
    with pytest.raises(LaunchError, match=message):
        launches.set_label(pending.launch_id, label)
    assert record_file(pending.launch_id).read_bytes() == before


def test_set_label_needs_an_existing_record():
    with pytest.raises(LaunchError, match="no launch record"):
        launches.set_label("a" * 32, "name")
    with pytest.raises(LaunchError, match="invalid launch id"):
        launches.set_label("../x", "name")


@pytest.mark.parametrize("label", ["", "-x", "a\x1bb", "x" * 65, 7, ["a"]])
def test_a_tampered_label_makes_the_record_invalid(label):
    pending = launches.create_pending(PROFILE, "agents", "zellij")
    path = record_file(pending.launch_id)
    data = json.loads(path.read_text())
    data["label"] = label
    path.write_text(json.dumps(data))
    assert launches.get(pending.launch_id) is None
