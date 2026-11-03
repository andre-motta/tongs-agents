"""Supervisor verbs: ``send``, ``read``, ``wait`` and ``agent-status``.

They drive only panes that tongs-agents spawned and verifies now; every call
goes through ``tongs_agents.control`` and is audited there. Exit codes:
0 ok, 1 error (usage errors included, see ``commands.EXIT_USAGE``), 2 timeout,
3 blocked (needs you), 4 agent exited, 5 refused (not spawned or not verified
by tongs-agents), 6 multiplexer unsupported.

This module registers several verbs through ``COMMANDS``.
"""

from __future__ import annotations

import json
import sys
from types import ModuleType

from tongs_agents import control

ACTOR = "cli"
STDIN = "-"
MAX_STDIN_BYTES = control.MAX_SEND_BYTES


def _fail(error: control.ControlError) -> int:
    print(f"tongs-agents: {error.detail}", file=sys.stderr)
    return error.exit_code


def _stdin_text() -> str:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise control.ControlError("no standard input to read the text from")
    raw = stream.read(MAX_STDIN_BYTES + 2)
    if len(raw) > MAX_STDIN_BYTES + 1:
        raise control.ControlError(
            f"standard input exceeds the {MAX_STDIN_BYTES} byte limit"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise control.ControlError("standard input is not valid UTF-8") from None
    text = text.replace("\r\n", "\n")
    return text[:-1] if text.endswith("\n") else text


def add_send_arguments(parser):
    parser.add_argument("launch_id", help="Launch id of a pane spawned by tongs-agents")
    parser.add_argument(
        "text", help="Prompt text, or - to read it from standard input (one trailing "
        "newline is dropped)",
    )
    parser.add_argument(
        "--no-submit", action="store_true",
        help="Paste the text without pressing Enter",
    )


def run_send(args) -> int:
    try:
        text = _stdin_text() if args.text == STDIN else args.text
        control.send(
            args.launch_id, text, actor=ACTOR, submit=not args.no_submit,
            caller=control.caller_launch_id(),
        )
    except control.ControlError as error:
        return _fail(error)
    return control.EXIT_OK


def add_read_arguments(parser):
    parser.add_argument("launch_id", help="Launch id of a pane spawned by tongs-agents")
    parser.add_argument(
        "--lines", type=int, default=control.MAX_READ_LINES,
        help=f"Number of trailing lines, 1 to {control.MAX_READ_LINES} "
        f"(default {control.MAX_READ_LINES})",
    )


def run_read(args) -> int:
    try:
        text = control.read(
            args.launch_id, args.lines, actor=ACTOR, caller=control.caller_launch_id()
        )
    except control.ControlError as error:
        return _fail(error)
    if text:
        print(text)
    return control.EXIT_OK


def add_wait_arguments(parser):
    parser.add_argument("launch_id", help="Launch id of a pane spawned by tongs-agents")
    parser.add_argument(
        "--until", choices=control.UNTIL, default="done",
        help="State to wait for (default done); any = done, needs-you, failed or ended",
    )
    parser.add_argument(
        "--timeout", type=float, default=float(control.MAX_WAIT_SECONDS),
        help=f"Seconds to wait, at most {control.MAX_WAIT_SECONDS} (default "
        f"{control.MAX_WAIT_SECONDS})",
    )


def run_wait(args) -> int:
    try:
        result = control.wait(
            args.launch_id, args.until, args.timeout, actor=ACTOR,
            caller=control.caller_launch_id(),
        )
    except control.ControlError as error:
        return _fail(error)
    shown = "timeout" if result.timed_out else result.status.state
    print(f"{result.status.launch_id} {shown}")
    return result.exit_code


def add_status_arguments(parser):
    parser.add_argument(
        "launch_id", nargs="?", help="Launch id; omit to list every launched pane",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON")


def _line(status: control.AgentStatus) -> str:
    detail = f"  {status.detail}" if status.detail else ""
    return (
        f"{status.launch_id}  {status.profile}  {status.session}:{status.pane_id}  "
        f"{status.state}{detail}"
    )


def run_status(args) -> int:
    caller = control.caller_launch_id()
    try:
        if args.launch_id is None:
            rows = control.list_statuses(actor=ACTOR, caller=caller)
        else:
            rows = [control.status(args.launch_id, actor=ACTOR, caller=caller)]
    except control.ControlError as error:
        return _fail(error)
    if args.json:
        payload = [row.to_json() for row in rows]
        print(json.dumps(payload if args.launch_id is None else payload[0], indent=2))
    else:
        for row in rows:
            print(_line(row))
    return control.EXIT_OK


def _verb(name: str, help_text: str, add_arguments, run) -> ModuleType:
    verb = ModuleType(f"{__name__}.{name}")
    verb.NAME = name
    verb.HELP = help_text
    verb.add_arguments = add_arguments
    verb.run = run
    return verb


COMMANDS = (
    _verb("agent-status", "Show the state of launched agent panes",
          add_status_arguments, run_status),
    _verb("read", "Print the last lines of a launched agent pane",
          add_read_arguments, run_read),
    _verb("send", "Send a prompt to a launched agent pane (paste, then Enter)",
          add_send_arguments, run_send),
    _verb("wait", "Wait until a launched agent is done, needs you or exits",
          add_wait_arguments, run_wait),
)
