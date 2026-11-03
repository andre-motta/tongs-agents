"""``tongs-agents notify``: report a launched pane's state without harness hooks.

Any command started from an ``other`` profile can call it, for example
``if make test; then tongs-agents notify done; else tongs-agents notify failed; fi``
(not ``&& ... ||``, which also reports failed when ``notify done`` is refused).
The pane is identified by ``TONGS_AGENTS_LAUNCH_ID``, which the launcher sets,
and its claimed launch record. The state is written as a schema v1 session
record (harness ``other``, source ``notify``) through the hook's record
builder and the shared session writer, so the sidebar and monitor show it like
a hook state.

Like the hook it is offline and never blocks on anything but the local state
directory. A call outside a launched pane, or in a pane whose harness reports
through its own hooks, exits 1 with one line on stderr. A record that cannot be
written is reported on stderr but exits 0, so wrappers are never failed by
their status report.
"""

from __future__ import annotations

import os
import sys
import time

from tongs_agents import discovery, hook, launches, terminal_apis
from tongs_agents.discovery import MAX_DETAIL_LENGTH, MAX_PATH_LENGTH
from tongs_agents.models import UNRESOLVED_INSTANCE_ID, SessionStatus
from tongs_agents.sessions_feed import NOTIFY_SOURCE

NAME = "notify"
HELP = "Report this launched pane's state (working, needs-you, done, failed, quiet)"

HARNESS = discovery.OTHER_HARNESS
EXIT_REFUSED = 1

STATES = {
    "working": SessionStatus.RUNNING,
    "needs-you": SessionStatus.WAITING,
    "done": SessionStatus.IDLE,
    "failed": SessionStatus.ERROR,
    "quiet": SessionStatus.DISCOVERED,
}


class NotifyRefused(Exception):
    """This process is not in a pane that ``notify`` may report for."""


def add_arguments(parser):
    parser.add_argument("state", choices=tuple(STATES), help="The state to report")
    parser.add_argument("message", nargs="*", help="Optional detail shown with the state")


def bounded_message(words, state: str) -> str:
    """One printable line of at most MAX_DETAIL_LENGTH UTF-8 bytes."""
    text = " ".join(words)
    text = "".join(char if char.isprintable() else " " for char in text)
    text = " ".join(text.split()) or state
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_DETAIL_LENGTH:
        text = encoded[:MAX_DETAIL_LENGTH].decode("utf-8", errors="ignore")
    return text


def _cwd() -> str:
    try:
        cwd = os.getcwd()
    except OSError:
        return ""
    if "\0" in cwd or len(cwd.encode("utf-8", errors="replace")) > MAX_PATH_LENGTH:
        return ""
    return cwd


def pane_launch() -> tuple[dict, launches.LaunchRecord]:
    """The launch identity from this process env and its claimed record."""
    launch = hook._capture_launch()
    if launch is None or "launch_id" not in launch:
        raise NotifyRefused(
            "not in a pane started by tongs-agents (no valid TONGS_AGENTS_LAUNCH_ID)"
        )
    launch_id = launch["launch_id"]
    record = launches.get(launch_id)
    if record is None or record.state != "running" or record.pid is None:
        raise NotifyRefused(f"no claimed launch record for {launch_id}")
    if record.harness != HARNESS:
        raise NotifyRefused(
            f"launch {launch_id} runs harness {record.harness!r}, "
            "which reports its state through its own hooks"
        )
    launch.setdefault("profile_id", record.profile)
    return launch, record


def _launched_process(record: launches.LaunchRecord):
    process = discovery.get_process_info(record.pid)
    if process is None or (
        record.process_start and process.start_token != record.process_start
    ):
        raise NotifyRefused(
            f"launch {record.launch_id} is no longer running (pid {record.pid})"
        )
    return process if record.process_start else None


def build_record(state: str, message: str, launch: dict,
                 record: launches.LaunchRecord, process) -> dict:
    session_id = record.launch_id
    if process is not None:
        instance_id = f"{process.pid}:{process.start_token}"
        process_identity = "identified"
    else:
        instance_id = UNRESOLVED_INSTANCE_ID
        process_identity = "unresolved"
    existing = discovery.read_session_record(HARNESS, session_id, instance_id)
    now = int(time.time())
    started = existing.get("started", now) if existing else now
    terminal_info = terminal_apis.capture_terminal_info()
    return hook.build_session_record(
        agent=HARNESS,
        session_id=session_id,
        instance_id=instance_id,
        cwd=_cwd(),
        process=process,
        process_identity=process_identity,
        status=STATES[state].value,
        detail=message,
        tool="",
        now=now,
        started=started,
        event_id=f"{HARNESS}.notify.{state}",
        terminal=terminal_info["terminal"],
        terminal_env=terminal_info["terminal_env"],
        launch=launch,
        source=NOTIFY_SOURCE,
    )


def run(args) -> int:
    try:
        launch, record = pane_launch()
        process = _launched_process(record)
    except NotifyRefused as error:
        print(f"tongs-agents notify: {error}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        session = build_record(
            args.state, bounded_message(args.message, args.state), launch, record, process
        )
        written = discovery.write_session_record(session)
    except (OSError, ValueError):
        written = False
    if not written:
        print(
            f"tongs-agents notify: could not record state {args.state!r}; continuing",
            file=sys.stderr,
        )
    return 0
