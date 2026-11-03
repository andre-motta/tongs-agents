"""Fleet monitoring screen for use within tongs."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Header

from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect

FLEET_CHILD_IDS = {
    "summary": "fleet-summary",
    "table": "fleet-table",
    "detail": "fleet-detail",
    "search": "fleet-search",
}


class FleetScreen(FocusOnSelect, Screen):
    """Fleet monitoring screen that can be pushed within a tongs app."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("slash", "toggle_search", "Search", show=True, key_display="/"),
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

    agents_panel_id = "fleet-panel"

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
            child_ids=FLEET_CHILD_IDS,
            id=self.agents_panel_id,
        )
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Agents"

    def action_go_back(self) -> None:
        self.app.pop_screen()
