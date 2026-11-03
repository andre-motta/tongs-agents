"""Launch records: one owned record per agent pane started by tongs-agents.

A spawner creates a pending record, and the launcher running inside the pane
claims it exactly once with its own pid, process start token and pane
identity before it execs the agent. A second claim of the same id (a
resurrected pane, for example) gets a fresh record that names the old one in
``replaces``. Records live in ``state_dir()/launches/<launch_id>.json``:
written atomically at 0600, read without following symlinks, 64 KiB at most.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from tongs_agents import discovery, hook, paths
from tongs_agents.multiplexers.base import PaneRef
from tongs_agents.profiles import HARNESSES, PROFILE_NAME_RE, Profile

SCHEMA_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024
MAX_TEXT_LENGTH = 1024
LAUNCH_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
STATES = ("pending", "running")


class LaunchError(Exception):
    """A launch record that cannot be created, found or claimed."""


@dataclass(frozen=True)
class LaunchRecord:
    launch_id: str
    profile: str
    harness: str
    multiplexer: str
    session: str
    created: int
    socket_dir: str | None = None
    pane_id: str | None = None
    pid: int | None = None
    process_start: str | None = None
    state: str = "pending"
    replaces: str | None = None
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict:
        data = asdict(self)
        return {"schema_version": data.pop("schema_version"), **data}


def is_launch_id(value) -> bool:
    return isinstance(value, str) and bool(LAUNCH_ID_RE.match(value))


def _text(value, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_TEXT_LENGTH
        and value.isprintable()
    )


def _parse(data) -> LaunchRecord | None:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    pid = data.get("pid")
    created = data.get("created")
    if not (
        is_launch_id(data.get("launch_id"))
        and isinstance(data.get("profile"), str)
        and PROFILE_NAME_RE.match(data["profile"])
        and data.get("harness") in HARNESSES
        and _text(data.get("multiplexer"))
        and _text(data.get("session"))
        and _text(data.get("socket_dir"), optional=True)
        and _text(data.get("pane_id"), optional=True)
        and (pid is None or (type(pid) is int and pid > 0))
        and _text(data.get("process_start"), optional=True)
        and data.get("state") in STATES
        and (data.get("replaces") is None or is_launch_id(data["replaces"]))
        and type(created) is int
    ):
        return None
    fields = LaunchRecord.__dataclass_fields__
    return LaunchRecord(**{key: data.get(key) for key in fields})


def _record_path(launch_id: str) -> Path:
    if not is_launch_id(launch_id):
        raise LaunchError(f"invalid launch id: {launch_id!r}")
    return paths.launches_dir() / f"{launch_id}.json"


def _read(path: Path) -> LaunchRecord | None:
    # O_NONBLOCK keeps a FIFO planted at the record path from hanging the
    # open; it has no effect on the regular file checked below.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        if os.name != "nt" and info.st_uid != os.getuid():
            return None
        raw = stream.read(MAX_RECORD_BYTES + 1)
    if len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        record = _parse(json.loads(raw))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if record is None or path.name != f"{record.launch_id}.json":
        return None
    return record


def _write(record: LaunchRecord) -> LaunchRecord:
    directory = paths.launches_dir(create=True)
    path = _record_path(record.launch_id)
    payload = json.dumps(record.to_json(), separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise LaunchError("launch record exceeds size limit")
    if os.path.lexists(path) and not stat.S_ISREG(path.lstat().st_mode):
        raise LaunchError(f"{path}: refusing to replace a non-regular file")
    descriptor, temp_name = tempfile.mkstemp(
        dir=directory, prefix=".launch-tmp-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return record


def _check_text(name: str, value: str | None, *, optional: bool = False) -> None:
    if not _text(value, optional=optional):
        raise LaunchError(f"invalid {name}: {value!r}")


def create_pending(profile: Profile, session: str, multiplexer: str) -> LaunchRecord:
    """Write a pending record for a pane about to run ``profile``."""
    _check_text("session", session)
    _check_text("multiplexer", multiplexer)
    record = LaunchRecord(
        launch_id=secrets.token_hex(16),
        profile=profile.name,
        harness=profile.harness,
        multiplexer=multiplexer,
        session=session,
        created=int(time.time()),
    )
    return _write(record)


def discard(launch_id: str) -> None:
    """Remove a pending record whose pane was never started; claimed ones stay."""
    path = _record_path(launch_id)
    record = _read(path)
    if record is not None and record.state != "pending":
        return
    for entry in (path, path.with_name(f".{launch_id}.claim")):
        try:
            if stat.S_ISREG(entry.lstat().st_mode):
                entry.unlink()
        except OSError:
            pass


def get(launch_id: str) -> LaunchRecord | None:
    """The stored record for ``launch_id``, or None when absent or invalid."""
    if not is_launch_id(launch_id):
        return None
    return _read(paths.launches_dir() / f"{launch_id}.json")


def _claimed(record: LaunchRecord, pid: int, process_start: str | None,
             pane: PaneRef | None) -> LaunchRecord:
    changes = {"pid": pid, "process_start": process_start, "state": "running"}
    if pane is not None:
        changes.update(
            multiplexer=pane.multiplexer,
            session=pane.session or record.session,
            pane_id=pane.pane_id,
            socket_dir=pane.socket_dir,
        )
    return replace(record, **changes)


def claim(launch_id: str, pid: int, process_start: str | None,
          pane: PaneRef | None) -> LaunchRecord:
    """Claim a pending record once; a claimed id yields a fresh replacing record."""
    if type(pid) is not int or pid <= 0:
        raise LaunchError(f"invalid pid: {pid!r}")
    _check_text("process start", process_start, optional=True)
    if pane is not None:
        for name in ("multiplexer", "pane_id"):
            _check_text(f"pane {name}", getattr(pane, name))
        if pane.session:
            _check_text("pane session", pane.session)
        _check_text("pane socket dir", pane.socket_dir, optional=True)
    path = _record_path(launch_id)
    record = _read(path)
    if record is None:
        raise LaunchError(f"no launch record for id {launch_id}")
    marker = path.with_name(f".{launch_id}.claim")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    won = False
    if record.state == "pending":
        try:
            os.close(os.open(marker, flags, 0o600))
            won = True
        except FileExistsError:
            pass
    if won:
        current = _read(path)
        if current is not None and current.state == "pending":
            return _write(_claimed(current, pid, process_start, pane))
    fresh = replace(
        record,
        launch_id=secrets.token_hex(16),
        created=int(time.time()),
        replaces=launch_id,
        pane_id=None,
        socket_dir=None,
    )
    return _write(_claimed(fresh, pid, process_start, pane))


def claims_session(record: LaunchRecord, data: dict) -> bool:
    """True when ``record`` claims the process of the session record ``data``.

    The reader-side check of #74: a session record names a launch only when
    its process is the launch's claimed pid and start token, or runs under
    that process. An exact match needs no process lookup; otherwise the
    session's process must still be alive with its stored start token.
    """
    if record.state != "running" or record.pid is None or not isinstance(data, dict):
        return False
    pid_text = data.get("pid")
    start = data.get("process_start") or ""
    if not (isinstance(pid_text, str) and pid_text.isascii() and pid_text.isdecimal()):
        return False
    if not isinstance(start, str):
        return False
    pid = int(pid_text)
    if pid == record.pid:
        return not record.process_start or start == record.process_start
    process = discovery.get_process_info(pid)
    if process is None or process.start_token != start:
        return False
    return hook.process_descends_from(process, record.pid, record.process_start)


def list_records(session: str | None = None) -> list[LaunchRecord]:
    """Valid records, oldest first, optionally only those in ``session``."""
    try:
        directory = paths.launches_dir()
        candidates = sorted(directory.glob("*.json"))
    except OSError:
        return []
    records = [record for record in map(_read, candidates) if record is not None]
    if session is not None:
        records = [record for record in records if record.session == session]
    return sorted(records, key=lambda record: (record.created, record.launch_id))


def _alive(record: LaunchRecord) -> bool:
    if record.pid is None:
        return False
    info = discovery.get_process_info(record.pid)
    if info is None:
        return False
    return record.process_start is None or info.start_token == record.process_start


def _prune_leftovers(cutoff: float) -> None:
    """Unlink interrupted temp files and claim markers without a valid record."""
    try:
        entries = list(paths.launches_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if name.startswith(".launch-tmp-"):
            stale = True
        elif name.startswith(".") and name.endswith(".claim"):
            stale = get(name[1:-len(".claim")]) is None
        else:
            continue
        try:
            info = entry.lstat()
            if stale and stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def prune(max_age_days: int = 7) -> int:
    """Delete records older than ``max_age_days`` whose process is gone.

    Temp files from interrupted writes and orphaned claim markers older than
    the same cutoff go too. Returns the number of records removed.
    """
    cutoff = time.time() - max_age_days * 86400
    removed = 0
    for record in list_records():
        if record.created > cutoff or _alive(record):
            continue
        path = paths.launches_dir() / f"{record.launch_id}.json"
        for entry in (path, path.with_name(f".{record.launch_id}.claim")):
            try:
                entry.unlink()
            except FileNotFoundError:
                pass
        removed += 1
    _prune_leftovers(cutoff)
    return removed
