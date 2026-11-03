# Changelog

## 0.10.2 (experimental)

Fixes from using the one-agent view of 0.10.1 with many agents.

### Added

- Rename an agent from the sidebar: `R` on a row opens a prompt prefilled
  with its current name; Enter saves, Escape cancels and an empty name
  restores the profile name. The name is a display label kept in the launch
  record (`label`), shown in the sidebar row, `agent-status` and the MCP
  tools that list agents, and given to the Zellij or tmux pane, best effort.
  It never changes the profile, harness, session id or command. Rows not
  started in this workspace, and agents still starting, are refused. A
  resume from the sidebar keeps the name (#130).

### Fixed

- The Zellij one-agent view no longer lists every agent as a title line above
  the one in view. Agents now open as pinned floating panes laid exactly over
  the `tongs-agents-home` pane: the selected one is raised and stays shown
  while the sidebar has the focus, the sidebar is the only list, and there is
  no longer a limit on the number of agents (`the agent stack is full` is
  gone). If the home pane is closed, agents float right of the sidebar, or
  over its right three quarters with a notice when the sidebar fills the tab.
  A session created by 0.10.1 keeps its stacked column and new agents float
  over it; start a new workspace session to get the new view (#124).
- The Zellij sidebar can be resized again: it starts at a quarter of the
  width instead of a fixed 40 columns. When its pane changes size (the
  sidebar or the terminal was resized), it moves the agents it launched back
  over the home pane.
- Typed text in the sidebar `:` command line is visible again (#129).

## 0.10.1 (experimental)

The first round of feedback from using 0.10.0 every day: start empty, add
agents on demand, see one agent at a time, and bring open sessions in.

### Changed

- A bare `tongs-agents` now opens the workspace with only the sidebar (and
  the tongs tab), or attaches to it, instead of printing help. It never
  starts agents, not even `[workspace].default_profiles`; the monitor stays
  available as `tongs-agents monitor` (#123).
- Profiles are templates: `cwd` is only the default folder of a profile.

### Added

- `tongs-agents run PROFILE [DIR] [-- ARGS...]` adds one agent pane to the
  workspace: in the current Zellij or tmux session, in the running workspace
  session from another terminal, or in a new workspace when none runs. DIR
  defaults to the current directory, and everything after `--` (such as
  `--resume`) is appended to the profile's command for that launch only. The
  folder and arguments are kept in the launch record and checked again by
  the launcher; sandboxed profiles keep their upload rules. MCP
  `agents_spawn` still takes neither (#123).
- The sidebar's Add agent dialog (`+`, or `Add agent…` in the command
  palette): a profile picker, a folder field with directory completion and
  an arguments field, with the resulting command shown before it starts.
- `tongs-agents --version`.
- One agent at a time (`[workspace] layout = "single"`, the default): the
  sidebar on the left and one agent in the main area. On Zellij agents share
  a stacked column whose collapsed title lines act as tabs, with a capacity
  check and automatic recovery of the stack's home pane; on tmux each agent
  lives in its own window and is swapped into the main slot.
  `layout = "split"` keeps the previous side-by-side panes (#124).
- A command line in the sidebar: `:` opens a prompt for the workspace verbs
  (`run`, `resume`) with the CLI's syntax, history and Tab completion (#123).
- `tongs-agents resume` lists recent sessions and starts one in the workspace
  with the matching profile, folder and resume arguments (Claude Code
  `--resume <id>`, Codex `resume <id>`); the sidebar has Resume in workspace
  (`u`) and a picker. A session already running in this workspace is
  refused, one running elsewhere needs confirmation, and nothing is ever
  killed (#125).

### Fixed

- Zellij 0.44.2 sometimes answered "no active session" when creating a pane;
  pane creation now retries that answer like the read-only queries (#107).

## 0.10.0 (experimental)

The first release of tongs-agents on PyPI, published as an experimental
release: OpenShell sandboxes, Codex `PreToolUse` handling, Pi chat delivery,
harness tool-list refresh on chat grants, and `claude mcp add` and
`pi mcp add` against the real CLIs are not yet validated against real
harnesses. Report problems at
<https://github.com/andre-motta/tongs-agents/issues>. It replaces claude-fleet-monitor
0.9.x and ships milestones M0 to M7 of the
[agent workspace plan](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-workspace-plan.md)
together. Upgrading from claude-fleet-monitor? Read
[Upgrading from claude-fleet-monitor](https://github.com/andre-motta/tongs-agents/blob/main/docs/migration.md)
first: the package, commands, MCP key, state directory and tongs entry point
are all renamed, and nothing is migrated automatically.

Install it with `pipx install tongs-agents` (or `pip install tongs-agents`),
then run `tongs-agents install`.

### Changed (breaking)

- The project is renamed from claude-fleet-monitor to tongs-agents. The
  distribution is `tongs-agents`, the Python module is `tongs_agents`, and the
  commands are `tongs-agents` and `tongs-agents-hook` (before: `claude-fleet`
  and `claude-fleet-hook`).
- Python 3.11 or newer is required (before: 3.10).
- textual must be 4.x to 8.x (`textual>=4,<9`; before: `textual>=1.0`).
- The tongs plugin entry point is renamed from `fleet-monitor` to `agents`.
  Move `[plugins.fleet-monitor]` settings in your tongs config to
  `[plugins.agents]`; the old section is ignored.
- The MCP server key is `tongs-agents` (before: `fleet`). Claude Code gets it
  through `claude mcp add --scope user` instead of `mcpServers` in
  `settings.json`, which Claude Code does not load. The `fleet_*` tool names
  are unchanged.
- Session records move from `~/.claude/fleet/` to
  `$XDG_STATE_HOME/tongs-agents/sessions/` (by default
  `~/.local/state/tongs-agents/sessions/`, a private 0700 directory).
  `TONGS_AGENTS_STATE_DIR` moves the whole state directory; `FLEET_DIR` is no
  longer read. Old records are not migrated.
- Pi runtime names change with the rest: config `tongs-agents.json` with
  owner `tongs-agents`, runtime symbol `tongs-agents.pi.runtime.v1` and
  package `tongs-agents-pi`.
- `tongs-agents install` and `uninstall` remove claude-fleet-monitor hook
  commands, its `fleet` MCP entries (only the forms it wrote) and its Pi
  extension, and keep everything else. `~/.claude/fleet/` is never deleted;
  `doctor` reports it with the command to remove it.

### Added

- M0, release safety: install adopts and cleans up claude-fleet-monitor
  0.9.x installs, and the PyPI publish workflow runs only after every test and
  validation gate passes on a tag that is on `main`, with hash-pinned build
  tools and a strict `twine check`.
- M1, the agent workspace:
  - agent profiles in `~/.config/tongs-agents/config.toml`: any argv command
    with its own config dir, cwd, env and model arguments (`profiles`);
  - `workspace` opens a Zellij session with one pane per profile and a status
    sidebar (`sidebar`); Enter on a row focuses that pane, `n` jumps to the
    next agent that needs you. A rerun inside the session adds panes instead
    of nesting. An unused spawn request expires after 24 hours (#66);
  - `install` covers every profile's config dir, and `doctor` reports what is
    missing with its fix (`--json`, `--config-dir`);
  - `notify` reports status from scripts and agents without hooks;
  - launch identity in hook records, so each status row is tied to the pane
    and process that tongs-agents started;
  - the tongs "Open agent workspace" action, with the `[tongs]` extra.
- M2, config layer and app shell:
  - `config status`, `config apply` and `config capture` emit one dotfiles
    directory of instructions, skills and MCP servers into every Claude Code,
    Codex and Pi config home. Edits go through an owned-file writer that keeps
    a one-time `<name>.tongs-agents.bak` copy and rolls back on failure;
  - `[workspace]`, `[ui]` and `[dotfiles]` app settings, a command palette
    (`Ctrl+P`) and a theme shared by `monitor` and the sidebar;
  - a pinned, checksum-verified Zellij download for Linux x86_64:
    `doctor --fix zellij` and `zellij install`. These, and `zellij install
    --web`, are the only commands that open a network connection.
- M3, multiplexers and agent control:
  - tmux 3.2 or newer as a second workspace backend;
  - `send`, `read`, `wait` and `agent-status` drive launched agent panes from
    a script, refusing unverified or stale panes;
  - opt-in MCP agent control (`agents_spawn`, `agents_send`, `agents_status`,
    `agents_wait`), off unless `[control] mcp_enabled = true` and the profile
    sets `mcp_control = true`.
- M4, session store: readers never write; one maintainer (`cleanup`, the
  `fleet_cleanup` MCP tool) removes ended, exited and stale records.
- M5, chat and web watching:
  - `chat`: a chat between you and the agent panes you grant, one pane at a
    time, with mentions delivered through hooks and wake, and three MCP chat
    tools that exist only for granted sessions;
  - `web`: read-only watching of the workspace from a browser or phone through
    the pinned Zellij web build, off until `web enable` and bound to
    127.0.0.1 only.
- M7, sandboxed agents (instrumented against a fake OpenShell CLI; the live
  gateway run is pending): a profile `sandbox` table runs its agent in an
  NVIDIA OpenShell sandbox as a normal pane; `sandbox` commands (setup wizard,
  list, pull, policy, rules); access requests approved or rejected one at a
  time in the sidebar.
- Documentation: a new README, a documentation index, tutorials, how-to
  guides, reference pages and a support matrix that labels each claim live,
  instrumented, mocked or inspected.

### Changed

- `tongs-agents monitor --refresh` is stricter: it takes a number of seconds
  from 0.5 to 3600, the same range as `[ui].refresh_interval`. Before, 0 and
  larger values were accepted (#94).
- `tongs-agents --help` lists the module commands sorted by command name, not
  by the name of the module file that defines them (#90).
- The tongs "Open agent workspace" modal says the command is already
  prepared ("Prepared; run this command in a terminal:"). Inside Zellij, the
  result is shown as a notification also when the modal was closed before it
  finished (#66).

### Removed

- Python 3.10 support.
- The `claude-fleet` and `claude-fleet-hook` commands and the
  `claude_fleet_monitor` module.
- The `FLEET_DIR` environment variable and the `fleet` MCP key.
