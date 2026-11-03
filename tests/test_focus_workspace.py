"""In-session focus: a caller inside the record's zellij session selects the pane."""

import json
import subprocess

import pytest

import tongs_agents.focus as focus_module
from tongs_agents.discovery import ProcessInfo, _record_path
from tongs_agents.focus import focus, focus_notification, focus_session
from tongs_agents.models import FocusOperation, FocusResult, canonical_session_id
from tongs_agents.multiplexers import zellij as mux_zellij
from tongs_agents.terminal_apis import zellij as terminal_zellij
from tests.test_multiplexers import LIST_PANES, FakeZellij, _clients, _sequence

ZELLIJ = "/opt/zellij/bin/zellij"
SOCKET_DIR = "/run/user/1000/zellij-test"


def _record(fleet_dir, *, pane_id="3", session="agents", socket_dir=SOCKET_DIR,
            kind="unavailable", session_id="wrapped-claude", status="running"):
    terminal_env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": session}
    if pane_id is not None:
        terminal_env["ZELLIJ_PANE_ID"] = pane_id
    if socket_dir is not None:
        terminal_env["ZELLIJ_SOCKET_DIR"] = socket_dir
    unresolved = kind == "unavailable"
    instance_id = "unresolved" if unresolved else "4242:1"
    canonical_id = canonical_session_id("claude", session_id, instance_id)
    record = {
        "schema_version": 1,
        "canonical_id": canonical_id,
        "session_id": session_id,
        "instance_id": instance_id,
        "harness_id": "claude",
        "agent": "claude",
        "repo": "example",
        "cwd": "/work/example",
        "pid": "" if unresolved else "4242",
        "process_start": "" if unresolved else "1",
        "process_identity": "unresolved" if unresolved else "identified",
        "status": status,
        "ts": 1,
        "started": 1,
        "source": "hook",
        "terminal": "zellij",
        "terminal_env": terminal_env,
        "focus_target": {
            "kind": kind,
            "terminal": "zellij",
            "terminal_env": terminal_env,
        },
    }
    (fleet_dir / _record_path(canonical_id).name).write_text(json.dumps(record))
    return record


@pytest.fixture
def caller(monkeypatch):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "agents")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "1")
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", SOCKET_DIR)


@pytest.fixture
def fake(monkeypatch):
    runner = FakeZellij({
        "list-panes": (0, json.dumps(LIST_PANES["0.45.1"]), ""),
        "list-clients": _sequence(
            _clients("1 terminal_1 tongs-agents sidebar\n", "2 terminal_4 bash\n"),
            _clients("1 terminal_3 claude\n", "2 terminal_4 bash\n"),
        ),
    })
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr(mux_zellij, "zellij_binary", lambda: ZELLIJ)
    monkeypatch.setattr(mux_zellij.time, "sleep", lambda seconds: None)
    return runner


@pytest.fixture
def existing_path(monkeypatch):
    """Record whether focus_session fell through to the process-based path."""
    seen = []

    def resolve(session):
        seen.append(session["session_id"])
        return None

    monkeypatch.setattr(focus_module, "resolve_session_process", resolve)
    return seen


@pytest.fixture
def no_activation(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("emulator activation attempted")

    for method in ("raise_window", "find_activation_target", "focus_result"):
        monkeypatch.setattr(terminal_zellij.ZellijAPI, method, forbidden)
    monkeypatch.setattr(focus_module, "find_terminal_api", forbidden)


@pytest.mark.parametrize("version", sorted(LIST_PANES))
def test_in_session_focus_with_two_clients_never_activates(
    fleet_dir, caller, fake, existing_path, no_activation, version
):
    fake.responses["list-panes"] = (0, json.dumps(LIST_PANES[version]), "")
    _record(fleet_dir)
    result = focus_session("wrapped-claude")
    assert result.selection.succeeded
    assert result.successful and result.target_found
    assert not result.activation.attempted
    assert result.activation.detail == "skipped: same zellij session"
    assert existing_path == []
    actions = [argv[argv.index("action") + 1:] for argv, _ in fake.calls]
    assert actions == [
        ["list-panes", "--json"],
        ["list-clients"],
        ["focus-pane-id", "terminal_3"],
        ["list-clients"],
    ]
    for argv, kwargs in fake.calls:
        assert argv[:3] == [ZELLIJ, "--session", "agents"]
        assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR
    assert result.pid is None
    assert focus_notification(result, "example") == (
        "Focused: example", "information"
    )


def test_in_session_focus_prints_focused_without_pid(
    fleet_dir, caller, fake, existing_path, no_activation, capsys
):
    _record(fleet_dir)
    assert focus("wrapped-claude") is True
    assert capsys.readouterr() == ("Focused session via zellij\n", "")


@pytest.mark.parametrize(
    "result",
    [
        FocusResult(
            state="partial", reason="", backend="tmux", target_found=True,
            selection=FocusOperation(True, True, "ok"),
            activation=FocusOperation.unavailable("skipped: same zellij session"),
        ),
        FocusResult(
            state="partial", reason="", backend="zellij", target_found=True,
            selection=FocusOperation(True, True, "ok"),
            activation=FocusOperation(True, False, "window activation"),
        ),
        FocusResult(
            state="partial", reason="", backend="zellij", target_found=True,
            selection=FocusOperation(True, True, "ok"),
            activation=FocusOperation.unavailable("not attempted"),
        ),
    ],
)
def test_other_partial_results_still_warn(result):
    message, severity = focus_notification(result, "example")
    assert message.startswith("Partially focused: example")
    assert severity == "warning"


def _live_process(monkeypatch, seen):
    process = ProcessInfo(4242, 1, "claude", "/usr/bin/claude", ("claude",), None, "1")

    def resolve(session):
        seen.append(session["session_id"])
        return process

    monkeypatch.setattr(focus_module, "resolve_session_process", resolve)


def test_resolved_record_with_live_process_focuses_with_its_pid(
    fleet_dir, caller, fake, no_activation, monkeypatch, capsys
):
    seen = []
    _live_process(monkeypatch, seen)
    _record(fleet_dir, kind="terminal")
    assert focus("wrapped-claude") is True
    assert capsys.readouterr() == ("Focused session via zellij (PID 4242)\n", "")
    assert seen == ["wrapped-claude"]
    assert any("focus-pane-id" in argv for argv, _ in fake.calls)


def test_resolved_record_whose_process_is_gone_reports_stale_identity(
    fleet_dir, caller, fake, existing_path
):
    _record(fleet_dir, kind="terminal")
    result = focus_session("wrapped-claude")
    assert result.state == "unavailable"
    assert result.reason == "stored process identity is unavailable or stale"
    assert fake.calls == []


def test_ended_resolved_record_skips_the_in_session_branch(
    fleet_dir, caller, fake, monkeypatch
):
    seen = []
    _live_process(monkeypatch, seen)
    monkeypatch.setattr(focus_module, "find_terminal_api", lambda terminal: None)
    _record(fleet_dir, kind="terminal", status="ended")
    result = focus_session("wrapped-claude")
    assert result.reason == "captured terminal backend 'zellij' is unavailable"
    assert seen == ["wrapped-claude"]
    assert fake.calls == []


def test_unresolved_record_relies_on_pane_presence_alone(
    fleet_dir, caller, fake, existing_path, no_activation
):
    _record(fleet_dir, status="ended")
    assert focus_session("wrapped-claude").selection.succeeded
    assert existing_path == []


def test_record_without_captured_socket_dir_uses_the_callers(
    fleet_dir, caller, fake, existing_path, no_activation
):
    _record(fleet_dir, socket_dir=None)
    assert focus_session("wrapped-claude").selection.succeeded
    assert all(
        kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR for _, kwargs in fake.calls
    )


def test_failed_readback_reports_failure_without_falling_back(
    fleet_dir, caller, fake, existing_path, no_activation
):
    fake.responses["list-clients"] = _clients("1 terminal_1 sidebar\n")
    _record(fleet_dir)
    result = focus_session("wrapped-claude")
    assert result.state == "failed"
    assert not result.activation.attempted
    assert existing_path == []


@pytest.mark.parametrize(
    "change",
    [
        {"session": "other"},
        {"socket_dir": "/run/user/1000/other-dir"},
        {"pane_id": None},
        {"pane_id": "terminal_3"},
        {"pane_id": "99"},
        {"kind": "desktop"},
    ],
)
def test_records_outside_the_callers_session_use_the_existing_path(
    fleet_dir, caller, fake, existing_path, change
):
    _record(fleet_dir, **change)
    result = focus_session("wrapped-claude")
    assert existing_path == ["wrapped-claude"]
    assert result.state == "unavailable"
    assert all("focus-pane-id" not in argv for argv, _ in fake.calls)


def test_caller_outside_zellij_uses_the_existing_path(
    fleet_dir, caller, fake, existing_path, monkeypatch
):
    monkeypatch.delenv("ZELLIJ")
    _record(fleet_dir)
    focus_session("wrapped-claude")
    assert existing_path == ["wrapped-claude"]
    assert fake.calls == []


@pytest.mark.parametrize(
    "response",
    [
        (0, "agents [Created 2m ago]\n", "Session 'agents' not found."),
        (1, "", "error"),
    ],
)
def test_unlisted_panes_fall_back_to_the_existing_path(
    fleet_dir, caller, fake, existing_path, response
):
    fake.responses["list-panes"] = response
    _record(fleet_dir)
    focus_session("wrapped-claude")
    assert existing_path == ["wrapped-claude"]
    assert all("focus-pane-id" not in argv for argv, _ in fake.calls)


def test_transient_list_panes_misses_keep_the_in_session_branch(
    fleet_dir, caller, fake, no_activation, monkeypatch
):
    # zellij 0.44.2 now and then answers list-panes with nothing or briefly
    # reports no active session (#72); a resolved record in the caller's
    # session must not fall back to the emulator path because of it.
    seen = []
    _live_process(monkeypatch, seen)
    fake.responses["list-panes"] = _sequence(
        (0, "", ""),
        (1, "", "There is no active session!\n"),
        (0, json.dumps(LIST_PANES["0.44.2"]), ""),
    )
    _record(fleet_dir, kind="terminal")
    result = focus_session("wrapped-claude")
    assert result.selection.succeeded
    assert result.activation.detail == "skipped: same zellij session"
    assert result.pid == 4242
    assert seen == ["wrapped-claude"]


def test_relative_socket_dir_in_record_is_not_trusted(
    fleet_dir, caller, fake, existing_path, monkeypatch
):
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "relative")
    _record(fleet_dir, socket_dir="relative")
    focus_session("wrapped-claude")
    assert existing_path == ["wrapped-claude"]
    assert fake.calls == []
