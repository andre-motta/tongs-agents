"""``tongs-agents workspace``: open the agent workspace in Zellij.

Inside Zellij it spawns the requested profiles into the current session and
adds a sidebar pane when none is listed; it never execs. Outside Zellij it
either spawns into a running session and attaches to it, or writes a spawn
request plus a rendered layout and execs Zellij to create the session, whose
sidebar then spawns the profiles. An EXITED session of the same name is
deleted first, or with ``--resurrect`` attached to as it is. Launch records
and runtime files written for a new session are removed again when anything
fails before the exec. Every agent pane goes through ``workspace_spawn``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
from importlib import resources

from tongs_agents import workspace_spawn
from tongs_agents.commands.doctor import Check
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import (
    SESSION_EXITED,
    SESSION_RUNNING,
    MultiplexerError,
    get_multiplexer,
)
from tongs_agents.multiplexers.zellij_binary import (
    ZELLIJ_CEILING,
    ZELLIJ_FLOOR,
    format_version,
    zellij_binary,
    zellij_version,
)
from tongs_agents.paths import UnsafePathError
from tongs_agents.profiles import ProfileError, load_profiles, profiles_path
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME, SpawnRequestError

NAME = "workspace"
HELP = "Open the agent workspace: a Zellij session with the sidebar and profile panes"

DEFAULT_SESSION = "agents"
EXIT_ERROR = 1
EXIT_USAGE = 2
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
TEMPLATE = "agents.kdl.tmpl"
_PLACEHOLDER = re.compile(r"@(PYTHON|SESSION|TOKEN|TONGS)@")
_TONGS_BLOCK = re.compile(
    r"^[ \t]*// @TONGS_TAB_BEGIN@\n(.*?)^[ \t]*// @TONGS_TAB_END@\n",
    re.MULTILINE | re.DOTALL,
)
CREATOR_ENV_DROP = ("TMUX", "TMUX_PANE", "ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID")
TEMP_DIRS = ("/tmp", "/var/tmp")

__all__ = ["SIDEBAR_PANE_NAME", "LayoutError", "render_layout", "run"]


class LayoutError(ValueError):
    """A value that cannot be placed in the workspace layout."""


def add_arguments(parser):
    parser.add_argument(
        "--profile", dest="profiles", action="append", default=[], metavar="NAME",
        help="Start an agent pane for this profile (repeatable)",
    )
    parser.add_argument(
        "--session", default=None, metavar="NAME",
        help=f"Zellij session name (default {DEFAULT_SESSION!r})",
    )
    parser.add_argument(
        "--no-tongs", action="store_true",
        help="Do not open a tongs tab, even when tongs is on PATH",
    )
    parser.add_argument(
        "--resurrect", action="store_true",
        help="When the session is EXITED, attach to it instead of deleting it "
             "and starting it fresh; starts no new panes. Ignored when there "
             "is nothing to resurrect",
    )
    output = parser.add_mutually_exclusive_group()
    output.add_argument(
        "--print-command", action="store_true",
        help="Prepare the workspace and print the zellij command instead of running it",
    )
    output.add_argument(
        "--print-alias", action="store_true",
        help="Print a shell alias for this workspace command; edits no files",
    )


def _is_windows() -> bool:
    return sys.platform == "win32"


def _error(message: str, code: int = EXIT_ERROR) -> int:
    print(f"tongs-agents workspace: {message}", file=sys.stderr)
    return code


def kdl_string(value: str) -> str:
    """``value`` escaped for a KDL quoted string; control characters are refused."""
    if not isinstance(value, str) or not value or not value.isprintable():
        raise LayoutError(f"refused layout value {value!r:.80}")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def valid_session(name) -> bool:
    return isinstance(name, str) and SESSION_RE.match(name) is not None


def _absolute(value, what: str) -> str:
    if not isinstance(value, str) or not os.path.isabs(value):
        raise LayoutError(f"{what} is not an absolute path: {value!r:.80}")
    return kdl_string(value)


def load_template() -> str:
    return resources.files("tongs_agents").joinpath("layouts", TEMPLATE).read_text(
        encoding="utf-8"
    )


def render_layout(python: str, session: str, token: str, tongs: str | None,
                  template: str | None = None) -> str:
    """The workspace layout with only the four validated values substituted."""
    if not valid_session(session):
        raise LayoutError(f"refused session name {session!r:.80}")
    if not workspace_spawn.is_token(token):
        raise LayoutError(f"refused spawn request token {token!r:.80}")
    values = {
        "PYTHON": _absolute(python, "the Python interpreter"),
        "SESSION": session,
        "TOKEN": token,
        "TONGS": _absolute(tongs, "tongs") if tongs is not None else "",
    }
    text = load_template() if template is None else template
    keep = tongs is not None
    text = _TONGS_BLOCK.sub(lambda match: match.group(1) if keep else "", text)
    return _PLACEHOLDER.sub(lambda match: values[match.group(1)], text)


def _profiles(names):
    try:
        profiles = load_profiles()
    except ProfileError as error:
        raise _Refused(str(error), EXIT_ERROR) from None
    unknown = [name for name in names if name not in profiles]
    if unknown:
        raise _Refused(
            f"unknown profile {unknown[0]!r:.80}; see 'tongs-agents profiles' "
            f"and {profiles_path()}",
            EXIT_USAGE,
        )
    return [profiles[name] for name in names]


class _Refused(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


def _self_command() -> list[str]:
    script = shutil.which("tongs-agents")
    if script:
        return [script]
    return [sys.executable, "-m", "tongs_agents.cli"]


def alias_line(args, session: str) -> str:
    command = [*_self_command(), NAME]
    for name in args.profiles:
        command += ["--profile", name]
    if session != DEFAULT_SESSION:
        command += ["--session", session]
    if args.no_tongs:
        command.append("--no-tongs")
    return f"alias {session}={shlex.quote(shlex.join(command))}"


def creator_env(base=None) -> dict[str, str]:
    """The environment for zellij: no outer tmux or zellij pane identity."""
    env = dict(os.environ if base is None else base)
    for key in CREATOR_ENV_DROP:
        env.pop(key, None)
    return env


def _inside(mux, args, profiles) -> int:
    here = mux.in_session()
    if here is None:
        return _error("ZELLIJ is set but the current Zellij session cannot be identified")
    if args.session is not None and args.session != here.session:
        return _error(
            f"already inside Zellij session {here.session!r}; refusing to use "
            f"--session {args.session!r} (run it outside Zellij, or drop --session)",
            EXIT_USAGE,
        )
    if args.print_command:
        return _error(
            "nothing to print inside a Zellij session; run without --print-command",
            EXIT_USAGE,
        )
    if args.resurrect:
        _notice(f"ignoring --resurrect: already inside running session {here.session!r}")
    try:
        workspace_spawn.spawn_profiles(mux, here.session, profiles)
        workspace_spawn.ensure_sidebar(mux, here.session)
    except (MultiplexerError, LaunchError, OSError) as error:
        return _error(getattr(error, "detail", None) or str(error))
    return 0


def _notice(message: str) -> None:
    print(f"tongs-agents workspace: {message}", file=sys.stderr)


def _prepare_new_session(mux, zellij: str, session: str, profiles,
                         no_tongs: bool) -> tuple[list[str], str, list]:
    """Write the pending launches, the spawn request and the layout.

    Everything written here is removed again when a later step fails.
    """
    records = []
    token = None
    try:
        records = workspace_spawn.create_launches(mux.name, session, profiles)
        token = workspace_spawn.write_spawn_request(
            session, [record.launch_id for record in records]
        )
        tongs = None if no_tongs else shutil.which("tongs")
        layout = render_layout(sys.executable, session, token, tongs)
        path = workspace_spawn.write_layout(token, layout)
    except BaseException:
        _discard_new_session(token, records)
        raise
    argv = [zellij, "--session", session, "--new-session-with-layout", str(path)]
    return argv, token, records


def _discard_new_session(token: str | None, records) -> None:
    if token is not None:
        workspace_spawn.discard_runtime_files(token)
    workspace_spawn.discard_launches(records)


def _zellij() -> str:
    binary = zellij_binary()
    if not binary:
        raise MultiplexerError("zellij not found on PATH")
    return binary


def _profile_names(profiles) -> str:
    return ", ".join(profile.name for profile in profiles)


def _outside(mux, args, session: str, profiles) -> int:
    token = None
    records = []
    try:
        zellij = _zellij()
        state = mux.session_state(session)
        if state == SESSION_EXITED and args.resurrect:
            _notice(
                f"attaching to the EXITED session {session!r}; zellij restores "
                "its panes with their commands suspended"
            )
            if profiles:
                _notice(
                    "--resurrect starts no new panes; not starting "
                    f"{_profile_names(profiles)}"
                )
            argv = [zellij, "attach", session]
        elif state == SESSION_EXITED and args.print_command:
            return _error(
                f"session {session!r} is EXITED; run 'zellij delete-session {session}' "
                "first, or add --resurrect to print the command that attaches to it"
            )
        elif state == SESSION_RUNNING:
            if args.print_command:
                if profiles:
                    _notice(
                        f"session {session!r} is running; the printed command only "
                        f"attaches and does not start {_profile_names(profiles)} "
                        "(run without --print-command to start them)"
                    )
            else:
                workspace_spawn.spawn_profiles(mux, session, profiles)
                workspace_spawn.ensure_sidebar(mux, session)
            argv = [zellij, "attach", session]
        else:
            if state == SESSION_EXITED:
                _notice(
                    f"session {session!r} is EXITED; deleting it and starting it "
                    "fresh (--resurrect attaches to it instead)"
                )
                mux.delete_session(session)
            argv, token, records = _prepare_new_session(
                mux, zellij, session, profiles, args.no_tongs
            )
    except (MultiplexerError, LaunchError, SpawnRequestError, LayoutError,
            UnsafePathError, OSError) as error:
        return _error(getattr(error, "detail", None) or str(error))
    if args.print_command:
        if token is not None:
            _notice(
                f"prepared a one-shot spawn request for the new session {session!r}; "
                "run the command below once to start it"
            )
        print(shlex.join(argv))
        return 0
    try:
        os.execvpe(argv[0], argv, creator_env())
    except OSError as error:
        _discard_new_session(token, records)
        return _error(f"cannot run {argv[0]}: {error.strerror or error}")
    return EXIT_ERROR


def run(args) -> int:
    if _is_windows():
        return _error("unsupported on Windows (it needs Zellij on Linux or macOS)", EXIT_USAGE)
    session = DEFAULT_SESSION if args.session is None else args.session
    if not valid_session(session):
        return _error(
            f"refused session name {session!r:.80}; use letters, digits, '.', '_' or '-'",
            EXIT_USAGE,
        )
    try:
        profiles = _profiles(args.profiles)
    except _Refused as refused:
        return _error(str(refused), refused.code)
    if args.print_alias:
        print(alias_line(args, session))
        return 0
    mux = get_multiplexer("zellij")
    available, detail = mux.available()
    if not available:
        return _error(f"{detail}; run 'tongs-agents doctor' for details")
    if os.environ.get("ZELLIJ") is not None:
        return _inside(mux, args, profiles)
    return _outside(mux, args, session, profiles)


def _zellij_checks(binary) -> list[Check]:
    floor = format_version(ZELLIJ_FLOOR)
    if not binary:
        return [Check(
            "workspace-zellij", "info",
            f"zellij is not on PATH; 'tongs-agents workspace' needs zellij {floor} or newer",
            f"Install zellij {floor} or newer to use the workspace",
        )]
    version = zellij_version(binary)
    if version is None:
        return [Check(
            "workspace-zellij", "warn", f"Could not read the version of {binary}",
            f"Check that '{binary} --version' prints 'zellij X.Y.Z'",
        )]
    shown = format_version(version)
    ceiling = format_version(ZELLIJ_CEILING)
    if version < ZELLIJ_FLOOR:
        return [Check(
            "workspace-zellij", "error",
            f"zellij {shown} at {binary} is older than the supported {floor}",
            f"Upgrade zellij to {floor} or newer",
        )]
    if version > ZELLIJ_CEILING:
        return [Check(
            "workspace-zellij", "warn",
            f"zellij {shown} at {binary} is newer than the validated {ceiling}",
            f"If the workspace misbehaves, use zellij {ceiling}",
        )]
    return [Check("workspace-zellij", "ok", f"zellij {shown} at {binary}")]


def _within(path: str, directory: str) -> bool:
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def _interpreter_checks(executable: str | None = None) -> list[Check]:
    executable = sys.executable if executable is None else executable
    if not executable:
        return []
    folder = os.path.dirname(os.path.abspath(executable))
    candidates = {folder, os.path.realpath(folder)}
    temp_dirs = set()
    for directory in (tempfile.gettempdir(), *TEMP_DIRS):
        temp_dirs.update({os.path.abspath(directory), os.path.realpath(directory)})
    if any(_within(path, temp) for path in candidates for temp in temp_dirs):
        return [Check(
            "workspace-python", "warn",
            f"tongs-agents runs from {executable}, inside a temporary directory; workspace "
            "layouts and agent panes start that interpreter and break once it is removed",
            "Install tongs-agents in a permanent environment, for example with pipx",
        )]
    return []


def doctor_checks():
    """Zellij for the workspace and, when it is present, the pane interpreter.

    Profile hooks and wrapper names are reported by the core doctor and the
    profiles command.
    """
    binary = zellij_binary()
    checks = _zellij_checks(binary)
    if binary:
        checks += _interpreter_checks()
    return checks
