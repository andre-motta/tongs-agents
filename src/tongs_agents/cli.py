"""CLI for installing and using tongs-agents."""

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from tongs_agents import __version__, commands, legacy, owned, paths, session_store, toml_tables
from tongs_agents.pi_install import (
    PI_CONFIG_NAME,
    install_pi,
    pi_config_dir,
    pi_environment,
    uninstall_pi,
)
from tongs_agents.profiles import CONFIG_DIR_ENV_BY_HARNESS, ProfileError, load_profiles


CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_USER_CONFIG = Path.home() / ".claude.json"
CODEX_DIR = Path.home() / ".codex"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
CODEX_HOOKS_FILE = CODEX_DIR / "hooks.json"
HOOK_COMMAND = "tongs-agents-hook"
MCP_MODULE = "tongs_agents.mcp_server"
MCP_NAME = "tongs-agents"
MCP_NAMES = (MCP_NAME, legacy.LEGACY_MCP_NAME)
# Codex starts a stdio MCP server with only HOME, LANG, PATH, TMPDIR and USER
# plus the names in its env_vars (docs/work/emitter-strategy.md), so the
# tongs-agents server lists everything its chat and control identity, and its
# state, config and runtime dirs, are read from.
CODEX_MCP_ENV_VARS = (
    "TONGS_AGENTS_LAUNCH_ID",
    "TONGS_AGENTS_PROFILE",
    "ZELLIJ_SESSION_NAME",
    "ZELLIJ_SOCKET_DIR",
    "TMUX",
    "TMUX_PANE",
    "XDG_STATE_HOME",
    paths.STATE_DIR_ENV,
    "XDG_CONFIG_HOME",
    paths.CONFIG_DIR_ENV,
    "XDG_RUNTIME_DIR",
)
HARNESS_NAMES = {"claude": "Claude Code", "codex": "Codex", "pi": "Pi"}
FLEET_FILE_PATTERNS = (
    "fleet-v*-*.json",
    ".fleet-v*-*.lock",
    ".fleet-identity-v*-*.lock",
    ".fleet-tmp-*.tmp",
)

CLAUDE_HOOK_EVENTS = [
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "prompt-submit"),
    ("PreToolUse", "tool-use"),
    ("Stop", "stop"),
    ("StopFailure", "stop-failure"),
    ("SessionEnd", "session-end"),
    ("PermissionRequest", "permission-request"),
    ("Elicitation", "elicitation"),
]

CODEX_HOOK_EVENTS = [
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "prompt-submit"),
    ("PreToolUse", "tool-use"),
    ("Stop", "stop"),
    ("SessionEnd", "session-end"),
    ("PermissionRequest", "permission-request"),
]


# A config file the installer must not modify; every owned.py refusal is one.
# It subclasses OSError: catch ConfigError before any broad ``except OSError``,
# which would otherwise swallow the refusal as an ordinary I/O error.
ConfigError = owned.OwnedFileError


def _is_windows():
    return os.name == "nt"


def _command_string(arguments):
    if _is_windows():
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


def _command_argv(command):
    try:
        if _is_windows():
            return [part.strip('"') for part in shlex.split(command, posix=False)]
        return shlex.split(command)
    except ValueError:
        return []


def _executable_name(path):
    name = re.split(r"[\\/]", path)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def _is_legacy_script(path):
    expanded = Path(path).expanduser()
    if not expanded.is_absolute():
        return False
    try:
        return expanded.resolve() in legacy.LEGACY_HOOK_SCRIPTS
    except (OSError, RuntimeError):
        return False


def _starts_with_legacy_script(command):
    """The bash-era installer wrote ``<script> <arg>`` unquoted, so a HOME with a
    space splits argv[0]; try each prefix that ends at a space or the end."""
    ends = [index for index, char in enumerate(command) if char == " "]
    return any(_is_legacy_script(command[:end]) for end in [*ends, len(command)] if end)


def _is_owned_hook(hook):
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    argv = _command_argv(hook["command"])
    if argv and argv[0]:
        if _executable_name(argv[0]) in (HOOK_COMMAND, *legacy.LEGACY_HOOK_COMMANDS):
            return True
        if _is_legacy_script(argv[0]):
            return True
    return _starts_with_legacy_script(hook["command"])


def _same_path(left, right):
    def normal(value):
        return os.path.normcase(os.path.abspath(os.path.expanduser(value)))
    return normal(str(left)) == normal(str(right))


def legacy_mcp_dir():
    return CLAUDE_DIR / legacy.LEGACY_MCP_DIR_NAME


def _is_legacy_uv_mcp(transport):
    command = transport.get("command")
    args = transport.get("args")
    return (
        isinstance(command, str)
        and _executable_name(command).lower() == legacy.LEGACY_MCP_RUNNER
        and isinstance(args, list)
        and len(args) == 4
        and args[:2] == ["run", "--directory"]
        and args[3] == legacy.LEGACY_MCP_SCRIPT
        and isinstance(args[2], str)
        and _same_path(args[2], legacy_mcp_dir())
    )


def _is_owned_mcp(config):
    if not isinstance(config, dict):
        return False
    transport = config.get("transport", config)
    if not isinstance(transport, dict):
        return False
    command = transport.get("command")
    owned_args = [["-m", module] for module in (MCP_MODULE, *legacy.LEGACY_MCP_MODULES)]
    return (
        isinstance(command, str)
        and transport.get("args") in owned_args
        and _executable_name(command).lower().startswith("python")
    ) or _is_legacy_uv_mcp(transport)


def _is_current_mcp(config):
    transport = config.get("transport", config)
    return (
        transport.get("command") == sys.executable
        and transport.get("args") == ["-m", MCP_MODULE]
    )


def _validate_config(path, data):
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    hooks = data.get("hooks")
    if "hooks" in data and (
        not isinstance(hooks, dict)
        or any(not isinstance(groups, list) for groups in hooks.values())
    ):
        raise ConfigError(f"{path}: 'hooks' must map event names to lists")
    if "mcpServers" in data and not isinstance(data["mcpServers"], dict):
        raise ConfigError(f"{path}: 'mcpServers' must be a JSON object")


def load_json(path):
    return owned.read_json(path, missing={}, validate=_validate_config)


def save_json(path, data, mode=None):
    """Write tongs-agents' own JSON state; harness files go through owned.ChangeSet."""
    owned.atomic_write(path, (json.dumps(data, indent=2) + "\n").encode("utf-8"), mode=mode)


HOOK_OWNERSHIP = owned.EntryOwnership(validate=_validate_config)


def _strip_owned_hooks(config):
    touched = set()
    for event, groups in config.get("hooks", {}).items():
        kept = []
        for group in groups:
            commands = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(commands, list):
                kept.append(group)
                continue
            remaining = [command for command in commands if not _is_owned_hook(command)]
            if len(remaining) == len(commands):
                kept.append(group)
                continue
            touched.add(event)
            if remaining:
                group["hooks"] = remaining
                kept.append(group)
        groups[:] = kept
    return touched


def _install_hooks(config, events, hook_cmd, agent):
    touched = _strip_owned_hooks(config)
    hooks = config.setdefault("hooks", {})
    managed = {event for event, _ in events}
    for event in touched - managed:
        if not hooks[event]:
            del hooks[event]
    for event, arg in events:
        event_hooks = hooks.setdefault(event, [])
        timeout = 3 if event == "SessionEnd" else 5
        hook_group = {
            "hooks": [{
                "type": "command",
                "command": _command_string([hook_cmd, arg, "--agent", agent]),
                "timeout": timeout,
            }]
        }
        if agent == "codex" and event == "SessionStart":
            hook_group["matcher"] = "startup|resume|clear"
        event_hooks.append(hook_group)


def _remove_hooks(config):
    touched = _strip_owned_hooks(config)
    if not touched:
        return
    hooks = config["hooks"]
    for event in touched:
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        del config["hooks"]


def _remove_settings_mcp(settings):
    """Drop our entries from settings.json, which Claude does not read for MCP."""
    servers = settings.get("mcpServers")
    if not isinstance(servers, dict):
        return
    removed = [name for name in MCP_NAMES if _is_owned_mcp(servers.get(name))]
    for name in removed:
        del servers[name]
    if removed and not servers:
        del settings["mcpServers"]


KINDS = ("claude", "codex", "pi")
DIR_OPTIONS = {
    "claude": ("--claude-config-dir", "claude_config_dirs"),
    "codex": ("--codex-home", "codex_homes"),
    "pi": ("--pi-agent-dir", "pi_agent_dirs"),
}
INSTALLS_FILE_NAME = "installs.json"
INSTALLS_SCHEMA = 1


@dataclass(frozen=True)
class Target:
    """One harness config home: ``directory`` None means the harness default."""

    harness: str
    directory: Path | None = None
    sources: tuple[str, ...] = field(default=(), compare=False)

    @property
    def config_file(self) -> Path:
        if self.harness == "claude":
            return SETTINGS_FILE if self.directory is None else self.directory / "settings.json"
        if self.harness == "codex":
            return CODEX_HOOKS_FILE if self.directory is None else self.directory / "hooks.json"
        return self.pi_dir / PI_CONFIG_NAME

    @property
    def pi_dir(self) -> Path:
        return pi_config_dir() if self.directory is None else self.directory

    @property
    def claude_user_config(self) -> Path:
        if self.directory is None:
            return CLAUDE_USER_CONFIG
        return self.directory / ".claude.json"

    @property
    def codex_home(self) -> Path:
        return CODEX_DIR if self.directory is None else self.directory

    def install_command(self) -> str:
        if any(source.startswith("profile ") for source in self.sources):
            return "tongs-agents install"
        if self.directory is None:
            return f"tongs-agents install --agent {self.harness}"
        flag = DIR_OPTIONS[self.harness][0]
        return _command_string(["tongs-agents", "install", flag, str(self.directory)])

    def describe(self) -> str:
        where = "default" if self.directory is None else str(self.directory)
        return f"{self.harness} ({where})"


def _normalize_dir(value) -> Path:
    return Path(os.path.abspath(Path(value).expanduser()))


def _make_target(harness, directory, source) -> Target:
    if directory is not None:
        directory = _normalize_dir(directory)
        if harness == "codex" and directory == _normalize_dir(CODEX_DIR):
            directory = None
        elif harness == "pi" and directory == _normalize_dir(pi_config_dir()):
            directory = None
    return Target(harness, directory, (source,))


def _merge_targets(targets):
    merged = {}
    for target in targets:
        key = (target.harness, target.directory)
        if key in merged:
            previous = merged[key]
            sources = tuple(dict.fromkeys(previous.sources + target.sources))
            merged[key] = Target(target.harness, target.directory, sources)
        else:
            merged[key] = target
    return list(merged.values())


def profile_home(profile):
    """The config home a profile's harness uses: (harness, dir or None, problem).

    ``dir`` None is the harness default. ``problem`` is set when the profile
    names a home tongs-agents cannot use, such as a relative env value.
    """
    variable = CONFIG_DIR_ENV_BY_HARNESS.get(profile.harness)
    if variable is None:
        return None, None, "harness other has no hooks"
    if profile.config_dir is not None:
        return profile.harness, _normalize_dir(profile.config_dir), None
    value = profile.env.get(variable)
    if value is None:
        return profile.harness, None, None
    if not os.path.isabs(value):
        return profile.harness, None, f"env.{variable} is not an absolute path: {value!r}"
    return profile.harness, _normalize_dir(value), None


def profile_targets(profiles, harnesses=KINDS):
    """Host config homes of ``profiles``; a sandboxed profile has none.

    A profile with a sandbox table runs its agent inside OpenShell, whose
    image carries its own spool hooks (docs/sandbox.md), so its host default
    home is not one of its targets.
    """
    targets = []
    for profile in profiles.values():
        if profile.sandbox is not None:
            continue
        harness, directory, problem = profile_home(profile)
        if harness is None or harness not in harnesses or problem is not None:
            continue
        targets.append(_make_target(harness, directory, f"profile {profile.name}"))
    return _merge_targets(targets)


def installs_path() -> Path:
    return paths.state_dir() / INSTALLS_FILE_NAME


def load_installs():
    """Config homes installed beyond the defaults, from ``installs.json``."""
    path = installs_path()
    data = load_json(path)
    if not data:
        return {kind: [] for kind in KINDS}
    dirs = data.get("dirs")
    if data.get("schema_version") != INSTALLS_SCHEMA or not isinstance(dirs, dict):
        raise ConfigError(f"{path}: unrecognized install record")
    installs = {}
    for kind in KINDS:
        entries = dirs.get(kind, [])
        if not isinstance(entries, list) or not all(
            isinstance(item, str) and os.path.isabs(item) for item in entries
        ):
            raise ConfigError(f"{path}: unrecognized install record")
        installs[kind] = list(dict.fromkeys(entries))
    return installs


def save_installs(installs):
    path = installs_path()
    if not any(installs.get(kind) for kind in KINDS):
        if path.is_symlink():
            raise ConfigError(f"{path}: refusing to modify a symlinked file")
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    try:
        paths.state_dir(create=True)
    except OSError as error:
        raise ConfigError(f"cannot use the state directory: {error}") from None
    data = {
        "schema_version": INSTALLS_SCHEMA,
        "dirs": {kind: sorted(set(installs.get(kind, []))) for kind in KINDS},
    }
    save_json(path, data, mode=0o600)


def recorded_targets(installs, harnesses=KINDS):
    return [
        _make_target(kind, directory, "recorded")
        for kind in harnesses
        for directory in installs.get(kind, [])
    ]


def _selected_harnesses(agent):
    if agent is None:
        return ("claude", "codex")
    if agent == "all":
        return KINDS
    return (agent,)


def _flag_targets(args):
    targets = []
    for kind, (_, attribute) in DIR_OPTIONS.items():
        for directory in getattr(args, attribute, None) or []:
            targets.append(_make_target(kind, directory, "option"))
    return _merge_targets(targets)


def _load_profiles_or_raise():
    try:
        return load_profiles()
    except ProfileError as error:
        raise ConfigError(str(error)) from None


def _plan(args, *, uninstall=False):
    """The targets install or uninstall acts on, and whether options chose them."""
    agent = getattr(args, "agent", None)
    explicit = _flag_targets(args)
    if explicit:
        if agent is not None:
            raise ConfigError("--agent cannot be combined with config dir options")
        return explicit, True
    selected = _selected_harnesses(agent)
    profile_harnesses = KINDS if agent is None else selected
    targets = [_make_target(kind, None, "default") for kind in selected]
    if uninstall:
        try:
            profiles = load_profiles()
        except ProfileError as error:
            print(f"  Skipped profiles: {error}")
            profiles = {}
        targets += profile_targets(profiles, profile_harnesses)
        targets += recorded_targets(load_installs(), profile_harnesses)
    else:
        targets += profile_targets(_load_profiles_or_raise(), profile_harnesses)
    return _merge_targets(targets), False


def _claude_environment(directory=None):
    environment = os.environ.copy()
    environment.pop("CLAUDE_CONFIG_DIR", None)
    if directory is not None:
        environment["CLAUDE_CONFIG_DIR"] = str(directory)
    return environment


def _claude_user_servers(config=None):
    """User-scope MCP servers, read without editing Claude's own file."""
    config = CLAUDE_USER_CONFIG if config is None else config
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _run_mcp_cli(label, argv, action, environment=None, timeout=10):
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"  Could not {action} {label} MCP server: {error}")
        return False
    if result.returncode != 0:
        print(f"  Could not {action} {label} MCP server: {result.stderr.strip()}")
        return False
    return True


def _run_claude_mcp(claude_cmd, arguments, action, directory=None):
    return _run_mcp_cli(
        "Claude Code", [claude_cmd, "mcp", *arguments], action,
        _claude_environment(directory), timeout=60,
    )


def _claude_servers_for(directory):
    if directory is None:
        return _claude_user_servers()
    return _claude_user_servers(directory / ".claude.json")


def _restore_claude_mcp(claude_cmd, previous, directory=None):
    """Re-add a replaced entry after a failed add, and say what to rerun."""
    rerun = Target("claude", directory).install_command()
    restored = _run_claude_mcp(
        claude_cmd,
        ["add-json", "--scope", "user", MCP_NAME, json.dumps(previous)],
        "restore",
        directory,
    )
    if restored:
        print(f"  Restored the previous Claude Code MCP server {MCP_NAME}; "
              f"run '{rerun}' to retry the update")
    else:
        print(f"  The previous Claude Code MCP server {MCP_NAME} was removed and could not "
              f"be restored; run '{rerun}' to register it again")


def _install_claude_mcp(claude_cmd, directory=None):
    """Register our Claude Code MCP server; True only when our entry is in place."""
    servers = _claude_servers_for(directory)
    if _is_owned_mcp(servers.get(legacy.LEGACY_MCP_NAME)):
        _run_claude_mcp(
            claude_cmd, ["remove", "--scope", "user", legacy.LEGACY_MCP_NAME], "remove",
            directory,
        )
    existing = servers.get(MCP_NAME)
    if existing is not None:
        if not _is_owned_mcp(existing):
            print(f"  Kept existing Claude Code MCP server named {MCP_NAME}")
            return False
        if _is_current_mcp(existing):
            return True
        if not _run_claude_mcp(
            claude_cmd, ["remove", "--scope", "user", MCP_NAME], "replace", directory
        ):
            return False
    added = _run_claude_mcp(
        claude_cmd,
        ["add", "--scope", "user", MCP_NAME, "--", sys.executable, "-m", MCP_MODULE],
        "add",
        directory,
    )
    if not added and existing is not None:
        _restore_claude_mcp(claude_cmd, existing, directory)
    return added


def _uninstall_claude_mcp(claude_cmd, directory=None):
    servers = _claude_servers_for(directory)
    for name in MCP_NAMES:
        if _is_owned_mcp(servers.get(name)):
            _run_claude_mcp(
                claude_cmd, ["remove", "--scope", "user", name], "remove", directory
            )


def codex_mcp_table():
    """The ``[mcp_servers.tongs-agents]`` table install and ``config apply`` write."""
    return {
        "command": sys.executable,
        "args": ["-m", MCP_MODULE],
        "env_vars": list(CODEX_MCP_ENV_VARS),
    }


def codex_config_file(directory=None):
    return Target("codex", directory).codex_home / "config.toml"


def _without_enabled(value):
    if isinstance(value, dict) and value.get("enabled") is False:
        return {key: item for key, item in value.items() if key != "enabled"}, True
    return value, False


def _codex_install_changes(text, kept):
    """Table changes that register our server; a foreign table is kept and reported."""
    tables = toml_tables.read_tables(text)
    changes = {}
    if _is_owned_mcp(tables.get(legacy.LEGACY_MCP_NAME)):
        changes[legacy.LEGACY_MCP_NAME] = None
    existing = tables.get(MCP_NAME)
    if existing is not None and not _is_owned_mcp(existing):
        kept.append(MCP_NAME)
        return changes
    current, disabled = _without_enabled(existing)
    wanted = codex_mcp_table()
    if current != wanted:
        changes[MCP_NAME] = {**wanted, "enabled": False} if disabled else wanted
    return changes


def _codex_uninstall_changes(text):
    tables = toml_tables.read_tables(text)
    return {name: None for name in MCP_NAMES if _is_owned_mcp(tables.get(name))}


def _edit_codex_config(path, plan, action):
    """Apply ``plan(text)`` table changes to ``path`` with the owned-table writer.

    The first edit of an existing file keeps ``<name>.tongs-agents.bak``. Every
    other byte of the file, user comments included, is kept. Returns False and
    prints why when the file cannot be edited exactly.
    """
    def mutate(text):
        changes = plan(text)
        return toml_tables.render_tables(text, changes) if changes else text

    changes = owned.ChangeSet()
    try:
        change = changes.edit_text_entries(path, mutate)
        changes.commit()
    except (owned.OwnedFileError, toml_tables.TomlTableError) as error:
        detail = str(error)
        if not detail.startswith(str(path)):
            detail = f"{path}: {detail}"
        print(f"  Could not {action} the Codex MCP server: {detail}")
        return False
    if change.kind != "unchanged":
        print(f"  Updated {path}")
    return True


def _install_codex_mcp(directory=None):
    """Register our Codex MCP table; True only when our table is in place.

    A foreign table, an unparseable or symlinked config.toml and any write error
    return False.
    """
    path = codex_config_file(directory)
    kept = []
    done = _edit_codex_config(path, lambda text: _codex_install_changes(text, kept), "register")
    if kept:
        print(f"  Kept existing Codex MCP server named {MCP_NAME}")
    return done and not kept


def _uninstall_codex_mcp(directory=None):
    path = codex_config_file(directory)
    if not os.path.lexists(path):
        return True
    return _edit_codex_config(path, _codex_uninstall_changes, "remove")


def has_owned_hooks(path):
    """True when ``path`` holds any tongs-agents (or legacy) hook command.

    An unreadable file counts as holding hooks, so callers stay conservative.
    """
    try:
        config = load_json(path)
    except ConfigError:
        return True
    return any(
        _is_owned_hook(hook)
        for groups in config.get("hooks", {}).values()
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
    )


def _remaining_monitoring(installs):
    """A description of monitoring that is still installed, or None."""
    homes = [Target("claude"), Target("codex"), *recorded_targets(installs, ("claude", "codex"))]
    for target in homes:
        if os.path.lexists(target.config_file) and has_owned_hooks(target.config_file):
            return f"{target.config_file} still has tongs-agents hooks"
    for target in [Target("pi"), *recorded_targets(installs, ("pi",))]:
        if os.path.lexists(target.config_file):
            return "Pi monitoring is still installed"
    return None


def _remove_session_data(installs=None):
    directory = paths.sessions_dir()
    if directory.is_symlink():
        print(f"  Kept {directory} because it is a symlink")
        return
    if not directory.is_dir():
        return
    try:
        installs = load_installs() if installs is None else installs
    except ConfigError as error:
        print(f"  Kept {directory} because {error}")
        return
    remaining = _remaining_monitoring(installs)
    if remaining is not None:
        print(f"  Kept {directory} because {remaining}")
        return
    for entry in directory.iterdir():
        if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in FLEET_FILE_PATTERNS):
            continue
        try:
            if stat.S_ISREG(entry.lstat().st_mode):
                entry.unlink()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        print(f"  Removed session records; kept {directory} because it holds other files")
        return
    print(f"  Removed {directory}")
    try:
        paths.state_dir().rmdir()
    except OSError:
        pass


def _hook_events(harness):
    return CLAUDE_HOOK_EVENTS if harness == "claude" else CODEX_HOOK_EVENTS


PI_PREREQUISITE = "Pi and tongs-agents-hook must be installed before enabling Pi"


def _profile_only(target):
    return bool(target.sources) and all(
        source.startswith("profile ") for source in target.sources
    )


def _describe_sources(target):
    profiles = [source for source in target.sources if source.startswith("profile ")]
    return f" (for {', '.join(profiles)})" if profiles else ""


def _record(installs, target, installed):
    if target.directory is None:
        return
    entries = installs.setdefault(target.harness, [])
    value = str(target.directory)
    if installed and value not in entries:
        entries.append(value)
    if not installed and value in entries:
        entries.remove(value)


def _install_edit(target, hook_cmd):
    def edit(config):
        _install_hooks(config, _hook_events(target.harness), hook_cmd, target.harness)
        if target.harness == "claude":
            _remove_settings_mcp(config)
    return edit


def _uninstall_edit(target):
    def edit(config):
        _remove_hooks(config)
        if target.harness == "claude":
            _remove_settings_mcp(config)
    return edit


def cmd_install(args):
    targets, _ = _plan(args)
    installs = load_installs()
    hook_cmd = shutil.which("tongs-agents-hook") or "tongs-agents-hook"
    hook_targets = [target for target in targets if target.harness != "pi"]
    pi_targets = [target for target in targets if target.harness == "pi"]

    changes = owned.ChangeSet()
    for target in hook_targets:
        changes.edit_json_entries(
            target.config_file, _install_edit(target, hook_cmd), ownership=HOOK_OWNERSHIP
        )

    if hook_targets:
        try:
            paths.sessions_dir(create=True)
        except OSError as error:
            raise ConfigError(f"cannot use the session directory: {error}") from None
    changes.commit()
    commands_found = {name: shutil.which(name) for name in ("claude", "codex")}
    installed = set()
    with_mcp = set()
    mcp_failed = set()
    skipped = set()
    for target in hook_targets:
        _record(installs, target, True)
        installed.add(target.harness)
        print(f"  Updated {target.config_file}{_describe_sources(target)}")
        command = commands_found[target.harness]
        if not command:
            if target.harness not in skipped:
                skipped.add(target.harness)
                name = HARNESS_NAMES[target.harness]
                print(f"  {name} CLI not found; skipped {name} MCP registration")
            continue
        if target.harness == "claude":
            registered = _install_claude_mcp(command, target.directory)
        else:
            registered = _install_codex_mcp(target.directory)
        (with_mcp if registered else mcp_failed).add(target.harness)

    pi_failed = False
    if pi_targets:
        pi_cmd = shutil.which("pi")
        if pi_cmd is None or not Path(hook_cmd).is_absolute():
            requested = [target for target in pi_targets if not _profile_only(target)]
            for target in pi_targets:
                if _profile_only(target):
                    print(f"  Warning: skipped Pi monitoring{_describe_sources(target)}: "
                          f"{PI_PREREQUISITE}")
            if requested:
                print(f"  {PI_PREREQUISITE}")
                pi_failed = True
        else:
            for target in pi_targets:
                environment = None if target.directory is None else pi_environment(
                    target.directory
                )
                if install_pi(pi_cmd, hook_cmd, environment):
                    _record(installs, target, True)
                    installed.add("pi")
                else:
                    pi_failed = True
    save_installs(installs)
    if pi_failed:
        raise SystemExit(1)
    _print_install_summary(installed, with_mcp, mcp_failed)


def _names(harnesses):
    names = [HARNESS_NAMES[kind] for kind in KINDS if kind in harnesses]
    if len(names) < 2:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _print_install_summary(installed, with_mcp, mcp_failed=frozenset()):
    """What install actually set up, from the harnesses it wrote, and what to run next.

    ``with_mcp`` holds harnesses whose MCP server is registered, ``mcp_failed``
    those where registration was attempted and did not leave our entry in place.
    """
    print()
    if not installed:
        print("Tongs Agents installed nothing: no agent integration could be set up.")
        return
    print(f"Tongs Agents installed for {_names(installed)}!")
    with_mcp = set(with_mcp) - set(mcp_failed)
    hooks_only = {kind for kind in installed if kind != "pi"} - with_mcp - set(mcp_failed)
    if hooks_only:
        print(f"  Hooks only for {_names(hooks_only)} (CLI not found; no MCP server "
              "registered)")
    if mcp_failed:
        print(f"  Hooks only for {_names(mcp_failed)} (MCP server not registered; "
              "see the messages above)")
    print()
    print("Next:")
    print("  tongs-agents doctor           # Check the installation")
    print("  tongs-agents                  # Open the workspace (only the sidebar)")
    print("  tongs-agents run PROFILE DIR  # Add an agent pane in a folder")
    print("  tongs-agents monitor          # TUI dashboard")
    if with_mcp:
        print(f"  (MCP tools available in {_names(with_mcp)} sessions)")
    if os.path.lexists(legacy.legacy_sessions_dir()):
        print()
        print(f"{legacy.legacy_sessions_dir()} is no longer used; "
              "see 'tongs-agents doctor' to remove it.")
    print()
    print("Restart agent sessions to activate monitoring.")
    if "codex" in installed:
        print("In Codex, review the installed hooks with /hooks.")


def _legacy_mcp_registrations():
    """Claude Code MCP entries that still start the bash-era server."""
    found = []
    for path, servers in (
        (CLAUDE_USER_CONFIG, _claude_user_servers()),
        (SETTINGS_FILE, _claude_user_servers(SETTINGS_FILE)),
    ):
        for name, config in servers.items():
            transport = config.get("transport", config) if isinstance(config, dict) else None
            if isinstance(transport, dict) and _is_legacy_uv_mcp(transport):
                found.append((path, name))
    return found


def _remove_legacy_claude_files():
    bin_dir = CLAUDE_DIR / "bin"
    mcp_dir = legacy_mcp_dir()
    for name in ("fleet-hook.sh", "fleet-monitor.sh", "fleet-focus.sh"):
        f = bin_dir / name
        if f.exists():
            f.unlink()
            print(f"  Removed legacy {f}")
    if not mcp_dir.exists():
        return
    registered = _legacy_mcp_registrations()
    if registered:
        path, name = registered[0]
        print(f"  Kept legacy {mcp_dir} because {path} still registers MCP server "
              f"'{name}' from it; run "
              f"'{_command_string(['claude', 'mcp', 'remove', '--scope', 'user', name])}' "
              "and uninstall again")
        return
    shutil.rmtree(mcp_dir)
    print(f"  Removed legacy {mcp_dir}")


def cmd_uninstall(args):
    agent = getattr(args, "agent", None)
    targets, explicit = _plan(args, uninstall=True)
    installs = load_installs()
    hook_targets = [target for target in targets if target.harness != "pi"]
    pi_targets = [target for target in targets if target.harness == "pi"]
    edited = [
        target for target in hook_targets
        if target.directory is None or os.path.lexists(target.config_file)
    ]
    changes = owned.ChangeSet()
    for target in edited:
        changes.edit_json_entries(
            target.config_file, _uninstall_edit(target), ownership=HOOK_OWNERSHIP
        )
    changes.commit()

    claude_cmd = shutil.which("claude")
    for target in hook_targets:
        if target in edited:
            print(f"  Updated {target.config_file}{_describe_sources(target)}")
        _record(installs, target, False)
        if target.harness == "codex":
            _uninstall_codex_mcp(target.directory)
        elif claude_cmd:
            _uninstall_claude_mcp(claude_cmd, target.directory)
        if target.directory is None and target.harness == "claude":
            _remove_legacy_claude_files()

    pi_failed = False
    if pi_targets:
        pi_cmd = shutil.which("pi")
        for target in pi_targets:
            environment = None if target.directory is None else pi_environment(
                target.directory
            )
            if uninstall_pi(pi_cmd, environment):
                _record(installs, target, False)
            else:
                pi_failed = True
    save_installs(installs)
    if pi_failed:
        raise SystemExit(1)

    full = agent is None or agent == "all" or explicit
    if full and not getattr(args, "keep_data", False):
        _remove_session_data(installs)

    harnesses = {target.harness for target in targets}
    print()
    print(f"Tongs Agents uninstalled from {_names(harnesses)}.")
    if "pi" in harnesses:
        print("Restart running Pi sessions to finish removing monitoring.")


def remove_directory_command(path):
    if _is_windows():
        return _command_string(["rmdir", "/s", "/q", str(path)])
    return _command_string(["rm", "-r", "--", str(path)])


def legacy_installed():
    """claude-fleet-monitor distributions and commands still present."""
    found = []
    try:
        version = metadata.version(legacy.LEGACY_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        pass
    else:
        found.append(f"{legacy.LEGACY_DISTRIBUTION} {version} in this Python environment")
    for command in legacy.LEGACY_COMMANDS:
        location = shutil.which(command)
        if location:
            found.append(f"{command} at {location}")
    return found


def hook_report(config, harness):
    """What a loaded hook config holds: current events, legacy and stale commands."""
    events = {}
    legacy_commands = 0
    stale = []
    expected = dict(_hook_events(harness))
    for event, groups in config.get("hooks", {}).items():
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            for hook in group["hooks"]:
                if not _is_owned_hook(hook):
                    continue
                argv = _command_argv(hook["command"])
                if not argv or _executable_name(argv[0]) != HOOK_COMMAND:
                    legacy_commands += 1
                    continue
                if event in expected and expected[event] in argv[1:2]:
                    events[event] = argv[0]
                if os.path.isabs(argv[0]) and not os.path.exists(argv[0]):
                    stale.append(argv[0])
    missing = [event for event in expected if event not in events]
    return {
        "present": sorted(events),
        "missing": missing,
        "legacy": legacy_commands,
        "stale": sorted(set(stale)),
    }


def owned_settings_mcp(config):
    servers = config.get("mcpServers", {})
    return [name for name in MCP_NAMES if _is_owned_mcp(servers.get(name))]


def cmd_monitor(args):
    from tongs_agents.tui import run_monitor
    return run_monitor(args)


def cmd_focus(args):
    from tongs_agents.focus import focus
    if not focus(args.query):
        sys.exit(1)


CLEANUP_HINT = (
    "Hint: no sidebar or monitor has maintained the session store in the last hour; "
    "run `tongs-agents cleanup` to drop ended and exited sessions."
)


def cmd_status(args):
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()
    if session_store.maintenance_overdue():
        print(CLEANUP_HINT, file=sys.stderr)

    if not sessions:
        print("No sessions found.")
        return

    sessions.sort(key=lambda s: s.get("ts", 0), reverse=True)
    print(f"{'AGENT':<8} {'REPO':<26} {'STATUS':<12} {'DETAIL':<40} {'AGE':>6}")
    print("-" * 95)
    for s in sessions:
        age = s.get("age_seconds", 0)
        detail = s.get("detail", "").replace("\n", " ").replace("\r", "")[:40]
        age_str = f"{age}s" if age < 60 else f"{age // 60}m" if age < 3600 else f"{age // 3600}h{age % 3600 // 60}m"
        print(
            f"{s.get('agent', 'claude'):<8} "
            f"{s.get('repo', '?'):<26} "
            f"{s.get('status', '?').upper():<12} "
            f"{detail:<40} "
            f"{age_str:>6}"
        )


def _add_dir_options(parser, verb):
    for kind, (flag, attribute) in DIR_OPTIONS.items():
        parser.add_argument(
            flag, dest=attribute, action="append", default=None, metavar="DIR",
            help=f"{verb} {kind} monitoring in DIR instead of the defaults (repeatable)",
        )


BUILTIN_COMMANDS = {
    "install": cmd_install,
    "uninstall": cmd_uninstall,
    "monitor": cmd_monitor,
    "focus": cmd_focus,
    "status": cmd_status,
}


PASSTHROUGH_COMMANDS = ("run",)
BARE_COMMAND = "workspace"


def parse_args(parser, argv):
    """Parse ``argv``; for ``run``, everything after the first ``--`` is ``args.extra``.

    Those arguments belong to the harness, so argparse never sees them: an
    option such as ``--resume`` or a value that looks like DIR stays in
    ``extra`` exactly as typed.
    """
    argv = list(argv)
    head, tail = argv, []
    verb = next((item for item in argv if not item.startswith("-")), None)
    if verb in PASSTHROUGH_COMMANDS and "--" in argv and argv.index(verb) < argv.index("--"):
        index = argv.index("--")
        head, tail = argv[:index], argv[index + 1:]
    args = parser.parse_args(head)
    if args.command in PASSTHROUGH_COMMANDS:
        args.extra = tail
    return args


def bare_args(parser):
    """The arguments a bare ``tongs-agents`` runs with: an empty workspace."""
    args = parser.parse_args([BARE_COMMAND])
    args.empty = True
    return args


def build_parser(modules=None):
    """The argument parser with built-in verbs and every command module.

    Returns the parser and the handler for each verb. A command module whose
    name collides with a built-in verb or another module raises.
    """
    parser = commands.CommandParser(
        prog="tongs-agents",
        description=(
            "Agent workspace for tongs: monitor and focus Claude Code, Codex and Pi "
            "sessions. Without a command it opens the workspace with only the sidebar "
            "(or attaches to it); add agents with 'tongs-agents run PROFILE [DIR]'."
        ),
    )
    parser.add_argument("--version", action="version", version=f"tongs-agents {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser(
        "install", help="Install agent monitoring",
        description=(
            "Install agent monitoring. Without --agent or config dir options this covers "
            "Claude Code, Codex and the config home of every profile. A pi profile is "
            "skipped with a warning, not an error, when pi or tongs-agents-hook is not "
            "installed; --agent pi or --pi-agent-dir still fail in that case. A profile "
            "with a sandbox table is skipped: its agent runs inside OpenShell. The Codex "
            "MCP server is written as one [mcp_servers.tongs-agents] table in config.toml; "
            "every other byte, comments included, is kept. Before it first changes an "
            "existing settings.json, hooks.json or config.toml, install keeps a copy "
            f"of it next to the file as <name>{owned.BACKUP_SUFFIX}. That copy is taken "
            "only once: later installs never refresh it and uninstall keeps it. Every hook "
            "file is written as one unit before any MCP server is registered."
        ),
    )
    p_install.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Install one agent integration; default: Claude Code, Codex and every profile",
    )
    _add_dir_options(p_install, "Install")

    p_uninstall = sub.add_parser(
        "uninstall", help="Remove agent monitoring",
        description=(
            "Remove agent monitoring: hooks and MCP entries, plus session records for the "
            "default or --agent all selection, or when a config directory option is given, "
            "unless --keep-data. The "
            f"<name>{owned.BACKUP_SUFFIX} copies install kept before its first "
            "edit of settings.json, hooks.json or config.toml are left in place; delete "
            "them yourself "
            "once you no longer need them."
        ),
    )
    p_uninstall.add_argument("--keep-data", action="store_true", help="Keep session status data")
    p_uninstall.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Remove one agent integration; default: Claude Code, Codex and every profile",
    )
    _add_dir_options(p_uninstall, "Remove")

    p_monitor = sub.add_parser("monitor", help="Launch TUI dashboard")
    p_monitor.add_argument("--refresh", type=float, default=None,
                           help="Refresh interval in seconds "
                                "(default: [ui] refresh_interval, else 2)")
    p_monitor.add_argument("--notify", choices=["all", "waiting", "none"], default=None,
                           help="Notification level: all, waiting-only, or none "
                                "(default: [ui] notify_level, else all)")
    p_monitor.add_argument("--compact", action=argparse.BooleanOptionalAction, default=None,
                           help="Always use compact rows (default: [ui] compact)")
    p_monitor.add_argument("--theme", default=None, metavar="NAME",
                           help="Textual theme (default: [ui] theme, else textual-dark)")

    p_focus = sub.add_parser("focus", help="Focus terminal tab for a session")
    p_focus.add_argument("query", help="Repo name, session ID, or PID")

    sub.add_parser("status", help="Print fleet status (no TUI)")

    handlers = dict(BUILTIN_COMMANDS)
    modules = commands.discover() if modules is None else modules
    for module in modules:
        name = commands.command_name(module)
        if name in handlers:
            raise commands.CommandCollisionError(
                f"command module {module.__name__} defines {name!r}, "
                "which is already a tongs-agents command"
            )
        command_parser = sub.add_parser(name, help=getattr(module, "HELP", None))
        module.add_arguments(command_parser)
        handlers[name] = module.run
    return parser, handlers


def main(argv=None):
    parser, handlers = build_parser()
    args = parse_args(parser, sys.argv[1:] if argv is None else argv)
    if args.command is None:
        args = bare_args(parser)

    try:
        status = handlers[args.command](args)
    except ConfigError as error:
        print(f"tongs-agents: {error}", file=sys.stderr)
        sys.exit(1)
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
