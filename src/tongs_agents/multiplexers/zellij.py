"""Zellij pane lifecycle: spawn, list and select panes in a session.

Shapes follow the M1-0 spike (docs/work/workspace-spike.md) on zellij 0.44.2
and 0.45.1. Every call is an argv list with stdin closed, a 5 s timeout and the
session's socket dir in its environment. `action` exits 0 for an unknown or
EXITED session, so output is parsed strictly and anything unexpected raises.
Focus moves only the client that most recently sent input and is confirmed by
`list-clients` readback; no emulator window is ever activated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Iterator

from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.multiplexers.base import (
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
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
UNSUPPORTED_IO = "zellij pane I/O is not implemented yet (M3)"
_NEW_PANE = re.compile(r"terminal_([0-9]{1,12})")
_SESSION_LINE = re.compile(r" \[Created [^\]\n]*\](?: \([^()\n]*\))*")
_PANE_KEYS = {
    "id": int,
    "is_plugin": bool,
    "title": str,
    "exited": bool,
    "is_focused": bool,
}


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
    return PaneInfo(
        ref=PaneRef(NAME, session, str(entry["id"]), socket_dir),
        title=entry["title"],
        command=command,
        exited=entry["exited"],
        focused=entry["is_focused"],
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
            raise MultiplexerError(str(error)) from error
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

    def session_exists(self, session: str) -> bool:
        """True only for a running session; EXITED sessions count as absent."""
        session = _session(session)
        argv = [self._zellij(), "list-sessions", "--no-formatting"]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=_cli_env(self._effective_socket_dir()),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MultiplexerError(f"zellij list-sessions failed: {error}") from error
        if result.returncode != 0:
            if NO_SESSIONS in (result.stderr or "").lower():
                return False
            raise MultiplexerError(
                f"zellij list-sessions exited {result.returncode}"
            )
        lines = _running_sessions(result.stdout or "")
        prefix = session + " "
        return any(
            line.startswith(prefix)
            and not exited
            and _SESSION_LINE.fullmatch(line[len(session):]) is not None
            for line, exited in lines.items()
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
        result = self._action(session, None, "list-panes", "--json")
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

    def _clients(self, session: str, socket_dir: str | None) -> dict[str, str]:
        result = self._action(session, socket_dir, "list-clients")
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
        raise Unsupported(UNSUPPORTED_IO)

    def send_keys(self, ref: PaneRef, keys: list[str]) -> None:
        raise Unsupported(UNSUPPORTED_IO)

    def capture(self, ref: PaneRef, lines: int = 200) -> str:
        raise Unsupported(UNSUPPORTED_IO)

    def subscribe(self, ref: PaneRef) -> Iterator[dict]:
        raise Unsupported(UNSUPPORTED_IO)
