"""Optional launch identity carried in hook records (issue #10)."""

import io
import json
import os
import subprocess
import sys

import pytest

from tongs_agents import discovery, mcp_server
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
def test_native_session_start_captures_launch(fleet_dir, monkeypatch, agent):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, agent=agent)
    record = read_new_record(fleet_dir)
    assert record["launch"] == {"launch_id": LAUNCH_ID, "profile_id": "reviewer"}
    assert record["schema_version"] == SCHEMA_VERSION == 1


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_first_event_without_start_captures_launch(
    fleet_dir, monkeypatch, agent
):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "prompt-submit", PAYLOAD, agent=agent)
    assert read_new_record(fleet_dir)["launch"]["launch_id"] == LAUNCH_ID


def test_pi_fleet_event_captures_and_carries_launch(fleet_dir, monkeypatch):
    set_launch(monkeypatch, profile="pi-local")
    assert run_pi(monkeypatch, 1)
    assert read_new_record(fleet_dir)["launch"] == {
        "launch_id": LAUNCH_ID, "profile_id": "pi-local",
    }
    set_launch(monkeypatch, OTHER_LAUNCH_ID, "other")
    assert run_pi(monkeypatch, 2, status="running", event="tool-execution-start")
    assert read_new_record(fleet_dir)["launch"]["launch_id"] == LAUNCH_ID


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_launch_is_carried_forward_on_later_events(fleet_dir, monkeypatch, agent):
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


def test_session_start_recaptures_launch(fleet_dir, monkeypatch):
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
def test_bad_profile_is_omitted_and_event_kept(fleet_dir, monkeypatch, bad):
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
    fleet_dir, monkeypatch, agent
):
    set_launch(monkeypatch)
    assert run_hook(monkeypatch, "session-start", PAYLOAD, agent=agent)
    written = read_new_record(fleet_dir)
    assert "launch" in written
    assert v010_record_errors(written) == []
    without = {key: value for key, value in written.items() if key != "launch"}
    assert discovery._validate_record(without) is not None
    assert discovery._validate_record(written)["launch"] == written["launch"]


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


def test_hook_subprocess_records_launch(fleet_dir, tmp_path):
    result = hook_subprocess(
        tmp_path, {LAUNCH_ID_ENV: LAUNCH_ID, PROFILE_ID_ENV: "reviewer"}
    )
    assert result.returncode == 0, result.stderr
    assert read_session_records()[0]["launch"] == {
        "launch_id": LAUNCH_ID, "profile_id": "reviewer",
    }


def test_hook_import_path_gains_no_modules():
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, tongs_agents.hook; print('\\n'.join(sorted(sys.modules)))"],
        capture_output=True, text=True, timeout=30, check=True,
    )
    modules = set(result.stdout.split())
    assert {name for name in modules if name.startswith("tongs_agents")} == {
        "tongs_agents",
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
