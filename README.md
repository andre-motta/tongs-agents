# tongs-agents

[![Tests](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)

tongs-agents is one workspace for the AI coding agents you run side by side.
It starts Claude Code, Codex, Pi or any other command as real panes of a
Zellij (or tmux) session, puts a sidebar next to them that shows which agent
is working, done or waiting for you, and takes you to that pane.

It reads status from each harness's own hooks, so it does not care which
harness an agent runs in. There is no daemon and no telemetry: hooks write
small JSON records, and the sidebar, the `monitor` view and the MCP tools read
them.

tongs-agents is a standalone companion app in the
[tongs](https://github.com/andre-motta/tongs) family. It runs on its own;
tongs is an optional neighbour tab in the workspace, and the `[tongs]` extra
adds an Agents screen to tongs.

Formerly `claude-fleet-monitor`; see
[Upgrading from claude-fleet-monitor](#upgrading-from-claude-fleet-monitor).

## Quick start

You need Python 3.11 or newer and git. The workspace needs Zellij 0.44.2 or
newer on Linux (tongs-agents can download a pinned build for you) or tmux 3.2
or newer.

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e .                 # or: pip install -e ".[tongs]" for the tongs plugin
```

Describe the agents you want as profiles in
`~/.config/tongs-agents/config.toml`:

```toml
[profiles.claude-main]
command = ["claude"]

[profiles.claude-alt]
command = ["claude"]
config_dir = "~/.claude-alt"     # a second Claude Code config dir

[profiles.codex-review]
command = ["codex"]

[profiles.pi-local]
command = ["pi", "--provider", "ollama", "--model", "qwen3-coder"]
```

Then install the hooks, check the setup and open the workspace:

```bash
tongs-agents install             # hooks for Claude Code, Codex and every profile's config dir
tongs-agents doctor              # what is missing, and how to fix it
tongs-agents doctor --fix zellij # only if doctor says Zellij is missing (Linux x86_64)
tongs-agents workspace --profile claude-main --profile codex-review
```

The sidebar lists each pane with its status. Enter jumps to the selected
agent, `n` jumps to the next one that needs you. In Codex, review and trust
the new hooks once with `/hooks`.

The [first workspace tutorial](https://github.com/andre-motta/tongs-agents/blob/main/docs/tutorials/first-workspace.md)
walks through all of this step by step.

## What you can do

| Feature | Commands | Docs |
| --- | --- | --- |
| A workspace with one pane per agent and a status sidebar | `workspace`, `sidebar` | [workspace](https://github.com/andre-motta/tongs-agents/blob/main/docs/workspace.md) |
| Profiles: any argv command, its own config dir, cwd, env and model arguments | `profiles` | [profiles](https://github.com/andre-motta/tongs-agents/blob/main/docs/profiles.md) |
| Status for scripts, builds and agents without hooks | `notify` | [profiles](https://github.com/andre-motta/tongs-agents/blob/main/docs/profiles.md#reporting-state-from-any-command) |
| A fleet-wide session list, outside any workspace too | `monitor`, `status`, `focus` | [app settings](https://github.com/andre-motta/tongs-agents/blob/main/docs/app.md) |
| One dotfiles directory for instructions, skills and MCP servers, emitted into every harness | `config status`, `config apply`, `config capture` | [config layer](https://github.com/andre-motta/tongs-agents/blob/main/docs/config.md) |
| Drive agent panes from a script: paste a prompt, read the screen, wait for done | `send`, `read`, `wait`, `agent-status` | [driving agent panes](https://github.com/andre-motta/tongs-agents/blob/main/docs/control.md) |
| Let one agent start and prompt others (opt-in) | MCP `agents_*` tools | [MCP agent control](https://github.com/andre-motta/tongs-agents/blob/main/docs/mcp-control.md) |
| A chat between you and the agents you grant, with mentions and wake | `chat` | [agent chat](https://github.com/andre-motta/tongs-agents/blob/main/docs/chat.md) |
| Watch the workspace read-only from a browser or phone | `web` | [web watching](https://github.com/andre-motta/tongs-agents/blob/main/docs/web.md) |
| Run an agent inside an NVIDIA OpenShell sandbox, still as a pane, and approve its access requests one at a time | profile `sandbox` table, `sandbox` | [tutorial](https://github.com/andre-motta/tongs-agents/blob/main/docs/tutorials/sandbox.md), [sandboxed profiles](https://github.com/andre-motta/tongs-agents/blob/main/docs/sandbox.md) |
| MCP tools that let agents read the fleet and focus sessions | MCP `fleet_*` tools | [MCP tools](#mcp-tools) |
| A pinned, checksum-verified Zellij download | `zellij`, `doctor --fix zellij` | [Zellij pins](https://github.com/andre-motta/tongs-agents/blob/main/docs/zellij-pins.md) |

Everything that acts on your agents is opt-in. MCP agent control needs
`[control] mcp_enabled = true` plus `mcp_control = true` on each profile;
chat needs a grant per pane; web watching starts only with `web enable` and
binds 127.0.0.1 only.

Sandboxed profiles have instrumented evidence only: every OpenShell call has
been checked against a fake CLI, and the live run on a real gateway is
pending. The [sandbox tutorial](https://github.com/andre-motta/tongs-agents/blob/main/docs/tutorials/sandbox.md)
walks through setup, and [sandboxed profiles](https://github.com/andre-motta/tongs-agents/blob/main/docs/sandbox.md)
is the reference.

## Support

| | Status |
| --- | --- |
| Harnesses | Claude Code and Codex through native hooks; Pi 0.84.4 or newer through a packaged extension (chat tools need Pi 0.99.0 or newer); any other command through `notify` |
| Multiplexers | Zellij 0.44.2 to 0.45.1 validated on Linux x86_64 (newer releases run with a `doctor` warning); tmux 3.2 or newer |
| Platforms | Linux. The workspace on macOS is unvalidated. Windows has no workspace; the hooks, `monitor` and `status` are cross-platform but only mocked there |
| Terminal focus | Konsole, tmux, Zellij, Kitty and GNU screen validated live on Linux; Ghostty, GNOME Terminal, iTerm2, macOS Terminal and Windows Terminal are mocked or inspected only; Terminator is detection only |

Evidence for each claim, and what is not validated yet, is in the
[support matrix](https://github.com/andre-motta/tongs-agents/blob/main/docs/support-matrix.md).
Some harness behaviours that chat and the sandbox rely on are read from
harness documentation and binaries but not observed live; those pages say so
where it matters.

## Documentation

Start at the [documentation index](https://github.com/andre-motta/tongs-agents/blob/main/docs/README.md).
It has tutorials, how-to guides, reference pages and background, grouped by
what you want to do.

## MCP tools

`tongs-agents install` registers the `tongs-agents` MCP server with Claude
Code (`claude mcp add --scope user`) and Codex (`codex mcp add`) when those
CLIs are on `PATH`. These read-only tools are always available:

| Tool | Description |
| --- | --- |
| `fleet_status` | All sessions with summary counts |
| `fleet_session` | One session by ID or prefix |
| `fleet_sessions_needing_attention` | Sessions waiting for input, or idle for more than 2 minutes |
| `fleet_focus` | Focus a session's terminal |
| `fleet_cleanup` | Run session store maintenance now |

The `agents_*` control tools and the `chat_*` tools appear only when you turn
them on; see [MCP agent control](https://github.com/andre-motta/tongs-agents/blob/main/docs/mcp-control.md)
and [agent chat](https://github.com/andre-motta/tongs-agents/blob/main/docs/chat.md).

## Upgrading from claude-fleet-monitor

Install tongs-agents, then run `tongs-agents install` (add `--agent all` if
you used Pi). It removes the claude-fleet-monitor hooks, its `fleet` MCP
entries and its Pi extension, and keeps everything else. Then run
`pip uninstall claude-fleet-monitor` and `tongs-agents doctor`, which reports
anything left over, including the old `~/.claude/fleet/` directory and the
command to remove it. Old session records are not migrated. See the
[migration guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/migration.md).

## Uninstall

```bash
tongs-agents uninstall              # hooks, MCP entries and session records
tongs-agents uninstall --keep-data  # keep the sessions directory
tongs-agents uninstall --agent pi   # only the Pi extension
pip uninstall tongs-agents
```

Session records are kept while the Pi extension is still installed. The
`<name>.tongs-agents.bak` copies that install kept of your `settings.json` and
`hooks.json` stay; delete them yourself when you no longer need them.

## Contributing

See [CONTRIBUTING.md](https://github.com/andre-motta/tongs-agents/blob/main/CONTRIBUTING.md)
for development setup and the PR process. Harness contributors should follow
the [adapter author guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/harness-adapters.md).
Agent-led initiatives follow the [SDLC profile](https://github.com/andre-motta/tongs-agents/blob/main/docs/SDLC.md).

## License

[MIT](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)
