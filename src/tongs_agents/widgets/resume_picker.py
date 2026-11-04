"""The sidebar's Resume session dialog: pick a session to start in the workspace.

It lists what ``commands.resume.all_sessions`` returns (newest first), the
sessions whose agent still runs and the past ones from the harnesses'
history: a running mark, harness, age, title, profile and folder. Typing
filters the list (every word must appear in the title, folder, profile,
harness or session id, ignoring case); Up and Down move the selection.
Enter or a click dismisses it with the chosen session id; Escape with None.
The resume itself (profile match, live process check, spawn) runs in the
host.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from tongs_agents.commands.resume import Resumable, short_path
from tongs_agents.models import format_age

RUNNING_MARK = "●"
UNTITLED = "(untitled)"


def option_text(session: Resumable, profile: str = "") -> Text:
    """One row: running mark, harness, age, title, profile and folder (cut first)."""
    text = Text(no_wrap=True, overflow="ellipsis")
    text.append(RUNNING_MARK if session.running else " ", style="green")
    text.append(f" {session.harness:<6}", style="bold")
    text.append(f" {format_age(session.age_seconds):>6} ", style="dim")
    text.append(session.title or UNTITLED, style="" if session.title else "dim")
    if profile:
        text.append(f"  {profile}", style="magenta")
    text.append(f"  {short_path(session.folder) or '?'}", style="cyan")
    return text


def matches(session: Resumable, profile: str, words: Sequence[str]) -> bool:
    haystack = " ".join((session.title, short_path(session.folder), session.folder, profile,
                         session.harness, session.session_id)).casefold()
    return all(word in haystack for word in words)


class ResumePickerScreen(ModalScreen[str | None]):
    """Choose one of ``sessions``; dismissed with its session id, or None.

    ``profiles`` maps (harness, session id) to the profile column text.
    """

    DEFAULT_CSS = """
    ResumePickerScreen {
        align: center middle;
    }
    ResumePickerScreen > Vertical {
        width: 110;
        max-width: 100%;
        height: auto;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ResumePickerScreen Input {
        margin-top: 1;
    }
    ResumePickerScreen OptionList {
        height: auto;
        max-height: 20;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
    ]

    def __init__(self, sessions: Sequence[Resumable], workspace_session: str,
                 profiles: Mapping[tuple[str, str], str] | None = None) -> None:
        super().__init__()
        self.sessions = list(sessions)
        self.workspace_session = workspace_session
        self.profiles = dict(profiles or {})
        self.shown: list[Resumable] = list(self.sessions)

    def _profile(self, session: Resumable) -> str:
        return self.profiles.get((session.harness, session.session_id), "")

    def _options(self) -> list[Option]:
        return [Option(option_text(session, self._profile(session)), id=str(index))
                for index, session in enumerate(self.shown)]

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Resume a session in {self.workspace_session} "
                f"({RUNNING_MARK} still runs: Enter shows its pane)",
                classes="resume-title",
            )
            yield Input(placeholder="filter: title, folder, profile, harness or id",
                        id="resume-filter")
            yield OptionList(*self._options(), id="resume-sessions")

    @property
    def option_list(self) -> OptionList:
        return self.query_one("#resume-sessions", OptionList)

    def on_mount(self) -> None:
        self.query_one("#resume-filter", Input).focus()
        if self.shown:
            self.option_list.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        words = event.value.casefold().split()
        self.shown = [session for session in self.sessions
                      if matches(session, self._profile(session), words)]
        options = self.option_list
        options.clear_options()
        options.add_options(self._options())
        if self.shown:
            options.highlighted = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        index = self.option_list.highlighted
        if index is not None and 0 <= index < len(self.shown):
            self.dismiss(self.shown[index].session_id)

    def action_move(self, step: int) -> None:
        options = self.option_list
        if step < 0:
            options.action_cursor_up()
        else:
            options.action_cursor_down()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.shown[int(event.option.id)].session_id)

    def action_cancel(self) -> None:
        self.dismiss(None)
