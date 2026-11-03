"""The inbox reader seam for mention delivery (M5-C, #32).

The hook (and the Pi digest through it, ``chat.delivery``) calls
``read_inbox`` after its own identity check, renders the result with
``chat.frame`` within its byte budget, and records what it injected with
``mark_injected``. Both open the file with the hook role (200 ms busy
timeout; reads plus an UPDATE of the caller's own ``deliveries`` row), or
use the hook-role store the caller already holds, and return nothing for an
ungranted or stale session. Callers fail open: any ``ChatError`` means no
injection.

``identity`` is anything with ``launch_id`` and ``process_start``. This
module imports only the store, so the hook can load it without the
launcher's imports.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass

from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import (
    HOOK_BUSY_TIMEOUT_MS,
    INBOX_MENTIONS,
    ChatStore,
    Message,
    grant_matches,
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


def _opened(identity, current: ChatStore | None, busy_ms: int):
    if current is not None:
        return nullcontext(current)
    return ChatStore.hook(identity.launch_id, busy_ms=busy_ms)


def read_inbox(identity, *, limit: int = INBOX_MENTIONS,
               busy_ms: int = HOOK_BUSY_TIMEOUT_MS,
               current: ChatStore | None = None) -> Inbox | None:
    """The granted session's unread mentions, or None when it has no chat."""
    if current is None and not chat_store.exists():
        return None
    with _opened(identity, current, busy_ms) as current:
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


def mark_injected(identity, through_id: int, *, now: int | None = None,
                  busy_ms: int = HOOK_BUSY_TIMEOUT_MS,
                  current: ChatStore | None = None) -> None:
    """Record that mentions up to ``through_id`` were injected into the session.

    The stamp moves even when ``through_id`` is not newer, so a notice counts
    toward the once-per-60-s limit of mid-turn notices.
    """
    if current is None and not chat_store.exists():
        return
    with _opened(identity, current, busy_ms) as current:
        if not grant_matches(current.grant_for(identity.launch_id), identity):
            return
        stamp = now_ms() if now is None else now
        current._write(lambda: current.connection.execute(
            "UPDATE deliveries SET injected_through = max(injected_through, ?), "
            "last_injected_at = ? WHERE participant = ?",
            (int(through_id), stamp, identity.launch_id),
        ))
