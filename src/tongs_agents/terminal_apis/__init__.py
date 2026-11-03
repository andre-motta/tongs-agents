"""Terminal API auto-detection and dispatch."""

from tongs_agents.terminal_apis import _ancestry
from tongs_agents.terminal_apis.base import TerminalAPI
from tongs_agents.terminal_apis.konsole import KonsoleAPI
from tongs_agents.terminal_apis.tmux import TmuxAPI
from tongs_agents.terminal_apis.zellij import ZellijAPI
from tongs_agents.terminal_apis.ghostty import GhosttyAPI
from tongs_agents.terminal_apis.gnome import GnomeAPI
from tongs_agents.terminal_apis.iterm2 import ITerm2API
from tongs_agents.terminal_apis.macos_terminal import MacOSTerminalAPI
from tongs_agents.terminal_apis.terminator import TerminatorAPI
from tongs_agents.terminal_apis.windows_terminal import WindowsTerminalAPI
from tongs_agents.terminal_apis.kitty import KittyAPI
from tongs_agents.terminal_apis.screen import ScreenAPI
from tongs_agents.terminal_apis.generic import GenericAPI

TERMINALS: list[type[TerminalAPI]] = [
    TmuxAPI,
    ZellijAPI,
    KonsoleAPI,
    GhosttyAPI,
    ITerm2API,
    MacOSTerminalAPI,
    GnomeAPI,
    TerminatorAPI,
    WindowsTerminalAPI,
    KittyAPI,
    ScreenAPI,
    GenericAPI,
]


def detect_terminal() -> TerminalAPI:
    nearest = _ancestry.nearest_terminal()
    if nearest:
        terminal = find_terminal_api(nearest)
        if terminal is not None:
            return terminal
    return detect_terminal_from_env()


def detect_terminal_from_env() -> TerminalAPI:
    for cls in TERMINALS:
        if cls.detect():
            return cls()
    return GenericAPI()


def capture_terminal_info() -> dict:
    terminal = detect_terminal()
    return {"terminal": terminal.name, "terminal_env": terminal.capture_env()}


def get_terminal_api(terminal_type: str) -> TerminalAPI:
    return find_terminal_api(terminal_type) or GenericAPI()


def find_terminal_api(terminal_type: str) -> TerminalAPI | None:
    for cls in TERMINALS:
        if cls.name == terminal_type:
            return cls()
    return None
