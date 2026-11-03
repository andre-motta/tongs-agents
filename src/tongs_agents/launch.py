"""The in-pane launcher: ``python -m tongs_agents.launch <profile> --launch-id <hex>``.

In order it validates the profile and launch id, claims the launch record
with its own pid, process start token and pane identity, builds the agent's
environment, changes to the launch folder (the record's ``folder``, else the
profile's cwd) and execs the profile argv plus the record's extra ``args``.
Exec keeps the pid, so the record names the agent (or its exec wrapper). The
folder is checked again here (``profiles.folder_problem``): it must still be a
directory, and a sandboxed profile still refuses a home or credentials folder.

A profile with a ``sandbox`` table execs ``openshell`` instead: the sandbox
is created or reused and confirmed Ready first (``sandbox.launcher``), and
the agent argv runs inside it.

Exit status 64 means a bad profile or launch id, 69 a sandbox that cannot be
prepared, 127 that exec failed. Either way one line is printed and, on a TTY,
the pane waits for Enter so the message stays readable.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from tongs_agents import discovery, launches
from tongs_agents.multiplexers.base import PaneRef
from tongs_agents.multiplexers.tmux import env_socket
from tongs_agents.profiles import (
    CONFIG_DIR_ENV_BY_HARNESS,
    Profile,
    ProfileError,
    folder_problem,
    load_profiles,
    uploads_workspace,
)
from tongs_agents.sandbox import launcher as sandbox_launcher
from tongs_agents.terminal_apis._env import is_decimal_id

EXIT_USAGE = 64
EXIT_SANDBOX = 69
EXIT_EXEC_FAILED = 127
LAUNCH_ID_ENV = "TONGS_AGENTS_LAUNCH_ID"
PROFILE_ENV = "TONGS_AGENTS_PROFILE"
CODEX_NO_DAEMON = "--no-daemon"
MAX_PANE_TEXT = 1024


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="python -m tongs_agents.launch",
        description="Claim a launch record and exec a tongs-agents profile.",
    )
    parser.add_argument("profile")
    parser.add_argument("--launch-id", required=True)
    return parser


def _pane_text(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_PANE_TEXT and value.isprintable()


def pane_from_env(env: Mapping[str, str]) -> PaneRef | None:
    """The pane this process runs in, from Zellij or tmux variables only.

    Without ``ZELLIJ`` a tmux pane is ``TMUX_PANE`` on the server socket
    named by ``TMUX`` (the socket path may itself hold commas). Its session
    stays the one the launch record was created for.
    """
    if env.get("ZELLIJ") is not None:
        session = env.get("ZELLIJ_SESSION_NAME")
        pane_id = env.get("ZELLIJ_PANE_ID")
        socket_dir = env.get("ZELLIJ_SOCKET_DIR") or None
        if not (_pane_text(session) and is_decimal_id(pane_id)):
            return None
        if socket_dir is not None and not (
            _pane_text(socket_dir) and os.path.isabs(socket_dir)
        ):
            socket_dir = None
        return PaneRef("zellij", session, pane_id, socket_dir)
    tmux_pane = env.get("TMUX_PANE")
    if (
        isinstance(tmux_pane, str)
        and tmux_pane.startswith("%")
        and is_decimal_id(tmux_pane[1:])
    ):
        socket = env_socket(env)
        if socket is None or not _pane_text(socket):
            # Without its server socket a tmux pane id is ambiguous across
            # servers, so the launch stays unclaimed rather than guessed.
            return None
        return PaneRef("tmux", "", tmux_pane, socket)
    return None


def build_argv(profile: Profile, launch_args: Sequence[str] = ()) -> list[str]:
    """The exec argv: profile command, ``--no-daemon`` for codex, model and
    effort args, then this launch's own arguments."""
    argv = list(profile.argv)
    extra = [*profile.model_args, *profile.effort_args, *launch_args]
    if profile.harness == "codex" and CODEX_NO_DAEMON not in (*argv, *extra):
        argv.append(CODEX_NO_DAEMON)
    return argv + extra


def launch_profile(profile: Profile, folder: str | None) -> Profile:
    """``profile`` with the launch folder as its cwd; raises ProfileError when refused."""
    if folder is not None:
        problem = folder_problem(profile, Path(folder))
        if problem is not None:
            raise ProfileError(f"profile {profile.name}: folder {problem}")
        return dataclasses.replace(profile, cwd=Path(folder))
    if uploads_workspace(profile) and profile.cwd is None:
        raise ProfileError(
            f"profile {profile.name}: no folder to upload into the sandbox; "
            "start it with 'tongs-agents run' and a project folder"
        )
    return profile


def build_env(profile: Profile, launch_id: str, base: Mapping[str, str]) -> dict[str, str]:
    """The agent environment: inherited, profile env, config dir, identity."""
    env = dict(base)
    env.update(profile.env)
    if profile.config_dir is not None:
        env[CONFIG_DIR_ENV_BY_HARNESS[profile.harness]] = str(profile.config_dir)
    env[LAUNCH_ID_ENV] = launch_id
    env[PROFILE_ENV] = profile.name
    if "ZELLIJ" in env:
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
    return env


def _fail(code: int, message: str) -> int:
    interactive = False
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (OSError, ValueError):
        pass
    suffix = " (press Enter to close)" if interactive else ""
    print(f"tongs-agents launch: {message}{suffix}", file=sys.stderr, flush=True)
    if interactive:
        try:
            input()
        except (EOFError, KeyboardInterrupt, OSError):
            pass
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
    except _UsageError as error:
        return _fail(EXIT_USAGE, str(error))
    if not launches.is_launch_id(args.launch_id):
        return _fail(EXIT_USAGE, f"invalid launch id {args.launch_id!r}")
    try:
        profiles = load_profiles()
    except ProfileError as error:
        return _fail(EXIT_USAGE, str(error))
    profile = profiles.get(args.profile)
    if profile is None:
        return _fail(EXIT_USAGE, f"unknown profile {args.profile!r}")
    pending = launches.get(args.launch_id)
    if pending is None:
        return _fail(EXIT_USAGE, f"no launch record for id {args.launch_id}")
    if pending.profile != profile.name:
        return _fail(
            EXIT_USAGE,
            f"launch {args.launch_id} is for profile {pending.profile!r}, not {profile.name!r}",
        )
    if pending.harness != profile.harness:
        return _fail(
            EXIT_USAGE,
            f"launch {args.launch_id} was created for harness {pending.harness!r}, "
            f"but profile {profile.name!r} now uses {profile.harness!r}",
        )
    try:
        profile = launch_profile(profile, pending.folder)
    except ProfileError as error:
        return _fail(EXIT_USAGE, str(error))
    pid = os.getpid()
    info = discovery.get_process_info(pid)
    try:
        record = launches.claim(
            args.launch_id,
            pid,
            info.start_token if info is not None else None,
            pane_from_env(os.environ),
        )
    except (launches.LaunchError, OSError) as error:
        return _fail(EXIT_USAGE, f"cannot claim launch {args.launch_id}: {error}")
    env = build_env(profile, record.launch_id, os.environ)
    command = build_argv(profile, record.args)
    if profile.sandbox is not None:
        try:
            prepared = sandbox_launcher.prepare(profile, record, os.environ, command)
        except sandbox_launcher.SandboxLaunchError as error:
            return _fail(EXIT_SANDBOX, f"profile {profile.name}: {error}")
        command, env = prepared.argv, prepared.env
    if profile.cwd is not None:
        try:
            os.chdir(profile.cwd)
        except OSError as error:
            return _fail(EXIT_USAGE, f"profile {profile.name}: cwd {profile.cwd}: {error.strerror}")
    try:
        os.execvpe(command[0], command, env)
    except OSError as error:
        return _fail(EXIT_EXEC_FAILED, f"cannot run {command[0]!r}: {error.strerror or error}")
    return EXIT_EXEC_FAILED


if __name__ == "__main__":
    sys.exit(main())
