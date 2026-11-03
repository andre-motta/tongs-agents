"""Chat through real MCP stdio servers under fake agent processes (Linux).

Each fake agent is a small Python wrapper process that stands in for the
claimed agent process of a launch record: it starts ``tongs_agents.mcp_server``
as its child, so the server's identity check walks real ``/proc`` ancestry.
Grants are written with the maintainer store, as ``chat.grants.apply`` does
after its pane check. No multiplexer, agent or model is started.
"""

import json
import os
import selectors
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tongs_agents import discovery, launches, paths
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import ChatStore

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads real process ancestry from /proc"
)

CHAT_TOOLS = {"chat_inbox", "chat_read", "chat_post"}
SOURCE = Path(__file__).resolve().parents[1] / "src"
WRAPPER = (
    "import os, subprocess, sys, time\n"
    "go = sys.argv[1]\n"
    "while not os.path.exists(go):\n"
    "    time.sleep(0.02)\n"
    "sys.exit(subprocess.call([sys.executable, '-u', '-m', 'tongs_agents.mcp_server']))\n"
)


class Client:
    """A minimal MCP stdio client that keeps notifications it receives."""

    def __init__(self, process):
        self.process = process
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.buffer = bytearray()
        self.notifications = []
        self.next_id = 1

    def send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        self.process.stdin.flush()

    def _messages(self, timeout):
        for key, _ in self.selector.select(timeout=timeout):
            chunk = os.read(key.fd, 65536)
            if not chunk:
                raise RuntimeError(f"server exited: {self.process.poll()}")
            self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer[:] = rest
            yield json.loads(line)

    def request(self, method, params=None, timeout=15):
        request_id = self.next_id
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params or {}})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in self._messages(0.2):
                if message.get("id") == request_id:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message["result"]
                if "method" in message:
                    self.notifications.append(message["method"])
        raise TimeoutError(method)

    def wait_notification(self, method, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if method in self.notifications:
                self.notifications.remove(method)
                return True
            for message in self._messages(0.2):
                if "method" in message:
                    self.notifications.append(message["method"])
        return False

    def initialize(self):
        result = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "chat-test", "version": "1"},
        })
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tools(self):
        return {tool["name"] for tool in self.request("tools/list")["tools"]}

    def call(self, name, **arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        text = result["content"][0]["text"]
        if result.get("isError"):
            return {"is_error": True, "text": text}
        return json.loads(text)


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    started = []

    def start(profile, claim_pid=None):
        go = tmp_path / f"go-{profile}"
        env = dict(os.environ, PYTHONPATH=str(SOURCE), PYTHONUNBUFFERED="1")
        for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "TMUX", "TMUX_PANE"):
            env.pop(key, None)
        launch_id = os.urandom(16).hex()
        env["TONGS_AGENTS_LAUNCH_ID"] = launch_id
        env["TONGS_AGENTS_PROFILE"] = profile
        process = subprocess.Popen(
            [sys.executable, "-c", WRAPPER, str(go)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0,
        )
        started.append(process)
        info = discovery.get_process_info(claim_pid or process.pid)
        assert info is not None
        launches._write(launches.LaunchRecord(
            launch_id=launch_id, profile=profile, harness="claude", multiplexer="zellij",
            session="agents", created=int(time.time()), pane_id=str(len(started)),
            pid=info.pid, process_start=info.start_token, state="running",
        ))
        go.write_text("")
        client = Client(process)
        client.launch_id = launch_id
        client.start_token = info.start_token
        client.profile = profile
        client.initialize()
        return client

    yield start
    for process in started:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout):
            stream.close()


def grant(client, handle=None):
    with ChatStore.maintainer(create=True) as maintainer:
        maintainer.put_grant(
            launch_id=client.launch_id, handle=handle or client.profile, level="chat",
            process_start=client.start_token, pane_session="agents", pane_id="1",
            socket_dir=None, via="cli", profile=client.profile, harness="claude",
        )


def revoke(client):
    with ChatStore.maintainer() as maintainer:
        maintainer.delete_grant(client.launch_id)


def audit_entries():
    path = paths.state_dir() / "audit" / "chat.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_two_granted_agents_hold_a_thread_and_an_ungranted_one_is_refused(agents):
    lead = agents("lead")
    review = agents("review")
    grant(lead)
    grant(review)
    outsider = agents("outsider")

    assert CHAT_TOOLS <= lead.tools()
    assert CHAT_TOOLS <= review.tools()
    assert not CHAT_TOOLS & outsider.tools()

    posted = lead.call("chat_post", channel="general", body="@review can you check the parser?")
    assert posted["ok"] and posted["mentioned"] == ["review"] and posted["handle"] == "lead"

    inbox = review.call("chat_inbox")
    assert inbox["handle"] == "review"
    assert inbox["notice"].startswith("[tongs-agents chat]")
    assert [m["id"] for m in inbox["mentions"]] == [posted["id"]]
    assert inbox["mentions"][0]["from"] == "lead"

    reply = review.call("chat_post", channel="general", body="@lead looks fine",
                        thread=posted["id"])
    assert reply["ok"] and reply["thread"] == posted["id"]

    thread = lead.call("chat_read", channel="general", thread=posted["id"], after_id=0)
    assert [(m["from"], m["body"]) for m in thread["messages"]] == [
        ("lead", "@review can you check the parser?"), ("review", "@lead looks fine"),
    ]
    read = lead.call("chat_read", channel="general", mark_read=True)
    assert read["read_through"] == reply["id"] and read["unread_remaining"] == 0

    direct = outsider.call("chat_post", channel="general", body="let me in")
    assert (direct["ok"], direct["outcome"], direct["error"]) == (
        False, "refused", "chat is not granted for this session",
    )
    with ChatStore.maintainer() as maintainer:
        assert [m.author_handle for m in maintainer.messages("general", after_id=0)] == [
            "lead", "review",
        ]


def test_revoke_and_grant_change_the_tool_list_and_the_next_call(agents):
    lead = agents("lead")
    other = agents("other")
    grant(lead)
    grant(other)
    assert CHAT_TOOLS <= lead.tools()

    revoke(lead)
    refused = lead.call("chat_post", channel="general", body="still here?")
    assert (refused["ok"], refused["outcome"]) == (False, "refused")
    assert lead.wait_notification("notifications/tools/list_changed")
    assert not CHAT_TOOLS & lead.tools()
    entry = audit_entries()[-1]
    assert (entry["action"], entry["outcome"], entry["launch_id"]) == (
        "chat_post", "refused", lead.launch_id,
    )
    assert "still here" not in json.dumps(audit_entries())

    grant(lead)
    assert lead.wait_notification("notifications/tools/list_changed")
    assert CHAT_TOOLS <= lead.tools()
    with ChatStore.maintainer() as maintainer:
        listed = maintainer.grant_for(lead.launch_id)
    assert listed.tools_listed_at is not None and listed.tools_listed_at >= listed.granted_at


def test_a_server_with_no_chat_file_lists_no_chat_tools(agents):
    lone = agents("lone")
    assert not chat_store.exists()
    assert not CHAT_TOOLS & lone.tools()


def test_an_unverifiable_server_never_registers_chat_tools(agents):
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        impostor = agents("impostor", claim_pid=bystander.pid)
        check_impostor(agents, impostor)
    finally:
        bystander.kill()
        bystander.wait()


def check_impostor(agents, impostor):
    grant(agents("peer"))
    grant(impostor)
    assert not CHAT_TOOLS & impostor.tools()
    call = impostor.call("chat_post", channel="general", body="hello")
    assert call["is_error"] and "chat_post" in call["text"]
