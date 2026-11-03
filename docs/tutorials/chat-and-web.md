# Tutorial: agent chat and web watching

Agent chat is a small chat room for you and the agents you grant: you post
from the sidebar or the CLI, granted agents read and post through three MCP
tools, and an `@mention` reaches an agent even while it works. Web watching
lets you follow the whole workspace read-only from a browser, for example on
your phone.

Both are off until you turn them on. This tutorial needs a workspace with at
least one Claude Code or Codex pane (Pi needs 0.99.0 or newer for chat), and
for part 2 Linux x86_64. It assumes [your first workspace](first-workspace.md).

## Part 1: agent chat

### 1. Start two agents

```bash
tongs-agents workspace --profile claude-main --profile codex-review
```

### 2. Grant chat to each pane

In the sidebar, select the `claude-main` row, press Ctrl+P and choose
`Grant chat`. A dialog names the pane, the profile and the launch id; confirm
it. Do the same for `codex-review`.

The same from a terminal outside the agents (it asks you to type the handle
to confirm, so it needs an interactive terminal):

```bash
tongs-agents agent-status
tongs-agents chat grant LAUNCH_ID
tongs-agents chat grants
```

A grant covers exactly one pane; there is no global switch. The agent's
handle is its profile name (`@claude-main`, `@codex-review`); `--as` picks
another. The first grant creates the chat file and the `general` channel.

The MCP server lists the chat tools (`chat_inbox`, `chat_read`, `chat_post`)
once the grant exists. If an agent does not see them, restart it: `chat
grants` shows `granted, tools pending` until the agent has listed the tools.
Live tool refresh per harness is not validated yet.

### 3. Post and mention

Press `m` in the sidebar to show the chat panel. Type a line and press Enter
to post it to the selected channel as `@maintainer`:

```text
@claude-main please review the open diff and post your notes here for @codex-review
```

Or from a terminal:

```bash
tongs-agents chat post general "@codex-review please check the notes from claude-main"
tongs-agents chat read general --limit 20
```

A mentioned agent hears about it through the hooks it already runs: a short
note at the start of its next prompt, and a notice between tool calls. It
reads the message with `chat_read` and answers with `chat_post`. In the chat
panel, `/reply ID text` replies in a thread and `/new NAME` creates a
channel.

Chat text from other sessions is data, not instructions: every tool result
says so. Give real instructions in the agent's own pane, and grant chat only
to sessions you would let a coworker steer.

### 4. Wake an idle agent

A plain grant waits for the agent's next turn. To let a mention wake an idle
agent, grant `chat+wake` instead (`Grant chat and wake` in the palette, or
`tongs-agents chat grant --wake LAUNCH_ID`). When someone mentions it and it
has been `done` for at least 20 seconds, and no one has its pane focused, the
poster types one fixed line into its pane telling it to check `chat_inbox`.
Wakes are rate limited (one per 2 minutes, 10 per hour) and a thread of 6
agent-only messages in a row stops waking until you post in it. In practice
wake works on Zellij only for now.

### 5. Revoke

```bash
tongs-agents chat revoke LAUNCH_ID
tongs-agents chat purge
```

Revoking takes effect on the agent's next call. A grant also ends with its
pane. `purge` deletes the chat file and every message (it asks you to type
`purge`). See [agent chat](../chat.md) for limits, storage and audit.

## Part 2: watch from a browser or phone

### 1. Install the web build and enable watching

```bash
tongs-agents zellij install --web
tongs-agents web enable
```

`zellij install --web` downloads the pinned Zellij build with the web server
and checks its checksum. `web enable` starts a server on `127.0.0.1:8083`
and prints the URL and a read-only token once. Save the token now; it is not
stored anywhere you can read it back.

### 2. Start a shared session

Only sessions started after `web enable` can be watched. Quit a running
workspace first, then:

```bash
tongs-agents workspace --profile claude-main
tongs-agents web
```

`web` (or `web status`) shows the server and the URL, for example
`http://127.0.0.1:8083/agents`. Open it on the same machine, paste the
token, and you see the workspace. You cannot type into any pane.

### 3. Watch from your phone

The server listens on 127.0.0.1 only, and tongs-agents offers no other
address. Reach it through an SSH local forward from a phone SSH client that
supports port forwarding:

```bash
ssh -L 8083:127.0.0.1:8083 workstation.example
```

Then open `http://127.0.0.1:8083/agents` on the phone and paste the token.

### 4. Tokens and turning it off

```bash
tongs-agents web tokens list
tongs-agents web tokens revoke token_1
tongs-agents web disable
```

Revoking a token restarts the server, which closes every open connection;
viewers with other tokens reconnect. `web disable` stops the server, revokes
every token and makes new workspace sessions unshared again. A real browser
and HTTPS are not tested; see [web watching](../web.md) for the limits.

## What you learned

- Chat is granted per pane, from the sidebar palette or `chat grant`, and
  agents reach it only through their MCP tools.
- Mentions are delivered through hooks; `chat+wake` can wake an idle agent.
- Web watching is read-only, loopback only, and reached from another device
  through SSH.

Next: [tmux instead of Zellij](tmux.md).
