"""The command palette of the monitor and the sidebar (Ctrl+P).

``AgentCommands`` is a Textual command provider that lists what the host app
offers through ``AppShell.palette_commands()``:

- ``New agent: <profile>`` spawns the profile through ``workspace_spawn`` into
  the Zellij session the app runs in (only offered inside one);
- ``Open tongs tab`` starts ``tongs`` in a new pane of that session with
  ``Multiplexer.spawn`` (only offered when ``tongs`` is on PATH and
  ``[workspace].tongs_tab`` is not false);
- ``Next needing attention``, ``Focus session`` and ``Toggle detail`` act on
  the agents panel like their keys;
- ``Reload profiles`` re-reads the profiles from the config file;
- in a host with chat actions (the sidebar, ``widgets.chat_panel.ChatActions``):
  ``Grant chat``, ``Grant chat and wake`` and ``Revoke chat`` for the selected
  launched pane (each confirmed in a dialog), ``Post to chat`` and
  ``Toggle chat``; ``Open chat pane`` starts ``tongs-agents chat`` in a new
  pane of the session.

``AppShell`` is the host mixin: it registers the provider next to Textual's
system commands, applies ``[ui].theme`` and runs the spawns in worker
threads, reporting the outcome as notifications.
"""

from __future__ import annotations

import shutil
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from textual import work
from textual.app import App
from textual.command import DiscoveryHit, Hit, Hits, Provider

from tongs_agents import workspace_spawn
from tongs_agents.app_config import AppConfig
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import Multiplexer, MultiplexerError
from tongs_agents.profiles import Profile, ProfileError, load_profiles
from tongs_agents.widgets.shell import apply_theme

NEW_AGENT_PREFIX = "New agent: "
OPEN_TONGS = "Open tongs tab"
NEXT_ATTENTION = "Next needing attention"
FOCUS_SESSION = "Focus session"
TOGGLE_DETAIL = "Toggle detail"
RELOAD_PROFILES = "Reload profiles"
TONGS_PANE_NAME = "tongs"
CHAT_GRANT = "Grant chat"
CHAT_GRANT_WAKE = "Grant chat and wake"
CHAT_REVOKE = "Revoke chat"
CHAT_POST = "Post to chat"
CHAT_TOGGLE = "Toggle chat"
CHAT_OPEN = "Open chat pane"
CHAT_PANE_NAME = "chat"
CHAT_PANE_ARGV = (sys.executable, "-m", "tongs_agents.commands.chat")


@dataclass(frozen=True)
class PaletteCommand:
    title: str
    help: str
    callback: Callable[[], object]


class AgentCommands(Provider):
    """Palette entries from the host's ``palette_commands()``."""

    def _commands(self) -> list[PaletteCommand]:
        commands = getattr(self.app, "palette_commands", None)
        return list(commands()) if callable(commands) else []

    async def discover(self) -> Hits:
        for command in self._commands():
            yield DiscoveryHit(command.title, command.callback, help=command.help)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for command in self._commands():
            score = matcher.match(command.title)
            if score > 0:
                yield Hit(
                    score, matcher.highlight(command.title), command.callback,
                    help=command.help,
                )


class AppShell:
    """Host mixin for the monitor and the sidebar: palette, theme and spawns.

    The host calls ``init_shell`` from ``__init__`` and ``mount_shell`` from
    ``on_mount``, and provides ``agents_panel`` and ``_do_focus`` (through
    ``FocusOnSelect``).
    """

    COMMANDS = App.COMMANDS | {AgentCommands}

    def init_shell(
        self,
        config: AppConfig | None,
        multiplexer: Multiplexer | None,
        session: str | None,
    ) -> None:
        self.app_config = config if config is not None else AppConfig()
        self.palette_multiplexer = multiplexer
        self.palette_session = session
        self._palette_profiles: dict[str, Profile] | None = None
        # Spawn workers and the UI thread both fill the cache.
        self._palette_profiles_lock = threading.Lock()
        self.palette_profile_error: str | None = None
        self.theme_warning: str | None = None

    def mount_shell(self) -> None:
        self.theme_warning = apply_theme(self, self.app_config.ui.theme)
        if self.theme_warning is not None:
            self.notify(self.theme_warning, title="Theme", severity="warning", timeout=8)

    # -- Profiles --

    def palette_profiles(self) -> dict[str, Profile]:
        with self._palette_profiles_lock:
            if self._palette_profiles is None:
                try:
                    self._palette_profiles = load_profiles()
                    self.palette_profile_error = None
                except ProfileError as error:
                    self._palette_profiles = {}
                    self.palette_profile_error = str(error)
            return self._palette_profiles

    def ordered_profile_names(self) -> list[str]:
        """Profile names, ``[workspace].default_profiles`` first in their order."""
        profiles = self.palette_profiles()
        defaults = [
            name for name in self.app_config.workspace.default_profiles if name in profiles
        ]
        rest = sorted(name for name in profiles if name not in defaults)
        return [*dict.fromkeys(defaults), *rest]

    def palette_reload_profiles(self) -> None:
        with self._palette_profiles_lock:
            self._palette_profiles = None
        profiles = self.palette_profiles()
        if self.palette_profile_error is not None:
            self.notify(self.palette_profile_error, title="Profiles", severity="error",
                        timeout=10)
            return
        count = len(profiles)
        self.notify(f"Loaded {count} profile{'s' if count != 1 else ''}", timeout=3)

    # -- Commands --

    def _in_session(self) -> bool:
        return self.palette_multiplexer is not None and self.palette_session is not None

    def palette_commands(self) -> list[PaletteCommand]:
        commands = []
        if self._in_session():
            session = self.palette_session
            for name in self.ordered_profile_names():
                commands.append(PaletteCommand(
                    f"{NEW_AGENT_PREFIX}{name}",
                    f"Start profile {name} in a new pane of session {session}",
                    partial(self.palette_new_agent, name),
                ))
            if self.app_config.workspace.tongs_tab and shutil.which("tongs"):
                commands.append(PaletteCommand(
                    OPEN_TONGS,
                    f"Start tongs in a new pane of session {session}",
                    self.palette_open_tongs,
                ))
        reload_help = "Read the profiles from the config file again"
        if self.palette_profile_error is not None:
            reload_help = f"Last read failed: {self.palette_profile_error}"
        commands += [
            PaletteCommand(NEXT_ATTENTION, "Select the next agent that needs you",
                           self.action_next_attention),
            PaletteCommand(FOCUS_SESSION, "Focus the selected agent",
                           self.palette_focus_selected),
            PaletteCommand(TOGGLE_DETAIL, "Show or hide the detail panel",
                           self.action_toggle_detail),
            PaletteCommand(RELOAD_PROFILES, reload_help, self.palette_reload_profiles),
        ]
        if getattr(self, "chat_enabled", False):
            commands += self.chat_commands()
        return commands

    def chat_commands(self) -> list[PaletteCommand]:
        """The chat entries of a host with ``ChatActions``."""
        commands = [
            PaletteCommand(CHAT_GRANT, "Let the selected launched pane use chat",
                           partial(self.chat_grant, False)),
            PaletteCommand(CHAT_GRANT_WAKE,
                           "Let the selected pane use chat and be woken on mention",
                           partial(self.chat_grant, True)),
            PaletteCommand(CHAT_REVOKE, "Take chat away from the selected pane",
                           self.chat_revoke),
            PaletteCommand(CHAT_POST, "Post a message as @maintainer",
                           self.chat_post_prompt),
            PaletteCommand(CHAT_TOGGLE, "Show or hide the chat panel",
                           self.action_toggle_chat),
        ]
        if self._in_session():
            commands.append(PaletteCommand(
                CHAT_OPEN, f"Start the chat pane in session {self.palette_session}",
                self.palette_open_chat,
            ))
        return commands

    def palette_focus_selected(self) -> None:
        session = self.agents_panel.selected_session()
        if session is None:
            self.notify("No agent selected", timeout=2)
            return
        self._do_focus(session)

    @work(thread=True, group="palette-spawn")
    def palette_new_agent(self, name: str) -> None:
        profile = self.palette_profiles().get(name)
        if profile is None or not self._in_session():
            self.call_from_thread(self._palette_failed, name, "profile is no longer configured")
            return
        try:
            workspace_spawn.spawn_profiles(
                self.palette_multiplexer, self.palette_session, [profile]
            )
        except (MultiplexerError, LaunchError, OSError) as error:
            detail = getattr(error, "detail", None) or str(error)
            self.call_from_thread(self._palette_failed, name, detail)
            return
        self.call_from_thread(self._palette_spawned, f"Started {name}")

    @work(thread=True, group="palette-spawn")
    def palette_open_tongs(self) -> None:
        tongs = shutil.which("tongs")
        if tongs is None or not self._in_session():
            self.call_from_thread(self._palette_failed, "tongs", "tongs is not on PATH")
            return
        try:
            self.palette_multiplexer.spawn(
                self.palette_session, [tongs], name=TONGS_PANE_NAME, focus=True
            )
        except (MultiplexerError, OSError) as error:
            detail = getattr(error, "detail", None) or str(error)
            self.call_from_thread(self._palette_failed, "tongs", detail)
            return
        self.call_from_thread(self._palette_spawned, "Opened tongs")

    @work(thread=True, group="palette-spawn")
    def palette_open_chat(self) -> None:
        if not self._in_session():
            self.call_from_thread(self._palette_failed, "chat", "not in a workspace session")
            return
        try:
            self.palette_multiplexer.spawn(
                self.palette_session, list(CHAT_PANE_ARGV), name=CHAT_PANE_NAME, focus=True
            )
        except (MultiplexerError, OSError) as error:
            detail = getattr(error, "detail", None) or str(error)
            self.call_from_thread(self._palette_failed, "chat", detail)
            return
        self.call_from_thread(self._palette_spawned, "Opened chat")

    def _palette_spawned(self, message: str) -> None:
        self.notify(message, timeout=3)
        self.agents_panel.poll()

    def _palette_failed(self, name: str, detail: str) -> None:
        self.notify(f"{name}: {detail}", title="Spawn failed", severity="error", timeout=15)
