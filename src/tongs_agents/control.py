"""The one policy layer for driving agent panes: verify, send, read, status, wait.

Control acts only on panes that tongs-agents spawned and can verify now.
``verify(launch_id)`` accepts a launch only when its record is claimed and
not replaced, its pane is listed in the same session (and socket dir), the
claimed process is still alive with the same start token
(``_identity_matches``, the one seam for stronger launch-identity checks),
and no hook session record for that launch names another pane. A hook record
counts for a launch only when it carries the launch id and its process is the
claimed one or runs under it (``launches.claims_session``, #74): a shared
Codex daemon runs later sessions' hooks with the first launch's environment.
Everything else is refused (exit 5), never guessed.

``send`` holds a per-pane lock (``runtime_dir()/locks``), so concurrent sends
to one pane arrive whole and in turn: it pastes the text in one bracketed
paste, waits ``[control].send_delay_ms`` and then sends Enter as a separate
call. Text is at most 64 KiB and may hold tabs and newlines but no other
control characters, so a paste can never carry its own end marker.

Every call, including refusals, appends one line to
``state_dir()/audit/control.jsonl`` (0600, rotated by size): time, actor,
caller and target launch ids, action, byte count, SHA-256, outcome and exit
code. Sent or read text is never logged.

Multiplexer adapters hold no policy; this module is the only caller of their
pane I/O.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from tongs_agents import discovery, launches, paths
from tongs_agents.attention import Attention, attention_state
from tongs_agents.launches import LaunchRecord
from tongs_agents.models import SessionStatus, parse_session, validated_launch
from tongs_agents.multiplexers import get_multiplexer
from tongs_agents.multiplexers.base import (
    Multiplexer,
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
)
from tongs_agents.multiplexers.tmux import env_socket
from tongs_agents.profiles import ProfileError, _read_config_bytes, profiles_path

try:
    import fcntl
except ImportError:  # Windows: no pane control there (no supported multiplexer).
    fcntl = None

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_TIMEOUT = 2
EXIT_BLOCKED = 3
EXIT_EXITED = 4
EXIT_REFUSED = 5
EXIT_UNSUPPORTED = 6

MAX_SEND_BYTES = 64 * 1024
MAX_READ_LINES = 200
MAX_WAIT_SECONDS = 600
MAX_SEND_DELAY_MS = 5000
DEFAULT_SEND_DELAY_MS = 150
LOCK_TIMEOUT = 30.0
LOCK_POLL = 0.02
WAIT_POLL = 0.5
AUDIT_MAX_BYTES = 1024 * 1024
AUDIT_DIR = "audit"
AUDIT_FILE = "control.jsonl"
LOCKS_DIR = "locks"
SENT_DIR = "sent"
_TMUX_PANE_RE = re.compile(r"%[0-9]{1,12}\Z")
MAX_MARK_BYTES = 64 * 1024
MAX_ACTOR_LENGTH = 64
CALLER_ENV = "TONGS_AGENTS_LAUNCH_ID"
ENDED = "ended"
UNTIL = ("done", "needs-you", "ended", "any")
STATES = tuple(item.value for item in Attention) + (ENDED,)
_SETTLED = {Attention.DONE.value, Attention.NEEDS_YOU.value, Attention.FAILED.value}
_ALLOWED_CONTROLS = {"\t", "\n"}


class ControlError(Exception):
    """A control call that did not run; ``exit_code`` is the CLI status."""

    exit_code = EXIT_ERROR
    outcome = "error"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class Refused(ControlError):
    """The target is not a pane tongs-agents spawned and verified."""

    exit_code = EXIT_REFUSED
    outcome = "refused"


class AgentExited(ControlError):
    """The launched pane's program has exited."""

    exit_code = EXIT_EXITED
    outcome = "exited"

    def __init__(self, detail: str, exit_status: int | None = None):
        super().__init__(detail)
        self.exit_status = exit_status


class ControlUnsupported(ControlError):
    """The launch's multiplexer has no pane control."""

    exit_code = EXIT_UNSUPPORTED
    outcome = "unsupported"


@dataclass(frozen=True)
class Settings:
    send_delay_ms: int = DEFAULT_SEND_DELAY_MS
    mcp_enabled: bool = False


@dataclass(frozen=True)
class Target:
    """A verified launch: its record, pane ref, live pane and multiplexer."""

    record: LaunchRecord
    ref: PaneRef
    pane: PaneInfo
    multiplexer: Multiplexer


@dataclass(frozen=True)
class AgentStatus:
    launch_id: str
    profile: str
    harness: str
    session: str
    pane_id: str
    state: str
    detail: str = ""
    exit_status: int | None = None
    updated: int | None = None
    label: str = ""

    def to_json(self) -> dict:
        return {
            "launch_id": self.launch_id,
            "profile": self.profile,
            "label": self.label,
            "harness": self.harness,
            "session": self.session,
            "pane_id": self.pane_id,
            "state": self.state,
            "detail": self.detail,
            "exit_status": self.exit_status,
            "updated": self.updated,
        }


@dataclass(frozen=True)
class WaitResult:
    status: AgentStatus
    exit_code: int

    @property
    def reached(self) -> bool:
        return self.exit_code == EXIT_OK

    @property
    def timed_out(self) -> bool:
        return self.exit_code == EXIT_TIMEOUT


def load_settings(path: Path | None = None) -> Settings:
    """The ``[control]`` section of the config file; absent means defaults."""
    path = profiles_path() if path is None else path
    try:
        raw = _read_config_bytes(path)
    except ProfileError as error:
        raise ControlError(str(error)) from None
    if raw is None:
        return Settings()
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ControlError(f"{path}: invalid config: {error}") from None
    section = document.get("control", {})
    if not isinstance(section, dict):
        raise ControlError(f"{path}: control: must be a table")
    delay = section.get("send_delay_ms", DEFAULT_SEND_DELAY_MS)
    if type(delay) is not int or not 0 <= delay <= MAX_SEND_DELAY_MS:
        raise ControlError(
            f"{path}: control.send_delay_ms: must be an integer from 0 to "
            f"{MAX_SEND_DELAY_MS}"
        )
    enabled = section.get("mcp_enabled", False)
    if not isinstance(enabled, bool):
        raise ControlError(f"{path}: control.mcp_enabled: must be true or false")
    return Settings(send_delay_ms=delay, mcp_enabled=enabled)


def caller_launch_id(env=None) -> str | None:
    """This process's own launch id when it runs in a launched pane."""
    value = (os.environ if env is None else env).get(CALLER_ENV)
    return value if launches.is_launch_id(value) else None


# Verification


def _multiplexer(record: LaunchRecord) -> Multiplexer:
    try:
        return get_multiplexer(record.multiplexer, socket_dir=record.socket_dir)
    except Unsupported as error:
        raise ControlUnsupported(error.detail) from None
    except MultiplexerError as error:
        raise Refused(f"launch {record.launch_id}: {error.detail}") from None


def _identity_matches(record: LaunchRecord) -> bool:
    """True when the record's claimed process is still the one running.

    The single launch-identity seam: the pid must be alive with the start
    token captured at claim time. Hook records are matched to this process
    separately, in ``_hook_records``.
    """
    if record.pid is None or not record.process_start:
        return False
    info = discovery.get_process_info(record.pid)
    return info is not None and info.start_token == record.process_start


def _same_dir(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return True
    return os.path.normpath(left) == os.path.normpath(right)


def _replaced(launch_id: str) -> bool:
    return any(record.replaces == launch_id for record in launches.list_records())


def _hook_records(record: LaunchRecord) -> list[dict]:
    """Session records for ``record``: its launch id and its claimed process.

    A record carrying the launch id whose process is not the claimed one (or
    under it) has no launch identity and is ignored, never trusted (#74).
    """
    found = []
    for data in discovery.read_session_records():
        launch = validated_launch(data.get("launch")) or {}
        if launch.get("launch_id") == record.launch_id and launches.claims_session(
            record, data
        ):
            found.append(data)
    return found


def _names_another_pane(record: LaunchRecord, env: dict) -> bool:
    session = env.get("ZELLIJ_SESSION_NAME")
    pane = env.get("ZELLIJ_PANE_ID")
    socket_dir = env.get("ZELLIJ_SOCKET_DIR")
    if (
        (pane is not None and pane != record.pane_id)
        or (session is not None and session != record.session)
        or not _same_dir(socket_dir, record.socket_dir)
    ):
        return True
    if record.multiplexer != "tmux":
        return False
    tmux_pane = env.get("TMUX_PANE")
    return (
        (isinstance(tmux_pane, str) and _TMUX_PANE_RE.match(tmux_pane) is not None
         and tmux_pane != record.pane_id)
        or not _same_dir(env_socket(env), record.socket_dir)
    )


def _check_hook_pane(record: LaunchRecord, hooks: list[dict]) -> None:
    """Refuse when a hook record for this launch names a different pane.

    Zellij variables are compared for every launch; ``TMUX_PANE`` and the
    server socket in ``TMUX`` for tmux launches. A record that names no pane,
    or a malformed ``TMUX_PANE``, does not contradict the launch.
    """
    for data in hooks:
        env = data.get("terminal_env")
        if isinstance(env, dict) and _names_another_pane(record, env):
            raise Refused(
                f"launch {record.launch_id}: a session record for it names "
                "another pane"
            )


def _claimed(launch_id) -> LaunchRecord:
    if not launches.is_launch_id(launch_id):
        raise Refused("not a tongs-agents launch id")
    record = launches.get(launch_id)
    if record is None:
        raise Refused(f"launch {launch_id} was not spawned by tongs-agents")
    if record.state != "running" or record.pid is None or not record.pane_id:
        raise Refused(f"launch {launch_id} has not been claimed by its pane")
    return record


def _verify(launch_id) -> Target:
    record = _claimed(launch_id)
    if _replaced(launch_id):
        raise Refused(f"launch {launch_id} was replaced by a later launch")
    mux = _multiplexer(record)
    try:
        panes = mux.list_panes(record.session)
    except Unsupported as error:
        raise ControlUnsupported(error.detail) from None
    except MultiplexerError as error:
        raise Refused(
            f"launch {launch_id}: cannot list session {record.session!r}: "
            f"{error.detail}"
        ) from None
    pane = next((item for item in panes if item.ref.pane_id == record.pane_id), None)
    if pane is None:
        raise Refused(
            f"launch {launch_id}: pane {record.pane_id} is not in session "
            f"{record.session!r}"
        )
    if not _same_dir(pane.ref.socket_dir, record.socket_dir):
        raise Refused(f"launch {launch_id}: pane is served from another socket dir")
    if pane.exited:
        shown = "" if pane.exit_status is None else f" with status {pane.exit_status}"
        raise AgentExited(f"launch {launch_id}: the agent exited{shown}", pane.exit_status)
    if not _identity_matches(record):
        raise Refused(
            f"launch {launch_id}: pane {record.pane_id} no longer runs the launched "
            "process (stale launch)"
        )
    _check_hook_pane(record, _hook_records(record))
    ref = PaneRef(record.multiplexer, record.session, record.pane_id, record.socket_dir)
    return Target(record, ref, pane, mux)


def verify(launch_id: str) -> PaneRef:
    """The verified pane of ``launch_id``; raises Refused, AgentExited or
    ControlUnsupported otherwise."""
    return _verify(launch_id).ref


def client_focused(launch_id: str) -> bool:
    """True when an attached client has the verified pane of ``launch_id`` focused.

    Zellij answers from ``list-clients``; a multiplexer without a per-client
    readback (tmux) answers with the pane's own focus flag, which errs toward
    focused. Chat wake (#32) uses it so a wake never lands in a pane the
    maintainer is typing in.
    """
    target = _verify(launch_id)
    focused_panes = getattr(target.multiplexer, "focused_panes", None)
    if focused_panes is None:
        return target.pane.focused
    try:
        return target.ref.pane_id in focused_panes(target.ref.session)
    except MultiplexerError as error:
        raise ControlError(
            f"launch {launch_id}: cannot read the attached clients: {error.detail}"
        ) from None


# Audit log


def _actor(value) -> str:
    if (
        isinstance(value, str)
        and 0 < len(value) <= MAX_ACTOR_LENGTH
        and all(char.isalnum() or char in "-_.:" for char in value)
    ):
        return value
    return "unknown"


def _audit_dir() -> Path:
    try:
        paths.state_dir(create=True)
        return paths.ensure_private_dir(paths.state_dir() / AUDIT_DIR)
    except OSError as error:
        raise ControlError(f"cannot use the audit log directory: {error}") from None


def _rotate(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISREG(info.st_mode) and info.st_size >= AUDIT_MAX_BYTES:
        os.replace(path, path.with_name(path.name + ".1"))


def audit(
    action: str,
    *,
    actor: str,
    launch_id,
    outcome: str,
    exit_code: int,
    data: bytes | None = None,
    caller: str | None = None,
) -> None:
    """Append one audit line; the text itself is never written."""
    entry = {
        "ts": round(time.time(), 3),
        "actor": _actor(actor),
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
        directory = _audit_dir()
        path = directory / AUDIT_FILE
        with _locked(directory / ".audit.lock"):
            _rotate(path)
            descriptor = os.open(path, flags, 0o600)
            try:
                info = os.fstat(descriptor)
                if stat.S_ISREG(info.st_mode):
                    os.write(descriptor, line)
            finally:
                os.close(descriptor)
    except (OSError, ControlError) as error:
        # An audit failure must never change the outcome: after a successful
        # send, exit 1 would make a retrying script submit the prompt twice.
        print(f"tongs-agents: control audit not written: {error}", file=sys.stderr)


# Locking


@contextmanager
def _locked(path: Path, timeout: float | None = None):
    timeout = LOCK_TIMEOUT if timeout is None else timeout
    if fcntl is None:
        raise ControlUnsupported("pane control needs POSIX file locks")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path}: not a regular lock file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ControlError(f"timed out waiting for {path.name}") from None
                time.sleep(LOCK_POLL)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def lock_path(session: str, pane_id: str) -> Path:
    """The send lock of one pane: runtime_dir()/locks/<sha256(session, pane)>."""
    key = json.dumps([session, pane_id], separators=(",", ":")).encode("utf-8")
    return paths.runtime_dir() / LOCKS_DIR / hashlib.sha256(key).hexdigest()


def _pane_lock(record: LaunchRecord):
    path = lock_path(record.session, record.pane_id or "")
    try:
        paths.ensure_private_dir(paths.runtime_dir(create=True) / LOCKS_DIR)
    except OSError as error:
        raise ControlError(f"cannot use the lock directory: {error}") from None
    return _locked(path)


# Send


def _send_bytes(text) -> bytes:
    if not isinstance(text, str):
        raise ControlError("text must be a string")
    for char in text:
        code = ord(char)
        if (code < 32 or 127 <= code < 160) and char not in _ALLOWED_CONTROLS:
            raise ControlError(
                f"text holds control character U+{code:04X}; only tab and newline "
                "are allowed"
            )
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise ControlError("text is not valid UTF-8") from None
    if len(data) > MAX_SEND_BYTES:
        raise ControlError(f"text is {len(data)} bytes; the limit is {MAX_SEND_BYTES}")
    return data


def _mark_path(launch_id: str) -> Path:
    return paths.runtime_dir() / SENT_DIR / f"{launch_id}.json"


def _hook_marks(hooks: list[dict]) -> dict[str, int]:
    marks = {}
    for data in hooks:
        identity = data.get("canonical_id") or data.get("session_id")
        if isinstance(identity, str) and identity:
            marks[identity] = _arrival(data)
    return marks


def _arrival(data: dict) -> int:
    value = data.get("arrival_sequence")
    return value if type(value) is int else 0


def _write_mark(launch_id: str, marks: dict[str, int]) -> None:
    """Remember which hook updates predate the last send, for ``wait``."""
    try:
        directory = paths.ensure_private_dir(paths.runtime_dir(create=True) / SENT_DIR)
        path = directory / f"{launch_id}.json"
        temp = directory / f".{launch_id}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"sent": time.time(), "hooks": marks}, stream)
        os.replace(temp, path)
    except OSError:
        pass


def _read_mark(launch_id: str) -> dict[str, int] | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(_mark_path(launch_id), flags)
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = json.loads(stream.read(MAX_MARK_BYTES))
    except (OSError, ValueError):
        return None
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return None
    return {
        key: value for key, value in hooks.items()
        if isinstance(key, str) and type(value) is int
    }


def send(
    launch_id: str,
    text: str,
    *,
    actor: str,
    submit: bool = True,
    caller: str | None = None,
    settings: Settings | None = None,
) -> None:
    """Paste ``text`` into the verified pane, then press Enter separately.

    Raises ControlError (or a subclass) after auditing the refusal.
    """
    data = None
    try:
        data = _send_bytes(text)
        settings = load_settings() if settings is None else settings
        record = _claimed(launch_id)
        with _pane_lock(record):
            target = _verify(launch_id)
            marks = _hook_marks(_hook_records(target.record))
            try:
                if text:
                    target.multiplexer.paste(target.ref, text)
                if submit:
                    if text:
                        time.sleep(settings.send_delay_ms / 1000)
                    target.multiplexer.send_keys(target.ref, ["Enter"])
            except Unsupported as error:
                raise ControlUnsupported(error.detail) from None
            except MultiplexerError as error:
                raise ControlError(f"send to launch {launch_id} failed: {error.detail}") from None
            _write_mark(launch_id, marks)
    except ControlError as error:
        audit("send", actor=actor, launch_id=launch_id, outcome=error.outcome,
              exit_code=error.exit_code, data=data, caller=caller)
        raise
    audit("send", actor=actor, launch_id=launch_id, outcome="ok", exit_code=EXIT_OK,
          data=data, caller=caller)


# Read


def read(
    launch_id: str, lines: int = MAX_READ_LINES, *, actor: str = "cli",
    caller: str | None = None,
) -> str:
    """The last ``lines`` (1 to 200) lines of the verified pane."""
    try:
        if type(lines) is not int or not 1 <= lines <= MAX_READ_LINES:
            raise ControlError(f"lines must be from 1 to {MAX_READ_LINES}")
        target = _verify(launch_id)
        try:
            text = target.multiplexer.capture(target.ref, lines)
        except Unsupported as error:
            raise ControlUnsupported(error.detail) from None
        except MultiplexerError as error:
            raise ControlError(f"read of launch {launch_id} failed: {error.detail}") from None
    except ControlError as error:
        audit("read", actor=actor, launch_id=launch_id, outcome=error.outcome,
              exit_code=error.exit_code, caller=caller)
        raise
    audit("read", actor=actor, launch_id=launch_id, outcome="ok", exit_code=EXIT_OK,
          data=text.encode("utf-8", errors="replace"), caller=caller)
    return text


# Status and wait


def _status_of(record: LaunchRecord, state: str, detail: str = "", *,
               exit_status: int | None = None, updated: int | None = None) -> AgentStatus:
    return AgentStatus(
        launch_id=record.launch_id,
        profile=record.profile,
        harness=record.harness,
        session=record.session,
        pane_id=record.pane_id or "",
        state=state,
        detail=detail,
        exit_status=exit_status,
        updated=updated,
        label=record.label or "",
    )


def _latest(hooks: list[dict]) -> dict | None:
    if not hooks:
        return None
    return max(hooks, key=lambda data: (data.get("ts", 0) if type(data.get("ts")) is int
                                        else 0, _arrival(data)))


def _current(launch_id: str) -> tuple[AgentStatus, dict | None]:
    """The launch's status and the hook record it came from, if any."""
    try:
        target = _verify(launch_id)
    except AgentExited as error:
        record = launches.get(launch_id)
        return _status_of(record, ENDED, error.detail, exit_status=error.exit_status), None
    hook = _latest(_hook_records(target.record))
    if hook is None:
        return _status_of(target.record, Attention.QUIET.value,
                          "running; no status reported"), None
    session = parse_session(hook)
    state = ENDED if session.status == SessionStatus.ENDED else attention_state(session).value
    detail = session.detail if isinstance(session.detail, str) else ""
    updated = session.ts if type(session.ts) is int else None
    return _status_of(target.record, state, detail[:200], updated=updated), hook


def status(launch_id: str, *, actor: str = "cli", caller: str | None = None) -> AgentStatus:
    """The attention state of a verified launch, or ``ended`` once it exited."""
    try:
        result, _ = _current(launch_id)
    except ControlError as error:
        audit("status", actor=actor, launch_id=launch_id, outcome=error.outcome,
              exit_code=error.exit_code, caller=caller)
        raise
    audit("status", actor=actor, launch_id=launch_id, outcome="ok", exit_code=EXIT_OK,
          caller=caller)
    return result


def _fresh(hook: dict | None, marks: dict[str, int] | None) -> bool:
    if hook is None or marks is None:
        return True
    identity = hook.get("canonical_id") or hook.get("session_id")
    return identity not in marks or _arrival(hook) > marks[identity]


def wait_code(until: str, state: str, fresh: bool = True) -> int | None:
    """The exit code that ends a wait for ``until`` in ``state``, else None."""
    if state == ENDED:
        return EXIT_OK if until in (ENDED, "any") else EXIT_EXITED
    if not fresh:
        return None
    if until == "any":
        return EXIT_OK if state in _SETTLED else None
    if until == Attention.DONE.value:
        return {
            Attention.DONE.value: EXIT_OK,
            Attention.NEEDS_YOU.value: EXIT_BLOCKED,
            Attention.FAILED.value: EXIT_ERROR,
        }.get(state)
    if until == Attention.NEEDS_YOU.value:
        return EXIT_OK if state == Attention.NEEDS_YOU.value else None
    return None


def wait(
    launch_id: str,
    until: str = Attention.DONE.value,
    timeout_s: float = MAX_WAIT_SECONDS,
    *,
    actor: str = "cli",
    caller: str | None = None,
    poll: float = WAIT_POLL,
    check: Callable[[], None] | None = None,
) -> WaitResult:
    """Poll the launch's status until ``until`` holds, it settles otherwise,
    or ``timeout_s`` (at most 600) passes.

    After a send, hook updates that predate it do not count, so the previous
    turn's ``done`` never ends the wait for the next one. ``check`` runs
    after every poll interval; a ControlError it raises ends the wait.
    """
    try:
        if until not in UNTIL:
            raise ControlError(f"until must be one of {', '.join(UNTIL)}")
        if (
            not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s) or not 0 < timeout_s <= MAX_WAIT_SECONDS
        ):
            raise ControlError(f"timeout must be more than 0 and at most {MAX_WAIT_SECONDS} s")
        marks = _read_mark(launch_id) if launches.is_launch_id(launch_id) else None
        deadline = time.monotonic() + timeout_s
        while True:
            current, hook = _current(launch_id)
            code = wait_code(until, current.state, _fresh(hook, marks))
            if code is not None:
                result = WaitResult(current, code)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = WaitResult(current, EXIT_TIMEOUT)
                break
            time.sleep(min(poll, remaining))
            if check is not None:
                check()
    except ControlError as error:
        audit("wait", actor=actor, launch_id=launch_id, outcome=error.outcome,
              exit_code=error.exit_code, caller=caller)
        raise
    outcome = "ok" if result.reached else ("timeout" if result.timed_out else current.state)
    audit("wait", actor=actor, launch_id=launch_id, outcome=outcome,
          exit_code=result.exit_code, caller=caller)
    return result


def list_statuses(*, actor: str = "cli", caller: str | None = None) -> list[AgentStatus]:
    """The status of every claimed, not replaced launch; refusals become rows."""
    records = launches.list_records()
    replaced = {record.replaces for record in records if record.replaces}
    rows = []
    for record in records:
        if record.state != "running" or record.launch_id in replaced:
            continue
        try:
            current, _ = _current(record.launch_id)
        except ControlError as error:
            current = _status_of(record, error.outcome, error.detail)
        rows.append(current)
    audit("status", actor=actor, launch_id=None, outcome="ok", exit_code=EXIT_OK,
          caller=caller)
    return rows
