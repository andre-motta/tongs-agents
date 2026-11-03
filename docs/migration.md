# Upgrading from claude-fleet-monitor

tongs-agents replaces claude-fleet-monitor 0.9.x. It uses its own runtime
names from its first release, so an old install is cleaned up rather than
migrated. Session records are live status that goes stale within minutes, so
nothing in them is carried over.

## What changed

| | claude-fleet-monitor 0.9.x | tongs-agents |
| --- | --- | --- |
| Commands | `claude-fleet`, `claude-fleet-hook` | `tongs-agents`, `tongs-agents-hook` |
| Session records | `~/.claude/fleet/` (`FLEET_DIR`) | `$XDG_STATE_HOME/tongs-agents/sessions/` (`~/.local/state/tongs-agents/sessions/`; `TONGS_AGENTS_STATE_DIR` overrides the state directory). `FLEET_DIR` is not read |
| Claude Code MCP | `mcpServers.fleet` in `~/.claude/settings.json` (Claude Code does not load user MCP servers from there) | `tongs-agents`, registered with `claude mcp add --scope user` |
| Codex MCP | `fleet` via `codex mcp add` | `tongs-agents` via `codex mcp add` |
| Pi config and marker | `claude-fleet-monitor.json`, owner `claude-fleet-monitor` | `tongs-agents.json`, owner `tongs-agents` |
| Pi runtime symbol and package | `claude-fleet-monitor.pi.runtime.v1`, `claude-fleet-monitor-pi` | `tongs-agents.pi.runtime.v1`, `tongs-agents-pi` |
| tongs entry point | `fleet-monitor` (`[plugins.fleet-monitor]`) | `agents` (`[plugins.agents]`) |
| Python module | `claude_fleet_monitor` | `tongs_agents` |

The MCP tool names (`fleet_status`, `fleet_focus`, ...) are unchanged.

## Steps

```bash
pip install tongs-agents
tongs-agents install                # or: tongs-agents install --agent all
pip uninstall claude-fleet-monitor  # or pipx uninstall, matching how you installed it
tongs-agents doctor
```

Restart running agent sessions afterwards. In Codex, review the new hooks with
`/hooks`; Codex trusts each hook command separately, so the old trust entries
do not carry over.

## What install and uninstall clean up

`tongs-agents install` and `tongs-agents uninstall` remove, for the agents
they act on:

- each hook command that runs `claude-fleet-hook` or the older
  `~/.claude/bin/fleet-hook.sh`, one command at a time. Other commands in the
  same hook group stay;
- a `fleet` MCP entry only when it starts `claude_fleet_monitor.mcp_server` or
  `tongs_agents.mcp_server` with Python: the `settings.json` entry, a Claude
  Code user-scope entry and the Codex entry. A `fleet` server that runs
  anything else is yours and is kept;
- with `--agent pi` or `--agent all`, the Pi extension paths recorded in
  `claude-fleet-monitor.json` with the `claude-fleet-monitor` marker, followed
  by that file. A failed removal keeps the file so a later run can retry, and a
  file without the marker is left alone.

Uninstall also removes the entries tongs-agents itself installed.

## What is never removed automatically

`~/.claude/fleet/` stays where it is. tongs-agents neither reads nor deletes
it. `tongs-agents doctor` reports it together with the command to remove it,
for example:

```console
$ tongs-agents doctor
Session directory: /home/user/.local/state/tongs-agents/sessions

Unused claude-fleet-monitor session directory: /home/user/.claude/fleet
  tongs-agents never reads or deletes it. Its records are stale; remove it with:
    rm -r -- /home/user/.claude/fleet
```

Doctor also reports a claude-fleet-monitor package or command that is still
installed, and any old hook, MCP or Pi entry that install has not cleaned up
yet. It only reads; it changes nothing.

## tongs

If you load the plugin in tongs, rename its configuration section from
`[plugins.fleet-monitor]` to `[plugins.agents]`.
