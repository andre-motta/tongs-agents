"""``tongs-agents doctor``: report what would leave agent sessions unmonitored.

Doctor only reads. It runs no harness CLI and never writes a file. Each
finding is a ``Check`` with a level; the exit status is 0 when nothing is
worse than ``info``, 1 for a warning and 2 for an error.

``doctor --fix NAME`` is the one exception: it runs only the named fix, the
``doctor_fix(args)`` of the command module called NAME (``--fix zellij``
installs the pinned Zellij build), and returns that fix's exit status. A
module that offers a fix names it in its checks' remediation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from tongs_agents import cli, commands, legacy, paths, pi_install
from tongs_agents.profiles import ProfileError, load_profiles

NAME = "doctor"
HELP = "Check monitoring coverage and claude-fleet-monitor leftovers"

LEVELS = ("ok", "info", "warn", "error")
EXIT_CODES = {"ok": 0, "info": 0, "warn": 1, "error": 2}
JSON_SCHEMA = 1
PLUGIN_GROUP = "tongs.plugins"
PLUGIN_MODULE = "tongs_agents.tongs_plugin"
ENV_HOMES = (("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME"))
_MAX_TOML_BYTES = 1024 * 1024
_PI_MINIMUM = ".".join(str(part) for part in pi_install.MINIMUM_PI_VERSION)


@dataclass(frozen=True)
class Check:
    """One doctor finding."""

    name: str
    level: str
    message: str
    remediation: str = ""
    details: tuple[str, ...] = ()
    leftover: bool = False

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"unknown check level {self.level!r}")

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "level": self.level,
            "message": self.message,
            "remediation": self.remediation,
            "details": list(self.details),
        }


def _config_dir_option(value: str):
    kind, separator, directory = value.partition("=")
    if not separator or kind not in cli.KINDS or not directory:
        raise argparse.ArgumentTypeError(
            f"expected KIND=DIR with KIND one of {', '.join(cli.KINDS)}"
        )
    path = Path(directory).expanduser()
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"{directory}: must be an absolute path")
    return kind, path


def add_arguments(parser):
    parser.add_argument("--json", action="store_true", help="Print the checks as JSON")
    parser.add_argument(
        "--config-dir", dest="config_dirs", action="append", default=[],
        type=_config_dir_option, metavar="KIND=DIR",
        help="Also check KIND (claude, codex or pi) monitoring in DIR (repeatable)",
    )
    parser.add_argument(
        "--fix", dest="fixes", action="append", default=[], metavar="NAME",
        help="Run the named fix instead of the checks (repeatable); "
             "'zellij' installs the pinned Zellij build",
    )


def check_hook_command() -> list[Check]:
    location = shutil.which(cli.HOOK_COMMAND)
    if location:
        return [Check("hook-command", "ok", f"{cli.HOOK_COMMAND} resolves to {location}")]
    return [Check(
        "hook-command", "error",
        f"{cli.HOOK_COMMAND} is not on PATH, so new installs cannot point hooks at it",
        "Reinstall tongs-agents, or add its environment's bin directory to PATH, "
        "then run 'tongs-agents install'",
    )]


def check_sessions_dir() -> list[Check]:
    sessions = paths.sessions_dir()
    state = paths.state_dir()
    for directory in (state, sessions):
        if directory.is_symlink():
            return [Check(
                "sessions-dir", "error",
                f"{directory} is a symlink; tongs-agents refuses to write through it",
                f"Replace the symlink with a real directory: {directory}",
            )]
    if not os.path.lexists(sessions):
        return [Check(
            "sessions-dir", "info",
            f"Session directory {sessions} does not exist yet; install or the first hook creates it",
        )]
    if not sessions.is_dir():
        return [Check(
            "sessions-dir", "error", f"{sessions} is not a directory",
            f"Move it aside so tongs-agents can create the directory: {sessions}",
        )]
    if not os.access(sessions, os.W_OK | os.X_OK):
        return [Check(
            "sessions-dir", "error",
            f"Session directory {sessions} is not writable, so hooks cannot record sessions",
            f"Make it writable by you: chmod u+rwx {sessions}",
        )]
    if not paths.is_private_dir(sessions):
        return [Check(
            "sessions-dir", "warn",
            f"Session directory {sessions} is readable by other users",
            f"chmod 700 {sessions}",
        )]
    return [Check("sessions-dir", "ok", f"Session directory {sessions} is writable")]


def check_legacy_sessions_dir() -> list[Check]:
    legacy_dir = legacy.legacy_sessions_dir()
    if not os.path.lexists(legacy_dir):
        return []
    return [Check(
        "legacy-sessions-dir", "warn",
        f"Unused claude-fleet-monitor session directory: {legacy_dir}",
        cli.remove_directory_command(legacy_dir),
        ("tongs-agents never reads or deletes it. Its records are stale; remove it with:",),
    )]


def _plugin_entry_points():
    try:
        return list(metadata.entry_points(group=PLUGIN_GROUP))
    except Exception:  # noqa: BLE001 - broken metadata must not crash doctor
        return []


def check_coinstall() -> list[Check]:
    checks = []
    installed = cli.legacy_installed()
    if installed:
        checks.append(Check(
            "legacy-installed", "error",
            "claude-fleet-monitor is still installed and keeps its own hooks and plugin",
            f"Uninstall it with the tool that installed it, for example: "
            f"pip uninstall {legacy.LEGACY_DISTRIBUTION}",
            tuple(installed),
        ))
    entries = _plugin_entry_points()
    old = [
        entry for entry in entries
        if entry.name == legacy.LEGACY_TONGS_PLUGIN
        or entry.value.startswith(f"{legacy.LEGACY_PACKAGE}.")
        or entry.value.startswith(f"{legacy.LEGACY_PACKAGE}:")
    ]
    ours = [entry for entry in entries if entry.value.split(":", 1)[0] == PLUGIN_MODULE]
    if old or len(ours) > 1:
        checks.append(Check(
            "duplicate-plugin", "error",
            f"tongs would load more than one agents plugin from {PLUGIN_GROUP}",
            f"Uninstall the old distribution, for example: pip uninstall {legacy.LEGACY_DISTRIBUTION}",
            tuple(f"{entry.name} = {entry.value}" for entry in [*old, *ours]),
            leftover=bool(old),
        ))
    return checks


def _read_toml(path: Path):
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_TOML_BYTES:
            return None
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, RecursionError):
        return None


def _profile_sources(target) -> str:
    profiles = [source[len("profile "):] for source in target.sources if source.startswith("profile ")]
    return f" (profile {', '.join(profiles)})" if profiles else ""


def _hook_checks(target, expected: bool) -> list[Check]:
    path = target.config_file
    label = f"{target.harness} hooks"
    try:
        config = cli.load_json(path)
    except cli.ConfigError as error:
        return [Check(
            "hooks", "error", f"Cannot check {error}", f"Fix or move aside {path}",
        )]
    report = cli.hook_report(config, target.harness)
    checks = []
    fix = target.install_command()
    sources = _profile_sources(target)
    if not report["missing"]:
        checks.append(Check("hooks", "ok", f"{label} installed in {path}{sources}"))
    elif report["present"] or expected:
        checks.append(Check(
            "hooks", "error",
            f"{path} is missing tongs-agents hooks for {', '.join(report['missing'])}{sources}; "
            "those sessions run unmonitored",
            fix,
        ))
    else:
        checks.append(Check(
            "hooks", "warn", f"tongs-agents is not installed in {path}", fix,
        ))
    if report["stale"]:
        checks.append(Check(
            "hooks", "error",
            f"{path} runs a tongs-agents-hook that no longer exists",
            fix, tuple(report["stale"]),
        ))
    if report["legacy"]:
        checks.append(Check(
            "legacy-hooks", "warn",
            f"{path} has {report['legacy']} claude-fleet-monitor hook command(s)",
            fix,
        ))
    if target.harness == "claude":
        stale_mcp = cli.owned_settings_mcp(config)
        if stale_mcp:
            checks.append(Check(
                "legacy-mcp", "warn",
                f"{path} has unused MCP entries: {', '.join(stale_mcp)}",
                fix,
            ))
        checks += _claude_mcp_checks(target, fix)
    else:
        checks += _codex_mcp_checks(target, fix)
        if report["present"]:
            checks += _codex_trust_checks(target)
    return checks


def _mcp_entry_checks(where, servers, fix) -> list[Check]:
    checks = []
    if cli._is_owned_mcp(servers.get(legacy.LEGACY_MCP_NAME)):
        checks.append(Check(
            "legacy-mcp", "warn",
            f"{where} registers the old MCP server '{legacy.LEGACY_MCP_NAME}'",
            fix,
        ))
    current = servers.get(cli.MCP_NAME)
    if current is not None and not cli._is_owned_mcp(current):
        checks.append(Check(
            "foreign-mcp", "warn",
            f"{where} has an MCP server named '{cli.MCP_NAME}' that tongs-agents did not "
            "install; install leaves it alone",
            f"Rename or remove that entry, then run '{fix}'",
        ))
    return checks


def _claude_mcp_checks(target, fix) -> list[Check]:
    return _mcp_entry_checks(
        target.claude_user_config, cli._claude_user_servers(target.claude_user_config), fix,
    )


def _codex_mcp_checks(target, fix) -> list[Check]:
    path = target.codex_home / "config.toml"
    data = _read_toml(path)
    servers = data.get("mcp_servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return _mcp_entry_checks(path, servers, fix)


def _codex_trust_checks(target) -> list[Check]:
    hooks_file = target.config_file
    config = _read_toml(target.codex_home / "config.toml")
    hooks = config.get("hooks") if isinstance(config, dict) else None
    state = hooks.get("state") if isinstance(hooks, dict) else None
    prefixes = {f"{hooks_file}:", f"{os.path.realpath(hooks_file)}:"}
    trusted = [
        key for key, value in (state or {}).items()
        if isinstance(value, dict) and value.get("trusted_hash")
        and any(key.startswith(prefix) for prefix in prefixes)
    ] if isinstance(state, dict) else []
    if trusted:
        return [Check(
            "codex-trust", "info",
            f"Codex records trust for {len(trusted)} hook(s) in {hooks_file}; it asks again "
            "when a hook command changes",
            "If Codex reports untrusted or modified hooks, review them with /hooks",
        )]
    return [Check(
        "codex-trust", "warn",
        f"Codex has no trust recorded for the hooks in {hooks_file}; it runs untrusted hooks "
        "only after review",
        "Start Codex with this home and review the hooks with /hooks",
    )]


def _pi_checks(target, expected: bool) -> list[Check]:
    checks = []
    config = target.config_file
    old = target.pi_dir / legacy.LEGACY_PI_CONFIG_NAME
    fix = target.install_command()
    if os.path.lexists(old):
        checks.append(Check(
            "legacy-pi", "warn",
            f"{old} manages the claude-fleet-monitor Pi extension",
            fix,
        ))
    if os.path.lexists(config):
        checks.append(Check(
            "hooks", "ok", f"pi extension installed in {target.pi_dir}{_profile_sources(target)}",
        ))
    elif expected:
        ready = shutil.which("pi") is not None and shutil.which(cli.HOOK_COMMAND) is not None
        if ready:
            level, remediation = "error", f"{fix} (needs Pi {_PI_MINIMUM} or newer)"
        else:
            # install skips a profile-only Pi home with a warning when Pi is missing
            level = "warn" if cli._profile_only(target) else "error"
            remediation = (
                f"Install Pi {_PI_MINIMUM} or newer and {cli.HOOK_COMMAND}, then run "
                f"{fix} (or remove the Pi profile)"
            )
        checks.append(Check(
            "hooks", level,
            f"The Pi extension is not installed in {target.pi_dir}{_profile_sources(target)}; "
            "those sessions run unmonitored",
            remediation,
        ))
    return checks


def _targets(args, profiles, installs):
    """Every config home to check, each with whether monitoring is expected there."""
    targets = []
    defaults = [cli.Target("claude", None, ("default",)), cli.Target("codex", None, ("default",))]
    default_pi = cli.Target("pi", None, ("default",))
    if os.path.lexists(default_pi.config_file) or os.path.lexists(
        default_pi.pi_dir / legacy.LEGACY_PI_CONFIG_NAME
    ):
        defaults.append(default_pi)
    targets += defaults
    targets += cli.recorded_targets(installs)
    targets += cli.profile_targets(profiles)
    for kind, directory in getattr(args, "config_dirs", None) or []:
        targets.append(cli._make_target(kind, directory, "option"))
    merged = cli._merge_targets(targets)
    return [
        (target, any(source != "default" for source in target.sources))
        for target in merged
    ]


def check_targets(args) -> list[Check]:
    checks = []
    try:
        installs = cli.load_installs()
    except cli.ConfigError as error:
        checks.append(Check(
            "installs", "error", str(error), f"Move {cli.installs_path()} aside and reinstall",
        ))
        installs = {kind: [] for kind in cli.KINDS}
    try:
        profiles = load_profiles()
    except ProfileError:
        profiles = {}
    for target, expected in _targets(args, profiles, installs):
        if target.harness == "pi":
            checks += _pi_checks(target, expected)
        else:
            checks += _hook_checks(target, expected)
    return checks


def check_environment() -> list[Check]:
    checks = []
    for harness, variable in ENV_HOMES:
        value = os.environ.get(variable)
        if not value:
            continue
        target = cli._make_target(harness, value, "environment")
        try:
            report = cli.hook_report(cli.load_json(target.config_file), harness)
        except cli.ConfigError:
            continue
        if report["missing"]:
            checks.append(Check(
                "environment", "warn",
                f"{variable} is set to {value}, but tongs-agents is not installed there; "
                f"{harness} sessions started with it run unmonitored "
                "(install never follows this variable)",
                target.install_command() if target.directory is not None
                else f"tongs-agents install --agent {harness}",
            ))
        else:
            checks.append(Check(
                "environment", "ok", f"{variable} points at a monitored home ({value})",
            ))
    return checks


def module_checks() -> list[Check]:
    checks = []
    for module in commands.discover():
        provider = getattr(module, "doctor_checks", None)
        if provider is None:
            continue
        try:
            found = list(provider())
        except Exception as error:  # noqa: BLE001 - one module must not hide the rest
            found = [Check(
                commands.command_name(module), "error",
                f"{module.__name__}.doctor_checks() failed: {type(error).__name__}: {error}",
            )]
        checks += found
    return checks


def collect(args) -> list[Check]:
    return [
        *check_hook_command(),
        *check_sessions_dir(),
        *check_legacy_sessions_dir(),
        *check_coinstall(),
        *check_targets(args),
        *check_environment(),
        *module_checks(),
    ]


def exit_code(checks) -> int:
    return max((EXIT_CODES[check.level] for check in checks), default=0)


def _is_leftover(check) -> bool:
    return check.leftover or check.name.startswith("legacy")


def _print_text(checks):
    print(f"Session directory: {paths.sessions_dir()}")
    for check in checks:
        if check.level == "ok":
            continue
        print()
        print(f"[{check.level.upper()}] {check.message}")
        for detail in check.details:
            print(f"  {detail}")
        if check.remediation:
            print(f"  Fix: {check.remediation}")
    print()
    if not any(_is_leftover(check) for check in checks if check.level != "ok"):
        print("No claude-fleet-monitor leftovers found.")
    errors = sum(check.level == "error" for check in checks)
    warnings = sum(check.level == "warn" for check in checks)
    if errors or warnings:
        print(f"{errors} error(s), {warnings} warning(s).")
    else:
        print("Everything checked is monitored.")


def available_fixes(modules=None) -> dict:
    """``{name: doctor_fix}`` for every command module that offers a fix."""
    modules = commands.discover() if modules is None else modules
    return {
        commands.command_name(module): module.doctor_fix
        for module in modules
        if callable(getattr(module, "doctor_fix", None))
    }


def run_fixes(args, names) -> int:
    fixes = available_fixes()
    unknown = [name for name in names if name not in fixes]
    if unknown:
        print(
            f"tongs-agents doctor: no fix named {unknown[0]!r:.80}; available: "
            f"{', '.join(sorted(fixes)) or 'none'}",
            file=sys.stderr,
        )
        return EXIT_CODES["error"]
    if getattr(args, "json", False):
        print("tongs-agents doctor: --fix cannot be combined with --json", file=sys.stderr)
        return EXIT_CODES["error"]
    code = 0
    for name in dict.fromkeys(names):
        code = max(code, fixes[name](args) or 0)
    return code


def run(args) -> int:
    fixes = getattr(args, "fixes", None) or []
    if fixes:
        return run_fixes(args, fixes)
    checks = collect(args)
    code = exit_code(checks)
    if getattr(args, "json", False):
        print(json.dumps({
            "schema_version": JSON_SCHEMA,
            "sessions_dir": str(paths.sessions_dir()),
            "exit_code": code,
            "checks": [check.to_json() for check in checks],
        }, indent=2))
    else:
        _print_text(checks)
    return code
