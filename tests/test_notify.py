"""``tongs-agents notify`` for panes without harness hooks (issue #56)."""

import argparse
import json
import os
import shlex
import subprocess
import sys

import pytest

from tests.test_workspace_spawn import FakeMux, profile
from tongs_agents import discovery, launches, paths
from tongs_agents.attention import Attention, attention_state
from tongs_agents.commands import notify
from tongs_agents.discovery import (
    MAX_DETAIL_LENGTH,
    ProcessInfo,
    read_session_records,
)
from tongs_agents.models import (
    LAUNCH_ID_ENV,
    PROFILE_ID_ENV,
    SessionStatus,
    parse_session,
)
from tongs_agents.multiplexers import PaneInfo, PaneRef
from tongs_agents.sessions_feed import NOTIFY_SOURCE, WorkspaceFeed
from tongs_agents.widgets.session_table import status_label

SESSION = "agents"
PID = 4242
START = "777"
ZELLIJ_ENV = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": SESSION, "ZELLIJ_PANE_ID": "3"}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (LAUNCH_ID_ENV, PROFILE_ID_ENV, "TMUX", "TMUX_PANE", *ZELLIJ_ENV):
        monkeypatch.delenv(name, raising=False)


def claimed(name="builder", harness="other", *, process_start=START):
    pending = launches.create_pending(profile(name, harness), SESSION, "zellij")
    return launches.claim(
        pending.launch_id, PID, process_start, PaneRef("zellij", SESSION, "3", None)
    )


def in_pane(monkeypatch, record, *, start=START, alive=True):
    monkeypatch.setenv(LAUNCH_ID_ENV, record.launch_id)
    monkeypatch.setenv(PROFILE_ID_ENV, record.profile)
    for name, value in ZELLIJ_ENV.items():
        monkeypatch.setenv(name, value)
    process = ProcessInfo(PID, 1, "sh", "sh", ("sh",), "/w/repo", start)
    monkeypatch.setattr(
        discovery, "get_process_info",
        lambda pid: process if alive and pid == PID else None,
    )


def call(state, *message):
    return notify.run(argparse.Namespace(state=state, message=list(message)))


def stored():
    return read_session_records()


@pytest.mark.parametrize("state,status,attention", [
    ("working", SessionStatus.RUNNING, Attention.WORKING),
    ("needs-you", SessionStatus.WAITING, Attention.NEEDS_YOU),
    ("done", SessionStatus.IDLE, Attention.DONE),
    ("failed", SessionStatus.ERROR, Attention.FAILED),
    ("quiet", SessionStatus.DISCOVERED, Attention.QUIET),
])
def test_each_state_is_recorded_like_a_hook_state(monkeypatch, state, status, attention):
    record = claimed()
    in_pane(monkeypatch, record)
    assert call(state, "make", "test") == 0
    [data] = stored()
    assert data["schema_version"] == 1
    assert data["harness_id"] == "other"
    assert data["session_id"] == record.launch_id
    assert data["instance_id"] == f"{PID}:{START}"
    assert data["pid"] == str(PID) and data["process_start"] == START
    assert data["source"] == NOTIFY_SOURCE
    assert data["status"] == status.value
    assert data["detail"] == "make test"
    assert data["last_event"] == f"other.notify.{state}"
    assert data["launch"] == {"launch_id": record.launch_id, "profile_id": "builder"}
    assert data["terminal"] == "zellij"
    assert data["terminal_env"]["ZELLIJ_SESSION_NAME"] == SESSION
    assert data["focus_target"]["kind"] == "terminal"
    session = parse_session(data)
    assert attention_state(session) is attention
    assert status_label(session) == attention.value.upper()


def test_a_later_state_replaces_the_earlier_one_and_keeps_started(monkeypatch):
    record = claimed()
    in_pane(monkeypatch, record)
    assert call("working") == 0
    [first] = stored()
    path = paths.sessions_dir() / next(
        name for name in os.listdir(paths.sessions_dir()) if name.endswith(".json")
    )
    data = json.loads(path.read_text())
    data["started"] -= 50
    path.write_text(json.dumps(data))
    assert call("done", "all green") == 0
    [second] = stored()
    assert second["canonical_id"] == first["canonical_id"]
    assert second["status"] == "idle"
    assert second["detail"] == "all green"
    assert second["started"] == first["started"] - 50


def test_the_message_is_one_bounded_printable_line():
    assert notify.bounded_message([], "done") == "done"
    assert notify.bounded_message(["a\nb\x1b[31m", " c\t"], "done") == "a b [31m c"
    long = notify.bounded_message(["\N{SNOWMAN}" * 1000], "done")
    assert len(long.encode("utf-8")) <= MAX_DETAIL_LENGTH
    assert set(long) == {"\N{SNOWMAN}"}


def test_a_long_message_is_written(monkeypatch):
    record = claimed()
    in_pane(monkeypatch, record)
    assert call("failed", "x" * 5000) == 0
    [data] = stored()
    assert data["detail"] == "x" * MAX_DETAIL_LENGTH


@pytest.mark.parametrize("launch_id", ["", "not-hex", "A" * 32])
def test_outside_a_launched_pane_is_refused(monkeypatch, capsys, launch_id):
    if launch_id:
        monkeypatch.setenv(LAUNCH_ID_ENV, launch_id)
    assert call("done") == notify.EXIT_REFUSED
    assert "not in a pane started by tongs-agents" in capsys.readouterr().err
    assert stored() == []


def test_an_unknown_launch_id_is_refused(monkeypatch, capsys):
    monkeypatch.setenv(LAUNCH_ID_ENV, "0" * 32)
    assert call("done") == notify.EXIT_REFUSED
    assert f"no claimed launch record for {'0' * 32}" in capsys.readouterr().err
    assert stored() == []


def test_a_pending_launch_is_refused(monkeypatch, capsys):
    pending = launches.create_pending(profile("builder"), SESSION, "zellij")
    monkeypatch.setenv(LAUNCH_ID_ENV, pending.launch_id)
    assert call("done") == notify.EXIT_REFUSED
    assert "no claimed launch record" in capsys.readouterr().err


def test_a_harness_pane_is_refused(monkeypatch, capsys):
    record = claimed(harness="claude")
    in_pane(monkeypatch, record)
    assert call("done") == notify.EXIT_REFUSED
    assert "through its own hooks" in capsys.readouterr().err
    assert stored() == []


@pytest.mark.parametrize("kwargs", [{"alive": False}, {"start": "778"}])
def test_a_launch_whose_process_is_gone_is_refused(monkeypatch, capsys, kwargs):
    record = claimed()
    in_pane(monkeypatch, record, **kwargs)
    assert call("done") == notify.EXIT_REFUSED
    assert "is no longer running" in capsys.readouterr().err
    assert stored() == []


def test_without_a_start_token_the_record_is_unresolved(monkeypatch):
    record = claimed(process_start=None)
    in_pane(monkeypatch, record)
    assert call("working") == 0
    [data] = stored()
    assert data["process_identity"] == "unresolved"
    assert data["pid"] == ""
    assert data["focus_target"]["kind"] == "unavailable"
    assert data["launch"]["launch_id"] == record.launch_id


@pytest.mark.parametrize("ending", ["process exits", "launch record removed"])
def test_an_unresolved_row_is_dropped_with_its_launch(monkeypatch, ending):
    record = claimed(process_start=None)
    in_pane(monkeypatch, record)
    assert call("working") == 0
    discovery._cleanup_stale_sessions()
    assert len(stored()) == 1
    if ending == "process exits":
        monkeypatch.setattr(discovery, "get_process_info", lambda pid: None)
    else:
        (paths.launches_dir() / f"{record.launch_id}.json").unlink()
    assert discovery._cleanup_stale_sessions() == 1
    assert stored() == []


def test_an_identified_row_is_not_joined_on_its_launch(monkeypatch):
    record = claimed()
    in_pane(monkeypatch, record)
    assert call("working") == 0
    (paths.launches_dir() / f"{record.launch_id}.json").unlink()
    discovery._cleanup_stale_sessions()
    assert len(stored()) == 1


def test_a_failed_write_never_fails_the_wrapper(monkeypatch, capsys):
    record = claimed()
    in_pane(monkeypatch, record)
    monkeypatch.setattr(discovery, "write_session_record", lambda data: False)
    assert call("done") == 0
    assert "could not record state 'done'" in capsys.readouterr().err


def test_an_unwritable_store_never_fails_the_wrapper(monkeypatch, capsys):
    record = claimed()
    in_pane(monkeypatch, record)

    def broken(data):
        raise OSError("read-only")

    monkeypatch.setattr(discovery, "write_session_record", broken)
    assert call("done") == 0
    assert "continuing" in capsys.readouterr().err


def test_the_hook_ignores_the_other_harness(monkeypatch):
    from tongs_agents import hook
    monkeypatch.setattr(sys, "stdin", None)
    assert hook.handle("session-start", "other") is False
    assert stored() == []


def test_the_hook_does_not_import_notify():
    code = (
        "import sys, tongs_agents.hook; "
        "assert 'tongs_agents.commands' not in sys.modules, 'commands imported'; "
        "assert 'textual' not in sys.modules, 'textual imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)


def test_notify_is_a_cli_command():
    from tongs_agents import cli
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["notify", "needs-you", "review", "the", "diff"])
    assert handlers["notify"] is notify.run
    assert (args.state, args.message) == ("needs-you", ["review", "the", "diff"])
    with pytest.raises(SystemExit):
        parser.parse_args(["notify", "finished"])


@pytest.mark.skipif(os.name == "nt", reason="the profile command is a POSIX shell line")
def test_a_non_harness_profile_shows_its_reported_state_in_the_sidebar(tmp_path):
    """Acceptance: the real launcher execs a shell profile that calls notify."""
    notify_cmd = shlex.join([sys.executable, "-m", "tongs_agents.cli", "notify"])
    script = (
        f"{notify_cmd} working; if true; then {notify_cmd} done 'tests passed'; "
        f"else {notify_cmd} failed; fi"
    )
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "config.toml"
    path.write_text(
        "[profiles.builder]\n"
        f"command = {json.dumps(['/bin/sh', '-c', script])}\n"
        "harness = \"other\"\n"
    )
    path.chmod(0o600)
    # The children cannot see conftest's ancestry mock; keep the terminal
    # running pytest out of their capture so it stays the faked Zellij pane.
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(
        "from tongs_agents.terminal_apis import _ancestry\n"
        "def _unreadable(pid):\n"
        "    raise OSError('ancestry is mocked in tests')\n"
        "_ancestry._read_stat = _unreadable\n"
    )
    pending = launches.create_pending(profile("builder"), SESSION, "zellij")
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("ZELLIJ", "TMUX", "TONGS_AGENTS_"))
    }
    env.update(ZELLIJ_ENV)
    env[paths.STATE_DIR_ENV] = os.environ[paths.STATE_DIR_ENV]
    env[paths.CONFIG_DIR_ENV] = str(config)
    env["PYTHONPATH"] = str(site)
    result = subprocess.run(
        [sys.executable, "-m", "tongs_agents.launch", "builder",
         "--launch-id", pending.launch_id],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    running = launches.get(pending.launch_id)
    assert running.state == "running" and running.pane_id == "3"

    pane = PaneInfo(PaneRef("zellij", SESSION, "3", None), "builder", "sh", False, False, None)
    feed = WorkspaceFeed(
        SESSION, FakeMux(panes=[pane]), read_session_records,
        launch_reader=lambda session: launches.list_records(session),
    )
    [row] = feed.snapshot()
    assert row.source == NOTIFY_SOURCE
    assert row.profile_id == "builder"
    assert row.status is SessionStatus.IDLE
    assert row.detail == "tests passed"
    assert status_label(row) == "DONE"
