"""Host-initiated bridge from sandbox spools to session records (rows 7 to 9).

Hooks inside a sandbox write one file per event under the launch's spool
(``<workdir>/.tongs-agents/spool/<launch_id>``). ``sync_all`` runs in the
sidebar's poll loop (and ``python -m tongs_agents.sandbox sync``): for each
sandboxed launch whose pane process is still the claimed one it runs one
``sandbox exec --no-tty --no-login-shell`` that prints at most
``MAX_FILES_PER_PULL`` spool files (base64, oldest first) and removes them
from the spool. Everything pulled is untrusted: the agent can write the spool
too. Symlinks, non-regular files, bad names and oversized files are dropped
inside the sandbox and again on the host; payloads go through the hook's own
normalisation and are written through ``session_store.import_session_record``.

An imported record takes its pid, process start, launch and terminal identity
from the host launch (row 9): pids, terminal fields and launch ids from inside
the sandbox are discarded. Because the spool is agent-writable, one launch
keeps at most ``MAX_SESSIONS_PER_LAUNCH`` distinct session records. At the
cap a new session replaces the oldest ended one; while every record is live,
events for further session ids are dropped with one diagnostic per sync.
``sync_all`` holds a per-launch file lock (``launch_lock``) from the store
read through the import, so two processes never sync one launch at once.
Until the first hook event arrives, the sandbox
phase stands in as a ``sandbox-<launch_id>`` record. Chat relay (row 11) is
M5 work; ``relay_chat`` is its seam.

The bridge writes only session records in the sessions directory and its
launch lock files, and removes its own sandbox launch records and lock files
once their launch is gone.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field

from tongs_agents import cli, discovery, hook, launches, paths, session_store
from tongs_agents.discovery import MAX_EVENT_BYTES, MAX_ID_LENGTH, ProcessInfo
from tongs_agents.launches import LaunchRecord
from tongs_agents.sandbox import state
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError, SandboxInfo

MAX_FILES_PER_PULL = 64
MAX_SESSIONS_PER_LAUNCH = 4
PULL_TIMEOUT = 10.0
PHASE_TIMEOUT = 10.0
SOURCE = "sandbox"
PHASE_SESSION_PREFIX = "sandbox-"
LOCKS_DIRECTORY = "locks"
FILE_NAME_RE = re.compile(r"^(\d{19})-(\d{1,10})-([a-z][a-z-]{0,31})\.json\Z")
FLEET_EVENT = "fleet-event"
EVENTS_BY_HARNESS = {
    "claude": frozenset(arg for _event, arg in cli.CLAUDE_HOOK_EVENTS),
    "codex": frozenset(arg for _event, arg in cli.CODEX_HOOK_EVENTS),
    "pi": frozenset({FLEET_EVENT}),
}
CREATING_EVENTS = frozenset({"session-start", "prompt-submit", FLEET_EVENT})
PHASE_STATUS = {
    "Unknown": ("started", "sandbox starting"),
    "Provisioning": ("started", "sandbox provisioning"),
    "Ready": ("started", "sandbox ready; waiting for the agent"),
    "Error": ("error", "sandbox error"),
    "Completed": ("ended", "sandbox completed"),
    "Stopped": ("ended", "sandbox stopped"),
    "Suspending": ("ended", "sandbox suspending"),
    "Suspended": ("ended", "sandbox suspended"),
    "Deleting": ("ended", "sandbox deleting"),
}

# Run inside the sandbox as ``sh -c PULL_SCRIPT sh <spool> <max bytes> <max files>``.
# Prints "F <name>", base64 lines and "." per file; "O <name>" for an
# oversized file and "X" for anything else it drops. Every printed or dropped
# entry is removed, so a flood drains over several polls.
PULL_SCRIPT = r"""
cd -- "$1" 2>/dev/null || exit 0
n=0
for f in *; do
  [ -e "$f" ] || [ -h "$f" ] || continue
  n=$((n+1))
  [ "$n" -le "$3" ] || break
  case "$f" in
    *[!A-Za-z0-9._-]*) printf 'X\n'; rm -rf -- "./$f"; continue;;
  esac
  if [ -h "$f" ] || [ ! -f "$f" ]; then printf 'X\n'; rm -rf -- "./$f"; continue; fi
  s=$(wc -c < "./$f" 2>/dev/null) || s=0
  s=$((s+0))
  if [ "$s" -gt "$2" ]; then printf 'O %s\n' "$f"; rm -f -- "./$f"; continue; fi
  printf 'F %s\n' "$f"
  head -c "$2" -- "./$f" | base64
  printf '.\n'
  rm -f -- "./$f"
done
exit 0
"""


@dataclass(frozen=True)
class SpoolFile:
    name: str
    data: bytes


@dataclass(frozen=True)
class BridgedSession:
    instance_id: str
    ended: bool
    ts: int


@dataclass
class SyncResult:
    launches: int = 0
    imported: int = 0
    rejected: int = 0
    phase_records: int = 0
    capped: int = 0
    replaced: int = 0
    busy: int = 0
    errors: list[str] = field(default_factory=list)


def pull_command(spool: str) -> list[str]:
    return ["sh", "-c", PULL_SCRIPT, "sh", spool, str(MAX_EVENT_BYTES), str(MAX_FILES_PER_PULL)]


def parse_pull(text: str) -> tuple[list[SpoolFile], int]:
    """``(files, dropped)`` from the pull script's output; stops at broken framing."""
    files: list[SpoolFile] = []
    dropped = 0
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line:
            continue
        if line == "X" or line.startswith("O "):
            dropped += 1
            continue
        if not line.startswith("F "):
            dropped += 1
            break
        name = line[2:]
        chunks = []
        closed = False
        while index < len(lines):
            body = lines[index]
            index += 1
            if body == ".":
                closed = True
                break
            chunks.append(body)
        if not closed:
            dropped += 1
            break
        try:
            data = base64.b64decode("".join(chunks), validate=True)
        except (binascii.Error, ValueError):
            dropped += 1
            continue
        if len(files) >= MAX_FILES_PER_PULL:
            dropped += 1
            continue
        files.append(SpoolFile(name, data))
    return files, dropped


def _payload(spool_file: SpoolFile, harness: str) -> tuple[str, int, dict] | None:
    """``(event, timestamp seconds, payload)`` for an acceptable spool file."""
    match = FILE_NAME_RE.match(spool_file.name)
    if match is None or len(spool_file.data) > MAX_EVENT_BYTES:
        return None
    event = match.group(3)
    if event not in EVENTS_BY_HARNESS.get(harness, ()):
        return None
    try:
        payload = json.loads(spool_file.data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(payload, dict):
        return None
    return event, int(match.group(1)) // 1_000_000_000, payload


def claimed_process(launch_id: str) -> tuple[LaunchRecord, ProcessInfo] | None:
    """The running launch and its live pane process, or None when either is gone."""
    record = launches.get(launch_id)
    if record is None or record.state != "running" or record.pid is None:
        return None
    if not record.process_start:
        return None
    process = discovery.get_process_info(record.pid)
    if process is None or process.start_token != record.process_start:
        return None
    return record, process


def build_record(launch: state.SandboxLaunch, process: ProcessInfo, event: str,
                 payload: dict, timestamp: int, now: int) -> dict | None:
    """The session record for one spooled event, or None when it is refused."""
    harness = launch.harness
    try:
        session_id = hook._text(payload, "session_id", MAX_ID_LENGTH, required=True)
        sandbox_cwd = hook._text(payload, "cwd", discovery.MAX_PATH_LENGTH)
        if event == FLEET_EVENT:
            sequence = hook._integer(payload, "sequence")
            if sequence is None:
                return None
            status, detail, tool, event_id = hook._normalized_update(harness, payload)
        else:
            sequence = None
            status, detail, tool = hook._event_update(event, payload)
            event_id = f"{harness}.{event}"
    except ValueError:
        return None
    if session_id.startswith(PHASE_SESSION_PREFIX):
        return None
    instance_id = f"{process.pid}:{process.start_token}"
    existing = discovery.read_session_record(harness, session_id, instance_id)
    if existing is None and event not in CREATING_EVENTS:
        return None
    started = now if existing is None or event == "session-start" else existing.get(
        "started", now)
    ts = min(timestamp, now) if timestamp > 0 else now
    record = hook.build_session_record(
        agent=harness,
        session_id=session_id,
        instance_id=instance_id,
        cwd=launch.cwd or sandbox_cwd,
        process=process,
        process_identity="identified",
        status=status,
        detail=detail,
        tool=tool,
        now=ts,
        started=started,
        event_id=event_id,
        terminal=launch.terminal,
        terminal_env=dict(launch.terminal_env),
        sequence=sequence,
        launch={"launch_id": launch.launch_id, "profile_id": launch.profile},
        source=SOURCE,
    )
    record["sandbox"] = launch.sandbox
    return record


def phase_record(launch: state.SandboxLaunch, process: ProcessInfo, phase: str,
                 now: int) -> dict:
    status, detail = PHASE_STATUS.get(phase, ("started", f"sandbox {phase.lower()}"))
    record = hook.build_session_record(
        agent=launch.harness,
        session_id=f"{PHASE_SESSION_PREFIX}{launch.launch_id}",
        instance_id=f"{process.pid}:{process.start_token}",
        cwd=launch.cwd,
        process=process,
        process_identity="identified",
        status=status,
        detail=detail,
        tool="",
        now=now,
        started=launch.created,
        event_id=f"{launch.harness}.sandbox-phase",
        terminal=launch.terminal,
        terminal_env=dict(launch.terminal_env),
        launch={"launch_id": launch.launch_id, "profile_id": launch.profile},
        source=SOURCE,
    )
    record["sandbox"] = launch.sandbox
    return record


def relay_chat(launch: state.SandboxLaunch, client: OpenShell) -> int:
    """Seam for the M5 spool-only chat mode (row 11); relays nothing yet."""
    return 0


def _bridged(record: dict) -> BridgedSession:
    ts = record.get("ts")
    return BridgedSession(
        instance_id=str(record.get("instance_id") or ""),
        ended=record.get("status") == "ended",
        ts=ts if type(ts) is int else 0,
    )


def hook_sessions(launch_ids) -> dict[str, dict[str, BridgedSession]]:
    """The bridged hook sessions per launch id, from one read of the store."""
    wanted = set(launch_ids)
    found: dict[str, dict[str, BridgedSession]] = {launch_id: {} for launch_id in wanted}
    if not wanted:
        return found
    for record in discovery.read_stored_sessions():
        launch_id = (record.get("launch") or {}).get("launch_id")
        session_id = record.get("session_id")
        if (
            record.get("source") == SOURCE
            and launch_id in wanted
            and isinstance(session_id, str)
            and not session_id.startswith(PHASE_SESSION_PREFIX)
        ):
            found[launch_id][session_id] = _bridged(record)
    return found


def _replace_oldest_ended(launch: state.SandboxLaunch,
                          sessions: dict[str, BridgedSession]) -> bool:
    """Delete the launch's oldest ended record to make room; False when all are live."""
    ended = sorted(
        (item.ts, session_id, item.instance_id)
        for session_id, item in sessions.items() if item.ended
    )
    for _ts, session_id, instance_id in ended:
        if discovery.delete_session_record(launch.harness, session_id, instance_id):
            del sessions[session_id]
            return True
    return False


@contextmanager
def launch_lock(launch_id: str):
    """The launch's sync lock, taken without waiting; ``TimeoutError`` when held.

    A private 0600 file under ``state_root()/locks``, opened without following
    symlinks.
    """
    if not launches.is_launch_id(launch_id):
        raise OSError(f"invalid launch id {launch_id!r}")
    directory = paths.ensure_private_dir(state.state_root(create=True) / LOCKS_DIRECTORY)
    with discovery._file_lock(directory / f"{launch_id}.lock", timeout=0):
        yield


def _remove_lock(launch_id: str) -> None:
    path = state.state_root() / LOCKS_DIRECTORY / f"{launch_id}.lock"
    try:
        if stat.S_ISREG(path.lstat().st_mode):
            path.unlink()
    except OSError:
        pass


def _update_phase(launch: state.SandboxLaunch, process: ProcessInfo, client: OpenShell,
                  result: SyncResult, now: int,
                  sessions: dict[str, BridgedSession]) -> None:
    instance_id = f"{process.pid}:{process.start_token}"
    session_id = f"{PHASE_SESSION_PREFIX}{launch.launch_id}"
    if sessions:
        discovery.delete_session_record(launch.harness, session_id, instance_id)
        return
    try:
        info: SandboxInfo | None = client.get(launch.sandbox, timeout=PHASE_TIMEOUT)
    except OpenShellError as error:
        result.errors.append(f"{launch.sandbox}: {error}")
        return
    phase = "Deleting" if info is None else info.phase
    record = phase_record(launch, process, phase, now)
    current = discovery.read_session_record(launch.harness, session_id, instance_id)
    if current is not None and (current.get("status"), current.get("detail")) == (
        record["status"], record["detail"]
    ):
        return
    if session_store.import_session_record(record):
        result.phase_records += 1


def sync_launch(launch: state.SandboxLaunch, client: OpenShell, process: ProcessInfo,
                result: SyncResult, *, now: int | None = None,
                sessions: dict[str, BridgedSession] | None = None) -> None:
    """Pull and import one launch's spool, then refresh its phase record.

    ``sessions`` holds the launch's bridged sessions (``hook_sessions``) and
    is updated in place; None reads them from the store. The caller holds
    the launch's ``launch_lock``.
    """
    now = int(time.time()) if now is None else now
    if sessions is None:
        sessions = hook_sessions([launch.launch_id])[launch.launch_id]
    capped = 0
    try:
        output = client.exec_capture(launch.sandbox, pull_command(launch.spool),
                                     timeout=PULL_TIMEOUT)
    except OpenShellError as error:
        result.errors.append(f"{launch.sandbox}: {error}")
        output = None
    if output is not None and not output.ok:
        result.errors.append(f"{launch.sandbox}: spool pull failed: {output.error_text()}")
    elif output is not None:
        files, dropped = parse_pull(output.stdout)
        result.rejected += dropped
        for spool_file in sorted(files, key=lambda item: item.name):
            parsed = _payload(spool_file, launch.harness)
            record = None if parsed is None else build_record(
                launch, process, parsed[0], parsed[2], parsed[1], now
            )
            replacing = False
            if (
                record is not None
                and record["session_id"] not in sessions
                and len(sessions) >= MAX_SESSIONS_PER_LAUNCH
            ):
                if _replace_oldest_ended(launch, sessions):
                    replacing = True
                else:
                    capped += 1
                    record = None
            if record is not None and session_store.import_session_record(record):
                result.imported += 1
                if replacing:
                    result.replaced += 1
                sessions[record["session_id"]] = _bridged(record)
            else:
                result.rejected += 1
        relay_chat(launch, client)
    if capped:
        result.capped += capped
        result.errors.append(
            f"{launch.sandbox}: dropped {capped} event(s) for sessions beyond "
            f"{MAX_SESSIONS_PER_LAUNCH} per launch"
        )
    _update_phase(launch, process, client, result, now, sessions)


_SYNC_LOCK = threading.Lock()


def sync_all(*, now: int | None = None, client_for=None) -> SyncResult:
    """Sync every live sandboxed launch once; never raises.

    A launch whose launch record is gone, or whose claimed pane process
    exited, has its sandbox launch record removed. Skips (empty result)
    while another sync in this process is running, and skips a launch
    (``busy``) whose ``launch_lock`` another process holds.
    """
    result = SyncResult()
    records = state.list_records()
    if not records or not _SYNC_LOCK.acquire(blocking=False):
        return result
    try:
        with ExitStack() as held:
            live = []
            for launch in records:
                claimed = claimed_process(launch.launch_id)
                if claimed is None:
                    state.remove(launch.launch_id)
                    _remove_lock(launch.launch_id)
                    continue
                try:
                    held.enter_context(launch_lock(launch.launch_id))
                except TimeoutError:
                    result.busy += 1
                    continue
                except OSError as error:
                    result.errors.append(f"{launch.sandbox}: sync lock: {error}")
                    continue
                live.append((launch, claimed[1]))
            known = hook_sessions(launch.launch_id for launch, _process in live)
            _sync_live(live, known, result, now, client_for)
    finally:
        _SYNC_LOCK.release()
    return result


def summary_lines(result: SyncResult) -> list[str]:
    """What one ``sync_all`` pass did, as printed by both sync entry points."""
    lines = [
        f"{result.launches} sandboxed launch(es): {result.imported} event(s) imported, "
        f"{result.rejected} rejected, {result.phase_records} phase update(s)"
    ]
    if result.replaced:
        noun = "session" if result.replaced == 1 else "sessions"
        lines.append(f"{result.replaced} ended {noun} replaced to stay within "
                     f"{MAX_SESSIONS_PER_LAUNCH} per launch")
    if result.busy:
        noun = "launch" if result.busy == 1 else "launches"
        lines.append(f"{result.busy} {noun} busy in another process; try again")
    return lines


def _sync_live(live, known, result: SyncResult, now: int | None, client_for) -> None:
    clients: dict[str | None, OpenShell | None] = {}
    for launch, process in live:
        if launch.gateway not in clients:
            try:
                clients[launch.gateway] = (
                    client_for(launch.gateway) if client_for is not None
                    else OpenShell(gateway=launch.gateway)
                )
            except OpenShellError as error:
                result.errors.append(str(error))
                clients[launch.gateway] = None
        client = clients[launch.gateway]
        if client is None:
            continue
        result.launches += 1
        try:
            sync_launch(launch, client, process, result, now=now,
                        sessions=known[launch.launch_id])
        except (OSError, ValueError) as error:
            result.errors.append(f"{launch.sandbox}: {error}")


__all__ = [
    "BridgedSession",
    "MAX_FILES_PER_PULL",
    "MAX_SESSIONS_PER_LAUNCH",
    "PULL_SCRIPT",
    "SyncResult",
    "build_record",
    "hook_sessions",
    "launch_lock",
    "parse_pull",
    "pull_command",
    "relay_chat",
    "summary_lines",
    "sync_all",
    "sync_launch",
]
