"""The MCP chat gate and server with a fake process table (no subprocess).

The end-to-end stdio run with real process ancestry is test_chat_mcp_stdio.py.
"""

import asyncio
import json

import pytest

from tests.test_chat_identity import AGENT_PID, LAUNCH, SERVER_PID, START, FakeProcess, record
from tongs_agents import discovery, mcp_chat, mcp_server, paths
from tongs_agents.chat import identity
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import ChatStore

ENV = {identity.LAUNCH_ENV: LAUNCH, identity.PROFILE_ENV: "lead"}
PEER = "e" * 32


@pytest.fixture
def processes(monkeypatch):
    table = {
        AGENT_PID: FakeProcess(AGENT_PID, START, 1),
        SERVER_PID: FakeProcess(SERVER_PID, "77", AGENT_PID),
    }
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    record()
    return table


def grant(launch_id=LAUNCH, handle="lead", start=START):
    with ChatStore.maintainer(create=True) as current:
        current.put_grant(
            launch_id=launch_id, handle=handle, level="chat", process_start=start,
            pane_session="agents", pane_id="1", socket_dir=None, via="cli",
            profile=handle, harness="claude",
        )


def revoke(launch_id=LAUNCH):
    with ChatStore.maintainer() as current:
        current.delete_grant(launch_id)


def gate():
    return mcp_chat.ChatGate.start(ENV, pid=SERVER_PID)


def audit():
    path = paths.state_dir() / "audit" / "chat.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def tool_names(server):
    return {tool.name for tool in asyncio.run(server.list_tools())}


def test_an_unverifiable_server_registers_no_chat_tools(processes, capsys):
    outside = mcp_chat.ChatGate.start({}, pid=SERVER_PID)
    assert not outside.enabled
    assert capsys.readouterr().err == ""
    stranger = mcp_chat.ChatGate.start(ENV, pid=999)
    assert not stranger.enabled
    assert "chat tools off" in capsys.readouterr().err
    grant()
    for chat_gate in (outside, stranger, None):
        server = mcp_server.create_server(chat_gate=chat_gate)
        assert not set(mcp_chat.CHAT_TOOL_NAMES) & tool_names(server)
        assert not set(mcp_chat.CHAT_TOOL_NAMES) & set(server._tool_manager._tools)


def test_chat_tools_are_listed_only_while_granted(processes):
    server = mcp_server.create_server(chat_gate=gate())
    assert not set(mcp_chat.CHAT_TOOL_NAMES) & tool_names(server)
    grant()
    assert set(mcp_chat.CHAT_TOOL_NAMES) <= tool_names(server)
    with ChatStore.maintainer() as current:
        assert current.grant_for(LAUNCH).tools_listed_at is not None
    revoke()
    assert not set(mcp_chat.CHAT_TOOL_NAMES) & tool_names(server)
    assert "fleet_status" in tool_names(server)


def test_a_grant_for_an_older_process_does_not_count(processes):
    grant(start="older")
    assert not gate().visible()


def test_poll_reports_changes_once(processes):
    chat_gate = gate()
    assert chat_gate.poll_changed() is False
    grant()
    assert chat_gate.poll_changed() is True
    assert chat_gate.poll_changed() is False
    revoke()
    assert chat_gate.poll_changed() is True
    chat_gate.note_listed(False)
    assert chat_gate.poll_changed() is False


def test_poll_skips_the_lookup_while_data_version_is_unchanged(processes, monkeypatch):
    grant()
    chat_gate = gate()
    assert chat_gate.visible()
    lookups = []
    original = ChatStore.grant_for
    monkeypatch.setattr(ChatStore, "grant_for",
                        lambda self, launch_id: lookups.append(launch_id) or original(
                            self, launch_id))
    assert chat_gate.visible() and lookups == []
    revoke()
    assert not chat_gate.visible() and lookups == [LAUNCH]


def test_a_purged_and_recreated_file_is_noticed(processes):
    grant()
    chat_gate = gate()
    assert chat_gate.visible()
    chat_store.purge()
    assert not chat_gate.visible()
    grant()
    assert chat_gate.visible()


def test_every_call_rechecks_identity_and_grant(processes):
    chat_gate = gate()
    refused = chat_gate.post("general", "hello before any grant")
    assert (refused["ok"], refused["outcome"]) == (False, "refused")
    grant()
    assert chat_gate.post("general", "hello")["ok"]
    processes[AGENT_PID] = FakeProcess(AGENT_PID, "restarted", 1)
    refused = chat_gate.read("general")
    assert refused["outcome"] == "refused" and "identity" in refused["error"]
    processes[AGENT_PID] = FakeProcess(AGENT_PID, START, 1)
    revoke()
    assert chat_gate.inbox()["outcome"] == "refused"
    entries = audit()
    assert [(e["action"], e["outcome"]) for e in entries] == [
        ("chat_post", "refused"), ("chat_read", "refused"), ("chat_inbox", "refused"),
    ]
    assert entries[0]["actor"] == f"mcp:{LAUNCH}"
    assert entries[0]["bytes"] == len(b"hello before any grant")
    assert "hello" not in json.dumps(entries)


def test_inbox_read_and_post(processes):
    grant()
    grant(PEER, "peer", start="1")
    with ChatStore.agent(PEER) as peer:
        root = peer.post("general", "@lead please\tlook\nat the parser")
    chat_gate = gate()
    inbox = chat_gate.inbox()
    assert inbox["ok"] and inbox["handle"] == "lead"
    assert inbox["notice"].startswith("[tongs-agents chat]")
    assert inbox["channels"] == [{"name": "general", "unread": 1}]
    assert inbox["unread_mentions"] == 1
    mention = inbox["mentions"][0]
    assert (mention["id"], mention["from"], mention["kind"]) == (root.id, "peer", "agent")
    assert mention["preview"] == "@lead please look at the parser"

    posted = chat_gate.post("general", "@peer on it", thread=root.id)
    assert posted == {"ok": True, "id": posted["id"], "channel": "general",
                      "thread": root.id, "handle": "lead", "mentioned": ["peer"]}
    with ChatStore.maintainer() as current:
        assert current.message(posted["id"]).author == LAUNCH

    read = chat_gate.read("general", mark_read=True)
    assert [m["id"] for m in read["messages"]] == [root.id, posted["id"]]
    assert read["read_through"] == posted["id"] and read["unread_remaining"] == 0
    assert chat_gate.inbox()["mentions"] == []
    assert chat_gate.read("general", thread=root.id, mark_read=True)["ok"] is False
    assert chat_gate.read("nope")["ok"] is False
    assert chat_gate.read("general", limit=99)["ok"] is False


def test_post_limits_are_refusals_and_audited(processes, monkeypatch):
    grant()
    monkeypatch.setattr(chat_store, "POST_RATE_PER_MINUTE", 1)
    chat_gate = gate()
    assert chat_gate.post("general", "one")["ok"]
    limited = chat_gate.post("general", "two")
    assert (limited["ok"], limited["outcome"]) == (False, "limit")
    assert audit()[-1]["outcome"] == "limit"


def test_tool_functions_return_json(processes):
    grant()
    server = mcp_server.create_server(chat_gate=gate())

    async def call():
        return await server.call_tool("chat_post", {"channel": "general", "body": "hi"})

    result = asyncio.run(call())
    blocks = result[0] if isinstance(result, tuple) else result
    payload = json.loads(blocks[0].text)
    assert payload["ok"] and payload["handle"] == "lead"


def test_watch_grant_sends_list_changed(processes):
    grant()
    server = mcp_server.create_server(chat_gate=gate())
    sent = []

    class Session:
        async def send_tool_list_changed(self):
            sent.append(True)

    server._chat_session = Session()
    server.chat_gate.note_listed(True)

    async def watch():
        task = asyncio.create_task(server.watch_grant(interval=0.01))
        await asyncio.sleep(0.05)
        await asyncio.to_thread(revoke)
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(watch())
    assert sent == [True]
