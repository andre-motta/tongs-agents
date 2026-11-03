"""Textual TUI for Tongs Agents."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect


class FleetMonitorApp(FocusOnSelect, App):
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
    ):
        super().__init__()
        self.refresh_interval = refresh_interval
        self.notify_level = notify_level
        self.compact = compact
        self.feed = feed

    def compose(self) -> ComposeResult:
        yield Header()
        yield AgentsPanel(
            self.feed,
            refresh_interval=self.refresh_interval,
            notify_level=self.notify_level,
            compact=self.compact,
            id=self.agents_panel_id,
        )
        yield Footer()


def main(refresh: int = 2, notify: str = "all", compact: bool = False) -> None:
    app = FleetMonitorApp(refresh_interval=refresh, notify_level=notify, compact=compact)
    app.run()
