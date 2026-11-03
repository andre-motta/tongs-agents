"""Validate read-only web watching (M5-A) against an isolated live Zellij web build.

Local-only Linux x86_64 helper; hosted CI does not run it. It follows
validation/workspace.py, whose fixture it reuses: a fresh temporary root holds
HOME, the XDG dirs, TMPDIR, ZELLIJ_SOCKET_DIR and ZELLIJ_CONFIG_DIR; Zellij
clients run in private PTYs; only processes this run started are signalled;
the evidence is redacted before it is printed or written.

The run, all through the real ``tongs-agents`` CLI unless noted:

1. ``zellij install --web`` installs the pinned web build into the private
   state dir (``--tarball`` feeds a local copy of the pinned asset through the
   same installer and its sha256 checks instead of downloading it);
2. ``web enable`` starts the web server on 127.0.0.1 and prints a read-only
   token once; the validator keeps the token in memory only and refuses to
   print or write evidence that contains it or a session cookie;
3. ``workspace`` creates a session with two fake-agent panes; its server must
   run the web build with web sharing on, while a session started by the PATH
   build and one started by the web build without sharing must refuse web
   clients;
4. a stdlib HTTP and websocket client logs in with the read-only token,
   watches the workspace and types a marker line, which must reach no pane
   (fake-agent input logs plus a capture of every pane); the same check
   passes for a marker typed by the interactive PTY client (positive control);
5. with the read-only websocket still open, the real sidebar (headless, with
   the sidebar pane's identity) focuses each agent row and the interactive
   client must follow (list-clients readback and keystroke routing);
6. ``web tokens revoke`` must close the open websocket, and the revoked
   cookie and token must get 401; revoking with the raw Zellij CLI alone is
   recorded for comparison;
7. the listener must be bound to 127.0.0.1 only, and ``web disable`` must stop
   the server, revoke every token and return new sessions to the PATH build.

No agent or model runs: the agents are the workspace validator's fixtures.
"""

import argparse
import asyncio
import base64
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import socket
import struct
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
    process_cmdline,
    receivers,
    run,
    typed_target,
    wait_until,
    zellij_cache_mentions,
)
import workspace as ws

from tongs_agents import focus, workspace_spawn, zellij_download
from tongs_agents.commands import web
from tongs_agents.multiplexers.base import PaneRef
from tongs_agents.multiplexers.zellij import ACTIVATION_SKIPPED, ZellijMultiplexer
from tongs_agents.multiplexers.zellij_binary import (
    managed_web_link,
    new_session_options,
    zellij_binary,
)
from tongs_agents.sessions_feed import WorkspaceFeed
from tongs_agents.terminal_apis import tmux
from tongs_agents.terminal_apis import zellij as zellij_terminal

REPO_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = REPO_ROOT / "validation" / "evidence" / "zellij-web-linux-x86_64.json"
SCHEMA = "zellij-web-validation/1"
HASHED = (
    "src/tongs_agents/commands/web.py",
    "src/tongs_agents/commands/workspace.py",
    "src/tongs_agents/commands/zellij.py",
    "src/tongs_agents/data/zellij_pins.json",
    "src/tongs_agents/multiplexers/zellij_binary.py",
    "src/tongs_agents/zellij_download.py",
    "validation/workspace.py",
    "validation/zellij_web.py",
)
PROFILES = ws.FIRST_PROFILES
WATCH_SECONDS = 2.0
TYPE_WAIT = 2.0
CLOSE_BUDGET = 10.0
RAW_REVOKE_WAIT = 5.0
NOT_SHARED = "does not allow web connections"
TOKEN_LINE = re.compile(r"^Read-only token (token_[0-9]+), shown once and not stored: (\S+)$",
                        re.MULTILINE)


class Check(Exception):
    """A validation expectation that did not hold."""



_WATCH_URL = re.compile(r"http://127\.0\.0\.1:\d+/[^\s)]+")
_FORWARD = re.compile(r"ssh -L \d+:127\.0\.0\.1:\d+")


def _redact_url(line: str) -> str:
    """Evidence keeps the shape of the watch URL, never its port or session."""
    line = _WATCH_URL.sub("http://127.0.0.1:<port>/<session>", line)
    return _FORWARD.sub("ssh -L <port>:127.0.0.1:<port>", line)

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


class Secrets:
    """Every token and cookie seen in this run, so evidence can be checked for them."""

    def __init__(self):
        self.values = set()

    def add(self, value):
        if value:
            self.values.add(value)
        return value

    def leaks(self, text):
        return [f"secret {index}" for index, value in enumerate(sorted(self.values))
                if value in text]


class Http:
    """A cookie-holding HTTP client for 127.0.0.1 only."""

    def __init__(self, port, vault):
        self.port = port
        self.vault = vault
        self.cookie = None

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body)
            headers["Content-Type"] = "application/json"
        if self.cookie:
            headers["Cookie"] = self.cookie
        try:
            connection.request(method, path, body=data, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            for key, value in response.getheaders():
                if key.lower() == "set-cookie" and value.startswith("session_token="):
                    self.cookie = self.vault.add(value.split(";", 1)[0])
            return response.status, payload
        finally:
            connection.close()

    def login(self, token):
        status, _ = self.request("POST", "/command/login",
                                 {"auth_token": token, "remember_me": False})
        return status

    def json(self, method, path):
        status, payload = self.request(method, path)
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, None


class WebSocket:
    """A minimal RFC 6455 client: masked text frames out, frames counted in."""

    def __init__(self, port, path, cookie):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [f"GET {path} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                 "Sec-WebSocket-Version: 13", f"Origin: http://127.0.0.1:{port}"]
        if cookie:
            lines.append(f"Cookie: {cookie}")
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            head += chunk
        header, _, self.buffer = head.partition(b"\r\n\r\n")
        parts = header.split(b" ", 2)
        self.status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        self.closed = self.status != 101
        self.received = 0
        self.texts = []

    def send(self, data, opcode=1):
        payload = data.encode() if isinstance(data, str) else data
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 65536:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self.sock.sendall(header + mask + masked)
        except OSError:
            self.closed = True

    def _fill(self, need, timeout):
        while len(self.buffer) < need:
            try:
                ready, _, _ = select.select([self.sock], [], [], timeout)
                if not ready:
                    return False
                chunk = self.sock.recv(65536)
            except OSError:
                chunk = b""
            if not chunk:
                self.closed = True
                return False
            self.buffer += chunk
        return True

    def pump(self, timeout=0.3):
        while not self.closed:
            if not self._fill(2, timeout):
                return
            first, second = self.buffer[0], self.buffer[1]
            length, offset = second & 0x7F, 2
            if length == 126:
                if not self._fill(4, timeout):
                    return
                length, offset = struct.unpack(">H", self.buffer[2:4])[0], 4
            elif length == 127:
                if not self._fill(10, timeout):
                    return
                length, offset = struct.unpack(">Q", self.buffer[2:10])[0], 10
            if not self._fill(offset + length, timeout):
                return
            payload = self.buffer[offset:offset + length]
            self.buffer = self.buffer[offset + length:]
            opcode = first & 0x0F
            self.received += length
            if opcode == 1:
                self.texts.append(payload.decode("utf-8", "replace")[:200])
            elif opcode == 8:
                self.closed = True
            elif opcode == 9:
                self.send(payload, 10)

    def watch(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.closed and time.monotonic() < deadline:
            self.pump(0.2)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Viewer:
    """One logged-in web client attached to one session (terminal and control sockets)."""

    def __init__(self, client, session):
        status, boot = client.json("POST", f"/session?session={session}")
        self.bootstrap_status = status
        self.read_only = (boot or {}).get("is_read_only")
        client_id = (boot or {}).get("web_client_id", "")
        self.terminal = WebSocket(
            client.port, f"/ws/terminal/{session}?web_client_id={client_id}&rows=30&cols=100",
            client.cookie)
        self.control = WebSocket(client.port, f"/ws/control?web_client_id={client_id}",
                                 client.cookie)

    def pump(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not (self.terminal.closed and self.control.closed):
            self.terminal.pump(0.1)
            self.control.pump(0.05)

    def facts(self):
        return {
            "bootstrap_status": self.bootstrap_status,
            "is_read_only": self.read_only,
            "terminal_upgrade": self.terminal.status,
            "control_upgrade": self.control.status,
            "terminal_bytes": self.terminal.received,
            "terminal_closed": self.terminal.closed,
            "control_messages": [text for text in self.control.texts if "LogError" in text][:2],
        }

    def close(self):
        self.terminal.close()
        self.control.close()


class TarballOpener:
    """Serve a local copy of the pinned asset to the real installer."""

    def __init__(self, path):
        self.path = path

    def __call__(self, url, timeout):
        data = Path(self.path).read_bytes()
        response = io.BytesIO(data)
        response.headers = {"Content-Length": str(len(data))}
        response.geturl = lambda: url
        return response


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def listeners(port):
    """Local addresses listening on ``port`` (IPv4 and IPv6), from /proc/net."""
    found = []
    for name, width in (("tcp", 8), ("tcp6", 32)):
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            address, _, hex_port = fields[1].partition(":")
            if int(hex_port, 16) != port:
                continue
            if width == 8:
                found.append(socket.inet_ntop(socket.AF_INET, bytes.fromhex(address)[::-1]))
            else:
                raw = b"".join(bytes.fromhex(address[index:index + 8])[::-1]
                               for index in range(0, 32, 8))
                found.append(socket.inet_ntop(socket.AF_INET6, raw))
    return sorted(found)


def session_server_exe(root, session):
    """realpath of the server process for ``session`` in the private socket dir."""
    sock = str(Path(root) / "sock")
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        command = process_cmdline(int(entry))
        if "--server" in command and sock in command and command.rstrip().endswith(
                f"/{session}"):
            try:
                return os.path.realpath(f"/proc/{entry}/exe")
            except OSError:
                return None
    return None


def tongs(env, *arguments, timeout=90):
    tongs_bin = str(Path(env["PATH"].split(os.pathsep)[0]) / "tongs-agents")
    result = subprocess.run([tongs_bin, *arguments], env=env, capture_output=True, text=True,
                            timeout=timeout, stdin=subprocess.DEVNULL, cwd=env["HOME"])
    return result.returncode, result.stdout, result.stderr


def install_web_build(env, tarball, observations):
    if tarball:
        installed = zellij_download.install(flavor=zellij_download.WEB,
                                            opener=TarballOpener(tarball))
        code, source = 0, "local copy of the pinned asset through the real installer"
        binary_sha = installed.binary_sha256
    else:
        code, out, err = tongs(env, "zellij", "install", "--web", timeout=600)
        expect(code == 0, f"zellij install --web failed: {web.redact(err)[-200:]}")
        source = "tongs-agents zellij install --web (downloaded from GitHub)"
        binary_sha = zellij_download.file_sha256(managed_web_link())
    pin = zellij_download.select_pin(flavor=zellij_download.WEB)
    observations["web_build"] = {
        "source": source,
        "exit_code": code,
        "version": pin.version,
        "asset": pin.asset,
        "tarball_sha256_pinned": pin.sha256,
        "binary_sha256": binary_sha,
        "binary_matches_pin": binary_sha == pin.binary_sha256,
    }
    expect(binary_sha == pin.binary_sha256, "the installed web build does not match its pin")


def enable(env, port, vault, label, observations):
    code, out, err = tongs(env, "web", "enable", "--port", str(port))
    match = TOKEN_LINE.search(out)
    token = vault.add(match.group(2)) if match else None
    observations[label] = {
        "argv": ["tongs-agents", "web", "enable", "--port", "<port>"],
        "exit_code": code,
        "stdout_lines": [_redact_url(web.redact(line)) for line in out.splitlines()],
        "stderr_tail": web.redact(err)[-200:],
        "token_printed_once": out.count(token) == 1 if token else False,
        "token_name": match.group(1) if match else None,
    }
    expect(code == 0 and token, f"web enable failed: {web.redact(err)[-200:]}")
    return match.group(1), token


def check_sharing(root, env, session, web_binary, vault, port, token, observations):
    path_build = ws.Session(f"{session}-nw", env, str(Path(root) / "bin" / "zellij"))
    web_build = ws.Session(f"{session}-off", env, web_binary)
    for target in (path_build, web_build):
        code, _, err = run([target.binary, "attach", "--create-background", target.name],
                           env=env)
        expect(code == 0, f"could not start the unshared session: {err[-120:]}")
    client = Http(port, vault)
    facts = {"login_status": client.login(token)}
    listed = {}

    def listing():
        status, payload = client.json("GET", "/session-list")
        listed["status"], listed["payload"] = status, payload
        names = session_names(payload)
        return names if session in names else None

    wait_until(listing, 10, 0.5)
    names = session_names(listed.get("payload"))
    facts["session_list_status"] = listed.get("status")
    facts["session_list_keys"] = sorted(listed["payload"]) if isinstance(
        listed.get("payload"), dict) else None
    facts["sessions_listed"] = len(names)
    facts["workspace_listed"] = session in names
    for key, target in (("no_web_build_session", path_build),
                        ("web_build_session_without_sharing", web_build)):
        viewer = Viewer(client, target.name)
        viewer.pump(WATCH_SECONDS)
        facts[key] = {**viewer.facts(), "listed": target.name in names}
        viewer.close()
    observations["unshared_sessions"] = facts
    for key in ("no_web_build_session", "web_build_session_without_sharing"):
        item = facts[key]
        expect(item["terminal_closed"] and any(NOT_SHARED in text
                                               for text in item["control_messages"]),
               f"{key} was watchable")
    return (path_build.name, web_build.name)


def session_names(payload):
    sessions = payload.get("sessions", []) if isinstance(payload, dict) else []
    return sorted(str((item or {}).get("name", "")) for item in sessions
                  if isinstance(item, dict))


def unauthenticated(port, vault):
    client = Http(port, vault)
    facts = {
        "GET /": client.request("GET", "/")[0],
        "GET /session-list": client.request("GET", "/session-list")[0],
        "POST /session": client.request("POST", "/session")[0],
        "bad_token_login": client.login("00000000-0000-0000-0000-000000000000"),
    }
    socket_client = WebSocket(port, "/ws/terminal", None)
    facts["ws /ws/terminal"] = socket_client.status
    socket_client.close()
    return facts


def capture_all(session, panes):
    mux = ZellijMultiplexer()
    screens = {}
    for pane in panes:
        ref = PaneRef("zellij", session, str(pane["id"]), os.environ["ZELLIJ_SOCKET_DIR"])
        try:
            screens[str(pane["id"])] = mux.capture(ref, lines=400)
        except Exception as error:  # noqa: BLE001 - recorded, the check then fails
            screens[str(pane["id"])] = f"<capture failed: {type(error).__name__}>"
    return screens


def typing_checks(session, target, io_dir, viewer, interactive, observations):
    panes = target.panes()
    clients_before = target.clients()
    marker = f"webro{secrets.token_hex(4)}"
    for chunk in (marker, "\r", "\x1b[B", marker + "\r"):
        viewer.terminal.send(chunk)
        time.sleep(0.1)
    viewer.pump(TYPE_WAIT)
    screens = capture_all(session, panes)
    reached_logs = receivers(io_dir, marker)
    reached_screens = sorted(pane for pane, text in screens.items() if marker in text)
    control = f"ptyrw{secrets.token_hex(4)}"
    routed = typed_target(interactive, io_dir, control)
    control_screens = sorted(pane for pane, text in capture_all(session, panes).items()
                             if control in text)
    observations["read_only_typing"] = {
        "panes_captured": len(screens),
        "captures_failed": sorted(pane for pane, text in screens.items()
                                  if text.startswith("<capture failed")),
        "marker_in_agent_input_logs": reached_logs,
        "marker_in_pane_captures": reached_screens,
        "list_clients_before": sorted(clients_before.values()),
        "list_clients_after": sorted(target.clients().values()),
        "viewer_terminal_open": not viewer.terminal.closed,
        "positive_control": {
            "typed_by": "the interactive PTY client, on an agent pane after the focus step",
            "agent_input_logs": routed,
            "pane_captures": control_screens,
        },
    }
    expect(not reached_logs and not reached_screens,
           "text typed by the read-only web client reached a pane")
    expect(len(screens) == len(panes) and not observations["read_only_typing"]["captures_failed"],
           "a pane could not be captured")
    expect(routed and control_screens,
           "the positive control did not show up in the input logs and captures")


def focus_with_viewer(session, env, target, tracker, io_dir, viewer, observations):
    """The real sidebar moves the interactive client while the web viewer is attached."""
    before_attach = target.clients()
    second = PtyClient([str(Path(env["PATH"].split(os.pathsep)[0]) / "zellij"), "attach",
                        session], env, env["HOME"], tracker, "client")
    expect(wait_until(lambda: len(target.clients()) == len(before_attach) + 1, 10),
           "the interactive client did not attach")
    second_id = next(key for key in target.clients() if key not in before_attach)
    sidebar_pane = next(str(pane.get("id")) for pane in target.panes()
                        if pane.get("title") == workspace_spawn.SIDEBAR_PANE_NAME)
    os.write(second.fd, ws.DOWN_ARROW)
    time.sleep(0.3)
    records = ws.records_by_profile(session)
    start = target.clients().get(second_id)
    order = sorted(PROFILES, key=lambda name: f"terminal_{records[name].pane_id}" == start)
    pane_env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": session, "ZELLIJ_PANE_ID": sidebar_pane,
                "ZELLIJ_SOCKET_DIR": env["ZELLIJ_SOCKET_DIR"]}
    calls = []
    steps = []

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
        for index, name in enumerate(order):
            record = records[name]
            row = wait_until(lambda: ws.rows_by_launch(feed).get(record.launch_id), 10)
            expect(row is not None, f"profile {name} has no row")
            before = target.clients()
            app = ws.ProbeSidebar(ZellijMultiplexer(), session, compact=True)
            result = asyncio.run(ws.select_row(app, row.identity))
            after = target.clients()
            routed = typed_target(second, io_dir, f"webfocus{index}")
            viewer.pump(0.5)
            steps.append({
                "profile": name,
                "state": result.state,
                "selection_succeeded": result.selection.succeeded,
                "activation_attempted": result.activation.attempted,
                "activation_detail": result.activation.detail,
                "interactive_client_was_elsewhere":
                    before.get(second_id) != f"terminal_{record.pane_id}",
                "interactive_client_moved_to_target":
                    after.get(second_id) == f"terminal_{record.pane_id}",
                "keystroke_routed_to": routed,
                "web_viewer_still_open": not viewer.terminal.closed,
            })
    observations["focus_with_web_viewer"] = {
        "driver": ("SidebarApp run headless in the validator with the sidebar pane's Zellij "
                   "identity; Enter on each agent row; readback by zellij list-clients and "
                   "keystrokes typed by the interactive client"),
        "list_clients_with_viewer": len(target.clients()),
        "parent_activation_calls": calls,
        "steps": steps,
    }
    expect(not calls, f"focus looked up an emulator for activation: {calls}")
    for step in steps:
        expect(step["selection_succeeded"] and not step["activation_attempted"]
               and step["activation_detail"] == ACTIVATION_SKIPPED,
               f"row focus for {step['profile']} was not an in-session selection")
        expect(step["interactive_client_was_elsewhere"]
               and step["interactive_client_moved_to_target"],
               f"list-clients did not confirm the move to {step['profile']}")
        expect(step["keystroke_routed_to"] == [step["profile"]],
               f"keystrokes did not reach {step['profile']}")
        expect(step["web_viewer_still_open"], "the web viewer closed during focus")
    return second


def revoke_checks(env, port, vault, session, client, viewer, name, token, observations):
    started = time.monotonic()
    code, out, err = tongs(env, "web", "tokens", "revoke", name)
    closed = wait_until(lambda: (viewer.pump(0.1), viewer.terminal.closed)[1], CLOSE_BUDGET)
    seconds = round(time.monotonic() - started, 2)
    retry = Http(port, vault)
    facts = {
        "argv": ["tongs-agents", "web", "tokens", "revoke", "<token name>"],
        "exit_code": code,
        "stdout_lines": [_redact_url(web.redact(line)) for line in out.splitlines()],
        "open_terminal_websocket_closed": bool(closed),
        "seconds_to_close": seconds,
        "close_budget_seconds": CLOSE_BUDGET,
        "revoked_cookie_session_list": client.request("GET", "/session-list")[0],
        "revoked_cookie_bootstrap": client.request("POST", f"/session?session={session}")[0],
        "revoked_token_login": retry.login(token),
    }
    observations["revoke"] = facts
    expect(code == 0, f"web tokens revoke failed: {web.redact(err)[-200:]}")
    expect(closed, "the open terminal websocket stayed open after the revoke")
    expect(facts["revoked_cookie_session_list"] == 401
           and facts["revoked_cookie_bootstrap"] == 401
           and facts["revoked_token_login"] == 401, "a revoked token or cookie still worked")


def raw_revoke_comparison(env, port, vault, session, web_binary, observations):
    """Revoke with Zellij's own CLI only, to record why tongs-agents restarts the server."""
    name, token = enable(env, port, vault, "enable_again", observations)
    client = Http(port, vault)
    client.login(token)
    viewer = Viewer(client, session)
    viewer.pump(WATCH_SECONDS)
    opened = not viewer.terminal.closed
    raw_env = web.zellij_env(env)
    code, _, _ = run([web_binary, "web", "--revoke-token", name], env=raw_env)
    viewer.pump(RAW_REVOKE_WAIT)
    before = viewer.terminal.received
    viewer.pump(0.5)
    observations["raw_zellij_revoke"] = {
        "argv": ["zellij", "web", "--revoke-token", "<token name>"],
        "exit_code": code,
        "terminal_websocket_open_before": opened,
        "terminal_websocket_still_open_after_seconds": (
            RAW_REVOKE_WAIT if not viewer.terminal.closed else None),
        "bytes_received_while_open": before,
        "revoked_cookie_session_list": client.request("GET", "/session-list")[0],
        "note": ("recorded, not a gate: Zellij 0.45.1 alone left the open websocket of a "
                 "revoked token open, so tongs-agents web tokens revoke restarts the server"),
    }
    viewer.close()


def disable_checks(env, port, observations):
    code, out, err = tongs(env, "web", "disable")
    listed, tokens_out, _ = tongs(env, "web", "tokens", "list")
    stopped = wait_until(lambda: not listeners(port), 5)
    facts = {
        "exit_code": code,
        "stdout_lines": [_redact_url(web.redact(line)) for line in out.splitlines()],
        "port_listening_after": listeners(port),
        "owned_server_processes_after": len(web.owned_server_pids()),
        "tokens_after": tokens_out.strip().splitlines(),
        "zellij_binary_is_web_build": zellij_binary() == str(managed_web_link()),
        "new_session_options": new_session_options(zellij_binary()),
    }
    observations["disable"] = facts
    expect(code == 0 and stopped and not facts["owned_server_processes_after"],
           "web disable left the server running")
    expect(facts["tokens_after"] == ["No tokens."], "web disable left tokens behind")
    expect(not facts["zellij_binary_is_web_build"] and not facts["new_session_options"],
           "the web build is still used after web disable")


def exercise(root, session, tracker, tarball, vault, observations):
    root = Path(root)
    env = ws.isolated_env(root)
    ws.check_isolation(env, root)
    io_dir = root / "io"
    path_zellij = str(root / "bin" / "zellij")
    code, version, _ = run([path_zellij, "--version"], env=env)
    expect(code == 0 and version, "zellij --version failed")
    observations["path_zellij_version"] = version
    with patch.dict(os.environ, env, clear=True):
        install_web_build(env, tarball, observations)
        web_binary = str(managed_web_link())
        observations["before_enable"] = {
            "zellij_binary_is_web_build": zellij_binary() == web_binary,
            "new_session_options": new_session_options(zellij_binary()),
        }
        expect(not observations["before_enable"]["zellij_binary_is_web_build"],
               "the web build was used before web watching was enabled")
        port = free_port()
        name, token = enable(env, port, vault, "enable", observations)
        observations["after_enable"] = {
            "zellij_binary_is_web_build": zellij_binary() == web_binary,
            "new_session_options": new_session_options(zellij_binary()),
            "listening_addresses": listeners(port),
            "config_bind": web.configured_bind(web.config_path().read_text()),
            "tokens_db_mode": oct(os.stat(web.tokens_db()).st_mode & 0o777),
            "web_dir_mode": oct(os.stat(web.web_dir()).st_mode & 0o777),
            "doctor": [(check.name, check.level) for check in web.doctor_checks()],
        }
        after = observations["after_enable"]
        expect(after["zellij_binary_is_web_build"], "web enable did not select the web build")
        expect(after["listening_addresses"] == ["127.0.0.1"],
               f"the web server listens on {after['listening_addresses']}")
        expect(after["tokens_db_mode"] == "0o600", "the token store is not 0600")
        observations["unauthenticated"] = unauthenticated(port, vault)
        expect(all(observations["unauthenticated"][key] == 401
                   for key in ("GET /session-list", "POST /session", "bad_token_login",
                               "ws /ws/terminal")), "an unauthenticated request was accepted")
        target = ws.Session(session, env, path_zellij)
        tongs_bin = str(root / "bin" / "tongs-agents")
        ws.create_workspace(root, session, env, tongs_bin, target, tracker, observations)
        reports = ws.agent_reports(io_dir, PROFILES)
        expect(len(reports) == len(PROFILES), "the fake agents did not start")
        server = session_server_exe(root, session)
        observations["workspace_server_runs_web_build"] = (
            server == os.path.realpath(web_binary))
        expect(observations["workspace_server_runs_web_build"],
               "the workspace session does not run the web build")
        unshared = check_sharing(root, env, session, web_binary, vault, port, token,
                                 observations)
        observations["_sessions"] = unshared
        expect(observations["unshared_sessions"]["workspace_listed"],
               "the workspace session is not listed")
        client = Http(port, vault)
        expect(client.login(token) == 200, "the read-only token could not log in")
        viewer = Viewer(client, session)
        viewer.pump(WATCH_SECONDS)
        observations["read_only_viewer"] = viewer.facts()
        expect(viewer.read_only is True, "the bootstrap did not mark the viewer read-only")
        expect(viewer.terminal.status == 101 and not viewer.terminal.closed
               and viewer.terminal.received > 0, "the read-only viewer received no output")
        second = focus_with_viewer(session, env, target, tracker, io_dir, viewer,
                                   observations)
        typing_checks(session, target, io_dir, viewer, second, observations)
        revoke_checks(env, port, vault, session, client, viewer, name, token, observations)
        viewer.close()
        raw_revoke_comparison(env, port, vault, session, web_binary, observations)
        disable_checks(env, port, observations)


def cleanup(root, sessions, tracker):
    root = Path(root)
    env = ws.isolated_env(root)
    with patch.dict(os.environ, env, clear=True):
        if web.owned_server_pids():
            web.stop_server(str(managed_web_link()) if os.path.lexists(managed_web_link())
                            else None)
    return ws.cleanup(root, sessions, tracker)


def public_text(evidence, redact, vault):
    text, leaks = ws.public_text(evidence, redact)
    leaks += vault.leaks(text)
    return text, leaks


def validate(write=True, output=EVIDENCE, tarball=None):
    zellij_path = shutil.which("zellij")
    if sys.platform != "linux" or not zellij_path or zellij_download.platform_key() is None:
        raise RuntimeError("This check requires Linux x86_64 and zellij on PATH")
    token = secrets.token_hex(4)
    session = f"tongs-web-{token}"
    real_home = Path.home()
    config_before = config_snapshot()
    agents_before = ws.agent_snapshot(real_home)
    root = Path(tempfile.mkdtemp(prefix=f"tongs-web-{token}-", dir=ws.private_base()))
    tracker = Tracker([str(root), session])
    vault = Secrets()
    observations = {}
    started = time.monotonic()
    try:
        ws.write_fixture(root, zellij_path)
        exercise(root, session, tracker, tarball, vault, observations)
        error = None
    except Exception as failure:
        where = traceback.extract_tb(failure.__traceback__)[-1]
        error = (f"{type(failure).__name__}: {web.redact(str(failure))} "
                 f"({os.path.basename(where.filename)}:{where.lineno})")
    finally:
        extra = observations.pop("_sessions", ())
        cleanup_result = cleanup(root, (session, *extra), tracker)
    for key in [key for key in observations if key.startswith("_")]:
        del observations[key]
    cleanup_result["marker_strays_after_run"] = len(Tracker([str(root), token]).stray())
    healthy_cleanup = (cleanup_result["survivors"] == 0 and cleanup_result["marker_strays"] == 0
                       and cleanup_result["marker_strays_after_run"] == 0
                       and cleanup_result["temp_dir_removed"])
    evidence = {
        "schema": SCHEMA,
        "result": "pass" if error is None and healthy_cleanup else "fail",
        "error": error,
        "provenance": "live",
        "provenance_detail": (
            "real pinned Zellij web build, web server, sessions and PTY clients; real "
            "tongs-agents zellij install --web, web enable, workspace, web tokens revoke and "
            "web disable in a private root; a stdlib HTTP and websocket client stands in for "
            "the browser; fake harness fixtures stand in for agents; row selection runs the "
            "real SidebarApp headless with the sidebar pane's identity"),
        "secrets": "tokens and cookies were held in memory only; this file was checked for "
                   "every one of them before it was written",
        **observations,
        "cleanup": cleanup_result,
        "user_config_unchanged": config_snapshot() == config_before,
        "user_agent_config_unchanged": ws.agent_snapshot(real_home) == agents_before,
        "user_zellij_cache_entries_for_run": zellij_cache_mentions(token),
        "not_tested": [
            "a real browser (cookie and Origin behaviour of browsers)",
            "HTTPS with a certificate and any bind address other than 127.0.0.1",
            "whether the web server counts as a unix-socket peer for parent activation",
            "the share plugin toggling sharing at runtime",
            "Zellij 0.44.2 (no web build is pinned for it)",
        ],
        "duration_seconds": round(time.monotonic() - started, 1),
        "platform": platform_facts(),
        **repo_facts(),
    }
    if (not evidence["user_config_unchanged"] or not evidence["user_agent_config_unchanged"]
            or evidence["user_zellij_cache_entries_for_run"]):
        evidence["result"] = "fail"
    redact = Redactor.for_host({str(root): "<tmp>", session: "<session>", token: "<token>",
                                str(REPO_ROOT): "<repo>", sys.executable: "<python>"})
    text, leaks = public_text(evidence, redact, vault)
    if leaks:
        raise RuntimeError(f"refusing to print evidence with private data: {leaks}")
    if write:
        Path(output).write_text(text, encoding="utf-8")
    print(text, end="")
    return json.loads(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-write", action="store_true", help="print evidence only")
    parser.add_argument("--out", default=str(EVIDENCE))
    parser.add_argument("--tarball", default=None,
                        help="a local copy of the pinned web asset; its sha256 is checked by "
                             "the installer (default: download it)")
    arguments = parser.parse_args(argv)
    evidence = validate(write=not arguments.no_write, output=arguments.out,
                        tarball=arguments.tarball)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
