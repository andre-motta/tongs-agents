"""MCP server exposing Claude Code, Codex and Pi sessions for Tongs Agents.

The read-only ``fleet_*`` tools are always listed. The agent control tools
(``agents_spawn``, ``agents_send``, ``agents_status``, ``agents_wait``) are
registered only when ``[control].mcp_enabled`` is true when the server starts;
their policy lives in ``mcp_control`` and every call is audited by
``control``. See docs/mcp-control.md.

The chat tools (``chat_inbox``, ``chat_read``, ``chat_post``) are registered
only when the server verifies its launch identity at start, and listed only
while the maintainer has granted that launch chat; ``mcp_chat`` holds that
policy. See docs/chat.md.
"""

import asyncio
import json
import sys

from mcp.server.fastmcp import FastMCP

from tongs_agents import control
from tongs_agents import focus as focus_module
from tongs_agents.app_config import AppConfigError, load_app_config
from tongs_agents.discovery import cleanup_ended_sessions, read_sessions
from tongs_agents.mcp_chat import ChatGate, ChatServer
from tongs_agents.mcp_control import McpControl, error_payload

SERVER_NAME = "tongs-agents"


def fleet_status() -> str:
    """Get all Claude Code, Codex and Pi session statuses."""
    sessions = read_sessions()
    if not sessions:
        return json.dumps({"sessions": [], "summary": "No active sessions"})

    active = sum(1 for s in sessions if s["status"] in ("running", "started"))
    idle = sum(1 for s in sessions if s["status"] == "idle")
    attention = sum(1 for s in sessions if s["needs_attention"])

    return json.dumps(
        {
            "sessions": sessions,
            "summary": {
                "total": len(sessions),
                "active": active,
                "idle": idle,
                "needs_attention": attention,
            },
        },
        indent=2,
    )


def fleet_session(session_id: str) -> str:
    """Get a session by canonical or native ID (full or prefix match)."""
    sessions = read_sessions()
    exact = [
        session for session in sessions
        if session_id in {
            session.get("canonical_id", ""), session.get("session_id", "")
        }
    ]
    matches = exact or [
        session for session in sessions
        if session.get("canonical_id", "").startswith(session_id)
        or session.get("session_id", "").startswith(session_id)
    ]
    if not matches:
        return json.dumps({"error": f"No session matching '{session_id}'"})
    if len(matches) > 1:
        return json.dumps({
            "error": f"Multiple sessions match '{session_id}'",
            "matches": [
                {
                    "canonical_id": session.get("canonical_id", ""),
                    "harness_id": session.get("harness_id", ""),
                    "session_id": session.get("session_id", ""),
                }
                for session in matches
            ],
        }, indent=2)
    return json.dumps(matches[0], indent=2)


def fleet_sessions_needing_attention() -> str:
    """Get sessions that are idle for over 2 minutes and likely need user input."""
    sessions = read_sessions()
    attention = [s for s in sessions if s["needs_attention"]]
    if not attention:
        return json.dumps({"message": "All sessions healthy", "sessions": []})
    return json.dumps(
        {
            "message": f"{len(attention)} session(s) may need input",
            "sessions": attention,
        },
        indent=2,
    )


def fleet_focus(query: str) -> str:
    """Focus the terminal running an agent session by repo, session ID, or PID."""
    result = focus_module.focus_session(query)
    payload = result.to_dict()
    # Keep the pre-0.10 reply keys for existing MCP consumers and prompts.
    payload["success"] = result.successful
    payload["message" if result.successful else "error"] = result.reason
    return json.dumps(payload)


def fleet_cleanup() -> str:
    """Remove status files for ended sessions older than 5 minutes."""
    return json.dumps({"removed": cleanup_ended_sessions()})


def _control_call(call) -> str:
    try:
        payload = call(McpControl())
    except control.ControlError as error:
        payload = error_payload(error)
    return json.dumps(payload, indent=2)


async def agents_spawn(profile: str) -> str:
    """Start a named profile with mcp_control = true in a new pane of the running
    workspace session. Returns its launch id. Takes no command, env or cwd."""
    return await asyncio.to_thread(_control_call, lambda c: c.spawn(profile))


async def agents_send(launch_id: str, text: str) -> str:
    """Send a prompt (pasted, then Enter) to an agent pane this agent spawned.

    At most 64 KiB; tabs and newlines are the only control characters allowed.
    """
    return await asyncio.to_thread(_control_call, lambda c: c.send(launch_id, text))


async def agents_status(launch_id: str) -> str:
    """The attention state (working, needs-you, done, failed, quiet, ended) of an
    agent pane this agent spawned."""
    return await asyncio.to_thread(_control_call, lambda c: c.status(launch_id))


async def agents_wait(launch_id: str, until: str = "done",
                      timeout_s: float = control.MAX_WAIT_SECONDS) -> str:
    """Wait until an agent pane this agent spawned reaches ``until`` (done,
    needs-you, ended or any), for at most timeout_s seconds (600 at most)."""
    return await asyncio.to_thread(
        _control_call, lambda c: c.wait(launch_id, until, timeout_s)
    )


FLEET_TOOLS = (
    fleet_status,
    fleet_session,
    fleet_sessions_needing_attention,
    fleet_focus,
    fleet_cleanup,
)
CONTROL_TOOLS = (agents_spawn, agents_send, agents_status, agents_wait)


def create_server(control_enabled: bool = False,
                  chat_gate: ChatGate | None = None) -> FastMCP:
    """A server with the fleet tools, plus the control tools when enabled and
    the chat tools when ``chat_gate`` verified this launch."""
    server = ChatServer(SERVER_NAME, chat_gate)
    for tool in FLEET_TOOLS + (CONTROL_TOOLS if control_enabled else ()):
        server.add_tool(tool)
    return server


def control_enabled() -> bool:
    """``[control].mcp_enabled``; an unreadable config leaves control off."""
    try:
        return load_app_config().control.mcp_enabled
    except AppConfigError as error:
        print(f"tongs-agents: MCP control tools off: {error}", file=sys.stderr)
        return False


mcp = create_server()


def main():
    create_server(control_enabled(), ChatGate.start()).run()


if __name__ == "__main__":
    main()
