import asyncio
import json
import threading
import time

import anyio
import pytest

import tongs_agents.discovery as discovery
import tongs_agents.mcp_server as mcp_server
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from tests.conftest import write_session


@pytest.mark.asyncio
async def test_mcp_protocol_lists_tools_and_returns_fleet_status(
    fleet_dir, monkeypatch
):
    """Exercise the MCP initialization, tool discovery, and status call."""
    monkeypatch.setattr(discovery, "discover_processes", lambda: None)
    now = int(time.time())
    write_session(
        fleet_dir,
        "running-session",
        "alpha",
        "/tmp/alpha",
        status="running",
        detail="working",
        ts=now,
    )
    write_session(
        fleet_dir,
        "waiting-session",
        "beta",
        "/tmp/beta",
        status="waiting",
        detail="permission needed",
        ts=now,
    )

    async def protocol_smoke():
        client_to_server_send, client_to_server_receive = (
            anyio.create_memory_object_stream(0)
        )
        server_to_client_send, server_to_client_receive = (
            anyio.create_memory_object_stream(0)
        )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                mcp_server.mcp._mcp_server.run,
                client_to_server_receive,
                server_to_client_send,
                mcp_server.mcp._mcp_server.create_initialization_options(),
            )
            async with ClientSession(
                server_to_client_receive, client_to_server_send
            ) as session:
                initialization = await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("fleet_status")
            task_group.cancel_scope.cancel()

        return initialization, tools, status

    initialization, tools, status = await asyncio.wait_for(
        protocol_smoke(), timeout=5
    )

    assert initialization.serverInfo.name == "tongs-agents"
    assert {tool.name for tool in tools.tools} == {
        "fleet_status",
        "fleet_session",
        "fleet_sessions_needing_attention",
        "fleet_focus",
        "fleet_cleanup",
    }
    assert status.isError is False
    payload = json.loads(status.content[0].text)
    assert payload["summary"] == {
        "total": 2,
        "active": 1,
        "idle": 0,
        "needs_attention": 1,
    }
    assert {session["session_id"] for session in payload["sessions"]} == {
        "running-session",
        "waiting-session",
    }


FLEET_TOOLS = {
    "fleet_status",
    "fleet_session",
    "fleet_sessions_needing_attention",
    "fleet_focus",
    "fleet_cleanup",
}
CONTROL_TOOLS = {"agents_spawn", "agents_send", "agents_status", "agents_wait"}


async def _list_and_call(server, name=None, arguments=None):
    client_to_server_send, client_to_server_receive = anyio.create_memory_object_stream(0)
    server_to_client_send, server_to_client_receive = anyio.create_memory_object_stream(0)
    async with anyio.create_task_group() as task_group:
        task_group.start_soon(
            server._mcp_server.run,
            client_to_server_receive,
            server_to_client_send,
            server._mcp_server.create_initialization_options(),
        )
        async with ClientSession(server_to_client_receive, client_to_server_send) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = None if name is None else await session.call_tool(name, arguments)
        task_group.cancel_scope.cancel()
    return tools, result


def _write_control(tmp_path, enabled):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(f"[control]\nmcp_enabled = {str(enabled).lower()}\n")
    path.chmod(0o600)
    return path


@pytest.mark.asyncio
async def test_control_tools_are_absent_when_control_is_disabled(tmp_path):
    _write_control(tmp_path, False)
    assert mcp_server.control_enabled() is False
    server = mcp_server.create_server(mcp_server.control_enabled())
    tools, _ = await asyncio.wait_for(_list_and_call(server), timeout=5)
    assert {tool.name for tool in tools.tools} == FLEET_TOOLS


@pytest.mark.asyncio
async def test_control_tools_are_listed_when_enabled_and_take_no_command(tmp_path):
    _write_control(tmp_path, True)
    assert mcp_server.control_enabled() is True
    server = mcp_server.create_server(mcp_server.control_enabled())
    tools, _ = await asyncio.wait_for(_list_and_call(server), timeout=5)
    assert {tool.name for tool in tools.tools} == FLEET_TOOLS | CONTROL_TOOLS
    parameters = {
        tool.name: set(tool.inputSchema.get("properties", {}))
        for tool in tools.tools if tool.name in CONTROL_TOOLS
    }
    assert parameters == {
        "agents_spawn": {"profile"},
        "agents_send": {"launch_id", "text"},
        "agents_status": {"launch_id"},
        "agents_wait": {"launch_id", "until", "timeout_s"},
    }


def test_an_invalid_config_leaves_control_off(tmp_path, capsys):
    path = _write_control(tmp_path, True)
    path.write_text("[control]\nmcp_enabled = 1\n")
    assert mcp_server.control_enabled() is False
    assert "MCP control tools off" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_a_control_call_outside_a_launched_pane_is_refused(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("TONGS_AGENTS_LAUNCH_ID", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    _write_control(tmp_path, True)
    server = mcp_server.create_server(True)
    _, result = await asyncio.wait_for(
        _list_and_call(server, "agents_send", {"launch_id": "a" * 32, "text": "hi"}),
        timeout=5,
    )
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert (payload["outcome"], payload["exit_code"]) == ("refused", 5)
    audit = (tmp_path / "state" / "audit" / "control.jsonl").read_text().splitlines()
    assert json.loads(audit[-1])["actor"] == "mcp"


@pytest.mark.asyncio
async def test_a_cancelled_agents_wait_stops_its_worker_thread(monkeypatch):
    started = threading.Event()
    stopped = threading.Event()

    class FakeControl:
        def wait(self, launch_id, until, timeout_s, stop=None):
            started.set()
            if stop.wait(5):
                stopped.set()
            return {"ok": True}

    monkeypatch.setattr(mcp_server, "McpControl", FakeControl)
    task = asyncio.create_task(mcp_server.agents_wait("a" * 32))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(stopped.wait, 5)


@pytest.mark.asyncio
async def test_a_finished_agents_wait_returns_its_reply(monkeypatch):
    seen = []

    class FakeControl:
        def wait(self, launch_id, until, timeout_s, stop=None):
            seen.append((launch_id, until, timeout_s, stop.is_set()))
            return {"ok": True, "reached": True}

    monkeypatch.setattr(mcp_server, "McpControl", FakeControl)
    reply = json.loads(await mcp_server.agents_wait("a" * 32, "any", 5))
    assert reply == {"ok": True, "reached": True}
    assert seen == [("a" * 32, "any", 5, False)]


@pytest.mark.asyncio
async def test_closing_the_transport_stops_a_pending_agents_wait(tmp_path, monkeypatch):
    """The caller agent exits mid-wait: its server's worker thread ends too."""
    _write_control(tmp_path, True)
    started = threading.Event()
    stopped = threading.Event()

    class FakeControl:
        def wait(self, launch_id, until, timeout_s, stop=None):
            started.set()
            if stop.wait(5):
                stopped.set()
            return {"ok": True}

    monkeypatch.setattr(mcp_server, "McpControl", FakeControl)
    server = mcp_server.create_server(True)
    client_send, server_receive = anyio.create_memory_object_stream(0)
    server_send, client_receive = anyio.create_memory_object_stream(0)

    async def call(session):
        with pytest.raises(McpError, match="Connection closed"):
            await session.call_tool("agents_wait", {"launch_id": "a" * 32})

    async def scenario():
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                server._mcp_server.run, server_receive, server_send,
                server._mcp_server.create_initialization_options(),
            )
            async with ClientSession(client_receive, client_send) as session:
                await session.initialize()
                task_group.start_soon(call, session)
                assert await anyio.to_thread.run_sync(started.wait, 5)
                await client_send.aclose()
                assert await anyio.to_thread.run_sync(stopped.wait, 5)
                task_group.cancel_scope.cancel()

    await asyncio.wait_for(scenario(), timeout=10)
