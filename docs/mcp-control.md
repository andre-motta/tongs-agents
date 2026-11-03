---
title: "MCP agent control"
description: "Let an agent start, prompt and watch other agents through opt-in MCP tools."
---

An agent running in a pane that tongs-agents launched can start, prompt and
watch other agents through four MCP tools on the `tongs-agents` server. They
are off by default and exist only when you opt in. The read-only `fleet_*`
tools are always available and unchanged.

| Tool | Arguments | Does |
| --- | --- | --- |
| `agents_spawn` | `profile` | Starts a named profile in a new pane of the running workspace session and returns its launch id |
| `agents_send` | `launch_id`, `text` | Pastes the text and presses Enter, like `tongs-agents send` |
| `agents_status` | `launch_id` | The pane's state, like `tongs-agents agent-status` |
| `agents_wait` | `launch_id`, `until` (default `done`), `timeout_s` (at most 600) | Waits like `tongs-agents wait` |

No tool takes a command, arguments, environment or working directory: an
agent can only start a profile you configured, as configured.

That limits what a caller can start, not what it can do once a pane runs.
`agents_send` types the text into the target as if you typed it, so it has
your full authority there: shell escapes such as `!` in Claude Code and Pi,
slash commands such as `/permissions`, and anything else the harness accepts
from its user. The target's permission mode and approval settings do not
stop it, and neither session asks you first. A caller that drives a
`worker` profile therefore holds every permission that profile could be
given, whatever mode the caller itself runs in. Set `mcp_control = true` only
on profiles whose full access you would grant the calling agent anyway.

## Turning it on

Control needs both the server switch and a per-profile opt-in:

```toml
[control]
mcp_enabled = true          # register the agents_* tools (default false)
mcp_any_target = false      # also act on opted-in panes another caller spawned (default false)
mcp_nested_spawn = false    # let an agent started through MCP start more agents (default false)
mcp_rate_per_minute = 6     # sends, and separately spawns, per caller per minute (1 to 60)

[profiles.lead]
command = ["claude"]
mcp_control = true          # may use the tools

[profiles.worker]
command = ["codex"]
mcp_control = true          # may be spawned and driven
```

The server reads `mcp_enabled` when it starts: with it off (or with an invalid
config file) the `agents_*` tools are not listed at all. Every call reads the
file again, so turning it off refuses calls from servers already running.
Restart the agent (or its MCP server) after turning it on.

## Who may do what

The caller is the agent whose MCP server makes the call, identified by the
server's `TONGS_AGENTS_LAUNCH_ID`. Each call is refused (exit code 5 in the
reply) unless:

1. That launch passes the same verification as `tongs-agents send` (claimed,
   not replaced, its pane listed and its process alive with the same start
   time), and the server process runs under that launch's process. A launch id
   copied into another process names nobody.
2. The caller's profile, the profile to spawn and the target launch's profile
   are named profiles with `mcp_control = true`.
3. The target is not the caller itself (its launch or its pane).
4. The target was spawned by this caller through `agents_spawn` (its launch
   record names the caller in `spawned_by`). With `mcp_any_target = true` any
   opted-in pane qualifies. Panes started by `tongs-agents workspace` or the
   sidebar have no spawning caller.
5. The caller has not used up `mcp_rate_per_minute` sends (or spawns) in the
   last 60 seconds. The window is kept in
   `$XDG_RUNTIME_DIR/tongs-agents/mcp-rate/` (or the state dir's
   `run/mcp-rate/`), so restarting the server does not reset it. A refused
   call does not count.
6. For `agents_spawn`, the session is running: the multiplexer, session and
   socket of the caller's own launch, whatever `[workspace]` says. A spawn
   never creates a session.

The target pane then goes through the full verification of
[driving agent panes](control.md#what-is-verified). `agents_spawn` waits up to
5 seconds for the new pane to claim its launch and reports `state` as
`running` or still `pending`; status and wait refuse a pending launch until
its pane claims it.

## Replies

Every tool returns JSON. A successful call has `"ok": true`; a refused or
failed one has `"ok": false` with `outcome` (`refused`, `rate-limited`,
`error`, `exited`, `unsupported`), the `exit_code` of the matching CLI verb
and an `error` message.

## Audit

Every call, including each refusal made by these checks, appends one line to
the control audit log described in [driving agent panes](control.md#audit-log)
(`audit/control.jsonl` in the state dir, 0600). The actor is
`mcp:<caller launch id>` (`mcp` when the server has no launch id), with the
caller's and the target's launch ids, the action (`spawn`, `send`, `status`,
`wait`), the byte count and SHA-256 of sent text, the outcome and the exit
code. Text is never logged.

## Validation

`validation/mcp_stdio.py` starts the server over stdio twice in a temporary
state and config dir: with control off it lists only the five `fleet_*`
tools; with control on it lists all nine and refuses (and audits) an
`agents_send` from outside a launched pane. It starts no agent and no
multiplexer.

```console
python validation/mcp_stdio.py
```
