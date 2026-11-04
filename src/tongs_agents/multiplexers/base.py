"""Pane-lifecycle abstraction for terminal multiplexers.

A Multiplexer creates, lists and selects panes inside a multiplexer session,
and pastes into, sends keys to and captures a pane. It sits beside
TerminalAPI, which focuses an existing agent from outside: a Multiplexer never
activates an emulator window. Adapters hold no policy: which panes may be
driven is decided by ``tongs_agents.control`` alone.

A workspace session shows one agent at a time (layout ``single``, #124) or
every agent side by side (``split``). The session itself records which: a
Zellij session has a ``tongs-agents-home`` pane beside the sidebar that
agents open over as pinned floating panes (and its sidebar pane runs with
``--layout single``, so the agent area can be found again after the home
pane was closed), a tmux session has the ``@tongs_agents_layout`` option
set to ``single``.
Adapters read that marker, so every spawner (the workspace and run
commands, the sidebar, the palette, MCP) places panes the same way, and a
session created before the marker existed keeps the split behaviour. A pane
named ``tongs-agents-sidebar`` is never floated or parked.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tongs_agents.models import FocusResult

SESSION_RUNNING = "running"
SESSION_EXITED = "exited"
SIDEBAR_PANE_NAME = "tongs-agents-sidebar"
HOME_PANE_NAME = "tongs-agents-home"
HOME_MODULE = "tongs_agents.workspace_spawn"
HOME_COMMAND = "home"
LAYOUT_OPTION = "--layout"
LAYOUT_SINGLE = "single"
LAYOUT_SPLIT = "split"
LAYOUTS: tuple[str, ...] = (LAYOUT_SINGLE, LAYOUT_SPLIT)


@dataclass(frozen=True)
class PaneRef:
    multiplexer: str
    session: str
    pane_id: str
    socket_dir: str | None = None


@dataclass(frozen=True)
class PaneInfo:
    ref: PaneRef
    title: str
    command: str | None
    exited: bool
    focused: bool
    exit_status: int | None = None


class MultiplexerError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class Unsupported(MultiplexerError):
    pass


@runtime_checkable
class Multiplexer(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def in_session(self) -> PaneRef | None: ...

    def session_state(self, session: str) -> str | None:
        """SESSION_RUNNING, SESSION_EXITED (resurrectable) or None when absent."""
        ...

    def session_exists(self, session: str) -> bool: ...

    def delete_session(self, session: str) -> None:
        """Delete an EXITED session; refuse a running or absent one."""
        ...

    def detach(self, session: str) -> None:
        """Detach the attached clients from ``session``; every pane keeps running."""
        ...

    def kill_session(self, session: str) -> None:
        """End the running ``session`` and every pane in it."""
        ...

    def spawn(
        self,
        session: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        name: str | None = None,
        focus: bool = False,
    ) -> PaneRef: ...

    def list_panes(self, session: str) -> list[PaneInfo]: ...

    def focus(self, ref: PaneRef) -> FocusResult: ...

    def show(self, ref: PaneRef) -> FocusResult:
        """Bring ref's pane into the main area the way the session's layout does.

        Zellij raises it over the other agents (a pane selection); tmux swaps
        a parked pane into the main slot of a ``single`` session and leaves a
        ``split`` session's active pane alone.
        """
        ...

    def align_agents(self, session: str, pane_ids) -> list[str]:
        """Move the given agent panes onto the session's agent area; return those moved.

        Zellij moves pinned floating panes after the sidebar or the terminal
        was resized; every other adapter has nothing to align.
        """
        return []

    def paste(self, ref: PaneRef, text: str) -> None:
        """Paste text into the pane as one bracketed paste; never submits it."""
        ...

    def send_keys(self, ref: PaneRef, keys: list[str]) -> None:
        """Send named keys (for example ``["Enter"]``) to the pane."""
        ...

    def capture(self, ref: PaneRef, lines: int = 200) -> str:
        """The last ``lines`` lines of the pane's screen and scrollback."""
        ...

    def subscribe(
        self, ref: PaneRef, *, max_events: int = 1000, timeout: float = 60.0
    ) -> Iterator[dict]:
        """Bounded render updates of the pane; closing it stops the child.

        A primitive only: it is not a status source.
        """
        ...

    def rename_pane(self, ref: PaneRef, name: str) -> None:
        """Set the name the multiplexer shows for the pane.

        A display name only: the pane's id, its process and its placement stay.
        """
        ...
