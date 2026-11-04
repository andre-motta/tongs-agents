"""The workspace quit dialog: detach (agents keep running) or kill everything.

The sidebar never quits on its own: ``q`` leaves the whole workspace. Detach
closes the window and leaves every pane running, so ``tongs-agents`` brings it
all back; kill ends the session and every agent in it, after a second
confirmation in the sidebar.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

DETACH = "detach"
KILL = "kill"
TITLE = "Quit tongs-agents?"


class QuitScreen(ModalScreen[str | None]):
    """Detach, kill or cancel; dismisses with DETACH, KILL or None."""

    DEFAULT_CSS = """
    QuitScreen {
        align: center middle;
    }
    QuitScreen > Vertical {
        width: 70;
        max-width: 100%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    QuitScreen Horizontal {
        height: auto;
        margin-top: 1;
    }
    QuitScreen Button {
        min-width: 8;
        width: 1fr;
    }
    """

    BINDINGS = [
        Binding("d", "answer('detach')", "Detach"),
        Binding("k", "answer('kill')", "Kill"),
        Binding("n", "answer(None)", "Cancel"),
        Binding("escape", "answer(None)", "Cancel"),
        Binding("q", "answer(None)", "Cancel", show=False),
    ]

    def __init__(self, session: str) -> None:
        super().__init__()
        self.title_text = TITLE
        self.session = session

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(TITLE, style="bold"))
            yield Static(Text(
                f"d  Detach: close the window; every agent in {self.session} keeps "
                "running and 'tongs-agents' brings it back.\n"
                f"k  Kill: end session {self.session} and every agent in it.\n"
                "n  Cancel."
            ))
            with Horizontal():
                yield Button("Detach", id=DETACH, variant="primary")
                yield Button("Kill", id=KILL, variant="error")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id if event.button.id in (DETACH, KILL) else None)

    def action_answer(self, value: str | None) -> None:
        self.dismiss(value)
