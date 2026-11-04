"""The workspace quit dialogs: detach (agents keep running) or kill everything.

The sidebar never quits on its own: ``q`` leaves the whole workspace. Detach
ends the multiplexer client and leaves every pane running, so the command
shown in the dialog attaches again; kill ends the session and every agent in
it, only after ``KillScreen`` is answered with ``y`` or its "Yes, kill"
button. Neither dialog acts on a reflex: the focus starts on the harmless
button, so Enter cancels, and a double-click on Kill cannot land on
"Yes, kill" (see ``KillScreen``).
"""

from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

DETACH = "detach"
KILL = "kill"
TITLE = "Quit tongs-agents?"
KILL_TITLE = "Kill the workspace?"
KILL_GRACE_SECONDS = 0.5

_CSS = """
{name} {{
    align: center middle;
}}
{name} > Vertical {{
    width: 70;
    max-width: 100%;
    height: auto;
    border: thick $accent;
    background: $surface;
    padding: 1 2;
}}
{name} Horizontal {{
    height: auto;
    margin-top: 1;
}}
{name} Button {{
    min-width: 8;
    width: 1fr;
}}
"""


def attach_command(session: str, multiplexer: str) -> str:
    """The command that attaches to ``session`` again after a detach."""
    command = f"tongs-agents workspace --session {session}"
    return command if multiplexer == "zellij" else f"{command} --multiplexer {multiplexer}"


class QuitScreen(ModalScreen[str | None]):
    """Detach, kill or cancel; dismisses with DETACH, KILL or None."""

    DEFAULT_CSS = _CSS.format(name="QuitScreen")
    AUTO_FOCUS = "#cancel"

    BINDINGS = [
        Binding("d", "answer('detach')", "Detach"),
        Binding("k", "answer('kill')", "Kill"),
        Binding("n", "answer(None)", "Cancel"),
        Binding("escape", "answer(None)", "Cancel"),
        Binding("q", "answer(None)", "Cancel", show=False),
    ]

    def __init__(self, session: str, multiplexer: str) -> None:
        super().__init__()
        self.title_text = TITLE
        self.session = session
        self.multiplexer = multiplexer

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(TITLE, style="bold"))
            yield Static(Text(
                f"d  Detach: leave the workspace and return to your shell; every "
                f"agent in {self.session} keeps running. Come back with: "
                f"{attach_command(self.session, self.multiplexer)}\n"
                f"k  Kill: end session {self.session} and every agent in it "
                "(asks again).\n"
                "n  Cancel."
            ))
            with Horizontal():
                yield Button("Kill", id=KILL, variant="error")
                yield Button("Detach", id=DETACH, variant="primary")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id if event.button.id in (DETACH, KILL) else None)

    def action_answer(self, value: str | None) -> None:
        self.dismiss(value)


class KillScreen(ModalScreen[bool]):
    """The second check before a kill: only ``y`` or "Yes, kill" confirms.

    "No" has the focus, so Enter cancels. "Yes, kill" sits on the right while
    Kill sits on the left of the first dialog, and a click on it in the first
    ``KILL_GRACE_SECONDS`` is ignored, so a double-click on Kill cannot confirm.
    """

    DEFAULT_CSS = _CSS.format(name="KillScreen")
    AUTO_FOCUS = "#no"

    BINDINGS = [
        Binding("y", "answer(True)", "Kill"),
        Binding("n", "answer(False)", "Cancel"),
        Binding("escape", "answer(False)", "Cancel"),
        Binding("q", "answer(False)", "Cancel", show=False),
    ]

    def __init__(self, session: str, grace: float = KILL_GRACE_SECONDS) -> None:
        super().__init__()
        self.title_text = KILL_TITLE
        self.session = session
        self.grace = grace
        self.opened = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(KILL_TITLE, style="bold"))
            yield Static(Text(
                f"End session {self.session} and every agent in it? Work in "
                "progress in those agents is interrupted.\n\n"
                "y  Yes, kill everything\nn  No"
            ))
            with Horizontal():
                yield Button("No", id="no", variant="primary")
                yield Button("Yes, kill", id="yes", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "yes" and time.monotonic() - self.opened < self.grace:
            return
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)
