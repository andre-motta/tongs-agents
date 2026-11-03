"""App settings: the ``[workspace]``, ``[ui]`` and ``[control]`` sections.

They live in the same ``config_dir()/config.toml`` as the profiles and are
read with the same checks (a regular file owned by the user, not writable by
group or others, at most 64 KiB). Sections other than these three belong to
their own loaders and are ignored here; an unknown key inside one of these
three is refused with its dotted name. tongs-agents never writes this file.
Command-line flags override the values read here.

This module imports no Textual code, so the MCP server can read
``[control]`` without loading the TUI.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tongs_agents import profiles

DEFAULT_SESSION = "agents"
DEFAULT_MULTIPLEXER = "zellij"
MULTIPLEXERS: tuple[str, ...] = ("zellij", "tmux")
DEFAULT_THEME = "textual-dark"
NOTIFY_LEVELS: tuple[str, ...] = ("all", "waiting", "none")
DEFAULT_REFRESH_INTERVAL = 2.0
MIN_REFRESH_INTERVAL = 0.5
MAX_REFRESH_INTERVAL = 3600.0
DEFAULT_SEND_DELAY_MS = 150
MAX_SEND_DELAY_MS = 5000
DEFAULT_MCP_RATE_PER_MINUTE = 6
MAX_MCP_RATE_PER_MINUTE = 60
MAX_DEFAULT_PROFILES = 32
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
THEME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

WORKSPACE_KEYS = frozenset({"session", "default_profiles", "tongs_tab", "multiplexer"})
UI_KEYS = frozenset({"refresh_interval", "notify_level", "theme", "compact"})
CONTROL_KEYS = frozenset({
    "mcp_enabled", "send_delay_ms", "mcp_any_target", "mcp_rate_per_minute",
    "mcp_nested_spawn",
})


class AppConfigError(ValueError):
    """The config file, or its [workspace], [ui] or [control] section, is invalid."""


@dataclass(frozen=True)
class WorkspaceSettings:
    session: str = DEFAULT_SESSION
    default_profiles: tuple[str, ...] = ()
    tongs_tab: bool = True
    multiplexer: str = DEFAULT_MULTIPLEXER


@dataclass(frozen=True)
class UISettings:
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL
    notify_level: str = "all"
    theme: str = DEFAULT_THEME
    compact: bool = False


@dataclass(frozen=True)
class ControlSettings:
    mcp_enabled: bool = False
    send_delay_ms: int = DEFAULT_SEND_DELAY_MS
    mcp_any_target: bool = False
    mcp_rate_per_minute: int = DEFAULT_MCP_RATE_PER_MINUTE
    mcp_nested_spawn: bool = False


@dataclass(frozen=True)
class AppConfig:
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    ui: UISettings = field(default_factory=UISettings)
    control: ControlSettings = field(default_factory=ControlSettings)


def config_path() -> Path:
    """The config file shared with the profiles."""
    return profiles.profiles_path()


def _section(document: dict, name: str, keys: frozenset[str]) -> dict:
    section = document.get(name, {})
    if not isinstance(section, dict):
        raise AppConfigError(f"{name}: must be a table")
    unknown = sorted(set(section) - keys)
    if unknown:
        raise AppConfigError(
            f"{name}.{unknown[0]}: unknown key (known: {', '.join(sorted(keys))})"
        )
    return section


def _bool(section: dict, key: str, where: str, default: bool) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise AppConfigError(f"{where}.{key}: must be true or false")
    return value


def _choice(section: dict, key: str, where: str, choices: tuple[str, ...],
            default: str) -> str:
    value = section.get(key, default)
    if value not in choices or not isinstance(value, str):
        raise AppConfigError(f"{where}.{key}: must be one of {', '.join(choices)}")
    return value


def parse_workspace(section: dict) -> WorkspaceSettings:
    where = "workspace"
    session = section.get("session", DEFAULT_SESSION)
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise AppConfigError(
            f"{where}.session: must be 1 to 64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit"
        )
    names = section.get("default_profiles", [])
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise AppConfigError(f"{where}.default_profiles: must be a list of profile names")
    if len(names) > MAX_DEFAULT_PROFILES:
        raise AppConfigError(
            f"{where}.default_profiles: at most {MAX_DEFAULT_PROFILES} profiles"
        )
    for index, name in enumerate(names):
        if not profiles.PROFILE_NAME_RE.match(name):
            raise AppConfigError(
                f"{where}.default_profiles[{index}]: {name!r:.40} is not a profile name"
            )
    return WorkspaceSettings(
        session=session,
        default_profiles=tuple(names),
        tongs_tab=_bool(section, "tongs_tab", where, True),
        multiplexer=_choice(section, "multiplexer", where, MULTIPLEXERS, DEFAULT_MULTIPLEXER),
    )


def parse_ui(section: dict) -> UISettings:
    where = "ui"
    interval = section.get("refresh_interval", DEFAULT_REFRESH_INTERVAL)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, int | float)
        or not MIN_REFRESH_INTERVAL <= interval <= MAX_REFRESH_INTERVAL
    ):
        raise AppConfigError(
            f"{where}.refresh_interval: must be a number of seconds from "
            f"{MIN_REFRESH_INTERVAL:g} to {MAX_REFRESH_INTERVAL:g}"
        )
    theme = section.get("theme", DEFAULT_THEME)
    if not isinstance(theme, str) or not THEME_RE.match(theme):
        raise AppConfigError(
            f"{where}.theme: must be a Textual theme name such as {DEFAULT_THEME!r}"
        )
    return UISettings(
        refresh_interval=float(interval),
        notify_level=_choice(section, "notify_level", where, NOTIFY_LEVELS, "all"),
        theme=theme,
        compact=_bool(section, "compact", where, False),
    )


def parse_control(section: dict) -> ControlSettings:
    where = "control"
    delay = section.get("send_delay_ms", DEFAULT_SEND_DELAY_MS)
    if isinstance(delay, bool) or not isinstance(delay, int) or not (
        0 <= delay <= MAX_SEND_DELAY_MS
    ):
        raise AppConfigError(
            f"{where}.send_delay_ms: must be a whole number from 0 to {MAX_SEND_DELAY_MS}"
        )
    rate = section.get("mcp_rate_per_minute", DEFAULT_MCP_RATE_PER_MINUTE)
    if isinstance(rate, bool) or not isinstance(rate, int) or not (
        1 <= rate <= MAX_MCP_RATE_PER_MINUTE
    ):
        raise AppConfigError(
            f"{where}.mcp_rate_per_minute: must be a whole number from 1 to "
            f"{MAX_MCP_RATE_PER_MINUTE}"
        )
    return ControlSettings(
        mcp_enabled=_bool(section, "mcp_enabled", where, False),
        send_delay_ms=delay,
        mcp_any_target=_bool(section, "mcp_any_target", where, False),
        mcp_rate_per_minute=rate,
        mcp_nested_spawn=_bool(section, "mcp_nested_spawn", where, False),
    )


def parse_document(document: dict) -> AppConfig:
    """The three app sections of a parsed TOML document; others are ignored."""
    return AppConfig(
        workspace=parse_workspace(_section(document, "workspace", WORKSPACE_KEYS)),
        ui=parse_ui(_section(document, "ui", UI_KEYS)),
        control=parse_control(_section(document, "control", CONTROL_KEYS)),
    )


def load_app_config(path: Path | None = None) -> AppConfig:
    """The app settings from the config file; an absent file or section yields defaults.

    Raises AppConfigError naming the file and the offending key. The file is
    only read.
    """
    path = config_path() if path is None else path
    try:
        raw = profiles._read_config_bytes(path)
    except profiles.ProfileError as error:
        raise AppConfigError(str(error)) from None
    if raw is None:
        return AppConfig()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise AppConfigError(f"{path}: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as error:
        raise AppConfigError(f"{path}: invalid TOML: {error}") from None
    try:
        return parse_document(document)
    except AppConfigError as error:
        raise AppConfigError(f"{path}: {error}") from None
