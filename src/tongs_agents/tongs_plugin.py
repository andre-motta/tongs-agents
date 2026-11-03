"""tongs plugin for Tongs Agents.

Two palette commands, and no tongs core changes:

- "Agents" pushes the agents panel screen.
- "Open agent workspace" pushes a small modal that does its work in a thread
  worker. Inside Zellij (``ZELLIJ`` is set) it focuses this session's sidebar
  pane, or adds one through ``workspace_spawn`` when none is listed, then
  closes. Outside Zellij it shows the exact command printed by
  ``tongs-agents workspace --print-command`` with a copy action; it never
  opens a terminal window, execs or exits.

The module imports without tongs; the plugin class is defined only when it is
installed.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static
from textual.worker import Worker, WorkerState

from tongs_agents import workspace_spawn
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import Multiplexer, MultiplexerError, get_multiplexer
from tongs_agents.views.fleet_screen import FleetScreen
from tongs_agents.widgets.agents_panel import NOTIFY_LEVELS
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

try:
    from tongs.plugins.base import TongsPlugin
except ImportError:
    TongsPlugin = None

try:
    from tongs_agents._version import __version__
except ImportError:
    __version__ = "0.0.0"

# A command can run before on_app_ready hands over the context; the running
# Textual app is then the only way to tell the user. The lookup is private API,
# so its absence only drops the notification.
try:
    from textual._context import active_app
except ImportError:
    active_app = None

log = logging.getLogger(__name__)

PLUGIN_NAME = "agents"
COMMAND_LABEL = "Agents"
DEFAULT_REFRESH_INTERVAL = 2
MIN_REFRESH_INTERVAL = 0.5
MAX_REFRESH_INTERVAL = 3600
NOT_READY_MESSAGE = "Agents is not ready yet; try again in a moment."
WORKSPACE_LABEL = "Open agent workspace"
WORKSPACE_HELP = "Focus or add the agent sidebar in Zellij, or show the workspace command"
PRINT_COMMAND_TIMEOUT = 60


def plugin_settings(config: object) -> tuple[float, str, list[str]]:
    """Validate the [plugins.agents] table.

    Returns (refresh_interval, notify_level, problems); invalid values fall
    back to the defaults and are reported, never raised.
    """
    if not isinstance(config, dict):
        config = {}
    problems = []
    refresh = config.get("refresh_interval", DEFAULT_REFRESH_INTERVAL)
    if (
        isinstance(refresh, bool)
        or not isinstance(refresh, (int, float))
        or not math.isfinite(refresh)
        or not MIN_REFRESH_INTERVAL <= refresh <= MAX_REFRESH_INTERVAL
    ):
        problems.append(
            f"refresh_interval must be a number from {MIN_REFRESH_INTERVAL}"
            f" to {MAX_REFRESH_INTERVAL}"
        )
        refresh = DEFAULT_REFRESH_INTERVAL
    notify = config.get("notify_level", "all")
    if notify not in NOTIFY_LEVELS:
        problems.append(f"notify_level must be one of {', '.join(NOTIFY_LEVELS)}")
        notify = "all"
    return refresh, notify, problems


def _notify_not_ready() -> None:
    log.warning("%s command ran before the app was ready", PLUGIN_NAME)
    if active_app is None:
        return
    try:
        app = active_app.get(None)
        if app is not None:
            app.notify(NOT_READY_MESSAGE, severity="warning")
    except Exception:
        log.warning("Could not show the not-ready notice", exc_info=True)


@dataclass(frozen=True)
class WorkspaceOutcome:
    """What the workspace action did; ``command`` is set only outside Zellij."""

    ok: bool
    message: str
    command: str | None = None


def in_zellij() -> bool:
    return os.environ.get("ZELLIJ") is not None


def print_command_argv() -> list[str]:
    return [sys.executable, "-m", "tongs_agents.cli", "workspace", "--print-command"]


def _last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def workspace_print_command() -> WorkspaceOutcome:
    """Run ``tongs-agents workspace --print-command`` and return its command."""
    try:
        result = subprocess.run(
            print_command_argv(),
            capture_output=True,
            text=True,
            timeout=PRINT_COMMAND_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return WorkspaceOutcome(False, f"Could not run tongs-agents workspace: {error}")
    command = (result.stdout or "").strip()
    if result.returncode != 0 or not command:
        detail = _last_line(result.stderr) or f"it exited with status {result.returncode}"
        return WorkspaceOutcome(False, f"tongs-agents workspace failed: {detail}")
    return WorkspaceOutcome(True, "Run this command in a terminal:", command)


def open_workspace_in_zellij(mux: Multiplexer | None = None) -> WorkspaceOutcome:
    """Focus the current session's sidebar pane, or add one when none is listed."""
    try:
        mux = get_multiplexer("zellij") if mux is None else mux
        available, detail = mux.available()
        if not available:
            return WorkspaceOutcome(False, f"{detail}; run 'tongs-agents doctor' for details")
        here = mux.in_session()
        if here is None:
            return WorkspaceOutcome(
                False, "ZELLIJ is set but the current Zellij session cannot be identified"
            )
        sidebar = next(
            (
                pane for pane in mux.list_panes(here.session)
                if pane.title == SIDEBAR_PANE_NAME and not pane.exited
            ),
            None,
        )
        if sidebar is not None:
            result = mux.focus(sidebar.ref)
            if result.successful:
                return WorkspaceOutcome(True, "Focused the agent sidebar")
            return WorkspaceOutcome(False, f"Could not focus the agent sidebar: {result.reason}")
        workspace_spawn.ensure_sidebar(mux, here.session)
    except (MultiplexerError, LaunchError, OSError) as error:
        return WorkspaceOutcome(False, getattr(error, "detail", None) or str(error))
    return WorkspaceOutcome(True, f"Added the agent sidebar to Zellij session {here.session}")


class WorkspaceScreen(ModalScreen[None]):
    """Opens the agent workspace in a worker; outside Zellij, shows its command."""

    DEFAULT_CSS = """
    WorkspaceScreen {
        align: center middle;
    }
    WorkspaceScreen > #workspace-dialog {
        width: 80%;
        max-width: 120;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    WorkspaceScreen #workspace-command {
        margin: 1 0;
        padding: 0 1;
        background: $boost;
        display: none;
    }
    WorkspaceScreen #workspace-status.error {
        color: $error;
    }
    WorkspaceScreen #workspace-buttons {
        height: auto;
        align-horizontal: right;
    }
    WorkspaceScreen Button {
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("c", "copy", "Copy", show=True),
    ]

    def __init__(
        self,
        inside: bool,
        *,
        opener: Callable[[], WorkspaceOutcome] = open_workspace_in_zellij,
        printer: Callable[[], WorkspaceOutcome] = workspace_print_command,
    ) -> None:
        super().__init__()
        self.inside = inside
        self._workspace_action = opener if inside else printer
        self.command: str | None = None

    def compose(self) -> ComposeResult:
        status = (
            "Opening the agent workspace in this Zellij session..."
            if self.inside else "Preparing the workspace command..."
        )
        with Vertical(id="workspace-dialog"):
            yield Label(WORKSPACE_LABEL, id="workspace-title")
            yield Static(status, id="workspace-status", markup=False)
            yield Static("", id="workspace-command", markup=False)
            with Horizontal(id="workspace-buttons"):
                yield Button("Copy", id="workspace-copy", disabled=True, variant="primary")
                yield Button("Close", id="workspace-close")

    def on_mount(self) -> None:
        self.run_worker(self._workspace_action, thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.SUCCESS:
            self.show_outcome(event.worker.result)
        elif event.state == WorkerState.ERROR:
            log.warning("Agent workspace action failed", exc_info=event.worker.error)
            self.show_outcome(WorkspaceOutcome(False, f"Unexpected error: {event.worker.error}"))

    def show_outcome(self, outcome: WorkspaceOutcome) -> None:
        status = self.query_one("#workspace-status", Static)
        if self.inside and outcome.ok:
            self.app.notify(outcome.message)
            self.dismiss()
            return
        status.update(outcome.message)
        status.set_class(not outcome.ok, "error")
        if outcome.ok and outcome.command:
            self.command = outcome.command
            command = self.query_one("#workspace-command", Static)
            command.update(outcome.command)
            command.display = True
            self.query_one("#workspace-copy", Button).disabled = False

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workspace-copy":
            self.action_copy()
        elif event.button.id == "workspace-close":
            self.action_close()

    def action_copy(self) -> None:
        if self.command is None:
            return
        self.app.copy_to_clipboard(self.command)
        self.app.notify("Copied the workspace command")

    def action_close(self) -> None:
        self.dismiss()


def _make_plugin_class():
    if TongsPlugin is None:
        return None

    class FleetMonitorPlugin(TongsPlugin):

        def __init__(self) -> None:
            super().__init__()
            self._ctx = None
            self.refresh_interval = DEFAULT_REFRESH_INTERVAL
            self.notify_level = "all"

        @property
        def name(self) -> str:
            return PLUGIN_NAME

        @property
        def version(self) -> str:
            return __version__ or "0.0.0"

        def get_commands(self) -> list[tuple[str, str, object]]:
            return [
                (
                    COMMAND_LABEL,
                    "View Claude Code, Codex and Pi agent sessions",
                    self._open_fleet,
                ),
                (WORKSPACE_LABEL, WORKSPACE_HELP, self._open_workspace),
            ]

        def get_screens(self) -> dict[str, type]:
            """Kept for the plugin ABC; tongs never uses it.

            tongs only collects these classes (``get_all_screens``) and never
            installs or pushes them; the palette commands push their screens.
            """
            return {"fleet": FleetScreen}

        def _open_workspace(self) -> None:
            if self._ctx is None:
                _notify_not_ready()
                return
            self._ctx.push_screen(WorkspaceScreen(in_zellij()))

        def _open_fleet(self) -> None:
            if self._ctx is None:
                _notify_not_ready()
                return
            self._ctx.push_screen(
                FleetScreen(
                    refresh_interval=self.refresh_interval,
                    notify_level=self.notify_level,
                )
            )

        async def on_app_ready(self, ctx) -> None:
            self._ctx = ctx
            try:
                config = ctx.plugin_config(PLUGIN_NAME)
            except Exception:
                log.warning("Could not read [plugins.%s]", PLUGIN_NAME, exc_info=True)
                config = {}
            self.refresh_interval, self.notify_level, problems = plugin_settings(config)
            for problem in problems:
                ctx.notify(
                    f"[plugins.{PLUGIN_NAME}] {problem}; using the default.",
                    severity="warning",
                )

    return FleetMonitorPlugin


_PluginClass = _make_plugin_class()

if _PluginClass is not None:
    FleetMonitorPlugin = _PluginClass
