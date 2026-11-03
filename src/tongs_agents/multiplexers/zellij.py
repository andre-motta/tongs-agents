"""Zellij pane lifecycle: spawn, list and select panes in a session.

Shapes follow the M1-0 spike (docs/work/workspace-spike.md) on zellij 0.44.2
and 0.45.1. Every call is an argv list with stdin closed, a 5 s timeout and the
session's socket dir in its environment. `action` exits 0 for an unknown or
EXITED session, so output is parsed strictly and anything unexpected raises.
Focus moves only the client that most recently sent input and is confirmed by
`list-clients` readback; no emulator window is ever activated.

Pane I/O always names the pane with `--pane-id`: `paste` (bracketed when the
pane's program turned bracketed paste on, at most 64 KiB), `send-keys`,
`dump-screen --full` into a 0600 temp file under runtime_dir() that is read
and unlinked, and a bounded `subscribe --format json` stream. `action` exits 0
for an unknown pane id, so callers verify the pane first (control.py); the
adapter holds no policy.
"""

from __future__ import annotations

import json
import os
import re
import select
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator

from tongs_agents import paths
from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.multiplexers.base import (
    SESSION_EXITED,
    SESSION_RUNNING,
    MultiplexerError,
    PaneInfo,
    PaneRef,
)
from tongs_agents.multiplexers.zellij_binary import (
    ZELLIJ_CEILING,
    ZELLIJ_FLOOR,
    format_version,
    zellij_binary,
    zellij_version,
)
from tongs_agents.terminal_apis._env import is_decimal_id
from tongs_agents.terminal_apis.zellij import (
    ALREADY_FOCUSED,
    COMMAND_TIMEOUT,
    READBACK_ATTEMPTS,
    READBACK_INTERVAL,
    SOCKET_DIR_KEY,
    _absolute_dir,
    _cli_env,
    _client_panes,
    _run,
    _safe_session,
)

NAME = "zellij"
MAX_NAME_BYTES = 256
MAX_ARG_BYTES = 128 * 1024
ACTIVATION_SKIPPED = "skipped: same zellij session"
NO_SESSIONS = "no active zellij sessions"
MAX_PASTE_BYTES = 64 * 1024
MAX_KEYS = 16
MAX_KEY_LENGTH = 32
MAX_CAPTURE_LINES = 10_000
MAX_CAPTURE_BYTES = 4 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
SUBSCRIBE_EVENTS = 1000
SUBSCRIBE_SECONDS = 60.0
STOP_TIMEOUT = 2.0
CAPTURE_DIR = "capture"
MAX_DUMP_POLLS = 20
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ]*\Z")
_NEW_PANE = re.compile(r"terminal_([0-9]{1,12})")
_SESSION_LINE = re.compile(r" \[Created [^\]\n]*\](?: \([^()\n]*\))*")
_PANE_KEYS = {
    "id": int,
    "is_plugin": bool,
    "title": str,
    "exited": bool,
    "is_focused": bool,
}


class _NoActiveSession(MultiplexerError):
    """zellij answered "There is no active session!" for the session."""


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _session(value) -> str:
    session = _safe_session(value)
    if session is None or _has_control(session):
        raise MultiplexerError(f"refused zellij session name {value!r:.80}")
    return session


def _pane_id(value) -> str:
    if not is_decimal_id(value):
        raise MultiplexerError(f"refused zellij pane id {value!r:.40}")
    return value


def _socket_dir(value) -> str | None:
    if value is None:
        return None
    socket_dir = _absolute_dir(value)
    if socket_dir is None:
        raise MultiplexerError(f"refused zellij socket dir {value!r:.80}")
    return socket_dir


def _pane_name(value) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("-")
        or "\0" in value
        or _has_control(value)
        or len(value.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES
    ):
        raise MultiplexerError(f"refused zellij pane name {value!r:.80}")
    return value


def _cwd(value) -> str:
    if not isinstance(value, str) or _absolute_dir(value) is None:
        raise MultiplexerError(f"refused zellij pane cwd {value!r:.80}")
    return value


def _argv(value) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(arg, str) and "\0" not in arg for arg in value)
        or not value[0]
        or sum(len(arg.encode("utf-8", "surrogatepass")) for arg in value)
        > MAX_ARG_BYTES
    ):
        raise MultiplexerError("refused zellij pane argv")
    return list(value)


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _pane_info(entry, session: str, socket_dir: str | None) -> PaneInfo | None:
    if not isinstance(entry, dict):
        raise MultiplexerError("unrecognized zellij list-panes entry")
    for key, kind in _PANE_KEYS.items():
        value = entry.get(key)
        valid = _int(value) if kind is int else isinstance(value, kind)
        if not valid:
            raise MultiplexerError(
                f"unrecognized zellij list-panes entry: field {key!r}"
            )
    if entry["is_plugin"]:
        return None
    if entry["id"] < 0:
        raise MultiplexerError("unrecognized zellij list-panes entry: field 'id'")
    command = None
    for key in ("pane_command", "terminal_command"):
        value = entry.get(key)
        if value is not None and not isinstance(value, str):
            raise MultiplexerError(
                f"unrecognized zellij list-panes entry: field {key!r}"
            )
        if command is None and value:
            command = value
    exit_status = entry.get("exit_status")
    return PaneInfo(
        ref=PaneRef(NAME, session, str(entry["id"]), socket_dir),
        title=entry["title"],
        command=command,
        exited=entry["exited"],
        focused=entry["is_focused"],
        exit_status=exit_status if _int(exit_status) else None,
    )


def _running_sessions(output: str) -> dict[str, bool]:
    """Map each listed line to whether it is EXITED, refusing unknown shapes."""
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if _SESSION_LINE.search(line) is None:
            raise MultiplexerError("unrecognized zellij list-sessions output")
    return {line: "(EXITED" in line for line in lines}


def _failed(detail: str) -> FocusResult:
    return FocusResult(
        state="failed",
        reason="zellij pane selection failed",
        backend=NAME,
        target_found=True,
        selection=FocusOperation(True, False, detail),
        activation=FocusOperation.unavailable(ACTIVATION_SKIPPED),
    )


def _selected(detail: str) -> FocusResult:
    return FocusResult(
        state="partial",
        reason=f"pane selected; window activation {ACTIVATION_SKIPPED}",
        backend=NAME,
        target_found=True,
        selection=FocusOperation(True, True, detail),
        activation=FocusOperation.unavailable(ACTIVATION_SKIPPED),
    )


class ZellijMultiplexer:
    name = NAME

    def __init__(self, socket_dir: str | None = None, binary: str | None = None):
        self._socket_dir = _socket_dir(socket_dir)
        self._binary = binary

    def _effective_socket_dir(self, socket_dir: str | None = None) -> str | None:
        return (
            _socket_dir(socket_dir)
            or self._socket_dir
            or _absolute_dir(os.environ.get(SOCKET_DIR_KEY))
        )

    def _zellij(self) -> str:
        binary = self._binary or zellij_binary()
        if not binary:
            raise MultiplexerError("zellij not found on PATH")
        return binary

    def _action(self, session: str, socket_dir: str | None, *args: str):
        argv = [self._zellij(), "--session", session, "action", *args]
        try:
            result = _run(argv, _cli_env(self._effective_socket_dir(socket_dir)))
        except RuntimeError as error:
            raise _NoActiveSession(str(error)) from error
        if result is None:
            raise MultiplexerError(
                f"zellij action {args[0]} failed: CLI missing, timed out, "
                "unknown session or unsupported version"
            )
        return result

    def available(self) -> tuple[bool, str]:
        binary = self._binary or zellij_binary()
        if not binary:
            return False, "zellij not found on PATH"
        version = zellij_version(binary)
        if version is None:
            return False, "could not read the zellij version"
        shown = format_version(version)
        if version < ZELLIJ_FLOOR:
            return False, (
                f"zellij {shown} is older than the supported floor "
                f"{format_version(ZELLIJ_FLOOR)}"
            )
        if version > ZELLIJ_CEILING:
            return True, (
                f"zellij {shown} (newer than the validated "
                f"{format_version(ZELLIJ_CEILING)})"
            )
        return True, f"zellij {shown}"

    def in_session(self) -> PaneRef | None:
        if not os.environ.get("ZELLIJ"):
            return None
        session = _safe_session(os.environ.get("ZELLIJ_SESSION_NAME"))
        pane_id = os.environ.get("ZELLIJ_PANE_ID")
        if session is None or _has_control(session) or not is_decimal_id(pane_id):
            return None
        return PaneRef(
            NAME, session, pane_id, _absolute_dir(os.environ.get(SOCKET_DIR_KEY))
        )

    def _cli(self, *args: str) -> subprocess.CompletedProcess:
        argv = [self._zellij(), *args]
        try:
            return subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=_cli_env(self._effective_socket_dir()),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MultiplexerError(f"zellij {args[0]} failed: {error}") from error

    def session_state(self, session: str) -> str | None:
        """SESSION_RUNNING, SESSION_EXITED (resurrectable) or None when not listed."""
        session = _session(session)
        result = self._cli("list-sessions", "--no-formatting")
        if result.returncode != 0:
            if NO_SESSIONS in (result.stderr or "").lower():
                return None
            raise MultiplexerError(
                f"zellij list-sessions exited {result.returncode}"
            )
        lines = _running_sessions(result.stdout or "")
        prefix = session + " "
        for line, exited in lines.items():
            if (
                line.startswith(prefix)
                and _SESSION_LINE.fullmatch(line[len(session):]) is not None
            ):
                return SESSION_EXITED if exited else SESSION_RUNNING
        return None

    def session_exists(self, session: str) -> bool:
        """True only for a running session; EXITED sessions count as absent."""
        return self.session_state(session) == SESSION_RUNNING

    def delete_session(self, session: str) -> None:
        """Delete ``session`` only while it is listed as EXITED; never forced."""
        session = _session(session)
        state = self.session_state(session)
        if state != SESSION_EXITED:
            shown = "running" if state == SESSION_RUNNING else "not listed"
            raise MultiplexerError(
                f"zellij session {session!r} is {shown}, not EXITED; not deleting it"
            )
        result = self._cli("delete-session", session)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise MultiplexerError(
                f"zellij delete-session {session} exited {result.returncode}"
                + (f": {detail[0][:200]}" if detail else "")
            )
        if self.session_state(session) is not None:
            raise MultiplexerError(
                f"zellij session {session!r} is still listed after delete-session"
            )

    def spawn(
        self,
        session: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        name: str | None = None,
        focus: bool = False,
    ) -> PaneRef:
        """Start argv in a new pane and return its ref.

        zellij moves the spawning client to the new pane on every supported
        version and 0.44.2 rejects `--no-focus`, so `focus` is not passed on
        (M1-0 recommendation).
        """
        session = _session(session)
        command = _argv(argv)
        options = []
        if name is not None:
            options += ["--name", _pane_name(name)]
        if cwd is not None:
            options += ["--cwd", _cwd(cwd)]
        result = self._action(session, None, "new-pane", *options, "--", *command)
        match = _NEW_PANE.fullmatch((result.stdout or "").strip())
        if match is None:
            raise MultiplexerError(
                "zellij new-pane did not report a pane id "
                "(unknown or EXITED session?)"
            )
        return PaneRef(NAME, session, match.group(1), self._effective_socket_dir())

    def list_panes(self, session: str) -> list[PaneInfo]:
        session = _session(session)
        result = self._answered(session, None, "list-panes", "--json")
        try:
            entries = json.loads(result.stdout or "")
        except ValueError as error:
            raise MultiplexerError(
                "unrecognized zellij list-panes output (not JSON; unknown or "
                "EXITED session?)"
            ) from error
        if not isinstance(entries, list):
            raise MultiplexerError("unrecognized zellij list-panes output")
        socket_dir = self._effective_socket_dir()
        panes = [_pane_info(entry, session, socket_dir) for entry in entries]
        return [pane for pane in panes if pane is not None]

    def _answered(self, session: str, socket_dir: str | None, *args: str):
        """Run a read-only query, asking again after a transient zellij miss.

        zellij 0.44.2 now and then exits 0 with no output for list-panes or
        list-clients on a running session (its 1 s action timeout ends the
        CLI before the answer) and its socket probe can briefly report no
        active session (#72). Both are asked again a bounded number of
        times; an unknown or EXITED session prints the session list instead,
        which is not retried.
        """
        for _ in range(READBACK_ATTEMPTS - 1):
            try:
                result = self._action(session, socket_dir, *args)
            except _NoActiveSession:
                pass
            else:
                if (result.stdout or "").strip():
                    return result
            time.sleep(READBACK_INTERVAL)
        return self._action(session, socket_dir, *args)

    def _clients(self, session: str, socket_dir: str | None) -> dict[str, str]:
        result = self._answered(session, socket_dir, "list-clients")
        try:
            return _client_panes(result.stdout or "")
        except RuntimeError as error:
            raise MultiplexerError(str(error)) from error

    def focus(self, ref: PaneRef) -> FocusResult:
        """Select ref's pane for the most recent client; never activate."""
        if not isinstance(ref, PaneRef) or ref.multiplexer != NAME:
            raise MultiplexerError("refused pane ref for another multiplexer")
        session = _session(ref.session)
        pane = _pane_id(ref.pane_id)
        socket_dir = _socket_dir(ref.socket_dir)
        expected = f"terminal_{pane}"
        try:
            before = self._clients(session, socket_dir)
            if not before:
                return _failed("no attached zellij client")
            if all(shown == expected for shown in before.values()):
                return _selected(ALREADY_FOCUSED)
            self._action(session, socket_dir, "focus-pane-id", expected)
            for attempt in range(READBACK_ATTEMPTS):
                after = self._clients(session, socket_dir)
                if any(
                    after.get(client) == expected and shown != expected
                    for client, shown in before.items()
                ):
                    return _selected("tab or pane selection")
                if attempt + 1 < READBACK_ATTEMPTS:
                    time.sleep(READBACK_INTERVAL)
        except MultiplexerError as error:
            return _failed(error.detail)
        return _failed(f"no zellij client moved to pane {pane}")

    def paste(self, ref: PaneRef, text: str) -> None:
        """Paste text into the pane; bracketed when the program asked for it."""
        session, pane, socket_dir = _target(ref)
        self._action(
            session, socket_dir, "paste", "--pane-id", pane, "--", _paste_text(text)
        )

    def send_keys(self, ref: PaneRef, keys: list[str]) -> None:
        session, pane, socket_dir = _target(ref)
        self._action(
            session, socket_dir, "send-keys", "--pane-id", pane, "--", *_keys(keys)
        )

    def capture(self, ref: PaneRef, lines: int = 200) -> str:
        """The last ``lines`` non-blank-trailing lines of screen and scrollback."""
        session, pane, socket_dir = _target(ref)
        if not _int(lines) or not 1 <= lines <= MAX_CAPTURE_LINES:
            raise MultiplexerError(f"refused capture line count {lines!r:.20}")
        try:
            directory = paths.ensure_private_dir(
                paths.runtime_dir(create=True) / CAPTURE_DIR
            )
            descriptor, name = tempfile.mkstemp(
                dir=directory, prefix="dump-", suffix=".txt"
            )
            os.close(descriptor)
        except OSError as error:
            raise MultiplexerError(f"cannot create a capture file: {error}") from error
        try:
            self._action(
                session, socket_dir, "dump-screen", "--full", "--pane-id", pane,
                "--path", name,
            )
            _wait_for_dump(name)
            raw = _read_dump(name)
        finally:
            try:
                os.unlink(name)
            except OSError:
                pass
        return _tail(raw.decode("utf-8", errors="replace"), lines)

    def subscribe(
        self,
        ref: PaneRef,
        *,
        max_events: int = SUBSCRIBE_EVENTS,
        timeout: float = SUBSCRIBE_SECONDS,
    ) -> Iterator[dict]:
        """At most ``max_events`` JSON render updates within ``timeout`` seconds.

        The `zellij subscribe` child starts on the first ``next()`` and is
        terminated when the iterator ends, is closed or is collected.
        """
        session, pane, socket_dir = _target(ref)
        if not _int(max_events) or max_events < 1:
            raise MultiplexerError(f"refused subscribe event count {max_events!r:.20}")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 0 < timeout <= 3600
        ):
            raise MultiplexerError(f"refused subscribe timeout {timeout!r:.20}")
        argv = [
            self._zellij(), "--session", session, "subscribe", "--pane-id", pane,
            "--format", "json",
        ]
        env = _cli_env(self._effective_socket_dir(socket_dir))
        return _events(argv, env, max_events, float(timeout))


def _target(ref) -> tuple[str, str, str | None]:
    if not isinstance(ref, PaneRef) or ref.multiplexer != NAME:
        raise MultiplexerError("refused pane ref for another multiplexer")
    return _session(ref.session), _pane_id(ref.pane_id), _socket_dir(ref.socket_dir)


def _paste_text(value) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise MultiplexerError("refused paste text")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise MultiplexerError("refused paste text that is not valid UTF-8") from None
    if size > MAX_PASTE_BYTES:
        raise MultiplexerError(
            f"refused paste of {size} bytes (limit {MAX_PASTE_BYTES})"
        )
    return value


def _keys(value) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not 0 < len(value) <= MAX_KEYS
        or not all(
            isinstance(key, str)
            and len(key) <= MAX_KEY_LENGTH
            and _KEY.match(key)
            for key in value
        )
    ):
        raise MultiplexerError(f"refused zellij keys {value!r:.80}")
    return list(value)


def _dump_size(name: str) -> int:
    try:
        return os.lstat(name).st_size
    except OSError:
        return -1


def _wait_for_dump(name: str) -> None:
    """Wait until the asynchronously written dump stops growing.

    The size must be the same, and not zero, across two polls. A dump that
    stays empty is given up after ``READBACK_ATTEMPTS`` polls (a blank pane),
    one that keeps growing after ``MAX_DUMP_POLLS``.
    """
    size = _dump_size(name)
    for attempt in range(1, MAX_DUMP_POLLS + 1):
        time.sleep(READBACK_INTERVAL)
        later = _dump_size(name)
        if later == size and (later > 0 or attempt >= READBACK_ATTEMPTS - 1):
            return
        size = later


def _read_dump(name: str) -> bytes:
    """The last MAX_CAPTURE_BYTES of a dump file, never following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags)
    except OSError as error:
        raise MultiplexerError(f"cannot read the zellij dump: {error}") from error
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise MultiplexerError("the zellij dump is not a regular file")
        if info.st_size > MAX_CAPTURE_BYTES:
            stream.seek(info.st_size - MAX_CAPTURE_BYTES)
        return stream.read(MAX_CAPTURE_BYTES)


def _tail(text: str, lines: int) -> str:
    rows = text.splitlines()
    while rows and not rows[-1].strip():
        rows.pop()
    return "\n".join(rows[-lines:])


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(STOP_TIMEOUT)
    if process.stdout is not None:
        process.stdout.close()


def _events(
    argv: list[str], env: dict | None, max_events: int, timeout: float
) -> Iterator[dict]:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError as error:
        raise MultiplexerError(f"zellij subscribe failed: {error}") from error
    try:
        descriptor = process.stdout.fileno()
        deadline = time.monotonic() + timeout
        buffer = b""
        count = 0
        while count < max_events:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            ready, _, _ = select.select([descriptor], [], [], remaining)
            if not ready:
                return
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer and count < max_events:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                yield _event(line)
                count += 1
            if len(buffer) > MAX_EVENT_BYTES:
                raise MultiplexerError("zellij subscribe event exceeds the size limit")
    finally:
        _stop(process)


def _event(line: bytes) -> dict:
    if len(line) > MAX_EVENT_BYTES:
        raise MultiplexerError("zellij subscribe event exceeds the size limit")
    try:
        event = json.loads(line)
    except ValueError as error:
        raise MultiplexerError("unrecognized zellij subscribe output") from error
    if not isinstance(event, dict):
        raise MultiplexerError("unrecognized zellij subscribe output")
    return event
