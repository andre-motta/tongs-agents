"""Who is calling: a verified agent launch, or the maintainer.

Agent identity (docs/work/agent-chat.md, Identity), checked in order and
stopping at the first failure:

1. ``TONGS_AGENTS_LAUNCH_ID`` is 32 lowercase hex and ``TONGS_AGENTS_PROFILE``
   is a valid profile name;
2. the launch record is ``running`` for that profile and no record replaces it;
3. a record without ``process_start`` is unverifiable: no chat;
4. the record's pid is the calling process or one of its ancestors within 64
   steps, alive with the recorded start token (``hook.process_descends_from``
   plus ``control._identity_matches``, the shared seam of #74).

A grant then matches only when its ``process_start`` equals the record's
(``grant_matches``), so a grant dies with the process it was given to.

The maintainer is any process with no ``TONGS_AGENTS_LAUNCH_ID`` and no live
claimed launch pid among its ancestors; every ``chat`` CLI verb refuses
otherwise. This is a consent and accident boundary, not a security boundary.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from tongs_agents import control, discovery, hook, launches
# grant_matches lives in the store so the hook reaches it without this module.
from tongs_agents.chat.store import grant_matches  # re-exported
from tongs_agents.launches import LaunchRecord
from tongs_agents.profiles import PROFILE_NAME_RE

LAUNCH_ENV = control.CALLER_ENV
PROFILE_ENV = "TONGS_AGENTS_PROFILE"
MAX_STEPS = hook.MAX_LINEAGE_STEPS


class IdentityError(Exception):
    """The caller's identity cannot be verified; it gets no chat."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class AgentIdentity:
    launch_id: str
    profile: str
    harness: str
    process_start: str
    record: LaunchRecord


def _replaced(launch_id: str) -> bool:
    return any(record.replaces == launch_id for record in launches.list_records())


def verify_agent(env: Mapping[str, str] | None = None, pid: int | None = None) -> AgentIdentity:
    """The verified launch this process runs under; raises IdentityError."""
    env = os.environ if env is None else env
    pid = os.getpid() if pid is None else pid
    launch_id = env.get(LAUNCH_ENV)
    profile = env.get(PROFILE_ENV)
    if not launches.is_launch_id(launch_id):
        raise IdentityError(f"no valid {LAUNCH_ENV}: not a pane tongs-agents launched")
    if not isinstance(profile, str) or not PROFILE_NAME_RE.match(profile):
        raise IdentityError(f"no valid {PROFILE_ENV}")
    record = launches.get(launch_id)
    if record is None:
        raise IdentityError(f"launch {launch_id} was not spawned by tongs-agents")
    if record.state != "running" or record.pid is None:
        raise IdentityError(f"launch {launch_id} has not been claimed by its pane")
    if record.profile != profile:
        raise IdentityError(f"launch {launch_id} is not a launch of profile {profile}")
    if _replaced(launch_id):
        raise IdentityError(f"launch {launch_id} was replaced by a later launch")
    if not record.process_start:
        raise IdentityError(
            f"launch {launch_id} has no process start token, so it cannot be verified "
            "for chat"
        )
    process = discovery.get_process_info(pid)
    if not hook.process_descends_from(process, record.pid, record.process_start):
        raise IdentityError(
            f"this process does not run under the claimed process of launch {launch_id}"
        )
    if not control._identity_matches(record):
        raise IdentityError(f"launch {launch_id} no longer runs its claimed process")
    return AgentIdentity(launch_id, record.profile, record.harness,
                         record.process_start, record)


def _ancestors(pid: int) -> list[tuple[int, str]]:
    """(pid, start token) of ``pid`` and its ancestors, at most 64 steps."""
    chain = []
    seen = set()
    current = discovery.get_process_info(pid)
    while current is not None and current.pid not in seen and len(chain) < MAX_STEPS:
        chain.append((current.pid, current.start_token))
        seen.add(current.pid)
        if not current.ppid or current.ppid <= 1:
            break
        current = discovery.get_process_info(current.ppid)
    return chain


def maintainer_refusal(env: Mapping[str, str] | None = None,
                       pid: int | None = None) -> str | None:
    """Why this process may not act as the maintainer, or None when it may."""
    env = os.environ if env is None else env
    if env.get(LAUNCH_ENV):
        return (
            f"refusing inside an agent session ({LAUNCH_ENV} is set); agents use "
            "their chat tools"
        )
    chain = _ancestors(os.getpid() if pid is None else pid)
    if not chain:
        return None
    starts = dict(chain)
    for record in launches.list_records():
        if record.state != "running" or record.pid not in starts:
            continue
        if not record.process_start or starts[record.pid] == record.process_start:
            return (
                f"refusing inside the process tree of agent launch {record.launch_id}; "
                "agents use their chat tools"
            )
    return None
