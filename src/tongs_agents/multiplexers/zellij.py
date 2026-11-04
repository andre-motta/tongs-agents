"""Zellij pane lifecycle: spawn, list and select panes in a session.

Shapes follow the M1-0 spike (docs/work/workspace-spike.md) on zellij 0.44.2
and 0.45.1. Every call is an argv list with stdin closed, a 5 s timeout and the
session's socket dir in its environment. `action` exits 0 for an unknown or
EXITED session, so output is parsed strictly and anything unexpected raises.
Focus moves only the client that most recently sent input and is confirmed by
`list-clients` readback; no emulator window is ever activated.

Pane I/O always names the pane with `--pane-id`: `paste` (bracketed when the
pane's program turned bracketed paste on, at most 64 KiB; 0.44.2 and 0.45.1
take the text only as an argument, never from stdin or a file, so it shows in
the process list while the call runs), `send-keys`,
`dump-screen --full` into a 0600 temp file under runtime_dir() that is read
and unlinked, and a bounded `subscribe --format json` stream. `action` exits 0
for an unknown pane id, so callers verify the pane first (control.py); the
adapter holds no policy.

A ``single`` workspace session (#124) has a tiled ``tongs-agents-home``
pane beside the sidebar. Every pane but the sidebar opens over it as a pinned
floating pane: ``new-pane --tab-id N --floating --pinned true`` with ``-x``,
``-y``, ``--width`` and ``--height`` set to the agent region, which is the
home pane's geometry from ``list-panes --json`` (the coordinates
``new-pane`` takes, tab bar included). Agents at the same geometry overlap;
``focus-pane-id`` raises one to the top, and a pinned pane stays shown while
the sidebar has the focus (a plain floating layer hides then). ``new-pane
--stacked`` is never used: it stacks onto whichever pane is focused. Nor is a
tiled ``new-pane``: it splits the tab's focused pane and, when that pane is
too small to split (a short sidebar, a held one-row pane), silently vanishes
with its process still running, while a floating pane always has room.
Placement is read back (the new pane is floating in the home pane's tab) and
a pane that is not is closed again. ``list-panes`` reports no pinned flag
(``dump-layout`` does).
``align_agents`` moves given floating panes to the current agent region with
``change-floating-pane-coordinates --pinned true`` (the sidebar calls it when
its own pane is resized). A session created by 0.10.1, whose home pane heads a
``stacked=true`` column, uses that whole column as the region; the agents
stacked there stay tiled under the floating ones.

The home pane can be closed. When it is gone but the sidebar pane runs with
``--layout single`` (the workspace layout marks it), the region is what lies
right of the sidebar in its tab (same rows, up to ``viewport_columns`` of
``list-tabs``). When the sidebar fills the tab (closing the home pane gives it
the space), the region is the sidebar's right three quarters and, once a
pane opened there, ``take_notices`` returns a notice saying so. A session whose sidebar carries
no marker (split, or created before it existed) keeps the plain placement
silently.
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
    HOME_PANE_NAME,
    LAYOUT_OPTION,
    LAYOUT_SINGLE,
    SESSION_EXITED,
    SESSION_RUNNING,
    SIDEBAR_PANE_NAME,
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
MIN_AGENT_COLUMNS = 20
SIDEBAR_COVERED = (
    "the tongs-agents-home pane is gone and the sidebar fills its tab, so agent panes "
    "cover the sidebar's right three quarters; start a new workspace session to get "
    "the agent area beside the sidebar back"
)
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ]*\Z")
_NEW_PANE = re.compile(r"terminal_([0-9]{1,12})")
_ALREADY_FOCUSED = re.compile(r"Pane Terminal\(([0-9]{1,12})\) is already focused")
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


def _geometry(entry: dict) -> tuple[int, int, int]:
    """``(tab_id, pane_x, pane_columns)``: the column a tiled pane sits in."""
    values = tuple(entry.get(key) for key in ("tab_id", "pane_x", "pane_columns"))
    if not all(_int(value) for value in values) or not isinstance(
        entry.get("is_floating"), bool
    ):
        raise MultiplexerError("unrecognized zellij list-panes entry: no pane geometry")
    return values


def _box(entry: dict) -> tuple[int, int, int, int]:
    """``(pane_x, pane_y, pane_columns, pane_rows)`` of a pane, frame included."""
    values = tuple(entry.get(key) for key in ("pane_x", "pane_y", "pane_columns", "pane_rows"))
    if not all(_int(value) and value >= 0 for value in values):
        raise MultiplexerError("unrecognized zellij list-panes entry: no pane geometry")
    return values


def _home_anchor(entries: list[dict]) -> dict | None:
    """The tiled ``tongs-agents-home`` pane with the lowest id, if any."""
    anchors = [
        entry for entry in entries
        if not entry["is_plugin"]
        and entry["title"] == HOME_PANE_NAME
        and entry.get("is_floating") is False
    ]
    return min(anchors, key=lambda entry: entry["id"]) if anchors else None


def _runs_single(entry: dict) -> bool:
    """True for a sidebar pane (exited ones too) started with ``--layout single``."""
    if entry["is_plugin"] or entry["title"] != SIDEBAR_PANE_NAME:
        return False
    words = (entry.get("terminal_command") or entry.get("pane_command") or "").split()
    return any(
        words[index:index + 2] == [LAYOUT_OPTION, LAYOUT_SINGLE]
        for index in range(len(words) - 1)
    )


def _marks_single(entry: dict) -> bool:
    """True for a tiled sidebar pane started with ``--layout single``."""
    return entry.get("is_floating") is False and _runs_single(entry)


def _home_region(entries: list[dict], home: dict) -> tuple[int, int, int, int]:
    """The home pane's box, or its whole column when it shares one (a 0.10.1 stack)."""
    column = _geometry(home)
    boxes = [
        _box(entry) for entry in entries
        if not entry["is_plugin"]
        and entry.get("is_floating") is False
        and _geometry(entry) == column
    ]
    top = min(box[1] for box in boxes)
    bottom = max(box[1] + box[3] for box in boxes)
    x, _, columns, _ = _box(home)
    return x, top, columns, bottom - top


def _beside_sidebar(
    sidebar: dict, viewport_columns: int
) -> tuple[tuple[int, int, int, int], bool]:
    """The region right of the sidebar, or its right three quarters when none is left.

    The flag is True for the fallback.
    """
    x, y, columns, rows = _box(sidebar)
    right = x + columns
    if viewport_columns - right >= MIN_AGENT_COLUMNS:
        return (right, y, viewport_columns - right, rows), False
    left = x + columns // 4
    return (left, y, right - left, rows), True


def _viewport_columns(output: str, tab_id: int) -> int:
    try:
        tabs = json.loads(output or "")
    except ValueError as error:
        raise MultiplexerError("unrecognized zellij list-tabs output (not JSON)") from error
    if not isinstance(tabs, list):
        raise MultiplexerError("unrecognized zellij list-tabs output")
    for tab in tabs:
        if isinstance(tab, dict) and tab.get("tab_id") == tab_id:
            columns = tab.get("viewport_columns")
            if _int(columns) and columns >= 0:
                return columns
            raise MultiplexerError(
                "unrecognized zellij list-tabs entry: field 'viewport_columns'"
            )
    raise MultiplexerError(f"zellij list-tabs does not list tab {tab_id}")


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
        self.notices: list[str] = []

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

    def detach(self, session: str) -> None:
        """Detach the client that last sent input; zellij has no detach-all."""
        self._action(_session(session), None, "detach")

    def kill_session(self, session: str) -> None:
        """End a running session and every pane in it (zellij may keep it EXITED)."""
        session = _session(session)
        if self.session_state(session) != SESSION_RUNNING:
            raise MultiplexerError(f"zellij session {session!r} is not running")
        result = self._cli("kill-session", session)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise MultiplexerError(
                f"zellij kill-session {session} exited {result.returncode}"
                + (f": {detail[0][:200]}" if detail else "")
            )

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
        (M1-0 recommendation). In a ``single`` session every pane but the
        sidebar opens as a pinned floating pane over the agent region (see
        the module docstring); a pane not read back as floating in that tab
        is closed again. A floating pane does not take the focus, so with
        ``focus`` it is selected afterwards (best effort).
        """
        session = _session(session)
        command = _argv(argv)
        options = []
        if name is not None:
            options += ["--name", _pane_name(name)]
        if cwd is not None:
            options += ["--cwd", _cwd(cwd)]
        if name != SIDEBAR_PANE_NAME:
            region = self._agent_region(session, self._pane_entries(session))
            if region is not None:
                tab_id, box, covered = region
                ref = self._spawn_over(session, tab_id, box, options, command)
                if covered:
                    self.notices.append(SIDEBAR_COVERED)
                if focus:
                    self.focus(ref)
                return ref
        result = self._new_pane(session, *options, "--", *command)
        return self._new_ref(session, result)

    def layout(self, session: str) -> str | None:
        """``single`` when the session has a home pane or a marked sidebar, else None.

        None means split or unknown (a session from before the marker).
        """
        entries = self._pane_entries(_session(session))
        if _home_anchor(entries) is not None or any(_runs_single(e) for e in entries):
            return LAYOUT_SINGLE
        return None

    def take_notices(self) -> list[str]:
        """Notices about pane placement since the last call (see the module docstring)."""
        notices, self.notices = self.notices, []
        return notices

    def align_agents(self, session: str, pane_ids) -> list[str]:
        """Move the given floating panes onto the current agent region; return those moved.

        Only floating panes in the region's tab whose geometry differs move;
        a pane that is gone, tiled or in another tab is left alone, and a
        session without an agent region (split) moves nothing.
        """
        session = _session(session)
        wanted = {_pane_id(pane_id) for pane_id in pane_ids}
        if not wanted:
            return []
        entries = self._pane_entries(session)
        region = self._agent_region(session, entries)
        if region is None:
            return []
        tab_id, box, _ = region
        moved = []
        for entry in entries:
            if (
                entry["is_plugin"]
                or str(entry["id"]) not in wanted
                or entry.get("is_floating") is not True
                or entry.get("tab_id") != tab_id
                or _box(entry) == box
            ):
                continue
            self._action(
                session, None, "change-floating-pane-coordinates",
                "--pane-id", f"terminal_{entry['id']}", *_coordinates(box),
                "--pinned", "true",
            )
            moved.append(str(entry["id"]))
        return moved

    def _agent_region(
        self, session: str, entries: list[dict]
    ) -> tuple[int, tuple[int, int, int, int], bool] | None:
        """``(tab_id, (x, y, columns, rows), covered)`` where agents show, or None (split).

        ``covered`` is True when the region lies over the sidebar's right part.
        """
        home = _home_anchor(entries)
        if home is not None:
            return _geometry(home)[0], _home_region(entries, home), False
        sidebar = next((entry for entry in entries if _marks_single(entry)), None)
        if sidebar is None:
            return None
        tab_id = _geometry(sidebar)[0]
        columns = _viewport_columns(
            self._answered(session, None, "list-tabs", "--json").stdout, tab_id
        )
        box, covered = _beside_sidebar(sidebar, columns)
        return tab_id, box, covered

    def _new_ref(self, session: str, result) -> PaneRef:
        match = _NEW_PANE.fullmatch((result.stdout or "").strip())
        if match is None:
            raise MultiplexerError(
                "zellij new-pane did not report a pane id "
                "(unknown or EXITED session?)"
            )
        return PaneRef(NAME, session, match.group(1), self._effective_socket_dir())

    def _spawn_over(
        self, session: str, tab_id: int, box: tuple[int, int, int, int],
        options: list[str], command: list[str],
    ) -> PaneRef:
        """Open a pinned floating pane over ``box`` in tab ``tab_id`` and read it back."""
        result = self._new_pane(
            session, "--tab-id", str(tab_id), "--floating", "--pinned", "true",
            *_coordinates(box), *options, "--", *command,
        )
        ref = self._new_ref(session, result)
        if self._floating_in(session, tab_id, ref.pane_id):
            return ref
        self._close_quietly(session, ref.pane_id)
        raise MultiplexerError(
            f"closed new pane {ref.pane_id}: it did not open as a floating pane "
            "in the agent area's tab"
        )

    def _floating_in(self, session: str, tab_id: int, pane_id: str) -> bool:
        """Read back that the pane is listed as floating in tab ``tab_id``."""
        for attempt in range(READBACK_ATTEMPTS):
            if any(
                not entry["is_plugin"]
                and str(entry["id"]) == pane_id
                and entry.get("is_floating") is True
                and entry.get("tab_id") == tab_id
                for entry in self._pane_entries(session)
            ):
                return True
            if attempt + 1 < READBACK_ATTEMPTS:
                time.sleep(READBACK_INTERVAL)
        return False

    def _close_quietly(self, session: str, pane_id: str) -> None:
        try:
            self._action(session, None, "close-pane", "--pane-id", f"terminal_{pane_id}")
        except MultiplexerError:
            pass

    def _new_pane(self, session: str, *args: str):
        """Run new-pane, asking again only after a transient no-active-session.

        zellij 0.44.2's socket probe can briefly deny a running session (#72,
        #107). That refusal comes before the request reaches the server, so no
        pane was created and asking again cannot open a second one. Any other
        failure, including a timeout, is not retried.
        """
        for _ in range(READBACK_ATTEMPTS - 1):
            try:
                return self._action(session, None, "new-pane", *args)
            except _NoActiveSession:
                time.sleep(READBACK_INTERVAL)
        return self._action(session, None, "new-pane", *args)

    def _pane_entries(self, session: str) -> list[dict]:
        """The raw list-panes entries, each checked for the fields PaneInfo needs."""
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
        for entry in entries:
            _pane_info(entry, session, None)
        return entries

    def list_panes(self, session: str) -> list[PaneInfo]:
        """Every pane, floating, collapsed and exited ones included."""
        session = _session(session)
        entries = self._pane_entries(session)
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

    def focused_panes(self, session: str) -> set[str]:
        """Pane ids that an attached client has focused (``list-clients``)."""
        prefix = "terminal_"
        clients = self._clients(_session(session), self._effective_socket_dir())
        return {
            shown[len(prefix):] for shown in clients.values() if shown.startswith(prefix)
        }

    def _focus_pane_id(self, session: str, socket_dir: str | None, expected: str) -> str | None:
        """Run focus-pane-id: None when it exited 0, else zellij's refusal text."""
        argv = [self._zellij(), "--session", session, "action", "focus-pane-id", expected]
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT,
                stdin=subprocess.DEVNULL,
                env=_cli_env(self._effective_socket_dir(socket_dir)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MultiplexerError(f"zellij action focus-pane-id failed: {error}") from error
        if result.returncode == 0:
            return None
        text = " ".join((result.stderr or result.stdout or "").split())[:200]
        return f"zellij action focus-pane-id failed (exit {result.returncode})" + (
            f": {text}" if text else ""
        )

    def focus(self, ref: PaneRef) -> FocusResult:
        """Select ref's pane for the most recent client; never activate.

        focus-pane-id acts on the client that most recently sent input and
        exits 2 with ``Pane Terminal(N) is already focused`` when that client
        already shows the pane (0.44.2 and 0.45.1). Only that refusal, for
        this pane and with the pane shown by a client on readback, counts as
        already focused; list-clients cannot tell which client the action
        targets, so another client merely showing the pane is not enough.
        """
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
            refused = self._focus_pane_id(session, socket_dir, expected)
            if refused is not None:
                already = _ALREADY_FOCUSED.search(refused)
                if already is None or already.group(1) != pane:
                    return _failed(refused)
            for attempt in range(READBACK_ATTEMPTS):
                after = self._clients(session, socket_dir)
                if refused is None and any(
                    after.get(client) == expected and shown != expected
                    for client, shown in before.items()
                ):
                    return _selected("tab or pane selection")
                if refused is not None and expected in after.values():
                    return _selected(ALREADY_FOCUSED)
                if attempt + 1 < READBACK_ATTEMPTS:
                    time.sleep(READBACK_INTERVAL)
            if refused is not None:
                return _failed(refused)
        except MultiplexerError as error:
            return _failed(error.detail)
        return _failed(f"no zellij client moved to pane {pane}")

    def show(self, ref: PaneRef) -> FocusResult:
        """Selecting a pinned agent pane raises it over the others, so showing is focusing."""
        return self.focus(ref)

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

    def rename_pane(self, ref: PaneRef, name: str) -> None:
        """Set the pane's frame title; the program in it may retitle it later."""
        session, pane, socket_dir = _target(ref)
        self._action(
            session, socket_dir, "rename-pane", "--pane-id", f"terminal_{pane}", "--",
            _pane_name(name),
        )


def _coordinates(box: tuple[int, int, int, int]) -> list[str]:
    x, y, columns, rows = box
    return ["-x", str(x), "-y", str(y), "--width", str(columns), "--height", str(rows)]


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
