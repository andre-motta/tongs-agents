"""Runtime locations owned by tongs-agents.

- Config lives in ``$XDG_CONFIG_HOME/tongs-agents`` (``~/.config/tongs-agents``
  when unset or relative); ``TONGS_AGENTS_CONFIG_DIR`` replaces it.
- State lives in ``$XDG_STATE_HOME/tongs-agents`` (``~/.local/state/tongs-agents``
  when unset or relative); ``TONGS_AGENTS_STATE_DIR`` replaces it.
- Runtime files live in ``$XDG_RUNTIME_DIR/tongs-agents``, or ``state_dir()/run``
  when that is unset or relative.

Directories created here are private (0700), never symlinks, owned by the
current user, and never equal to or inside the legacy claude-fleet-monitor
session directory. An ancestor of it (``$HOME``, for example) is allowed:
tongs-agents only writes its own named children there.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

APP_NAME = "tongs-agents"
STATE_DIR_ENV = "TONGS_AGENTS_STATE_DIR"
CONFIG_DIR_ENV = "TONGS_AGENTS_CONFIG_DIR"


class UnsafePathError(OSError):
    """A runtime directory that tongs-agents refuses to use."""


def _is_windows() -> bool:
    return os.name == "nt"


def _xdg_dir(variable: str, *default: str) -> Path:
    value = os.environ.get(variable)
    if value and os.path.isabs(value):
        return Path(value) / APP_NAME
    return Path.home().joinpath(*default, APP_NAME)


def _configured_state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _xdg_dir("XDG_STATE_HOME", ".local", "state")


def _configured_config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return _xdg_dir("XDG_CONFIG_HOME", ".config")


def _refuse_legacy_overlap(path: Path) -> None:
    # Imported here, not at module level: the hook imports paths and its
    # import surface is pinned by tests, so legacy loads only when creating.
    from tongs_agents import legacy

    owned = os.path.normcase(os.path.abspath(path))
    old = os.path.normcase(os.path.abspath(legacy.legacy_sessions_dir()))
    try:
        common = os.path.commonpath([owned, old])
    except ValueError:
        return
    if common == old:
        raise UnsafePathError(
            f"{path}: overlaps the legacy session directory {legacy.legacy_sessions_dir()}"
        )


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


def _owned_root(path: Path, create: bool) -> Path:
    if create:
        _refuse_legacy_overlap(path)
        ensure_private_dir(path)
    return path


def config_dir(create: bool = False) -> Path:
    """The tongs-agents config directory; ``create`` makes it private first."""
    return _owned_root(_configured_config_dir(), create)


def state_dir(create: bool = False) -> Path:
    """The tongs-agents state directory; ``create`` makes it private first."""
    return _owned_root(_configured_state_dir(), create)


def runtime_dir(create: bool = False) -> Path:
    """Short-lived files (spawn requests, locks); ``create`` makes it private."""
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg and os.path.isabs(xdg):
        return _owned_root(Path(xdg) / APP_NAME, create)
    path = state_dir(create) / "run"
    if create:
        ensure_private_dir(path)
    return path


def sessions_dir(create: bool = False) -> Path:
    """Session records written by hooks and read by every consumer."""
    path = state_dir(create) / "sessions"
    if create:
        ensure_private_dir(path)
    return path


def launches_dir(create: bool = False) -> Path:
    """Launch records written by the spawner and claimed by the launcher."""
    path = state_dir(create) / "launches"
    if create:
        ensure_private_dir(path)
    return path
