"""Locate the zellij binary and read its version.

While web watching is enabled (``tongs-agents web enable``) and the managed
web build that ``tongs-agents zellij install --web`` placed behind
``state_dir()/bin/zellij-web`` resolves, that build is used, so a new
workspace session can be watched from a browser. Otherwise a zellij on PATH at
or above the floor wins; failing that the managed no-web binary in
``state_dir()/bin/zellij`` is used when it exists, and failing that the PATH
binary is returned as it is so callers can report its version.

That one choice serves every Zellij CLI call, including calls against a
session that is already running. A session keeps the server version it was
started with, so when the web build and the usual zellij differ in version a
session started on one side of ``web enable`` or ``web disable`` is reached
by a client of the other version; ``tongs-agents web`` tells the user to
restart the workspace then (see docs/web.md).

This module never downloads anything; ``tongs_agents.zellij_download`` does.
It only reads the web state file that ``tongs_agents.commands.web`` writes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from pathlib import Path

from tongs_agents import paths
from tongs_agents.terminal_apis.zellij import _run

ZELLIJ_FLOOR = (0, 44, 2)
ZELLIJ_CEILING = (0, 45, 1)
ZELLIJ_VALIDATED = ZELLIJ_CEILING
MANAGED_BIN_DIR = "bin"
MANAGED_NAME = "zellij"
MANAGED_WEB_NAME = "zellij-web"
WEB_DIR = "web"
WEB_STATE_FILE = "state.json"
WEB_STATE_MAX_BYTES = 4096
WEB_SHARING_OPTIONS = ("options", "--web-sharing", "on")
_VERSION = re.compile(r"zellij (\d{1,4})\.(\d{1,4})\.(\d{1,4})(?![0-9])")
_version_cache: dict[tuple, tuple[int, int, int] | None] = {}


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def managed_bin_dir() -> Path:
    return paths.state_dir() / MANAGED_BIN_DIR


def managed_link() -> Path:
    """``state_dir()/bin/zellij``, the link the installer repoints."""
    return managed_bin_dir() / MANAGED_NAME


def managed_web_link() -> Path:
    """``state_dir()/bin/zellij-web``, the link ``zellij install --web`` repoints."""
    return managed_bin_dir() / MANAGED_WEB_NAME


def _executable(link: Path) -> str | None:
    try:
        info = os.stat(link)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or not os.access(link, os.X_OK):
        return None
    return str(link)


def managed_binary() -> str | None:
    """The managed zellij when its link resolves to an executable regular file."""
    return _executable(managed_link())


def managed_web_binary() -> str | None:
    """The managed web build when its link resolves to an executable regular file."""
    return _executable(managed_web_link())


def web_state_path() -> Path:
    return paths.state_dir() / WEB_DIR / WEB_STATE_FILE


def read_web_state() -> dict:
    """The state ``tongs-agents web`` recorded; empty when absent or unreadable."""
    path = web_state_path()
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > WEB_STATE_MAX_BYTES:
            return {}
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def web_enabled() -> bool:
    return read_web_state().get("enabled") is True


def web_binary() -> str | None:
    """The managed web build while web watching is enabled, else None."""
    if not web_enabled():
        return None
    return managed_web_binary()


def new_session_options(binary: str | None) -> list[str]:
    """Options a new workspace session started with ``binary`` needs.

    Web sharing is turned on for that session only, and only when ``binary``
    is the managed web build in use because web watching is enabled; the
    user's Zellij config is never changed.
    """
    web = web_binary()
    if binary is None or web is None or binary != web:
        return []
    return list(WEB_SHARING_OPTIONS)


def path_binary() -> str | None:
    return shutil.which("zellij")


def _cached_version(binary: str) -> tuple[int, int, int] | None:
    try:
        info = os.stat(binary)
    except OSError:
        return None
    key = (os.path.realpath(binary), info.st_mtime_ns, info.st_size)
    if key not in _version_cache:
        _version_cache[key] = zellij_version(binary)
    return _version_cache[key]


def zellij_binary() -> str | None:
    """The zellij to run, in the order the module docstring gives."""
    return web_binary() or usual_binary()


def usual_binary() -> str | None:
    """The zellij run while web watching is off: PATH at the floor, else managed."""
    found = path_binary()
    managed = managed_binary()
    if managed is None:
        return found
    if found is not None:
        version = _cached_version(found)
        if version is not None and version >= ZELLIJ_FLOOR:
            return found
    return managed


def zellij_version(binary: str | None = None) -> tuple[int, int, int] | None:
    """Return zellij's (major, minor, patch), or None when it cannot be read."""
    binary = binary or zellij_binary()
    if binary is None:
        return None
    try:
        result = _run([binary, "--version"])
    except RuntimeError:
        return None
    if result is None:
        return None
    match = _VERSION.match((result.stdout or "").strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch
