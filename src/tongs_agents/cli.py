"""CLI for installing and using the fleet monitor."""

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
from pathlib import Path

from tongs_agents import legacy
from tongs_agents.pi_install import PI_CONFIG_NAME, install_pi, pi_config_dir, uninstall_pi


CLAUDE_DIR = Path.home() / ".claude"
CODEX_DIR = Path.home() / ".codex"
FLEET_DIR = Path(os.environ.get("FLEET_DIR", CLAUDE_DIR / "fleet"))
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
CODEX_HOOKS_FILE = CODEX_DIR / "hooks.json"
HOOK_COMMAND = "tongs-agents-hook"
MCP_MODULE = "tongs_agents.mcp_server"
FLEET_FILE_PATTERNS = (
    "*.json",
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


def _is_owned_hook(hook):
    if not isinstance(hook, dict) or not isinstance(hook.get("command"), str):
        return False
    argv = _command_argv(hook["command"])
    if not argv or not argv[0]:
        return False
    if _executable_name(argv[0]) in (HOOK_COMMAND, *legacy.LEGACY_HOOK_COMMANDS):
        return True
    return _is_legacy_script(argv[0])


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
    )


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


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ConfigError(f"{path}: refusing to modify a symlinked file")
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


def _install_claude_mcp(settings, python_path):
    servers = settings.setdefault("mcpServers", {})
    existing = servers.get("fleet")
    if existing is not None and not _is_owned_mcp(existing):
        print("  Kept existing Claude MCP server named fleet")
        return
    servers["fleet"] = {
        "command": python_path,
        "args": ["-m", MCP_MODULE],
        "env": {},
    }


def _remove_claude_mcp(settings):
    servers = settings.get("mcpServers")
    if isinstance(servers, dict) and _is_owned_mcp(servers.get("fleet")):
        del servers["fleet"]


def _get_codex_mcp(codex_cmd):
    try:
        result = subprocess.run(
            [codex_cmd, "mcp", "get", "fleet", "--json"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def _run_codex_mcp(codex_cmd, arguments, action):
    try:
        result = subprocess.run(
            [codex_cmd, "mcp", *arguments],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"  Could not {action} Codex MCP server: {error}")
        return False
    if result.returncode != 0:
        print(f"  Could not {action} Codex MCP server: {result.stderr.strip()}")
        return False
    return True


def _install_codex_mcp(codex_cmd):
    existing = _get_codex_mcp(codex_cmd)
    if existing is not None:
        if not _is_owned_mcp(existing):
            print("  Kept existing Codex MCP server named fleet")
            return
        if _is_current_mcp(existing):
            return
        if not _run_codex_mcp(codex_cmd, ["remove", "fleet"], "replace"):
            return
    _run_codex_mcp(
        codex_cmd,
        ["add", "fleet", "--", sys.executable, "-m", MCP_MODULE],
        "add",
    )


def _uninstall_codex_mcp(codex_cmd):
    existing = _get_codex_mcp(codex_cmd)
    if existing is None or not _is_owned_mcp(existing):
        return
    _run_codex_mcp(codex_cmd, ["remove", "fleet"], "remove")


def _remove_fleet_data():
    if FLEET_DIR.is_symlink():
        print(f"  Kept {FLEET_DIR} because it is a symlink")
        return
    if not FLEET_DIR.is_dir():
        return
    if os.path.lexists(pi_config_dir() / PI_CONFIG_NAME):
        print(f"  Kept {FLEET_DIR} because Pi monitoring is still installed")
        return
    for entry in FLEET_DIR.iterdir():
        if not any(fnmatch.fnmatch(entry.name, pattern) for pattern in FLEET_FILE_PATTERNS):
            continue
        try:
            if stat.S_ISREG(entry.lstat().st_mode):
                entry.unlink()
        except OSError:
            pass
    try:
        FLEET_DIR.rmdir()
    except OSError:
        print(f"  Removed fleet records; kept {FLEET_DIR} because it holds other files")
        return
    print(f"  Removed {FLEET_DIR}")


def cmd_install(args):
    agent = getattr(args, "agent", None)
    selected = {"claude", "codex"} if agent is None else {agent}
    if "all" in selected:
        selected = {"claude", "codex", "pi"}
    hook_cmd = shutil.which("tongs-agents-hook") or "tongs-agents-hook"
    python_path = sys.executable

    if "claude" in selected:
        settings = load_settings()
    if "codex" in selected:
        codex_hooks = load_json(CODEX_HOOKS_FILE)

    if "claude" in selected or "codex" in selected:
        FLEET_DIR.mkdir(parents=True, exist_ok=True)
    if "claude" in selected:
        _install_hooks(settings, CLAUDE_HOOK_EVENTS, hook_cmd, "claude")
        _install_claude_mcp(settings, python_path)
        save_settings(settings)
        print(f"  Updated {SETTINGS_FILE}")
    if "codex" in selected:
        _install_hooks(codex_hooks, CODEX_HOOK_EVENTS, hook_cmd, "codex")
        save_json(CODEX_HOOKS_FILE, codex_hooks)
        print(f"  Updated {CODEX_HOOKS_FILE}")

        codex_cmd = shutil.which("codex")
        if codex_cmd:
            _install_codex_mcp(codex_cmd)
        else:
            print("  Codex CLI not found; skipped Codex MCP registration")
    if "pi" in selected:
        pi_cmd = shutil.which("pi")
        if pi_cmd is None or not Path(hook_cmd).is_absolute():
            print("  Pi and tongs-agents-hook must be installed before enabling Pi")
            raise SystemExit(1)
        if not install_pi(pi_cmd, hook_cmd):
            raise SystemExit(1)

    print()
    if agent is None:
        print("Tongs Agents installed for Claude Code and Codex!")
    else:
        names = ", ".join(name.title() for name in sorted(selected))
        print(f"Tongs Agents installed for {names}!")
    print()
    print("Usage:")
    print("  tongs-agents monitor          # TUI dashboard")
    print("  tongs-agents focus <repo>     # Focus terminal tab")
    if "claude" in selected or "codex" in selected:
        print("  (MCP tools available in Claude Code and Codex sessions)")
    print()
    if agent is None:
        print("Restart agent sessions to activate hooks. In Codex, review them with /hooks.")
    else:
        print("Restart selected agent sessions to activate monitoring.")
        if "codex" in selected:
            print("In Codex, review installed hooks with /hooks.")


def cmd_uninstall(args):
    agent = getattr(args, "agent", None)
    selected = {"claude", "codex"} if agent is None else {agent}
    if "all" in selected:
        selected = {"claude", "codex", "pi"}
    if "claude" in selected:
        settings = load_settings()
    if "codex" in selected:
        codex_hooks = load_json(CODEX_HOOKS_FILE)

    # Clean up legacy bash scripts and MCP dir from older versions
    if "claude" in selected:
        bin_dir = CLAUDE_DIR / "bin"
        mcp_dir = CLAUDE_DIR / "fleet-mcp"
        for name in ("fleet-hook.sh", "fleet-monitor.sh", "fleet-focus.sh"):
            f = bin_dir / name
            if f.exists():
                f.unlink()
                print(f"  Removed legacy {f}")
        if mcp_dir.exists():
            shutil.rmtree(mcp_dir)
            print(f"  Removed legacy {mcp_dir}")
        _remove_hooks(settings)
        _remove_claude_mcp(settings)
        save_settings(settings)
        print(f"  Updated {SETTINGS_FILE}")
    if "codex" in selected:
        _remove_hooks(codex_hooks)
        save_json(CODEX_HOOKS_FILE, codex_hooks)
        print(f"  Updated {CODEX_HOOKS_FILE}")
        codex_cmd = shutil.which("codex")
        if codex_cmd:
            _uninstall_codex_mcp(codex_cmd)
    if "pi" in selected:
        pi_cmd = shutil.which("pi")
        if not uninstall_pi(pi_cmd):
            raise SystemExit(1)

    if (agent is None or selected == {"claude", "codex", "pi"}) and not args.keep_data:
        _remove_fleet_data()

    print()
    if agent is None:
        print("Tongs Agents uninstalled from Claude Code and Codex.")
    else:
        names = ", ".join(name.title() for name in sorted(selected))
        print(f"Tongs Agents uninstalled from {names}.")
    if "pi" in selected:
        print("Restart running Pi sessions to finish removing monitoring.")


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


def main():
    parser = argparse.ArgumentParser(
        prog="tongs-agents",
        description="Agent workspace for tongs: monitor and focus Claude Code, Codex and Pi sessions",
    )
    sub = parser.add_subparsers(dest="command")

    p_install = sub.add_parser("install", help="Install agent monitoring")
    p_install.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Install one agent integration; default: Claude Code and Codex",
    )

    p_uninstall = sub.add_parser("uninstall", help="Remove agent monitoring")
    p_uninstall.add_argument("--keep-data", action="store_true", help="Keep fleet status data")
    p_uninstall.add_argument(
        "--agent", choices=["claude", "codex", "pi", "all"],
        help="Remove one agent integration; default: Claude Code and Codex",
    )

    p_monitor = sub.add_parser("monitor", help="Launch TUI dashboard")
    p_monitor.add_argument("--refresh", type=int, default=2, help="Refresh interval in seconds")
    p_monitor.add_argument("--notify", choices=["all", "waiting", "none"], default="all",
                           help="Notification level: all, waiting-only, or none")

    p_focus = sub.add_parser("focus", help="Focus terminal tab for a session")
    p_focus.add_argument("query", help="Repo name, session ID, or PID")

    sub.add_parser("status", help="Print fleet status (no TUI)")

    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        sys.exit(1)

    cmds = {
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "monitor": cmd_monitor,
        "focus": cmd_focus,
        "status": cmd_status,
    }
    try:
        cmds[args.command](args)
    except ConfigError as error:
        print(f"tongs-agents: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
