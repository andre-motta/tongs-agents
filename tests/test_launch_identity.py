"""Optional launch identity carried in hook records (issues #10 and #74)."""

import io
import json
import os
import subprocess
import sys
import time

import pytest

from tongs_agents import _envcheck, discovery, hook, launches, mcp_server, paths
from tongs_agents.discovery import ProcessInfo, read_session_records
from tongs_agents.hook import handle
from tongs_agents.models import (
    LAUNCH_ID_ENV,
    PROFILE_ID_ENV,
    SCHEMA_VERSION,
    canonical_session_id,
    parse_session,
    validated_launch,
)
from tongs_agents.terminal_apis import _env
from tests import test_pi_extension
from tests.conftest import write_session
from tests.test_harness_contract import make_record
from tests.test_hook import read_new_record, run_hook

LAUNCH_ID = "0123456789abcdef0123456789abcdef"
OTHER_LAUNCH_ID = "fedcba9876543210fedcba9876543210"
PAYLOAD = {"session_id": "native-1", "cwd": "/tmp/repo"}

BAD_LAUNCH_IDS = [
    "",
    "0123456789ABCDEF0123456789ABCDEF",
    LAUNCH_ID[:-1],
    LAUNCH_ID + "0",
    LAUNCH_ID + "\n",
    " " + LAUNCH_ID,
    "0123456789abcdef0123456789abcdeg",
    "\u0660" * 32,
    "a" * 4096,
]
BAD_PROFILES = [
    "",
    "-reviewer",
    "_reviewer",
    "Reviewer",
    "a" * 33,
    "has space",
    "a/b",
    "a.b",
    "reviewer\n",
    "r\u00e9viewer",
]


@pytest.fixture(autouse=True)
def _no_inherited_launch(monkeypatch):
    monkeypatch.delenv(LAUNCH_ID_ENV, raising=False)
    monkeypatch.delenv(PROFILE_ID_ENV, raising=False)


def set_launch(monkeypatch, launch_id=LAUNCH_ID, profile="reviewer"):
    monkeypatch.setenv(LAUNCH_ID_ENV, launch_id)
    monkeypatch.setenv(PROFILE_ID_ENV, profile)


HOOK_PID = 12345
HOOK_START = "100"


def write_claim(launch_id=LAUNCH_ID, pid=HOOK_PID, start=HOOK_START, *,
                harness="claude", profile="reviewer", state="running"):
    """A launch record for ``launch_id`` claimed by ``pid`` (the launcher)."""
    if start is None and state == "running":
        info = discovery.get_process_info(pid)
        start = info.start_token if info is not None else None
    record = launches.LaunchRecord(
        launch_id=launch_id,
        profile=profile,
        harness=harness,
        multiplexer="zellij",
        session="agents",
        created=int(time.time()),
        pane_id="1" if state == "running" else None,
        pid=pid if state == "running" else None,
        process_start=start if state == "running" else None,
        state=state,
    )
    return launches._write(record)


@pytest.fixture
def claimed(fleet_dir):
    """Both test launch ids claimed by the process ``run_hook`` resolves."""
    write_claim(LAUNCH_ID)
    write_claim(OTHER_LAUNCH_ID, profile="other")


def run_pi(monkeypatch, sequence, status="started", event="session-start"):
    process = ProcessInfo(303, 1, "node", "node", ("node",), "/tmp/repo", "40")
    monkeypatch.setattr(
        "tongs_agents.hook.resolve_process_identity",
        lambda harness, pid=None: process,
    )
    monkeypatch.setattr(
        "tongs_agents.terminal_apis.capture_terminal_info",
        lambda: {"terminal": "test", "terminal_env": {}},
    )
    payload = {
        "schema_version": 1,
        "event_id": f"pi.{event}",
        "session_id": "pi-native",
        "instance_id": "extension-process-token",
        "sequence": sequence,
        "pid": 303,
        "cwd": "/tmp/repo",
        "status": status,
        "detail": "",
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return handle("fleet-event", "pi")


def write_raw_record(fleet_dir, record):
    path = discovery._record_path(record["canonical_id"])
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_session_start_captures_launch(fleet_dir, claimed, monkeypatch, agent):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, agent=agent)
    record = read_new_record(fleet_dir)
    assert record["launch"] == {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}
    assert record["schema_version"] == SCHEMA_VERSION == 1


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_first_event_without_start_captures_launch(
    fleet_dir, claimed, monkeypatch, agent
):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "prompt-submit", PAYLOAD, agent=agent)
    assert read_new_record(fleet_dir)["launch"]["launch_id"] == LAUNCH_ID


def test_pi_fleet_event_captures_and_carries_launch(fleet_dir, monkeypatch):
    write_claim(LAUNCH_ID, 303, "40", harness="pi", profile="pi-local")
    write_claim(OTHER_LAUNCH_ID, 303, "40", harness="pi", profile="other")
    set_launch(monkeypatch, profile="pi-local")
    assert run_pi(monkeypatch, 1)
    assert read_new_record(fleet_dir)["launch"] == {
        "launch_id": LAUNCH_ID, "profile_id": "pi-local",
    }
    set_launch(monkeypatch, OTHER_LAUNCH_ID, "other")
    assert run_pi(monkeypatch, 2, status="running", event="tool-execution-start")
    assert read_new_record(fleet_dir)["launch"]["launch_id"] == LAUNCH_ID


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_launch_is_carried_forward_on_later_events(fleet_dir, claimed, monkeypatch, agent):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, agent=agent)
    monkeypatch.delenv(LAUNCH_ID_ENV)
    monkeypatch.delenv(PROFILE_ID_ENV)
    assert run_hook(monkeypatch, "tool-use", dict(PAYLOAD, tool_name="Bash"), agent=agent)
    set_launch(monkeypatch, OTHER_LAUNCH_ID, "other")
    assert run_hook(monkeypatch, "stop", PAYLOAD, agent=agent)
    record = read_new_record(fleet_dir)
    assert record["status"] == "idle"
    assert record["launch"] == {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}


def test_session_start_recaptures_launch(fleet_dir, claimed, monkeypatch):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    set_launch(monkeypatch, OTHER_LAUNCH_ID, "other")
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    assert read_new_record(fleet_dir)["launch"] == {
        "launch_id": OTHER_LAUNCH_ID, "profile_id": "other",
    }
    monkeypatch.delenv(LAUNCH_ID_ENV)
    monkeypatch.delenv(PROFILE_ID_ENV)
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    assert "launch" not in read_new_record(fleet_dir)


def test_missing_env_writes_no_launch_key(fleet_dir, monkeypatch):
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    record = read_new_record(fleet_dir)
    assert "launch" not in record
    assert parse_session(record).launch_id == ""


@pytest.mark.parametrize("bad", BAD_LAUNCH_IDS)
def test_bad_launch_id_is_omitted_and_event_kept(fleet_dir, monkeypatch, bad):
    set_launch(monkeypatch, bad, "reviewer")
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    record = read_new_record(fleet_dir)
    assert record["status"] == "started"
    assert record["launch"] == {"profile_id": "reviewer"}


@pytest.mark.parametrize("bad", BAD_PROFILES)
def test_bad_profile_is_omitted_and_event_kept(fleet_dir, claimed, monkeypatch, bad):
    set_launch(monkeypatch, LAUNCH_ID, bad)
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    assert read_new_record(fleet_dir)["launch"] == {"launch_id": LAUNCH_ID}


def test_both_values_bad_omits_launch(fleet_dir, monkeypatch):
    set_launch(monkeypatch, "not-a-launch", "Not A Profile")
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    assert "launch" not in read_new_record(fleet_dir)


@pytest.mark.parametrize(
    "launch",
    [
        "launch",
        [LAUNCH_ID],
        {},
        {"launch_id": LAUNCH_ID, "extra": "x"},
        {"launch_id": LAUNCH_ID.upper()},
        {"launch_id": LAUNCH_ID, "profile_id": "Bad"},
        {"launch_id": "a" * 2000, "profile_id": "p" * 2000},
        {"launch_id": 123},
        {"launch_id": {"nested": LAUNCH_ID}},
        {"profile_id": None},
    ],
)
def test_malformed_stored_launch_is_dropped_and_record_kept(fleet_dir, launch):
    record = make_record()
    record["launch"] = launch
    write_raw_record(fleet_dir, record)
    records = read_session_records()
    assert [item["canonical_id"] for item in records] == [record["canonical_id"]]
    assert "launch" not in records[0]
    assert parse_session(records[0]).launch_id == ""


def test_oversized_launch_record_follows_the_record_size_limit(fleet_dir):
    record = make_record()
    record["launch"] = {"launch_id": "a" * (discovery.MAX_RECORD_BYTES + 1)}
    write_raw_record(fleet_dir, record)
    assert read_session_records() == []


def test_stored_launch_round_trips_to_session_and_json(fleet_dir):
    record = make_record()
    record["launch"] = {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}
    write_raw_record(fleet_dir, record)
    stored = read_session_records()[0]
    assert stored["launch"] == record["launch"]
    session = parse_session(stored)
    assert (session.launch_id, session.profile_id) == (LAUNCH_ID, "reviewer")
    rendered = json.loads(session.to_json())
    assert rendered["launch_id"] == LAUNCH_ID
    assert rendered["profile_id"] == "reviewer"


def test_hook_carries_forward_only_a_valid_stored_launch(fleet_dir, monkeypatch):
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    record = read_new_record(fleet_dir)
    record["launch"] = {"launch_id": "BAD"}
    write_raw_record(fleet_dir, record)
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "tool-use", dict(PAYLOAD, tool_name="Read"))
    record = read_new_record(fleet_dir)
    assert record["status"] == "running"
    assert "launch" not in record


def test_old_records_parse_with_empty_launch_fields(fleet_dir):
    old = make_record()
    session = parse_session(old)
    assert (session.launch_id, session.profile_id) == ("", "")
    rendered = json.loads(session.to_json())
    assert (rendered["launch_id"], rendered["profile_id"]) == ("", "")
    write_session(fleet_dir, "legacy", "repo", "/tmp/repo")
    legacy = read_session_records()[0]
    assert parse_session(legacy).profile_id == ""


def test_parse_session_ignores_malformed_launch_without_reader():
    record = make_record()
    record["launch"] = {"launch_id": "x" * 64, "profile_id": "reviewer"}
    session = parse_session(record)
    assert (session.launch_id, session.profile_id) == ("", "")


def test_validated_launch_returns_a_copy():
    launch = {"launch_id": LAUNCH_ID}
    result = validated_launch(launch)
    assert result == launch and result is not launch


def test_mcp_fleet_session_shows_launch(fleet_dir, monkeypatch):
    monkeypatch.setattr(discovery, "discover_processes", lambda: None)
    monkeypatch.setattr(discovery, "_cleanup_stale_sessions", lambda: None)
    record = make_record(session="launched")
    record["launch"] = {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}
    write_raw_record(fleet_dir, record)
    old = make_record(session="plain", instance="102:20")
    old["pid"] = "102"
    write_raw_record(fleet_dir, old)
    shown = json.loads(mcp_server.fleet_session("launched"))
    assert shown["launch_id"] == LAUNCH_ID
    assert shown["profile_id"] == "reviewer"
    plain = json.loads(mcp_server.fleet_session("plain"))
    assert (plain["launch_id"], plain["profile_id"]) == ("", "")


V010_STRING_FIELDS = (
    "record_kind", "canonical_id", "session_id", "instance_id", "harness_id",
    "agent", "repo", "cwd", "pid", "process_start", "process_identity",
    "status", "detail", "tool", "source", "last_event", "terminal",
)


def v010_record_errors(record):
    """The v1 record contract a v0.10 reader enforces; unknown keys are kept."""
    errors = []
    if record.get("schema_version") != 1:
        errors.append("schema_version")
    for key in V010_STRING_FIELDS:
        if not isinstance(record.get(key), str):
            errors.append(key)
    for key in ("ts", "started", "arrival_sequence"):
        if not isinstance(record.get(key), int):
            errors.append(key)
    if not isinstance(record.get("terminal_env"), dict):
        errors.append("terminal_env")
    if not isinstance(record.get("focus_target"), dict):
        errors.append("focus_target")
    expected = canonical_session_id(
        record.get("harness_id", ""), record.get("session_id", ""),
        record.get("instance_id", ""),
    )
    if record.get("canonical_id") != expected:
        errors.append("canonical_id derivation")
    return errors


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_v010_style_validator_accepts_records_with_launch(
    fleet_dir, claimed, monkeypatch, agent
):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, agent=agent)
    written = read_new_record(fleet_dir)
    assert "launch" in written
    assert v010_record_errors(written) == []
    without = {key: value for key, value in written.items() if key != "launch"}
    assert discovery._validate_record(without) is not None
    assert discovery._validate_record(written)["launch"] == written["launch"]


# Launch identity is verified against the claimed process (#74)

FIRST_TUI = ProcessInfo(5151, 77, "codex", "codex", ("codex",), "/tmp/repo", "950")
LATER_TUI = ProcessInfo(6161, 88, "codex", "codex", ("codex",), "/tmp/repo", "960")
DAEMON = ProcessInfo(
    4242, 1, "codex", "codex", ("codex", "app-server", "--listen"), "/tmp/repo", "900"
)


def codex_start(monkeypatch, session_id, process):
    monkeypatch.setattr(
        "tongs_agents.hook.resolve_process_identity", lambda harness, pid=None: process
    )
    monkeypatch.setattr(
        "tongs_agents.terminal_apis.capture_terminal_info",
        lambda: {"terminal": "test", "terminal_env": {}},
    )
    payload = {"session_id": session_id, "cwd": "/tmp/repo"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return handle("session-start", "codex")


def stored_launches():
    return {
        record["session_id"]: record.get("launch")
        for record in read_session_records()
    }


def test_daemon_hosted_second_tui_with_first_launch_env_records_no_launch(
    fleet_dir, monkeypatch
):
    write_claim(LAUNCH_ID, FIRST_TUI.pid, FIRST_TUI.start_token, harness="codex")
    write_claim(OTHER_LAUNCH_ID, LATER_TUI.pid, LATER_TUI.start_token,
                harness="codex", profile="other")
    # The daemon hosts both TUIs' sessions and always carries the first
    # daemon-attached launch's environment.
    set_launch(monkeypatch)
    assert codex_start(monkeypatch, "first", DAEMON)
    assert codex_start(monkeypatch, "later", DAEMON)
    # A --no-daemon TUI runs its hooks under its own, claimed, process.
    set_launch(monkeypatch, OTHER_LAUNCH_ID, "other")
    assert codex_start(monkeypatch, "in-process", LATER_TUI)

    assert stored_launches() == {
        "first": None,
        "later": None,
        "in-process": {"launch_id": OTHER_LAUNCH_ID, "profile_id": "other"},
    }


def test_a_session_under_the_claimed_process_keeps_its_launch(fleet_dir, monkeypatch):
    wrapper = ProcessInfo(700, 1, "claude-alt", "bash", ("claude-alt",), None, "7")
    shell = ProcessInfo(701, 700, "bash", "bash", ("bash",), None, "8")
    agent = ProcessInfo(HOOK_PID, 701, "claude", "claude", ("claude",), None, HOOK_START)
    table = {process.pid: process for process in (wrapper, shell)}
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    monkeypatch.setattr(
        "tongs_agents.hook.resolve_process_identity", lambda harness, pid=None: agent
    )
    write_claim(LAUNCH_ID, wrapper.pid, wrapper.start_token)
    set_launch(monkeypatch)
    assert run_hook_resolved(monkeypatch, agent)
    assert read_new_record(fleet_dir)["launch"]["launch_id"] == LAUNCH_ID


def run_hook_resolved(monkeypatch, process):
    monkeypatch.setattr(
        "tongs_agents.hook.resolve_process_identity", lambda harness, pid=None: process
    )
    monkeypatch.setattr(
        "tongs_agents.terminal_apis.capture_terminal_info",
        lambda: {"terminal": "test", "terminal_env": {}},
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(PAYLOAD)))
    return handle("session-start", "claude")


def _symlinked(launch_id):
    target = write_claim(OTHER_LAUNCH_ID)
    path = paths.launches_dir() / f"{launch_id}.json"
    path.symlink_to(paths.launches_dir() / f"{target.launch_id}.json")


def _misnamed(launch_id):
    write_claim(OTHER_LAUNCH_ID)
    os.replace(
        paths.launches_dir() / f"{OTHER_LAUNCH_ID}.json",
        paths.launches_dir() / f"{launch_id}.json",
    )


def _raw(text):
    def write(launch_id):
        paths.launches_dir(create=True)
        (paths.launches_dir() / f"{launch_id}.json").write_text(text)
    return write


def _claim_payload(**changes):
    data = {
        "schema_version": 1, "launch_id": LAUNCH_ID, "profile": "reviewer",
        "harness": "claude", "multiplexer": "zellij", "session": "agents",
        "created": 1, "pid": HOOK_PID, "process_start": HOOK_START,
        "state": "running",
    }
    data.update(changes)
    return json.dumps(data)


UNVERIFIED = {
    "no launch record": lambda launch_id: None,
    "pending, not claimed": lambda launch_id: write_claim(launch_id, state="pending"),
    "pid reused (other start token)": lambda launch_id: write_claim(
        launch_id, HOOK_PID, "999"
    ),
    "claimed by another process": lambda launch_id: write_claim(launch_id, 999),
    "record is a symlink": _symlinked,
    "record names another id": _misnamed,
    "record is not JSON": _raw("{"),
    "record is oversized": _raw(" " * (64 * 1024) + _claim_payload()),
    "record has another schema": _raw(_claim_payload(schema_version=2)),
    "record pid is not an integer": _raw(_claim_payload(pid="12345")),
    "record pid is a bool": _raw(_claim_payload(pid=True)),
    "record start token is empty": _raw(_claim_payload(process_start="")),
}


@pytest.mark.parametrize("case", sorted(UNVERIFIED))
def test_an_unverified_env_launch_records_no_identity(fleet_dir, monkeypatch, case):
    UNVERIFIED[case](LAUNCH_ID)
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    record = read_new_record(fleet_dir)
    assert record["status"] == "started"
    assert "launch" not in record


def test_a_valid_raw_claim_is_read_like_launches_reads_it(fleet_dir):
    _raw(_claim_payload())(LAUNCH_ID)
    assert hook._launch_claim(LAUNCH_ID) == (HOOK_PID, HOOK_START)
    assert launches.get(LAUNCH_ID).pid == HOOK_PID
    _raw(_claim_payload(process_start=None))(LAUNCH_ID)
    assert hook._launch_claim(LAUNCH_ID) == (HOOK_PID, None)


def test_an_unresolved_process_records_no_launch(fleet_dir, monkeypatch, claimed):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, resolved=False)
    record = read_new_record(fleet_dir)
    assert record["process_identity"] == "unresolved"
    assert "launch" not in record


def test_an_unresolved_process_under_the_launcher_keeps_its_launch(fleet_dir, monkeypatch):
    launcher = ProcessInfo(800, 1, "python3", "python3", ("python3",), None, "9")
    hook_parent = ProcessInfo(801, 800, "sh", "sh", ("sh",), None, "10")
    table = {process.pid: process for process in (launcher, hook_parent)}
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    monkeypatch.setattr(hook.os, "getppid", lambda: hook_parent.pid)
    write_claim(LAUNCH_ID, launcher.pid, launcher.start_token)
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, resolved=False)
    record = read_new_record(fleet_dir)
    assert record["process_identity"] == "unresolved"
    assert record["launch"]["launch_id"] == LAUNCH_ID


def test_a_profile_without_a_launch_id_is_kept_unverified(fleet_dir, monkeypatch):
    set_launch(monkeypatch, "not-a-launch")
    assert run_hook(monkeypatch, "session-start", PAYLOAD)
    assert read_new_record(fleet_dir)["launch"] == {"profile_id": "reviewer"}


def test_hook_claim_reader_mirrors_the_launch_record_format():
    assert hook._LAUNCH_SCHEMA_VERSION == launches.SCHEMA_VERSION
    assert hook._MAX_LAUNCH_RECORD_BYTES == launches.MAX_RECORD_BYTES


def test_hook_claim_reader_never_raises_on_an_unusable_state_dir(monkeypatch):
    def broken(create=False):
        raise paths.UnsafePathError("refused")

    monkeypatch.setattr(paths, "launches_dir", broken)
    assert hook._launch_claim(LAUNCH_ID) is None


def lineage(*processes):
    table = {process.pid: process for process in processes}
    return table.get


def test_lineage_walk_matches_the_process_or_an_ancestor():
    root = ProcessInfo(10, 1, "launcher", "python", (), None, "a")
    child = ProcessInfo(11, 10, "sh", "sh", (), None, "b")
    leaf = ProcessInfo(12, 11, "claude", "claude", (), None, "c")
    lookup = lineage(root, child)
    assert hook.process_descends_from(leaf, 12, "c", lookup)
    assert hook.process_descends_from(leaf, 10, "a", lookup)
    assert hook.process_descends_from(leaf, 10, None, lookup)
    assert not hook.process_descends_from(leaf, 10, "reused", lookup)
    assert not hook.process_descends_from(leaf, 99, "a", lookup)
    assert not hook.process_descends_from(None, 10, "a", lookup)
    assert not hook.process_descends_from(leaf, True, "a", lookup)
    assert not hook.process_descends_from(leaf, 0, "a", lookup)


def test_lineage_walk_stops_on_a_cycle_and_after_its_step_limit():
    first = ProcessInfo(20, 21, "a", "a", (), None, "x")
    second = ProcessInfo(21, 20, "b", "b", (), None, "y")
    assert not hook.process_descends_from(first, 99, "z", lineage(first, second))

    chain = [
        ProcessInfo(1000 + step, 1000 + step + 1, "p", "p", (), None, str(step))
        for step in range(hook.MAX_LINEAGE_STEPS + 1)
    ]
    calls = []
    table = lineage(*chain)

    def lookup(pid):
        calls.append(pid)
        return table(pid)

    top = chain[-1]
    assert not hook.process_descends_from(chain[0], top.pid, top.start_token, lookup)
    assert len(calls) == hook.MAX_LINEAGE_STEPS - 1
    within = chain[hook.MAX_LINEAGE_STEPS - 1]
    assert hook.process_descends_from(chain[0], within.pid, within.start_token, table)


# Reader-side check: launches.claims_session


def claimed_record(pid=500, start="50", state="running"):
    return launches.LaunchRecord(
        launch_id=LAUNCH_ID, profile="reviewer", harness="codex",
        multiplexer="zellij", session="agents", created=1, pane_id="1",
        pid=pid if state == "running" else None,
        process_start=start if state == "running" else None, state=state,
    )


def test_claims_session_matches_the_exact_process_without_a_lookup(monkeypatch):
    monkeypatch.setattr(discovery, "get_process_info", lambda pid: pytest.fail("looked up"))
    record = claimed_record()
    assert launches.claims_session(record, {"pid": "500", "process_start": "50"})
    assert not launches.claims_session(record, {"pid": "500", "process_start": "51"})
    assert not launches.claims_session(record, {"pid": "500", "process_start": ""})
    no_token = claimed_record(start=None)
    assert launches.claims_session(no_token, {"pid": "500", "process_start": ""})


def test_claims_session_walks_a_live_descendant(monkeypatch):
    launcher = ProcessInfo(500, 1, "launcher", "python", (), None, "50")
    agent = ProcessInfo(600, 500, "codex", "codex", (), None, "60")
    monkeypatch.setattr(discovery, "get_process_info", lineage(launcher, agent))
    record = claimed_record()
    assert launches.claims_session(record, {"pid": "600", "process_start": "60"})
    assert not launches.claims_session(record, {"pid": "600", "process_start": "61"})
    assert not launches.claims_session(record, {"pid": "700", "process_start": "60"})


def test_claims_session_rejects_the_daemon(monkeypatch):
    daemon = ProcessInfo(4242, 1, "codex", "codex", (), None, "900")
    monkeypatch.setattr(discovery, "get_process_info", lineage(daemon))
    data = {"pid": "4242", "process_start": "900"}
    assert not launches.claims_session(claimed_record(), data)


@pytest.mark.parametrize(
    "data",
    [{}, {"pid": ""}, {"pid": 500}, {"pid": "+500"}, {"pid": "\u0665\u0660\u0660"},
     {"pid": "500", "process_start": 50}, "500"],
)
def test_claims_session_rejects_malformed_session_records(data):
    assert not launches.claims_session(claimed_record(), data)


def test_claims_session_rejects_an_unclaimed_record():
    pending = claimed_record(state="pending")
    assert not launches.claims_session(pending, {"pid": "500", "process_start": "50"})


def hook_subprocess(tmp_path, extra_env):
    environment = {
        key: value for key, value in os.environ.items()
        if key not in (LAUNCH_ID_ENV, PROFILE_ID_ENV)
    }
    environment["HOME"] = str(tmp_path / "home")
    environment.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "tongs_agents.hook", "session-start",
         "--agent", "claude"],
        input=json.dumps({"session_id": "subprocess", "cwd": str(tmp_path)}).encode(),
        env=environment,
        capture_output=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("launch_id", "profile"),
    [
        ("a" * 4096, "Bad Profile"),
        (b"\xff" * 32, b"\xfe\xff"),
        (LAUNCH_ID.upper(), "-x"),
        ("", ""),
    ],
)
def test_hook_exits_zero_with_malformed_env(fleet_dir, tmp_path, launch_id, profile):
    result = hook_subprocess(
        tmp_path, {LAUNCH_ID_ENV: launch_id, PROFILE_ID_ENV: profile}
    )
    assert result.returncode == 0, result.stderr
    records = read_session_records()
    assert [record["session_id"] for record in records] == ["subprocess"]
    assert "launch" not in records[0]


CLAIMING_AGENT = """
import os, subprocess, sys
from tongs_agents import discovery, launches
from tongs_agents.profiles import Profile

claimant = int(sys.argv[1]) or os.getpid()
pending = launches.create_pending(
    Profile("reviewer", "claude", ("claude",)), "agents", "zellij"
)
info = discovery.get_process_info(claimant)
record = launches.claim(pending.launch_id, claimant, info.start_token, None)
env = dict(os.environ, TONGS_AGENTS_LAUNCH_ID=record.launch_id,
           TONGS_AGENTS_PROFILE="reviewer")
result = subprocess.run(
    [os.path.realpath(sys.executable), "-m", "tongs_agents.hook",
     "session-start", "--agent", "claude"],
    input=sys.stdin.buffer.read(), env=env, timeout=30,
)
print(record.launch_id)
sys.exit(result.returncode)
"""


def run_claiming_agent(tmp_path, claimant=0):
    """A process named ``claude`` claims a launch, then runs the real hook.

    ``claimant`` 0 claims the agent itself; another pid stands for the first
    daemon-attached launch whose environment a later session inherits.
    """
    agent = tmp_path / "bin" / "claude"
    agent.parent.mkdir()
    agent.symlink_to(os.path.realpath(sys.executable))
    environment = {
        key: value for key, value in os.environ.items()
        if key not in (LAUNCH_ID_ENV, PROFILE_ID_ENV)
    }
    environment["HOME"] = str(tmp_path / "home")
    environment["PYTHONPATH"] = os.path.dirname(os.path.dirname(hook.__file__))
    result = subprocess.run(
        [str(agent), "-c", CLAIMING_AGENT, str(claimant)],
        input=json.dumps({"session_id": "subprocess", "cwd": str(tmp_path)}).encode(),
        env=environment,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


linux_only = pytest.mark.skipif(
    sys.platform != "linux", reason="process names come from /proc comm"
)


@linux_only
def test_hook_subprocess_records_a_launch_its_agent_claimed(fleet_dir, tmp_path):
    launch_id = run_claiming_agent(tmp_path)
    [record] = read_session_records()
    assert record["launch"] == {"launch_id": launch_id, "profile_id": "reviewer"}
    assert record["pid"] == str(launches.get(launch_id).pid)


@linux_only
def test_hook_subprocess_drops_a_launch_another_process_claimed(fleet_dir, tmp_path):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        launch_id = run_claiming_agent(tmp_path, other.pid)
    finally:
        other.kill()
        other.wait()
    assert launches.get(launch_id).pid == other.pid
    [record] = read_session_records()
    assert record["status"] == "started" and record["pid"]
    assert "launch" not in record


def test_launch_capture_and_terminal_capture_share_one_env_check(monkeypatch):
    monkeypatch.setenv(LAUNCH_ID_ENV, LAUNCH_ID)
    monkeypatch.setenv(PROFILE_ID_ENV, "reviewer")
    assert hook._capture_launch() == {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}
    assert _env.bounded_env((PROFILE_ID_ENV,)) == {PROFILE_ID_ENV: "reviewer"}
    monkeypatch.setattr(_envcheck, "MAX_VALUE_BYTES", len("reviewer") - 1)
    assert hook._capture_launch() is None
    assert _env.bounded_env((PROFILE_ID_ENV,)) == {}


def test_hook_import_path_gains_no_modules():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, tongs_agents.hook; print('\\n'.join(sorted(sys.modules)))"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    modules = set(result.stdout.split())
    assert {name for name in modules if name.startswith("tongs_agents")} == {
        "tongs_agents",
        "tongs_agents._envcheck",
        "tongs_agents._version",
        "tongs_agents.discovery",
        "tongs_agents.harnesses",
        "tongs_agents.hook",
        "tongs_agents.models",
        "tongs_agents.paths",
    }
    for heavy in ("textual", "mcp", "tongs_agents.terminal_apis"):
        assert heavy not in modules


PI_ENV_HOOK = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys\n"
    "sys.stdin.read()\n"
    "with open(os.environ['FLEET_TEST_EVENTS'], 'a', encoding='utf-8') as stream:\n"
    "    stream.write(json.dumps({\n"
    f"        'launch_id': os.environ.get({LAUNCH_ID_ENV!r}),\n"
    f"        'profile_id': os.environ.get({PROFILE_ID_ENV!r}),\n"
    "    }) + '\\n')\n"
)


@pytest.mark.skipif(test_pi_extension.NODE is None, reason="Node.js is not installed")
def test_pi_extension_spawn_passes_launch_env_to_hook(tmp_path, monkeypatch):
    set_launch(monkeypatch, profile="pi-local")
    records, _ = test_pi_extension.run_bridge(tmp_path, """
fire('session_start', { reason: 'startup' });
await fire('session_shutdown', { reason: 'quit' });
""", hook_body=PI_ENV_HOOK)
    assert records
    assert all(
        record == {"launch_id": LAUNCH_ID, "profile_id": "pi-local"}
        for record in records
    )
