"""Mention delivery through the hook (docs/work/agent-chat.md, Delivery; #32).

``tongs-agents-hook`` calls ``hook_output`` only after its cheap checks (a
launch id in the env and an existing chat file) and only from the events
below. It then opens the file with the hook role (200 ms busy timeout, then
nothing), looks up the launch's grant (one indexed lookup) and, only for a
granted session, checks the agent identity and reads the inbox:

| Point | Claude and Codex | Pi | Text | Max bytes |
| --- | --- | --- | --- | --- |
| start | SessionStart | none | handle, unread count, tool names | 512 |
| prompt | UserPromptSubmit | ``before_agent_start`` | framed digest of unread mentions | 2048 |
| tool | PreToolUse | ``tool_execution_end`` | a notice without message text, for a mention newer than the last injection, at most once per 60 s | 512 |

Claude and Codex get ``{"hookSpecificOutput": {"hookEventName": ...,
"additionalContext": ...}}`` and nothing else; Pi gets the bare text, which
its extension wraps in a ``tongs-agents-chat`` message. Every text starts
with the data-not-instructions preamble (``chat.frame``).

The identity check follows ``chat.identity.verify_agent`` step by step with
the hook's own dependency-free readers (``hook.read_launch_data``,
``hook.process_descends_from``), so the hook never imports the launcher or
the control layer. The only write is the session's own ``deliveries`` row
(row 9). Any error means no output.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass

from tongs_agents import discovery, hook, paths
from tongs_agents.chat import frame, inbox
from tongs_agents.chat.inbox import Inbox
from tongs_agents.chat.store import ChatError, ChatStore, is_launch_id, now_ms
from tongs_agents.models import valid_launch_value

START = "start"
PROMPT = "prompt"
TOOL = "tool"
BUDGETS = {START: 512, PROMPT: 2048, TOOL: 512}
NOTICE_INTERVAL_MS = 60_000
HOOK_EVENTS = {
    "session-start": ("SessionStart", START),
    "prompt-submit": ("UserPromptSubmit", PROMPT),
    "tool-use": ("PreToolUse", TOOL),
}
PI_POINTS = {"before_agent_start": PROMPT, "tool_execution_end": TOOL}


@dataclass(frozen=True)
class HookIdentity:
    """A launch verified from the hook: enough for ``grant_matches``."""

    launch_id: str
    profile: str
    process_start: str


def _replaced(launch_id: str) -> bool:
    try:
        names = [entry.name for entry in os.scandir(paths.launches_dir())]
    except OSError:
        return False
    for name in names:
        other = name[:-5] if name.endswith(".json") else ""
        if other == launch_id or not is_launch_id(other):
            continue
        data = hook.read_launch_data(other)
        if data is not None and data.get("replaces") == launch_id:
            return True
    return False


def verify_hook(launch: dict, pid: int | None = None) -> HookIdentity | None:
    """The verified launch the hook runs under, or None (identity steps 1 to 6).

    1. the env's launch id and profile are valid;
    2. the record is claimed (``running``) for that profile, and no record
       replaces it;
    3. a record without a process start is unverifiable;
    4. the record's pid is ``pid`` (default: this process) or an ancestor
       within 64 steps, with the recorded start token, so it is alive.
    """
    launch_id = launch.get("launch_id")
    profile = launch.get("profile_id")
    if not valid_launch_value("launch_id", launch_id):
        return None
    if not valid_launch_value("profile_id", profile):
        return None
    data = hook.read_launch_data(launch_id)
    claim = hook.launch_claim_of(data)
    if claim is None or data.get("profile") != profile:
        return None
    claimed_pid, start = claim
    if not start or _replaced(launch_id):
        return None
    process = discovery.get_process_info(os.getpid() if pid is None else pid)
    if not hook.process_descends_from(process, claimed_pid, start):
        return None
    return HookIdentity(launch_id, profile, start)


def render(point: str, found: Inbox, now: int) -> tuple[str, int]:
    """(text, mention id to mark injected through) for ``point``; text may be empty."""
    budget = BUDGETS[point]
    if point == START:
        return frame.cut_utf8(frame.session_start_note(found.handle, found.unread_mentions),
                              budget), 0
    if point == PROMPT:
        if not found.unread_mentions or not found.mentions:
            return "", 0
        text = frame.mention_digest(found.handle, list(found.mentions),
                                    total=found.unread_mentions, max_bytes=budget)
        return text, found.newest_mention_id
    if not found.has_new:
        return "", 0
    if found.last_injected_at is not None and now - found.last_injected_at < NOTICE_INTERVAL_MS:
        return "", 0
    return frame.cut_utf8(frame.MID_TURN_NOTICE, budget), found.newest_mention_id


def inject(point: str, launch: dict, *, now: int | None = None,
           pid: int | None = None) -> str:
    """The text to inject at ``point`` for a granted session, else ''.

    Records the injection in the session's own ``deliveries`` row. Raises
    ChatError or sqlite3.Error on a busy or broken file; ``hook_output``
    turns every error into no output.
    """
    launch_id = launch.get("launch_id")
    if point not in BUDGETS or not is_launch_id(launch_id):
        return ""
    stamp = now_ms() if now is None else now
    with ChatStore.hook(launch_id) as current:
        if current.grant_for(launch_id) is None:
            return ""
        identity = verify_hook(launch, pid)
        if identity is None:
            return ""
        found = inbox.read_inbox(identity, current=current)
        if found is None:
            return ""
        text, through = render(point, found, stamp)
        if text and through:
            inbox.mark_injected(identity, through, now=stamp, current=current)
        return text


def claude_output(event_name: str, text: str) -> str:
    """The hook JSON both Claude Code and Codex accept: exactly two fields."""
    return json.dumps({
        "hookSpecificOutput": {"hookEventName": event_name, "additionalContext": text},
    })


def hook_output(agent: str, event: str, launch: dict, *, point=None,
                now: int | None = None, pid: int | None = None) -> str:
    """What the hook prints for chat, or '' (ungranted, nothing new, any error).

    Claude and Codex name the hook ``event``; Pi passes ``chat-digest`` with
    the extension event as ``point`` (``PI_POINTS``).
    """
    try:
        if agent == "pi":
            pi_point = PI_POINTS.get(point)
            return inject(pi_point, launch, now=now, pid=pid) if pi_point else ""
        mapped = HOOK_EVENTS.get(event)
        if mapped is None:
            return ""
        event_name, hook_point = mapped
        text = inject(hook_point, launch, now=now, pid=pid)
        return claude_output(event_name, text) if text else ""
    except (ChatError, sqlite3.Error, OSError, ValueError):
        return ""
