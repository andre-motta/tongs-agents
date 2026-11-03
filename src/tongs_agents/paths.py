"""Runtime locations owned by tongs-agents.

State lives in ``$XDG_STATE_HOME/tongs-agents`` (``~/.local/state/tongs-agents``
when unset or relative). ``TONGS_AGENTS_STATE_DIR`` replaces that whole
directory. Directories created here are private (0700) and never symlinks.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

APP_NAME = "tongs-agents"
STATE_DIR_ENV = "TONGS_AGENTS_STATE_DIR"


class UnsafePathError(OSError):
    """A runtime directory that tongs-agents refuses to use."""


def _is_windows() -> bool:
    return os.name == "nt"


def _configured_state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg and os.path.isabs(xdg):
        return Path(xdg) / APP_NAME
    return Path.home() / ".local" / "state" / APP_NAME


def is_private_dir(path: Path) -> bool:
    """True when ``path`` is a real directory that only its owner can use."""
    try:
        info = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if _is_windows():
        return True
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` with mode 0700, or tighten one we own; refuse symlinks."""
    try:
        path.mkdir(mode=0o700, parents=True)
    except FileExistsError:
        pass
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise UnsafePathError(f"{path}: refusing to use a symlinked directory")
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{path}: not a directory")
    if _is_windows():
        return path
    if info.st_uid != os.getuid():
        raise UnsafePathError(f"{path}: owned by another user")
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def state_dir(create: bool = False) -> Path:
    """The tongs-agents state directory; ``create`` makes it private first."""
    path = _configured_state_dir()
    if create:
        ensure_private_dir(path)
    return path


def sessions_dir(create: bool = False) -> Path:
    """Session records written by hooks and read by every consumer."""
    path = state_dir(create) / "sessions"
    if create:
        ensure_private_dir(path)
    return path
