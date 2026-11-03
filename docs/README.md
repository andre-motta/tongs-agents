---
title: "tongs-agents documentation"
description: "Tutorials, how-to guides, reference and background for tongs-agents, grouped by what you want to do."
---

tongs-agents runs your AI coding agents (Claude Code, Codex, Pi or any
command) as panes of one Zellij or tmux session, with a sidebar that shows
which one needs you. These pages are grouped by what you want to do:

- **Tutorials** teach by doing. Follow one from the top on a fresh machine.
- **How-to guides** answer one question each, for when you know what you
  want.
- **Reference** pages list every option, file and exit code.
- **Background** pages explain why things work the way they do.

New here? Start with [your first workspace](tutorials/first-workspace.md).

## Tutorials

1. [Your first workspace](tutorials/first-workspace.md): install, profiles
   for two Claude Code config dirs, Codex and Pi, `install`, `doctor`,
   `workspace` and the sidebar.
2. [Any command as a pane](tutorials/any-command.md): profiles with harness
   `other`, and status from scripts with `tongs-agents notify`.
3. [One dotfiles directory for every harness](tutorials/dotfiles.md):
   instructions, skills and MCP servers with `config status`, `apply` and
   `capture`.
4. [Driving agents](tutorials/driving-agents.md): `send`, `read` and `wait`
   on a free shell pane, then opt-in MCP agent control.
5. [Agent chat and web watching](tutorials/chat-and-web.md): grants,
   mentions and wake; watching the workspace read-only from a phone.
6. [Sandboxed agents with OpenShell](tutorials/sandbox.md): the setup
   wizard, a sandboxed profile in the workspace, the `sandbox` commands, and
   approving or rejecting access requests in the sidebar (unvalidated against
   a live gateway).
7. [tmux instead of Zellij](tutorials/tmux.md): the same workspace on tmux,
   and what differs.

## How-to guides

| Task | Page |
| --- | --- |
| Install tongs-agents, and see what `install` changes in harness files | [Installing](install.md) |
| Define agents with their own config dir, cwd, env or model | [Agent profiles](profiles.md) |
| Report status from a script or an agent without hooks | [Reporting state from any command](profiles.md#reporting-state-from-any-command) |
| Open, reopen or resurrect a workspace | [Agent workspace](workspace.md) |
| Set defaults for the workspace, the sidebar and the monitor; pick a theme | [App settings](app.md) |
| Keep instructions, skills and MCP servers in one dotfiles directory | [Config layer](config.md) |
| Send prompts to agents from a script | [Driving agent panes](control.md) |
| Let an agent start and prompt other agents | [MCP agent control](mcp-control.md) |
| Talk to agents and let them talk to each other | [Agent chat](chat.md) |
| Watch the workspace from a browser or phone | [Web watching](web.md) |
| Run an agent in an NVIDIA OpenShell sandbox | [Sandboxed profiles](sandbox.md), [tutorial](tutorials/sandbox.md) |
| Use Pi | [Pi lifecycle integration](pi.md) |
| Install a pinned Zellij build | [Zellij pins and the managed download](zellij-pins.md#managed-download) |
| Move from claude-fleet-monitor | [Upgrading from claude-fleet-monitor](migration.md) |

## Reference

| Topic | Page |
| --- | --- |
| Every `config.toml` section and key | [Profiles](profiles.md#keys), [app settings](app.md), [control](mcp-control.md#turning-it-on), [dotfiles path](config.md#choosing-the-dotfiles-directory), [sandbox](sandbox.md#profile-keys) |
| `workspace` options and runtime files | [Agent workspace](workspace.md) |
| `send`, `read`, `wait`, `agent-status` and their exit codes | [Driving agent panes](control.md#exit-codes) |
| `config` verbs, entry states and exit codes | [Config layer](config.md#verbs) |
| `chat` verbs, MCP chat tools, limits and storage | [Agent chat](chat.md) |
| `web` verbs | [Web watching](web.md#commands) |
| `sandbox` commands, the setup wizard and access requests | [Sandboxed profiles](sandbox.md#managing-sandboxes) |
| Session records, readers and cleanup | [Session store](sessions.md) |
| What is supported, with the evidence for each claim | [Support matrix](support-matrix.md) |
| The Zellij pin table | [Zellij pins](zellij-pins.md) |

Every command also prints its options with `--help`, for example
`tongs-agents workspace --help`.

## Background

- [Support matrix](support-matrix.md): what "live", "instrumented",
  "mocked", "inspected" and "unvalidated" mean, and which label each feature
  has.
- [Harness adapter guide](harness-adapters.md): how a harness reports status,
  for contributors adding one.
- [Session store](sessions.md): why there is no daemon, and who may change
  the store.
- [Agent chat](chat.md#treat-chat-text-as-data): why chat text is data, not
  instructions.

The design records under [work/](https://github.com/andre-motta/tongs-agents/tree/main/docs/work/) are the project's internal history
(plans, decisions and evidence). They are not user documentation and may
describe earlier states of the code.
