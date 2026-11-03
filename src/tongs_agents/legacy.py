"""Markers written by the claude-fleet-monitor 0.9.x predecessor.

Install and uninstall remove these so an old install is cleaned up rather than
duplicated, and doctor reports what they cannot remove. This is the only
source module that spells them.
"""

from pathlib import Path


LEGACY_DISTRIBUTION = "claude-fleet-monitor"
LEGACY_COMMANDS = ("claude-fleet", "claude-fleet-hook")
LEGACY_HOOK_COMMANDS = ("claude-fleet-hook",)
LEGACY_HOOK_SCRIPTS = (
    (Path.home() / ".claude" / "bin" / "fleet-hook.sh").resolve(),
)
LEGACY_MCP_NAME = "fleet"
LEGACY_MCP_MODULES = ("claude_fleet_monitor.mcp_server",)
LEGACY_PI_CONFIG_NAME = "claude-fleet-monitor.json"
LEGACY_PI_CONFIG_OWNER = "claude-fleet-monitor"
LEGACY_PACKAGE = "claude_fleet_monitor"
LEGACY_TONGS_PLUGIN = "fleet-monitor"


def legacy_sessions_dir() -> Path:
    """The 0.9.x session directory; never deleted automatically."""
    return Path.home() / ".claude" / "fleet"
