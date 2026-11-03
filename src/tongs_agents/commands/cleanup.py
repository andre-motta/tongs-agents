"""``tongs-agents cleanup``: run session store maintenance now.

The sidebar and the monitor already maintain the store while they run; this
command is for when neither does. It records agent processes no hook reported
and removes ended, exited and stale session records, with the same lock and
per-record checks (see docs/sessions.md). It exits 1 when another maintainer
holds the store lock or the store cannot be used.
"""

from __future__ import annotations

import sys

from tongs_agents import session_store

NAME = "cleanup"
HELP = "Remove ended, exited and stale session records now"


def add_arguments(parser):
    pass


def run(args) -> int:
    result = session_store.maintain_store(force=True)
    if not result.ran:
        reason = (
            "another sidebar or monitor is maintaining the store; try again"
            if result.skipped == session_store.SKIPPED_BUSY
            else "the session store is unavailable"
        )
        print(f"tongs-agents cleanup: {reason}", file=sys.stderr)
        return 1
    print(f"Removed {result.removed} session record(s).")
    return 0
