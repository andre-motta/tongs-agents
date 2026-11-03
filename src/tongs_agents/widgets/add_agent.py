"""The Add agent dialog of the sidebar: a profile, a folder and optional arguments.

The folder field completes directory names (Right arrow accepts the
suggestion) and defaults to the profile's ``cwd``, else the folder the
sidebar started in. The arguments field is split like a shell line
(``shlex.split``), never run through a shell, and the dialog shows the
resulting command before anything starts. Start checks both through
``workspace_spawn.check_launch`` (the folder must exist; a sandboxed profile
keeps its upload rules) and stays open with the reason when they are refused.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import Button, Input, Select, Static

from tongs_agents import workspace_spawn
from tongs_agents.launch import build_argv
from tongs_agents.launches import LaunchError
from tongs_agents.profiles import Profile

MAX_COMPLETION_ENTRIES = 2000


@dataclass(frozen=True)
class AddAgentRequest:
    """What the maintainer chose: start ``profile`` in ``folder`` with ``args``."""

    profile: str
    folder: str
    args: tuple[str, ...] = ()


def complete_folder(value: str) -> str | None:
    """``value`` extended to the next directory name it can only mean, or None.

    Hidden directories are offered only once the typed name starts with a
    dot. At most ``MAX_COMPLETION_ENTRIES`` entries are read.
    """
    if not value:
        return None
    if value == "~":
        return "~" + os.sep
    directory, prefix = os.path.split(os.path.expanduser(value))
    if not directory:
        return None
    names = []
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_COMPLETION_ENTRIES:
                    break
                name = entry.name
                if not name.startswith(prefix):
                    continue
                if name.startswith(".") and not prefix.startswith("."):
                    continue
                try:
                    if entry.is_dir():
                        names.append(name)
                except OSError:
                    continue
    except OSError:
        return None
    if not names:
        return None
    completion = names[0] + os.sep if len(names) == 1 else os.path.commonprefix(names)
    if len(completion) <= len(prefix):
        return None
    return value + completion[len(prefix):]


class FolderSuggester(Suggester):
    """Directory completion for the folder field; never cached, the disk changes."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        return complete_folder(value)


def split_args(text: str) -> tuple[str, ...]:
    """The arguments field as argv; ValueError for unbalanced quotes."""
    return tuple(shlex.split(text))


class AddAgentScreen(ModalScreen[AddAgentRequest | None]):
    """Pick a profile, a folder and extra arguments; dismissed with the request."""

    DEFAULT_CSS = """
    AddAgentScreen {
        align: center middle;
    }
    AddAgentScreen > Vertical {
        width: 80;
        max-width: 100%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    AddAgentScreen .add-label {
        margin-top: 1;
    }
    AddAgentScreen #add-preview {
        margin-top: 1;
    }
    AddAgentScreen #add-error {
        color: $error;
    }
    AddAgentScreen Horizontal {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, profiles: Mapping[str, Profile], order: Sequence[str],
                 start_folder: str) -> None:
        super().__init__()
        self.profiles = dict(profiles)
        self.order = [name for name in order if name in self.profiles]
        self.start_folder = start_folder
        self._folder_default = self._default_folder(self.order[0])

    def _default_folder(self, name: str) -> str:
        profile = self.profiles.get(name)
        if profile is not None and profile.cwd is not None:
            return str(profile.cwd)
        return self.start_folder

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text("Add agent", style="bold"))
            yield Static("Profile", classes="add-label")
            yield Select(
                [(name, name) for name in self.order], value=self.order[0],
                allow_blank=False, id="add-profile",
            )
            yield Static("Folder", classes="add-label")
            yield Input(value=self._folder_default, suggester=FolderSuggester(),
                        id="add-folder")
            yield Static("Arguments (optional, for example --resume)", classes="add-label")
            yield Input(placeholder="--resume", id="add-args")
            yield Static("", id="add-preview")
            yield Static("", id="add-error")
            with Horizontal():
                yield Button("Start", id="add-start", variant="primary")
                yield Button("Cancel", id="add-cancel")

    def on_mount(self) -> None:
        self._update_preview()
        self.query_one("#add-folder", Input).focus()

    def _profile(self) -> Profile | None:
        return self.profiles.get(self.query_one("#add-profile", Select).value)

    def _set_error(self, message: str) -> None:
        self.query_one("#add-error", Static).update(Text(message))

    def preview_text(self) -> str:
        profile = self._profile()
        if profile is None:
            return ""
        try:
            extra = split_args(self.query_one("#add-args", Input).value)
        except ValueError as error:
            return f"Arguments: {error}"
        folder = self.query_one("#add-folder", Input).value.strip() or "?"
        return f"Runs: {shlex.join(build_argv(profile, extra))}\nIn: {folder}"

    def _update_preview(self) -> None:
        self.query_one("#add-preview", Static).update(Text(self.preview_text()))

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        folder = self.query_one("#add-folder", Input)
        default = self._default_folder(str(event.value))
        if folder.value == self._folder_default:
            folder.value = default
        self._folder_default = default
        self._update_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        event.stop()
        self._set_error("")
        self._update_preview()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_start()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "add-start":
            self.action_start()
        else:
            self.dismiss(None)

    def checked_request(self) -> AddAgentRequest:
        """The request from the fields; LaunchError or ValueError when refused."""
        profile = self._profile()
        if profile is None:
            raise LaunchError("pick a profile")
        text = self.query_one("#add-folder", Input).value.strip()
        if not text:
            raise LaunchError("name the folder the agent starts in")
        folder = os.path.abspath(os.path.expanduser(text))
        try:
            extra = split_args(self.query_one("#add-args", Input).value)
        except ValueError as error:
            raise ValueError(f"arguments: {error}") from None
        folder, extra = workspace_spawn.check_launch(profile, folder, extra)
        return AddAgentRequest(profile.name, folder, extra)

    def action_start(self) -> None:
        try:
            request = self.checked_request()
        except (LaunchError, ValueError) as error:
            self._set_error(str(error))
            return
        self.dismiss(request)

    def action_cancel(self) -> None:
        self.dismiss(None)
