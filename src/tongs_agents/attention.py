"""Map session statuses onto the attention a person needs to give them."""

from __future__ import annotations

from enum import Enum

from tongs_agents.models import FleetSession, SessionStatus


class Attention(Enum):
    NEEDS_YOU = "needs-you"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"
    QUIET = "quiet"


_ATTENTION_BY_STATUS = {
    SessionStatus.WAITING: Attention.NEEDS_YOU,
    SessionStatus.ERROR: Attention.FAILED,
    SessionStatus.STARTED: Attention.WORKING,
    SessionStatus.RUNNING: Attention.WORKING,
    SessionStatus.IDLE: Attention.DONE,
    SessionStatus.ENDED: Attention.QUIET,
    SessionStatus.DISCOVERED: Attention.QUIET,
}


def attention_state(session: FleetSession) -> Attention:
    return _ATTENTION_BY_STATUS.get(session.status, Attention.QUIET)
