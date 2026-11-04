"""DataTable widget for fleet sessions."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import DataTable

from tongs_agents.attention import attention_state
from tongs_agents.models import FleetSession, SessionStatus, format_age
from tongs_agents.sessions_feed import LAUNCH_SOURCE, NOTIFY_SOURCE

FULL_MIN_WIDTH = 80
COMPACT_FIXED_WIDTHS = {"attention": 1, "icon": 2, "status": 9, "age": 5}
COMPACT_MIN_NAME_WIDTH = 4
SCROLLBAR_RESERVE = 2

STATUS_STYLES = {
    SessionStatus.RUNNING: "green",
    SessionStatus.IDLE: "yellow",
    SessionStatus.WAITING: "yellow",
    SessionStatus.STARTED: "cyan",
    SessionStatus.ERROR: "red",
    SessionStatus.ENDED: "bright_black",
    SessionStatus.DISCOVERED: "magenta",
}

STATUS_ICONS = {
    SessionStatus.RUNNING: "*",
    SessionStatus.IDLE: "=",
    SessionStatus.WAITING: "?",
    SessionStatus.STARTED: ">",
    SessionStatus.ERROR: "x",
    SessionStatus.ENDED: "-",
    SessionStatus.DISCOVERED: "+",
}

COMPACT_STATUS_LABELS = {
    SessionStatus.DISCOVERED: "DETECTED",
}

TERMINAL_ABBREV = {
    "konsole": "KDE",
    "tmux": "tmx",
    "zellij": "zel",
    "ghostty": "gho",
    "iterm2": "iTm",
    "macos_terminal": "mac",
    "gnome": "GTK",
    "terminator": "trm",
    "windows_terminal": "win",
    "kitty": "kty",
    "screen": "scr",
    "generic": "gen",
}


def _age_style(session: FleetSession) -> str:
    if session.status in (SessionStatus.RUNNING, SessionStatus.STARTED):
        return "green dim"
    if session.age_seconds > 600:
        return "red"
    if session.age_seconds > 120:
        return "yellow"
    return "dim"


def session_name(session: FleetSession) -> str:
    """The row's name: the label given in the sidebar, else the profile, else the repo."""
    return (
        str(getattr(session, "label", "") or "")
        or str(getattr(session, "profile_id", "") or "")
        or session.repo
    )


def compact_status_label(status: SessionStatus) -> str:
    return COMPACT_STATUS_LABELS.get(status, status.value.upper())


def status_label(session: FleetSession) -> str:
    """The status column text.

    Hookless launch rows show their pane state; rows reported with
    ``tongs-agents notify`` show the reported state (working, needs-you, ...).
    """
    if session.source == LAUNCH_SOURCE and session.detail:
        return session.detail.upper()[:COMPACT_FIXED_WIDTHS["status"]]
    if session.source == NOTIFY_SOURCE:
        return attention_state(session).value.upper()
    return session.status.value.upper()


def compact_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 100 * 3600:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class SessionTable(DataTable):
    """Fleet session table with status-colored rows.

    The full column set needs ``full_min_width`` cells. Narrower tables, or
    ``compact=True``, use the compact set: attention, icon, a flexible name,
    status and age, with the detail column hidden.
    """

    def __init__(self, *args, compact: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.force_compact = compact
        self._sessions: list[FleetSession] = []
        self._layout: tuple[str, int] | None = None

    @property
    def full_min_width(self) -> int:
        """Cells the full column set needs; narrower tables use the compact set."""
        return FULL_MIN_WIDTH

    @property
    def compact(self) -> bool:
        width = self.size.width
        return self.force_compact or (0 < width < self.full_min_width)

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self._ensure_layout()

    def on_resize(self) -> None:
        selected = self.get_selected_session_id()
        if self._ensure_layout():
            self._fill(self._sessions, selected)

    def _name_width(self) -> int:
        columns = len(COMPACT_FIXED_WIDTHS) + 1
        used = sum(COMPACT_FIXED_WIDTHS.values()) + 2 * self.cell_padding * columns
        return max(COMPACT_MIN_NAME_WIDTH, self.size.width - used - SCROLLBAR_RESERVE)

    def _ensure_layout(self) -> bool:
        layout = ("compact", self._name_width()) if self.compact else ("full", 0)
        if layout == self._layout:
            return False
        self._layout = layout
        self.clear(columns=True)
        if layout[0] == "compact":
            widths = COMPACT_FIXED_WIDTHS
            self.add_column("!", key="attention", width=widths["attention"])
            self.add_column("", key="icon", width=widths["icon"])
            self.add_column("Name", key="name", width=layout[1])
            self.add_column("Status", key="status", width=widths["status"])
            self.add_column("Age", key="age", width=widths["age"])
        else:
            self.add_column("!", key="attention", width=2)
            self.add_column("", key="icon", width=2)
            self.add_column("Agent", key="agent", width=7)
            self.add_column("Repo", key="repo", width=24)
            self.add_column("Status", key="status", width=12)
            self.add_column("Term", key="terminal", width=5)
            self.add_column("Detail", key="detail")
            self.add_column("Age", key="age", width=8)
        return True

    def set_sessions(self, sessions: list[FleetSession]) -> None:
        selected = self.get_selected_session_id()
        self._sessions = list(sessions)
        self._ensure_layout()
        self._fill(self._sessions, selected)

    def _row(self, s: FleetSession) -> tuple[Text, ...]:
        style = STATUS_STYLES.get(s.status, "bright_black")
        icon = Text(STATUS_ICONS.get(s.status, "?"), style=style)
        attention = Text("!", style="bold yellow blink") if s.needs_attention else Text(" ")
        if self._layout and self._layout[0] == "compact":
            return (
                attention,
                icon,
                Text(session_name(s), style="bold", no_wrap=True, overflow="ellipsis"),
                Text(
                    status_label(s) if s.source in (LAUNCH_SOURCE, NOTIFY_SOURCE)
                    else compact_status_label(s.status),
                    style=style,
                ),
                Text(compact_age(s.age_seconds), style=_age_style(s)),
            )
        detail = s.detail.replace("\n", " ").replace("\r", "")
        if len(detail) > 60:
            detail = detail[:57] + "..."
        term = TERMINAL_ABBREV.get(s.terminal, s.terminal[:3] if s.terminal else "")
        return (
            attention,
            icon,
            Text(s.agent[:6], style="dim"),
            Text((str(getattr(s, "label", "") or "") or s.repo)[:23], style="bold"),
            Text(status_label(s), style=style),
            Text(term, style="dim"),
            Text(detail),
            Text(format_age(s.age_seconds), style=_age_style(s)),
        )

    def _fill(self, sessions: list[FleetSession], selected: str | None) -> None:
        self.clear()
        restore_row = None

        for i, s in enumerate(sessions):
            self.add_row(*self._row(s), key=s.identity)
            if selected and s.identity == selected:
                restore_row = i

        if restore_row is not None:
            self.move_cursor(row=restore_row)

    def get_selected_session_id(self) -> str | None:
        if self.row_count == 0:
            return None
        try:
            cell_key = self.coordinate_to_cell_key(self.cursor_coordinate)
            return cell_key.row_key.value
        except Exception:
            return None
