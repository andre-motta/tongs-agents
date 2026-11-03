# tongs-agents

[![Tests](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/andre-motta/tongs-agents/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)

tongs-agents shows your Claude Code, Codex and pi sessions in one list, tells
you which one needs input, and takes you to its terminal. It reads status from
each harness's own hooks, so it works whichever harness an agent runs in. It is
growing into a Zellij agent workspace (in progress). It is a standalone,
harness-agnostic companion app in the
[tongs](https://github.com/andre-motta/tongs) family: it runs on its own, and
tongs is optional.

> **Status: pre-release.** tongs-agents is not on PyPI yet; install it from
> source. v0.10.0 will be the first release and will ship milestones M0 to M7
> together (no date).
>
> - **Works today:** hook-based status for Claude Code, Codex and pi, the
>   `monitor` TUI, `focus`, `status`, `doctor`, an MCP server, an optional
>   tongs plugin screen, and from source the `workspace` command with its
>   Zellij sidebar and launch profiles (instrumented evidence; the live run is
>   pending). See [What works today](#what-works-today).
> - **Coming:** config emitters, agent control, agent chat and sandboxing.
>   None of these are available yet.
> - **Plan:** [agent workspace plan](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-workspace-plan.md)
>   and tracker [issue #1](https://github.com/andre-motta/tongs-agents/issues/1).
>
> Formerly `claude-fleet-monitor`; see
> [Upgrading from claude-fleet-monitor](#upgrading-from-claude-fleet-monitor).

## How it fits with tongs

[tongs](https://github.com/andre-motta/tongs) is the MR and CI terminal UI.
tongs-agents works without it. Today, with the `[tongs]` extra, tongs gets an
Agents screen (see [tongs plugin](#tongs-plugin)).

The planned shape, part of milestone M1 and not available yet:

- tongs-agents owns its own Textual sidebar app and a Zellij session in which
  Claude Code, Codex and pi run as real panes. There is no terminal emulation
  inside Textual.
- tongs is an optional neighbour tab in that Zellij session.
- A thin, optional tongs plugin (entry point `agents`) adds an agents screen
  and an "Open agent workspace" action to tongs. No tongs core changes are
  needed.
- Status comes from harness hooks. No daemon, no telemetry.

## Quick start (from source)

Requires Python 3.11 or newer and git.

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e .              # or: pip install -e ".[tongs]" for the tongs plugin
tongs-agents install          # Claude Code and Codex; then --agent pi for Pi, or use --agent all
```

Restart your agent sessions after the first install. Codex asks you to review
and trust the installed hooks through `/hooks` before they run. Then:

```bash
tongs-agents monitor          # live list of sessions; Enter focuses one
tongs-agents focus my-repo    # jump to a session by repo, session ID or PID
```

`tongs-agents install` writes:

- Claude Code hooks to `~/.claude/settings.json` and Codex hooks to
  `~/.codex/hooks.json`.
- The `tongs-agents` MCP server through `claude mcp add --scope user` and
  `codex mcp add`, only when those CLIs are on `PATH`.
- With `--agent pi` (Pi 0.84.4 or newer), a dependency-free Pi lifecycle
  extension. It needs `pi` on `PATH` and `tongs-agents-hook` resolvable to
  an absolute path, keeps your other Pi extensions and registers no MCP
  server. See the
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
| `uninstall [--keep-data] [--agent ...]` | Removes hooks and MCP entries, including legacy ones; deletes session records for the default or `all` selection unless `--keep-data`; session records are kept while the Pi extension is installed | mocked |
| `monitor [--refresh N] [--notify all\|waiting\|none]` | Textual list of sessions with search, sort, status filters, details, attention jumping and focus; compact layout on narrow terminals | instrumented (headless Textual check in CI) |
| `focus <query>` | Matches exactly, then by substring, over session ID, repo and PID, and focuses the captured terminal; reports complete, partial, unavailable or failed, or lists the candidates when several sessions match | per terminal, see [support matrix](https://github.com/andre-motta/tongs-agents/blob/main/docs/support-matrix.md#supported-terminals) |
| In-session Zellij focus | When you run `focus`, or select a sidebar row, inside the agent's Zellij session, selects its pane directly and never activates a window | instrumented (sidebar row select with a second client attached, `list-clients` readback), mocked |
| `workspace [--profile NAME]... [--session NAME] [--no-tongs]` | Opens a Zellij session with the sidebar and one pane per [profile](https://github.com/andre-motta/tongs-agents/blob/main/docs/profiles.md); inside Zellij it adds panes to the current session without nesting | instrumented ([workspace validator](https://github.com/andre-motta/tongs-agents/blob/main/validation/README.md#agent-workspace-on-linux) on Zellij 0.44.2 and 0.45.1 with fake agents, hosted CI); live run pending |
| `sidebar` | Runs in the workspace: consumes the spawn request once, lists each launched pane with hook status or pane state | instrumented (same validator) |
| Launch profiles and `profiles` | Named argv commands with their own config dir, cwd, env and model arguments | instrumented (fixture profiles with private config dirs), mocked |
| `install --claude-config-dir DIR` (also `--codex-home`, `--pi-agent-dir`) | Installs hooks into one harness config dir and leaves the default untouched | instrumented for Claude (a fixture in that dir reports hook status), mocked |
| `status` | Plain table of agent, repo, status, detail and age; read only, and suggests `cleanup` when no sidebar or monitor has maintained the store in the last hour | unvalidated |
| `cleanup` | Runs session store maintenance now (the sidebar and `monitor` already do while they run): records agent processes no hook reported and removes ended, exited and stale records; exits 1 when another maintainer holds the lock or the store is unavailable ([session store](https://github.com/andre-motta/tongs-agents/blob/main/docs/sessions.md)) | mocked |
| `doctor` | Read-only report of claude-fleet-monitor leftovers | mocked |
| `tongs-agents-hook` | Hook entry point for Claude Code, Codex and pi events | instrumented (nine shells in Podman, synthetic payloads) |
| MCP server `tongs-agents` | Five tools for agents to read and focus sessions (below) | instrumented (real server over stdio, synthetic session), mocked |
| Pi extension | Lifecycle events from pi | instrumented (real Pi 0.85.0 in hosted CI, synthetic provider, tmux pane selection; no GUI) |
| tongs plugin | Palette command "Agents" opens the agents screen | mocked (CI job against tongs) |

Process discovery also finds running `claude` and `codex` processes before any
hook fires; they show as `discovered`. Session states are `started`,
`running`, `idle`, `waiting`, `error`, `ended` and `discovered`
(`status` prints them in uppercase).

The plugin action, agent chat, config emitters and sandboxing are in progress
on other branches and are not available.

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
| `fleet_cleanup` | Run session store maintenance now: record agent processes no hook reported and remove ended, exited and stale records; replies `{"removed": N}`, plus `"skipped"` when another maintainer holds the lock or the store is unavailable ([session store](https://github.com/andre-motta/tongs-agents/blob/main/docs/sessions.md)) |

### tongs plugin

With `pip install -e ".[tongs]"`, tongs finds the plugin under the
`tongs.plugins` entry point `agents`. Open the command palette (Ctrl+P) and
choose "Agents". Its `refresh_interval` and `notify_level` settings live in
`[plugins.agents]` in the tongs config.

## Harness support

| Harness | Status source | Process discovery | MCP registration |
| --- | --- | --- | --- |
| Claude Code | Native hooks | Yes, by exact process name | Yes, when the CLI is found |
| Codex | Native hooks | Yes, by exact process name | Yes, when the CLI is found |
| Pi | Optional extension | No; the extension reports the exact PID | None |

The agent workspace needs Zellij 0.44.2 or newer on Linux x86_64 (0.44.2 and
0.45.1 are validated, instrumented; the maintainer's live run is pending);
macOS and Linux aarch64 are unvalidated and Windows is unsupported. Profiles
with their own Claude, Codex or pi config dir report hook status once
`install` has written hooks into that dir. See
[Agent workspace, multiplexers and profiles](https://github.com/andre-motta/tongs-agents/blob/main/docs/support-matrix.md#agent-workspace-multiplexers-and-profiles).

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
- **M1, thin usable workspace:** Zellij spike, runtime rename, agents panel, multiplexer protocol, pinned Zellij CI, launch profiles, `tongs-agents workspace`, sidebar, plugin action and the instrumented workspace validator merged; the maintainer's live run is pending.
- **M2, config layer and app shell:** owned-file writer, `config status`/`apply`/`capture`, emitters for Claude Code, Codex and pi, managed Zellij download and `notify` merged; app shell in progress.
- **M3, multiplexer completeness and agent control:** send, paste and capture, a tmux adapter, opt-in audited MCP control. In progress.
- **M4, session store hardening:** read-only readers and one cleanup owner. Planned.
- **M5, agent chat:** off by default, granted per session. Design in progress.
- **M7, sandboxed agents with OpenShell:** sandboxed profiles, policies and approvals in the sidebar. Investigation in progress; built before the release.
- **M6, release v0.10.0 and sunset:** first PyPI release with M0 to M7, then the claude-fleet-monitor sunset. Planned.

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

## Uninstall

```bash
tongs-agents uninstall              # hooks, MCP entries and session records
tongs-agents uninstall --keep-data  # keep the sessions directory
tongs-agents uninstall --agent pi   # only the Pi extension (and any claude-fleet-monitor Pi leftovers)
```

Session records are kept while the Pi extension is still installed. Then run
`pip uninstall tongs-agents`.

## Contributing

See [CONTRIBUTING.md](https://github.com/andre-motta/tongs-agents/blob/main/CONTRIBUTING.md)
for development setup and the PR process. Harness contributors should follow
the [adapter author guide](https://github.com/andre-motta/tongs-agents/blob/main/docs/harness-adapters.md)
and the [harness contract](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/harness-contract.md).
Agent-led initiatives follow the [SDLC profile](https://github.com/andre-motta/tongs-agents/blob/main/docs/SDLC.md).

## License

[MIT](https://github.com/andre-motta/tongs-agents/blob/main/LICENSE)
