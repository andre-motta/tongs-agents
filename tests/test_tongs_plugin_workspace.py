"""The tongs plugin's "Open agent workspace" action, inside and outside Zellij."""

import asyncio
import os
import shlex
import subprocess
import sys

import pytest
from textual.app import App
from textual.widgets import Button, Static

from tests.test_workspace_spawn import FakeMux
from tongs_agents import paths, tongs_plugin, workspace_spawn
from tongs_agents.models import FocusResult
from tongs_agents.multiplexers import MultiplexerError, PaneInfo, PaneRef
from tongs_agents.tongs_plugin import (
    WORKSPACE_LABEL,
    WorkspaceOutcome,
    WorkspaceScreen,
    open_workspace_in_zellij,
    print_command_argv,
    workspace_print_command,
)
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

HERE = PaneRef("zellij", "work", "3", None)
FAKE_ZELLIJ = """#!{python}
import sys
args = sys.argv[1:]
if args == ["--version"]:
    print("zellij 0.45.1")
    sys.exit(0)
if args[:1] == ["list-sessions"]:
    print("No active zellij sessions found.", file=sys.stderr)
    sys.exit(1)
print("unexpected zellij call: " + " ".join(args), file=sys.stderr)
sys.exit(3)
"""


@pytest.fixture(autouse=True)
def _no_exec(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the plugin must never exec or exit")

    for name in ("execv", "execve", "execvp", "execvpe", "_exit"):
        monkeypatch.setattr(os, name, refuse)


def _sidebar(pane_id="9", exited=False):
    ref = PaneRef("zellij", HERE.session, pane_id, None)
    return PaneInfo(ref, SIDEBAR_PANE_NAME, sys.executable, exited, False)


def _other(pane_id="4"):
    return PaneInfo(PaneRef("zellij", HERE.session, pane_id, None), "alpha", "sleep", False, False)


class TestInsideZellij:
    def test_listed_sidebar_is_focused_and_nothing_spawned(self):
        mux = FakeMux(here=HERE, panes=[_other(), _sidebar()])
        outcome = open_workspace_in_zellij(mux)
        assert outcome.ok and outcome.command is None
        assert mux.focused == [_sidebar().ref]
        assert mux.spawned == []

    def test_missing_sidebar_is_spawned_in_the_current_session(self):
        mux = FakeMux(here=HERE, panes=[_other()])
        outcome = open_workspace_in_zellij(mux)
        assert outcome.ok and HERE.session in outcome.message
        assert mux.focused == []
        assert mux.spawned == [{
            "session": HERE.session,
            "argv": workspace_spawn.sidebar_argv(),
            "name": SIDEBAR_PANE_NAME,
            "focus": False,
            "cwd": None,
        }]

    def test_exited_sidebar_does_not_count(self):
        mux = FakeMux(here=HERE, panes=[_sidebar(exited=True)])
        assert open_workspace_in_zellij(mux).ok
        assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME]
        assert mux.focused == []

    def test_unavailable_zellij_is_reported(self):
        mux = FakeMux(here=HERE, available=(False, "zellij not found on PATH"))
        outcome = open_workspace_in_zellij(mux)
        assert not outcome.ok
        assert "zellij not found on PATH" in outcome.message
        assert ("list_panes", HERE.session) not in mux.calls

    def test_unidentified_session_is_reported(self):
        mux = FakeMux(here=None)
        outcome = open_workspace_in_zellij(mux)
        assert not outcome.ok and "cannot be identified" in outcome.message
        assert mux.spawned == []

    def test_spawn_failure_is_reported(self):
        mux = FakeMux(here=HERE, fail_spawn={SIDEBAR_PANE_NAME})
        outcome = open_workspace_in_zellij(mux)
        assert not outcome.ok
        assert outcome.message == f"cannot spawn {SIDEBAR_PANE_NAME}"

    def test_list_failure_is_reported(self, monkeypatch):
        mux = FakeMux(here=HERE)

        def broken(session):
            raise MultiplexerError("zellij action list-panes failed")

        monkeypatch.setattr(mux, "list_panes", broken)
        outcome = open_workspace_in_zellij(mux)
        assert not outcome.ok and "list-panes failed" in outcome.message

    def test_focus_failure_is_reported(self, monkeypatch):
        mux = FakeMux(here=HERE, panes=[_sidebar()])
        monkeypatch.setattr(
            mux, "focus", lambda ref: FocusResult(state="failed", reason="no attached client")
        )
        outcome = open_workspace_in_zellij(mux)
        assert not outcome.ok and "no attached client" in outcome.message
        assert mux.spawned == []

    def test_default_multiplexer_is_zellij(self, monkeypatch):
        mux = FakeMux(here=HERE, panes=[_sidebar()])
        names = []
        monkeypatch.setattr(
            tongs_plugin, "get_multiplexer", lambda name: names.append(name) or mux
        )
        assert open_workspace_in_zellij().ok
        assert names == ["zellij"]


class TestPrintCommand:
    def test_argv_runs_the_workspace_verb(self):
        assert print_command_argv() == [
            sys.executable, "-m", "tongs_agents.cli", "workspace", "--print-command",
        ]

    def test_stdout_is_the_command(self, monkeypatch):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, "zellij attach agents\n", "")

        monkeypatch.setattr(tongs_plugin.subprocess, "run", run)
        outcome = workspace_print_command()
        assert outcome == WorkspaceOutcome(True, outcome.message, "zellij attach agents")
        (argv, kwargs), = calls
        assert argv == print_command_argv()
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"] > 0

    def test_failure_reports_the_last_stderr_line(self, monkeypatch):
        monkeypatch.setattr(
            tongs_plugin.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(
                argv, 1, "", "tongs-agents workspace: zellij not found on PATH\n",
            ),
        )
        outcome = workspace_print_command()
        assert not outcome.ok and outcome.command is None
        assert outcome.message.endswith("zellij not found on PATH")

    def test_empty_output_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(
            tongs_plugin.subprocess, "run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "\n", ""),
        )
        assert not workspace_print_command().ok

    @pytest.mark.parametrize(
        "error", [OSError("no interpreter"), subprocess.TimeoutExpired("python", 60)]
    )
    def test_run_errors_are_reported(self, monkeypatch, error):
        def run(argv, **kwargs):
            raise error

        monkeypatch.setattr(tongs_plugin.subprocess, "run", run)
        outcome = workspace_print_command()
        assert not outcome.ok and "Could not run" in outcome.message


class ScreenApp(App):
    def __init__(self, screen):
        super().__init__()
        self.workspace_screen = screen
        self.copied = []
        self.notes = []

    def on_mount(self):
        self.push_screen(self.workspace_screen)

    def copy_to_clipboard(self, text):
        self.copied.append(text)

    def notify(self, message, *, severity="information", **kwargs):
        self.notes.append((message, severity))


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _text(screen, selector):
    widget = screen.query_one(selector, Static)
    # Textual 4.0 exposes the text as renderable; later releases as content.
    return str(getattr(widget, "content", None) or widget.renderable)


def _fail(*args):
    raise AssertionError("the other branch must not run")


@pytest.mark.asyncio
async def test_outside_modal_shows_and_copies_the_exact_command():
    command = "/opt/zellij --session agents --new-session-with-layout '/run/a b.kdl'"
    screen = WorkspaceScreen(
        False, opener=_fail,
        printer=lambda: WorkspaceOutcome(True, "Run this command in a terminal:", command),
    )
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.screen is screen
        assert _text(screen, "#workspace-command") == command
        assert screen.query_one("#workspace-command").display
        assert not screen.query_one("#workspace-copy", Button).disabled
        await pilot.click("#workspace-copy")
        await pilot.press("c")
        assert app.copied == [command, command]
        await pilot.press("escape")
        await pilot.pause()
        assert app.screen is not screen


@pytest.mark.asyncio
async def test_outside_failure_shows_the_error_and_copies_nothing():
    screen = WorkspaceScreen(
        False, opener=_fail,
        printer=lambda: WorkspaceOutcome(False, "tongs-agents workspace failed: boom"),
    )
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert _text(screen, "#workspace-status") == "tongs-agents workspace failed: boom"
        assert screen.query_one("#workspace-status").has_class("error")
        assert screen.query_one("#workspace-copy", Button).disabled
        await pilot.press("c")
        assert app.copied == []


@pytest.mark.asyncio
async def test_inside_success_notifies_and_closes():
    screen = WorkspaceScreen(
        True, printer=_fail, opener=lambda: WorkspaceOutcome(True, "Focused the agent sidebar"),
    )
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.screen is not screen
        assert app.notes == [("Focused the agent sidebar", "information")]
        assert app.copied == []


@pytest.mark.asyncio
async def test_inside_failure_stays_open_with_the_error():
    screen = WorkspaceScreen(
        True, printer=_fail, opener=lambda: WorkspaceOutcome(False, "no attached client"),
    )
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.screen is screen
        assert _text(screen, "#workspace-status") == "no attached client"
        assert screen.query_one("#workspace-copy", Button).disabled
        await pilot.click("#workspace-close")
        await pilot.pause()
        assert app.screen is not screen


@pytest.mark.asyncio
async def test_unexpected_worker_error_is_shown():
    def broken():
        raise RuntimeError("kaboom")

    screen = WorkspaceScreen(True, printer=_fail, opener=broken)
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.screen is screen
        assert "kaboom" in _text(screen, "#workspace-status")


@pytest.fixture
def fake_zellij_path(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("the workspace verb is unsupported on Windows")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    zellij = bin_dir / "zellij"
    zellij.write_text(FAKE_ZELLIJ.format(python=sys.executable))
    zellij.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(home))
    for key in ("XDG_RUNTIME_DIR", "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID",
                "ZELLIJ_SOCKET_DIR"):
        monkeypatch.delenv(key, raising=False)
    return str(zellij)


@pytest.mark.asyncio
async def test_modal_text_equals_the_print_command_output(fake_zellij_path):
    screen = WorkspaceScreen(False, opener=_fail)
    app = ScreenApp(screen)
    async with app.run_test() as pilot:
        await _settle(app, pilot)
        shown = _text(screen, "#workspace-command")
    layouts = list((paths.runtime_dir() / "layouts").iterdir())
    assert len(layouts) == 1
    assert shown == shlex.join([
        fake_zellij_path, "--session", "agents", "--new-session-with-layout", str(layouts[0]),
    ])
    assert screen.command == shown
    token = layouts[0].stem
    assert (paths.runtime_dir() / "requests" / f"{token}.json").is_file()


def test_print_command_output_is_used_verbatim(fake_zellij_path, monkeypatch):
    outputs = []
    real_run = subprocess.run

    def recording_run(argv, **kwargs):
        result = real_run(argv, **kwargs)
        outputs.append(result.stdout)
        return result

    monkeypatch.setattr(tongs_plugin.subprocess, "run", recording_run)
    outcome = workspace_print_command()
    assert outcome.ok, outcome.message
    (stdout,) = outputs
    assert stdout == outcome.command + "\n"


class TestPluginCommand:
    @pytest.fixture(autouse=True)
    def _check_tongs(self):
        pytest.importorskip("tongs")

    class Ctx:
        def __init__(self):
            self.pushed = []

        def plugin_config(self, name):
            return {}

        def push_screen(self, screen):
            self.pushed.append(screen)

        def notify(self, message, severity="information"):
            pass

    def _workspace_command(self, plugin):
        (callback,) = [
            callback for label, _, callback in plugin.get_commands() if label == WORKSPACE_LABEL
        ]
        return callback

    @pytest.mark.parametrize(("zellij", "inside"), [("0", True), (None, False)])
    def test_pushes_the_modal_for_the_current_branch(self, monkeypatch, zellij, inside):
        if zellij is None:
            monkeypatch.delenv("ZELLIJ", raising=False)
        else:
            monkeypatch.setenv("ZELLIJ", zellij)
        plugin = tongs_plugin.FleetMonitorPlugin()
        ctx = self.Ctx()
        asyncio.run(plugin.on_app_ready(ctx))
        self._workspace_command(plugin)()
        (screen,) = ctx.pushed
        assert isinstance(screen, WorkspaceScreen)
        assert screen.inside is inside

    def test_before_ready_notifies_instead_of_raising(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tongs_plugin, "_notify_not_ready", lambda: calls.append(1))
        plugin = tongs_plugin.FleetMonitorPlugin()
        self._workspace_command(plugin)()
        assert calls == [1]

    def test_get_screens_is_documented_as_unused(self):
        doc = tongs_plugin.FleetMonitorPlugin.get_screens.__doc__
        assert "never" in doc
