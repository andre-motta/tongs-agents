"""Textual TUI for Tongs Agents."""

from __future__ import annotations

import argparse
import dataclasses
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding

from tongs_agents.app_config import (
    MAX_REFRESH_INTERVAL,
    MIN_REFRESH_INTERVAL,
    AppConfig,
    AppConfigError,
    UISettings,
    load_app_config,
)
from tongs_agents.multiplexers import Multiplexer, get_multiplexer
from tongs_agents.palette import AppShell
from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect
from tongs_agents.widgets.shell import AppFooter, AppHeader

EXIT_ERROR = 1


class FleetMonitorApp(AppShell, FocusOnSelect, App):
    """Standalone fleet monitoring TUI: a thin host around AgentsPanel."""

    TITLE = "Tongs Agents"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("slash", "toggle_search", "Search", show=True, key_display="/"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("s", "cycle_sort", "Sort", show=True),
        Binding("d", "toggle_detail", "Detail", show=True),
        Binding("n", "next_attention", "Next", show=True),
        Binding("N", "prev_attention", "Prev", show=False),
        Binding("o", "open_cwd", "Open dir", show=True),
        Binding("c", "copy_info", "Copy", show=True),
        Binding("C", "copy_json", "JSON", show=False),
        Binding("1", "filter_status('running')", "Running", show=False),
        Binding("2", "filter_status('idle')", "Idle", show=False),
        Binding("3", "filter_status('waiting')", "Waiting", show=False),
        Binding("4", "filter_status('error')", "Error", show=False),
        Binding("0", "filter_status('')", "All", show=False),
    ]

    agents_panel_id = "agents-panel"

    def __init__(
        self,
        refresh_interval: float = 2,
        notify_level: str = "all",
        *,
        compact: bool = False,
        feed: SessionFeed | None = None,
        config: AppConfig | None = None,
        multiplexer: Multiplexer | None = None,
        session: str | None = None,
    ):
        super().__init__()
        self.refresh_interval = refresh_interval
        self.notify_level = notify_level
        self.compact = compact
        self.feed = feed
        self.init_shell(config, multiplexer, session)

    def compose(self) -> ComposeResult:
        yield AppHeader()
        yield AgentsPanel(
            self.feed,
            refresh_interval=self.refresh_interval,
            notify_level=self.notify_level,
            compact=self.compact,
            id=self.agents_panel_id,
        )
        yield AppFooter()

    def on_mount(self) -> None:
        self.mount_shell()


def ui_settings(
    config: AppConfig,
    *,
    refresh: float | None = None,
    notify: str | None = None,
    compact: bool | None = None,
    theme: str | None = None,
) -> UISettings:
    """``[ui]`` from the config file with each given command-line flag on top."""
    overrides = {
        key: value for key, value in (
            ("refresh_interval", refresh),
            ("notify_level", notify),
            ("compact", compact),
            ("theme", theme),
        ) if value is not None
    }
    return dataclasses.replace(config.ui, **overrides)


def current_session() -> tuple[Multiplexer | None, str | None]:
    """The Zellij multiplexer and session this process runs in, if any."""
    mux = get_multiplexer("zellij")
    here = mux.in_session()
    if here is None:
        return None, None
    return mux, here.session


def run_monitor(args) -> int:
    """``tongs-agents monitor``: [ui] from the config file, flags on top."""
    refresh = getattr(args, "refresh", None)
    if refresh is not None and not MIN_REFRESH_INTERVAL <= refresh <= MAX_REFRESH_INTERVAL:
        print(
            f"tongs-agents monitor: --refresh must be from {MIN_REFRESH_INTERVAL:g} "
            f"to {MAX_REFRESH_INTERVAL:g} seconds",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        config = load_app_config()
    except AppConfigError as error:
        # The monitor predates the config file; a problem in it (even one
        # unrelated to [ui]) must not stop the fleet view from starting.
        print(f"tongs-agents monitor: {error}; using the default settings", file=sys.stderr)
        config = AppConfig()
    ui = ui_settings(
        config,
        refresh=refresh,
        notify=getattr(args, "notify", None),
        compact=getattr(args, "compact", None),
        theme=getattr(args, "theme", None),
    )
    config = dataclasses.replace(config, ui=ui)
    mux, session = current_session()
    FleetMonitorApp(
        refresh_interval=ui.refresh_interval,
        notify_level=ui.notify_level,
        compact=ui.compact,
        config=config,
        multiplexer=mux,
        session=session,
    ).run()
    return 0


def main(refresh: float | None = None, notify: str | None = None,
         compact: bool | None = None) -> int:
    return run_monitor(
        argparse.Namespace(refresh=refresh, notify=notify, compact=compact, theme=None)
    )
