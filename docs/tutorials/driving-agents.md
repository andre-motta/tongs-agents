# Tutorial: driving agents

tongs-agents can type into the panes it started: paste a prompt and press
Enter, read the screen, and wait until the agent is done. In this tutorial
you practise on a free shell pane, then let one agent start and prompt
another through MCP.

Part 1 needs only a shell. Part 2 needs two agents that can use MCP tools,
for example Claude Code and Codex. It assumes
[your first workspace](first-workspace.md) and
[any command as a pane](any-command.md).

## Part 1: send, read and wait

### 1. Start a practice pane

```toml
[profiles.scratch]
command = ["bash", "--norc"]
```

```bash
tongs-agents workspace --session practice --profile scratch
```

### 2. Find its launch id

Open a second terminal, outside the workspace, and list the launched panes:

```bash
tongs-agents agent-status
```

Each line shows a launch id (32 hex characters), the profile, the
`session:pane` and the state. `scratch` is `quiet`: it runs but reports no
status. Copy its launch id; below it is `LAUNCH_ID`. Inside a pane the same
id is in `$TONGS_AGENTS_LAUNCH_ID`.

Only panes that tongs-agents started can be driven. A pane you opened
yourself has no launch record and is refused with exit 5.

### 3. Send a prompt and read the screen

```bash
tongs-agents send LAUNCH_ID "echo hello from tongs-agents"
tongs-agents read LAUNCH_ID --lines 5
```

`send` pastes the text as one bracketed paste, waits `send_delay_ms`
(150 ms by default) and presses Enter. `read` prints the last lines of the
pane, here the echoed line. Multi-line text arrives as one prompt; pass `-`
to read it from standard input:

```bash
printf 'echo one\necho two\n' | tongs-agents send LAUNCH_ID -
tongs-agents send --no-submit LAUNCH_ID "typed, not submitted"
```

### 4. Wait for done

A shell has no hooks, so let the command it runs report its own result with
`notify`:

```bash
tongs-agents send LAUNCH_ID "sleep 5; tongs-agents notify done 'slept'"
tongs-agents wait LAUNCH_ID --until done --timeout 60
```

`wait` returns once the pane reports `done`, and prints the launch id and
the state. Status written before the last `send` does not count, so an
earlier `done` never ends the wait for the next one. With a real agent the
hooks report `done` when its turn ends, and the same two commands drive it.

The exit status tells a script what happened:

| Exit | Meaning |
| --- | --- |
| 0 | the state was reached |
| 1 | error, or the turn failed |
| 2 | timeout |
| 3 | the agent needs you |
| 4 | the agent exited |
| 5 | refused: not a pane tongs-agents started and verified |
| 6 | the multiplexer has no pane control |

Finish the pane and wait for that too:

```bash
tongs-agents send LAUNCH_ID "exit"
tongs-agents wait LAUNCH_ID --until ended --timeout 30
```

Every call, including refusals, is appended to the audit log
`$XDG_STATE_HOME/tongs-agents/audit/control.jsonl` with byte counts and
hashes, never the text. See [driving agent panes](../control.md) for every
check `send` makes before it types.

## Part 2: let an agent drive another

The MCP server that `tongs-agents install` registered can offer four extra
tools: `agents_spawn`, `agents_send`, `agents_status` and `agents_wait`. They
are off by default. An agent can only start a profile you configured, as you
configured it; no tool takes a command, arguments or environment.

### 1. Opt in

In `~/.config/tongs-agents/config.toml`:

```toml
[control]
mcp_enabled = true

[profiles.lead]
command = ["claude"]
mcp_control = true

[profiles.worker]
command = ["codex"]
mcp_control = true
```

Both sides opt in: `mcp_enabled` registers the tools, and only profiles with
`mcp_control = true` may call them or be driven by them. The caller is
identified by the `TONGS_AGENTS_LAUNCH_ID` its MCP server inherits from the
pane, so this tutorial uses Claude Code, which passes its environment to MCP
servers, as the lead. Codex or Pi as the lead is not validated. By default a caller
may drive only the panes it spawned itself, at most 6 sends and 6 spawns per
minute.

### 2. Start the lead

```bash
tongs-agents install
tongs-agents workspace --profile lead
```

Run `install` again if `lead` or `worker` uses a config home that has no
hooks yet. The MCP server reads `mcp_enabled` when it starts, so restart an
agent that was already running.

### 3. Ask the lead to delegate

In the `lead` pane, ask for example:

```text
Use the agents_spawn tool to start the worker profile. Then use agents_send
to ask it to list the TODO comments in this repository, agents_wait until it
is done, and summarise its answer.
```

A `worker` pane appears next to `lead` and in the sidebar. The lead gets the
worker's launch id from `agents_spawn` and drives it with the other three
tools. Every call is in the same audit log as part 1, with the actor
`mcp:<lead launch id>`.

To turn it off again, set `mcp_enabled = false`. Running servers refuse calls
at once and stop listing the tools after a restart.

## What you learned

- `send`, `read` and `wait` drive panes tongs-agents started, and nothing
  else.
- Exit codes make them safe to script.
- MCP agent control is two opt-ins: `[control] mcp_enabled` and
  `mcp_control` per profile.

Reference: [driving agent panes](../control.md),
[MCP agent control](../mcp-control.md).

Next: [agent chat and web watching](chat-and-web.md).
