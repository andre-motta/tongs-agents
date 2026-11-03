"""The one session source for UI modules, plus pure view helpers."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from tongs_agents import discovery
from tongs_agents.models import SORT_KEYS, FleetSession, parse_session
from tongs_agents.models import sort_sessions as _sort_sessions

__all__ = [
    "SORT_KEYS",
    "SessionFeed",
    "attention_rows",
    "filter_sessions",
    "next_sort_key",
    "search_sessions",
    "sort_sessions",
    "visible_sessions",
]

Reader = Callable[[], Iterable[dict]]


class SessionFeed:
    """Read session records and parse them into FleetSession objects.

    The default reader is looked up at call time so that tests and the
    store rename can replace ``discovery.read_sessions`` in one place.
    """

    def __init__(self, reader: Reader | None = None) -> None:
        self._reader = reader

    def snapshot(self) -> list[FleetSession]:
        reader = self._reader or discovery.read_sessions
        return [parse_session(record) for record in reader()]


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
