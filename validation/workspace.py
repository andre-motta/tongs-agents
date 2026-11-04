"""Validate the M1 agent workspace against an isolated live Zellij session.

Linux helper, run locally and by the hosted workspace-validation job on each
pinned Zellij release. It follows validation/zellij.py: a fresh temporary root
holds HOME, the XDG config, state, cache, data and runtime dirs, TMPDIR,
ZELLIJ_SOCKET_DIR, ZELLIJ_CONFIG_DIR and every profile config dir; Zellij
clients run in private PTYs; only processes this run started (PID plus kernel
start time) are signalled; the evidence is redacted before it is printed or
written.

The workspace runs for real: ``tongs-agents install --claude-config-dir``,
``tongs-agents workspace`` (which execs Zellij), the sidebar consuming its spawn
request, the launcher claiming launch records, ``tongs-agents-hook`` writing
session records and an in-session rerun. The agents are fake harness
fixtures (validation/fixtures/fake_agent.py) that send hook events with their
inherited launcher env and then idle; claude, codex and pi on the private
PATH are a stub (validation/fixtures/fake_cli.py), so no real agent can start.
Row selection runs the real SidebarApp headless in this process with the
sidebar pane's Zellij identity, while parent terminal lookups are recorded to
prove no window activation is attempted; that step is instrumented, not a
keystroke into the sidebar pane.

The workspace uses the default ``single`` layout (#124): agent panes open as
pinned floating panes over the tiled home pane beside the sidebar. The run
checks that every agent pane is floating at the home pane's geometry (and
``dump-layout`` reports the floating panes pinned), that a row selection
raises its pane to the top (the last pane frame the first client drew is
the target's) and that it stays shown when the sidebar is focused again,
that resizing the clients and then the sidebar moves the launched agent
panes onto the new home geometry (the real sidebar's ``align_agents``) and
keeps them pinned, and, after closing the home pane, that a spawn floats
over the sidebar's right three quarters with a notice while the sidebar
fills the tab, then beside the sidebar with no notice once a tiled pane is
split off right of it; no home pane is opened again.
"""

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import struct
import sys
import tempfile
import termios
import threading
import time
import traceback
from unittest.mock import patch

from probe_terminals import (
    PtyClient,
    Redactor,
    Tracker,
    config_snapshot,
    platform_facts,
    process_cmdline,
    run,
    typed_target,
    wait_until,
    zellij_cache_mentions,
)

from tongs_agents import focus, launches, paths, workspace_spawn
from tongs_agents.commands import sidebar
from tongs_agents.multiplexers import HOME_PANE_NAME, MultiplexerError
from tongs_agents.multiplexers.zellij import (
    ACTIVATION_SKIPPED,
    MIN_AGENT_COLUMNS,
    SIDEBAR_COVERED,
    ZellijMultiplexer,
)
from tongs_agents.sessions_feed import LAUNCH_SOURCE, WorkspaceFeed
from tongs_agents.terminal_apis import tmux
from tongs_agents.terminal_apis import zellij as zellij_terminal

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "validation" / "fixtures"
FAKE_AGENT = FIXTURES / "fake_agent.py"
FAKE_CLI = FIXTURES / "fake_cli.py"
EVIDENCE = REPO_ROOT / "validation" / "evidence" / "workspace-linux-x86_64.json"
SCHEMA = "workspace-validation/1"
RUN_SCHEMA = "workspace-validation-run/1"
HASHED = (
    "src/tongs_agents/cli.py",
    "src/tongs_agents/commands/sidebar.py",
    "src/tongs_agents/commands/workspace.py",
    "src/tongs_agents/focus.py",
    "src/tongs_agents/hook.py",
    "src/tongs_agents/launch.py",
    "src/tongs_agents/launches.py",
    "src/tongs_agents/layouts/agents.kdl.tmpl",
    "src/tongs_agents/multiplexers/base.py",
    "src/tongs_agents/multiplexers/zellij.py",
    "src/tongs_agents/profiles.py",
    "src/tongs_agents/sessions_feed.py",
    "src/tongs_agents/workspace_spawn.py",
    "validation/fixtures/fake_agent.py",
    "validation/fixtures/fake_cli.py",
    "validation/workspace.py",
)
PRIVATE_DIRS = ("home", "bin", "sock", "zellij-cfg", "xdg-config", "xdg-state", "xdg-cache",
                "xdg-data", "xdg-run", "tmp", "io", "work", "claude-hooked", "claude-bare")
ISOLATION_KEYS = ("HOME", "ZELLIJ_SOCKET_DIR", "ZELLIJ_CONFIG_DIR", "XDG_CONFIG_HOME",
                  "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
                  "TMPDIR")
PASSED_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME", "PYTHONPATH")
PATH_TAIL = ("/usr/bin", "/bin")
FAKE_CLIS = ("claude", "codex", "pi")
FAKE_CLI_LOG_ENV = "TONGS_AGENTS_FAKE_CLI_LOG"
CONFIG_DIR_KEYS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR")
USER_CONFIG_TREES = (".claude", ".codex", ".pi", ".config", ".local", ".cache")
USER_AGENT_FILES = (".claude/settings.json", ".codex/hooks.json", ".codex/config.toml",
                    ".config/tongs-agents/config.toml", ".config/tongs-agents/installs.json",
                    ".pi/agent/settings.json")
FIRST_PROFILES = ("f1", "f2")
RERUN_PROFILES = ("f3", "f4")
HOOKED_PROFILE = "f3"
CONTROL_PROFILE = "f4"
# A budget is what a quiet machine meets; it is recorded in the evidence, and
# only its ceiling fails the run. Shared hosted runners can be slow enough to
# miss the budget with nothing wrong (#107).
SESSION_BUDGET = 5.0
SESSION_CEILING = 20.0
PANES_CEILING = 20.0
STATUS_BUDGET = 4.0
STATUS_CEILING = 15.0
REPORT_WAIT = 10.0
TOKEN_RE = re.compile(r"--spawn-request ([0-9a-f]{32})")
DOWN_ARROW = b"\x1b[B"
PRIVATE_PATH_RE = re.compile(r"/(?:tmp|home|run/user)/\S*")
DIAGNOSTIC_LINES = 40
DIAGNOSTIC_LINE_CHARS = 200
SCREEN_AFTER = 5.0
CLIENT_TAIL_BYTES = 16384
ANSI_RE = re.compile(
    rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()=>][0-9A-Za-z]?"
)
# Zellij draws rows with cursor positioning, not newlines.
CURSOR_ROW_RE = re.compile(rb"\x1b\[\d*(?:;\d*)?[Hf]")
RERUN_STDERR = "rerun.stderr"
# exec keeps the rerun the pane's own process; only its stderr goes to the file.
STDERR_TO_FILE = 'exec "$@" 2>"$0"'
CLIENT_ROWS = 40
CLIENT_COLUMNS = 120
WIDER_COLUMNS = 150
RESIZE_WAIT = 10.0
LAYOUT_WAIT = 5.0
PANES_RETRY = 3.0
FRAME_TITLE_RE = re.compile(r"\u250c ([^\u2500\u2502\u2510\u250c\n]+?) \u2500")
PINNED_RE = re.compile(r"^\s*pinned true\s*$", re.M)
IDLE_PANE = "import sys, time; time.sleep(600)"


class Check(Exception):
    """A validation expectation that did not hold."""


def expect(condition, message):
    if not condition:
        raise Check(message)


def file_digest(relative):
    return hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()


def repo_facts():
    code, head, _ = run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    _, dirty, _ = run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *HASHED])
    return {
        "tested_commit": head if code == 0 else None,
        "modified_since_commit": bool(dirty),
        "sha256": {relative: file_digest(relative) for relative in HASHED},
    }


def _inside(path, directory):
    try:
        return os.path.commonpath([path, directory]) == directory
    except ValueError:
        return False


def default_socket_dirs(real_env=None):
    """Where zellij puts sockets when ZELLIJ_SOCKET_DIR is unset."""
    real_env = os.environ if real_env is None else real_env
    uid = os.getuid()
    dirs = {f"/run/user/{uid}/zellij", f"/tmp/zellij-{uid}",
            os.path.join(tempfile.gettempdir(), f"zellij-{uid}")}
    runtime = real_env.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime):
        dirs.add(os.path.join(runtime, "zellij"))
    return {os.path.realpath(path) for path in dirs}


def check_isolation(env, root, home=None, real_env=None):
    """Refuse an env that could reach the user's sockets, config or state."""
    home = os.path.realpath(Path.home() if home is None else home)
    root = os.path.realpath(root)
    refused = default_socket_dirs(real_env) | {
        os.path.realpath(os.path.join(home, tree)) for tree in USER_CONFIG_TREES}
    expect(root != home and not any(_inside(root, tree) for tree in refused),
           "the private root is inside a user config or default socket dir")
    for key in ISOLATION_KEYS:
        value = env.get(key)
        expect(isinstance(value, str) and os.path.isabs(value),
               f"{key} is not an absolute private path")
        real = os.path.realpath(value)
        expect(_inside(real, root), f"{key} is outside the private root")
        expect(real != home and not any(_inside(real, tree) for tree in refused),
               f"{key} points at a user config or default socket dir")
    for key, value in env.items():
        if key in CONFIG_DIR_KEYS or (key.startswith("TONGS_AGENTS_")
                                      and key != FAKE_CLI_LOG_ENV) or key == "FLEET_DIR":
            raise Check(f"{key} is inherited into the isolated env")
        if key.startswith(("ZELLIJ", "TMUX")) and key not in ISOLATION_KEYS:
            raise Check(f"{key} is inherited into the isolated env")
    entries = env.get("PATH", "").split(os.pathsep)
    expect(entries[:1] == [os.path.join(root, "bin")],
           "the private bin dir is not first on PATH")
    expect(not any(_inside(os.path.realpath(entry), home) for entry in entries[1:]),
           "PATH reaches into the user's home")
    return True


def isolated_env(root, real_env=None):
    """A fresh env for every process of the run: allowlisted values plus private dirs."""
    real_env = os.environ if real_env is None else real_env
    root = Path(root)
    env = {key: real_env[key] for key in PASSED_ENV if key in real_env}
    env.update({
        "PATH": os.pathsep.join((str(root / "bin"), *PATH_TAIL)),
        "TERM": "xterm-256color",
        "HOME": str(root / "home"),
        "ZELLIJ_SOCKET_DIR": str(root / "sock"),
        "ZELLIJ_CONFIG_DIR": str(root / "zellij-cfg"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_RUNTIME_DIR": str(root / "xdg-run"),
        "TMPDIR": str(root / "tmp"),
        FAKE_CLI_LOG_ENV: str(root / "io" / "cli-calls.jsonl"),
    })
    return env


def entry_point(name):
    beside = Path(sys.executable).parent / name
    if beside.is_file() and os.access(beside, os.X_OK):
        return str(beside)
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not installed beside {sys.executable} or on PATH")
    return found


def write_bin(root, zellij_binary):
    """Private PATH: zellij, the tongs-agents entry points and stub agent CLIs."""
    directory = Path(root) / "bin"
    links = {"zellij": os.path.realpath(zellij_binary)}
    for name in ("tongs-agents", "tongs-agents-hook"):
        links[name] = entry_point(name)
    for name in FAKE_CLIS:
        links[name] = str(FAKE_CLI)
    for name, target in links.items():
        os.symlink(target, directory / name)
    return directory


def toml_string(value):
    return json.dumps(str(value))


def write_profiles(root, python=None):
    """The fixture profiles: two pi emitters, a hooked and a bare Claude config dir."""
    root = Path(root)
    python = sys.executable if python is None else python
    io_dir = root / "io"
    specs = (
        ("f1", "pi", "pi", None),
        ("f2", "pi", "pi", None),
        (HOOKED_PROFILE, "claude", "claude", root / "claude-hooked"),
        (CONTROL_PROFILE, "claude", "claude", root / "claude-bare"),
    )
    lines = []
    for name, mode, harness, config_dir in specs:
        cwd = root / "work" / name
        cwd.mkdir(parents=True, exist_ok=True)
        command = [python, str(FAKE_AGENT), mode, str(io_dir)]
        lines += [f"[profiles.{name}]", f"command = {json.dumps(command)}",
                  f"harness = {toml_string(harness)}", f"cwd = {toml_string(cwd)}"]
        if config_dir is not None:
            lines.append(f"config_dir = {toml_string(config_dir)}")
        lines.append("")
    directory = root / "xdg-config" / "tongs-agents"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_fixture(root, zellij_binary):
    root = Path(root)
    for name in PRIVATE_DIRS:
        (root / name).mkdir(mode=0o700)
    (root / "zellij-cfg" / "config.kdl").write_text(
        "show_startup_tips false\nshow_release_notes false\nsession_serialization false\n")
    write_bin(root, zellij_binary)
    write_profiles(root)


class Session:
    """Raw zellij commands against one session in the private socket dir."""

    def __init__(self, name, env, binary):
        self.name = name
        self.env = env
        self.binary = binary

    def action(self, *arguments):
        return run([self.binary, "--session", self.name, "action", *arguments], env=self.env)

    def _clients_once(self):
        code, out, _ = self.action("list-clients")
        if code != 0:
            return {}
        return {parts[0]: parts[1] for parts in (line.split() for line in out.splitlines()[1:])
                if len(parts) >= 2}

    def clients(self):
        """Attached clients; zellij 0.44.2 sometimes prints nothing, so empty is retried."""
        return wait_until(self._clients_once, 2) or {}

    def _panes_once(self):
        code, out, _ = self.action("list-panes", "--json")
        if code != 0 or not out.strip():
            return None
        try:
            panes = [item for item in json.loads(out) if not item.get("is_plugin")]
        except ValueError:
            return None
        return panes or None

    def panes(self):
        """Terminal panes; empty or failed answers are asked again for a few seconds.

        zellij 0.44.2 and 0.45.1 now and then answer list-panes with nothing
        on a running session (#72), and a workspace session always has
        terminal panes, so an empty answer is never taken as the result
        before ``PANES_RETRY`` seconds have passed.
        """
        return wait_until(self._panes_once, PANES_RETRY) or []

    def sessions(self):
        code, out, _ = run([self.binary, "list-sessions", "--short", "--no-formatting"],
                           env=self.env)
        return sorted(out.split()) if code == 0 else []


def servers(root):
    """zellij server processes serving the private socket dir."""
    sock = str(Path(root) / "sock")
    found = 0
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            command = process_cmdline(int(entry))
            if "--server" in command and sock in command:
                found += 1
    return found


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cli_calls(root):
    calls = []
    try:
        lines = (Path(root) / "io" / "cli-calls.jsonl").read_text().splitlines()
    except OSError:
        return calls
    for line in lines:
        try:
            calls.append(json.loads(line))
        except ValueError:
            pass
    return calls


def agent_snapshot(home):
    """Content hashes of the user's own agent and tongs-agents config files."""
    digests = {}
    for relative in USER_AGENT_FILES:
        path = Path(home) / relative
        try:
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digests[relative] = None
    return digests


def records_by_profile(session):
    return {record.profile: record for record in launches.list_records(session)
            if record.session == session}


def rows_by_launch(feed):
    return {row.launch_id: row for row in feed.snapshot() if row.launch_id}


def exercise(root, session, tracker, observations):
    root = Path(root)
    env = isolated_env(root)
    check_isolation(env, root)
    zellij_bin = str(root / "bin" / "zellij")
    tongs_bin = str(root / "bin" / "tongs-agents")
    io_dir = root / "io"
    code, version, _ = run([zellij_bin, "--version"], env=env)
    expect(code == 0 and version, "zellij --version failed")
    observations["zellij_version"] = version
    target = Session(session, env, zellij_bin)

    with patch.dict(os.environ, env, clear=True):
        owned = {"state": paths.state_dir(), "config": paths.config_dir(),
                 "runtime": paths.runtime_dir()}
        expect(all(_inside(os.path.realpath(path), os.path.realpath(root))
                   for path in owned.values()), "tongs-agents dirs resolve outside the root")
        install(root, env, tongs_bin, observations)
        mux = ZellijMultiplexer()
        feed = WorkspaceFeed(session, mux)
        token = create_workspace(root, session, env, tongs_bin, target, tracker, observations)
        spawn_request_consumed(session, token, target, observations)
        first = hook_rows(session, feed, target, FIRST_PROFILES,
                          observations.pop("_panes_ready_at"), observations, "first_profiles")
        overlay_membership(target, FIRST_PROFILES, observations, "overlay_first")
        rerun_in_session(root, session, tongs_bin, target, tracker, observations)
        rerun_rows(session, feed, target, observations)
        overlay_membership(target, (*FIRST_PROFILES, *RERUN_PROFILES), observations,
                           "overlay_after_rerun")
        agent_env(io_dir, session, target, observations)
        row_focus(session, env, target, tracker, io_dir, first, observations)
        resize_alignment(target, observations)
        home_recovery(root, session, target, tracker, observations)


def install(root, env, tongs_bin, observations):
    hooked = root / "claude-hooked"
    code, _, err = run([tongs_bin, "install", "--claude-config-dir", str(hooked)],
                       env=env, cwd=str(root / "work"), timeout=60)
    settings = read_json(hooked / "settings.json") or {}
    commands = [hook.get("command", "") for groups in (settings.get("hooks") or {}).values()
                for group in groups for hook in group.get("hooks", [])]
    events = sorted(event for event, groups in (settings.get("hooks") or {}).items()
                    if any("tongs-agents-hook" in hook.get("command", "")
                           for group in groups for hook in group.get("hooks", [])))
    calls = cli_calls(root)
    mcp = [call for call in calls if call["cli"] == "claude" and call["args"][:2] == ["mcp", "add"]]
    observations["install"] = {
        "argv": ["tongs-agents", "install", "--claude-config-dir", "<claude-hooked>"],
        "exit_code": code,
        "hook_events": events,
        "hook_commands_are_owned": bool(commands) and all(
            os.path.basename(command.split()[0]) == "tongs-agents-hook" for command in commands),
        "private_home_settings_written": (root / "home" / ".claude" / "settings.json").exists(),
        "bare_config_dir_untouched": not any((root / "claude-bare").iterdir()),
        "claude_mcp_add_calls": [{"args": call["args"][:5],
                                  "claude_config_dir_is_target":
                                      call["env"].get("CLAUDE_CONFIG_DIR") == str(hooked)}
                                 for call in mcp],
    }
    expect(code == 0, f"install --claude-config-dir failed: {err[-200:]}")
    expect("SessionStart" in events and "Stop" in events, "install wrote no hook groups")
    expect(observations["install"]["hook_commands_are_owned"], "foreign hook command written")
    expect(not observations["install"]["private_home_settings_written"],
           "install touched the default Claude config dir")
    expect(observations["install"]["bare_config_dir_untouched"],
           "install touched a config dir it was not given")
    expect(len(mcp) == 1 and observations["install"]["claude_mcp_add_calls"][0][
        "claude_config_dir_is_target"], "MCP registration did not target the config dir")


class CapturingClient(PtyClient):
    """A PtyClient that keeps its last output, to explain a failed workspace start."""

    def __init__(self, *arguments):
        self.tail = b""
        super().__init__(*arguments)

    def _drain(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.tail = (self.tail + data)[-CLIENT_TAIL_BYTES:]

    def screen(self):
        return screen_text(self.tail)


def screen_text(raw):
    """Terminal output as plain text lines, with escape sequences removed.

    A cursor move to a row starts a new line, so a redrawn screen does not run
    together into one long line.
    """
    text = ANSI_RE.sub(b"", CURSOR_ROW_RE.sub(b"\n", raw)).decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def pane_titles(target):
    return sorted(pane.get("title", "") for pane in target.panes())


def diagnostic_lines(text, root, limit=DIAGNOSTIC_LINES, width=DIAGNOSTIC_LINE_CHARS):
    """The last ``limit`` non-blank lines of ``text``, with private paths dropped.

    Each line is cut to ``width`` characters after redaction.

    A pane dump wraps long lines, so a path the final redaction would replace
    can be split; any piece still naming a private dir becomes ``<path>``.
    """
    text = (text or "").replace(str(root), "<tmp>")
    lines = [PRIVATE_PATH_RE.sub("<path>", line).rstrip() for line in text.splitlines()]
    lines = [line if len(line) <= width else line[:width - 3] + "..." for line in lines]
    return [line for line in lines if line.strip()][-limit:]


def pane_screen(target, title, root):
    """The visible screen of the first pane titled ``title``, for failure evidence."""
    pane = next((item for item in target.panes() if item.get("title") == title), None)
    if pane is None:
        return None
    code, out, _ = target.action("dump-screen", "--pane-id", f"terminal_{pane.get('id')}")
    return diagnostic_lines(out, root) if code == 0 else None


def create_workspace(root, session, env, tongs_bin, target, tracker, observations):
    argv = [tongs_bin, "workspace"]
    for name in FIRST_PROFILES:
        argv += ["--profile", name]
    argv += ["--no-tongs", "--session", session]
    started = time.monotonic()
    client = CapturingClient(argv, env, str(root / "work"), tracker, "client")
    observations.setdefault("_ptys", []).append(client)
    created = wait_until(lambda: session in target.sessions(), SESSION_CEILING)
    session_seconds = round(time.monotonic() - started, 2)
    timing = {
        "session_listed_seconds": session_seconds,
        "session_budget_seconds": SESSION_BUDGET,
        "session_within_budget": bool(created) and session_seconds <= SESSION_BUDGET,
        "session_ceiling_seconds": SESSION_CEILING,
    }
    if not created:
        observations["workspace"] = {
            **timing,
            "session_listed": False,
            "other_sessions": len(target.sessions()),
            "client_screen": diagnostic_lines(client.screen(), root),
        }
    expect(created, f"workspace did not create the session within {SESSION_CEILING} s "
                    "(see workspace.client_screen)")
    expected = len(FIRST_PROFILES) + 2
    early = {}

    def panes_ready():
        if len(target.panes()) >= expected:
            return True
        # A spawn failure is a 15 s sidebar toast; read it while it shows.
        if "screen" not in early and time.monotonic() - started >= SCREEN_AFTER:
            early["screen"] = pane_screen(target, workspace_spawn.SIDEBAR_PANE_NAME, root)
        return False

    ready = wait_until(panes_ready, PANES_CEILING)
    panes_seconds = round(time.monotonic() - started, 2)
    tracker.sweep()
    panes = target.panes()
    sidebar_panes = [pane for pane in panes
                     if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME]
    home_panes = [pane for pane in panes if pane.get("title") == HOME_PANE_NAME]
    command = (sidebar_panes[0].get("terminal_command") or "") if sidebar_panes else ""
    match = TOKEN_RE.search(command)
    observations["workspace"] = {
        "argv": ["tongs-agents", "workspace", "--profile", "f1", "--profile", "f2",
                 "--no-tongs", "--session", "<session>"],
        **timing,
        "panes_listed_seconds": panes_seconds,
        "panes_ceiling_seconds": PANES_CEILING,
        "terminal_panes": len(panes),
        "expected_terminal_panes": expected,
        "pane_titles": pane_titles(target),
        "sidebar_panes": len(sidebar_panes),
        "home_panes": len(home_panes),
    }
    if not ready or len(panes) != expected:
        observations["workspace"]["sidebar_screen"] = early.get("screen") or pane_screen(
            target, workspace_spawn.SIDEBAR_PANE_NAME, root)
        observations["workspace"]["client_screen"] = diagnostic_lines(client.screen(), root)
    observations["_panes_ready_at"] = time.monotonic()
    expect(session_seconds <= SESSION_CEILING,
           f"session took {session_seconds} s (ceiling {SESSION_CEILING} s, "
           f"budget {SESSION_BUDGET} s)")
    expect(ready and len(panes) == expected, f"expected {expected} panes, got {len(panes)}")
    expect(len(sidebar_panes) == 1, "the workspace does not have exactly one sidebar pane")
    expect(len(home_panes) == 1, "the workspace does not have exactly one home pane")
    expect(match is not None, "the sidebar pane does not name a spawn request")
    return match.group(1)


def spawn_request_consumed(session, token, target, observations):
    records = records_by_profile(session)
    requests = workspace_spawn.request_path(token).parent
    layouts = workspace_spawn.layout_path(token).parent
    titles = {pane.get("title"): str(pane.get("id")) for pane in target.panes()}
    observations["spawn_request"] = {
        "request_file_after_mount": workspace_spawn.request_path(token).exists(),
        "layout_file_after_mount": workspace_spawn.layout_path(token).exists(),
        "requests_dir_entries": len(list(requests.iterdir())) if requests.is_dir() else 0,
        "layouts_dir_entries": len(list(layouts.iterdir())) if layouts.is_dir() else 0,
        "second_consume": workspace_spawn.consume_spawn_request(token),
        "launch_states": {name: records[name].state for name in sorted(records)},
        "launch_panes_match": all(records[name].pane_id == titles.get(name)
                                  for name in FIRST_PROFILES if name in records),
    }
    facts = observations["spawn_request"]
    expect(not facts["request_file_after_mount"] and facts["requests_dir_entries"] == 0,
           "the spawn request was not consumed")
    expect(not facts["layout_file_after_mount"], "the rendered layout was not removed")
    expect(facts["second_consume"] is None, "a second consume returned the request")
    expect(set(records) == set(FIRST_PROFILES), f"unexpected launch records {sorted(records)}")


def hook_rows(session, feed, target, profiles, since, observations, key):
    """Wait until every profile has a hook row joined to its launch record."""
    deadline = since + STATUS_CEILING
    found = {}
    while time.monotonic() < deadline:
        records = records_by_profile(session)
        rows = rows_by_launch(feed)
        for name in profiles:
            record = records.get(name)
            row = rows.get(record.launch_id) if record else None
            if name not in found and row is not None and row.source == "hook":
                found[name] = (round(time.monotonic() - since, 2), record, row)
        if len(found) == len(profiles):
            break
        time.sleep(0.2)
    facts = {}
    for name in profiles:
        if name not in found:
            facts[name] = {"hook_row": False}
            continue
        seconds, record, row = found[name]
        facts[name] = {
            "hook_row": True,
            "seconds_after_panes_listed": seconds,
            "within_status_budget": seconds <= STATUS_BUDGET,
            "source": row.source,
            "status": row.status.value,
            "agent": row.agent,
            "launch_id_matches": row.launch_id == record.launch_id,
            "profile_id_matches": row.profile_id == record.profile == name,
            "pid_matches_launch": bool(row.pid) and row.pid == str(record.pid),
            "process_identity_resolved": bool(row.pid),
            "pane_matches_launch": row.terminal_env.get("ZELLIJ_PANE_ID") == record.pane_id,
            "session_matches": row.terminal_env.get("ZELLIJ_SESSION_NAME") == session,
        }
    observations[key] = {"status_budget_seconds": STATUS_BUDGET,
                         "status_ceiling_seconds": STATUS_CEILING, "profiles": facts}
    for name, item in facts.items():
        expect(item["hook_row"], f"profile {name} has no hook row in sidebar data "
                                 f"within {STATUS_CEILING} s")
        expect(item["seconds_after_panes_listed"] <= STATUS_CEILING,
               f"profile {name} status took {item['seconds_after_panes_listed']} s "
               f"(ceiling {STATUS_CEILING} s, budget {STATUS_BUDGET} s)")
        expect(item["launch_id_matches"] and item["profile_id_matches"]
               and item["pane_matches_launch"] and item["session_matches"],
               f"profile {name} row does not match its launch record")
        expect(item["status"] != "discovered", f"profile {name} row is only discovered")
    return {name: found[name][2] for name in profiles}


def box(pane):
    return tuple(pane.get(key) for key in ("tab_id", "pane_x", "pane_y", "pane_columns",
                                           "pane_rows"))


def overlay_of(panes):
    """The tiled home pane and the floating panes at exactly its tab and geometry."""
    home = next((pane for pane in panes if pane.get("title") == HOME_PANE_NAME
                 and not pane.get("is_floating")), None)
    if home is None:
        return None, []
    return home, [pane for pane in panes if pane.get("is_floating") and box(pane) == box(home)]


def pinned_panes(target, expected):
    """How many floating panes ``dump-layout`` reports pinned (list-panes has no field).

    zellij 0.44.2 now and then answers with no layout (#72), so the count is
    read again until it reaches ``expected`` or the wait ends.
    """
    seen = []

    def count():
        code, out, _ = target.action("dump-layout")
        if code == 0 and out.strip():
            seen.append(len(PINNED_RE.findall(out)))
        return seen and seen[-1] == expected

    wait_until(count, LAYOUT_WAIT)
    return seen[-1] if seen else None


def overlay_membership(target, profiles, observations, key):
    """Every profile pane floats pinned over the home pane; the sidebar stays tiled."""
    panes = target.panes()
    home, members = overlay_of(panes)
    titles = sorted(pane.get("title", "") for pane in members)
    sidebar_pane = next((pane for pane in panes
                         if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME), {})
    floating = sum(1 for pane in panes if pane.get("is_floating"))
    observations[key] = {
        "home_pane": home is not None,
        "member_titles": titles,
        "profiles_over_home": all(name in titles for name in profiles),
        "sidebar_tiled": bool(sidebar_pane) and not sidebar_pane.get("is_floating"),
        "floating_panes": floating,
        "pinned_panes": pinned_panes(target, floating),
        "all_listed": len(panes),
    }
    facts = observations[key]
    expect(facts["home_pane"], "the workspace has no home pane")
    expect(facts["profiles_over_home"], f"profile panes are not over the home pane: {titles}")
    expect(facts["sidebar_tiled"], "the sidebar is not a tiled pane")
    expect(facts["pinned_panes"] == floating,
           f"{facts['pinned_panes']} of {floating} floating panes are pinned")


def frame_titles(client):
    """Pane frame titles in the order the client drew them, oldest first."""
    return FRAME_TITLE_RE.findall(client.screen())


def top_title(client, candidates, wait=1.0):
    """The last of ``candidates`` the client drew as a pane frame title, once settled.

    A floating pane brought to the top is redrawn whole, frame included; a
    hidden floating layer redraws the home pane's frame instead.
    """
    time.sleep(wait)
    titles = [title for title in frame_titles(client) if title in candidates]
    return titles[-1] if titles else None


def viewport_columns(target, tab_id):
    """The tab's viewport width; asks again for ``PANES_RETRY`` seconds on an empty answer.

    zellij 0.44.2 now and then answers list-tabs with nothing, like list-panes.
    """
    found = {}

    def once():
        code, out, _ = target.action("list-tabs", "--json")
        if code != 0 or not out:
            return False
        try:
            tabs = json.loads(out)
        except ValueError:
            return False
        found["columns"] = next((tab.get("viewport_columns") for tab in tabs
                                 if isinstance(tab, dict) and tab.get("tab_id") == tab_id),
                                None)
        return found["columns"] is not None

    wait_until(once, PANES_RETRY)
    return found.get("columns")


def sidebar_of(panes):
    return next((pane for pane in panes
                 if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME), {})


def placement(root, session, target, tracker, name):
    """Spawn ``name`` through the adapter; return its facts against the sidebar."""
    mux = ZellijMultiplexer()
    ref = mux.spawn(session, [sys.executable, "-c", IDLE_PANE, str(root)], name=name)
    tracker.sweep()
    panes = target.panes()
    sidebar_pane = sidebar_of(panes)
    pane = next((item for item in panes if str(item.get("id")) == ref.pane_id), {})
    x, y, width, rows = (pane.get(key) or 0 for key in
                         ("pane_x", "pane_y", "pane_columns", "pane_rows"))
    left = sidebar_pane.get("pane_x") or 0
    right = left + (sidebar_pane.get("pane_columns") or 0)
    viewport = viewport_columns(target, sidebar_pane.get("tab_id"))
    return {
        "home_reopened": any(item.get("title") == HOME_PANE_NAME for item in panes),
        "floating_in_sidebar_tab": bool(pane.get("is_floating"))
        and pane.get("tab_id") == sidebar_pane.get("tab_id"),
        "rows_match_sidebar": (y, rows) == (sidebar_pane.get("pane_y"),
                                            sidebar_pane.get("pane_rows")),
        "viewport_columns": viewport,
        "sidebar_fills_tab": viewport is not None and viewport - right < MIN_AGENT_COLUMNS,
        "beside_sidebar": x == right and viewport is not None and width == viewport - right,
        "covers_sidebar_right": left < x < right and x + width == right,
        "sidebar_geometry": [sidebar_pane.get(key) for key in
                             ("pane_x", "pane_y", "pane_columns", "pane_rows")],
        "spawn_geometry": [x, y, width, rows],
        "notices": mux.take_notices(),
    }


def home_recovery(root, session, target, tracker, observations):
    """Close the home pane, then spawn twice through the adapter.

    The sidebar of a ``single`` layout runs with ``--layout single``, so the
    first spawn, with the sidebar filling the tab, floats over the sidebar's
    right three quarters with the covered-sidebar notice. A tiled pane is
    then split off right of the sidebar, and the second spawn floats over
    everything right of the sidebar with no notice. No home pane is opened
    again.
    """
    home, _ = overlay_of(target.panes())
    expect(home is not None, "the workspace has no home pane")
    code, _, _ = target.action("close-pane", "--pane-id", f"terminal_{home.get('id')}")
    closed = wait_until(lambda: overlay_of(target.panes())[0] is None, RESIZE_WAIT)
    covered = placement(root, session, target, tracker, "recovered")
    sidebar_pane = sidebar_of(target.panes())
    target.action("focus-pane-id", f"terminal_{sidebar_pane.get('id')}")
    split_code, out, _ = target.action("new-pane", "--direction", "right", "--name", "filler",
                                       "--", sys.executable, "-c", IDLE_PANE, str(root))
    filler_id = out.strip().removeprefix("terminal_")
    tracker.sweep()

    def split():
        panes = target.panes()
        filler = next((pane for pane in panes if str(pane.get("id")) == filler_id), {})
        side = sidebar_of(panes)
        return (filler and not filler.get("is_floating")
                and filler.get("pane_x") == (side.get("pane_x") or 0)
                + (side.get("pane_columns") or 0))

    split_ok = wait_until(split, RESIZE_WAIT)
    beside = placement(root, session, target, tracker, "beside")
    observations["home_recovery"] = {
        "home_closed": bool(closed) and code == 0,
        "covered": covered,
        "filler_tiled_right_of_sidebar": bool(split_ok) and split_code == 0,
        "beside": beside,
    }
    facts = observations["home_recovery"]
    expect(facts["home_closed"], "the home pane could not be closed")
    for step in (covered, beside):
        expect(not step["home_reopened"], "a home pane was opened again")
        expect(step["floating_in_sidebar_tab"] and step["rows_match_sidebar"],
               f"a spawn after closing the home pane is not in the agent area: {step}")
    expect(covered["sidebar_fills_tab"] and covered["covers_sidebar_right"]
           and covered["notices"] == [SIDEBAR_COVERED],
           f"the spawn over a sidebar filling the tab is misplaced: {covered}")
    expect(facts["filler_tiled_right_of_sidebar"],
           f"no tiled pane could be split off right of the sidebar: {facts}")
    expect(not beside["sidebar_fills_tab"] and beside["beside_sidebar"]
           and not beside["notices"],
           f"the spawn beside the sidebar is misplaced: {beside}")


def resize(ptys, rows, columns):
    for client in ptys:
        fcntl.ioctl(client.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


def launched_agents_aligned(target, session):
    """``(home, aligned)``: whether every launched pane sits at the home geometry."""
    panes = {str(pane.get("id")): pane for pane in target.panes()}
    home, _ = overlay_of(list(panes.values()))
    ids = [record.pane_id for record in records_by_profile(session).values()]
    if home is None or not ids:
        return home, False
    return home, all(box(panes.get(pane_id, {})) == box(home) for pane_id in ids)


def resize_alignment(target, observations):
    """Widen the clients, then the sidebar; the sidebar moves its agents along.

    The real sidebar pane gets each resize and calls ``align_agents`` for the
    panes of its session's launch records.
    """
    session = target.name
    home, _ = overlay_of(target.panes())
    expect(home is not None, "the workspace has no home pane")
    before = box(home)
    resize(observations["_ptys"], CLIENT_ROWS, WIDER_COLUMNS)

    def realigned(previous):
        def check():
            moved, aligned = launched_agents_aligned(target, session)
            return moved is not None and box(moved) != previous and aligned
        return check

    after_clients = wait_until(realigned(before), RESIZE_WAIT)
    home_now, _ = overlay_of(target.panes())
    widened = box(home_now) if home_now else None
    sidebar_pane = next(str(pane.get("id")) for pane in target.panes()
                        if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME)
    target.action("focus-pane-id", f"terminal_{sidebar_pane}")
    target.action("resize", "increase", "right")
    after_sidebar = wait_until(realigned(widened), RESIZE_WAIT)
    home_last, _ = overlay_of(target.panes())
    floating = sum(1 for pane in target.panes() if pane.get("is_floating"))
    observations["resize_alignment"] = {
        "home_before": list(before),
        "home_after_clients": list(widened) if widened else None,
        "home_after_sidebar": list(box(home_last)) if home_last else None,
        "aligned_after_clients": bool(after_clients),
        "aligned_after_sidebar": bool(after_sidebar),
        "floating_panes": floating,
        "pinned_panes": pinned_panes(target, floating),
    }
    facts = observations["resize_alignment"]
    if not (after_clients and after_sidebar):
        facts["geometry"] = [[pane.get("title"), *box(pane), pane.get("is_floating")]
                             for pane in target.panes()]
    expect(facts["aligned_after_clients"],
           f"the agents did not follow the home pane after a terminal resize: {facts}")
    expect(facts["aligned_after_sidebar"],
           f"the agents did not follow the home pane after a sidebar resize: {facts}")
    expect(facts["pinned_panes"] == floating,
           f"{facts['pinned_panes']} of {floating} floating panes are pinned after moving")


def rerun_in_session(root, session, tongs_bin, target, tracker, observations):
    before_panes = {str(pane.get("id")) for pane in target.panes()}
    before_sessions = target.sessions()
    before_servers = servers(root)
    argv = [tongs_bin, "workspace"]
    for name in RERUN_PROFILES:
        argv += ["--profile", name]
    stderr_path = Path(root) / "io" / RERUN_STDERR
    started = time.monotonic()
    code, out, err = target.action("new-pane", "--name", "rerun", "--", "/bin/sh", "-c",
                                   STDERR_TO_FILE, str(stderr_path), *argv)
    expect(code == 0 and out.startswith("terminal_"), f"rerun pane did not start: {err[:120]}")
    rerun_id = out.removeprefix("terminal_")

    def rerun_pane():
        return next((pane for pane in target.panes() if str(pane.get("id")) == rerun_id), None)

    exited = wait_until(lambda: (rerun_pane() or {}).get("exited"), 20)
    expected = len(before_panes) + 1 + len(RERUN_PROFILES)
    wait_until(lambda: len(target.panes()) >= expected, 15)
    observations["_rerun_ready_at"] = time.monotonic()
    tracker.sweep()
    pane = rerun_pane() or {}
    panes = target.panes()
    sidebars = [item for item in panes if item.get("title") == workspace_spawn.SIDEBAR_PANE_NAME
                and not item.get("exited")]
    observations["rerun"] = {
        "argv": ["tongs-agents", "workspace", "--profile", "f3", "--profile", "f4"],
        "run_from": "a new pane of the workspace session (ZELLIJ set by Zellij)",
        "exited": bool(exited),
        "exit_status": pane.get("exit_status"),
        "seconds": round(time.monotonic() - started, 2),
        "terminal_panes_before": len(before_panes),
        "terminal_panes_after": len(panes),
        "new_pane_titles": sorted(item.get("title", "") for item in panes
                                  if str(item.get("id")) not in before_panes),
        "sidebar_panes": len(sidebars),
        "sessions_before": ["<session>" if name == session else "<other>"
                            for name in before_sessions],
        "sessions_after": ["<session>" if name == session else "<other>"
                           for name in target.sessions()],
        "zellij_servers_before": before_servers,
        "zellij_servers_after": servers(root),
    }
    facts = observations["rerun"]
    if not (facts["exited"] and facts["exit_status"] == 0):
        try:
            stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            stderr = ""
        facts["stderr"] = diagnostic_lines(stderr, root)
        facts["rerun_screen"] = pane_screen(target, "rerun", root)
        facts["sidebar_screen"] = pane_screen(target, workspace_spawn.SIDEBAR_PANE_NAME, root)
    expect(facts["exited"] and facts["exit_status"] == 0,
           f"in-session rerun did not exit 0: {facts['exit_status']}")
    expect(facts["sessions_after"] == ["<session>"] and facts["zellij_servers_after"] == 1,
           "the rerun nested or created another zellij session")
    expect(facts["terminal_panes_after"] == expected, f"rerun left {len(panes)} panes")
    expect(set(RERUN_PROFILES) <= set(facts["new_pane_titles"]), "rerun panes missing")
    expect(facts["sidebar_panes"] == 1, "the rerun added a second sidebar")


def rerun_rows(session, feed, target, observations):
    since = observations.pop("_rerun_ready_at")
    hook_rows(session, feed, target, (HOOKED_PROFILE,), since, observations, "claude_config_dir")
    records = records_by_profile(session)
    control = records.get(CONTROL_PROFILE)
    row = wait_until(lambda: rows_by_launch(feed).get(control.launch_id) if control else None,
                     STATUS_CEILING)
    observations["claude_config_dir"]["control"] = {
        "profile": CONTROL_PROFILE,
        "config_dir_installed": False,
        "row_present": row is not None,
        "source": getattr(row, "source", None),
        "status": getattr(getattr(row, "status", None), "value", None),
        "launch_state": control.state if control else None,
    }
    facts = observations["claude_config_dir"]["control"]
    expect(facts["source"] == LAUNCH_SOURCE and facts["status"] == "discovered",
           "the uninstalled control profile did not stay a hookless launch row")


def agent_reports(io_dir, names, timeout=REPORT_WAIT):
    """Wait for each fake agent's report; it lands after its hooks have run."""
    deadline = time.monotonic() + timeout
    reports = {}
    while True:
        for name in names:
            if name not in reports:
                report = read_json(io_dir / f"agent-{name}.json")
                if report is not None:
                    reports[name] = report
        if len(reports) == len(names) or time.monotonic() >= deadline:
            return reports
        time.sleep(0.1)


def agent_env(io_dir, session, target, observations):
    reports = agent_reports(io_dir, (*FIRST_PROFILES, *RERUN_PROFILES))
    records = records_by_profile(session)
    titles = {pane.get("title"): str(pane.get("id")) for pane in target.panes()}
    facts = {}
    for name in (*FIRST_PROFILES, *RERUN_PROFILES):
        report = reports.get(name, {})
        child = report.get("env", {})
        record = records.get(name)
        facts[name] = {
            "mode": report.get("mode"),
            "launch_id_env_matches": record is not None
            and child.get("TONGS_AGENTS_LAUNCH_ID") == record.launch_id,
            "profile_env_matches": child.get("TONGS_AGENTS_PROFILE") == name,
            "zellij_session_matches": child.get("ZELLIJ_SESSION_NAME") == session,
            "zellij_pane_matches": child.get("ZELLIJ_PANE_ID") == titles.get(name),
            "pid_is_launch_pid": record is not None and report.get("pid") == record.pid,
            "tmux_env_absent": "TMUX" not in child and "TMUX_PANE" not in child,
            "claude_config_dir": None if "CLAUDE_CONFIG_DIR" not in child else (
                "<claude-hooked>" if child["CLAUDE_CONFIG_DIR"].endswith("claude-hooked")
                else "<claude-bare>"),
            "settings_found": report.get("settings_found"),
            "hooks": [[hook["event"], hook["hook_command"], *hook["hook_args"],
                       hook["returncode"]] for hook in report.get("hooks", [])],
        }
    observations["agent_env"] = facts
    for name, item in facts.items():
        expect(item["launch_id_env_matches"] and item["profile_env_matches"]
               and item["zellij_session_matches"] and item["zellij_pane_matches"]
               and item["pid_is_launch_pid"], f"profile {name} did not inherit its identity")
        expect(all(hook[-1] == 0 for hook in item["hooks"]), f"a hook of {name} failed")
    expect(facts[HOOKED_PROFILE]["hooks"] and not facts[CONTROL_PROFILE]["hooks"],
           "hook commands ran for the wrong config dir")


class ProbeSidebar(sidebar.SidebarApp):
    """The real sidebar, recording each focus result it would notify."""

    def __init__(self, *arguments, **options):
        super().__init__(*arguments, **options)
        self.results = []
        self.done = threading.Event()

    def _notify_focus_result(self, repo, result):
        self.results.append(result)
        self.done.set()
        super()._notify_focus_result(repo, result)


async def select_row(app, identity, timeout=15):
    """Highlight the row for ``identity`` and press Enter, as a user would."""
    async with app.run_test(size=(80, 30)) as pilot:
        deadline = time.monotonic() + timeout
        panel = app.agents_panel
        while not any(row.identity == identity for row in panel.shown_sessions()):
            expect(time.monotonic() < deadline, "the row never appeared in the sidebar")
            panel.poll()
            await pilot.pause(0.2)
        table = panel.table
        table.focus()
        table.move_cursor(row=table.get_row_index(identity))
        await pilot.pause()
        app.done.clear()
        await pilot.press("enter")
        while not app.done.is_set():
            expect(time.monotonic() < deadline, "the row selection reported no result")
            await pilot.pause(0.1)
        return app.results[-1]


def row_focus(session, env, target, tracker, io_dir, first_rows, observations):
    before_attach = target.clients()
    second = PtyClient([str(Path(env["PATH"].split(os.pathsep)[0]) / "zellij"), "attach",
                        session], env, env["HOME"], tracker, "client")
    observations.setdefault("_ptys", []).append(second)
    attached = wait_until(lambda: len(target.clients()) == 2, 10)
    expect(attached, "the second client did not attach")
    second_id = next(key for key in target.clients() if key not in before_attach)
    first_id = next(iter(before_attach))
    sidebar_pane = next(str(pane.get("id")) for pane in target.panes()
                        if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME)
    records = records_by_profile(session)
    first = observations["_ptys"][0]
    os.write(second.fd, DOWN_ARROW)
    time.sleep(0.3)
    start = target.clients().get(second_id)
    first_choice = next(name for name in FIRST_PROFILES
                        if f"terminal_{records[name].pane_id}" != start)
    plan = (
        (first_choice, first_rows[first_choice].identity, "hook row (pi fixture)"),
        (HOOKED_PROFILE, None, "hook row (Claude config dir fixture)"),
        (CONTROL_PROFILE, f"launch:{records[CONTROL_PROFILE].launch_id}", "launch row"),
    )
    pane_env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": session, "ZELLIJ_PANE_ID": sidebar_pane,
                "ZELLIJ_SOCKET_DIR": env["ZELLIJ_SOCKET_DIR"]}
    steps = []
    calls = []

    def role(client_id):
        return {first_id: "first", second_id: "second"}.get(client_id, "other")

    def recorder(label):
        def record(*arguments, **options):
            calls.append(label)
            return None
        return record

    with patch.dict(os.environ, pane_env), \
            patch.object(focus, "find_terminal_api", recorder("find_terminal_api")), \
            patch.object(tmux, "_detect_parent_terminal", recorder("parent_terminal")), \
            patch.object(zellij_terminal, "_attached_client", recorder("attached_client")):
        feed = WorkspaceFeed(session, ZellijMultiplexer())
        for index, (name, identity, kind) in enumerate(plan):
            record = records[name]
            if identity is None:
                row = rows_by_launch(feed).get(record.launch_id)
                expect(row is not None, f"profile {name} has no row")
                identity = row.identity
            before = target.clients()
            # The shipped refresh interval: faster polls stack up on zellij 0.44.2
            # and starve its server past its 1 s action timeout (#72).
            app = ProbeSidebar(ZellijMultiplexer(), session, compact=True)
            result = asyncio.run(select_row(app, identity))
            after = target.clients()
            panes = {str(pane.get("id")): pane for pane in target.panes()}
            _, members = overlay_of(list(panes.values()))
            frames = {HOME_PANE_NAME, *(pane.get("title") for pane in members)}
            shown = top_title(first, frames)
            routed = typed_target(second, io_dir, f"rowfocus{index}")
            target.action("focus-pane-id", f"terminal_{sidebar_pane}")
            kept = top_title(first, frames)
            sidebar_focused = target.clients().get(second_id) == f"terminal_{sidebar_pane}"
            steps.append({
                "profile": name,
                "row": kind,
                "state": result.state,
                "backend": result.backend,
                "selection_succeeded": result.selection.succeeded,
                "activation_attempted": result.activation.attempted,
                "activation_detail": result.activation.detail,
                "notification": focus.focus_notification(result, name)[0],
                "second_client_moved_to_target":
                    after.get(second_id) == f"terminal_{record.pane_id}",
                "second_client_was_elsewhere":
                    before.get(second_id) != f"terminal_{record.pane_id}",
                "first_client_unchanged": after.get(first_id) == before.get(first_id),
                "clients_before": {role(key): value for key, value in before.items()},
                "clients_after": {role(key): value for key, value in after.items()},
                "keystroke_routed_to": routed,
                "target_over_home": any(str(pane.get("id")) == record.pane_id
                                        for pane in members),
                "top_frame": shown,
                "sidebar_focused_after": sidebar_focused,
                "top_frame_after_sidebar_focus": kept,
            })
    observations["row_focus"] = {
        "driver": ("SidebarApp run headless in the validator with the sidebar pane's "
                   "ZELLIJ, ZELLIJ_SESSION_NAME, ZELLIJ_PANE_ID and ZELLIJ_SOCKET_DIR; Enter on "
                   "the highlighted row; readback by zellij list-clients and keystrokes "
                   "typed by the second client"),
        "attached_clients": 2,
        "first_client": "workspace creator, sent no input",
        "second_client": "zellij attach, the most recent input client",
        "first_client_rule": (
            "not asserted: first_client_unchanged records what happened; the first client's "
            "screen shows which agent frame was drawn last (top_frame)"),
        "parent_activation_calls": calls,
        "steps": steps,
    }
    expect(not calls, f"focus looked up an emulator for activation: {calls}")
    for step in steps:
        expect(step["selection_succeeded"] and not step["activation_attempted"]
               and step["activation_detail"] == ACTIVATION_SKIPPED,
               f"row focus for {step['profile']} was not an in-session selection: {step}")
        expect(step["second_client_was_elsewhere"] and step["second_client_moved_to_target"],
               f"list-clients readback did not confirm {step['profile']}")
        expect(step["keystroke_routed_to"] == [step["profile"]],
               f"keystrokes did not reach {step['profile']}: {step['keystroke_routed_to']}")
        expect(step["target_over_home"] and step["top_frame"] == step["profile"],
               f"selecting {step['profile']} did not raise it over the home pane: {step}")
        expect(step["sidebar_focused_after"]
               and step["top_frame_after_sidebar_focus"] == step["profile"],
               f"{step['profile']} was not kept shown with the sidebar focused: {step}")


def cleanup(root, sessions, tracker):
    root = Path(root)
    env = isolated_env(root)
    binary = str(root / "bin" / "zellij")
    if os.path.lexists(binary):
        for name in sessions:
            run([binary, "kill-session", name], env=env)
            run([binary, "delete-session", "--force", name], env=env)
    result = tracker.terminate()
    shutil.rmtree(root, ignore_errors=True)
    result["temp_dir_removed"] = not root.exists()
    return result


def public_text(evidence, redact):
    text = redact(json.dumps(evidence, indent=2) + "\n")
    leaks = Redactor.for_host().leaks(text)
    if "/home/" in text or "/tmp/" in text or "/run/user/" in text:
        leaks.append("absolute path")
    return text, leaks


def private_base():
    """A short base dir: zellij refuses socket paths over 107 bytes."""
    return "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None


def validate(write=True, output=EVIDENCE):
    zellij_binary = shutil.which("zellij")
    if sys.platform != "linux" or not zellij_binary:
        raise RuntimeError("This check requires Linux and zellij on PATH")
    token = secrets.token_hex(4)
    session = f"tongs-ws-{token}"
    real_home = Path.home()
    config_before = config_snapshot()
    agents_before = agent_snapshot(real_home)
    root = Path(tempfile.mkdtemp(prefix=f"tongs-ws-{token}-", dir=private_base()))
    tracker = Tracker([str(root), session])
    observations = {}
    started = time.monotonic()
    try:
        write_fixture(root, zellij_binary)
        exercise(root, session, tracker, observations)
        error = None
    except Exception as failure:
        where = traceback.extract_tb(failure.__traceback__)[-1]
        error = (f"{type(failure).__name__}: {failure} "
                 f"({os.path.basename(where.filename)}:{where.lineno})")
    finally:
        cleanup_result = cleanup(root, (session,), tracker)
    for key in [key for key in observations if key.startswith("_")]:
        del observations[key]
    cleanup_result["marker_strays_after_run"] = len(Tracker([str(root), token]).stray())
    healthy_cleanup = (cleanup_result["survivors"] == 0 and cleanup_result["marker_strays"] == 0
                       and cleanup_result["marker_strays_after_run"] == 0
                       and cleanup_result["temp_dir_removed"])
    evidence = {
        "schema": RUN_SCHEMA,
        "result": "pass" if error is None and healthy_cleanup else "fail",
        "error": error,
        "zellij_version": observations.pop("zellij_version", None),
        "provenance": "instrumented",
        "provenance_detail": (
            "real zellij server and PTY clients, real tongs-agents install, workspace, "
            "sidebar, launcher and hook in a private root; fake harness fixtures stand in for "
            "agents and send hook events; row selection runs the real SidebarApp headless "
            "with the sidebar pane's identity and recorded parent-terminal lookups"),
        "fixtures": {
            "f1": "pi fixture, fleet-event through tongs-agents-hook",
            "f2": "pi fixture, fleet-event through tongs-agents-hook",
            HOOKED_PROFILE: "Claude fixture, runs the hooks installed in its CLAUDE_CONFIG_DIR",
            CONTROL_PROFILE: "Claude fixture, CLAUDE_CONFIG_DIR without installed hooks",
            "claude, codex and pi on PATH": "stub CLI that records its arguments",
        },
        **observations,
        "cleanup": cleanup_result,
        "user_config_unchanged": config_snapshot() == config_before,
        "user_agent_config_unchanged": agent_snapshot(real_home) == agents_before,
        "user_zellij_cache_entries_for_run": zellij_cache_mentions(token),
        "kwin_calls": 0,
        "duration_seconds": round(time.monotonic() - started, 1),
        "platform": platform_facts(),
        **repo_facts(),
    }
    if (not evidence["user_config_unchanged"] or not evidence["user_agent_config_unchanged"]
            or evidence["user_zellij_cache_entries_for_run"]):
        evidence["result"] = "fail"
    redact = Redactor.for_host({str(root): "<tmp>", session: "<session>", token: "<token>",
                                str(REPO_ROOT): "<repo>", sys.executable: "<python>"})
    text, leaks = public_text(evidence, redact)
    if leaks:
        raise RuntimeError(f"refusing to print evidence with private data: {leaks}")
    run_evidence = json.loads(text)
    if write:
        write_evidence(Path(output), run_evidence)
    print(text, end="")
    return run_evidence


def version_key(evidence):
    reported = evidence.get("zellij_version") or ""
    return reported.removeprefix("zellij ").strip() or "unknown"


def write_evidence(path, run_evidence):
    """Replace this zellij version's run in the evidence file, keeping the others."""
    current = read_json(path)
    runs = current.get("runs", {}) if isinstance(current, dict) and current.get(
        "schema") == SCHEMA else {}
    runs[version_key(run_evidence)] = run_evidence
    document = {
        "schema": SCHEMA,
        "provenance": "instrumented",
        "runs": dict(sorted(runs.items(),
                            key=lambda item: tuple(int(part) for part in item[0].split(".")
                                                   if part.isdigit()))),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-write", action="store_true", help="print evidence only")
    parser.add_argument("--out", default=str(EVIDENCE))
    arguments = parser.parse_args(argv)
    evidence = validate(write=not arguments.no_write, output=arguments.out)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
