"""The one session source for UI modules, plus pure view helpers."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable

from tongs_agents import discovery, launches, session_store
from tongs_agents.launches import LaunchRecord
from tongs_agents.models import (
    SORT_KEYS,
    FleetSession,
    SessionStatus,
    parse_session,
)
from tongs_agents.models import sort_sessions as _sort_sessions
from tongs_agents.multiplexers.base import (
    Multiplexer,
    MultiplexerError,
    PaneInfo,
    PaneRef,
)

__all__ = [
    "LAUNCH_SOURCE",
    "NOTIFY_SOURCE",
    "SORT_KEYS",
    "SessionFeed",
    "WorkspaceFeed",
    "launch_session",
    "attention_rows",
    "filter_sessions",
    "next_sort_key",
    "search_sessions",
    "sort_sessions",
    "visible_sessions",
]

Reader = Callable[[], Iterable[dict]]
Maintainer = Callable[[], object]
LaunchReader = Callable[[str], Iterable[LaunchRecord]]

LAUNCH_SOURCE = "launch"
NOTIFY_SOURCE = "notify"
LAUNCH_ID_PREFIX = "launch:"


class SessionFeed:
    """Read session records and parse them into FleetSession objects.

    The feed is the store's one cleanup owner: reading the store (no
    ``reader`` given) first runs ``session_store.maintain_store``, which
    skips itself while another sidebar or monitor ran it recently. A custom
    ``reader`` reads without maintenance unless a ``maintainer`` is given.
    Both defaults are looked up at call time so tests can replace them.
    """

    def __init__(
        self, reader: Reader | None = None, *, maintainer: Maintainer | None = None
    ) -> None:
        self._reader = reader
        self._maintainer = maintainer

    def maintain(self) -> None:
        maintainer = self._maintainer
        if maintainer is None:
            if self._reader is not None:
                return
            maintainer = session_store.maintain_store
        maintainer()

    def snapshot(self) -> list[FleetSession]:
        self.maintain()
        reader = self._reader or discovery.read_sessions
        return [parse_session(record) for record in reader()]


def _pane_state(pane: PaneInfo, pending: bool) -> tuple[str, SessionStatus]:
    if pane.exited:
        if pane.exit_status is None:
            return "exited", SessionStatus.ERROR
        status = SessionStatus.ENDED if pane.exit_status == 0 else SessionStatus.ERROR
        return f"exited {pane.exit_status}", status
    return ("starting" if pending else "running"), SessionStatus.DISCOVERED


def launch_session(record: LaunchRecord, pane: PaneInfo, now: float) -> FleetSession:
    """A row for a launched pane that no hook record names: profile and pane state."""
    detail, status = _pane_state(pane, record.state == "pending")
    terminal_env = {
        "ZELLIJ_SESSION_NAME": pane.ref.session,
        "ZELLIJ_PANE_ID": pane.ref.pane_id,
    }
    if pane.ref.socket_dir:
        terminal_env["ZELLIJ_SOCKET_DIR"] = pane.ref.socket_dir
    return FleetSession(
        session_id=record.launch_id,
        repo=record.profile,
        cwd="",
        status=status,
        detail=detail,
        ts=record.created,
        started=record.created,
        pid=str(record.pid or ""),
        terminal=pane.ref.multiplexer,
        terminal_env=terminal_env,
        source=LAUNCH_SOURCE,
        tool="",
        age_seconds=max(0, int(now) - record.created),
        agent=record.harness,
        canonical_id=f"{LAUNCH_ID_PREFIX}{record.launch_id}",
        process_start=record.process_start or "",
        launch_id=record.launch_id,
        profile_id=record.profile,
    )


def _alive(record: LaunchRecord) -> bool:
    if record.pid is None:
        return False
    info = discovery.get_process_info(record.pid)
    if info is None:
        return False
    return record.process_start is None or info.start_token == record.process_start


def _same_dir(left: str, right: str | None) -> bool:
    return right is not None and os.path.normpath(left) == os.path.normpath(right)


class WorkspaceFeed(SessionFeed):
    """The sessions of one Zellij session: its hook sessions and launched panes.

    The sidebar is per-session; ``tongs-agents monitor`` stays fleet-wide.
    A hook session is kept only when its captured ``ZELLIJ_SESSION_NAME`` is
    ``session`` and, when it captured a socket dir, that dir is
    ``socket_dir``. A launch record joins its hook session, or the session
    ``tongs-agents notify`` reports for it, through the launch id. Records
    with no such session show their pane state from the multiplexer:
    running, starting (launcher not claimed yet) or exited with its status.
    A claimed record whose process is gone while its pane id runs something
    else belongs to an earlier session of the same name and is skipped, as
    are records replaced by a later claim.
    """

    def __init__(
        self,
        session: str,
        multiplexer: Multiplexer,
        reader: Reader | None = None,
        *,
        maintainer: Maintainer | None = None,
        launch_reader: LaunchReader | None = None,
        alive: Callable[[LaunchRecord], bool] = _alive,
        clock: Callable[[], float] = time.time,
        socket_dir: str | None = None,
    ) -> None:
        super().__init__(reader, maintainer=maintainer)
        self.session = session
        self.multiplexer = multiplexer
        self.socket_dir = socket_dir
        self._launch_reader = launch_reader
        self._alive = alive
        self._clock = clock
        self._lock = threading.Lock()
        self._spawned: dict[str, PaneRef] = {}

    def note_spawned(self, launch_id: str, ref: PaneRef) -> None:
        """Remember the pane of a launch this process spawned before it is claimed."""
        with self._lock:
            self._spawned[launch_id] = ref

    def snapshot(self) -> list[FleetSession]:
        sessions = [
            session for session in super().snapshot() if self.in_session(session)
        ]
        return sessions + self.launch_rows(sessions)

    def in_session(self, session: FleetSession) -> bool:
        """True when a hook session's captured Zellij identity is this session."""
        env = session.terminal_env if isinstance(session.terminal_env, dict) else {}
        if env.get("ZELLIJ_SESSION_NAME") != self.session:
            return False
        captured = env.get("ZELLIJ_SOCKET_DIR")
        if not captured or not self.socket_dir:
            return True
        return _same_dir(captured, self.socket_dir)

    def _pane_id(self, record: LaunchRecord, spawned: dict[str, PaneRef]) -> str | None:
        if record.state == "running":
            return record.pane_id
        ref = spawned.get(record.launch_id)
        return ref.pane_id if ref is not None else None

    def launch_rows(self, sessions: Iterable[FleetSession]) -> list[FleetSession]:
        reader = self._launch_reader or launches.list_records
        records = [
            record for record in reader(self.session)
            if record.session == self.session
            and record.multiplexer == self.multiplexer.name
        ]
        hooked = {session.launch_id for session in sessions if session.launch_id}
        replaced = {record.replaces for record in records if record.replaces}
        with self._lock:
            spawned = dict(self._spawned)
        candidates = []
        for record in records:
            if record.launch_id in hooked or record.launch_id in replaced:
                continue
            pane_id = self._pane_id(record, spawned)
            if pane_id is not None:
                candidates.append((record, pane_id))
        if not candidates:
            return []
        try:
            panes = {
                pane.ref.pane_id: pane
                for pane in self.multiplexer.list_panes(self.session)
            }
        except MultiplexerError:
            return []
        now = self._clock()
        rows = []
        for record, pane_id in candidates:
            pane = panes.get(pane_id)
            if pane is None:
                continue
            if (
                record.socket_dir and pane.ref.socket_dir
                and record.socket_dir != pane.ref.socket_dir
            ):
                continue
            if record.state == "running" and not pane.exited and not self._alive(record):
                continue
            rows.append(launch_session(record, pane, now))
        return rows


def filter_sessions(
    sessions: Iterable[FleetSession], status: str = ""
) -> list[FleetSession]:
    if not status:
        return list(sessions)
    return [s for s in sessions if s.status.value == status]


def _search_fields(session: FleetSession) -> tuple[str, ...]:
    return (
        session.agent,
        session.repo,
        session.detail,
        session.status.value,
        str(getattr(session, "profile_id", "") or ""),
    )


def search_sessions(
    sessions: Iterable[FleetSession], query: str = ""
) -> list[FleetSession]:
    needle = query.lower()
    if not needle:
        return list(sessions)
    return [
        s for s in sessions
        if any(needle in field.lower() for field in _search_fields(s))
    ]


def sort_sessions(
    sessions: Iterable[FleetSession], key: str = "repo"
) -> list[FleetSession]:
    return _sort_sessions(list(sessions), key)


def next_sort_key(current: str) -> str:
    index = SORT_KEYS.index(current) if current in SORT_KEYS else -1
    return SORT_KEYS[(index + 1) % len(SORT_KEYS)]


def visible_sessions(
    sessions: Iterable[FleetSession],
    *,
    status: str = "",
    query: str = "",
    sort_key: str = "repo",
) -> list[FleetSession]:
    return sort_sessions(
        search_sessions(filter_sessions(sessions, status), query), sort_key
    )


def attention_rows(sessions: Iterable[FleetSession]) -> list[int]:
    return [index for index, s in enumerate(sessions) if s.needs_attention]
