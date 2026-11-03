"""AgentsPanel: the one session view behind the monitor, sidebar and tongs screen."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from collections.abc import Mapping

from rich.text import Text
from textual import work
from textual.actions import SkipAction
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import DataTable, Input, Static

from tongs_agents.app_config import NOTIFY_LEVELS
from tongs_agents.attention import Attention, attention_state
from tongs_agents.focus import focus_notification, focus_session
from tongs_agents.models import FleetSession, FocusResult, SessionStatus, format_age
from tongs_agents.sessions_feed import (
    SessionFeed,
    attention_rows,
    next_sort_key,
    visible_sessions,
)
from tongs_agents.widgets.detail_panel import DetailPanel
from tongs_agents.widgets.session_table import FULL_MIN_WIDTH, SessionTable

DEFAULT_CHILD_IDS = {
    "summary": "summary-bar",
    "table": "session-table",
    "detail": "detail-panel",
    "search": "search-input",
}


class AgentsPanel(Widget):
    """Poll, filter, search, sort and act on agent sessions.

    The panel posts ``AgentsPanel.Selected`` when a row is chosen; the host
    decides what selection means (``FocusOnSelect`` focuses the session).
    """

    DEFAULT_CSS = """
    AgentsPanel {
        height: 1fr;
        layout: vertical;
    }

    AgentsPanel > .agents-summary {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }

    AgentsPanel > SessionTable {
        height: 1fr;
    }

    AgentsPanel > Input {
        display: none;
    }

    AgentsPanel > Input.visible {
        display: block;
    }
    """

    BINDINGS = [
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

    class Selected(Message):
        """A session row was chosen (Enter or click)."""

        def __init__(self, panel: AgentsPanel, session: FleetSession) -> None:
            super().__init__()
            self.panel = panel
            self.session = session

        @property
        def control(self) -> AgentsPanel:
            return self.panel

    def __init__(
        self,
        feed: SessionFeed | None = None,
        refresh_interval: float = 2,
        notify_level: str = "all",
        compact: bool = False,
        *,
        child_ids: Mapping[str, str] | None = None,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.feed = feed if feed is not None else SessionFeed()
        self.refresh_interval = refresh_interval
        self.notify_level = notify_level if notify_level in NOTIFY_LEVELS else "all"
        self.compact = compact
        self.child_ids = {**DEFAULT_CHILD_IDS, **(child_ids or {})}
        self._all_sessions: list[FleetSession] = []
        self._notified: set[str] = set()
        self._search_query = ""
        self._status_filter = ""
        self._sort_key = "repo"

    def compose(self) -> ComposeResult:
        ids = self.child_ids
        yield Static(id=ids["summary"], classes="agents-summary")
        yield SessionTable(id=ids["table"], compact=self.compact)
        yield DetailPanel(id=ids["detail"])
        yield Input(placeholder="Filter agents...", id=ids["search"])

    @property
    def table(self) -> SessionTable:
        return self.query_one(f"#{self.child_ids['table']}", SessionTable)

    @property
    def detail_panel(self) -> DetailPanel:
        return self.query_one(f"#{self.child_ids['detail']}", DetailPanel)

    @property
    def search_input(self) -> Input:
        return self.query_one(f"#{self.child_ids['search']}", Input)

    @property
    def summary_bar(self) -> Static:
        return self.query_one(f"#{self.child_ids['summary']}", Static)

    @property
    def sessions(self) -> list[FleetSession]:
        return list(self._all_sessions)

    def on_mount(self) -> None:
        self.set_interval(self.refresh_interval, self.poll)
        self.poll()

    @work(thread=True, group="agents-poll")
    def poll(self) -> None:
        sessions = self.feed.snapshot()
        self.app.call_from_thread(self.update_sessions, sessions)

    @property
    def _live(self) -> bool:
        # is_mounted stays True after removal; is_attached tracks the DOM link.
        return self.is_mounted and self.is_attached

    def update_sessions(self, sessions: list[FleetSession]) -> None:
        # A poll can land after the panel was removed and its children are gone.
        if not self._live:
            return
        self._all_sessions = list(sessions)
        self._notify_new(self._all_sessions)
        self._render_view()

    def shown_sessions(self) -> list[FleetSession]:
        return visible_sessions(
            self._all_sessions,
            status=self._status_filter,
            query=self._search_query,
            sort_key=self._sort_key,
        )

    def _render_view(self) -> None:
        if not self._live:
            return
        shown = self.shown_sessions()
        self._update_summary(shown)
        self.table.set_sessions(shown)

    def on_resize(self) -> None:
        if self._live and self._all_sessions:
            self._update_summary(self.shown_sessions())

    def _is_narrow(self) -> bool:
        width = self.size.width
        return self.compact or (0 < width < FULL_MIN_WIDTH)

    def _update_summary(self, shown: list[FleetSession]) -> None:
        sessions = self._all_sessions
        total = len(sessions)
        need_input = sum(1 for s in sessions if s.needs_attention)
        text = Text(no_wrap=True, overflow="ellipsis")
        if self._is_narrow():
            states = [attention_state(s) for s in sessions]
            text.append(f"{total} agents", style="bold")
            text.append(" | ")
            text.append(f"{states.count(Attention.WORKING)} working", style="green")
            if need_input:
                text.append(" | ")
                text.append(f"{need_input} need you", style="bold yellow")
            if self._status_filter:
                text.append(f" [{self._status_filter}]", style="dim cyan")
            if len(shown) != total:
                text.append(f" {len(shown)}/{total}", style="dim")
            self.summary_bar.update(text)
            return

        active = sum(
            1 for s in sessions
            if s.status in (SessionStatus.RUNNING, SessionStatus.STARTED)
        )
        idle = sum(1 for s in sessions if s.status is SessionStatus.IDLE)
        text.append(f"{total} sessions", style="bold")
        text.append(" | ")
        text.append(f"{active} active", style="green")
        text.append(" | ")
        text.append(f"{idle} idle", style="yellow")
        if need_input:
            text.append(" | ")
            text.append(f"{need_input} need input", style="bold yellow")
        text.append("  ")
        text.append(f"[sort:{self._sort_key}]", style="dim")
        if self._status_filter:
            text.append(f" [filter:{self._status_filter}]", style="dim cyan")
        if self.notify_level != "all":
            text.append(f" (notify:{self.notify_level})", style="dim")
        if len(shown) != total:
            text.append(f"  showing {len(shown)}/{total}", style="dim")
        self.summary_bar.update(text)

    def _notify_new(self, sessions: list[FleetSession]) -> None:
        if self.notify_level == "none":
            return
        notify_send = shutil.which("notify-send")
        for s in sessions:
            waiting = s.status is SessionStatus.WAITING
            should_notify = waiting if self.notify_level == "waiting" else s.needs_attention
            if should_notify and s.identity not in self._notified:
                self._notified.add(s.identity)
                if notify_send:
                    subprocess.Popen(
                        [
                            notify_send,
                            "-u", "normal",
                            "-a", "Tongs Agents",
                            "Session needs input",
                            f"{s.repo} idle for {format_age(s.age_seconds)}",
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                if waiting:
                    self.app.bell()
            elif not should_notify and s.identity in self._notified:
                self._notified.discard(s.identity)

    def selected_session(self) -> FleetSession | None:
        sid = self.table.get_selected_session_id()
        if not sid:
            return None
        return self._session_by_id(sid)

    def _session_by_id(self, sid: str) -> FleetSession | None:
        return next((s for s in self._all_sessions if s.identity == sid), None)

    # -- Actions --

    def action_refresh(self) -> None:
        self.poll()
        self.notify("Refreshed", timeout=2)

    def action_cycle_sort(self) -> None:
        self._sort_key = next_sort_key(self._sort_key)
        self._render_view()
        self.notify(f"Sort: {self._sort_key}", timeout=2)

    def action_filter_status(self, status: str) -> None:
        self._status_filter = "" if self._status_filter == status else status
        self._render_view()
        self.notify(f"Filter: {self._status_filter or 'all'}", timeout=2)

    def action_toggle_detail(self) -> None:
        panel = self.detail_panel
        if panel.has_class("visible"):
            panel.remove_class("visible")
        else:
            panel.add_class("visible")
            panel.set_session(self.selected_session())

    def action_next_attention(self) -> None:
        self._jump_attention(forward=True)

    def action_prev_attention(self) -> None:
        self._jump_attention(forward=False)

    def _jump_attention(self, forward: bool) -> None:
        table = self.table
        if table.row_count == 0:
            return
        rows = attention_rows(self.shown_sessions())
        if not rows:
            self.notify("No sessions need attention", timeout=2)
            return
        current = table.cursor_coordinate.row
        if forward:
            target = next((i for i in rows if i > current), rows[0])
        else:
            target = next((i for i in reversed(rows) if i < current), rows[-1])
        table.move_cursor(row=target)

    def action_open_cwd(self) -> None:
        session = self.selected_session()
        if not session or not session.cwd:
            return
        if sys.platform == "darwin":
            cmd = ["open", session.cwd]
        elif sys.platform == "win32":
            cmd = ["explorer", session.cwd]
        else:
            cmd = ["xdg-open", session.cwd]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.notify(f"Opened: {session.cwd}", timeout=2)

    def action_copy_info(self) -> None:
        session = self.selected_session()
        if not session:
            return
        self.app.copy_to_clipboard(session.summary_line())
        self.notify("Copied summary", timeout=2)

    def action_copy_json(self) -> None:
        session = self.selected_session()
        if not session:
            return
        self.app.copy_to_clipboard(session.to_json())
        self.notify("Copied JSON", timeout=2)

    def action_toggle_search(self) -> None:
        search = self.search_input
        if search.has_class("visible"):
            self._close_search(search)
        else:
            search.add_class("visible")
            search.focus()

    def action_clear_search(self) -> None:
        search = self.search_input
        if not search.has_class("visible"):
            raise SkipAction()
        self._close_search(search)

    def _close_search(self, search: Input) -> None:
        search.remove_class("visible")
        search.value = ""
        self._search_query = ""
        self._render_view()
        self.table.focus()

    # -- Events --

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        session = self._session_by_id(str(event.row_key.value))
        if session:
            self.post_message(self.Selected(self, session))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        event.stop()
        panel = self.detail_panel
        if not panel.has_class("visible"):
            return
        if event.row_key is None:
            panel.set_session(None)
            return
        panel.set_session(self._session_by_id(str(event.row_key.value)))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != self.child_ids["search"]:
            return
        event.stop()
        self._search_query = event.value
        self._render_view()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != self.child_ids["search"]:
            return
        event.stop()
        self.table.focus()


class FocusOnSelect:
    """Host mixin: focus the selected session's terminal in a worker thread.

    It also delegates the shared panel actions so host bindings keep working
    when focus is outside the panel.
    """

    agents_panel_id = "agents-panel"

    @property
    def agents_panel(self) -> AgentsPanel:
        return self.query_one(f"#{self.agents_panel_id}", AgentsPanel)

    def on_agents_panel_selected(self, event: AgentsPanel.Selected) -> None:
        event.stop()
        self._do_focus(event.session)

    def _do_focus(self, session: FleetSession) -> None:
        def _run():
            try:
                result = focus_session(session.identity)
            except Exception as error:
                result = FocusResult(state="failed", reason=str(error))
            self.app.call_from_thread(self._notify_focus_result, session.repo, result)

        threading.Thread(target=_run, daemon=True).start()

    def _notify_focus_result(self, repo, result) -> None:
        message, severity = focus_notification(result, repo)
        self.notify(message, severity=severity, timeout=3)

    def _get_selected_session(self) -> FleetSession | None:
        return self.agents_panel.selected_session()

    def action_refresh(self) -> None:
        self.agents_panel.action_refresh()

    def action_cycle_sort(self) -> None:
        self.agents_panel.action_cycle_sort()

    def action_filter_status(self, status: str) -> None:
        self.agents_panel.action_filter_status(status)

    def action_toggle_detail(self) -> None:
        self.agents_panel.action_toggle_detail()

    def action_next_attention(self) -> None:
        self.agents_panel.action_next_attention()

    def action_prev_attention(self) -> None:
        self.agents_panel.action_prev_attention()

    def action_open_cwd(self) -> None:
        self.agents_panel.action_open_cwd()

    def action_copy_info(self) -> None:
        self.agents_panel.action_copy_info()

    def action_copy_json(self) -> None:
        self.agents_panel.action_copy_json()

    def action_toggle_search(self) -> None:
        self.agents_panel.action_toggle_search()

    def action_clear_search(self) -> None:
        self.agents_panel.action_clear_search()
