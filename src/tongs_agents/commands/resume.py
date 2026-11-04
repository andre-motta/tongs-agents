"""``tongs-agents resume [ID [DIR]]``: resume an agent session in the workspace.

Without ID it lists the recent sessions (it only reads): those the session
store holds (their agent still runs) and the past ones the harnesses' own
history stores hold under each profile's config dir (``history``), newest
first, with harness, session id, the profile or config dir that holds it,
the last status, its age, folder and title. With an ID or a unique prefix of
one it starts that session in the workspace through the ``run`` path
(``commands.run``), with:

- the profile: ``--profile NAME``; else the profile recorded by the launch
  that started the session; else the one profile of that harness whose
  config dir holds the session's transcript (Claude Code
  ``projects/*/ID.jsonl``, Codex ``sessions/YYYY/MM/DD/rollout-*-ID.jsonl``,
  Pi ``sessions/*/*_ID.jsonl``); of several, the one the newest resume
  launch of the session used. None or several is refused, naming
  ``--profile``. A sandboxed profile is only used when named or recorded;
- DIR, else the recorded folder; one that no longer exists is refused;
- the label of the latest launch that ran the session, else its title;
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
entries use ``all_sessions``, ``lookup`` and ``plan``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from tongs_agents import discovery, history, launches, workspace_spawn
from tongs_agents.app_config import MULTIPLEXERS, AppConfigError, load_app_config
from tongs_agents.commands import run as run_command
from tongs_agents.commands import workspace
from tongs_agents.harnesses import get_harness
from tongs_agents.launches import LaunchError
from tongs_agents.models import format_age, validated_launch
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
SESSION_ID_RE = history.SESSION_ID_RE
SHORT_ID = 8
MAX_SCAN_ENTRIES = 20000
DAY_SECONDS = 24 * 3600
PENDING_LAUNCH_SECONDS = 30
RECENT_WRITE_SECONDS = 120
LIST_LIMIT = 50
PI_SESSION_DIR_ENV = "PI_CODING_AGENT_SESSION_DIR"
PI_SETTINGS_NAME = "settings.json"
PI_SETTINGS_BYTES = 256 * 1024
PICKER_LIMIT = 300
NO_STATUS = "-"
EPILOG = (
    "Without ID it lists the sessions it can resume, running and past. ID may be a "
    "unique prefix, for example: tongs-agents resume 3f2a"
)


def add_arguments(parser):
    parser.epilog = EPILOG
    parser.add_argument(
        "session_id", nargs="?", default=None, metavar="ID",
        help="The session id, or a unique prefix of it, to resume",
    )
    parser.add_argument(
        "folder", nargs="?", default=None, metavar="DIR",
        help="The folder to resume it in (default: the folder it ran in)",
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
    """One harness session: from its most recent store record, its history, or both.

    ``homes`` are the config dirs whose history holds it (empty for a
    session only the store knows), ``title`` its history title.
    """

    harness: str
    session_id: str
    folder: str
    status: str
    ts: int
    age_seconds: int
    profile_id: str = ""
    records: tuple[dict, ...] = field(default=(), compare=False, repr=False)
    title: str = ""
    homes: tuple[Path, ...] = ()

    @property
    def short_id(self) -> str:
        return self.session_id[:SHORT_ID]

    @property
    def running(self) -> bool:
        """True when the store still holds a record of it that has not ended."""
        return any(record.get("status") != "ended" for record in self.records)

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
    label: str | None = None

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
    return harness in RESUME_ARGS and history.resumable_id(harness, session_id)


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
        sessions.append(Resumable(
            harness=harness,
            session_id=session_id,
            folder=history.safe_folder(latest.get("cwd")),
            status=str(latest.get("status", "")),
            ts=_int(latest.get("ts")),
            age_seconds=_int(latest.get("age_seconds")),
            profile_id=str(latest.get("profile_id", "")),
            records=tuple(group),
        ))
    sessions.sort(key=lambda item: (-item.ts, item.harness, item.session_id))
    return sessions


def _pi_setting(home: Path) -> str | None:
    """``sessionDir`` from ``home/settings.json``; None when unset or unreadable."""
    try:
        with history.open_file(home / PI_SETTINGS_NAME) as handle:
            raw = handle.read(PI_SETTINGS_BYTES + 1)
        if len(raw) > PI_SETTINGS_BYTES:
            return None
        data = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return None
    value = data.get("sessionDir") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def _absolute(value: str) -> Path | None:
    expanded = os.path.expanduser(value)
    return Path(os.path.normpath(expanded)) if os.path.isabs(expanded) else None


def pi_sessions_dir(profile: Profile, home: Path,
                    environ: Mapping[str, str] | None = None) -> Path | None:
    """The session dir ``profile`` runs Pi with; None when it cannot be known.

    ``PI_CODING_AGENT_SESSION_DIR`` from the profile's env, else from
    ``environ``, else ``sessionDir`` in the agent dir's ``settings.json``,
    else ``home/sessions``. ``~`` is expanded as Pi does; a relative path
    is resolved by Pi from the folder it starts in, so it cannot be known
    here. Project settings (``.pi/settings.json``) are not read.
    """
    environ = os.environ if environ is None else environ
    for source in (profile.env, environ):
        value = source.get(PI_SESSION_DIR_ENV)
        if value:
            return _absolute(value)
    value = _pi_setting(home)
    if value is not None:
        return _absolute(value)
    return home / "sessions"


def history_homes(profiles: Mapping[str, Profile]) -> list[tuple[str, Path, Path | None]]:
    """The (harness, config dir, Pi session dir) whose history is listed, in profile order.

    The config dirs of the profiles that can resume a session unnamed (not
    sandboxed), each once; the session dir is None except for Pi. A Pi
    profile whose session dir cannot be known lists nothing.
    """
    stores: list[tuple[str, Path, Path | None]] = []
    for harness in RESUME_ARGS:
        for profile in _candidates(profiles, harness):
            home = profile_home(profile)
            if home is None:
                continue
            sessions = None
            if harness == "pi":
                sessions = pi_sessions_dir(profile, home)
                if sessions is None:
                    continue
            if (harness, home, sessions) not in stores:
                stores.append((harness, home, sessions))
    return stores


def past_sessions(profiles: Mapping[str, Profile]) -> list[history.PastSession]:
    """Every session the profiles' history stores hold, within ``history``'s caps."""
    found = []
    for harness, home, sessions in history_homes(profiles):
        found.extend(history.scan(harness, home, sessions=sessions))
    return found


def merge(stored: Iterable[Resumable], past: Iterable[history.PastSession], *,
          now: int | None = None) -> list[Resumable]:
    """``stored`` sessions with their history added, then the past-only ones; newest first.

    A stored session keeps its record's folder (else takes the history's)
    and its age is that of the latest activity of either.
    """
    now = int(time.time()) if now is None else now
    sessions = {(item.harness, item.session_id): item for item in stored}
    for entry in past:
        key = (entry.harness, entry.session_id)
        known = sessions.get(key)
        if known is None:
            sessions[key] = Resumable(
                harness=entry.harness, session_id=entry.session_id, folder=entry.folder,
                status="", ts=entry.mtime, age_seconds=max(0, now - entry.mtime),
                title=entry.title, homes=(entry.home,),
            )
            continue
        if entry.home in known.homes:
            continue
        ts = max(known.ts, entry.mtime)
        sessions[key] = dataclasses.replace(
            known, folder=known.folder or entry.folder, title=known.title or entry.title,
            homes=(*known.homes, entry.home), ts=ts,
            age_seconds=known.age_seconds if ts == known.ts else max(0, now - ts),
        )
    return sorted(sessions.values(),
                  key=lambda item: (-item.ts, item.harness, item.session_id))


def all_sessions(profiles: Mapping[str, Profile], records: Iterable[dict] | None = None,
                 *, now: int | None = None) -> list[Resumable]:
    """The store's sessions and the profiles' past sessions, newest first; only reads."""
    now = int(time.time()) if now is None else now
    return merge(recent_sessions(records, now=now), past_sessions(profiles), now=now)


def _located(profiles: Mapping[str, Profile], session_id: str) -> list[history.PastSession]:
    return [
        entry for harness, home, store in history_homes(profiles)
        if (entry := history.locate(harness, home, session_id, sessions=store)) is not None
    ]


def lookup(profiles: Mapping[str, Profile], text: str,
           sessions: Iterable[Resumable] | None = None) -> Resumable:
    """The session ``text`` names among ``all_sessions``; a full id past the
    listing caps is looked up in each history store too.

    A full id the store holds is completed from history by id only, without
    listing every history store.
    """
    if sessions is None:
        stored = recent_sessions()
        exact = [item for item in stored if item.session_id == text]
        if exact:
            return find_session(merge(exact, _located(profiles, text)), text)
        sessions = merge(stored, past_sessions(profiles))
    else:
        sessions = list(sessions)
    if text and not any(item.session_id.startswith(text) for item in sessions):
        located = _located(profiles, text)
        if located:
            sessions = merge(sessions, located)
    return find_session(sessions, text)


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
            f"no session {text!r:.80} in the session store or the history of a profile's "
            "config dir; 'tongs-agents resume' lists the ones it can resume"
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
    ``MAX_SCAN_ENTRIES`` directory entries and never raises.

    A session read from history knows config dirs that hold it; another
    config dir is still searched for its id (``history.locate``), since a
    capped listing may have left it out there.
    """
    if session.homes:
        return home in session.homes or (
            history.locate(harness, home, session.session_id) is not None)
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


def resume_launch_index() -> Callable[[Resumable], list[launches.LaunchRecord]]:
    """``resume_launches`` over one read of the launch records, for a listing."""
    records = list(reversed(launches.list_records()))

    def resumed_by(session: Resumable) -> list[launches.LaunchRecord]:
        tail = resume_args(session.harness, session.session_id)
        return [record for record in records if record.harness == session.harness
                and tuple(record.args[-len(tail):]) == tail]

    return resumed_by


def resume_launches(session: Resumable) -> list[launches.LaunchRecord]:
    """The launch records that resumed ``session``, newest first."""
    return resume_launch_index()(session)


def match_profile(
    session: Resumable, profiles: Mapping[str, Profile], name: str | None = None,
    *, holds: Callable[[str, Path, Resumable], bool] = holds_session,
    resumed_by: Callable[[Resumable], list[launches.LaunchRecord]] = resume_launches,
) -> Profile:
    """The profile to resume ``session`` with; ResumeRefused when none or several fit.

    Of several profiles whose config dir holds it, the one the newest resume
    launch of the session used (``resumed_by``) is taken.
    """
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
    by_name = {profile.name: profile for profile in matched}
    for record in resumed_by(session):
        if record.profile in by_name:
            return by_name[record.profile]
    raise ResumeRefused(
        f"several {session.label} profiles have a config dir that holds session "
        f"{session.short_id} ({names}); {hint}"
    )


def listed_holds(harness: str, home: Path, session: Resumable) -> bool:
    """``holds_session`` for a listing: a session read from history is held
    only by the config dirs it was read from, so nothing more is searched."""
    if session.homes:
        return home in session.homes
    return holds_session(harness, home, session)


def origin(session: Resumable, profiles: Mapping[str, Profile],
           resumed_by: Callable[[Resumable], list[launches.LaunchRecord]] = resume_launches,
           holds: Callable[[str, Path, Resumable], bool] = holds_session) -> str:
    """The profile that would resume ``session``, else the config dir holding it."""
    try:
        return match_profile(session, profiles, holds=holds, resumed_by=resumed_by).name
    except ResumeRefused:
        pass
    if session.homes:
        return short_path(str(session.homes[0]))
    homes = {profile_home(profile) for profile in profiles.values()
             if profile.harness == session.harness}
    homes.add(Path(os.path.abspath(default_home(session.harness))))
    for home in sorted(path for path in homes if path is not None):
        if holds_session(session.harness, home, session):
            return short_path(str(home))
    return "?"


def resolve_dir(value: str) -> str:
    """DIR as an absolute path, ``~`` expanded, relative to the current directory."""
    return os.path.abspath(os.path.expanduser(value))


def plan(session: Resumable, profiles: Mapping[str, Profile],
         name: str | None = None, folder: str | None = None) -> Plan:
    """The profile and checked launch options that resume ``session``.

    ``folder`` (DIR, absolute) replaces the recorded one.
    """
    hint = f"give the folder to resume it in: tongs-agents resume {session.short_id} DIR"
    folder = folder or session.folder
    if not folder:
        raise ResumeRefused(f"session {session.short_id} has no recorded folder; {hint}")
    if not history.resumable_id(session.harness, session.session_id):
        raise ResumeRefused(
            f"session id {session.session_id!r:.80} cannot be resumed by id with "
            f"{session.label}"
        )
    if os.path.exists(folder) and not os.path.isdir(folder):
        raise ResumeRefused(f"{folder} of session {session.short_id} is not a folder; {hint}")
    if not os.path.isdir(folder):
        raise ResumeRefused(
            f"folder {folder} of session {session.short_id} does not exist (moved or "
            f"deleted); {hint}"
        )
    profile = match_profile(session, profiles, name)
    try:
        folder, extra = workspace_spawn.check_launch(
            profile, folder, resume_args(session.harness, session.session_id),
        )
    except LaunchError as error:
        raise ResumeRefused(str(error)) from None
    return Plan(session, profile, {"folder": folder, "args": extra},
                label=previous_label(session) or title_label(session.title))


def title_label(title: str) -> str | None:
    """``title`` as a launch label (``launches.check_label``), or None when it cannot be one."""
    text = history.clean_title(title).lstrip("-").strip()
    if len(text) > launches.MAX_LABEL_LENGTH:
        text = text[:launches.MAX_LABEL_LENGTH - 1].rstrip() + history.ELLIPSIS
    try:
        return launches.check_label(text)
    except LaunchError:
        return None


def previous_label(session: Resumable) -> str | None:
    """The sidebar label of the latest launch that ran ``session``, if any.

    A resume from the sidebar gives it to the new launch, so a renamed agent
    keeps its name. The launches its store records name are read newest
    first, then the launches that resumed it; a launch record that was
    pruned or never labelled gives nothing.
    """
    for record in session.records:
        launch = validated_launch(record.get("launch")) or {}
        launched = launches.get(launch.get("launch_id", ""))
        if launched is not None and launched.label:
            return launched.label
    for launched in resume_launches(session):
        if launched.label:
            return launched.label
    return None


# -- Live processes --

@dataclass(frozen=True)
class LiveProcess:
    """A live process of a session: its pid, where it runs and whether that is here.

    ``pid`` is None for a resume launch whose pane has not started yet.
    ``written`` is set instead (seconds since its transcript changed) for a
    session no store record knows whose history was written moments ago: a
    process may still write it.
    """

    pid: int | None
    where: str
    here: bool
    written: int | None = None


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
    if found is None and not session.records and session.ts > 0:
        written = int(time.time()) - session.ts
        if written < RECENT_WRITE_SECONDS:
            found = LiveProcess(None, "elsewhere", False, written=max(0, written))
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
    if live.written is not None:
        return (
            f"session {session.short_id} was written {format_age(live.written)} ago "
            "and may still run in another terminal; two processes on one session "
            "conflict, so quit that one before you type in the new pane"
        )
    return (
        f"session {session.short_id} still runs {_as_pid(live)} {live.where}; "
        "two processes on one session conflict, so quit that one before you type in "
        "the new pane (it was not stopped)"
    )


def here_refusal(session: Resumable, live: LiveProcess) -> str:
    """Why a session already running in this workspace is not resumed again."""
    text = (
        f"session {session.short_id} already runs in this workspace {_as_pid(live)}; "
        "show its pane instead (select its row in the sidebar, or run "
        f"'tongs-agents focus {session.session_id}')"
    )
    if live.pid is None:
        text += f"; if that pane failed to start, retry in {PENDING_LAUNCH_SECONDS} seconds"
    return text


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


def origins(sessions: Iterable[Resumable],
            profiles: Mapping[str, Profile]) -> dict[tuple[str, str], str]:
    """(harness, session id) to the ``PROFILE/CONFIG`` text of each session."""
    resumed_by = resume_launch_index()
    return {(session.harness, session.session_id):
            origin(session, profiles, resumed_by, listed_holds)
            for session in sessions}


def status_text(session: Resumable) -> str:
    return session.status or NO_STATUS


def table(sessions: Iterable[Resumable], profiles: Mapping[str, Profile]) -> str:
    """The listing ``tongs-agents resume`` prints."""
    sessions = list(sessions)
    named = origins(sessions, profiles)
    header = ("HARNESS", "SESSION", "PROFILE/CONFIG", "STATUS", "AGE", "FOLDER", "TITLE")
    rows = [header]
    for session in sessions:
        rows.append((
            session.harness, session.session_id,
            named[(session.harness, session.session_id)],
            status_text(session), format_age(session.age_seconds),
            short_path(session.folder) or "?", session.title,
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
    sessions = all_sessions(profiles)
    if not sessions:
        print("No sessions to resume: neither the session store nor the history in "
              "a profile's config dir holds one.")
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
        folder = resolve_dir(args.folder) if args.folder is not None else None
    except OSError:
        return _error(f"cannot resolve {args.folder!r:.80}: the current directory is "
                      "unreadable; give an absolute path", EXIT_USAGE)
    try:
        target = lookup(profiles, args.session_id)
        resumed = plan(target, profiles, args.profile, folder)
    except ResumeRefused as refused:
        return _error(str(refused), EXIT_USAGE)
    here, multiplexer = _workspace_here(args, settings)
    live = live_process(target, here, multiplexer)
    if live is not None and live.here:
        return _error(here_refusal(target, live), EXIT_USAGE)
    if live is not None:
        print(f"tongs-agents resume: warning: {warning_text(target, live)}", file=sys.stderr)
    print(f"tongs-agents resume: resuming {resumed.summary}", file=sys.stderr)
    options = dict(resumed.options)
    if resumed.label is not None:
        options["label"] = resumed.label
    if os.environ.get("ZELLIJ") is not None:
        return run_command._inside("zellij", args, resumed.profile, options)
    if os.environ.get("TMUX"):
        return run_command._inside("tmux", args, resumed.profile, options)
    return run_command._outside(args, settings, resumed.profile, options)
