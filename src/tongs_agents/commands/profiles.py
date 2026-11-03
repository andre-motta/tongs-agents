"""``tongs-agents profiles``: list or show the configured agent profiles.

Read-only. Env values are never printed, only their names, because profiles
may carry tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from tongs_agents import cli
from tongs_agents.commands.doctor import Check
from tongs_agents.profiles import ProfileError, load_profiles, profiles_path

NAME = "profiles"
HELP = "List or show agent profiles (read-only)"


def add_arguments(parser):
    actions = parser.add_subparsers(dest="profiles_action")
    list_parser = actions.add_parser("list", help="List profiles (default)")
    list_parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Print JSON"
    )
    show_parser = actions.add_parser("show", help="Show one profile")
    show_parser.add_argument("name")
    show_parser.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="Print JSON"
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _home(profile) -> str:
    harness, directory, problem = cli.profile_home(profile)
    if problem is not None:
        return f"unmonitored: {problem}"
    if directory is not None:
        return str(directory)
    target = cli.Target(harness)
    default = target.pi_dir if harness == "pi" else target.config_file.parent
    return f"{default} (default)"


def describe(profile) -> dict:
    return {
        "name": profile.name,
        "harness": profile.harness,
        "command": list(profile.argv),
        "model_args": list(profile.model_args),
        "effort_args": list(profile.effort_args),
        "env": sorted(profile.env),
        "config_dir": str(profile.config_dir) if profile.config_dir else None,
        "config_home": _home(profile),
        "cwd": str(profile.cwd) if profile.cwd else None,
        "mcp_control": profile.mcp_control,
    }


def _print_list(profiles):
    if not profiles:
        print(f"No profiles in {profiles_path()}.")
        return
    rows = [
        (profile.name, profile.harness, _home(profile), cli._command_string(list(profile.argv)))
        for profile in profiles.values()
    ]
    widths = [max(len(row[index]) for row in [("NAME", "HARNESS", "HOME", ""), *rows])
              for index in range(3)]
    header = ("NAME", "HARNESS", "HOME", "COMMAND")
    for row in [header, *rows]:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)) + "  " + row[3])


def _print_profile(info):
    for key, value in info.items():
        if isinstance(value, list):
            value = cli._command_string(value) if key != "env" else ", ".join(value)
        print(f"{key}: {'' if value is None else value}")


def run(args) -> int:
    try:
        profiles = load_profiles()
    except ProfileError as error:
        print(f"tongs-agents: {error}", file=sys.stderr)
        return 1
    as_json = getattr(args, "json", False)
    if getattr(args, "profiles_action", None) == "show":
        profile = profiles.get(args.name)
        if profile is None:
            print(f"tongs-agents: unknown profile {args.name!r}", file=sys.stderr)
            return 1
        info = describe(profile)
        if as_json:
            print(json.dumps(info, indent=2))
        else:
            _print_profile(info)
        return 0
    if as_json:
        print(json.dumps([describe(profile) for profile in profiles.values()], indent=2))
    else:
        _print_list(profiles)
    return 0


def doctor_checks():
    """Profiles that would run unmonitored, fail to start or cannot be checked."""
    try:
        profiles = load_profiles()
    except ProfileError as error:
        return [Check(
            "profiles", "error", str(error), f"Fix {profiles_path()}",
        )]
    checks = []
    for profile in profiles.values():
        if profile.cwd is not None and not profile.cwd.is_dir():
            checks.append(Check(
                "profiles", "warn",
                f"Profile {profile.name} has cwd {profile.cwd}, which is not a directory; "
                "its pane fails to start",
                f"Create that directory or change cwd in [profiles.{profile.name}]",
            ))
        harness, directory, problem = cli.profile_home(profile)
        if harness is None:
            checks.append(Check(
                "profiles", "info",
                f"Profile {profile.name} (harness other) has no hooks: tongs-agents installs "
                "them only for claude, codex and pi, so its status comes only from "
                "tongs-agents notify",
                "Report status with tongs-agents notify, or set harness in "
                f"[profiles.{profile.name}] for a wrapper of claude, codex or pi",
            ))
            continue
        if problem is not None:
            checks.append(Check(
                "profiles", "warn",
                f"Profile {profile.name} runs unmonitored: {problem}",
                f"Set config_dir in [profiles.{profile.name}] to an absolute path",
            ))
            continue
        program = os.path.basename(profile.argv[0])
        if harness == "claude" and program != "claude":
            where = "its default home" if directory is None else str(directory)
            checks.append(Check(
                "profiles", "warn",
                f"Profile {profile.name} runs {profile.argv[0]}, not claude: its sessions "
                f"are found only if the wrapper execs claude, and are monitored only if "
                f"they use {where}",
                f"Make the wrapper 'exec claude', and set config_dir in "
                f"[profiles.{profile.name}] when it uses another Claude config dir",
            ))
        elif program != harness and directory is None and harness != "pi":
            checks.append(Check(
                "profiles", "info",
                f"Profile {profile.name} runs the wrapper {profile.argv[0]}; tongs-agents "
                f"assumes it uses the default {harness} home",
                f"Set config_dir in [profiles.{profile.name}] if the wrapper uses another one",
            ))
    return checks
