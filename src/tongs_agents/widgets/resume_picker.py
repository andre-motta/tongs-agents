"""The sidebar's Resume session dialog: pick a recent session to start in the workspace.

It lists what ``commands.resume.recent_sessions`` returns (newest first):
harness, session id, last status, age and folder. Enter or a click
dismisses it with the chosen session id; Escape with None. The resume itself
(profile match, live process warning, spawn) runs in the host.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from tongs_agents.commands.resume import Resumable, short_path
from tongs_agents.models import format_age


def option_text(session: Resumable) -> Text:
    """One row: harness, short id, status, age and folder."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(f"{session.harness:<7}", style="bold")
    text.append(f" {session.short_id} ")
    text.append(f"{session.status or '?':<8}", style="cyan")
    text.append(f" {format_age(session.age_seconds):>6} ", style="dim")
    text.append(short_path(session.folder) or "?")
    return text


class ResumePickerScreen(ModalScreen[str | None]):
    """Choose one of ``sessions``; dismissed with its session id, or None."""

    DEFAULT_CSS = """
    ResumePickerScreen {
        align: center middle;
    }
    ResumePickerScreen > Vertical {
        width: 90;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ResumePickerScreen OptionList {
        height: auto;
        max-height: 20;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, sessions: Sequence[Resumable], workspace_session: str) -> None:
        super().__init__()
        self.sessions = list(sessions)
        self.workspace_session = workspace_session

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"Resume a session in {self.workspace_session}", classes="resume-title")
            yield OptionList(
                *(Option(option_text(session), id=str(index))
                  for index, session in enumerate(self.sessions)),
                id="resume-sessions",
            )

    def on_mount(self) -> None:
        self.query_one("#resume-sessions", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.sessions[int(event.option.id)].session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
