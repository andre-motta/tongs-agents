"""Grant and revoke chat for one launch (one pane): maintainer code paths only.

A grant names one launch, never a profile. ``prepare`` checks the launch
(claimed, carrying a process start token, its pane verified now through
``control.verify``) and picks the handle; the caller shows that preview and
asks for confirmation (the CLI makes the maintainer type the handle, the
sidebar shows a dialog); ``apply`` checks again and writes the grant, creating
the chat file on first use. Only the CLI and the sidebar call this module: no
MCP or hook code path writes grants, and their connections' authorizers refuse
it anyway. Grants, revokes, expiries and refusals are audited.

A grant ends by itself when its process ends or its pane is replaced:
``grant_state`` reports such rows as stale and ``remove_stale`` (run by the
sidebar) deletes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tongs_agents import control, launches, pi_install
from tongs_agents import profiles as profiles_module
from tongs_agents.chat import audit as chat_audit
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import (
    LEVEL_CHAT,
    LEVEL_WAKE,
    MAINTAINER,
    ChatError,
    ChatRefused,
    ChatStore,
    Grant,
)
from tongs_agents.launches import LaunchRecord
from tongs_agents.profiles import ProfileError

PI_MCP_MINIMUM = (0, 99, 0)
STATE_ACTIVE = "active"
STATE_PENDING = "granted, tools pending"
STATE_STALE = "stale"


@dataclass(frozen=True)
class GrantRequest:
    """What the maintainer confirms: the pane, profile, launch id and handle."""

    launch_id: str
    profile: str
    harness: str
    pane_session: str
    pane_id: str
    socket_dir: str | None
    process_start: str
    handle: str
    level: str
    notes: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        return (
            f"pane {self.pane_session}:{self.pane_id}, profile {self.profile} "
            f"({self.harness}), launch {self.launch_id}, handle @{self.handle}, "
            f"level {self.level}"
        )


def _version_text(version) -> str:
    return ".".join(str(part) for part in version)


def _pi_notes(record: LaunchRecord) -> tuple[str, ...]:
    """Chat tools reach Pi only through its native MCP (0.99.0 or newer)."""
    if record.harness != "pi":
        return ()
    try:
        profile = profiles_module.load_profiles().get(record.profile)
    except ProfileError:
        profile = None
    if profile is None or not profile.argv:
        return (f"cannot check the Pi version of profile {record.profile}; chat tools "
                f"need Pi {_version_text(PI_MCP_MINIMUM)} or newer",)
    version = pi_install.detect_pi_version(profile.argv[0])
    if version is None:
        return (f"cannot read the Pi version; chat tools need Pi "
                f"{_version_text(PI_MCP_MINIMUM)} or newer",)
    if tuple(version) < PI_MCP_MINIMUM:
        raise ChatRefused(
            f"Pi {_version_text(version)} has no native MCP; chat tools need Pi "
            f"{_version_text(PI_MCP_MINIMUM)} or newer"
        )
    return ()


def _base_handle(profile: str) -> str:
    base = re.sub(r"[^a-z0-9-]", "-", profile.lower()).strip("-") or "agent"
    return "agent" if base == MAINTAINER else base[:32]


def default_handle(profile: str, taken: dict[str, str], launch_id: str) -> str:
    """The profile name, then ``<profile>-2`` and so on, skipping taken handles."""
    base = _base_handle(profile)
    number = 1
    while True:
        suffix = "" if number == 1 else f"-{number}"
        handle = base[:32 - len(suffix)].rstrip("-") + suffix
        if taken.get(handle) in (None, launch_id):
            return handle
        number += 1


def _verified(launch_id) -> tuple[LaunchRecord, str, str, str | None]:
    if not launches.is_launch_id(launch_id):
        raise ChatRefused("not a tongs-agents launch id")
    record = launches.get(launch_id)
    if record is None:
        raise ChatRefused(f"launch {launch_id} was not spawned by tongs-agents")
    if record.state != "running":
        raise ChatRefused(f"launch {launch_id} has not been claimed by its pane")
    if not record.process_start:
        raise ChatRefused(
            f"launch {launch_id} has no process start token, so it cannot be verified "
            "for chat; restart it with this tongs-agents to grant chat"
        )
    try:
        ref = control.verify(launch_id)
    except control.ControlError as error:
        raise ChatRefused(f"pane check failed: {error.detail}") from None
    return record, ref.session, ref.pane_id, ref.socket_dir


def prepare(launch_id, *, wake: bool = False, handle: str | None = None,
            actor: str = "cli") -> GrantRequest:
    """Check a launch for a grant and pick its handle; nothing is written."""
    try:
        record, session, pane_id, socket_dir = _verified(launch_id)
        notes = _pi_notes(record)
        taken: dict[str, str] = {}
        if chat_store.exists():
            with ChatStore.maintainer() as current:
                taken = current.handles()
        if handle is None:
            existing = next((name for name, owner in taken.items() if owner == launch_id),
                            None)
            handle = existing or default_handle(record.profile, taken, launch_id)
        elif not chat_store.is_name(handle) or handle == MAINTAINER:
            raise ChatError(
                f"invalid handle {handle!r:.40}: use 1 to 32 of a-z, 0-9 and -, "
                "not 'maintainer'"
            )
        elif taken.get(handle) not in (None, launch_id):
            raise ChatRefused(f"handle @{handle} is already granted to another launch")
    except ChatError as error:
        chat_audit.audit("grant", actor=actor, launch_id=launch_id, outcome=error.outcome,
                         exit_code=control.EXIT_REFUSED)
        raise
    return GrantRequest(
        launch_id=record.launch_id, profile=record.profile, harness=record.harness,
        pane_session=session, pane_id=pane_id, socket_dir=socket_dir,
        process_start=record.process_start, handle=handle,
        level=LEVEL_WAKE if wake else LEVEL_CHAT, notes=notes,
    )


def apply(request: GrantRequest, *, via: str, actor: str | None = None) -> Grant:
    """Check the launch again and write the confirmed grant."""
    actor = actor or via
    try:
        record, session, pane_id, socket_dir = _verified(request.launch_id)
        if (record.process_start, session, pane_id) != (
            request.process_start, request.pane_session, request.pane_id
        ):
            raise ChatRefused(
                f"launch {request.launch_id} changed since it was confirmed; try again"
            )
        with ChatStore.maintainer(create=True) as current:
            grant = current.put_grant(
                launch_id=record.launch_id, handle=request.handle, level=request.level,
                process_start=record.process_start, pane_session=session,
                pane_id=pane_id, socket_dir=socket_dir, via=via,
                profile=record.profile, harness=record.harness,
            )
    except ChatError as error:
        chat_audit.audit("grant", actor=actor, launch_id=request.launch_id,
                         outcome=error.outcome, exit_code=control.EXIT_REFUSED)
        raise
    chat_audit.audit("grant", actor=actor, launch_id=request.launch_id, outcome="ok")
    return grant


def revoke(launch_id, *, actor: str = "cli") -> bool:
    """Delete the grant of ``launch_id``; True when there was one."""
    if not launches.is_launch_id(launch_id):
        raise ChatRefused("not a tongs-agents launch id")
    if not chat_store.exists():
        removed = False
    else:
        with ChatStore.maintainer() as current:
            removed = current.delete_grant(launch_id)
    chat_audit.audit("revoke", actor=actor, launch_id=launch_id,
                     outcome="ok" if removed else "absent")
    return removed


def stale_reason(grant: Grant) -> str | None:
    """Why ``grant`` no longer applies (process ended, pane replaced), or None."""
    record = launches.get(grant.launch_id)
    if record is None or record.state != "running":
        return "launch record is gone"
    if record.process_start != grant.process_start:
        return "the launch runs another process"
    if any(item.replaces == grant.launch_id for item in launches.list_records()):
        return "the pane was replaced by a later launch"
    if not control._identity_matches(record):
        return "the agent process ended"
    return None


def grant_state(grant: Grant) -> str:
    reason = stale_reason(grant)
    if reason is not None:
        return f"{STATE_STALE}: {reason}"
    if grant.tools_listed_at is None or grant.tools_listed_at < grant.granted_at:
        return STATE_PENDING
    return STATE_ACTIVE


def remove_stale(*, actor: str = "sidebar") -> list[Grant]:
    """Delete grants whose process ended or whose pane was replaced."""
    if not chat_store.exists():
        return []
    removed = []
    with ChatStore.maintainer() as current:
        for grant in current.grants():
            if stale_reason(grant) is not None and current.delete_grant(grant.launch_id):
                removed.append(grant)
                chat_audit.audit("expire", actor=actor, launch_id=grant.launch_id,
                                 outcome="ok")
    return removed
