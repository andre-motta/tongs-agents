"""The chat audit log: ``state_dir()/audit/chat.jsonl``.

Same shape, rotation and locking as the control log (``control.audit``):
time, actor, caller and target launch ids, action, byte count, SHA-256,
outcome and exit code. Grants, revokes, wakes and refusals are logged;
message text never is. An audit failure never changes the outcome.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import time

from tongs_agents import control, launches

AUDIT_FILE = "chat.jsonl"


def audit(action: str, *, actor: str, launch_id=None, outcome: str, exit_code: int = 0,
          data: bytes | None = None, caller: str | None = None) -> None:
    entry = {
        "ts": round(time.time(), 3),
        "actor": control._actor(actor),
        "caller_launch_id": caller if launches.is_launch_id(caller) else None,
        "launch_id": launch_id if launches.is_launch_id(launch_id) else None,
        "action": action,
        "bytes": None if data is None else len(data),
        "sha256": None if data is None else hashlib.sha256(data).hexdigest(),
        "outcome": outcome,
        "exit_code": exit_code,
    }
    line = (json.dumps(entry, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        directory = control._audit_dir()
        path = directory / AUDIT_FILE
        with control._locked(directory / ".chat-audit.lock"):
            control._rotate(path)
            descriptor = os.open(path, flags, 0o600)
            try:
                if stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.write(descriptor, line)
            finally:
                os.close(descriptor)
    except (OSError, control.ControlError) as error:
        print(f"tongs-agents: chat audit not written: {error}", file=sys.stderr)
