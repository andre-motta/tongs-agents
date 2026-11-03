"""Locate the zellij binary and read its version.

A zellij on PATH at or above the floor wins. Otherwise the managed binary
that ``tongs-agents zellij install`` (or ``doctor --fix zellij``) placed in
``state_dir()/bin/zellij`` is used when it exists, and failing that the PATH
binary is returned as it is so callers can report its version.

This module never downloads anything; ``tongs_agents.zellij_download`` does.
"""

from __future__ import annotations

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
_VERSION = re.compile(r"zellij (\d{1,4})\.(\d{1,4})\.(\d{1,4})(?![0-9])")
_version_cache: dict[tuple, tuple[int, int, int] | None] = {}


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def managed_bin_dir() -> Path:
    return paths.state_dir() / MANAGED_BIN_DIR


def managed_link() -> Path:
    """``state_dir()/bin/zellij``, the link the installer repoints."""
    return managed_bin_dir() / MANAGED_NAME


def managed_binary() -> str | None:
    """The managed zellij when its link resolves to an executable regular file."""
    link = managed_link()
    try:
        info = os.stat(link)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or not os.access(link, os.X_OK):
        return None
    return str(link)


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
    """PATH zellij at or above the floor, else the managed one, else PATH's."""
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
