# tongs-agents

[![Tests](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)

tongs-agents gives you one workspace for many coding agents, whichever harness
they run in. Claude Code, Codex and pi each keep their own sessions, terminals
and status. tongs-agents collects that status from the harnesses' own hooks,
shows every session in one list, tells you which one needs input, and takes you
to the right terminal. It is a standalone, harness-agnostic companion app in the
[tongs](https://github.com/andre-motta/tongs) family: it runs on its own, and
tongs is optional.

> **Status: pre-release.** tongs-agents is not on PyPI yet; install it from
> source. v0.10.0 will be the first release and ships milestones M0 to M5 (no
> date).
>
> - **Works today:** hook-based status for Claude Code, Codex and pi, the
>   `monitor` TUI, `focus`, `status`, `doctor`, an MCP server and an optional
>   tongs plugin screen. See [What works today](#what-works-today).
> - **Coming:** the `tongs-agents workspace` command with a Zellij session and
>   sidebar, launch profiles, config emitters, agent control, agent chat and
>   sandboxing. None of these are available yet.
> - **Plan:** [agent workspace plan](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-workspace-plan.md)
>   and tracker [issue #1](https://github.com/andre-motta/tongs-agents/issues/1).
>
> Formerly `claude-fleet-monitor`; see
> [Upgrading from claude-fleet-monitor](#upgrading-from-claude-fleet-monitor).

## How it fits with tongs

[tongs](https://github.com/andre-motta/tongs) is the MR and CI terminal UI.
tongs-agents is its neighbour for agent sessions. The planned shape is:

- tongs-agents owns its own Textual sidebar app and a Zellij session in which
  Claude Code, Codex and pi run as real panes. There is no terminal emulation
  inside Textual.
- tongs is an optional neighbour tab in that Zellij session.
- A thin, optional tongs plugin (entry point `agents`) adds an agents screen
  and an "Open agent workspace" action to tongs. No tongs core changes are
  needed.
- Status comes from harness hooks. No daemon, no telemetry.

Today the plugin provides the agents screen only; the workspace, sidebar and
plugin action are part of milestone M1.

## Quick start (from source)

Requires Python 3.10 or newer and git.

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e .              # or: pip install -e ".[tongs]" for the tongs plugin
tongs-agents install          # Claude Code and Codex; add --agent pi or --agent all
```

Restart your agent sessions after the first install. Codex asks you to review
and trust the installed hooks through `/hooks` before they run. Then:

```bash
tongs-agents monitor          # live list of sessions; Enter focuses one
tongs-agents focus autofix    # jump to a session by repo, session ID or PID
```

`tongs-agents install` writes:

- Claude Code hooks to `~/.claude/settings.json` and Codex hooks to
  `~/.codex/hooks.json`.
- The `tongs-agents` MCP server through `claude mcp add --scope user` and
  `codex mcp add`, only when those CLIs are on `PATH`.
- With `--agent pi` (Pi 0.84.4 or newer), a dependency-free Pi lifecycle
  extension. It needs `pi` and an absolute `tongs-agents-hook` on `PATH`,
  keeps your other Pi extensions and registers no MCP server. See the
  [Pi integration guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/pi.md).

Session records go to `$XDG_STATE_HOME/tongs-agents/sessions/`
(`~/.local/state/tongs-agents/sessions/` by default), a private 0700
directory. Set `TONGS_AGENTS_STATE_DIR` to move it.

Runtime dependencies are `textual` and `mcp`. Terminal focus uses optional
tools when present (`qdbus`, `xdotool`, `kitten`, the `tmux`, `zellij` and
`screen` CLIs); desktop notifications use `notify-send` on Linux.

## What works today

Evidence labels: **live** (real program observed on Fedora 44 KDE Plasma
Wayland), **instrumented** (real program with tongs-agents calls recorded),
**mocked** (pytest with substituted OS calls), **inspected** (source or
documentation only) and **unvalidated** (not observed). The
[validation guide](https://github.com/andre-motta/tongs-agents/blob/main/validation/README.md)
has the reproduction commands.

| Command or part | What it does | Evidence |
| --- | --- | --- |
| `install [--agent claude\|codex\|pi\|all]` | Installs hooks and MCP registration; cleans up claude-fleet-monitor 0.9.x entries | mocked |
| `uninstall [--keep-data] [--agent ...]` | Removes hooks and MCP entries, including legacy ones; deletes session records for the default or `all` selection unless `--keep-data` | mocked |
| `monitor [--refresh N] [--notify all\|waiting\|none]` | Textual list of sessions with search, sort, status filters, details, attention jumping and focus; compact layout on narrow terminals | instrumented (headless Textual check in CI) |
| `focus <query>` | Matches exactly, then by substring, over session ID, repo and PID, and focuses the captured terminal; reports complete, partial, unavailable or failed | per terminal, see [support matrix](https://github.com/andre-motta/tongs-agents/blob/main/docs/support-matrix.md#supported-terminals) |
| In-session Zellij focus | When you run `focus` inside the agent's Zellij session, selects its pane directly | mocked |
| `status` | Plain table of agent, repo, status, detail and age | live |
| `doctor` | Read-only report of claude-fleet-monitor leftovers | live |
| `tongs-agents-hook` | Hook entry point for Claude Code, Codex and pi events | instrumented (nine shells in Podman, synthetic payloads) |
| MCP server `tongs-agents` | Five tools for agents to read and focus sessions (below) | instrumented (real server over stdio, synthetic session), mocked |
| Pi extension | Lifecycle events from pi | live process, no GUI (real Pi 0.85.0, synthetic provider, tmux) |
| tongs plugin | Palette command "Agents" opens the agents screen | mocked (CI job against tongs) |

Process discovery also finds running `claude` and `codex` processes before any
hook fires; they show as `discovered`. Session states are `started`,
`running`, `idle`, `waiting`, `error`, `ended` and `discovered`
(`status` prints them in uppercase).

Launch profiles, the `workspace` command, the sidebar, the plugin action, agent
chat, config emitters and sandboxing are in progress on other branches and are
not available.

### Monitor keys

| Key | Action |
| --- | --- |
| Enter or click | Focus the session's terminal |
| `/`, Esc | Search by repo, detail or status; clear |
| `1` `2` `3` `4`, `0` | Filter running, idle, waiting, error; show all |
| `s` | Cycle sort |
| `d` | Toggle the detail panel |
| `n`, `N` | Next or previous session needing attention |
| `o` | Open the session's directory |
| `c`, `C` | Copy a summary or the JSON record |
| `r`, `q` | Refresh; quit |

A session needs attention when it is `waiting`, or `idle` for more than two
minutes. With the default `--notify all`, `monitor` sends a `notify-send`
alert for each one (Linux only) and rings the terminal bell for waiting
sessions. `--notify waiting` alerts on waiting sessions only; `--notify none`
turns alerts off.

### MCP tools

| Tool | Description |
| --- | --- |
| `fleet_status` | All sessions with summary counts |
| `fleet_session` | One session by ID or prefix |
| `fleet_sessions_needing_attention` | Sessions waiting for input, or idle for more than 2 minutes |
| `fleet_focus` | Focus a session's terminal |
| `fleet_cleanup` | Remove ended session records older than 5 minutes |

### tongs plugin

With `pip install -e ".[tongs]"`, tongs finds the plugin under the
`tongs.plugins` entry point `agents`. Open the command palette (Ctrl+P) and
choose "Agents". Known issue: the plugin still reads its `refresh_interval`
and `notify_level` settings from `[plugins.fleet-monitor]` instead of
`[plugins.agents]`.

## Harness support

| Harness | Status source | Process discovery | MCP registration |
| --- | --- | --- | --- |
| Claude Code | Native hooks | Yes, by exact process name | Yes, when the CLI is found |
| Codex | Native hooks | Yes, by exact process name | Yes, when the CLI is found |
| Pi | Optional extension | No; the extension reports the exact PID | None |

Terminal focus support differs by backend. Konsole, tmux, Zellij, Kitty and
GNU screen are validated live on Linux; Ghostty, GNOME Terminal, iTerm2, macOS
Terminal and Windows Terminal are mocked or inspected only; Terminator is
detection only. The full harness and terminal matrices, with evidence for each
claim, nested terminal routing, Kitty remote-control setup and known limits,
are in the
[support matrix](https://github.com/andre-motta/tongs-agents/blob/main/docs/support-matrix.md).

## Roadmap

From the [agent workspace plan](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-workspace-plan.md):

- **M0, release safety foundations:** upgrade adoption, publish hardening, plan record, harness facts. Merged.
- **M1, thin usable workspace:** runtime rename, agents panel, multiplexer protocol and pinned Zellij CI merged; launch profiles, `tongs-agents workspace`, sidebar and plugin action in progress.
- **M2, config layer and app shell:** config emitters for Claude Code, Codex and pi, owned-file writer, managed Zellij download. In progress.
- **M3, multiplexer completeness and agent control:** send, paste and capture, a tmux adapter, opt-in audited MCP control. Planned.
- **M4, session store hardening:** read-only readers and one cleanup owner. Planned.
- **M5, agent chat:** off by default, granted per session. In progress.
- **M6, release v0.10.0 and sunset:** first PyPI release with M0 to M5, then the claude-fleet-monitor sunset. Planned.
- **M7, sandboxed agents with OpenShell:** sandboxed profiles, policies and approvals in the sidebar. Later.

## Upgrading from claude-fleet-monitor

Install tongs-agents from source, then run `tongs-agents install` (add
`--agent all` if you used Pi). It removes the claude-fleet-monitor hooks, its
`fleet` MCP entries and its Pi extension, and keeps everything else. Then run
`pip uninstall claude-fleet-monitor` and `tongs-agents doctor`, which reports
anything left over, including the old `~/.claude/fleet/` directory and the
command to remove it. Old session records are not migrated; they go stale
within minutes. Issue and PR numbers from before the rename refer to the
[claude-fleet-monitor tracker](https://github.com/andre-motta/claude-fleet-monitor/issues).
See the [migration guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/migration.md).

To remove tongs-agents:

```bash
tongs-agents uninstall              # hooks, MCP entries and session records
tongs-agents uninstall --keep-data  # keep the sessions directory
tongs-agents uninstall --agent pi   # only the Pi extension tongs-agents owns
```

## Contributing

See [CONTRIBUTING.md](https://github.com/andre-motta/tongs-agents/blob/main/CONTRIBUTING.md)
for development setup and the PR process. Harness contributors should follow
the [adapter author guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/harness-adapters.md)
and the [harness contract](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/harness-contract.md).
Agent-led initiatives follow the [SDLC profile](https://github.com/andre-motta/tongs-agents/blob/main/docs/SDLC.md).

## License

[MIT](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)
