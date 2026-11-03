"""The one spawn path for agent panes, plus the workspace's runtime files.

Every agent pane, whether started by ``tongs-agents workspace`` or
``tongs-agents run`` inside or outside a session, by the sidebar from a spawn
request, its Add agent dialog or the palette, is created by ``spawn_launch``:
a pending launch record names the profile, and the pane runs
``<python> -m tongs_agents.launch <profile> --launch-id <id>``, which claims
the record and execs the profile. A launch may carry a folder and extra
arguments chosen by the maintainer (``check_launch`` validates them first).

A spawn request (``runtime_dir()/requests/<token>.json``) hands pending
launches from the workspace command to the sidebar of a session that does
not exist yet. It is written 0600 with ``O_EXCL`` and consumed once: the
sidebar renames it away before reading, so a second sidebar with the same
token spawns nothing. The rendered layout for that session lives beside it in
``runtime_dir()/layouts/<token>.kdl``.

A request nobody consumes (a printed command that was never run, or the
tongs plugin's modal opened and closed) expires after
``REQUEST_MAX_AGE`` seconds: writing a new request first removes expired
requests, their layouts and the pending launches they name.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from tongs_agents import launches, paths
from tongs_agents.launches import LaunchError, LaunchRecord
from tongs_agents.multiplexers.base import Multiplexer, MultiplexerError, PaneRef
from tongs_agents.profiles import Profile, folder_problem

LAUNCH_MODULE = "tongs_agents.launch"
SIDEBAR_MODULE = "tongs_agents.commands.sidebar"
SIDEBAR_PANE_NAME = "tongs-agents-sidebar"
REQUEST_SCHEMA = 1
REQUESTS_DIR = "requests"
LAYOUTS_DIR = "layouts"
MAX_REQUEST_BYTES = 64 * 1024
MAX_REQUEST_LAUNCHES = 64
MAX_SESSION_LENGTH = 1024
TOKEN_RE = re.compile(r"^[0-9a-f]{32}\Z")
REQUEST_MAX_AGE = 24 * 3600


class SpawnRequestError(Exception):
    """A spawn request that cannot be written or that is refused on read."""


@dataclass(frozen=True)
class SpawnRequest:
    session: str
    launches: tuple[str, ...]


def is_token(value) -> bool:
    return isinstance(value, str) and TOKEN_RE.match(value) is not None


def new_token() -> str:
    return secrets.token_hex(16)


def launcher_argv(profile: str, launch_id: str) -> list[str]:
    return [sys.executable, "-m", LAUNCH_MODULE, profile, "--launch-id", launch_id]


def sidebar_argv(token: str | None = None) -> list[str]:
    argv = [sys.executable, "-m", SIDEBAR_MODULE, "--compact"]
    if token is not None:
        argv += ["--spawn-request", token]
    return argv


class LaunchOptionError(LaunchError):
    """A folder or extra arguments a profile cannot be started with."""


def check_launch(profile: Profile, folder: str | None = None,
                 args: Iterable[str] = ()) -> tuple[str | None, tuple[str, ...]]:
    """The validated folder and extra arguments for one launch of ``profile``.

    The folder must be an absolute existing directory (and, for a sandboxed
    profile that uploads its workspace, not a home or credentials folder).
    Raises LaunchOptionError naming the problem.
    """
    try:
        extra = launches.check_args(args if isinstance(args, str) else list(args))
        if folder is not None:
            launches.check_folder(folder)
    except LaunchError as error:
        raise LaunchOptionError(str(error)) from None
    if folder is not None:
        problem = folder_problem(profile, Path(folder))
        if problem is not None:
            raise LaunchOptionError(f"profile {profile.name}: folder {problem}")
    return folder, extra


def create_launches(
    multiplexer: str, session: str, profiles: Iterable[Profile], *,
    folder: str | None = None, args: Iterable[str] = (),
) -> list[LaunchRecord]:
    """Pending launch records, one per profile, for panes not spawned yet.

    ``folder`` and ``args`` apply to every one of them. When one cannot be
    written, those already written are discarded.
    """
    records = []
    profiles = list(profiles)
    for profile in profiles:
        folder, args = check_launch(profile, folder, args)
    try:
        for profile in profiles:
            records.append(launches.create_pending(
                profile, session, multiplexer, folder=folder, args=args,
            ))
    except BaseException:
        discard_launches(records)
        raise
    return records


def spawn_launch(mux: Multiplexer, record: LaunchRecord) -> PaneRef:
    """Start the launcher for a pending record in a new pane of its session."""
    return mux.spawn(
        record.session,
        launcher_argv(record.profile, record.launch_id),
        name=record.profile,
        focus=False,
    )


def spawn_profiles(
    mux: Multiplexer, session: str, profiles: Iterable[Profile], *,
    folder: str | None = None, args: Iterable[str] = (),
) -> list[LaunchRecord]:
    """Create a pending record and spawn one launcher pane per profile, in order.

    ``folder`` and ``args`` (see ``check_launch``) apply to every profile and
    are checked for all of them before any pane starts. Raises the first
    MultiplexerError or LaunchError; panes spawned before it stay up, and the
    record of the pane that failed to start is discarded.
    """
    records = []
    profiles = list(profiles)
    for profile in profiles:
        folder, args = check_launch(profile, folder, args)
    for profile in profiles:
        record = launches.create_pending(
            profile, session, mux.name, folder=folder, args=args,
        )
        try:
            spawn_launch(mux, record)
        except BaseException:
            discard_launches([record])
            raise
        records.append(record)
    return records


def discard_launches(records: Iterable[LaunchRecord]) -> None:
    """Remove pending records no pane will claim; never raises for a missing one."""
    for record in records:
        try:
            launches.discard(record.launch_id)
        except LaunchError:
            pass


def has_sidebar(mux: Multiplexer, session: str) -> bool:
    """True when a live (not exited) sidebar pane is listed in ``session``."""
    return any(
        pane.title == SIDEBAR_PANE_NAME and not pane.exited
        for pane in mux.list_panes(session)
    )


def ensure_sidebar(mux: Multiplexer, session: str) -> PaneRef | None:
    """Add a sidebar pane unless one is listed; return the new pane, if any."""
    if has_sidebar(mux, session):
        return None
    return mux.spawn(session, sidebar_argv(), name=SIDEBAR_PANE_NAME, focus=False)


def _runtime_subdir(name: str, *, create: bool) -> Path:
    directory = paths.runtime_dir(create=create) / name
    if create:
        paths.ensure_private_dir(directory)
    return directory


def request_path(token: str) -> Path:
    if not is_token(token):
        raise SpawnRequestError(f"invalid spawn request token {token!r:.80}")
    return _runtime_subdir(REQUESTS_DIR, create=False) / f"{token}.json"


def layout_path(token: str) -> Path:
    if not is_token(token):
        raise SpawnRequestError(f"invalid spawn request token {token!r:.80}")
    return _runtime_subdir(LAYOUTS_DIR, create=False) / f"{token}.kdl"


def write_private_file(path: Path, data: bytes) -> Path:
    """Create ``path`` at 0600; never replace, follow or reuse an existing entry."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def write_spawn_request(session: str, launch_ids: Iterable[str]) -> str:
    """Write a spawn request for ``session`` and return its token."""
    ids = list(launch_ids)
    if not all(launches.is_launch_id(launch_id) for launch_id in ids):
        raise SpawnRequestError("spawn request holds an invalid launch id")
    if len(ids) > MAX_REQUEST_LAUNCHES:
        raise SpawnRequestError(f"more than {MAX_REQUEST_LAUNCHES} launches in one request")
    _runtime_subdir(REQUESTS_DIR, create=True)
    prune_spawn_requests()
    token = new_token()
    payload = json.dumps(
        {"schema_version": REQUEST_SCHEMA, "session": session, "launches": ids},
        separators=(",", ":"),
    ).encode("utf-8")
    write_private_file(request_path(token), payload)
    return token


def write_layout(token: str, text: str) -> Path:
    _runtime_subdir(LAYOUTS_DIR, create=True)
    return write_private_file(layout_path(token), text.encode("utf-8"))


def _discard(path: Path) -> None:
    try:
        if stat.S_ISREG(path.lstat().st_mode):
            path.unlink()
    except OSError:
        pass


def discard_runtime_files(token: str) -> None:
    """Remove the request and layout of ``token``, ignoring missing files."""
    _discard(request_path(token))
    _discard(layout_path(token))


def _expired_tokens(directory: Path | None, suffix: str, cutoff: float) -> list[str]:
    """Tokens of the regular ``<token><suffix>`` files last modified before ``cutoff``."""
    tokens = []
    if directory is None:
        return tokens
    try:
        entries = list(directory.iterdir())
    except OSError:
        return tokens
    for entry in entries:
        token = entry.name[:-len(suffix)] if entry.name.endswith(suffix) else ""
        if not is_token(token):
            continue
        try:
            info = entry.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
            tokens.append(token)
    return tokens


def _discard_pending(request: SpawnRequest) -> None:
    for launch_id in request.launches:
        try:
            record = launches.get(launch_id)
            if (
                record is not None
                and record.state == "pending"
                and record.session == request.session
            ):
                launches.discard(launch_id)
        except LaunchError:
            pass


def prune_spawn_requests(max_age: float = REQUEST_MAX_AGE) -> int:
    """Remove spawn requests and layouts older than ``max_age`` seconds.

    The pending launches an expired request names go with it; a launch a pane
    already claimed is kept. Returns the number of requests removed and never
    raises.
    """
    cutoff = time.time() - max_age
    removed = 0
    try:
        requests = _runtime_subdir(REQUESTS_DIR, create=False)
        layouts = _runtime_subdir(LAYOUTS_DIR, create=False)
    except OSError:
        return removed
    if not paths.is_private_dir(requests):
        requests = None
    if not paths.is_private_dir(layouts):
        layouts = None
    for token in _expired_tokens(requests, ".json", cutoff):
        try:
            request = consume_spawn_request(token)
        except (SpawnRequestError, OSError):
            request = None
        if request is not None:
            _discard_pending(request)
            removed += 1
        if layouts is not None:
            _discard(layout_path(token))
    for token in _expired_tokens(layouts, ".kdl", cutoff):
        _discard(layout_path(token))
    return removed


def _parse_request(data) -> SpawnRequest:
    if not isinstance(data, dict) or data.get("schema_version") != REQUEST_SCHEMA:
        raise SpawnRequestError("unrecognized spawn request")
    session = data.get("session")
    ids = data.get("launches")
    if (
        not isinstance(session, str)
        or not 0 < len(session) <= MAX_SESSION_LENGTH
        or not session.isprintable()
        or not isinstance(ids, list)
        or len(ids) > MAX_REQUEST_LAUNCHES
        or not all(launches.is_launch_id(launch_id) for launch_id in ids)
    ):
        raise SpawnRequestError("unrecognized spawn request")
    return SpawnRequest(session, tuple(dict.fromkeys(ids)))


def consume_spawn_request(token: str) -> SpawnRequest | None:
    """Read and delete the request for ``token``; None when it is already gone.

    The request is renamed to a private name first, so only one reader ever
    gets it. Symlinks, foreign files and oversized requests are refused.
    """
    path = request_path(token)
    directory = path.parent
    if not os.path.lexists(directory):
        return None
    if not paths.is_private_dir(directory):
        raise SpawnRequestError(f"{directory}: not a private directory")
    claimed = directory / f".{token}.{os.getpid()}.{new_token()[:8]}.consumed"
    try:
        os.rename(path, claimed)
    except FileNotFoundError:
        return None
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            descriptor = os.open(claimed, flags)
        except OSError as error:
            raise SpawnRequestError(f"cannot read spawn request: {error.strerror}") from None
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise SpawnRequestError("spawn request is not a regular file")
            if os.name != "nt" and info.st_uid != os.getuid():
                raise SpawnRequestError("spawn request is owned by another user")
            raw = stream.read(MAX_REQUEST_BYTES + 1)
    finally:
        try:
            os.unlink(claimed)
        except OSError:
            pass
    if len(raw) > MAX_REQUEST_BYTES:
        raise SpawnRequestError("spawn request exceeds size limit")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise SpawnRequestError("spawn request is not JSON") from None
    return _parse_request(data)


def spawn_requested(
    mux: Multiplexer,
    session: str,
    token: str,
    on_spawned: Callable[[str, PaneRef], None] | None = None,
) -> list[str]:
    """Consume ``token``'s request and spawn its pending launches in ``session``.

    Returns one message per launch that could not be spawned. The request and
    its layout are removed whatever happens.
    """
    try:
        request = consume_spawn_request(token)
    except SpawnRequestError as error:
        return [str(error)]
    finally:
        try:
            _discard(layout_path(token))
        except SpawnRequestError:
            pass
    if request is None:
        return []
    if request.session != session:
        return [
            f"spawn request is for session {request.session!r:.80}, "
            f"not {session!r:.80}; nothing spawned"
        ]
    failures = []
    for launch_id in request.launches:
        record = launches.get(launch_id)
        if record is None:
            failures.append(f"launch {launch_id}: no launch record")
            continue
        if record.state != "pending":
            continue
        if record.session != session or record.multiplexer != mux.name:
            failures.append(f"launch {launch_id}: belongs to another session")
            continue
        try:
            ref = spawn_launch(mux, record)
        except (MultiplexerError, LaunchError) as error:
            detail = getattr(error, "detail", None) or str(error)
            failures.append(f"profile {record.profile}: {detail}")
            continue
        if on_spawned is not None:
            on_spawned(record.launch_id, ref)
    return failures
