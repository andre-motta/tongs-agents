"""Session store maintenance and the writer for imported session records.

Readers (``discovery.read_sessions``, ``read_session_records``, MCP ``fleet_*``
tools, ``tongs-agents status``, focus and control) never write. Everything
that changes the store outside the hook goes through this module:

- ``maintain_store`` records agent processes no hook reported and removes
  ended, exited and stale records. Its one owner is ``SessionFeed`` (the
  sidebar and the monitor); ``tongs-agents cleanup`` and the ``fleet_cleanup``
  MCP tool call it explicitly. See docs/sessions.md.
- ``import_session_record`` writes a record produced outside the hook, such as
  a sandbox bridge, with the hook's validation, locks and atomic write.
"""

from __future__ import annotations

import contextlib
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tongs_agents import discovery, paths
from tongs_agents.models import SCHEMA_VERSION

MIN_INTERVAL_SECONDS = 5.0
OVERDUE_SECONDS = 3600.0
FUTURE_TOLERANCE_SECONDS = 60.0
STAMP_NAME = ".fleet-maintenance.stamp"
MAX_STAMP_BYTES = 64

SKIPPED_BUSY = "busy"
SKIPPED_RECENT = "recent"
SKIPPED_UNAVAILABLE = "unavailable"

__all__ = [
    "MIN_INTERVAL_SECONDS",
    "MaintenanceResult",
    "import_session_record",
    "maintain_store",
    "maintenance_overdue",
]


@dataclass(frozen=True)
class MaintenanceResult:
    """What one ``maintain_store`` call did.

    ``ran`` is False when it skipped; ``skipped`` then says why: ``busy``
    (another maintainer holds the lock), ``recent`` (the last run is younger
    than the minimum interval) or ``unavailable`` (the store cannot be used).
    """

    ran: bool
    removed: int = 0
    skipped: str = ""


def _stamp_path() -> Path:
    return paths.sessions_dir() / STAMP_NAME


def _read_stamp() -> float | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(_stamp_path(), flags)
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_STAMP_BYTES + 1)
        value = float(raw.decode("ascii"))
    except (OSError, ValueError, UnicodeError):
        return None
    if len(raw) > MAX_STAMP_BYTES or not math.isfinite(value):
        return None
    return value


def _write_stamp(now: float) -> None:
    path = _stamp_path()
    if path.is_symlink():
        raise OSError("unsafe maintenance stamp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(f"{now:.3f}".encode("ascii"))


def maintenance_overdue(*, clock: Callable[[], float] = time.time) -> bool:
    """True when the store exists and its last maintenance run is missing or stale.

    Stale means older than ``OVERDUE_SECONDS`` or stamped more than
    ``FUTURE_TOLERANCE_SECONDS`` in the future (the stamp is rounded to the
    millisecond). Read only: for ``tongs-agents status`` to suggest
    ``tongs-agents cleanup``.
    """
    try:
        if not paths.sessions_dir().is_dir():
            return False
    except (OSError, ValueError):
        return False
    last = _read_stamp()
    if last is None:
        return True
    age = clock() - last
    return age > OVERDUE_SECONDS or age < -FUTURE_TOLERANCE_SECONDS


def _is_recent(last: float | None, now: float, min_interval: float) -> bool:
    return last is not None and 0 <= now - last < min_interval


def maintain_store(
    *,
    force: bool = False,
    min_interval: float = MIN_INTERVAL_SECONDS,
    clock: Callable[[], float] = time.time,
) -> MaintenanceResult:
    """Run process discovery and cleanup once, under the maintenance lock.

    Without ``force`` a run younger than ``min_interval`` seconds (from the
    stamp any maintainer leaves) is skipped, so several sidebars and monitors
    polling the same store share one run. A held lock is never waited for.
    Each unlink takes the record's lock and re-reads it first, so a hook write
    racing the cleanup is kept. Only failing to take the maintenance lock is
    ``busy``; any other store error, including a record lock timing out
    during the run, is ``unavailable``. A run that fails after writing the
    stamp still counts toward the interval. Never raises for store errors.
    """
    try:
        if not discovery._store_available(create=True):
            return MaintenanceResult(False, skipped=SKIPPED_UNAVAILABLE)
        with contextlib.ExitStack() as held:
            try:
                held.enter_context(discovery.store_maintenance_lock())
            except TimeoutError:
                return MaintenanceResult(False, skipped=SKIPPED_BUSY)
            now = clock()
            if not force and _is_recent(_read_stamp(), now, min_interval):
                return MaintenanceResult(False, skipped=SKIPPED_RECENT)
            _write_stamp(now)
            discovery.discover_processes()
            removed = discovery._cleanup_stale_sessions()
    except (OSError, ValueError):
        return MaintenanceResult(False, skipped=SKIPPED_UNAVAILABLE)
    return MaintenanceResult(True, removed=removed)


def import_session_record(record: dict) -> bool:
    """Write a schema 1 session record produced outside the hook.

    For bridges that relay a session the hook cannot see, such as an agent in
    a sandbox. The record gets the hook's treatment: the same validation, the
    session identity lock, the record lock with its pid and sequence checks,
    and an atomic replace. Returns False when it is refused or not written.
    The caller's dict is never changed. An imported record is maintained like
    any other: once its pid no longer names the recorded live local process
    the next ``maintain_store`` removes it, and a pid-less record must be
    refreshed within ``discovery.PIDLESS_RETENTION_SECONDS``.
    """
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        return False
    harness_id = record.get("harness_id")
    session_id = record.get("session_id")
    if not isinstance(harness_id, str) or not isinstance(session_id, str):
        return False
    try:
        with discovery.session_identity_lock(harness_id, session_id):
            return discovery.write_session_record(dict(record))
    except (OSError, ValueError):
        return False
