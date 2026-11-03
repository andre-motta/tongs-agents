"""Agent hook handler that writes normalized fleet events."""

import json
import os
import stat
import sys
import time
import uuid

from tongs_agents import discovery, paths
from tongs_agents._envcheck import bounded_env_value
from tongs_agents.discovery import (
    MAX_DETAIL_LENGTH,
    MAX_EVENT_BYTES,
    MAX_ID_LENGTH,
    MAX_PATH_LENGTH,
    MAX_TOOL_LENGTH,
    delete_session_record,
    read_session_record,
    read_stored_sessions,
    resolve_process_identity,
    session_identity_lock,
    write_session_record,
)
from tongs_agents.harnesses import ProcessIdentity, get_harness
from tongs_agents.models import (
    LAUNCH_ID_ENV,
    PROFILE_ID_ENV,
    SCHEMA_VERSION,
    SessionStatus,
    UNRESOLVED_INSTANCE_ID,
    canonical_session_id,
    valid_launch_value,
    validated_launch,
)

_CREATING_EVENTS = frozenset({"session-start", "prompt-submit", "fleet-event"})
# Mirrors launches.SCHEMA_VERSION and MAX_RECORD_BYTES; the hook never imports
# launches (its import surface is pinned by tests).
_LAUNCH_SCHEMA_VERSION = 1
_MAX_LAUNCH_RECORD_BYTES = 64 * 1024
MAX_LINEAGE_STEPS = 64


def _find_agent_pid(agent):
    process = resolve_process_identity(agent)
    return str(process.pid) if process else ""


def _read_payload():
    try:
        raw = sys.stdin.read(MAX_EVENT_BYTES + 1)
        encoded_size = len(raw.encode("utf-8"))
    except (OSError, UnicodeError):
        return None
    if encoded_size > MAX_EVENT_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(payload, key, limit, *, required=False):
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"invalid {key}")
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(f"invalid {key}") from error
    if "\0" in value or len(encoded) > limit or (required and not value):
        raise ValueError(f"invalid {key}")
    return value


def _integer(payload, key):
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"invalid {key}")
    return value


def _existing_record(harness_id, session_id, instance_id, pid=""):
    if instance_id:
        current = read_session_record(harness_id, session_id, instance_id)
        if current is not None:
            return current
    candidates = []
    for record in read_stored_sessions():
        if record["harness_id"] != harness_id or record["session_id"] != session_id:
            continue
        if not instance_id and pid and record.get("pid") == pid:
            candidates.append(record)
        if record.get("schema_version", 0) == 0:
            candidates.append(record)
    return max(candidates, key=lambda item: item.get("ts", 0), default=None)


def _unresolved_record(harness_id, session_id):
    return read_session_record(harness_id, session_id, UNRESOLVED_INSTANCE_ID)


def _capture_terminal_info():
    from tongs_agents.terminal_apis import capture_terminal_info
    return capture_terminal_info()


def _capture_launch():
    """Read the launcher's identity from this process env; never raises."""
    launch = {}
    for key, name in (
        ("launch_id", LAUNCH_ID_ENV),
        ("profile_id", PROFILE_ID_ENV),
    ):
        value = bounded_env_value(name) or ""
        if valid_launch_value(key, value):
            launch[key] = value
    return launch or None


def process_descends_from(process, pid, start_token, lookup=None):
    """True when ``process`` is, or runs under, ``pid`` started at ``start_token``.

    Compares ``process`` and at most ``MAX_LINEAGE_STEPS - 1`` ancestors,
    stopping at pid 1 and on a cycle. A process found with ``pid`` but another
    start token is a reused pid and fails. Without a start token the pid
    alone is compared.
    """
    if process is None or type(pid) is not int or pid <= 0:
        return False
    lookup = lookup or discovery.get_process_info
    seen = set()
    current = process
    while True:
        if current.pid == pid:
            return not start_token or current.start_token == start_token
        seen.add(current.pid)
        parent = current.ppid
        if len(seen) >= MAX_LINEAGE_STEPS or not parent or parent <= 1 or parent in seen:
            return False
        current = lookup(parent)
        if current is None:
            return False


def _launch_claim(launch_id):
    """The claimed ``(pid, process_start)`` of a launch record, or None.

    A dependency-free reader for the two claim fields with the checks
    ``launches`` applies: no symlink, a regular file owned by this user, at
    most 64 KiB, schema 1, named for its own id and claimed. Never raises.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(paths.launches_dir() / f"{launch_id}.json", flags)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            if os.name != "nt" and info.st_uid != os.getuid():
                return None
            raw = stream.read(_MAX_LAUNCH_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(raw) > _MAX_LAUNCH_RECORD_BYTES:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != _LAUNCH_SCHEMA_VERSION
        or data.get("launch_id") != launch_id
        or data.get("state") != "running"
    ):
        return None
    pid = data.get("pid")
    start = data.get("process_start")
    if type(pid) is not int or pid <= 0:
        return None
    if start is not None and not (isinstance(start, str) and start):
        return None
    return pid, start


def _verified_launch(launch, process):
    """``launch`` when its launch record claims ``process`` or an ancestor.

    A shared Codex daemon runs a later session's hooks with the first
    daemon-attached launch's environment (#74), so an env launch id is kept
    only when its claimed pid and start token are the hook's resolved process
    or one of its ancestors. A mismatch, or a missing or unclaimed record,
    drops the whole identity, profile included.
    """
    if launch is None or "launch_id" not in launch:
        return launch
    claim = _launch_claim(launch["launch_id"])
    if claim is None or not process_descends_from(process, *claim):
        return None
    return launch


def _record_launch(event, existing, process):
    """The launch identity the record written for ``event`` carries.

    The environment is read only when the event starts the session or creates
    its record, and kept only when its launch record claims this process.
    Every other event, including ``session-end``, keeps the stored identity: a
    shared Codex daemon runs a later session's late SessionEnd with the first
    launch's environment, so that environment never names the session being
    ended.
    """
    if event == "session-start" or (
        existing is None and event in _CREATING_EVENTS
    ):
        return _verified_launch(_capture_launch(), process)
    if existing is None:
        return None
    return validated_launch(existing.get("launch"))


def _event_update(event, payload):
    tool_name = _text(payload, "tool_name", MAX_TOOL_LENGTH)
    if event == "session-start":
        return "started", "session started", ""
    if event == "prompt-submit":
        return "running", "processing prompt", ""
    if event == "tool-use":
        return "running", f"using {tool_name or 'tool'}", tool_name
    if event == "stop":
        message = _text(payload, "last_assistant_message", MAX_DETAIL_LENGTH * 4)
        summary = (message or "finished").replace("\n", " ").replace("\r", "")
        if len(summary) > 120:
            summary = summary[:120] + "..."
        return "idle", summary, ""
    if event == "stop-failure":
        return "error", "turn failed (API error)", ""
    if event == "permission-request":
        return "waiting", f"permission needed: {tool_name or 'tool'}", tool_name
    if event == "elicitation":
        return "waiting", "waiting for user input", ""
    return "ended", "session closed", ""


def _normalized_update(agent, payload):
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    event_id = _text(payload, "event_id", MAX_ID_LENGTH, required=True)
    namespace = f"{agent}."
    if not event_id.startswith(namespace) or len(event_id) == len(namespace):
        raise ValueError("event_id must use the harness namespace")
    status = _text(payload, "status", 32, required=True)
    lifecycle_statuses = {
        member.value
        for member in SessionStatus
        if member is not SessionStatus.DISCOVERED
    }
    if status not in lifecycle_statuses:
        raise ValueError("invalid status")
    detail = _text(payload, "detail", MAX_DETAIL_LENGTH)
    tool = _text(payload, "tool", MAX_TOOL_LENGTH)
    return status, detail, tool, event_id


def build_session_record(
    *,
    agent,
    session_id,
    instance_id,
    cwd,
    process,
    process_identity,
    status,
    detail,
    tool,
    now,
    started,
    event_id,
    terminal,
    terminal_env,
    sequence=None,
    launch=None,
    source="hook",
):
    """The schema v1 session record the hook (and ``notify``) writes."""
    canonical_id = canonical_session_id(agent, session_id, instance_id)
    process_pid = str(process.pid) if process is not None else ""
    process_start = process.start_token if process is not None else ""
    record = {
        "schema_version": SCHEMA_VERSION,
        "record_kind": "session",
        "canonical_id": canonical_id,
        "session_id": session_id,
        "instance_id": instance_id,
        "harness_id": agent,
        "agent": agent,
        "repo": os.path.basename(os.path.normpath(cwd)) if cwd else "",
        "cwd": cwd,
        "pid": process_pid,
        "process_start": process_start,
        "process_identity": process_identity,
        "status": status,
        "detail": detail,
        "tool": tool,
        "ts": now,
        "started": started,
        "source": source,
        "last_event": event_id,
        "terminal": terminal,
        "terminal_env": terminal_env,
        "focus_target": {
            "kind": (
                "terminal"
                if terminal and process is not None
                else "unavailable"
            ),
            "terminal": terminal,
            "terminal_env": terminal_env,
        },
    }
    if sequence is not None:
        record["sequence"] = sequence
    if launch is not None:
        record["launch"] = launch
    return record


def _store_event(
    harness,
    event,
    agent,
    session_id,
    cwd,
    timestamp,
    supplied_pid,
    supplied_instance,
    sequence,
    status,
    detail,
    tool,
    event_id,
):
    process = resolve_process_identity(
        agent,
        supplied_pid
        if harness.process_identity is ProcessIdentity.EMITTER
        else None,
    )
    if harness.process_identity is ProcessIdentity.EMITTER and process is None:
        return False
    if process is None:
        instance_id = UNRESOLVED_INSTANCE_ID
        existing = _unresolved_record(agent, session_id)
        process_identity = "unresolved"
    elif supplied_instance:
        instance_id = supplied_instance
        existing = _existing_record(agent, session_id, instance_id)
        process_identity = "identified"
    elif process.start_token:
        instance_id = f"{process.pid}:{process.start_token}"
        existing = _existing_record(agent, session_id, instance_id)
        process_identity = "identified"
    else:
        existing = _existing_record(agent, session_id, "", str(process.pid))
        instance_id = existing.get("instance_id", "") if existing else ""
        if not instance_id:
            instance_id = f"{process.pid}:{uuid.uuid4().hex}"
        process_identity = "identified"
    if process is not None and existing is None and not supplied_instance:
        existing = _unresolved_record(agent, session_id)
    if event not in _CREATING_EVENTS and existing is None:
        return False

    now = int(time.time()) if timestamp is None else timestamp
    terminal_info = None
    if existing is None or event == "session-start":
        terminal_info = _capture_terminal_info()
    launch = _record_launch(event, existing, process)
    terminal = (
        terminal_info["terminal"]
        if terminal_info is not None
        else existing.get("terminal", "")
    )
    terminal_env = (
        terminal_info["terminal_env"]
        if terminal_info is not None
        else existing.get("terminal_env", {})
    )
    started = (
        now
        if existing is None or event == "session-start"
        else existing.get("started", now)
    )
    record = build_session_record(
        agent=agent,
        session_id=session_id,
        instance_id=instance_id,
        cwd=cwd,
        process=process,
        process_identity=process_identity,
        status=status,
        detail=detail,
        tool=tool,
        now=now,
        started=started,
        event_id=event_id,
        terminal=terminal,
        terminal_env=terminal_env,
        sequence=sequence,
        launch=launch,
    )
    written = write_session_record(record)
    if (
        written
        and process is not None
        and harness.process_identity is ProcessIdentity.DISCOVERABLE
    ):
        delete_session_record(agent, session_id, UNRESOLVED_INSTANCE_ID)
    return written


def handle(event, agent="claude"):
    harness = get_harness(agent)
    if harness is None:
        raise ValueError(f"unsupported agent: {agent}")
    if not harness.supports_event(event):
        return False

    payload = _read_payload()
    if payload is None:
        return False
    try:
        session_id = _text(payload, "session_id", MAX_ID_LENGTH, required=True)
        cwd = _text(payload, "cwd", MAX_PATH_LENGTH, required=True)
        timestamp = _integer(payload, "ts")
        if event == "fleet-event":
            supplied_pid = _integer(payload, "pid")
            sequence = _integer(payload, "sequence")
            status, detail, tool, event_id = _normalized_update(agent, payload)
            supplied_instance = _text(
                payload, "instance_id", MAX_ID_LENGTH, required=True
            )
        else:
            supplied_pid = None
            sequence = None
            status, detail, tool = _event_update(event, payload)
            event_id = f"{agent}.{event}"
            supplied_instance = ""
    except ValueError:
        return False

    if harness.process_identity is ProcessIdentity.EMITTER and supplied_pid is None:
        return False
    if harness.process_identity is ProcessIdentity.EMITTER and sequence is None:
        return False
    arguments = (
        harness,
        event,
        agent,
        session_id,
        cwd,
        timestamp,
        supplied_pid,
        supplied_instance,
        sequence,
        status,
        detail,
        tool,
        event_id,
    )
    if harness.process_identity is ProcessIdentity.DISCOVERABLE:
        try:
            with session_identity_lock(agent, session_id):
                return _store_event(*arguments)
        except OSError:
            return False
    return _store_event(*arguments)


def main():
    if len(sys.argv) < 2:
        sys.exit(1)
    agent = "claude"
    if "--agent" in sys.argv:
        index = sys.argv.index("--agent")
        if index + 1 >= len(sys.argv):
            sys.exit(1)
        agent = sys.argv[index + 1]
    try:
        handle(sys.argv[1], agent)
    except ValueError:
        sys.exit(1)


if __name__ == "__main__":
    main()
