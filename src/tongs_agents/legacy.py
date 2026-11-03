"""Markers written by the claude-fleet-monitor predecessor.

The installer adopts these on upgrade so an existing install is replaced
rather than duplicated. This is the only source module that spells them.
"""

from pathlib import Path


LEGACY_HOOK_COMMANDS = ("claude-fleet-hook",)
LEGACY_HOOK_SCRIPTS = (
    (Path.home() / ".claude" / "bin" / "fleet-hook.sh").resolve(),
)
LEGACY_MCP_MODULES = ("claude_fleet_monitor.mcp_server",)
