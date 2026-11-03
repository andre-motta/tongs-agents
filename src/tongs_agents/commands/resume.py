"""``tongs-agents resume [ID]``: move an existing agent session into the workspace.

Without ID it lists the recent sessions the session store holds (it only
reads the store): harness, folder, session id, the profile or config dir
that holds the session, the last status and its age. With an ID or a unique
prefix of one it starts that session in the workspace through the ``run``
path (``commands.run``), with:

- the profile: ``--profile NAME``; else the profile recorded by the launch
  that started the session; else the one profile of that harness whose
  config dir holds the session's transcript (Claude Code
  ``projects/*/ID.jsonl``, Codex ``sessions/YYYY/MM/DD/rollout-*-ID.jsonl``,
  Pi ``sessions/*/*_ID.jsonl``). None or several is refused, naming
  ``--profile``. A sandboxed profile is only used when named or recorded;
- the recorded folder;
- the harness's resume arguments (``RESUME_ARGS``): Claude Code
  ``--resume ID``, Codex ``resume ID``, Pi ``--session ID`` (Pi 1.5.0
  ``docs/cli.md``: "Opens a session by file path or full/partial ID, and
  resumes it in its working directory"; its own exit hint prints
  ``pi --session <id>``).

When the session still has a live process (any status but ended) in the
workspace session it would join, it is refused: that pane is already there,
so show it instead. A resume of the session that was started in that
workspace session counts too, before the harness writes any record: a
pending launch with its resume arguments created in the last
``PENDING_LAUNCH_SECONDS``, or a claimed one whose process still runs. A live process anywhere else gets a warning: two
processes on one session conflict. It never stops that process. That is the
usual case: maintenance removes a record once its process is gone, so a
session is resumed while it still runs, and the old one is quit afterwards.

The sidebar's command line (``widgets.command_line``) and its palette
entries use ``recent_sessions``, ``find_session`` and ``plan``.
"""

from __future__ import annotations

import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tongs_agents import discovery, launches, workspace_spawn
from tongs_agents.app_config import MULTIPLEXERS, AppConfigError, load_app_config
from tongs_agents.commands import run as run_command
from tongs_agents.commands import workspace
from tongs_agents.harnesses import get_harness
from tongs_agents.launches import LaunchError
from tongs_agents.models import format_age
from tongs_agents.multiplexers import get_multiplexer
from tongs_agents.pi_install import pi_config_dir
from tongs_agents.profiles import (
    CONFIG_DIR_ENV_BY_HARNESS,
    Profile,
    ProfileError,
    load_profiles,
)

NAME = "resume"
HELP = "List recent agent sessions, or resume one in the workspace"

EXIT_ERROR = 1
EXIT_USAGE = 2
RESUME_ARGS = {
    "claude": ("--resume",),
    "codex": ("resume",),
    "pi": ("--session",),
}
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
SHORT_ID = 8
MAX_SCAN_ENTRIES = 20000
DAY_SECONDS = 24 * 3600
PENDING_LAUNCH_SECONDS = 30
LIST_LIMIT = 50
EPILOG = (
    "Without ID it lists the sessions it can resume. ID may be a unique prefix, "
    "for example: tongs-agents resume 3f2a"
)


def add_arguments(parser):
    parser.epilog = EPILOG
    parser.add_argument(
        "session_id", nargs="?", default=None, metavar="ID",
        help="The session id, or a unique prefix of it, to resume",
    )
    parser.add_argument(
        "--profile", default=None, metavar="NAME",
        help="The profile to resume it with (default: the one that matches its "
             "harness and config dir)",
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


class ResumeRefused(Exception):
    """A resume refused before anything starts."""


@dataclass(frozen=True)
class Resumable:
    """One harness session the store holds, from its most recent record."""

    harness: str
    session_id: str
    folder: str
    status: str
    ts: int
    age_seconds: int
    profile_id: str = ""
    records: tuple[dict, ...] = field(default=(), compare=False, repr=False)

    @property
    def short_id(self) -> str:
        return self.session_id[:SHORT_ID]

    @property
    def label(self) -> str:
        harness = get_harness(self.harness)
        return harness.display_name if harness is not None else self.harness


@dataclass(frozen=True)
class Plan:
    """What resuming ``target`` starts: ``profile`` with the checked ``options``."""

    target: Resumable
    profile: Profile
    options: dict

    @property
    def summary(self) -> str:
        return (f"{self.target.label} session {self.target.short_id} with profile "
                f"{self.profile.name} in {self.options['folder']}")


def resume_args(harness: str, session_id: str) -> tuple[str, ...]:
    """The arguments that make ``harness`` resume ``session_id``."""
    return (*RESUME_ARGS[harness], session_id)


def _resumable_record(record: dict) -> bool:
    if record.get("_process") or record.get("source") == "process":
        return False
    harness = record.get("harness_id", record.get("agent"))
    session_id = record.get("session_id")
    return (
        harness in RESUME_ARGS
        and isinstance(session_id, str)
        and SESSION_ID_RE.match(session_id) is not None
    )


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def recent_sessions(
    records: Iterable[dict] | None = None, *, now: int | None = None,
) -> list[Resumable]:
    """The resumable sessions in the store, most recently active first; only reads.

    A session with several records (instances) is listed once, from its
    latest record; every record is kept for the live process check.
    """
    if records is None:
        records = discovery.read_session_records()
    now = int(time.time()) if now is None else now
    grouped: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        if not isinstance(record, dict) or not _resumable_record(record):
            continue
        harness = record.get("harness_id", record.get("agent"))
        grouped.setdefault((harness, record["session_id"]), []).append(record)
    sessions = []
    for (harness, session_id), group in grouped.items():
        group.sort(key=lambda item: _int(item.get("ts")), reverse=True)
        latest = discovery.annotate_session(group[0], now)
        cwd = latest.get("cwd")
        sessions.append(Resumable(
            harness=harness,
            session_id=session_id,
            folder=cwd if isinstance(cwd, str) else "",
            status=str(latest.get("status", "")),
            ts=_int(latest.get("ts")),
            age_seconds=_int(latest.get("age_seconds")),
            profile_id=str(latest.get("profile_id", "")),
            records=tuple(group),
        ))
    sessions.sort(key=lambda item: (-item.ts, item.harness, item.session_id))
    return sessions


def find_session(sessions: Iterable[Resumable], text: str) -> Resumable:
    """The session ``text`` names exactly, else the one whose id starts with it."""
    sessions = list(sessions)
    if not text:
        raise ResumeRefused("give a session id; 'tongs-agents resume' lists them")
    exact = [item for item in sessions if item.session_id == text]
    found = exact or [item for item in sessions if item.session_id.startswith(text)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ResumeRefused(
            f"no session {text!r:.80} in the session store; 'tongs-agents resume' lists "
            "the ones it holds (sessions whose agent still runs)"
        )
    names = ", ".join(f"{item.harness} {item.session_id}" for item in found[:5])
    more = f" and {len(found) - 5} more" if len(found) > 5 else ""
    raise ResumeRefused(f"{text!r:.80} matches {names}{more}; type more of the id")


# -- Config dirs --

def default_home(harness: str, environ: Mapping[str, str] | None = None) -> Path:
    """The config dir ``harness`` uses when no profile names one."""
    environ = os.environ if environ is None else environ
    if harness == "pi":
        return pi_config_dir(environ)
    value = environ.get(CONFIG_DIR_ENV_BY_HARNESS[harness])
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / (".claude" if harness == "claude" else ".codex")


def profile_home(profile: Profile, environ: Mapping[str, str] | None = None) -> Path | None:
    """The config dir ``profile`` runs its harness with; None when it names a relative one."""
    if profile.config_dir is not None:
        return Path(os.path.abspath(profile.config_dir.expanduser()))
    variable = CONFIG_DIR_ENV_BY_HARNESS.get(profile.harness)
    if variable is None:
        return None
    value = profile.env.get(variable)
    if value is None:
        return Path(os.path.abspath(default_home(profile.harness, environ)))
    if not os.path.isabs(value):
        return None
    return Path(os.path.abspath(Path(value).expanduser()))


def _entries(directory: Path, budget: list[int]) -> list[os.DirEntry]:
    found = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                budget[0] -= 1
                if budget[0] < 0:
                    break
                found.append(entry)
    except OSError:
        return []
    return found


def _is_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _newest_dirs(root: Path, depth: int, budget: list[int]) -> Iterator[Path]:
    """The directories ``depth`` levels below ``root``, highest names first.

    Codex names them ``YYYY/MM/DD``, so the newest days come first and the
    shared budget is spent on recent sessions.
    """
    if depth == 0:
        yield root
        return
    children = sorted(
        (entry for entry in _entries(root, budget) if _is_dir(entry)),
        key=lambda entry: entry.name, reverse=True,
    )
    for child in children:
        yield from _newest_dirs(Path(child.path), depth - 1, budget)
        if budget[0] < 0:
            return


def codex_days(session: Resumable) -> list[tuple[str, str, str]]:
    """The ``YYYY, MM, DD`` folders a Codex rollout of ``session`` most likely sits in.

    Every record's ``started`` and ``ts`` as a UTC and a local date, each
    with the day before and after, in that order and without repeats.
    """
    stamps = []
    for record in session.records:
        for key in ("started", "ts"):
            value = _int(record.get(key))
            if value > 0 and value not in stamps:
                stamps.append(value)
    if session.ts > 0 and session.ts not in stamps:
        stamps.append(session.ts)
    days: list[tuple[str, str, str]] = []
    for stamp in stamps:
        for offset in (0, -DAY_SECONDS, DAY_SECONDS):
            for convert in (time.gmtime, time.localtime):
                try:
                    moment = convert(stamp + offset)
                except (OverflowError, OSError, ValueError):
                    continue
                day = (f"{moment.tm_year:04d}", f"{moment.tm_mon:02d}",
                       f"{moment.tm_mday:02d}")
                if day not in days:
                    days.append(day)
    return days


def _claude_project(folder: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def holds_session(harness: str, home: Path, session: Resumable) -> bool:
    """True when ``home`` holds the transcript of ``session``; reads at most
    ``MAX_SCAN_ENTRIES`` directory entries and never raises."""
    session_id = session.session_id
    budget = [MAX_SCAN_ENTRIES]
    if harness == "claude":
        projects = home / "projects"
        name = f"{session_id}.jsonl"
        if session.folder:
            try:
                if (projects / _claude_project(session.folder) / name).is_file():
                    return True
            except OSError:
                pass
        for entry in _entries(projects, budget):
            try:
                if _is_dir(entry) and (Path(entry.path) / name).is_file():
                    return True
            except OSError:
                continue
        return False
    if harness == "codex":
        ending = f"-{session_id}.jsonl"
        sessions = home / "sessions"

        def holds(directory: Path) -> bool:
            return any(
                entry.name.startswith("rollout-") and entry.name.endswith(ending)
                for entry in _entries(directory, budget)
            )

        probed = set()
        for day in codex_days(session):
            directory = sessions.joinpath(*day)
            probed.add(directory)
            if holds(directory):
                return True
        for directory in _newest_dirs(sessions, 3, budget):
            if directory not in probed and holds(directory):
                return True
            if budget[0] < 0:
                break
        return False
    if harness == "pi":
        ending = f"_{session_id}.jsonl"
        return any(
            entry.name.endswith(ending)
            for directory in _entries(home / "sessions", budget) if _is_dir(directory)
            for entry in _entries(Path(directory.path), budget)
        )
    return False


# -- Profile --

def _candidates(profiles: Mapping[str, Profile], harness: str) -> list[Profile]:
    return [profiles[name] for name in sorted(profiles)
            if profiles[name].harness == harness and profiles[name].sandbox is None]


def match_profile(
    session: Resumable, profiles: Mapping[str, Profile], name: str | None = None,
    *, holds: Callable[[str, Path, Resumable], bool] = holds_session,
) -> Profile:
    """The profile to resume ``session`` with; ResumeRefused when none or several fit."""
    harness = session.harness
    if name is not None:
        profile = profiles.get(name)
        if profile is None:
            raise ResumeRefused(f"unknown profile {name!r:.80}; see 'tongs-agents profiles'")
        if profile.harness != harness:
            raise ResumeRefused(
                f"profile {name} runs {profile.harness}, but session {session.short_id} "
                f"is a {session.label} session"
            )
        return profile
    recorded = profiles.get(session.profile_id) if session.profile_id else None
    if recorded is not None and recorded.harness == harness:
        return recorded
    candidates = _candidates(profiles, harness)
    hint = f"pass --profile NAME: tongs-agents resume {session.short_id} --profile NAME"
    if not candidates:
        raise ResumeRefused(f"no {session.label} profile to resume it with; add one, or {hint}")
    homes: dict[Path, list[Profile]] = {}
    for profile in candidates:
        home = profile_home(profile)
        if home is not None:
            homes.setdefault(home, []).append(profile)
    matched = [profile for home, group in homes.items()
               if holds(harness, home, session) for profile in group]
    names = ", ".join(profile.name for profile in (matched or candidates))
    if len(matched) == 1:
        return matched[0]
    if not matched:
        raise ResumeRefused(
            f"no {session.label} profile's config dir holds session {session.short_id} "
            f"(profiles: {names}); {hint}"
        )
    raise ResumeRefused(
        f"several {session.label} profiles have a config dir that holds session "
        f"{session.short_id} ({names}); {hint}"
    )


def origin(session: Resumable, profiles: Mapping[str, Profile]) -> str:
    """The profile that would resume ``session``, else the config dir holding it."""
    try:
        return match_profile(session, profiles).name
    except ResumeRefused:
        pass
    homes = {profile_home(profile) for profile in profiles.values()
             if profile.harness == session.harness}
    homes.add(Path(os.path.abspath(default_home(session.harness))))
    for home in sorted(path for path in homes if path is not None):
        if holds_session(session.harness, home, session):
            return short_path(str(home))
    return "?"


def plan(session: Resumable, profiles: Mapping[str, Profile],
         name: str | None = None) -> Plan:
    """The profile and checked launch options that resume ``session``."""
    if not session.folder:
        raise ResumeRefused(f"session {session.short_id} has no recorded folder")
    profile = match_profile(session, profiles, name)
    try:
        folder, extra = workspace_spawn.check_launch(
            profile, session.folder, resume_args(session.harness, session.session_id),
        )
    except LaunchError as error:
        raise ResumeRefused(str(error)) from None
    return Plan(session, profile, {"folder": folder, "args": extra})


# -- Live processes --

@dataclass(frozen=True)
class LiveProcess:
    """A live process of a session: its pid, where it runs and whether that is here.

    ``pid`` is None for a resume launch whose pane has not started yet.
    """

    pid: int | None
    where: str
    here: bool


def _location(record: dict) -> tuple[str | None, str | None]:
    """The workspace session (and its multiplexer, when known) a record runs in."""
    env = record.get("terminal_env")
    env = env if isinstance(env, dict) else {}
    name = env.get("ZELLIJ_SESSION_NAME")
    if isinstance(name, str) and name:
        return name, "zellij"
    launch = record.get("launch") if isinstance(record.get("launch"), dict) else {}
    if launch.get("launch_id"):
        launched = launches.get(launch["launch_id"])
        if launched is not None:
            return launched.session, launched.multiplexer
    return None, None


def _where(record: dict, name: str | None, here: bool) -> str:
    if here:
        return "in this workspace"
    if name:
        return f"in session {name!r}"
    terminal = record.get("terminal")
    return f"in {terminal}" if isinstance(terminal, str) and terminal else "elsewhere"


def _launch_reported(launch_id: str) -> bool:
    """True when a session record already names ``launch_id``."""
    for record in discovery.read_session_records():
        launch = record.get("launch") if isinstance(record, dict) else None
        if isinstance(launch, dict) and launch.get("launch_id") == launch_id:
            return True
    return False


def resuming_launch(session: Resumable, workspace_session: str,
                    multiplexer: str | None = None, *,
                    now: float | None = None) -> LiveProcess | None:
    """A launch in ``workspace_session`` that resumes ``session`` and has no record yet.

    Its arguments end with the harness's resume arguments for the session.
    It counts while pending for at most ``PENDING_LAUNCH_SECONDS``, or once
    claimed while its process (pid and start token) runs; a launch that a
    session record already names is left to the store (the agent may have
    moved on to another session since, for example with ``/clear``).
    """
    tail = resume_args(session.harness, session.session_id)
    now = time.time() if now is None else now
    for record in reversed(launches.list_records(workspace_session)):
        if record.harness != session.harness or tuple(record.args[-len(tail):]) != tail:
            continue
        if multiplexer is not None and record.multiplexer != multiplexer:
            continue
        if record.state == "pending":
            if now - record.created > PENDING_LAUNCH_SECONDS:
                continue
            pid = None
        elif record.state == "running" and launches.alive(record):
            pid = record.pid
        else:
            continue
        if _launch_reported(record.launch_id):
            continue
        return LiveProcess(pid, "in this workspace", True)
    return None


def live_process(session: Resumable, workspace_session: str | None = None,
                 multiplexer: str | None = None) -> LiveProcess | None:
    """The live process of ``session``, if any; ``here`` when it runs in
    ``workspace_session`` (on ``multiplexer``, when both are known).

    An ended record is skipped: its process may have started another session
    (Claude Code ``/clear``). A process in this workspace wins over one
    elsewhere, and a resume launch in this workspace whose harness has not
    written a record yet (``resuming_launch``) counts as one. The process is
    never stopped.
    """
    if workspace_session is not None:
        starting = resuming_launch(session, workspace_session, multiplexer)
        if starting is not None:
            return starting
    found = None
    for record in session.records:
        if record.get("status") == "ended":
            continue
        try:
            process = discovery.resolve_session_process(record)
        except (OSError, ValueError):
            process = None
        if process is None:
            continue
        name, runs_on = _location(record)
        here = (
            workspace_session is not None
            and name == workspace_session
            and (multiplexer is None or runs_on is None or runs_on == multiplexer)
        )
        live = LiveProcess(process.pid, _where(record, name, here), here)
        if here:
            return live
        found = found or live
    return found


def live_warning(session: Resumable, workspace_session: str | None = None,
                 multiplexer: str | None = None) -> str | None:
    """A warning when ``session`` still has a live process; None otherwise."""
    live = live_process(session, workspace_session, multiplexer)
    if live is None:
        return None
    return warning_text(session, live)


def _as_pid(live: LiveProcess) -> str:
    return f"as pid {live.pid}" if live.pid is not None else "in a pane that is starting"


def warning_text(session: Resumable, live: LiveProcess) -> str:
    return (
        f"session {session.short_id} still runs {_as_pid(live)} {live.where}; "
        "two processes on one session conflict, so quit that one before you type in "
        "the new pane (it was not stopped)"
    )


def here_refusal(session: Resumable, live: LiveProcess) -> str:
    """Why a session already running in this workspace is not resumed again."""
    return (
        f"session {session.short_id} already runs in this workspace {_as_pid(live)}; "
        "show its pane instead (select its row in the sidebar, or run "
        f"'tongs-agents focus {session.session_id}')"
    )


def _workspace_here(args, settings) -> tuple[str | None, str | None]:
    """The workspace session and multiplexer ``run`` would add the pane to."""
    if os.environ.get("ZELLIJ") is not None:
        return os.environ.get("ZELLIJ_SESSION_NAME"), "zellij"
    if os.environ.get("TMUX"):
        here = get_multiplexer("tmux").in_session()
        return (here.session if here is not None else None), "tmux"
    session = settings.session if args.session is None else args.session
    return session, args.multiplexer or settings.multiplexer


# -- Listing --

def short_path(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def table(sessions: Iterable[Resumable], profiles: Mapping[str, Profile]) -> str:
    """The listing ``tongs-agents resume`` prints."""
    header = ("HARNESS", "SESSION", "PROFILE/CONFIG", "STATUS", "AGE", "FOLDER")
    rows = [header]
    for session in sessions:
        rows.append((
            session.harness, session.session_id, origin(session, profiles),
            session.status or "?", format_age(session.age_seconds),
            short_path(session.folder) or "?",
        ))
    widths = [max(len(row[index]) for row in rows) for index in range(len(header) - 1)]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    return "\n".join(lines)


# -- CLI --

def _error(message: str, code: int = EXIT_ERROR) -> int:
    print(f"tongs-agents resume: {message}", file=sys.stderr)
    return code


def _list(profiles: Mapping[str, Profile]) -> int:
    sessions = recent_sessions()
    if not sessions:
        print("No sessions to resume: the session store only holds sessions whose "
              "agent still runs.")
        return 0
    shown = sessions[:LIST_LIMIT]
    print(table(shown, profiles))
    if len(sessions) > len(shown):
        print(f"({len(sessions) - len(shown)} older sessions not shown)")
    print("\nResume one with: tongs-agents resume ID (a unique prefix is enough)")
    return 0


def run(args) -> int:
    if workspace._is_windows():
        return _error(
            "unsupported on Windows (it needs Zellij or tmux on Linux or macOS)", EXIT_USAGE
        )
    try:
        profiles = load_profiles()
    except ProfileError as error:
        if args.session_id is not None:
            return _error(str(error))
        print(f"tongs-agents resume: {error}", file=sys.stderr)
        profiles = {}
    if args.session_id is None:
        return _list(profiles)
    try:
        settings = load_app_config().workspace
    except AppConfigError as error:
        return _error(str(error))
    try:
        target = find_session(recent_sessions(), args.session_id)
        resumed = plan(target, profiles, args.profile)
    except ResumeRefused as refused:
        return _error(str(refused), EXIT_USAGE)
    here, multiplexer = _workspace_here(args, settings)
    live = live_process(target, here, multiplexer)
    if live is not None and live.here:
        return _error(here_refusal(target, live), EXIT_USAGE)
    if live is not None:
        print(f"tongs-agents resume: warning: {warning_text(target, live)}", file=sys.stderr)
    print(f"tongs-agents resume: resuming {resumed.summary}", file=sys.stderr)
    if os.environ.get("ZELLIJ") is not None:
        return run_command._inside("zellij", args, resumed.profile, resumed.options)
    if os.environ.get("TMUX"):
        return run_command._inside("tmux", args, resumed.profile, resumed.options)
    return run_command._outside(args, settings, resumed.profile, resumed.options)
