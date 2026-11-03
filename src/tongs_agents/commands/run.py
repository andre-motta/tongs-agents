"""``tongs-agents run PROFILE [DIR] [-- ARGS...]``: add one agent pane to the workspace.

A profile is a template; ``run`` picks the folder and may append arguments
(``--resume``, ``resume --last``, a prompt) for this one launch. DIR defaults
to the current directory, and to the profile's ``cwd`` only when the current
directory cannot be read. Everything after ``--`` is appended to the
profile's command unparsed (``cli.parse_args`` splits it off before argparse
sees it). Both are validated (``workspace_spawn.check_launch``) and recorded
in the 0600 launch record, which the launcher reads and checks again.

Where the pane goes:

- inside Zellij or tmux: the current session, plus a sidebar when none is
  listed; it never nests or execs;
- outside, the configured workspace session is running: the pane is added
  to it and the command returns, so a terminal already attached elsewhere
  shows it;
- outside, no session: the workspace starts with the sidebar and this one
  pane, through the same new-session paths as ``tongs-agents workspace``
  (a Zellij spawn request and layout, or a detached tmux session), then
  attaches. An EXITED Zellij session of that name is deleted first.

MCP ``agents_spawn`` never takes a folder or arguments; only this command and
the sidebar's Add agent dialog, both run by the maintainer, write them.
"""

from __future__ import annotations

import argparse
import os
import sys

from tongs_agents import workspace_spawn
from tongs_agents.app_config import MULTIPLEXERS, AppConfigError, load_app_config
from tongs_agents.commands import workspace
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import SESSION_RUNNING, MultiplexerError, get_multiplexer
from tongs_agents.paths import UnsafePathError
from tongs_agents.profiles import ProfileError, load_profiles, profiles_path

NAME = "run"
HELP = "Add an agent pane for a profile to the workspace, in a folder, with extra arguments"

EXIT_ERROR = 1
EXIT_USAGE = 2
USAGE = ("%(prog)s [-h] [--session NAME] [--multiplexer {zellij,tmux}] "
         "PROFILE [DIR] [-- ARGS ...]")
EPILOG = ("Everything after -- is appended to the profile's command for this launch "
          "only, for example: tongs-agents run claude-main ~/src/app -- --resume")


def add_arguments(parser):
    parser.usage = USAGE
    parser.epilog = EPILOG
    parser.add_argument("profile", metavar="PROFILE", help="The profile to start")
    parser.add_argument(
        "folder", nargs="?", default=None, metavar="DIR",
        help="The folder the agent starts in (default: the current directory)",
    )
    parser.add_argument(
        "--session", default=None, metavar="NAME",
        help="The workspace session when run outside one "
             f"(default: [workspace] session, else {workspace.DEFAULT_SESSION!r})",
    )
    parser.add_argument(
        "--multiplexer", choices=MULTIPLEXERS, default=None,
        help="The multiplexer when run outside one "
             "(default: [workspace] multiplexer, else zellij)",
    )
    parser.set_defaults(extra=[])


def _error(message: str, code: int = EXIT_ERROR) -> int:
    print(f"tongs-agents run: {message}", file=sys.stderr)
    return code


def _notice(message: str) -> None:
    print(f"tongs-agents run: {message}", file=sys.stderr)


def resolve_folder(value: str | None, profile) -> str | None:
    """DIR as an absolute path; without it the current directory, else the profile cwd."""
    if value is not None:
        return os.path.abspath(os.path.expanduser(value))
    try:
        return os.getcwd()
    except OSError:
        return str(profile.cwd) if profile.cwd is not None else None


def _described(profile, options) -> str:
    text = profile.name
    if options["folder"] is not None:
        text += f" in {options['folder']}"
    return text


def _available(mux) -> str | None:
    available, detail = mux.available()
    return None if available else f"{detail}; run 'tongs-agents doctor' for details"


def _inside(name: str, args, profile, options) -> int:
    label, variable = workspace.LABELS[name]
    if args.multiplexer not in (None, name):
        return _error(
            f"already inside {label}; refusing --multiplexer {args.multiplexer} "
            "(run it outside, or drop --multiplexer)",
            EXIT_USAGE,
        )
    mux = get_multiplexer(name)
    problem = _available(mux)
    if problem is not None:
        return _error(problem)
    here = mux.in_session()
    if here is None:
        return _error(
            f"{variable} is set but the current {label} session cannot be identified"
        )
    if args.session is not None and args.session != here.session:
        return _error(
            f"already inside {label} session {here.session!r}; refusing to use "
            f"--session {args.session!r} (run it outside {label}, or drop --session)",
            EXIT_USAGE,
        )
    try:
        workspace_spawn.spawn_profiles(mux, here.session, [profile], **options)
        workspace_spawn.ensure_sidebar(mux, here.session)
    except (MultiplexerError, LaunchError, OSError) as error:
        return _error(getattr(error, "detail", None) or str(error))
    _notice(f"started {_described(profile, options)} in session {here.session!r}")
    return 0


def _attach_hint(session: str, default: str) -> str:
    if session == default:
        return "tongs-agents"
    return f"tongs-agents workspace --session {session}"


def _outside(args, settings, profile, options) -> int:
    session = settings.session if args.session is None else args.session
    if not workspace.valid_session(session):
        return _error(
            f"refused session name {session!r:.80}; use letters, digits, '.', '_' or '-'",
            EXIT_USAGE,
        )
    name = args.multiplexer or settings.multiplexer
    mux = get_multiplexer(name)
    problem = _available(mux)
    if problem is not None:
        return _error(problem)
    try:
        state = mux.session_state(session)
        if state == SESSION_RUNNING:
            workspace_spawn.spawn_profiles(mux, session, [profile], **options)
            workspace_spawn.ensure_sidebar(mux, session)
            _notice(
                f"started {_described(profile, options)} in the running session "
                f"{session!r}; attach with '{_attach_hint(session, settings.session)}'"
            )
            return 0
    except (MultiplexerError, LaunchError, UnsafePathError, OSError) as error:
        return _error(getattr(error, "detail", None) or str(error))
    plain = argparse.Namespace(resurrect=False, print_command=False, tongs=None)
    if name == "tmux":
        return workspace._outside_tmux(mux, plain, session, [profile], [profile], options)
    return workspace._outside(
        mux, plain, session, [profile], [profile], settings.tongs_tab, options
    )


def run(args) -> int:
    if workspace._is_windows():
        return _error(
            "unsupported on Windows (it needs Zellij or tmux on Linux or macOS)", EXIT_USAGE
        )
    try:
        settings = load_app_config().workspace
    except AppConfigError as error:
        return _error(str(error))
    try:
        profiles = load_profiles()
    except ProfileError as error:
        return _error(str(error))
    profile = profiles.get(args.profile)
    if profile is None:
        return _error(
            f"unknown profile {args.profile!r:.80}; see 'tongs-agents profiles' "
            f"and {profiles_path()}",
            EXIT_USAGE,
        )
    folder = resolve_folder(args.folder, profile)
    try:
        folder, extra = workspace_spawn.check_launch(profile, folder, args.extra or ())
    except LaunchError as error:
        hint = "" if args.folder is not None else "; name a folder: tongs-agents run PROFILE DIR"
        return _error(f"{error}{hint}", EXIT_USAGE)
    options = {"folder": folder, "args": extra}
    if os.environ.get("ZELLIJ") is not None:
        return _inside("zellij", args, profile, options)
    if os.environ.get("TMUX"):
        return _inside("tmux", args, profile, options)
    return _outside(args, settings, profile, options)
