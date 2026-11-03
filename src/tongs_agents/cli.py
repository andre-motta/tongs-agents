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
import tempfile
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from tongs_agents import commands, legacy, paths
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


class ConfigError(Exception):
    """A harness config file that the installer must not modify."""


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
    if path.is_symlink():
        raise ConfigError(f"{path}: refusing to modify a symlinked file")
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"{path}: malformed JSON ({error.msg} at line {error.lineno} column {error.colno})"
        ) from None
    except (OSError, UnicodeError, RecursionError) as error:
        raise ConfigError(f"{path}: cannot read ({type(error).__name__})") from None
    _validate_config(path, data)
    return data


def save_json(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ConfigError(f"{path}: refusing to modify a symlinked file")
    if mode is None:
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = 0o600
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_settings():
    return load_json(SETTINGS_FILE)


def save_settings(settings):
    save_json(SETTINGS_FILE, settings)


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
    targets = []
    for profile in profiles.values():
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


def _codex_environment(directory=None):
    environment = os.environ.copy()
    environment.pop("CODEX_HOME", None)
    if directory is not None:
        environment["CODEX_HOME"] = str(directory)
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
            return
        if _is_current_mcp(existing):
            return
        if not _run_claude_mcp(
            claude_cmd, ["remove", "--scope", "user", MCP_NAME], "replace", directory
        ):
            return
    added = _run_claude_mcp(
        claude_cmd,
        ["add", "--scope", "user", MCP_NAME, "--", sys.executable, "-m", MCP_MODULE],
        "add",
        directory,
    )
    if not added and existing is not None:
        _restore_claude_mcp(claude_cmd, existing, directory)


def _uninstall_claude_mcp(claude_cmd, directory=None):
    servers = _claude_servers_for(directory)
    for name in MCP_NAMES:
        if _is_owned_mcp(servers.get(name)):
            _run_claude_mcp(
                claude_cmd, ["remove", "--scope", "user", name], "remove", directory
            )


def _get_codex_mcp(codex_cmd, name, directory=None):
    try:
        result = subprocess.run(
            [codex_cmd, "mcp", "get", name, "--json"],
            capture_output=True, text=True, timeout=10,
            env=_codex_environment(directory),
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _run_codex_mcp(codex_cmd, arguments, action, directory=None):
    return _run_mcp_cli(
        "Codex", [codex_cmd, "mcp", *arguments], action, _codex_environment(directory)
    )


def _remove_legacy_codex_mcp(codex_cmd, directory=None):
    existing = _get_codex_mcp(codex_cmd, legacy.LEGACY_MCP_NAME, directory)
    if existing is not None and _is_owned_mcp(existing):
        _run_codex_mcp(codex_cmd, ["remove", legacy.LEGACY_MCP_NAME], "remove", directory)


def _codex_add_arguments(command, args, env=None):
    flags = [part for key, value in (env or {}).items() for part in ("--env", f"{key}={value}")]
    return ["add", MCP_NAME, *flags, "--", command, *args]


def _restore_codex_mcp(codex_cmd, previous, directory=None):
    """Re-add a replaced entry after a failed add, and say what to rerun."""
    rerun = Target("codex", directory).install_command()
    transport = previous.get("transport", previous)
    env = transport.get("env")
    if not isinstance(env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in env.items()
    ):
        env = None
    restored = _run_codex_mcp(
        codex_cmd,
        _codex_add_arguments(transport["command"], transport["args"], env),
        "restore",
        directory,
    )
    if restored:
        print(f"  Restored the previous Codex MCP server {MCP_NAME}; "
              f"run '{rerun}' to retry the update")
    else:
        print(f"  The previous Codex MCP server {MCP_NAME} was removed and could not be "
              f"restored; run '{rerun}' to register it again")


def _install_codex_mcp(codex_cmd, directory=None):
    _remove_legacy_codex_mcp(codex_cmd, directory)
    existing = _get_codex_mcp(codex_cmd, MCP_NAME, directory)
    replaced = None
    if existing is not None:
        if not _is_owned_mcp(existing):
            print(f"  Kept existing Codex MCP server named {MCP_NAME}")
            return
        if _is_current_mcp(existing):
            return
        if not _run_codex_mcp(codex_cmd, ["remove", MCP_NAME], "replace", directory):
            return
        replaced = existing
    added = _run_codex_mcp(
        codex_cmd,
        _codex_add_arguments(sys.executable, ["-m", MCP_MODULE]),
        "add",
        directory,
    )
    if not added and replaced is not None:
        _restore_codex_mcp(codex_cmd, replaced, directory)


def _uninstall_codex_mcp(codex_cmd, directory=None):
    _remove_legacy_codex_mcp(codex_cmd, directory)
    existing = _get_codex_mcp(codex_cmd, MCP_NAME, directory)
    if existing is not None and _is_owned_mcp(existing):
        _run_codex_mcp(codex_cmd, ["remove", MCP_NAME], "remove", directory)


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


def cmd_install(args):
    agent = getattr(args, "agent", None)
    targets, explicit = _plan(args)
    installs = load_installs()
    hook_cmd = shutil.which("tongs-agents-hook") or "tongs-agents-hook"
    hook_targets = [target for target in targets if target.harness != "pi"]
    pi_targets = [target for target in targets if target.harness == "pi"]

    for target in hook_targets:
        load_json(target.config_file)

    if hook_targets:
        try:
            paths.sessions_dir(create=True)
        except OSError as error:
            raise ConfigError(f"cannot use the session directory: {error}") from None
    commands_found = {name: shutil.which(name) for name in ("claude", "codex")}
    skipped = set()
    for target in hook_targets:
        config = load_json(target.config_file)
        _install_hooks(config, _hook_events(target.harness), hook_cmd, target.harness)
        if target.harness == "claude":
            _remove_settings_mcp(config)
        save_json(target.config_file, config)
        _record(installs, target, True)
        print(f"  Updated {target.config_file}{_describe_sources(target)}")
        command = commands_found[target.harness]
        if target.harness == "claude":
            if command:
                _install_claude_mcp(command, target.directory)
            elif "claude" not in skipped:
                skipped.add("claude")
                print("  Claude Code CLI not found; skipped Claude Code MCP registration")
        else:
            if command:
                _install_codex_mcp(command, target.directory)
            elif "codex" not in skipped:
                skipped.add("codex")
                print("  Codex CLI not found; skipped Codex MCP registration")

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
                else:
                    pi_failed = True
    save_installs(installs)
    if pi_failed:
        raise SystemExit(1)

    harnesses = {target.harness for target in targets}
    print()
    if agent is None and not explicit:
        print("Tongs Agents installed for Claude Code and Codex!")
    else:
        names = ", ".join(name.title() for name in sorted(harnesses))
        print(f"Tongs Agents installed for {names}!")
    print()
    print("Usage:")
    print("  tongs-agents monitor          # TUI dashboard")
    print("  tongs-agents focus <repo>     # Focus terminal tab")
    if hook_targets:
        print("  (MCP tools available in Claude Code and Codex sessions)")
    if os.path.lexists(legacy.legacy_sessions_dir()):
        print()
        print(f"{legacy.legacy_sessions_dir()} is no longer used; "
              "see 'tongs-agents doctor' to remove it.")
    print()
    if agent is None and not explicit:
        print("Restart agent sessions to activate hooks. In Codex, review them with /hooks.")
    else:
        print("Restart selected agent sessions to activate monitoring.")
        if "codex" in harnesses:
            print("In Codex, review installed hooks with /hooks.")


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
    for target in hook_targets:
        if target.directory is None or os.path.lexists(target.config_file):
            load_json(target.config_file)

    commands_found = {name: shutil.which(name) for name in ("claude", "codex")}
    for target in hook_targets:
        if target.directory is None or os.path.lexists(target.config_file):
            config = load_json(target.config_file)
            _remove_hooks(config)
            if target.harness == "claude":
                _remove_settings_mcp(config)
            save_json(target.config_file, config)
            print(f"  Updated {target.config_file}{_describe_sources(target)}")
        _record(installs, target, False)
        command = commands_found[target.harness]
        if command and target.harness == "claude":
            _uninstall_claude_mcp(command, target.directory)
        elif command:
            _uninstall_codex_mcp(command, target.directory)
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
    if agent is None and not explicit:
        print("Tongs Agents uninstalled from Claude Code and Codex.")
    else:
        names = ", ".join(name.title() for name in sorted(harnesses))
        print(f"Tongs Agents uninstalled from {names}.")
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
    from tongs_agents.tui import FleetMonitorApp
    app = FleetMonitorApp(refresh_interval=args.refresh, notify_level=args.notify)
    app.run()


def cmd_focus(args):
    from tongs_agents.focus import focus
    if not focus(args.query):
        sys.exit(1)


def cmd_status(args):
    from tongs_agents.discovery import read_sessions
    sessions = read_sessions()

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


def build_parser(modules=None):
    """The argument parser with built-in verbs and every command module.

    Returns the parser and the handler for each verb. A command module whose
    name collides with a built-in verb or another module raises.
    """
    parser = argparse.ArgumentParser(
        prog="tongs-agents",
        description="Agent workspace for tongs: monitor and focus Claude Code, Codex and Pi sessions",
    )
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser(
        "install", help="Install agent monitoring",
        description=(
            "Install agent monitoring. Without --agent or config dir options this covers "
            "Claude Code, Codex and the config home of every profile. A pi profile is "
            "skipped with a warning, not an error, when pi or tongs-agents-hook is not "
            "installed; --agent pi or --pi-agent-dir still fail in that case."
        ),
    )
    p_install.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Install one agent integration; default: Claude Code, Codex and every profile",
    )
    _add_dir_options(p_install, "Install")

    p_uninstall = sub.add_parser("uninstall", help="Remove agent monitoring")
    p_uninstall.add_argument("--keep-data", action="store_true", help="Keep session status data")
    p_uninstall.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Remove one agent integration; default: Claude Code, Codex and every profile",
    )
    _add_dir_options(p_uninstall, "Remove")

    p_monitor = sub.add_parser("monitor", help="Launch TUI dashboard")
    p_monitor.add_argument("--refresh", type=int, default=2, help="Refresh interval in seconds")
    p_monitor.add_argument("--notify", choices=["all", "waiting", "none"], default="all",
                           help="Notification level: all, waiting-only, or none")

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


def main():
    parser, handlers = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        status = handlers[args.command](args)
    except ConfigError as error:
        print(f"tongs-agents: {error}", file=sys.stderr)
        sys.exit(1)
    if status:
        sys.exit(status)


if __name__ == "__main__":
    main()
