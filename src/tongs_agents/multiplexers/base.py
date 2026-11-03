"""Pane-lifecycle abstraction for terminal multiplexers.

A Multiplexer creates, lists and selects panes inside a multiplexer session.
It sits beside TerminalAPI, which focuses an existing agent from outside:
a Multiplexer never activates an emulator window.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tongs_agents.models import FocusResult


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

    def session_exists(self, session: str) -> bool: ...

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

    def paste(self, ref: PaneRef, text: str) -> None: ...

    def send_keys(self, ref: PaneRef, keys: list[str]) -> None: ...

    def capture(self, ref: PaneRef, lines: int = 200) -> str: ...

    def subscribe(self, ref: PaneRef) -> Iterator[dict]: ...
