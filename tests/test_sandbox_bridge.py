"""The host bridge (rows 7 to 10): hostile spools, launch identity, phase fallback, discovery."""

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_sandbox_support import (  # noqa: F401
    LAUNCH_ID,
    create_sandbox,
    fake,
    make_launch,
    pane_process,
    posix_only,
    sessions,
    spool_dir,
    stamp,
)
from tests.test_sandbox_validation import load_validator
from tongs_agents import discovery, paths, session_store
from tongs_agents.discovery import MAX_EVENT_BYTES
from tongs_agents.profiles import parse_profile
from tongs_agents.sandbox import bridge, state
from tongs_agents.sandbox.__main__ import main as module_main
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError
from tongs_agents.sessions_feed import SessionFeed

pytestmark = posix_only


def payload(session="s1", **extra):
    return json.dumps({"session_id": session, "cwd": "/sandbox/workspace", **extra})


def put(spool: Path, index: int, event: str, data: str, pid: int = 7) -> Path:
    path = spool / f"{stamp(index)}-{pid}-{event}.json"
    path.write_text(data)
    return path


@pytest.fixture(autouse=True)
def no_host_agents(monkeypatch):
    """Keep maintenance from recording the agents running on the test host."""
    monkeypatch.setattr(discovery, "_find_agent_processes", lambda: [])


@pytest.fixture
def launch(fake, pane_process):
    _process, info = pane_process
    create_sandbox(fake)
    return make_launch(info)


def sync():
    return bridge.sync_all()


def test_spooled_events_become_records_with_host_identity(fake, launch, pane_process):
    _process, info = pane_process
    spool = spool_dir(fake, launch)
    put(spool, 1, "session-start", payload(), pid=99999)
    put(spool, 2, "tool-use", payload(tool_name="Bash"))
    result = sync()
    assert (result.imported, result.rejected) == (2, 0), result.errors
    [record] = [item for item in sessions() if item["session_id"] == "s1"]
    assert record["pid"] == str(info.pid)
    assert record["process_start"] == info.start_token
    assert record["instance_id"] == f"{info.pid}:{info.start_token}"
    assert record["launch"] == {"launch_id": LAUNCH_ID, "profile_id": "sandboxed"}
    assert record["source"] == "sandbox"
    assert record["sandbox"] == "tongs-sandboxed"
    assert record["cwd"] == "/home/user/project"
    assert record["repo"] == "project"
    assert record["terminal"] == "zellij"
    assert record["terminal_env"]["ZELLIJ_PANE_ID"] == "3"
    assert (record["status"], record["detail"], record["tool"]) == (
        "running", "using Bash", "Bash"
    )
    assert list(spool.iterdir()) == []


def test_a_sandbox_launch_id_or_pid_in_the_payload_is_ignored(fake, launch, pane_process):
    _process, info = pane_process
    put(spool_dir(fake, launch), 1, "session-start",
        payload(pid=1, launch_id="f" * 32, terminal="kitty"))
    assert sync().imported == 1
    [record] = [item for item in sessions() if item["session_id"] == "s1"]
    assert record["pid"] == str(info.pid)
    assert record["launch"]["launch_id"] == LAUNCH_ID
    assert record["terminal"] == "zellij"


@pytest.mark.parametrize("make", [
    "symlink", "directory", "oversized", "bad_json", "array_json", "unknown_event",
    "traversal", "space", "dash", "no_session", "non_creating_first",
])
def test_hostile_spool_entries_are_dropped(fake, launch, tmp_path, make):
    spool = spool_dir(fake, launch)
    secret = tmp_path / "host-secret.json"
    secret.write_text(payload("leaked"))
    if make == "symlink":
        (spool / f"{stamp(1)}-1-session-start.json").symlink_to(secret)
    elif make == "directory":
        (spool / f"{stamp(1)}-1-session-start.json").mkdir()
    elif make == "oversized":
        put(spool, 1, "session-start", payload(pad="x" * (MAX_EVENT_BYTES + 10)))
    elif make == "bad_json":
        put(spool, 1, "session-start", "{not json")
    elif make == "array_json":
        put(spool, 1, "session-start", "[1, 2]")
    elif make == "unknown_event":
        put(spool, 1, "fleet-event", payload())
    elif make == "traversal":
        (spool / "x..%2f..%2fsessions.json").write_text(payload())
        (spool / "x..json").write_text(payload())
    elif make == "space":
        (spool / f"{stamp(1)} 1-session-start.json").write_text(payload())
    elif make == "dash":
        (spool / "-rf.json").write_text(payload())
    elif make == "no_session":
        put(spool, 1, "session-start", json.dumps({"cwd": "/x"}))
    elif make == "non_creating_first":
        put(spool, 1, "stop", payload())
    result = sync()
    assert result.imported == 0
    assert result.rejected >= 1
    assert [item for item in sessions() if item.get("session_id") != f"sandbox-{LAUNCH_ID}"] \
        == []
    assert list(spool.iterdir()) == []
    assert secret.read_text() == payload("leaked")


def test_dot_files_are_left_alone(fake, launch):
    spool = spool_dir(fake, launch)
    (spool / ".1-2.tmp").write_text(payload())
    assert sync().imported == 0
    assert [path.name for path in spool.iterdir()] == [".1-2.tmp"]


def test_a_flood_drains_over_several_polls_in_order(fake, launch):
    spool = spool_dir(fake, launch)
    put(spool, 0, "session-start", payload())
    for index in range(1, 150):
        put(spool, index, "tool-use", payload(tool_name=f"T{index}"))
    first = sync()
    assert first.imported == bridge.MAX_FILES_PER_PULL
    assert len(list(spool.iterdir())) == 150 - bridge.MAX_FILES_PER_PULL
    while list(spool.iterdir()):
        sync()
    [record] = [item for item in sessions() if item["session_id"] == "s1"]
    assert record["tool"] == "T149"


def test_a_flood_of_distinct_sessions_is_capped_per_launch(fake, launch):
    spool = spool_dir(fake, launch)
    for index in range(40):
        put(spool, index, "session-start", payload(f"s{index:02d}"))
    first = sync()
    cap = bridge.MAX_SESSIONS_PER_LAUNCH
    assert (first.imported, first.capped) == (cap, 40 - cap)
    capped_errors = [error for error in first.errors if "beyond" in error]
    assert capped_errors == [
        f"tongs-sandboxed: dropped {40 - cap} event(s) for sessions beyond {cap} per launch"
    ]
    for index in range(40, 200):
        put(spool, index, "session-start", payload(f"s{index:03d}"))
    put(spool, 300, "tool-use", payload("s00", tool_name="Bash"))
    while list(spool.iterdir()):
        result = sync()
        assert len([error for error in result.errors if "beyond" in error]) <= 1
    ids = sorted(item["session_id"] for item in sessions())
    assert ids == [f"s{index:02d}" for index in range(cap)]
    [first_session] = [item for item in sessions() if item["session_id"] == "s00"]
    assert first_session["tool"] == "Bash"


def put_at(spool: Path, seconds_ago: int, index: int, event: str, data: str) -> Path:
    stamp_ns = (int(time.time()) - seconds_ago) * 1_000_000_000 + index
    path = spool / f"{stamp_ns:019d}-7-{event}.json"
    path.write_text(data)
    return path


def bridged_ids():
    return sorted(item["session_id"] for item in sessions()
                  if not item["session_id"].startswith("sandbox-"))


def test_a_new_session_replaces_the_oldest_ended_one_at_the_cap(fake, launch):
    spool = spool_dir(fake, launch)
    cap = bridge.MAX_SESSIONS_PER_LAUNCH
    for index in range(cap):
        put(spool, index, "session-start", payload(f"s{index}"))
    assert sync().imported == cap
    put_at(spool, 100, 10, "session-end", payload("s2"))
    put_at(spool, 50, 11, "session-end", payload("s1"))
    first = sync()
    assert (first.imported, first.capped, first.replaced) == (2, 0, 0), first.errors
    put(spool, 20, "session-start", payload("n1"))
    assert (sync().replaced, bridged_ids()) == (1, ["n1", "s0", "s1", "s3"])
    put(spool, 21, "session-start", payload("n2"))
    put(spool, 22, "session-start", payload("n3"))
    second = sync()
    assert (second.imported, second.replaced, second.capped) == (1, 1, 1)
    assert bridged_ids() == ["n1", "n2", "s0", "s3"]
    order = [error for error in second.errors if "beyond" in error]
    assert order == [f"tongs-sandboxed: dropped 1 event(s) for sessions beyond {cap} per launch"]


def test_a_replacement_counts_only_once_its_import_succeeds(fake, launch, monkeypatch):
    spool = spool_dir(fake, launch)
    cap = bridge.MAX_SESSIONS_PER_LAUNCH
    for index in range(cap):
        put(spool, index, "session-start", payload(f"s{index}"))
    put(spool, cap, "session-end", payload("s0"))
    assert sync().imported == cap + 1
    real = session_store.import_session_record
    monkeypatch.setattr(session_store, "import_session_record",
                        lambda record: record["session_id"] != "n1" and real(record))
    put(spool, 20, "session-start", payload("n1"))
    result = sync()
    assert (result.imported, result.replaced, result.rejected) == (0, 0, 1)
    monkeypatch.setattr(session_store, "import_session_record", real)
    put(spool, 21, "session-start", payload("n2"))
    result = sync()
    assert (result.imported, result.replaced) == (1, 0)
    assert bridged_ids() == ["n2", "s1", "s2", "s3"]


def test_ended_sessions_do_not_let_a_flood_bypass_the_cap(fake, launch):
    spool = spool_dir(fake, launch)
    cap = bridge.MAX_SESSIONS_PER_LAUNCH
    for index in range(30):
        put(spool, 2 * index, "session-start", payload(f"churn{index:02d}"))
        put(spool, 2 * index + 1, "session-end", payload(f"churn{index:02d}"))
    replaced = 0
    while list(spool.iterdir()):
        replaced += sync().replaced
        assert len(bridged_ids()) <= cap
    assert replaced == 30 - cap
    for index in range(cap):
        put(spool, 100 + index, "session-start", payload(f"live{index}"))
    sync()
    assert bridged_ids() == [f"live{index}" for index in range(cap)]
    for index in range(20):
        put(spool, 200 + 2 * index, "session-start", payload(f"late{index:02d}"))
        put(spool, 201 + 2 * index, "session-end", payload(f"late{index:02d}"))
    while list(spool.iterdir()):
        result = sync()
        assert result.replaced == 0
    assert bridged_ids() == [f"live{index}" for index in range(cap)]


def test_a_launch_locked_by_another_process_is_skipped(fake, launch):
    spool = spool_dir(fake, launch)
    entry = put(spool, 1, "session-start", payload())
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\n"
         "from tongs_agents.sandbox import bridge\n"
         f"with bridge.launch_lock({LAUNCH_ID!r}):\n"
         "    print('held', flush=True)\n"
         "    sys.stdin.readline()\n"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "held"
        result = sync()
        assert (result.busy, result.launches, result.imported) == (1, 0, 0)
        assert entry.exists()
    finally:
        holder.communicate("\n", timeout=30)
    assert holder.returncode == 0
    assert sync().imported == 1
    assert bridged_ids() == ["s1"]


def test_the_cap_check_reads_the_store_under_the_launch_lock(fake, launch, monkeypatch):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    held = []
    real = discovery.read_stored_sessions

    def read(*args, **kwargs):
        try:
            with bridge.launch_lock(LAUNCH_ID):
                held.append(False)
        except TimeoutError:
            held.append(True)
        return real(*args, **kwargs)

    monkeypatch.setattr(discovery, "read_stored_sessions", read)
    assert sync().imported == 1
    assert held == [True]


def test_the_launch_lock_is_private_and_removed_with_the_launch(fake, launch, pane_process):
    process, _info = pane_process
    sync()
    lock = state.state_root() / bridge.LOCKS_DIRECTORY / f"{LAUNCH_ID}.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.parent.stat().st_mode) == 0o700
    process.kill()
    process.wait()
    sync()
    assert not lock.exists()


def test_a_symlinked_launch_lock_is_refused(fake, launch, tmp_path):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    target = tmp_path / "elsewhere"
    target.write_text("keep")
    locks = paths.ensure_private_dir(state.state_root() / bridge.LOCKS_DIRECTORY)
    (locks / f"{LAUNCH_ID}.lock").symlink_to(target)
    result = sync()
    assert (result.launches, result.imported) == (0, 0)
    assert any("sync lock" in error for error in result.errors)
    assert target.read_text() == "keep"
    assert bridged_ids() == []


def test_sync_all_reads_the_store_once(fake, launch, monkeypatch):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    reads = []
    real = discovery.read_stored_sessions
    monkeypatch.setattr(discovery, "read_stored_sessions",
                        lambda *args, **kwargs: reads.append(1) or real(*args, **kwargs))
    assert sync().imported == 1
    assert len(reads) == 1
    reads.clear()
    sync()
    assert len(reads) == 1
    assert [item["session_id"] for item in sessions()] == ["s1"]


def test_parse_pull_stops_at_broken_framing():
    good = "F a.json\neyJ4IjoxfQ==\n.\n"
    files, dropped = bridge.parse_pull(good + "garbage line\nF b.json\nZZ\n.\n")
    assert [item.name for item in files] == ["a.json"]
    assert dropped == 1
    files, dropped = bridge.parse_pull("F a.json\n!!!\n.\nO big.json\nX\nF c.json\nYQ==\n")
    assert files == []
    assert dropped == 4


def test_pi_events_keep_their_sequence_order(fake, pane_process):
    _process, info = pane_process
    create_sandbox(fake)
    launch = make_launch(info, harness="pi")
    spool = spool_dir(fake, launch)

    def event(sequence, status):
        return json.dumps({
            "schema_version": 1, "event_id": f"pi.e{sequence}", "session_id": "pi-1",
            "instance_id": "inner", "sequence": sequence, "pid": 4, "cwd": "/sandbox/w",
            "status": status, "detail": status, "tool": "",
        })

    put(spool, 1, "fleet-event", event(1, "started"))
    put(spool, 2, "fleet-event", event(3, "idle"))
    put(spool, 3, "fleet-event", event(2, "running"))
    result = sync()
    assert (result.imported, result.rejected) == (2, 1)
    [record] = [item for item in sessions() if item["session_id"] == "pi-1"]
    assert (record["status"], record["sequence"]) == ("idle", 3)
    assert record["instance_id"] == f"{info.pid}:{info.start_token}"


def test_phase_record_stands_in_until_hook_data_arrives(fake, pane_process):
    _process, info = pane_process
    create_sandbox(fake, phases="Provisioning,Ready")
    launch = make_launch(info)
    sync()
    [phase] = sessions()
    assert phase["session_id"] == f"sandbox-{LAUNCH_ID}"
    assert (phase["status"], phase["detail"]) == ("started", "sandbox provisioning")
    sync()
    [phase] = sessions()
    assert phase["detail"] == "sandbox ready; waiting for the agent"
    put(spool_dir(fake, launch), 1, "session-start", payload())
    sync()
    sync()
    assert [item["session_id"] for item in sessions()] == ["s1"]


@pytest.mark.parametrize("phase, status", [("Error", "error"), ("Completed", "ended")])
def test_phase_maps_to_status(fake, pane_process, phase, status):
    _process, info = pane_process
    create_sandbox(fake, phases=phase)
    make_launch(info)
    sync()
    [record] = sessions()
    assert record["status"] == status


def test_unchanged_phase_is_not_rewritten(fake, launch):
    sync()
    first = sessions()[0]["arrival_sequence"]
    sync()
    assert sessions()[0]["arrival_sequence"] == first


def test_maintenance_keeps_imported_records_while_the_pane_lives(fake, launch, pane_process):
    process, _info = pane_process
    put(spool_dir(fake, launch), 1, "session-start", payload())
    sync()
    assert session_store.maintain_store(force=True).ran
    assert [item["session_id"] for item in sessions()] == ["s1"]
    process.kill()
    process.wait()
    session_store.maintain_store(force=True)
    assert sessions() == []
    sync()
    assert state.list_records() == []


def test_a_forged_sandbox_record_does_not_survive_maintenance(fake, launch, pane_process):
    _process, info = pane_process
    put(spool_dir(fake, launch), 1, "session-start", payload())
    sync()
    [record] = sessions()
    path = next(paths.sessions_dir().glob("*.json"))
    forged = dict(record, launch={"launch_id": "e" * 32, "profile_id": "x"})
    path.write_text(json.dumps(forged))
    session_store.maintain_store(force=True)
    assert sessions() == []


def test_stale_launch_state_is_removed(fake, pane_process):
    _process, info = pane_process
    make_launch(info)
    (paths.launches_dir() / f"{LAUNCH_ID}.json").unlink()
    result = sync()
    assert result.launches == 0
    assert state.list_records() == []


def test_bridge_writes_only_in_the_sessions_directory(fake, launch, tmp_path):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    paths.sessions_dir(create=True)

    def snapshot():
        return {
            path: path.stat().st_mtime_ns for path in tmp_path.rglob("*")
            if "fake-openshell" not in path.relative_to(tmp_path).parts
        }

    before = snapshot()
    sync()
    after = snapshot()
    changed = {path for path in after if before.get(path) != after[path]}
    assert changed
    sessions_dir = paths.sessions_dir()
    locks = state.state_root() / bridge.LOCKS_DIRECTORY
    allowed = {state.state_root(), locks, locks / f"{LAUNCH_ID}.lock"}
    assert all(
        path in allowed or path == sessions_dir or sessions_dir in path.parents
        for path in changed
    ), changed


def test_unreachable_cli_is_reported_not_raised(fake, launch, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "exec,get")
    result = sync()
    assert result.imported == 0
    assert result.errors


def test_sync_without_sandboxed_launches_runs_nothing(monkeypatch):
    monkeypatch.setattr(OpenShell, "__init__", lambda *a, **k: pytest.fail("no CLI call"))
    assert bridge.sync_all().launches == 0


def test_session_feed_runs_the_bridge_before_maintenance(fake, launch, monkeypatch):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    order = []
    real = bridge.sync_all
    monkeypatch.setattr(bridge, "sync_all", lambda: order.append("sync") or real())
    monkeypatch.setattr(session_store, "maintain_store", lambda: order.append("maintain"))
    snapshot = SessionFeed().snapshot()
    assert order == ["sync", "maintain"]
    assert [session.session_id for session in snapshot] == ["s1"]


def test_module_sync_entry_point(fake, launch):
    put(spool_dir(fake, launch), 1, "session-start", payload())
    assert module_main(["sync"]) == 0
    assert [item["session_id"] for item in sessions()] == ["s1"]


def test_summary_lines_report_replaced_and_busy_only_when_present():
    assert bridge.summary_lines(bridge.SyncResult()) == [
        "0 sandboxed launch(es): 0 event(s) imported, 0 rejected, 0 phase update(s)",
    ]
    lines = bridge.summary_lines(bridge.SyncResult(launches=2, replaced=1, busy=2))
    assert lines[1:] == [
        f"1 ended session replaced to stay within {bridge.MAX_SESSIONS_PER_LAUNCH} per launch",
        "2 launches busy in another process; try again",
    ]
    assert bridge.summary_lines(bridge.SyncResult(replaced=3, busy=1))[1:] == [
        f"3 ended sessions replaced to stay within {bridge.MAX_SESSIONS_PER_LAUNCH} per launch",
        "1 launch busy in another process; try again",
    ]


def test_module_sync_prints_busy_and_replaced(monkeypatch, capsys):
    monkeypatch.setattr(bridge, "sync_all", lambda: bridge.SyncResult(replaced=1, busy=1))
    assert module_main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "1 launch busy in another process; try again" in out
    assert "1 ended session replaced" in out


def test_validator_clear_config_reports_a_cli_failure_as_not_cleared(tmp_path):
    validator = load_validator()

    class Failing:
        def exec_capture(self, *args, **kwargs):
            raise OpenShellError("openshell sandbox exec: gateway unreachable")

    profile = parse_profile("validator", {
        "command": ["claude"], "harness": "claude", "cwd": str(tmp_path),
        "sandbox": {"name": "tongs-validate-unit", "image": "example/fake-agent:1"},
    })
    assert validator.clear_config(Failing(), "tongs-validate-unit", profile, LAUNCH_ID) is False


def test_chat_relay_is_a_stub_seam(fake, launch):
    assert bridge.relay_chat(launch, OpenShell()) == 0


# Process discovery (row 10)


def test_processes_in_another_pid_namespace_are_not_discovered(monkeypatch):
    monkeypatch.setattr(discovery, "_find_agent_processes", lambda: [("claude", 4242)])
    info = discovery.ProcessInfo(4242, 1, "claude", "claude", ("claude",), "/w", "77")
    monkeypatch.setattr(discovery, "get_process_info", lambda pid: info)
    monkeypatch.setattr(discovery, "in_other_pid_namespace", lambda pid: True)
    discovery.discover_processes()
    assert sessions() == []
    monkeypatch.setattr(discovery, "in_other_pid_namespace", lambda pid: False)
    discovery.discover_processes()
    assert [item["pid"] for item in sessions()] == ["4242"]


@pytest.mark.skipif(not os.path.exists("/proc/self/ns/pid"), reason="Linux /proc only")
def test_pid_namespace_comparison(monkeypatch):
    links = {"self": "pid:[1]", "10": "pid:[1]", "11": "pid:[2]"}

    def readlink(path):
        key = path.split("/")[2]
        if key not in links:
            raise PermissionError(path)
        return links[key]

    monkeypatch.setattr(discovery.os, "readlink", readlink)
    assert discovery.in_other_pid_namespace(10) is False
    assert discovery.in_other_pid_namespace(11) is True
    assert discovery.in_other_pid_namespace(12) is False


@pytest.mark.skipif(not os.path.exists("/proc/self/ns/pid"), reason="Linux /proc only")
@pytest.mark.parametrize("init_readable", [True, False])
def test_a_sidebar_in_its_own_pid_namespace_still_sees_host_agents(monkeypatch,
                                                                   init_readable):
    init = "pid:[100]" if init_readable else discovery.INIT_PID_NAMESPACE
    links = {"self": "pid:[5]", "10": init, "11": "pid:[6]", "12": "pid:[5]"}
    if init_readable:
        links["1"] = init

    def readlink(path):
        key = path.split("/")[2]
        if key not in links:
            raise PermissionError(path)
        return links[key]

    monkeypatch.setattr(discovery.os, "readlink", readlink)
    assert discovery.in_other_pid_namespace(10) is False
    assert discovery.in_other_pid_namespace(11) is True
    assert discovery.in_other_pid_namespace(12) is False


def test_own_pid_namespace_is_not_other():
    assert discovery.in_other_pid_namespace(os.getpid()) is False


def test_pull_script_is_plain_posix_sh():
    result = subprocess.run(["sh", "-n", "-c", bridge.PULL_SCRIPT], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
