"""Fake harness fixture for the workspace validator; never a real agent.

A profile command runs it in a launcher pane:
``python fake_agent.py <pi|claude> <io_dir>``. It reports its inherited
launcher env, sends status through tongs-agents-hook, then idles reading
stdin, so keystrokes routed to its pane can be confirmed.

- ``pi`` sends fleet-event payloads (started, running, idle) with its own pid,
  as the pi extension does.
- ``claude`` reads ``$CLAUDE_CONFIG_DIR/settings.json`` and runs the
  SessionStart, UserPromptSubmit and Stop hook commands registered there with
  Claude-shaped payloads, as Claude Code does. A config dir without installed
  hooks sends nothing.

Hook commands are split with shlex and never run through a shell. Only the
standard library is used.
"""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

REPORTED_ENV = (
    "TONGS_AGENTS_LAUNCH_ID",
    "TONGS_AGENTS_PROFILE",
    "ZELLIJ",
    "ZELLIJ_SESSION_NAME",
    "ZELLIJ_PANE_ID",
    "ZELLIJ_SOCKET_DIR",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "PI_CODING_AGENT_DIR",
    "TMUX",
    "TMUX_PANE",
)
CLAUDE_EVENTS = (
    ("SessionStart", {"source": "startup"}),
    ("UserPromptSubmit", {"prompt": "fixture prompt"}),
    ("Stop", {"last_assistant_message": "fixture finished", "stop_hook_active": False}),
)
PI_STATUSES = (
    ("started", "session started"),
    ("running", "processing prompt"),
    ("idle", "fixture finished"),
)
HOOK_TIMEOUT = 10
CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f]")
SAFE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")


def profile_name(env):
    name = env.get("TONGS_AGENTS_PROFILE", "")
    return name if SAFE_NAME.match(name) else "unknown"


def pi_payloads(pid, session_id, cwd):
    """The fleet-event payloads a pi session sends, in order."""
    return [
        {
            "schema_version": 1,
            "event_id": f"pi.fixture.{sequence}",
            "session_id": session_id,
            "instance_id": f"{pid}:fixture",
            "cwd": cwd,
            "pid": pid,
            "sequence": sequence,
            "status": status,
            "detail": detail,
        }
        for sequence, (status, detail) in enumerate(PI_STATUSES, start=1)
    ]


def claude_hook_commands(settings, event):
    """Command strings registered for ``event`` in a Claude settings document."""
    groups = (settings.get("hooks") or {}).get(event) or []
    commands = []
    for group in groups:
        for hook in (group or {}).get("hooks") or []:
            if hook.get("type") == "command" and isinstance(hook.get("command"), str):
                commands.append(hook["command"])
    return commands


def claude_calls(settings, session_id, cwd):
    """(event, argv, payload) for every hook Claude Code would run."""
    calls = []
    for event, extra in CLAUDE_EVENTS:
        payload = {"session_id": session_id, "cwd": cwd, "hook_event_name": event, **extra}
        for command in claude_hook_commands(settings, event):
            calls.append((event, shlex.split(command), payload))
    return calls


def run_hook(argv, payload):
    try:
        result = subprocess.run(
            argv, input=json.dumps(payload), text=True, capture_output=True,
            timeout=HOOK_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return type(error).__name__
    return result.returncode


def read_settings(config_dir):
    if not config_dir:
        return None
    try:
        return json.loads((Path(config_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def emit(mode, env, cwd):
    pid = os.getpid()
    session_id = f"fixture-{uuid.uuid4().hex}"
    hooks = []
    settings_found = None
    if mode == "pi":
        binary = shutil.which("tongs-agents-hook") or "tongs-agents-hook"
        for payload in pi_payloads(pid, session_id, cwd):
            argv = [binary, "fleet-event", "--agent", "pi"]
            hooks.append({"event": payload["status"], "argv": argv,
                          "returncode": run_hook(argv, payload)})
    else:
        settings = read_settings(env.get("CLAUDE_CONFIG_DIR"))
        settings_found = settings is not None
        for event, argv, payload in claude_calls(settings or {}, session_id, cwd):
            hooks.append({"event": event, "argv": argv, "returncode": run_hook(argv, payload)})
    return {
        "mode": mode,
        "pid": pid,
        "session_id": session_id,
        "cwd": cwd,
        "settings_found": settings_found,
        "env": {key: env[key] for key in REPORTED_ENV if key in env},
        "hooks": [{"event": hook["event"],
                   "hook_command": os.path.basename(hook["argv"][0]) if hook["argv"] else "",
                   "hook_args": hook["argv"][1:],
                   "returncode": hook["returncode"]} for hook in hooks],
    }


def write_report(io_dir, name, report):
    path = Path(io_dir) / f"agent-{name}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report), encoding="utf-8")
    os.replace(temporary, path)


def idle(io_dir, name):
    """Record every line typed into this pane, without terminal control sequences."""
    target = Path(io_dir) / f"in-{name}"
    stream = sys.stdin.buffer
    while True:
        line = stream.readline()
        if not line:
            return
        text = CONTROL.sub("", line.decode("utf-8", "replace")).strip()
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2 or arguments[0] not in ("pi", "claude"):
        print("usage: fake_agent.py <pi|claude> <io_dir>", file=sys.stderr)
        return 64
    mode, io_dir = arguments
    env = dict(os.environ)
    name = profile_name(env)
    write_report(io_dir, name, emit(mode, env, os.getcwd()))
    idle(io_dir, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
