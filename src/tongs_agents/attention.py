"""Map session statuses onto the attention a person needs to give them."""

from __future__ import annotations

import dataclasses
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

ACCESS_TOOL = "openshell"


def attention_state(session: FleetSession) -> Attention:
    return _ATTENTION_BY_STATUS.get(session.status, Attention.QUIET)


def access_request_detail(count: int, *, unreadable: bool = False) -> str:
    if unreadable:
        return "OpenShell access requests need reading (a to review)"
    noun = "request" if count == 1 else "requests"
    return f"{count} OpenShell access {noun} (a to review)"


def with_access_requests(session: FleetSession, count: int, *,
                         unreadable: bool = False) -> FleetSession:
    """A sandboxed row whose sandbox has pending access requests needs you (#37).

    The agent is blocked on network access only a person may grant (row 14),
    so the row reads as waiting, whatever its hook status, and carries the
    count. An ended row stays ended. Without requests the row is unchanged.
    """
    if (count <= 0 and not unreadable) or session.status is SessionStatus.ENDED:
        return session
    return dataclasses.replace(
        session,
        status=SessionStatus.WAITING,
        needs_attention=True,
        detail=access_request_detail(count, unreadable=unreadable),
        tool=ACCESS_TOOL,
    )
