"""The inbox reader seam for mention delivery (M5-C, #32).

The hook (and the Pi digest through it) will call ``read_inbox`` after its
own identity check, render the result with ``chat.frame`` within its byte
budget, and record what it injected with ``mark_injected``. Both open the
file with the hook role (200 ms busy timeout; reads plus an UPDATE of the
caller's own ``deliveries`` row) and return nothing for an ungranted or stale
session. Callers fail open: any ``ChatError`` means no injection.

Nothing in M5-B calls this from the hook; ``hook.py`` is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass

from tongs_agents.chat import store as chat_store
from tongs_agents.chat.identity import AgentIdentity, grant_matches
from tongs_agents.chat.store import (
    HOOK_BUSY_TIMEOUT_MS,
    INBOX_MENTIONS,
    ChatStore,
    Message,
    now_ms,
)


@dataclass(frozen=True)
class Inbox:
    launch_id: str
    handle: str
    level: str
    unread_mentions: int
    mentions: tuple[Message, ...]
    injected_through: int
    last_injected_at: int | None

    @property
    def newest_mention_id(self) -> int:
        return max((message.id for message in self.mentions), default=0)

    @property
    def has_new(self) -> bool:
        """A mention newer than the last injection (the PreToolUse trigger)."""
        return self.newest_mention_id > self.injected_through


def read_inbox(identity: AgentIdentity, *, limit: int = INBOX_MENTIONS,
               busy_ms: int = HOOK_BUSY_TIMEOUT_MS) -> Inbox | None:
    """The granted session's unread mentions, or None when it has no chat."""
    if not chat_store.exists():
        return None
    with ChatStore.hook(identity.launch_id, busy_ms=busy_ms) as current:
        grant = current.grant_for(identity.launch_id)
        if not grant_matches(grant, identity):
            return None
        row = current.connection.execute(
            "SELECT injected_through, last_injected_at FROM deliveries WHERE participant = ?",
            (identity.launch_id,),
        ).fetchone()
        injected, last = (row if row is not None else (0, None))
        return Inbox(
            launch_id=identity.launch_id,
            handle=grant.handle,
            level=grant.level,
            unread_mentions=current.unread_mention_count(),
            mentions=tuple(current.unread_mentions(limit)),
            injected_through=injected,
            last_injected_at=last,
        )


def mark_injected(identity: AgentIdentity, through_id: int, *, now: int | None = None,
                  busy_ms: int = HOOK_BUSY_TIMEOUT_MS) -> None:
    """Record that mentions up to ``through_id`` were injected into the session."""
    if not chat_store.exists():
        return
    with ChatStore.hook(identity.launch_id, busy_ms=busy_ms) as current:
        if not grant_matches(current.grant_for(identity.launch_id), identity):
            return
        stamp = now_ms() if now is None else now
        current._write(lambda: current.connection.execute(
            "UPDATE deliveries SET injected_through = max(injected_through, ?), "
            "last_injected_at = ? WHERE participant = ?",
            (int(through_id), stamp, identity.launch_id),
        ))
