"""Agent profiles: named argv commands read from the tongs-agents config file.

Only the ``[profiles]`` section of ``config_dir()/config.toml`` is read here;
other sections belong to their own loaders and are ignored. Nothing is ever
shell-parsed: a profile's command is an argv list passed to ``os.execvpe``.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from tongs_agents import paths

CONFIG_FILE_NAME = "config.toml"
MAX_CONFIG_BYTES = 64 * 1024
MAX_ENV_VALUE_BYTES = 4 * 1024
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
RESERVED_ENV_PREFIX = "TONGS_AGENTS_"

Harness = Literal["claude", "codex", "pi", "other"]
HARNESSES: tuple[str, ...] = ("claude", "codex", "pi", "other")
CONFIG_DIR_ENV_BY_HARNESS = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
}
PROFILE_KEYS = frozenset({
    "command",
    "harness",
    "env",
    "model_args",
    "effort_args",
    "config_dir",
    "cwd",
    "mcp_control",
})


class ProfileError(ValueError):
    """The config file or one of its profiles is invalid."""


@dataclass(frozen=True)
class Profile:
    name: str
    harness: Harness
    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    model_args: tuple[str, ...] = ()
    effort_args: tuple[str, ...] = ()
    config_dir: Path | None = None
    cwd: Path | None = None
    mcp_control: bool = False


def profiles_path() -> Path:
    """The config file that holds the ``[profiles]`` section."""
    return paths.config_dir() / CONFIG_FILE_NAME


def _read_config_bytes(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ProfileError(f"{path}: cannot read: {error.strerror}") from None
    if stat.S_ISLNK(info.st_mode):
        raise ProfileError(f"{path}: refusing to read a symlinked config file")
    if not stat.S_ISREG(info.st_mode):
        raise ProfileError(f"{path}: not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ProfileError(f"{path}: cannot read: {error.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise ProfileError(f"{path}: not a regular file")
        if os.name != "nt":
            if opened.st_uid != os.getuid():
                raise ProfileError(f"{path}: owned by another user")
            if opened.st_mode & 0o022:
                raise ProfileError(
                    f"{path}: writable by group or others; run chmod go-w on it"
                )
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ProfileError(f"{path}: larger than {MAX_CONFIG_BYTES // 1024} KiB")
    return raw


def _string_list(value, where: str, *, non_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProfileError(f"{where}: must be a list of strings")
    if non_empty and (not value or not value[0]):
        raise ProfileError(f"{where}: must start with a non-empty command")
    for index, item in enumerate(value):
        if "\0" in item:
            raise ProfileError(f"{where}[{index}]: contains a NUL character")
    return tuple(value)


def _env_table(value, where: str) -> Mapping[str, str]:
    if not isinstance(value, dict):
        raise ProfileError(f"{where}: must be a table of strings")
    env = {}
    for key, item in value.items():
        key_where = f"{where}.{key}"
        if not ENV_KEY_RE.match(key):
            raise ProfileError(f"{key_where}: name must match {ENV_KEY_RE.pattern}")
        if key.startswith(RESERVED_ENV_PREFIX):
            raise ProfileError(
                f"{key_where}: {RESERVED_ENV_PREFIX}* names are reserved for tongs-agents"
            )
        if not isinstance(item, str):
            raise ProfileError(f"{key_where}: must be a string")
        if "\0" in item:
            raise ProfileError(f"{key_where}: contains a NUL character")
        if len(item.encode("utf-8")) >= MAX_ENV_VALUE_BYTES:
            raise ProfileError(
                f"{key_where}: value must be under {MAX_ENV_VALUE_BYTES // 1024} KiB"
            )
        env[key] = item
    return MappingProxyType(env)


def _absolute_path(value, where: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ProfileError(f"{where}: must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProfileError(f"{where}: must be an absolute path (or start with ~/)")
    return path


def _infer_harness(argv: tuple[str, ...]) -> str:
    name = os.path.basename(argv[0])
    return name if name in CONFIG_DIR_ENV_BY_HARNESS else "other"


def parse_profile(name: str, table) -> Profile:
    """Validate one ``[profiles.<name>]`` table into a Profile."""
    where = f"profiles.{name}"
    if not PROFILE_NAME_RE.match(name):
        raise ProfileError(f"{where}: name must match {PROFILE_NAME_RE.pattern}")
    if not isinstance(table, dict):
        raise ProfileError(f"{where}: must be a table")
    unknown = sorted(set(table) - PROFILE_KEYS)
    if unknown:
        raise ProfileError(f"{where}.{unknown[0]}: unknown key")
    if "command" not in table:
        raise ProfileError(f"{where}.command: missing (an argv list, for example [\"claude\"])")
    argv = _string_list(table["command"], f"{where}.command", non_empty=True)
    harness = table.get("harness", _infer_harness(argv))
    if harness not in HARNESSES:
        raise ProfileError(f"{where}.harness: must be one of {', '.join(HARNESSES)}")
    env = _env_table(table.get("env", {}), f"{where}.env")
    model_args = _string_list(table.get("model_args", []), f"{where}.model_args")
    effort_args = _string_list(table.get("effort_args", []), f"{where}.effort_args")
    config_dir = None
    if "config_dir" in table:
        config_dir = _absolute_path(table["config_dir"], f"{where}.config_dir")
        variable = CONFIG_DIR_ENV_BY_HARNESS.get(harness)
        if variable is None:
            raise ProfileError(
                f"{where}.config_dir: needs harness claude, codex or pi (not {harness})"
            )
        if variable in env:
            raise ProfileError(
                f"{where}.config_dir: conflicts with env.{variable}; set only one"
            )
    cwd = _absolute_path(table["cwd"], f"{where}.cwd") if "cwd" in table else None
    mcp_control = table.get("mcp_control", False)
    if not isinstance(mcp_control, bool):
        raise ProfileError(f"{where}.mcp_control: must be true or false")
    return Profile(
        name=name,
        harness=harness,
        argv=argv,
        env=env,
        model_args=model_args,
        effort_args=effort_args,
        config_dir=config_dir,
        cwd=cwd,
        mcp_control=mcp_control,
    )


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """Every profile in the config file; an absent file or section yields none.

    Raises ProfileError naming the file and the offending key.
    """
    path = profiles_path() if path is None else path
    raw = _read_config_bytes(path)
    if raw is None:
        return {}
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise ProfileError(f"{path}: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"{path}: invalid TOML: {error}") from None
    section = document.get("profiles", {})
    if not isinstance(section, dict):
        raise ProfileError(f"{path}: profiles: must be a table")
    try:
        return {name: parse_profile(name, table) for name, table in section.items()}
    except ProfileError as error:
        raise ProfileError(f"{path}: {error}") from None
