"""tmux pane lifecycle: spawn, list, select, paste into and capture panes.

The second Multiplexer backend (#24). Every call is an argv list with stdin
closed (``terminal_apis.tmux._run_tmux``, 5 s timeout) that names the server
socket with ``-S``: the ref's or adapter's socket, else the socket in
``TMUX``, else tmux's default socket for this user (``$TMUX_TMPDIR`` or
``/tmp``, then ``tmux-<uid>/default``, refused unless that directory is
private to this user, as tmux itself checks). The one call that needs input,
``load-buffer -``, gets the paste text on stdin instead.

Sessions are always named exactly (``=name``), so a prefix never matches
another session. Pane ids (``%N``) are unique per server. Panes are named
with the pane user option ``@tongs_agents_name``, which programs cannot
overwrite the way they overwrite pane titles. Focus selects the window and
pane for the session and reads the active pane back; no client or emulator
window is ever raised. tmux keeps no EXITED sessions, so there is nothing to
delete or resurrect, and it has no render stream, so ``subscribe`` is
unsupported. The adapter holds no policy: ``tongs_agents.control`` decides
which panes may be driven.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import subprocess
from collections.abc import Iterator

from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.multiplexers.base import (
    SESSION_RUNNING,
    MultiplexerError,
    PaneInfo,
    PaneRef,
    Unsupported,
)
from tongs_agents.terminal_apis.tmux import _run_tmux, _tmux_context

NAME = "tmux"
TMUX = "tmux"
TMUX_FLOOR = (3, 2)
NAME_OPTION = "@tongs_agents_name"
BUFFER_PREFIX = "tongs-agents-"
COMMAND_TIMEOUT = 5
DEFAULT_TMPDIR = "/tmp"
DEFAULT_LABEL = "default"
ACTIVATION_SKIPPED = "skipped: tmux selects panes only"
ALREADY_SELECTED = "already the active pane"
MAX_NAME_BYTES = 256
MAX_SESSION_BYTES = 256
MAX_ARG_BYTES = 128 * 1024
MAX_PASTE_BYTES = 64 * 1024
MAX_CAPTURE_LINES = 10_000
MAX_SOCKET_BYTES = 4096
KEYS = {
    "Enter": "Enter",
    "Escape": "Escape",
    "Tab": "Tab",
    "Space": "Space",
    "Backspace": "BSpace",
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",
}
MAX_KEYS = 16
_VERSION = re.compile(r"tmux (?:next-)?([0-9]{1,4})\.([0-9]{1,4})([a-z]?)\s*\Z")
_PANE_ID = re.compile(r"%[0-9]{1,12}\Z")
_WINDOW_ID = re.compile(r"@[0-9]{1,12}\Z")
_SESSION_ID = re.compile(r"\$[0-9]{1,12}\Z")
_PANE_FIELDS = (
    "#{session_id}",
    "#{window_id}",
    "#{pane_id}",
    "#{pane_dead}",
    "#{pane_dead_status}",
    "#{pane_active}",
    "#{window_active}",
    "#{" + NAME_OPTION + "}",
    "#{pane_current_command}",
    "#{pane_title}",
)
PANE_FORMAT = "\t".join(_PANE_FIELDS)


def _has_control(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _text(value, limit: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not _has_control(value)
        and len(value.encode("utf-8", "surrogatepass")) <= limit
    )


def _session(value, *, create: bool = False) -> str:
    """A tmux session name; ``:`` never, ``.`` not for a new session.

    tmux up to 3.3 replaces ``.`` and ``:`` in a new session's name with
    ``_``, so the exact name would never be found again.
    """
    if (
        not _text(value, MAX_SESSION_BYTES)
        or value.startswith(("-", "="))
        or ":" in value
        or (create and "." in value)
    ):
        raise MultiplexerError(f"refused tmux session name {value!r:.80}")
    return value


def _pane_id(value) -> str:
    if not isinstance(value, str) or _PANE_ID.match(value) is None:
        raise MultiplexerError(f"refused tmux pane id {value!r:.40}")
    return value


def _socket(value) -> str | None:
    if value is None:
        return None
    if not _text(value, MAX_SOCKET_BYTES) or not os.path.isabs(value):
        raise MultiplexerError(f"refused tmux socket {value!r:.80}")
    return value


def _pane_name(value) -> str:
    if not _text(value, MAX_NAME_BYTES) or value.startswith("-"):
        raise MultiplexerError(f"refused tmux pane name {value!r:.80}")
    return value


def _cwd(value) -> str:
    if not _text(value, MAX_SOCKET_BYTES) or not os.path.isabs(value) or not os.path.isdir(value):
        raise MultiplexerError(f"refused tmux pane cwd {value!r:.80}")
    return value


def _argv(value) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(isinstance(arg, str) and "\0" not in arg for arg in value)
        or not value[0]
        or value[0].startswith("-")
        or sum(len(arg.encode("utf-8", "surrogatepass")) for arg in value) > MAX_ARG_BYTES
    ):
        raise MultiplexerError("refused tmux pane argv")
    command = list(value)
    if len(command) == 1:
        # tmux hands a single argument to the shell; env runs it as a program.
        if "=" in command[0]:
            raise MultiplexerError("refused tmux pane argv")
        command = ["env", "--", command[0]]
    return command


def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_version(text) -> tuple[int, int] | None:
    """``(major, minor)`` from ``tmux -V`` output such as ``tmux 3.4`` or ``tmux 3.3a``."""
    if not isinstance(text, str):
        return None
    match = _VERSION.match(text.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def format_version(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def default_socket(env=None) -> str | None:
    """tmux's default socket for this user: ``<TMUX_TMPDIR or /tmp>/tmux-<uid>/default``.

    Only the path; ``checked_default_socket`` applies tmux's own ownership check.
    """
    env = os.environ if env is None else env
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        return None
    base = env.get("TMUX_TMPDIR") or DEFAULT_TMPDIR
    if not os.path.isabs(base):
        return None
    return os.path.join(os.path.realpath(base), f"tmux-{getuid()}", DEFAULT_LABEL)


def checked_default_socket(env=None) -> str | None:
    """``default_socket`` after the check tmux runs before it trusts that path.

    ``tmux -S`` skips tmux's own check, so it is repeated here: create
    ``tmux-<uid>`` as 0700 when missing, then refuse it unless it is a real
    directory (not a symlink) owned by this user with no group or other access.
    """
    socket = default_socket(env)
    if socket is None:
        return None
    directory = os.path.dirname(socket)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise MultiplexerError(
            f"cannot create the tmux socket directory: {error.strerror or error}"
        ) from None
    try:
        info = os.lstat(directory)
    except OSError as error:
        raise MultiplexerError(
            f"cannot read the tmux socket directory: {error.strerror or error}"
        ) from None
    if not stat.S_ISDIR(info.st_mode):
        raise MultiplexerError("refused the tmux socket directory: not a directory")
    if info.st_uid != os.getuid():
        raise MultiplexerError("refused the tmux socket directory: owned by another user")
    if info.st_mode & 0o077:
        raise MultiplexerError(
            "refused the tmux socket directory: open to other users "
            f"(mode {stat.S_IMODE(info.st_mode):o})"
        )
    return socket


def env_socket(env=None) -> str | None:
    """The server socket named by ``TMUX`` (``<socket>,<pid>,<session>``)."""
    env = os.environ if env is None else env
    context = _tmux_context({"TMUX": env.get("TMUX", "")})
    if context is None:
        return None
    socket = context[0][2]
    return socket if _text(socket, MAX_SOCKET_BYTES) and os.path.isabs(socket) else None


def _failed(detail: str) -> FocusResult:
    return FocusResult(
        state="failed",
        reason="tmux pane selection failed",
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


def _target(ref) -> tuple[str, str, str | None]:
    if not isinstance(ref, PaneRef) or ref.multiplexer != NAME:
        raise MultiplexerError("refused pane ref for another multiplexer")
    return _session(ref.session), _pane_id(ref.pane_id), _socket(ref.socket_dir)


def _paste_bytes(value) -> bytes:
    if not isinstance(value, str) or "\0" in value:
        raise MultiplexerError("refused paste text")
    try:
        data = value.encode("utf-8")
    except UnicodeEncodeError:
        raise MultiplexerError("refused paste text that is not valid UTF-8") from None
    if len(data) > MAX_PASTE_BYTES:
        raise MultiplexerError(f"refused paste of {len(data)} bytes (limit {MAX_PASTE_BYTES})")
    return data


def _keys(value) -> list[str]:
    if (
        not isinstance(value, (list, tuple))
        or not 0 < len(value) <= MAX_KEYS
        or not all(isinstance(key, str) and key in KEYS for key in value)
    ):
        raise MultiplexerError(f"refused tmux keys {value!r:.80}")
    return [KEYS[key] for key in value]


def _tail(text: str, lines: int) -> str:
    rows = [row.rstrip() for row in text.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    return "\n".join(rows[-lines:])


def _pane_info(line: str, session: str, socket: str) -> PaneInfo:
    parts = line.split("\t", len(_PANE_FIELDS) - 1)
    if len(parts) != len(_PANE_FIELDS):
        raise MultiplexerError("unrecognized tmux list-panes output")
    (_, window_id, pane_id, dead, dead_status, pane_active, window_active,
     name, command, title) = parts
    if (
        _WINDOW_ID.match(window_id) is None
        or _PANE_ID.match(pane_id) is None
        or dead not in {"0", "1"}
        or pane_active not in {"0", "1"}
        or window_active not in {"0", "1"}
        or (dead_status and not dead_status.isdigit())
    ):
        raise MultiplexerError("unrecognized tmux list-panes output")
    exited = dead == "1"
    return PaneInfo(
        ref=PaneRef(NAME, session, pane_id, socket),
        title=name or title,
        command=command or None,
        exited=exited,
        focused=pane_active == "1" and window_active == "1",
        exit_status=int(dead_status) if exited and dead_status else None,
    )


class TmuxMultiplexer:
    name = NAME

    def __init__(self, socket_dir: str | None = None):
        """``socket_dir`` is the server socket path (the first field of ``TMUX``)."""
        self._socket = _socket(socket_dir)
        self.version: tuple[int, int] | None = None

    @property
    def socket(self) -> str:
        socket = self._socket or env_socket() or checked_default_socket()
        if socket is None:
            raise MultiplexerError("cannot tell which tmux server socket to use")
        return socket

    def _command(self, socket: str | None, *args: str) -> list[str]:
        return [TMUX, "-S", _socket(socket) or self.socket, *args]

    def _tmux(self, socket: str | None, *args: str) -> str:
        result = _run_tmux(self._command(socket, *args))
        if result is None:
            raise MultiplexerError(
                f"tmux {args[0]} failed: tmux missing, timed out, no server "
                "or unknown target"
            )
        return result.stdout or ""

    def attach_argv(self, session: str) -> list[str]:
        """The command that attaches a terminal to ``session`` on this server."""
        return self._command(None, "attach-session", "-t", f"={_session(session)}")

    def available(self) -> tuple[bool, str]:
        """Read ``tmux -V`` now, keep it in ``self.version`` and check the floor."""
        if not shutil.which(TMUX):
            return False, "tmux not found on PATH"
        result = _run_tmux([TMUX, "-V"])
        version = parse_version(result.stdout if result is not None else None)
        self.version = version
        if version is None:
            return False, "could not read the tmux version"
        if version < TMUX_FLOOR:
            return False, (
                f"tmux {format_version(version)} is older than the supported floor "
                f"{format_version(TMUX_FLOOR)}"
            )
        return True, f"tmux {(result.stdout or '').strip().removeprefix('tmux ')}"

    def in_session(self) -> PaneRef | None:
        socket = env_socket()
        pane = os.environ.get("TMUX_PANE")
        if socket is None or not isinstance(pane, str) or _PANE_ID.match(pane) is None:
            return None
        result = _run_tmux([TMUX, "-S", socket, "display-message", "-p", "-t", pane,
                            "#{session_name}"])
        if result is None:
            return None
        try:
            session = _session((result.stdout or "").rstrip("\n"))
        except MultiplexerError:
            return None
        return PaneRef(NAME, session, pane, socket)

    def session_state(self, session: str) -> str | None:
        """SESSION_RUNNING when the exact session exists; tmux has no EXITED state."""
        session = _session(session)
        result = _run_tmux(self._command(None, "has-session", "-t", f"={session}"))
        return SESSION_RUNNING if result is not None else None

    def session_exists(self, session: str) -> bool:
        return self.session_state(session) == SESSION_RUNNING

    def delete_session(self, session: str) -> None:
        _session(session)
        raise MultiplexerError(
            "tmux keeps no EXITED sessions; nothing to delete (a running session "
            "is never deleted)"
        )

    def _session_id(self, socket: str | None, session: str) -> str:
        output = self._tmux(socket, "display-message", "-p", "-t", f"={session}:",
                            "#{session_id}").strip()
        if _SESSION_ID.match(output) is None:
            raise MultiplexerError(f"tmux session {session!r} not found")
        return output

    def spawn(
        self,
        session: str,
        argv: list[str],
        *,
        cwd: str | None = None,
        name: str | None = None,
        focus: bool = False,
    ) -> PaneRef:
        """Start argv in a new pane of ``session``; create the session if absent.

        A new session starts detached with argv as its first pane. Otherwise
        the active pane of the session's current window is split side by
        side and the row is spread evenly, or a new window is opened when it
        has no room. ``focus`` False keeps the
        current pane active.
        """
        session = _session(session)
        command = _argv(argv)
        pane_name = None if name is None else _pane_name(name)
        options = ["-P", "-F", "#{pane_id}"]
        if cwd is not None:
            options += ["-c", _cwd(cwd)]
        socket = self.socket
        detached = [] if focus else ["-d"]
        split = False
        result = None
        if self.session_state(session) != SESSION_RUNNING:
            _session(session, create=True)
            result = _run_tmux(self._command(
                socket, "new-session", "-d", "-s", session, *options, "--", *command))
            if result is None and self.session_state(session) != SESSION_RUNNING:
                raise MultiplexerError(f"tmux new-session {session} failed")
        if result is None:
            target = f"={session}:"
            result = _run_tmux(self._command(
                socket, "split-window", "-h", *detached, "-t", target, *options, "--", *command))
            split = result is not None
            if result is None:
                result = _run_tmux(self._command(
                    socket, "new-window", *detached, "-t", target, *options, "--",
                    *command))
            if result is None:
                raise MultiplexerError(
                    f"tmux could not add a pane to session {session!r} "
                    "(split-window and new-window failed)"
                )
        pane = (result.stdout or "").strip()
        if _PANE_ID.match(pane) is None:
            raise MultiplexerError("tmux did not report the new pane id")
        if pane_name is not None:
            self._tmux(socket, "set-option", "-p", "-t", pane, NAME_OPTION, pane_name)
        if split:
            # Spread the new pane and its neighbours evenly; a failure leaves sizes as they are.
            _run_tmux(self._command(socket, "select-layout", "-t", pane, "-E"))
        return PaneRef(NAME, session, pane, socket)

    def list_panes(self, session: str) -> list[PaneInfo]:
        session = _session(session)
        socket = self.socket
        session_id = self._session_id(socket, session)
        output = self._tmux(socket, "list-panes", "-a", "-F", PANE_FORMAT)
        prefix = session_id + "\t"
        return [
            _pane_info(line, session, socket)
            for line in output.splitlines()
            if line.startswith(prefix)
        ]

    def focus(self, ref: PaneRef) -> FocusResult:
        """Select ref's window and pane in its session and read it back; never raise."""
        try:
            session, pane, socket = _target(ref)
            session_id = self._session_id(socket, session)
            located = self._tmux(socket, "display-message", "-p", "-t", pane,
                                 "#{session_id}\t#{window_id}").strip().split("\t")
            if (
                len(located) != 2
                or located[0] != session_id
                or _WINDOW_ID.match(located[1]) is None
            ):
                return _failed(f"pane {pane} is not in session {session!r}")
            if self._active(socket, session) == pane:
                return _selected(ALREADY_SELECTED)
            self._tmux(socket, "select-window", "-t", f"={session}:{located[1]}")
            self._tmux(socket, "select-pane", "-t", pane)
            if self._active(socket, session) == pane:
                return _selected("window and pane selection")
        except MultiplexerError as error:
            return _failed(error.detail)
        return _failed(f"tmux did not make pane {pane} the active pane")

    def _active(self, socket: str | None, session: str) -> str:
        return self._tmux(socket, "display-message", "-p", "-t", f"={session}:",
                          "#{pane_id}").strip()

    def paste(self, ref: PaneRef, text: str) -> None:
        """Load text into a uniquely named buffer and paste it, bracketed when asked.

        ``-r`` keeps line feeds as they are, ``-p`` brackets the paste when
        the pane's program turned bracketed paste on, and ``-d`` deletes the
        buffer afterwards.
        """
        _, pane, socket = _target(ref)
        data = _paste_bytes(text)
        buffer = BUFFER_PREFIX + secrets.token_hex(8)
        load = self._command(socket, "load-buffer", "-b", buffer, "-")
        try:
            result = subprocess.run(load, input=data, capture_output=True,
                                    timeout=COMMAND_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MultiplexerError(f"tmux load-buffer failed: {error}") from None
        if result.returncode != 0:
            raise MultiplexerError(f"tmux load-buffer exited {result.returncode}")
        try:
            self._tmux(socket, "paste-buffer", "-p", "-r", "-d", "-b", buffer, "-t", pane)
        except MultiplexerError:
            _run_tmux(self._command(socket, "delete-buffer", "-b", buffer))
            raise

    def send_keys(self, ref: PaneRef, keys: list[str]) -> None:
        _, pane, socket = _target(ref)
        self._tmux(socket, "send-keys", "-t", pane, *_keys(keys))

    def capture(self, ref: PaneRef, lines: int = 200) -> str:
        """The last ``lines`` lines of the pane's history and screen, wraps joined."""
        _, pane, socket = _target(ref)
        if not _int(lines) or not 1 <= lines <= MAX_CAPTURE_LINES:
            raise MultiplexerError(f"refused capture line count {lines!r:.20}")
        output = self._tmux(socket, "capture-pane", "-p", "-J", "-t", pane,
                            "-S", f"-{lines}")
        return _tail(output, lines)

    def subscribe(
        self, ref: PaneRef, *, max_events: int = 1000, timeout: float = 60.0
    ) -> Iterator[dict]:
        raise Unsupported("tmux has no pane render subscription")
