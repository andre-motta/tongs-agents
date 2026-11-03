"""Validate the tmux workspace backend against isolated live tmux servers.

Linux helper, run locally and by the hosted tmux-mux job (tmux from apt). It
reuses the workspace and send validators' isolation: a fresh temporary root
holds HOME, the XDG config, state, cache, data and runtime dirs, TMPDIR and
TMUX_TMPDIR; claude, codex and pi on the private PATH are a stub; only
processes this run started (PID plus kernel start time) are signalled; the
evidence is redacted before it is printed or written.

Server A is the run's own ``-L tongs-<token>`` socket under the private
TMUX_TMPDIR. TmuxMultiplexer creates a detached session there with the
sidebar as its first pane, records the ``single`` layout (#124) and spawns
fake REPL profiles (validation/fixtures/fake_repl.py) through
``workspace_spawn`` and the real launcher. Checked against it:

- spawn: a new detached session whose first pane is the live sidebar, and
  launch records claimed with the TMUX_PANE pane id and server socket;
- swap: each agent is parked in its own window and the last one shown in
  the sidebar's window; showing a parked agent swaps it into the main slot
  (its pane id moves with it), a parked agent can still be captured, and an
  empty main slot is filled with ``join-pane`` beside a sidebar about 40
  columns wide;
- list: list_panes agrees with raw ``tmux list-panes`` and names every pane;
- focus readback: selection is read back with ``display-message`` and no
  client is attached, raised or created;
- ``tongs-agents workspace --multiplexer tmux`` run inside a pane adds its
  profile to the current session and never nests (no new session, no
  client);
- ``tongs-agents send`` of a multi-line prompt goes through control.send and
  gives exactly one submitted prompt with the exact text in one bracketed
  paste; ``read`` returns the tail; an unclaimed launch is refused (exit 5);
- the control audit log holds no sent text.

Server B is tmux's default socket under the private TMUX_TMPDIR. Outside
tmux, ``workspace --print-command`` creates the detached session (layout
``single``) with the sidebar and a profile shown beside it and prints the
attach command, and a second run in a
private PTY execs ``tmux attach-session`` (the client pid is the command's).
Both servers are killed by their socket path at the end.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

from probe_terminals import (
    PtyClient,
    Redactor,
    Tracker,
    config_snapshot,
    platform_facts,
    run,
    wait_until,
)
from send import FAKE_REPL, Cli, prompts, settled_prompts
from workspace import (
    FAKE_CLI,
    FAKE_CLIS,
    agent_snapshot,
    check_isolation,
    entry_point,
    isolated_env,
    private_base,
    public_text,
)

from tongs_agents import control, launches, paths, workspace_spawn
from tongs_agents.multiplexers import LAYOUT_SINGLE, PaneRef
from tongs_agents.multiplexers.tmux import (
    LAYOUT_OPTION,
    NAME_OPTION,
    SIDEBAR_COLUMNS,
    TmuxMultiplexer,
)
from tongs_agents.profiles import Profile, load_profiles
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "validation" / "evidence" / "tmux-workspace-linux-x86_64.json"
SCHEMA = "tmux-workspace-validation/1"
RUN_SCHEMA = "tmux-workspace-validation-run/1"
HASHED = (
    "src/tongs_agents/commands/sidebar.py",
    "src/tongs_agents/commands/workspace.py",
    "src/tongs_agents/control.py",
    "src/tongs_agents/focus.py",
    "src/tongs_agents/launch.py",
    "src/tongs_agents/launches.py",
    "src/tongs_agents/multiplexers/base.py",
    "src/tongs_agents/multiplexers/tmux.py",
    "src/tongs_agents/terminal_apis/tmux.py",
    "src/tongs_agents/workspace_spawn.py",
    "validation/fixtures/fake_repl.py",
    "validation/tmux_workspace.py",
)
PRIVATE_DIRS = ("home", "bin", "xdg-config", "xdg-state", "xdg-cache", "xdg-data", "xdg-run",
                "tmp", "io", "work", "tmux-tmp")
SPAWNED = ("r1", "r2")
IN_SESSION = "r3"
OUTSIDE = "r4"
PROFILES = (*SPAWNED, IN_SESSION, OUTSIDE)
SEND_DELAY_MS = 150
SIDEBAR_ALIVE_SECONDS = 2.0
WINDOW_COLUMNS = 160
WINDOW_ROWS = 40
IDLE_PANE = "import sys, time; time.sleep(600)"
CLI_TIMEOUT = 60
VERSION_RE = re.compile(r"[0-9]+")


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
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(directory)]) == \
            os.path.realpath(directory)
    except ValueError:
        return False


def socket_dir(root):
    return os.path.realpath(Path(root) / "tmux-tmp" / f"tmux-{os.getuid()}")


def tmux_env(root):
    """The isolated env plus a private TMUX_TMPDIR, checked before it is used."""
    root = Path(root)
    env = isolated_env(root)
    check_isolation(env, root)
    env["TMUX_TMPDIR"] = str(root / "tmux-tmp")
    expect(_inside(env["TMUX_TMPDIR"], root), "TMUX_TMPDIR is outside the private root")
    expect(not os.path.realpath(env["TMUX_TMPDIR"]).startswith(
        os.path.realpath(tempfile.gettempdir()) + os.sep + "tmux-"),
        "TMUX_TMPDIR is the default tmux socket dir")
    return env


def write_bin(root, tmux_binary):
    directory = Path(root) / "bin"
    links = {"tmux": os.path.realpath(tmux_binary)}
    for name in ("tongs-agents", "tongs-agents-hook"):
        links[name] = entry_point(name)
    for name in FAKE_CLIS:
        links[name] = str(FAKE_CLI)
    for name, target in links.items():
        os.symlink(target, directory / name)


def write_config(root, python=None):
    root = Path(root)
    python = sys.executable if python is None else python
    lines = []
    for name in PROFILES:
        cwd = root / "work" / name
        cwd.mkdir(parents=True, exist_ok=True)
        command = [python, str(FAKE_REPL), str(root / "io")]
        lines += [f"[profiles.{name}]", f"command = {json.dumps(command)}",
                  'harness = "other"', f"cwd = {json.dumps(str(cwd))}", ""]
    lines += ["[control]", f"send_delay_ms = {SEND_DELAY_MS}", ""]
    directory = root / "xdg-config" / "tongs-agents"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_fixture(root, tmux_binary):
    root = Path(root)
    for name in PRIVATE_DIRS:
        (root / name).mkdir(mode=0o700)
    Path(socket_dir(root)).mkdir(mode=0o700)
    write_bin(root, tmux_binary)
    write_config(root)


class Server:
    """Raw tmux commands against one server socket of this run."""

    def __init__(self, socket, env, binary, root):
        expect(_inside(socket, root), "a tmux socket is outside the private root")
        self.socket = socket
        self.env = env
        self.binary = binary

    def __call__(self, *arguments):
        return run([self.binary, "-S", self.socket, *arguments], env=self.env)

    def sessions(self):
        code, out, _ = self("list-sessions", "-F", "#{session_name}")
        return out.splitlines() if code == 0 else []

    def clients(self):
        code, out, _ = self("list-clients", "-F", "#{client_pid}\t#{session_name}")
        return [line.split("\t") for line in out.splitlines()] if code == 0 else []

    def pane_ids(self, session):
        code, out, _ = self("list-panes", "-s", "-t", f"={session}", "-F", "#{pane_id}")
        return sorted(out.split()) if code == 0 else []

    def names(self, session):
        code, out, _ = self("list-panes", "-s", "-t", f"={session}", "-F",
                            "#{" + NAME_OPTION + "}")
        return out.split() if code == 0 else []

    def active(self, session):
        _, out, _ = self("display-message", "-p", "-t", f"={session}:", "#{pane_id}")
        return out

    def windows(self, session):
        """``{pane id: window id}`` for every pane of the session."""
        code, out, _ = self("list-panes", "-s", "-t", f"={session}", "-F",
                            "#{pane_id} #{window_id}")
        return dict(line.split() for line in out.splitlines()) if code == 0 else {}

    def window_name(self, window):
        _, out, _ = self("display-message", "-p", "-t", window, "#{window_name}")
        return out

    def width(self, pane):
        _, out, _ = self("display-message", "-p", "-t", pane, "#{pane_width}")
        return int(out) if out.isdigit() else None

    def layout(self, session):
        _, out, _ = self("show-options", "-q", "-v", "-t", f"={session}:", LAYOUT_OPTION)
        return out

    def kill(self):
        self("kill-server")
        return wait_until(lambda: self("list-sessions")[0] != 0, 5, 0.1)


def claimed(records, io_dir, timeout=20):
    def ready():
        current = {record.profile: launches.get(record.launch_id) for record in records}
        if all((Path(io_dir) / f"repl-{name}.ready").exists() for name in current) and all(
                item and item.state == "running" for item in current.values()):
            return current
        return None
    return wait_until(ready, timeout)


def exercise(root, session, outside_session, label, tracker, observations):
    root = Path(root)
    env = tmux_env(root)
    binary = str(root / "bin" / "tmux")
    io_dir = root / "io"
    code, version, _ = run([binary, "-V"], env=env)
    expect(code == 0 and version, "tmux -V failed")
    observations["tmux_version"] = version
    server = Server(os.path.join(socket_dir(root), label), env, binary, root)
    cli = Cli(str(root / "bin" / "tongs-agents"), env, str(root / "work"))
    sent = []
    with patch.dict(os.environ, env, clear=True):
        mux = TmuxMultiplexer(socket_dir=server.socket)
        available, detail = mux.available()
        observations["available"] = {"ok": available, "detail": detail,
                                     "floor": "3.2", "version_read_live": mux.version is not None}
        expect(available, f"tmux is not available: {detail}")
        records = spawn(mux, server, session, io_dir, tracker, observations)
        swap(mux, server, session, records, root, tracker, observations)
        listing(mux, server, session, records, observations)
        focus(mux, server, session, records, observations)
        in_session(cli, server, session, io_dir, tracker, observations)
        multi_line(cli, records["r1"].launch_id, io_dir, session, sent, observations)
        read_tail(cli, records["r1"].launch_id, sent, observations)
        refused(cli, session, io_dir, sent, observations)
        audit(sent, observations)
    outside(root, env, binary, outside_session, cli, io_dir, tracker, observations)


def spawn(mux, server, session, io_dir, tracker, observations):
    before = server.sessions()
    started = time.monotonic()
    sidebar = mux.spawn(session, workspace_spawn.sidebar_argv(), name=SIDEBAR_PANE_NAME)
    tracker.sweep()
    server("resize-window", "-t", f"={session}:", "-x", str(WINDOW_COLUMNS),
           "-y", str(WINDOW_ROWS))
    mux.set_layout(session, LAYOUT_SINGLE)
    profiles = load_profiles()
    created = workspace_spawn.spawn_profiles(mux, session, [profiles[name] for name in SPAWNED])
    tracker.sweep()
    records = claimed(created, io_dir)
    time.sleep(SIDEBAR_ALIVE_SECONDS)
    sidebar_alive = sidebar.pane_id in server.pane_ids(session)
    observations["spawn"] = {
        "session_absent_before": session not in before,
        "sessions_after": len(server.sessions()),
        "sidebar_first_pane": sidebar.pane_id == "%0",
        "sidebar_alive_after_seconds": SIDEBAR_ALIVE_SECONDS if sidebar_alive else None,
        "profiles": list(SPAWNED),
        "claimed": bool(records),
        "seconds": round(time.monotonic() - started, 2),
        "pane_ids": {name: records[name].pane_id for name in SPAWNED} if records else None,
        "socket_recorded": bool(records) and all(
            os.path.normpath(records[name].socket_dir or "") == os.path.normpath(server.socket)
            for name in SPAWNED),
        "session_recorded": bool(records) and all(
            records[name].session == session for name in SPAWNED),
        "layout": server.layout(session),
    }
    facts = observations["spawn"]
    expect(facts["session_absent_before"] and facts["sessions_after"] == 1,
           "the session was not created as the only session on the server")
    expect(facts["sidebar_first_pane"] and sidebar_alive,
           "the sidebar is not the live first pane")
    expect(records, "the fake REPL panes were not claimed and ready")
    expect(facts["socket_recorded"] and facts["session_recorded"],
           "the launch records do not name the session and socket")
    expect(facts["layout"] == LAYOUT_SINGLE, "the session does not record the single layout")
    return records


def swap(mux, server, session, records, root, tracker, observations):
    """Parked agents, show by swap-pane and join-pane, capture of a parked pane."""
    sidebar = "%0"
    first, last = (records[name].pane_id for name in SPAWNED)
    windows = server.windows(session)
    main = windows.get(sidebar)
    spawned = {
        "main_window_panes": sorted(pane for pane, window in windows.items() if window == main),
        "first_parked_alone": windows.get(first) != main and list(windows.values()).count(
            windows.get(first)) == 1,
        "last_shown_and_active": windows.get(last) == main and server.active(session) == last,
    }
    parked_window = windows.get(first)
    shown = mux.show(PaneRef("tmux", session, first, server.socket))
    windows = server.windows(session)
    swapped = {
        "detail": shown.selection.detail,
        "moved_into_main": windows.get(first) == main,
        "pane_id_kept": first in windows,
        "previous_parked_in_its_window": windows.get(last) == parked_window,
        "parked_window_renamed": server.window_name(parked_window) == SPAWNED[1],
        "active_pane": server.active(session) == first,
        "attached_clients": len(server.clients()),
    }
    capture = mux.capture(PaneRef("tmux", session, last, server.socket), 20)
    swapped["parked_capture_has_prompt"] = "fake-repl>" in capture
    spare = mux.spawn(session, [sys.executable, "-c", IDLE_PANE, str(root)], name="spare")
    tracker.sweep()
    mux.show(spare)
    server("kill-pane", "-t", spare.pane_id)
    emptied = sorted(pane for pane, window in server.windows(session).items() if window == main)
    joined = mux.show(PaneRef("tmux", session, first, server.socket))
    windows = server.windows(session)
    rejoined = {
        "main_slot_emptied": emptied == [sidebar],
        "detail": joined.selection.detail,
        "joined_into_main": windows.get(first) == main,
        "sidebar_columns": server.width(sidebar),
        "active_pane": server.active(session) == first,
    }
    observations["swap"] = {"after_spawn": spawned, "show_parked": swapped,
                            "show_into_empty_slot": rejoined}
    expect(spawned["main_window_panes"] == sorted([sidebar, last])
           and spawned["first_parked_alone"] and spawned["last_shown_and_active"],
           f"spawned agents were not parked and shown: {spawned}")
    expect(shown.selection.succeeded and swapped["moved_into_main"]
           and swapped["pane_id_kept"] and swapped["previous_parked_in_its_window"]
           and swapped["active_pane"], f"show did not swap the parked agent in: {swapped}")
    expect(swapped["attached_clients"] == 0, "show attached a client")
    expect(swapped["parked_capture_has_prompt"], "a parked agent could not be captured")
    expect(rejoined["main_slot_emptied"] and joined.selection.succeeded
           and rejoined["joined_into_main"] and rejoined["active_pane"],
           f"show did not join the agent into the empty main slot: {rejoined}")
    expect(rejoined["sidebar_columns"] == SIDEBAR_COLUMNS,
           f"the sidebar is {rejoined['sidebar_columns']} columns wide after join-pane")


def listing(mux, server, session, records, observations):
    panes = mux.list_panes(session)
    titles = {pane.title: pane for pane in panes}
    raw = server.pane_ids(session)
    observations["list"] = {
        "panes": len(panes),
        "titles": sorted(titles),
        "matches_raw_list_panes": sorted(pane.ref.pane_id for pane in panes) == raw,
        "profile_panes_match_records": all(
            titles.get(name) is not None and titles[name].ref.pane_id == records[name].pane_id
            for name in SPAWNED),
        "none_exited": not any(pane.exited for pane in panes),
    }
    facts = observations["list"]
    expect(facts["titles"] == sorted([SIDEBAR_PANE_NAME, *SPAWNED]),
           f"unexpected panes {facts['titles']}")
    expect(facts["matches_raw_list_panes"] and facts["profile_panes_match_records"]
           and facts["none_exited"], "list_panes disagrees with tmux or the records")


def focus(mux, server, session, records, observations):
    steps = []
    for name in ("r2", "r1", "r1"):
        pane = records[name].pane_id
        before = server.active(session)
        result = mux.focus(PaneRef("tmux", session, pane, server.socket))
        steps.append({
            "profile": name,
            "was_active": before == pane,
            "selection_succeeded": result.selection.succeeded,
            "selection_detail": result.selection.detail,
            "activation_attempted": result.activation.attempted,
            "readback_active_pane_matches": server.active(session) == pane,
        })
    observations["focus"] = {
        "steps": steps,
        "attached_clients": len(server.clients()),
        "sessions_after": len(server.sessions()),
    }
    for step in steps:
        expect(step["selection_succeeded"] and step["readback_active_pane_matches"],
               f"focus readback failed for {step['profile']}: {step}")
        expect(not step["activation_attempted"], "focus tried to activate a window")
    expect(steps[2]["was_active"] and steps[2]["selection_detail"] == "already the active pane",
           "a second focus of the active pane was not recognized")
    expect(observations["focus"]["attached_clients"] == 0, "focus attached a client")


def in_session(cli, server, session, io_dir, tracker, observations):
    out = io_dir / "in-session.out"
    status = io_dir / "in-session.code"
    script = '"$0" workspace --multiplexer tmux --profile "$1" >"$2" 2>&1; echo $? >"$3.tmp"' \
             ' && mv "$3.tmp" "$3"'
    before = set(server.pane_ids(session))
    code, _, err = server("new-window", "-d", "-t", f"={session}:", "--", "sh", "-c", script,
                          cli.binary, IN_SESSION, str(out), str(status))
    expect(code == 0, f"could not start the in-session command: {err[-200:]}")
    finished = wait_until(lambda: status.exists(), 30)
    tracker.sweep()
    exit_code = int(status.read_text().strip()) if finished else None
    pending = [record for record in launches.list_records(session)
               if record.profile == IN_SESSION]
    records = claimed(pending, io_dir) if pending else None
    after = set(server.pane_ids(session))
    new = after - before
    observations["in_session"] = {
        "argv": ["tongs-agents", "workspace", "--multiplexer", "tmux", "--profile", IN_SESSION],
        "ran_inside_a_pane_with_tmux_set": True,
        "exit_code": exit_code,
        "output": out.read_text().strip()[:200] if out.exists() else None,
        "sessions_after": len(server.sessions()),
        "attached_clients": len(server.clients()),
        "profile_pane_added": bool(records) and records[IN_SESSION].pane_id in new,
        "sidebar_panes": server.names(session).count(SIDEBAR_PANE_NAME),
    }
    facts = observations["in_session"]
    expect(exit_code == 0, f"the in-session workspace exited {exit_code}: {facts['output']}")
    expect(facts["sessions_after"] == 1, "the in-session workspace created another session")
    expect(facts["attached_clients"] == 0, "the in-session workspace attached a client (nested)")
    expect(facts["profile_pane_added"], "the in-session profile pane was not added and claimed")
    expect(facts["sidebar_panes"] == 1, "the in-session workspace added a second sidebar")


def multi_line(cli, launch_id, io_dir, session, sent, observations):
    token = secrets.token_hex(4)
    text = f"multi {token} line one\nline two\n\tline three"
    sent.append(text)
    code, _, err = cli("send", launch_id, "-", stdin=text + "\n")
    entries = settled_prompts(io_dir, "r1", 1)
    observations["multi_line"] = {
        "argv": ["tongs-agents", "send", "<r1>", "-"],
        "exit_code": code,
        "sent_lines": 3,
        "submitted_prompts": len(entries),
        "text_intact": bool(entries) and entries[0]["text"] == text,
        "pastes_in_prompt": entries[0]["pastes"] if entries else None,
        "received_lines": entries[0]["lines"] if entries else None,
    }
    facts = observations["multi_line"]
    expect(code == 0, f"send exited {code}: {err[-200:]}")
    expect(facts["submitted_prompts"] == 1, f"{len(entries)} prompts submitted, expected 1")
    expect(facts["text_intact"] and facts["pastes_in_prompt"] == 1,
           "the multi-line prompt did not arrive as one bracketed paste")


def read_tail(cli, launch_id, sent, observations):
    code, out, err = cli("read", launch_id, "--lines", "5")
    rows = out.rstrip("\n").splitlines()
    first_line = sent[0].split("\n", 1)[0]
    observations["read"] = {
        "argv": ["tongs-agents", "read", "<r1>", "--lines", "5"],
        "exit_code": code,
        "lines_returned": len(rows),
        "acknowledgement_in_tail": any(f"received #1: {first_line}"[:40] in row for row in rows),
        "last_line_is_prompt": bool(rows) and rows[-1].startswith("fake-repl>"),
    }
    facts = observations["read"]
    expect(code == 0, f"read failed: {err[-200:]}")
    expect(0 < facts["lines_returned"] <= 5, f"read returned {len(rows)} lines")
    expect(facts["acknowledgement_in_tail"] and facts["last_line_is_prompt"],
           "read did not return the pane's tail")


def refused(cli, session, io_dir, sent, observations):
    pending = launches.create_pending(Profile("r2", "other", ("unused",)), session, "tmux")
    before = len(prompts(io_dir, "r2"))
    text = f"must not arrive {secrets.token_hex(4)}"
    sent.append(text)
    send_code, _, _ = cli("send", pending.launch_id, text)
    read_code, _, _ = cli("read", pending.launch_id)
    time.sleep(1.0)
    launches.discard(pending.launch_id)
    observations["refused"] = {
        "case": "unclaimed launch record in the session",
        "send_exit_code": send_code,
        "read_exit_code": read_code,
        "prompts_delivered": len(prompts(io_dir, "r2")) - before,
    }
    expect(send_code == read_code == control.EXIT_REFUSED,
           f"the unclaimed launch was not refused: {send_code} {read_code}")
    expect(observations["refused"]["prompts_delivered"] == 0, "a refused send was delivered")


def audit(sent, observations):
    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    raw = path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.splitlines()]
    sends = [line for line in lines if line["action"] == "send"]
    leaked = [text for text in sent for part in text.split("\n") if part and part in raw]
    observations["audit"] = {
        "mode": oct(path.stat().st_mode & 0o777),
        "sends_ok": sum(1 for line in sends if line["outcome"] == "ok"),
        "sends_refused": sum(1 for line in sends if line["outcome"] == "refused"),
        "sent_text_found": bool(leaked),
    }
    facts = observations["audit"]
    expect(facts["mode"] == "0o600", "the audit log is not 0600")
    expect(facts["sends_ok"] == 1 and facts["sends_refused"] == 1,
           f"unexpected audited sends {facts}")
    expect(not facts["sent_text_found"], "sent text appears in the audit log")


def outside(root, env, binary, session, cli, io_dir, tracker, observations):
    socket = os.path.join(socket_dir(root), "default")
    server = Server(socket, env, binary, root)
    code, out, err = cli("workspace", "--multiplexer", "tmux", "--session", session,
                         "--profile", OUTSIDE, "--print-command")
    tracker.sweep()
    with patch.dict(os.environ, env, clear=True):
        pending = [record for record in launches.list_records(session)]
        records = claimed(pending, io_dir) if pending else None
    expected = shlex.join([binary.rsplit("/", 1)[-1], "-S", socket, "attach-session", "-t",
                           f"={session}"])
    panes_before_attach = server.pane_ids(session)
    client = PtyClient([cli.binary, "workspace", "--multiplexer", "tmux", "--session", session],
                       env, str(root / "work"), tracker, "attach client")
    clients = wait_until(lambda: server.clients(), 10) or []
    tracker.sweep()
    observations["outside"] = {
        "argv": ["tongs-agents", "workspace", "--multiplexer", "tmux", "--session", "<session2>",
                 "--profile", OUTSIDE, "--print-command"],
        "socket": "tmux default socket under the private TMUX_TMPDIR",
        "exit_code": code,
        "printed_attach_command": out.strip() == expected,
        "notice": err.strip().splitlines()[-1][:160] if err.strip() else None,
        "sessions": len(server.sessions()),
        "panes": len(panes_before_attach),
        "profile_claimed": bool(records),
        "pane_names": sorted(server.names(session)),
        "layout": server.layout(session),
        "profile_shown_beside_sidebar": len(set(server.windows(session).values())) == 1,
        "exec_attach_clients": len(clients),
        "exec_attach_client_is_the_command": [str(client.pid), session] in clients,
        "panes_after_attach": len(server.pane_ids(session)),
    }
    facts = observations["outside"]
    expect(code == 0 and facts["printed_attach_command"],
           f"workspace --print-command failed ({code}): {err[-200:]} {out[-200:]}")
    expect(facts["sessions"] == 1 and facts["panes"] == 2
           and facts["pane_names"] == sorted([SIDEBAR_PANE_NAME, OUTSIDE])
           and facts["profile_claimed"], f"the detached session is not sidebar plus profile: {facts}")
    expect(facts["layout"] == LAYOUT_SINGLE and facts["profile_shown_beside_sidebar"],
           f"the new session does not show its profile beside the sidebar: {facts}")
    expect(facts["exec_attach_client_is_the_command"],
           "the workspace command did not exec tmux attach-session")
    expect(facts["panes_after_attach"] == 2, "attaching to a running session added panes")


def cleanup(root, labels, tracker):
    root = Path(root)
    killed = 0
    binary = root / "bin" / "tmux"
    if os.path.lexists(binary):
        env = tmux_env(root)
        for label in labels:
            socket = os.path.join(socket_dir(root), label)
            if os.path.exists(socket):
                Server(socket, env, str(binary), root).kill()
                killed += 1
    result = tracker.terminate()
    result["servers_killed"] = killed
    shutil.rmtree(root, ignore_errors=True)
    result["temp_dir_removed"] = not root.exists()
    return result


def validate(write=True, output=EVIDENCE):
    tmux_binary = shutil.which("tmux")
    if sys.platform != "linux" or not tmux_binary:
        raise RuntimeError("This check requires Linux and tmux on PATH")
    token = secrets.token_hex(4)
    session = f"tongs-tmux-{token}"
    outside_session = f"tongs-tmuxws-{token}"
    label = f"tongs-{token}"
    real_home = Path.home()
    config_before = config_snapshot()
    agents_before = agent_snapshot(real_home)
    root = Path(tempfile.mkdtemp(prefix=f"tongs-tmux-{token}-", dir=private_base()))
    tracker = Tracker([str(root), session, outside_session, label])
    observations = {}
    started = time.monotonic()
    try:
        write_fixture(root, tmux_binary)
        exercise(root, session, outside_session, label, tracker, observations)
        error = None
    except Exception as failure:
        where = traceback.extract_tb(failure.__traceback__)[-1]
        error = (f"{type(failure).__name__}: {failure} "
                 f"({os.path.basename(where.filename)}:{where.lineno})")
    finally:
        cleanup_result = cleanup(root, (label, "default"), tracker)
    cleanup_result["marker_strays_after_run"] = len(Tracker([str(root), token]).stray())
    healthy_cleanup = (cleanup_result["survivors"] == 0 and cleanup_result["marker_strays"] == 0
                       and cleanup_result["marker_strays_after_run"] == 0
                       and cleanup_result["temp_dir_removed"])
    evidence = {
        "schema": RUN_SCHEMA,
        "result": "pass" if error is None and healthy_cleanup else "fail",
        "error": error,
        "tmux_version": observations.pop("tmux_version", None),
        "provenance": "instrumented",
        "provenance_detail": (
            "real tmux servers on private sockets (-L tongs-<token> and the default socket "
            "under a private TMUX_TMPDIR), real TmuxMultiplexer, spawn path, launcher, sidebar "
            "and workspace/send/read CLI processes in a private root; fake REPL fixtures "
            "with bracketed paste stand in for agents and log submitted prompts"),
        "fixtures": {
            **{name: "fake REPL profile (harness other) spawned by tongs-agents"
               for name in PROFILES},
            "claude, codex and pi on PATH": "stub CLI that records its arguments",
        },
        "send_delay_ms": SEND_DELAY_MS,
        **observations,
        "cleanup": cleanup_result,
        "user_config_unchanged": config_snapshot() == config_before,
        "user_agent_config_unchanged": agent_snapshot(real_home) == agents_before,
        "duration_seconds": round(time.monotonic() - started, 1),
        "platform": platform_facts(),
        **repo_facts(),
    }
    if not evidence["user_config_unchanged"] or not evidence["user_agent_config_unchanged"]:
        evidence["result"] = "fail"
    redact = Redactor.for_host({
        str(root): "<tmp>", os.path.realpath(root): "<tmp>", session: "<session>",
        outside_session: "<session2>", token: "<token>", str(REPO_ROOT): "<repo>",
        sys.executable: "<python>"})
    text, leaks = public_text(evidence, redact)
    if leaks:
        raise RuntimeError(f"refusing to print evidence with private data: {leaks}")
    run_evidence = json.loads(text)
    if write:
        write_evidence(Path(output), run_evidence)
    print(text, end="")
    return run_evidence


def version_key(evidence):
    reported = evidence.get("tmux_version") or ""
    return reported.removeprefix("tmux ").strip() or "unknown"


def _sort_key(item):
    return tuple(int(part) for part in VERSION_RE.findall(item[0])), item[0]


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_evidence(path, run_evidence):
    """Replace this tmux version's run in the evidence file, keeping the others."""
    current = read_json(path)
    runs = current.get("runs", {}) if isinstance(current, dict) and current.get(
        "schema") == SCHEMA else {}
    runs[version_key(run_evidence)] = run_evidence
    document = {
        "schema": SCHEMA,
        "provenance": "instrumented",
        "runs": dict(sorted(runs.items(), key=_sort_key)),
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
