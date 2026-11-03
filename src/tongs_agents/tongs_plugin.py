"""tongs plugin for Tongs Agents."""

from __future__ import annotations

import logging
import math

from tongs_agents.views.fleet_screen import FleetScreen
from tongs_agents.widgets.agents_panel import NOTIFY_LEVELS

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

PLUGIN_NAME = "fleet-monitor"
COMMAND_LABEL = "Agents"
DEFAULT_REFRESH_INTERVAL = 2
MIN_REFRESH_INTERVAL = 0.5
MAX_REFRESH_INTERVAL = 3600
NOT_READY_MESSAGE = "Agents is not ready yet; try again in a moment."


def plugin_settings(config: object) -> tuple[float, str, list[str]]:
    """Validate the [plugins.fleet-monitor] table.

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
    app = active_app.get(None) if active_app is not None else None
    if app is not None:
        app.notify(NOT_READY_MESSAGE, severity="warning")


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
            ]

        def get_screens(self) -> dict[str, type]:
            return {"fleet": FleetScreen}

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
