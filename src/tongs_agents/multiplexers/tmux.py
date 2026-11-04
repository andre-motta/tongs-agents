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

A ``single`` workspace session (#124, session option ``@tongs_agents_layout``)
shows the sidebar and one agent in its first window, the main slot. Every
other pane is parked in its own detached window (``new-window -d``), and
showing one swaps it into the main slot with ``swap-pane`` (``join-pane``
beside the sidebar when the slot is empty), then selects it; the parked
window is renamed after the pane it now holds. Pane ids move with their
process, so control, capture and readback keep working on parked panes, and
``focus`` of a parked pane shows it rather than selecting its window.
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
    LAYOUT_SINGLE,
    LAYOUTS,
    SESSION_RUNNING,
    SIDEBAR_PANE_NAME,
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
LAYOUT_OPTION = "@tongs_agents_layout"
SIDEBAR_COLUMNS = 40
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
SLOT_FORMAT = "\t".join((
    "#{pane_id}", "#{window_id}", "#{" + NAME_OPTION + "}", "#{pane_active}",
    "#{window_width}",
))


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


def _literal(value: str) -> str:
    """``value`` for an argument tmux expands as a format: ``#`` doubled."""
    return value.replace("#", "##")


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


def _main_columns(window_width: int) -> int:
    """The main pane's width beside a sidebar of about SIDEBAR_COLUMNS columns."""
    if window_width >= 2 * SIDEBAR_COLUMNS + 1:
        return window_width - SIDEBAR_COLUMNS - 1
    return max(window_width // 2, 1)


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

    def detach(self, session: str) -> None:
        """Detach every client attached to the session; its panes keep running."""
        self._tmux(None, "detach-client", "-s", f"={_session(session)}")

    def kill_session(self, session: str) -> None:
        """End the session and every pane in it."""
        self._tmux(None, "kill-session", "-t", f"={_session(session)}")

    def delete_session(self, session: str) -> None:
        _session(session)
        raise MultiplexerError(
            "tmux keeps no EXITED sessions; nothing to delete (a running session "
            "is never deleted)"
        )

    def set_layout(self, session: str, layout: str) -> None:
        """Record ``layout`` on the running session; ``spawn`` and ``show`` follow it."""
        session = _session(session)
        if layout not in LAYOUTS:
            raise MultiplexerError(f"refused workspace layout {layout!r:.40}")
        self._tmux(None, "set-option", "-t", f"={session}:", LAYOUT_OPTION, layout)

    def layout(self, session: str) -> str | None:
        """The layout recorded on the session, or None (a split session)."""
        return self._layout(None, _session(session))

    def _layout(self, socket: str | None, session: str) -> str | None:
        result = _run_tmux(self._command(
            socket, "show-options", "-q", "-v", "-t", f"={session}:", LAYOUT_OPTION))
        if result is None:
            return None
        value = (result.stdout or "").strip()
        return value if value in LAYOUTS else None

    def align_agents(self, session: str, pane_ids) -> list[str]:
        """Nothing to align: tmux tiles every pane itself."""
        return []

    def session_of(self, pane: str) -> str:
        """The name of the session that holds ``pane`` on this server."""
        output = self._tmux(None, "display-message", "-p", "-t", _pane_id(pane),
                            "#{session_name}")
        return _session(output.rstrip("\n"))

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

        A new session starts detached with argv as its first pane. In a
        ``single`` session any pane but the sidebar is parked in a new
        detached window (``show`` brings it into view, at once with
        ``focus``). Otherwise the active
        pane of the session's current window is split side by side and the
        row is spread evenly, or a new window is opened when it has no room.
        ``focus`` False keeps the current pane active.
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
        parked = False
        result = None
        if self.session_state(session) != SESSION_RUNNING:
            _session(session, create=True)
            result = _run_tmux(self._command(
                socket, "new-session", "-d", "-s", session, *options, "--", *command))
            if result is None and self.session_state(session) != SESSION_RUNNING:
                raise MultiplexerError(f"tmux new-session {session} failed")
        if result is None and pane_name != SIDEBAR_PANE_NAME and (
            self._layout(socket, session) == LAYOUT_SINGLE
        ):
            named = [] if pane_name is None else ["-n", _literal(pane_name)]
            result = _run_tmux(self._command(
                socket, "new-window", "-d", "-t", f"={session}:", *named, *options, "--",
                *command))
            if result is None:
                raise MultiplexerError(
                    f"tmux could not park a new window in session {session!r}"
                )
            parked = True
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
        ref = PaneRef(NAME, session, pane, socket)
        if parked and focus:
            self.show(ref)
        return ref

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

    def focused_panes(self, session: str) -> set[str]:
        """Pane ids that any client of the tmux server shows (``list-clients``).

        tmux marks the active pane of every window active whether a client is
        attached or not, so only each attached client's current pane counts.
        Clients of every session count, not only ``session``'s: a grouped
        session (``new-session -t``) or a linked window shows the same panes,
        and pane ids are unique per server. Control-mode clients count too.
        """
        _session(session)
        output = self._tmux(None, "list-clients", "-F", "#{pane_id}")
        panes = {line for line in output.splitlines() if line}
        if not all(_PANE_ID.match(pane) for pane in panes):
            raise MultiplexerError("unrecognized tmux list-clients output")
        return panes

    def focus(self, ref: PaneRef) -> FocusResult:
        """Select ref's pane in its session and read it back; never raise.

        In a ``single`` session a parked pane is shown (swapped into the main
        slot) rather than selected in its own window.
        """
        return self._bring(ref, park_aware=True)

    def show(self, ref: PaneRef) -> FocusResult:
        """Swap ref's pane into the main slot of a ``single`` session and select it.

        A ``split`` session is left as it is: every pane there is already laid
        out, and spawning keeps the current pane active.
        """
        return self._bring(ref, park_aware=False)

    def _bring(self, ref: PaneRef, *, park_aware: bool) -> FocusResult:
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
            if self._layout(socket, session) == LAYOUT_SINGLE:
                slot = self._slot(socket, session)
                if slot is not None and located[1] != slot[1]:
                    return self._swap_in(socket, session, pane, located[1], slot)
            elif not park_aware:
                return _selected("split layout: every pane is already laid out")
            if self._active(socket, session) == pane:
                return _selected(ALREADY_SELECTED)
            self._tmux(socket, "select-window", "-t", f"={session}:{located[1]}")
            self._tmux(socket, "select-pane", "-t", pane)
            if self._active(socket, session) == pane:
                return _selected("window and pane selection")
        except MultiplexerError as error:
            return _failed(error.detail)
        return _failed(f"tmux did not make pane {pane} the active pane")

    def _slot(self, socket: str | None, session: str):
        """``(sidebar pane, its window, window width, main pane or None)``.

        None when the session has no live sidebar. The main pane is the
        window's active pane unless that is the sidebar, else its first
        other pane.
        """
        output = self._tmux(socket, "list-panes", "-s", "-t", f"={session}", "-F",
                            SLOT_FORMAT)
        rows = []
        for line in output.splitlines():
            parts = line.split("\t")
            if (
                len(parts) != 5
                or _PANE_ID.match(parts[0]) is None
                or _WINDOW_ID.match(parts[1]) is None
                or parts[3] not in {"0", "1"}
                or not parts[4].isdigit()
            ):
                raise MultiplexerError("unrecognized tmux list-panes output")
            rows.append(parts)
        sidebar = next((row for row in rows if row[2] == SIDEBAR_PANE_NAME), None)
        if sidebar is None:
            return None
        others = [row for row in rows if row[1] == sidebar[1] and row[0] != sidebar[0]]
        main = next((row for row in others if row[3] == "1"), others[0] if others else None)
        return sidebar[0], sidebar[1], int(sidebar[4]), main[0] if main else None

    def _swap_in(self, socket, session: str, pane: str, parked_window: str, slot):
        sidebar, window, width, main = slot
        if main is not None:
            self._tmux(socket, "swap-pane", "-d", "-s", pane, "-t", main)
            name = _run_tmux(self._command(
                socket, "display-message", "-p", "-t", main, "#{" + NAME_OPTION + "}"))
            shown = (name.stdout or "").strip() if name is not None else ""
            if _text(shown, MAX_NAME_BYTES):
                # Best effort: the status bar names the pane the window now holds.
                _run_tmux(self._command(socket, "rename-window", "-t", parked_window,
                                        _literal(shown)))
            detail = "swapped into the main slot"
        else:
            self._tmux(socket, "join-pane", "-h", "-d", "-l", str(_main_columns(width)),
                       "-s", pane, "-t", sidebar)
            detail = "joined into the empty main slot"
        self._tmux(socket, "select-window", "-t", f"={session}:{window}")
        self._tmux(socket, "select-pane", "-t", pane)
        moved = self._tmux(socket, "display-message", "-p", "-t", pane,
                           "#{window_id}").strip()
        if moved == window and self._active(socket, session) == pane:
            return _selected(detail)
        return _failed(f"tmux did not move pane {pane} into the main slot")

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

    def rename_pane(self, ref: PaneRef, name: str) -> None:
        """Name the pane: its ``@tongs_agents_name``, its title and its parked window.

        The pane option is what ``list_panes`` reports and what a swap names
        the parked window after. The title (``select-pane -T``) and, in a
        ``single`` session, the window the pane is parked in alone follow it
        best effort; tmux expands formats in both, so ``#`` is doubled.
        """
        session, pane, socket = _target(ref)
        name = _pane_name(name)
        self._tmux(socket, "set-option", "-p", "-t", pane, NAME_OPTION, name)
        _run_tmux(self._command(socket, "select-pane", "-t", pane, "-T", _literal(name)))
        if self._layout(socket, session) != LAYOUT_SINGLE:
            return
        located = _run_tmux(self._command(
            socket, "display-message", "-p", "-t", pane,
            "#{window_id}\t#{window_panes}"))
        window, _, count = ((located.stdout or "").strip() if located else "").partition("\t")
        if _WINDOW_ID.match(window) is None or count != "1":
            return
        try:
            slot = self._slot(socket, session)
        except MultiplexerError:
            return
        if slot is None or slot[1] != window:
            _run_tmux(self._command(socket, "rename-window", "-t", window, _literal(name)))
