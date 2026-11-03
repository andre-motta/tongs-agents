"""Mocked-subprocess tests for the zellij Multiplexer.

Fixtures are hand-written to the CLI shapes recorded by the M1-0 spike
(docs/work/workspace-spike.md) for zellij 0.44.2 and 0.45.1; no upstream
output is vendored.
"""

import json
import os
import subprocess
import sys

import pytest

from tongs_agents import multiplexers
from tongs_agents.multiplexers import (
    SESSION_EXITED,
    SESSION_RUNNING,
    Multiplexer,
    MultiplexerError,
    PaneRef,
    Unsupported,
    ZellijMultiplexer,
    get_multiplexer,
)
from tongs_agents.multiplexers import zellij as mux_zellij
from tongs_agents.multiplexers import zellij_binary

ZELLIJ = "/opt/zellij/bin/zellij"
SOCKET_DIR = "/run/user/1000/zellij-test"


def _pane(pane_id, title="claude", **extra):
    entry = {
        "id": pane_id, "is_plugin": False, "title": title, "is_focused": False,
        "is_fullscreen": False, "is_floating": False, "is_suppressed": False,
        "is_selectable": True, "exited": False, "exit_status": None,
        "is_held": False, "pane_x": 30, "pane_y": 0, "pane_rows": 40,
        "pane_columns": 90, "pane_content_x": 31, "pane_content_y": 1,
        "pane_content_rows": 38, "pane_content_columns": 88,
        "cursor_coordinates_in_pane": [0, 3], "terminal_command": "claude",
        "pane_command": "claude", "pane_cwd": "/work/example",
        "plugin_url": None, "index_in_pane_group": {}, "default_fg": None,
        "default_bg": None, "tab_id": 0, "tab_position": 0, "tab_name": "agents",
    }
    entry.update(extra)
    return entry


PLUGIN_PANE = {
    "id": 0, "is_plugin": True, "title": "tab-bar", "is_focused": False,
    "is_fullscreen": False, "is_floating": False, "is_suppressed": False,
    "is_selectable": False, "exited": False, "exit_status": None,
    "is_held": False, "pane_x": 0, "pane_y": 0, "pane_rows": 1,
    "pane_columns": 120, "pane_content_x": 0, "pane_content_y": 0,
    "pane_content_rows": 1, "pane_content_columns": 120,
    "cursor_coordinates_in_pane": None, "terminal_command": None,
    "plugin_url": "zellij:tab-bar", "index_in_pane_group": {},
    "default_fg": None, "default_bg": None, "tab_id": 0, "tab_position": 0,
    "tab_name": "agents",
}

LIST_PANES = {
    "0.44.2": [
        PLUGIN_PANE,
        _pane(3, is_focused=True),
        _pane(
            4, title="codex", terminal_command="codex", pane_command="codex",
            exited=True, exit_status=2, is_held=True,
            cursor_coordinates_in_pane=None,
        ),
    ],
    "0.45.1": [
        PLUGIN_PANE,
        _pane(3, is_focused=True),
        _pane(
            4, title="codex", terminal_command="codex", pane_command="codex",
            exited=True, exit_status=None, is_held=True,
        ),
    ],
}

LIST_SESSIONS = (
    "agents [Created 2m 5s ago] \n"
    "scratch [Created 40s ago] (EXITED - attach to resurrect)\n"
)


class FakeZellij:
    """Answers zellij argv by subcommand and records each call."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = dict(responses or {})

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        key = self._key(argv)
        response = self.responses.get(key, (0, "", ""))
        if callable(response):
            response = response()
        if isinstance(response, BaseException):
            raise response
        code, stdout, stderr = response
        return subprocess.CompletedProcess(argv, code, stdout, stderr)

    @staticmethod
    def _key(argv):
        if "action" in argv:
            return argv[argv.index("action") + 1]
        return argv[1]


@pytest.fixture
def fake(monkeypatch):
    runner = FakeZellij()
    monkeypatch.setattr(subprocess, "run", runner)
    monkeypatch.setattr(mux_zellij.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("ZELLIJ_SOCKET_DIR", raising=False)
    return runner


def _mux(socket_dir=SOCKET_DIR):
    return ZellijMultiplexer(socket_dir=socket_dir, binary=ZELLIJ)


def test_get_multiplexer_returns_the_zellij_protocol_adapter():
    mux = get_multiplexer()
    assert isinstance(mux, ZellijMultiplexer)
    assert isinstance(mux, Multiplexer)
    assert mux.name == "zellij"
    with pytest.raises(Unsupported):
        get_multiplexer("screen")
    assert issubclass(Unsupported, MultiplexerError)


def test_importing_multiplexers_does_not_import_textual():
    code = (
        "import sys, tongs_agents.multiplexers, tongs_agents.focus; "
        "print(any(name == 'textual' or name.startswith('textual.') "
        "for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    ("output", "version"),
    [
        ("zellij 0.44.2\n", (0, 44, 2)),
        ("zellij 0.45.1\n", (0, 45, 1)),
        ("zellij 0.46.0-rc1\n", (0, 46, 0)),
        ("zellij\n", None),
        ("something else 0.44.2\n", None),
        ("", None),
    ],
)
def test_zellij_version_parses_only_the_version_line(fake, output, version):
    fake.responses["--version"] = (0, output, "")
    assert zellij_binary.zellij_version(ZELLIJ) == version
    argv, kwargs = fake.calls[0]
    assert argv == [ZELLIJ, "--version"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 5
    assert "shell" not in kwargs


def test_zellij_binary_uses_path_only(monkeypatch):
    seen = []
    monkeypatch.setattr(
        zellij_binary.shutil, "which", lambda name: seen.append(name) or None
    )
    assert zellij_binary.zellij_binary() is None
    assert zellij_binary.zellij_version() is None
    assert seen == ["zellij", "zellij"]


@pytest.mark.parametrize(
    ("version", "ok", "reason"),
    [
        ("0.44.0", False, "zellij 0.44.0 is older than the supported floor 0.44.2"),
        ("0.44.1", False, "zellij 0.44.1 is older than the supported floor 0.44.2"),
        ("0.44.2", True, "zellij 0.44.2"),
        ("0.45.1", True, "zellij 0.45.1"),
        ("0.46.0", True, "zellij 0.46.0 (newer than the validated 0.45.1)"),
    ],
)
def test_available_gates_on_the_version_floor(fake, version, ok, reason):
    fake.responses["--version"] = (0, f"zellij {version}\n", "")
    assert _mux().available() == (ok, reason)


def test_available_reports_missing_and_unreadable_binaries(fake, monkeypatch):
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: None)
    assert ZellijMultiplexer().available() == (False, "zellij not found on PATH")
    fake.responses["--version"] = (1, "", "boom")
    assert _mux().available() == (False, "could not read the zellij version")
    fake.responses["--version"] = OSError("gone")
    assert _mux().available() == (False, "could not read the zellij version")


def test_calls_fail_cleanly_without_a_binary(fake, monkeypatch):
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: None)
    monkeypatch.setattr(mux_zellij, "zellij_binary", lambda: None)
    with pytest.raises(MultiplexerError, match="not found"):
        ZellijMultiplexer().list_panes("agents")
    assert fake.calls == []


def test_spawn_argv_has_no_shell_and_separates_the_command(fake):
    fake.responses["new-pane"] = (0, "terminal_7\n", "")
    ref = _mux().spawn(
        "agents",
        ["python3", "-m", "tongs_agents.launch", "--launch-id", "x; rm -rf ~"],
        cwd="/work/example",
        name="claude-work",
    )
    assert ref == PaneRef("zellij", "agents", "7", SOCKET_DIR)
    argv, kwargs = fake.calls[0]
    assert argv == [
        ZELLIJ, "--session", "agents", "action", "new-pane",
        "--name", "claude-work", "--cwd", "/work/example",
        "--", "python3", "-m", "tongs_agents.launch", "--launch-id", "x; rm -rf ~",
    ]
    assert "shell" not in kwargs
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 5
    assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR


def test_spawn_keeps_dash_arguments_after_the_separator(fake):
    fake.responses["new-pane"] = (0, "terminal_12\n", "")
    ref = _mux().spawn("agents", ["--help"])
    assert ref.pane_id == "12"
    argv = fake.calls[0][0]
    assert argv[-2:] == ["--", "--help"]
    assert "--no-focus" not in argv


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "terminal_\n",
        "terminal_7\nterminal_8\n",
        "plugin_3\n",
        "1\nterminal_7\n",
        "terminal_-1\n",
        "\x1b[32magents\x1b[0m\nscratch\n",
    ],
)
def test_spawn_refuses_any_other_output(fake, stdout):
    fake.responses["new-pane"] = (0, stdout, "")
    with pytest.raises(MultiplexerError, match="pane id"):
        _mux().spawn("agents", ["claude"])


def test_spawn_unknown_session_exit_zero_is_refused(fake):
    fake.responses["new-pane"] = (
        0,
        "\x1b[32;1magents\x1b[m [Created 2m ago]\n",
        "Session 'nope' not found. The following sessions are active:\n",
    )
    with pytest.raises(MultiplexerError):
        _mux().spawn("nope", ["claude"])


def test_spawn_failure_exit_is_an_error_and_not_retried(fake):
    fake.responses["new-pane"] = (2, "", "error: unexpected argument")
    with pytest.raises(MultiplexerError, match="new-pane failed"):
        _mux().spawn("agents", ["claude"])
    assert len(fake.calls) == 1
    fake.responses["new-pane"] = subprocess.TimeoutExpired("zellij", 5)
    with pytest.raises(MultiplexerError, match="new-pane failed"):
        _mux().spawn("agents", ["claude"])
    assert len(fake.calls) == 2


def test_spawn_asks_again_after_a_transient_no_active_session(fake, monkeypatch):
    # zellij 0.44.2's socket probe can briefly deny a running session (#107).
    sleeps = []
    monkeypatch.setattr(mux_zellij.time, "sleep", sleeps.append)
    fake.responses["new-pane"] = _sequence(
        (1, "", "There is no active session!\n"),
        (1, "", "There is no active session!\n"),
        (0, "terminal_9\n", ""),
    )
    ref = _mux().spawn("agents", ["claude"], cwd="/work/example", name="claude")
    assert ref == PaneRef("zellij", "agents", "9", SOCKET_DIR)
    assert len(fake.calls) == 3
    assert all(call[0] == fake.calls[0][0] for call in fake.calls)
    assert sleeps == [mux_zellij.READBACK_INTERVAL] * 2


def test_spawn_gives_up_on_no_active_session_after_bounded_attempts(fake):
    fake.responses["new-pane"] = (1, "", "There is no active session!")
    with pytest.raises(MultiplexerError, match="no active session"):
        _mux().spawn("agents", ["claude"])
    assert len(fake.calls) == mux_zellij.READBACK_ATTEMPTS


def test_spawn_does_not_retry_an_unknown_session(fake):
    fake.responses["new-pane"] = (
        0, "agents [Created 2m ago]\n", "Session 'nope' not found."
    )
    with pytest.raises(MultiplexerError, match="pane id"):
        _mux().spawn("nope", ["claude"])
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "session",
    ["", "-x", "--session", "a\0b", "a\nb", "x" * 257, None, 3],
)
def test_bad_session_names_are_refused_before_any_call(fake, session):
    mux = _mux()
    for call in (
        lambda: mux.spawn(session, ["claude"]),
        lambda: mux.list_panes(session),
        lambda: mux.session_exists(session),
        lambda: mux.session_state(session),
        lambda: mux.delete_session(session),
        lambda: mux.focus(PaneRef("zellij", session, "3")),
    ):
        with pytest.raises(MultiplexerError, match="refused"):
            call()
    assert fake.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "-x"},
        {"name": ""},
        {"name": "a\nb"},
        {"cwd": "relative/dir"},
        {"cwd": "-x"},
        {"cwd": "/a\0b"},
    ],
)
def test_bad_pane_names_and_cwds_are_refused(fake, kwargs):
    with pytest.raises(MultiplexerError, match="refused"):
        _mux().spawn("agents", ["claude"], **kwargs)
    assert fake.calls == []


@pytest.mark.parametrize("argv", [[], [""], ["a\0b"], "claude", [1], None])
def test_bad_argv_is_refused(fake, argv):
    with pytest.raises(MultiplexerError, match="argv"):
        _mux().spawn("agents", argv)
    assert fake.calls == []


@pytest.mark.parametrize(
    "pane_id", ["", "-1", "3a", "terminal_3", "1" * 13, "٣", None, 3]
)
def test_bad_pane_ids_are_refused(fake, pane_id):
    with pytest.raises(MultiplexerError, match="refused"):
        _mux().focus(PaneRef("zellij", "agents", pane_id))
    assert fake.calls == []


def test_refs_for_other_multiplexers_and_bad_socket_dirs_are_refused(fake):
    with pytest.raises(MultiplexerError):
        _mux().focus(PaneRef("tmux", "agents", "3"))
    with pytest.raises(MultiplexerError, match="socket dir"):
        _mux().focus(PaneRef("zellij", "agents", "3", "relative"))
    with pytest.raises(MultiplexerError, match="socket dir"):
        ZellijMultiplexer(socket_dir="relative")
    assert fake.calls == []


@pytest.mark.parametrize("version", sorted(LIST_PANES))
def test_list_panes_parses_both_versions(fake, version):
    fake.responses["list-panes"] = (0, json.dumps(LIST_PANES[version]), "")
    panes = _mux().list_panes("agents")
    assert [pane.ref.pane_id for pane in panes] == ["3", "4"]
    first, second = panes
    assert first.ref == PaneRef("zellij", "agents", "3", SOCKET_DIR)
    assert (first.title, first.command, first.exited, first.focused) == (
        "claude", "claude", False, True
    )
    assert (second.title, second.command, second.exited) == ("codex", "codex", True)
    argv, kwargs = fake.calls[0]
    assert argv == [ZELLIJ, "--session", "agents", "action", "list-panes", "--json"]
    assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR


def test_list_panes_accepts_a_pane_without_a_command(fake):
    entry = _pane(5, terminal_command=None, pane_command=None)
    fake.responses["list-panes"] = (0, json.dumps([entry]), "")
    assert _mux().list_panes("agents")[0].command is None


@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "PANE_ID TYPE TITLE\nterminal_3 terminal claude\n",
        "agents [Created 2m ago]\n",
        json.dumps({"panes": []}),
        json.dumps([1, 2]),
        json.dumps([{**_pane(3), "id": "3"}]),
        json.dumps([{**_pane(3), "id": True}]),
        json.dumps([{**_pane(3), "id": -1}]),
        json.dumps([{k: v for k, v in _pane(3).items() if k != "exited"}]),
        json.dumps([{**_pane(3), "exited": "no"}]),
        json.dumps([{**_pane(3), "pane_command": ["claude"]}]),
    ],
)
def test_list_panes_refuses_unknown_shapes(fake, stdout):
    fake.responses["list-panes"] = (0, stdout, "")
    with pytest.raises(MultiplexerError, match="unrecognized"):
        _mux().list_panes("agents")


def test_list_panes_maps_no_active_session_to_an_error(fake):
    fake.responses["list-panes"] = (1, "", "There is no active session!")
    with pytest.raises(MultiplexerError, match="no active session"):
        _mux().list_panes("agents")


def test_list_panes_asks_again_after_a_blank_answer(fake):
    # zellij 0.44.2 now and then exits 0 with no output for a running session.
    fake.responses["list-panes"] = _sequence(
        (0, "", ""), (0, "\n", ""), (0, json.dumps(LIST_PANES["0.44.2"]), "")
    )
    panes = _mux().list_panes("agents")
    assert [pane.ref.pane_id for pane in panes] == ["3", "4"]
    assert len(fake.calls) == 3


def test_list_panes_asks_again_after_a_transient_no_active_session(fake):
    # zellij 0.44.2's socket probe can briefly deny a running session.
    fake.responses["list-panes"] = _sequence(
        (1, "", "There is no active session!\n"),
        (0, json.dumps(LIST_PANES["0.44.2"]), ""),
    )
    assert [pane.ref.pane_id for pane in _mux().list_panes("agents")] == ["3", "4"]
    assert len(fake.calls) == 2


def test_list_panes_gives_up_on_no_active_session_after_bounded_attempts(fake):
    fake.responses["list-panes"] = (1, "", "There is no active session!")
    with pytest.raises(MultiplexerError, match="no active session"):
        _mux().list_panes("agents")
    assert len(fake.calls) == mux_zellij.READBACK_ATTEMPTS


def test_list_panes_gives_up_on_blank_answers_after_bounded_attempts(fake):
    fake.responses["list-panes"] = (0, "", "")
    with pytest.raises(MultiplexerError, match="unrecognized"):
        _mux().list_panes("agents")
    assert len(fake.calls) == mux_zellij.READBACK_ATTEMPTS


def test_list_panes_does_not_retry_an_unknown_session(fake):
    fake.responses["list-panes"] = (
        0, "agents [Created 2m ago]\n", "Session 'other' not found."
    )
    with pytest.raises(MultiplexerError, match="unrecognized"):
        _mux().list_panes("other")
    assert len(fake.calls) == 1


def test_session_exists_ignores_exited_sessions(fake):
    fake.responses["list-sessions"] = (0, LIST_SESSIONS, "")
    mux = _mux()
    assert mux.session_exists("agents") is True
    assert mux.session_exists("scratch") is False
    assert mux.session_exists("agent") is False
    assert mux.session_exists("missing") is False
    argv, kwargs = fake.calls[0]
    assert argv == [ZELLIJ, "list-sessions", "--no-formatting"]
    assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["timeout"] == 5


def test_session_exists_with_no_sessions(fake):
    fake.responses["list-sessions"] = (1, "", "No active zellij sessions found.\n")
    assert _mux().session_exists("agents") is False


@pytest.mark.parametrize(
    "response",
    [
        (0, "agents\nscratch\n", ""),
        (0, "\x1b[32;1magents\x1b[m", ""),
        (2, "", "error: unexpected argument"),
        subprocess.TimeoutExpired("zellij", 5),
    ],
)
def test_session_exists_refuses_unparseable_output(fake, response):
    fake.responses["list-sessions"] = response
    with pytest.raises(MultiplexerError):
        _mux().session_exists("agents")


def test_session_state_tells_running_exited_and_absent_apart(fake):
    fake.responses["list-sessions"] = (0, LIST_SESSIONS, "")
    mux = _mux()
    assert mux.session_state("agents") == SESSION_RUNNING
    assert mux.session_state("scratch") == SESSION_EXITED
    assert mux.session_state("scratc") is None
    assert mux.session_state("missing") is None
    fake.responses["list-sessions"] = (1, "", "No active zellij sessions found.\n")
    assert mux.session_state("agents") is None


def test_delete_session_deletes_only_the_named_exited_session(fake):
    listings = iter([LIST_SESSIONS, "agents [Created 2m 5s ago] \n"])
    fake.responses["list-sessions"] = lambda: (0, next(listings), "")
    _mux().delete_session("scratch")
    deletes = [argv for argv, _ in fake.calls if argv[1] == "delete-session"]
    assert deletes == [[ZELLIJ, "delete-session", "scratch"]]
    _, kwargs = fake.calls[1]
    assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR
    assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize("session", ["agents", "missing"])
def test_delete_session_refuses_a_running_or_absent_session(fake, session):
    fake.responses["list-sessions"] = (0, LIST_SESSIONS, "")
    with pytest.raises(MultiplexerError, match="not deleting"):
        _mux().delete_session(session)
    assert [argv[1] for argv, _ in fake.calls] == ["list-sessions"]


def test_delete_session_reports_a_failure(fake):
    fake.responses["list-sessions"] = (0, LIST_SESSIONS, "")
    fake.responses["delete-session"] = (1, "", "Session scratch not found\n")
    with pytest.raises(MultiplexerError, match="delete-session scratch exited 1: Session"):
        _mux().delete_session("scratch")


def test_delete_session_checks_that_the_session_is_gone(fake):
    fake.responses["list-sessions"] = (0, LIST_SESSIONS, "")
    with pytest.raises(MultiplexerError, match="still listed"):
        _mux().delete_session("scratch")


def test_socket_dir_defaults_to_the_callers_environment(fake, monkeypatch):
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "/run/user/1000/zellij-env")
    fake.responses["new-pane"] = (0, "terminal_2\n", "")
    ref = ZellijMultiplexer(binary=ZELLIJ).spawn("agents", ["claude"])
    assert ref.socket_dir == "/run/user/1000/zellij-env"
    env = fake.calls[0][1]["env"]
    assert env["ZELLIJ_SOCKET_DIR"] == "/run/user/1000/zellij-env"


def test_without_any_socket_dir_the_default_is_inherited(fake):
    fake.responses["new-pane"] = (0, "terminal_2\n", "")
    ref = ZellijMultiplexer(binary=ZELLIJ).spawn("agents", ["claude"])
    assert ref.socket_dir is None
    assert fake.calls[0][1]["env"] is None


def test_in_session_reads_the_callers_pane(monkeypatch):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "agents")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", SOCKET_DIR)
    assert _mux().in_session() == PaneRef("zellij", "agents", "3", SOCKET_DIR)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "terminal_3")
    assert _mux().in_session() is None
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "-bad")
    assert _mux().in_session() is None
    monkeypatch.delenv("ZELLIJ")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "agents")
    assert _mux().in_session() is None


def _clients(*rows):
    return (0, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n" + "".join(rows), "")


def _sequence(*responses):
    queue = list(responses)
    return lambda: queue.pop(0) if len(queue) > 1 else queue[0]


def test_focus_moves_one_of_two_clients_without_activation(fake):
    fake.responses["list-clients"] = _sequence(
        _clients("1 terminal_1 tongs-agents sidebar\n", "2 terminal_5 bash\n"),
        _clients("1 terminal_3 claude --resume\n", "2 terminal_5 bash\n"),
    )
    result = _mux().focus(PaneRef("zellij", "agents", "3"))
    assert result.selection.succeeded
    assert not result.activation.attempted
    assert result.activation.detail == "skipped: same zellij session"
    assert result.successful and result.target_found
    assert result.backend == "zellij"
    actions = [argv[argv.index("action") + 1:] for argv, _ in fake.calls]
    assert actions == [
        ["list-clients"], ["focus-pane-id", "terminal_3"], ["list-clients"],
    ]
    for argv, kwargs in fake.calls:
        assert argv[:3] == [ZELLIJ, "--session", "agents"]
        assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR


def test_focus_prefers_the_refs_socket_dir(fake):
    fake.responses["list-clients"] = _clients("1 terminal_3 claude\n")
    ref = PaneRef("zellij", "agents", "3", "/run/user/1000/other")
    result = _mux().focus(ref)
    assert result.selection.detail == "pane already focused"
    assert fake.calls[0][1]["env"]["ZELLIJ_SOCKET_DIR"] == "/run/user/1000/other"
    assert len(fake.calls) == 1


def test_focus_fails_when_no_client_moves(fake):
    fake.responses["list-clients"] = _clients("1 terminal_1 sidebar\n")
    result = _mux().focus(PaneRef("zellij", "agents", "3"))
    assert result.state == "failed"
    assert result.selection.attempted and not result.selection.succeeded
    assert "no zellij client moved" in result.selection.detail
    assert not result.activation.attempted


@pytest.mark.parametrize(
    "response",
    [
        (0, "", ""),
        (0, "garbage\n", ""),
        (0, "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n", ""),
        (1, "", "error"),
    ],
)
def test_focus_reports_unparseable_list_clients(fake, response):
    fake.responses["list-clients"] = response
    result = _mux().focus(PaneRef("zellij", "agents", "3"))
    assert result.state == "failed"
    assert not result.activation.attempted
    assert all("focus-pane-id" not in argv for argv, _ in fake.calls)


def test_focus_never_uses_the_terminal_activation_path(fake, monkeypatch):
    from tongs_agents.terminal_apis import zellij as terminal_zellij

    def forbidden(*args, **kwargs):
        raise AssertionError("activation attempted")

    monkeypatch.setattr(terminal_zellij.ZellijAPI, "raise_window", forbidden)
    monkeypatch.setattr(
        terminal_zellij.ZellijAPI, "find_activation_target", forbidden
    )
    fake.responses["list-clients"] = _sequence(
        _clients("1 terminal_1 a\n", "2 terminal_1 b\n"),
        _clients("1 terminal_1 a\n", "2 terminal_3 b\n"),
    )
    assert _mux().focus(PaneRef("zellij", "agents", "3")).selection.succeeded


REF = PaneRef("zellij", "agents", "3", SOCKET_DIR)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return tmp_path / "state" / "run"


def test_paste_names_the_pane_and_passes_text_after_the_separator(fake):
    _mux().paste(REF, "-first line\nsecond line")

    argv, kwargs = fake.calls[-1]
    assert argv == [
        ZELLIJ, "--session", "agents", "action", "paste", "--pane-id", "3", "--",
        "-first line\nsecond line",
    ]
    assert kwargs["env"]["ZELLIJ_SOCKET_DIR"] == SOCKET_DIR
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_paste_is_capped_at_64_kib(fake):
    _mux().paste(REF, "x" * mux_zellij.MAX_PASTE_BYTES)
    with pytest.raises(MultiplexerError, match="limit"):
        _mux().paste(REF, "x" * (mux_zellij.MAX_PASTE_BYTES + 1))
    with pytest.raises(MultiplexerError, match="limit"):
        _mux().paste(REF, "é" * (mux_zellij.MAX_PASTE_BYTES // 2 + 1))
    assert len(fake.calls) == 1


@pytest.mark.parametrize("text", [None, b"bytes", "nul\0inside", "\ud800"])
def test_paste_refuses_text_argv_cannot_carry(fake, text):
    with pytest.raises(MultiplexerError):
        _mux().paste(REF, text)
    assert fake.calls == []


def test_paste_failure_is_an_error(fake):
    fake.responses["paste"] = (1, "", "Session 'agents' not found")
    with pytest.raises(MultiplexerError, match="paste"):
        _mux().paste(REF, "hello")


def test_send_keys_names_the_pane(fake):
    _mux().send_keys(REF, ["Enter"])

    argv, _ = fake.calls[-1]
    assert argv == [
        ZELLIJ, "--session", "agents", "action", "send-keys", "--pane-id", "3", "--",
        "Enter",
    ]


@pytest.mark.parametrize(
    "keys", [[], "Enter", ["-p"], ["Enter\n"], [""], ["x" * 40], [1], ["Enter"] * 17],
)
def test_send_keys_refuses_bad_keys(fake, keys):
    with pytest.raises(MultiplexerError):
        _mux().send_keys(REF, keys)
    assert fake.calls == []


@pytest.mark.parametrize(
    "call",
    [
        lambda mux, ref: mux.paste(ref, "hi"),
        lambda mux, ref: mux.send_keys(ref, ["Enter"]),
        lambda mux, ref: mux.capture(ref),
        lambda mux, ref: mux.subscribe(ref),
    ],
)
@pytest.mark.parametrize(
    "ref",
    [
        PaneRef("tmux", "agents", "3"),
        PaneRef("zellij", "agents", "%3"),
        PaneRef("zellij", "-agents", "3"),
        PaneRef("zellij", "agents", "3", "relative/dir"),
        "zellij:agents:3",
    ],
)
def test_pane_io_refuses_bad_refs_before_any_call(fake, runtime, call, ref):
    with pytest.raises(MultiplexerError):
        call(_mux(), ref)
    assert fake.calls == []


def _dumping(fake, text, *, after=0):
    """A dump-screen response that writes text to the --path file."""
    attempts = []

    def respond():
        argv = fake.calls[-1][0]
        path = argv[argv.index("--path") + 1]
        attempts.append(path)
        if len(attempts) > after and text:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return (0, "", "")

    fake.responses["dump-screen"] = respond
    return attempts


def test_capture_dumps_full_scrollback_into_a_private_temp_file(fake, runtime):
    seen = {}
    rows = "".join(f"line {index}\n" for index in range(300)) + "\n\n   \n"

    def respond():
        argv = fake.calls[-1][0]
        path = argv[argv.index("--path") + 1]
        seen["mode"] = os.stat(path).st_mode & 0o777
        seen["dir_mode"] = os.stat(os.path.dirname(path)).st_mode & 0o777
        seen["path"] = path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rows)
        return (0, "", "")

    fake.responses["dump-screen"] = respond
    text = _mux().capture(REF, lines=3)

    argv, _ = fake.calls[-1]
    assert argv[:9] == [
        ZELLIJ, "--session", "agents", "action", "dump-screen", "--full",
        "--pane-id", "3", "--path",
    ]
    assert text == "line 297\nline 298\nline 299"
    assert seen["mode"] == 0o600 and seen["dir_mode"] == 0o700
    assert os.path.dirname(seen["path"]) == str(runtime / mux_zellij.CAPTURE_DIR)
    assert not os.path.exists(seen["path"])


def test_capture_waits_briefly_for_a_late_dump(fake, runtime, monkeypatch):
    paths_seen = _dumping(fake, "", after=0)
    sleeps = []

    def late_write(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            with open(paths_seen[0], "w", encoding="utf-8") as handle:
                handle.write("prompt> hello\n")

    monkeypatch.setattr(mux_zellij.time, "sleep", late_write)
    assert _mux().capture(REF) == "prompt> hello"
    assert len(sleeps) == 3


def test_capture_rereads_a_partial_dump_until_its_size_is_stable(fake, runtime, monkeypatch):
    paths_seen = _dumping(fake, "line 1\n")
    sleeps = []

    def growing(seconds):
        sleeps.append(seconds)
        if len(sleeps) <= 2:
            with open(paths_seen[0], "a", encoding="utf-8") as handle:
                handle.write(f"line {len(sleeps) + 1}\n")

    monkeypatch.setattr(mux_zellij.time, "sleep", growing)
    assert _mux().capture(REF) == "line 1\nline 2\nline 3"
    assert len(sleeps) == 3


def test_capture_of_an_ever_growing_dump_stops_polling(fake, runtime, monkeypatch):
    paths_seen = _dumping(fake, "x\n")
    sleeps = []

    def growing(seconds):
        sleeps.append(seconds)
        with open(paths_seen[0], "a", encoding="utf-8") as handle:
            handle.write("x\n")

    monkeypatch.setattr(mux_zellij.time, "sleep", growing)
    assert _mux().capture(REF, lines=1) == "x"
    assert len(sleeps) == mux_zellij.MAX_DUMP_POLLS


def test_capture_of_a_blank_pane_is_empty(fake, runtime):
    fake.responses["dump-screen"] = (0, "", "")
    assert _mux().capture(REF) == ""
    assert list((runtime / mux_zellij.CAPTURE_DIR).iterdir()) == []


def test_capture_reads_only_the_tail_of_a_huge_dump(fake, runtime, monkeypatch):
    monkeypatch.setattr(mux_zellij, "MAX_CAPTURE_BYTES", 64)
    _dumping(fake, "a" * 500 + "\nlast line\n")
    assert _mux().capture(REF, lines=1) == "last line"


def test_capture_unlinks_the_file_when_zellij_fails(fake, runtime):
    fake.responses["dump-screen"] = (1, "", "Session 'agents' not found")
    with pytest.raises(MultiplexerError):
        _mux().capture(REF)
    assert list((runtime / mux_zellij.CAPTURE_DIR).iterdir()) == []


@pytest.mark.parametrize("lines", [0, -1, 10_001, 2.5, True, "5"])
def test_capture_refuses_bad_line_counts(fake, runtime, lines):
    with pytest.raises(MultiplexerError):
        _mux().capture(REF, lines=lines)
    assert fake.calls == []


def _script(tmp_path, body):
    path = tmp_path / "fake-zellij"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


SUBSCRIBER = r"""import json, os, sys, time
with open(os.environ["ARGV_LOG"], "w") as handle:
    json.dump(sys.argv[1:], handle)
for index in range(int(os.environ.get("EVENTS", "3"))):
    print(json.dumps({"event": "pane_update", "pane_id": "terminal_3",
                      "viewport": [f"row {index}"]}), flush=True)
    print("", flush=True)
time.sleep(float(os.environ.get("LINGER", "30")))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes and signals")
def test_subscribe_yields_bounded_json_events_and_stops_its_child(tmp_path, monkeypatch):
    log = tmp_path / "argv.json"
    monkeypatch.setenv("ARGV_LOG", str(log))
    started = []
    real_popen = subprocess.Popen

    def popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", popen)
    mux = ZellijMultiplexer(socket_dir=SOCKET_DIR, binary=_script(tmp_path, SUBSCRIBER))

    events = mux.subscribe(REF, max_events=2, timeout=10)
    assert started == []
    received = list(events)

    assert [event["viewport"] for event in received] == [["row 0"], ["row 1"]]
    assert json.loads(log.read_text()) == [
        "--session", "agents", "subscribe", "--pane-id", "3", "--format", "json",
    ]
    assert started[0].poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes and signals")
def test_subscribe_close_terminates_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGV_LOG", str(tmp_path / "argv.json"))
    started = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **k: started.append(real_popen(*a, **k)) or started[-1],
    )
    mux = ZellijMultiplexer(binary=_script(tmp_path, SUBSCRIBER))

    events = mux.subscribe(REF, max_events=100, timeout=10)
    assert next(events)["pane_id"] == "terminal_3"
    events.close()

    assert started[0].poll() is not None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes and signals")
def test_subscribe_ends_at_its_deadline(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGV_LOG", str(tmp_path / "argv.json"))
    monkeypatch.setenv("EVENTS", "1")
    mux = ZellijMultiplexer(binary=_script(tmp_path, SUBSCRIBER))

    received = list(mux.subscribe(REF, max_events=100, timeout=0.5))

    assert len(received) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX pipes and signals")
def test_subscribe_refuses_unrecognized_output(tmp_path):
    mux = ZellijMultiplexer(binary=_script(tmp_path, 'print("not json", flush=True)\n'))
    with pytest.raises(MultiplexerError, match="subscribe"):
        list(mux.subscribe(REF, timeout=5))


@pytest.mark.parametrize(
    "options", [{"max_events": 0}, {"max_events": 1.5}, {"timeout": 0},
                {"timeout": True}, {"timeout": 7200}],
)
def test_subscribe_refuses_unbounded_options(options):
    with pytest.raises(MultiplexerError):
        ZellijMultiplexer(binary=ZELLIJ).subscribe(REF, **options)


def test_package_exports_the_interface():
    assert set(multiplexers.__all__) >= {
        "Multiplexer", "MultiplexerError", "PaneInfo", "PaneRef", "Unsupported",
        "get_multiplexer",
    }
    assert zellij_binary.ZELLIJ_FLOOR == (0, 44, 2)
    assert zellij_binary.ZELLIJ_CEILING == (0, 45, 1)
