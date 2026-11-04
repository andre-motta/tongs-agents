"""Rename an agent from the sidebar: a one-line prompt for its display name (#130).

``R`` on a row opens the prompt prefilled with the agent's current name.
Enter saves the name in the launch record (``launches.set_label``), Escape
cancels, and an empty name restores the default (the profile name). The
name is a display label only: the profile, harness, session id and command
stay as they are. After saving, the multiplexer pane is renamed too, best
effort; when that fails the label is kept and a warning says why.

Only an agent started in this workspace has a launch record to keep the
name in. Any other row (a discovered or hook-only session) is refused with
a short message in the prompt's place, and so is an agent that is still
starting (its launcher has not claimed the record yet). Notifications show
the name as plain text, never as Textual markup.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static

from tongs_agents import launches
from tongs_agents.launches import STILL_STARTING, LaunchError
from tongs_agents.widgets.session_table import session_name

REFUSAL_SECONDS = 4.0
NOT_HERE = "not started in this workspace, so it cannot be renamed"


class RenamePrompt(Widget):
    """The name prompt and its message line; hidden until ``open`` or ``refuse``."""

    DEFAULT_CSS = """
    RenamePrompt {
        height: auto;
        display: none;
        layout: vertical;
    }
    RenamePrompt.visible {
        display: block;
    }
    RenamePrompt > Horizontal {
        height: 1;
    }
    RenamePrompt.refused > Horizontal {
        display: none;
    }
    RenamePrompt .rename-label {
        width: 6;
        padding: 0 0 0 1;
        color: $accent;
    }
    RenamePrompt Input, RenamePrompt Input:focus {
        border: none;
        height: 1;
        padding: 0;
        width: 1fr;
    }
    RenamePrompt > .rename-message {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    RenamePrompt > .rename-message.error {
        color: $error;
    }
    """

    BINDINGS = [Binding("escape", "close", "Cancel", show=False)]

    class Submitted(Message):
        """A checked name for ``launch_id``; ``label`` None restores the default."""

        def __init__(self, launch_id: str, label: str | None) -> None:
            super().__init__()
            self.launch_id = launch_id
            self.label = label

    class Closed(Message):
        """The prompt was closed without saving."""

    def __init__(self, *, name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.launch_id: str | None = None
        self._refusal_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("Name", classes="rename-label")
            yield Input(id="rename-input", select_on_focus=False,
                        max_length=launches.MAX_LABEL_LENGTH)
        yield Static("", classes="rename-message")

    @property
    def input(self) -> Input:
        return self.query_one("#rename-input", Input)

    @property
    def message_text(self) -> str:
        return str(self.query_one(".rename-message", Static).render())

    def _show_message(self, message: str, *, error: bool = False) -> None:
        line = self.query_one(".rename-message", Static)
        line.set_class(error, "error")
        line.update(Text(message))

    def _stop_timer(self) -> None:
        if self._refusal_timer is not None:
            self._refusal_timer.stop()
            self._refusal_timer = None

    def open(self, launch_id: str, current: str, default: str) -> None:
        """Ask for a new name of ``launch_id``, prefilled with ``current``."""
        self._stop_timer()
        self.launch_id = launch_id
        self.remove_class("refused")
        self.add_class("visible")
        field = self.input
        field.placeholder = default
        field.value = current
        field.cursor_position = len(current)
        self._show_message(f"Enter saves, Escape cancels, empty restores {default}")
        field.focus()

    def refuse(self, message: str) -> None:
        """Show ``message`` in the prompt's place for a few seconds; no input."""
        self._stop_timer()
        self.launch_id = None
        self.input.value = ""
        self.add_class("visible", "refused")
        self._show_message(message, error=True)
        self._refusal_timer = self.set_timer(REFUSAL_SECONDS, self._hide)

    def _hide(self) -> None:
        self._stop_timer()
        self.remove_class("visible", "refused")
        self.launch_id = None
        self.input.value = ""
        self._show_message("")

    def action_close(self) -> None:
        self._hide()
        self.post_message(self.Closed())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        launch_id = self.launch_id
        if launch_id is None:
            return
        try:
            label = launches.check_label(event.value)
        except LaunchError as error:
            self._show_message(str(error), error=True)
            return
        self._hide()
        self.post_message(self.Submitted(launch_id, label))


class RenameActions:
    """Host mixin: ``R`` renames the selected agent (the sidebar).

    The host provides ``agents_panel`` (``FocusOnSelect``), ``feed`` (a
    ``WorkspaceFeed``) and ``multiplexer``, and mounts a ``RenamePrompt``
    with id ``rename_prompt_id``.
    """

    rename_prompt_id = "rename-prompt"

    @property
    def rename_prompt(self) -> RenamePrompt:
        return self.query_one(f"#{self.rename_prompt_id}", RenamePrompt)

    def action_rename_agent(self) -> None:
        session = self.agents_panel.selected_session()
        if session is None:
            self.rename_prompt.refuse("No agent selected")
            return
        record = self.feed.launch_record(session.launch_id) if session.launch_id else None
        if record is None:
            self.rename_prompt.refuse(f"{session_name(session)}: {NOT_HERE}")
            return
        if record.state != "running":
            self.rename_prompt.refuse(f"{session_name(session)}: {STILL_STARTING}")
            return
        self.rename_prompt.open(record.launch_id, record.label or record.profile,
                                record.profile)

    def on_rename_prompt_closed(self, event: RenamePrompt.Closed) -> None:
        event.stop()
        self.agents_panel.table.focus()

    def on_rename_prompt_submitted(self, event: RenamePrompt.Submitted) -> None:
        event.stop()
        self.agents_panel.table.focus()
        launch_id, label = event.launch_id, event.label
        self.run_worker(lambda: self._rename(launch_id, label), thread=True,
                        group=f"rename-{launch_id}")

    def _rename(self, launch_id: str, label: str | None) -> None:
        """Save the label, then rename the pane; a pane failure keeps the label."""
        try:
            record = launches.set_label(launch_id, label)
        except (LaunchError, OSError) as error:
            self.call_from_thread(self.notify, str(error), title="Rename failed",
                                  severity="error", timeout=8, markup=False)
            return
        shown = record.label or record.profile
        warning = None
        try:
            ref = self.feed.launch_pane(launch_id)
            if ref is None:
                raise LookupError("its pane is no longer recorded")
            self.multiplexer.rename_pane(ref, shown)
        except Exception as error:  # noqa: BLE001 - best effort, never crash the sidebar
            detail = getattr(error, "detail", None) or str(error) or type(error).__name__
            warning = f"the pane keeps its old name: {detail}"
        self.call_from_thread(self._renamed, shown, warning)

    def _renamed(self, shown: str, warning: str | None) -> None:
        self.notify(f"Renamed to {shown}", title="Rename", timeout=3, markup=False)
        if warning is not None:
            self.notify(warning, title="Pane not renamed", severity="warning", timeout=8,
                        markup=False)
        self.agents_panel.poll()
