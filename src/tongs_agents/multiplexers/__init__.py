"""Terminal multiplexer pane lifecycle (beside terminal_apis)."""

from tongs_agents.multiplexers.base import (
    SESSION_EXITED,
    SESSION_RUNNING,
    Multiplexer,
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
)
from tongs_agents.multiplexers.tmux import TmuxMultiplexer
from tongs_agents.multiplexers.zellij import ZellijMultiplexer

__all__ = [
    "SESSION_EXITED",
    "SESSION_RUNNING",
    "Multiplexer",
    "MultiplexerError",
    "PaneInfo",
    "PaneRef",
    "TmuxMultiplexer",
    "Unsupported",
    "ZellijMultiplexer",
    "get_multiplexer",
]


def get_multiplexer(
    name: str = "zellij", *, socket_dir: str | None = None
) -> Multiplexer:
    if name == ZellijMultiplexer.name:
        return ZellijMultiplexer(socket_dir=socket_dir)
    if name == TmuxMultiplexer.name:
        return TmuxMultiplexer(socket_dir=socket_dir)
    raise Unsupported(f"no multiplexer adapter named {name!r:.40}")
