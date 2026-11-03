"""Locate the zellij binary and read its version.

M1 resolves zellij from PATH only; a managed download comes later.
"""

from __future__ import annotations

import re
import shutil

from tongs_agents.terminal_apis.zellij import _run

ZELLIJ_FLOOR = (0, 44, 2)
ZELLIJ_CEILING = (0, 45, 1)
ZELLIJ_VALIDATED = ZELLIJ_CEILING
_VERSION = re.compile(r"zellij (\d{1,4})\.(\d{1,4})\.(\d{1,4})(?![0-9])")


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def zellij_binary() -> str | None:
    return shutil.which("zellij")


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
