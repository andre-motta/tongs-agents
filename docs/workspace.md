---
title: "Agent workspace"
description: "Open a Zellij or tmux session with the tongs-agents sidebar, then add agent panes in any folder with tongs-agents run or the sidebar."
---

The workspace is a Zellij (or tmux) session with the tongs-agents sidebar.
It starts empty; you add agents to it as you need them:

```console
tongs-agents                                        # open or attach: only the sidebar
tongs-agents run claude-main ~/src/app              # add an agent pane in a folder
tongs-agents run codex-review -- resume --last      # in the current directory, extra args
```

A bare `tongs-agents` runs `tongs-agents workspace` with no profiles, not even
`[workspace].default_profiles`: a new session gets the sidebar (and the
`tongs` tab when `tongs` is on `PATH` and `[workspace].tongs_tab` is not
false), and a running one is attached to. In the sidebar, `+` opens the
[Add agent](#add-agent-in-the-sidebar) dialog. `tongs-agents monitor` is still
the fleet-wide view, and `tongs-agents --version` prints the version.

## One agent at a time

The workspace shows the sidebar on the left and one agent beside it, like
terminal tabs. Selecting a row (Enter or a click), `tongs-agents run`, a
palette `New agent` entry and the Add agent dialog bring that agent into the
main area; the others keep running out of view, keep reporting status, and
`send`, `read`, `wait` and chat still reach them.

- **Zellij**: the main area is a stacked column headed by a
  `tongs-agents-home` pane that shows a short hint. Every agent joins the
  stack; the one in view is expanded and the others are collapsed to their
  title lines (framed on 0.44.2, a compact list marked with `>` on 0.45.1),
  which you can also click or reach with Zellij's own pane keys. The home
  pane anchors the stack. If you close it, the next agent started brings it
  back: the home pane is opened again and stacked onto an agent of the
  column (when that does not work, the agent pane itself anchors the stack).
  Only when every pane of the column is gone too does the new agent open
  side by side, with a notice saying so (a warning in the sidebar, a
  `notice:` line under its command line, a line on stderr for `run`,
  `notices` in an MCP `agents_spawn` reply).
- **tmux**: the sidebar's window holds the sidebar and one agent, the main
  slot. Every other agent is parked in its own window (shown in the status
  bar under the agent's name) and is swapped into the main slot when you
  select it.

`layout = "split"` in `[workspace]` keeps the earlier layout instead, with
every agent side by side next to the sidebar:

```toml
[workspace]
layout = "split"     # "single" (default): one agent at a time
```

The layout is chosen when a session is created and stays with it: Zellij
reads it from the home pane, and from the sidebar pane's `--layout single`
argument once the home pane is closed; tmux reads the session option
`@tongs_agents_layout`. Changing the setting affects the next new session;
a session created by an older tongs-agents, or a Zellij or tmux session the
workspace did not create, keeps placing panes side by side. On Zellij, MCP
`agents_spawn` panes join the stack like any other, and on tmux they are
parked without changing the view.

A Zellij stack fits the window's height minus five agents (16 in a 24-row
terminal, the home pane included). When it is full, `run`, the dialog and
the palette refuse with `the agent stack is full`; close an agent pane or
make the terminal taller. A pane that Zellij does not place in the stack is
closed again rather than left elsewhere.

## Adding agents: run

```console
tongs-agents run [--session NAME] [--multiplexer {zellij,tmux}] PROFILE [DIR] [-- ARGS ...]
```

A [profile](profiles.md) is a template: its command, harness, config dir,
model arguments and environment. `run` picks the rest for this one launch:

| Part | Meaning |
| --- | --- |
| `PROFILE` | The profile to start. Unknown names are refused (exit 2). |
| `DIR` | The folder the agent starts in. Relative paths and `~` are resolved by `run`. Without it, the current directory; the profile's `cwd` is used only when the current directory cannot be read. The folder must exist and be a directory, and for a [sandboxed profile](sandbox.md) that uploads its workspace it must not be `/`, your home folder, a folder holding it or a hidden folder in it. |
| `-- ARGS` | Everything after `--` is appended to the profile's command for this launch only, as separate arguments and never through a shell: `-- --resume`, `-- resume --last`, `-- "fix the failing test"`. At most 64 arguments of up to 4096 characters each (16384 in all), with no NUL. An option before `--`, such as `run claude-main --resume`, is a usage error. |
| `--session`, `--multiplexer` | The session and multiplexer to use when `run` is started outside any workspace; the defaults come from `[workspace]`. Inside a session they must name that session and multiplexer or be left out. |

The folder and arguments are written into the launch record (0600, in the
state directory); the launcher checks the folder again before it starts the
agent, changes to it and execs the profile command, then `--no-daemon` for
Codex (unless present), the profile's `model_args` and `effort_args`, then
the extra arguments. Launch identity, status, pane control and chat grants
work as for any other pane. MCP `agents_spawn` never takes a folder or
arguments: only `run` and the sidebar dialog, both run by you, set them.

| Where you run it | Result |
| --- | --- |
| Inside Zellij or tmux | Adds the pane to the current session (and a sidebar if no live one is listed). It never nests or execs. |
| Outside, the workspace session is running | Adds the pane to it and returns, so a terminal already attached shows it; the notice names the command that attaches. |
| Outside, no session | Starts the workspace with the sidebar and this one pane, exactly like `tongs-agents workspace` would for a new session (an EXITED Zellij session of that name is deleted first), then attaches. |

On Windows `run` prints that the workspace is unsupported and exits 2.

## Add agent in the sidebar

`+` in the sidebar, or `Add agent…` in its command palette (Ctrl+P), opens a
dialog with:

- a profile picker, `[workspace].default_profiles` first;
- a folder field that starts at the profile's `cwd`, else the folder the
  sidebar started in, and completes directory names as you type (Right arrow
  accepts the suggestion);
- an optional arguments field, split like a shell line (`--resume 'two
  words'` is two arguments) but never run through a shell.

Below the fields the dialog shows the command it will run and the folder.
Start checks both with the same rules as `run`; a missing folder, a refused
sandbox folder or unbalanced quotes keep the dialog open with the reason.
Escape cancels. The palette's `New agent: <profile>` entries still start a
profile with its own defaults in one step.

`:` in the sidebar opens a command line that takes the same
`run PROFILE [DIR] [-- ARGS ...]` as the CLI, for the sidebar's session, with
history and Tab completion of profiles and folders; see
[sidebar command line](app.md#sidebar-command-line).

## The workspace verb

`tongs-agents workspace` opens a Zellij session with the tongs-agents sidebar
and one pane per requested [profile](profiles.md), started in its `cwd`. It needs Zellij 0.44.2 or
newer (0.45.1 is the newest validated release) on Linux or macOS; on Windows
it prints that the workspace is unsupported and exits 2. With
`--multiplexer tmux` it uses tmux 3.2 or newer instead; see
[tmux](#tmux).

```console
tongs-agents workspace --profile claude-alt --profile codex-review
tongs-agents workspace --session work --profile pi-local --no-tongs
tongs-agents workspace --resurrect
tongs-agents workspace --print-command --profile claude-alt
tongs-agents workspace --print-alias --profile claude-alt
tongs-agents workspace --multiplexer tmux --profile claude-alt
```

| Option | Meaning |
| --- | --- |
| `--profile NAME` | Start an agent pane for this profile. Repeatable. Unknown names are refused before anything starts. Without it a new session starts `[workspace].default_profiles` (none by default) and an existing one gets only the sidebar. |
| `--multiplexer {zellij,tmux}` | The multiplexer that hosts the workspace: `[workspace].multiplexer` from the config file, else `zellij`. See [tmux](#tmux). |
| `--session NAME` | The Zellij session: `[workspace].session` from the config file, else `agents`. Names use letters, digits, `.`, `_` and `-` (at most 64 characters). |
| `--tongs` / `--no-tongs` | Add, or do not add, the `tongs` tab to a new session. Without either, `[workspace].tongs_tab` decides (on by default) and the tab opens only when `tongs` is on `PATH`. |
| `--resurrect` | When the session is EXITED, attach to it instead of deleting it and starting it fresh. Starts no new panes; without an EXITED session it changes nothing. |
| `--print-command` | Print the `zellij` command instead of running it. See [Printing the command](#printing-the-command). |
| `--print-alias` | Print a shell alias for the same workspace command. Nothing else runs and no file is edited; add the line to your shell rc yourself. |

When `zellij` is missing or older than 0.44.2 the command stops and points at
`tongs-agents doctor`.

The flags override the `[workspace]` section of the config file; an invalid
section, or a default profile that does not exist, stops the command with the
offending key before anything starts. See [app settings](app.md).

## What it does

Every agent pane runs the launcher,
`<python> -m tongs_agents.launch <profile> --launch-id <id>`, for a pending
launch record created just before; the launcher claims the record and execs
the profile command. Panes are named after their profile.

| Where you run it | Result |
| --- | --- |
| Inside Zellij (`ZELLIJ` is set) | Spawns the profiles into the current session, shows each in turn, and adds a sidebar pane if no live one is listed. It never execs. A different `--session` is refused, and so is `--print-command`; `--resurrect` is ignored with a notice. |
| Outside Zellij, session running | Spawns the profiles into that session (and a sidebar if it has none), then runs `zellij attach <session>`. |
| Outside Zellij, no session | Writes a spawn request and a layout (`[workspace].layout`), then runs `zellij --session <session> --new-session-with-layout <layout>`. The sidebar in that layout spawns the profiles when it starts. |
| Outside Zellij, session EXITED | Prints a one-line notice, runs `zellij delete-session <session>`, then creates it as above. With `--resurrect` it runs `zellij attach <session>` instead and starts nothing new. |

### Second run: EXITED sessions

Zellij serializes sessions by default, so quitting the workspace leaves the
session listed as `EXITED - attach to resurrect`, and Zellij refuses to create
a new session with that name. The workspace checks
`zellij list-sessions --no-formatting` (with the same `ZELLIJ_SOCKET_DIR`)
before it writes anything. For an EXITED session of the requested name it
deletes exactly that session, without `--force`, and starts it fresh; a
running session or any other session is never deleted. If the delete fails,
nothing is written and the command exits 1.

`--resurrect` keeps the EXITED session instead: Zellij restores its layout
with every command pane suspended until you press Enter in it, and requested
profiles are not started (a notice names them). Agents are not restarted.

### Failures

Launch records, the spawn request and the layout written for a new session
are removed again when any later step fails before Zellij takes over: writing
the request, rendering or writing the layout, or running `zellij`. When a
profile pane cannot be spawned into a running session, its record is removed;
panes already started stay up.

Zellij is started without `TMUX`, `TMUX_PANE` or an outer Zellij pane identity
in its environment, so agent panes never address an outer multiplexer.
`ZELLIJ_SOCKET_DIR` and `ZELLIJ_CONFIG_DIR` are kept.

## Runtime files

Both live in the runtime directory (`$XDG_RUNTIME_DIR/tongs-agents`, else
`$XDG_STATE_HOME/tongs-agents/run`), in private (0700) directories, and are
created 0600 without following or replacing anything:

- `requests/<token>.json`: `{"schema_version": 1, "session": ..., "launches": [<launch id>...]}`.
  The sidebar renames it away before reading it, so it is used once; a second
  sidebar started with the same token spawns nothing.
- `layouts/<token>.kdl`: rendered from the packaged `layouts/agents.kdl.tmpl`.
  Only the Python interpreter path, the session name, the token and the
  `tongs` path are substituted, as escaped KDL strings; control characters are
  refused. The template's `single` block (the sidebar, 40 columns wide and
  started with `--layout single`, beside a `stacked=true` column holding the
  home pane, which runs `<python> -m tongs_agents.workspace_spawn home`) or
  its `split` block (the sidebar alone) is kept, as `[workspace].layout`
  says. The sidebar deletes
  it on start.

### Printing the command

`--print-command` writes the command to stdout and notes to stderr:

- Running session: prints `zellij attach <session>` and has no side effects.
  No profile or sidebar pane is spawned; a note names the profiles it did not
  start.
- No session: prepares a one-shot spawn request, its layout and pending
  launch records, says so on stderr, and prints the create command. Run that
  command once; the sidebar consumes the request when it starts. Unused
  requests and layouts stay in the runtime directory.
- EXITED session: refused with exit 1 and a message naming
  `zellij delete-session <session>` and `--resurrect`, because printing never
  deletes a session. With `--resurrect` it prints `zellij attach <session>`
  and has no side effects.

## Sidebar

`tongs-agents sidebar [--spawn-request TOKEN] [--compact] [--layout {single,split}]`
(started by the layout as `python -m tongs_agents.commands.sidebar`; the
`single` layout passes `--layout single` so the agent stack can be found
again without its home pane, and a sidebar re-added by `run`, `workspace` or
the tongs plugin gets the same argument when the session has a home pane or
an earlier marked sidebar, or records `single` on tmux) runs the compact agents
panel inside a Zellij pane named `tongs-agents-sidebar` (or a tmux pane, see
[tmux](#tmux)). It refuses to start outside a Zellij or tmux session. It shares the monitor's header, footer, theme and command
palette (`Ctrl+P`, with `Add agent…`, `New agent: <profile>` and
`Open tongs tab` for its own session; `+` opens Add agent too) and reads its refresh interval, notify level and theme from
`[ui]`; see [app settings](app.md).

The sidebar is per-session; `tongs-agents monitor` remains the fleet-wide
view. It lists the hook sessions whose captured `ZELLIJ_SESSION_NAME` is its
session (and, when both the hook and the sidebar know a `ZELLIJ_SOCKET_DIR`, whose
socket dir matches) plus every pane the launcher started in its session, joined
by launch id. A hook session that captured no Zellij session, such as one in
a tmux pane, is listed when its launch id names a claimed launch of this
session, multiplexer and socket and its process is that launch's process or
runs under it. Agents in other sessions or other terminals appear only in
`tongs-agents monitor`.

| Row | Status column |
| --- | --- |
| A hook record carries the launch id | The hook status, as in `tongs-agents monitor` |
| No hook record (harness `other`, hooks not installed, not started yet) | The pane state: `RUNNING`, `STARTING` (launcher not claimed yet), or `EXITED <status>` |

Exited panes stay listed because Zellij keeps them open, so a launcher error
(exit 64) or a command that cannot start stays visible. Launch records of an
earlier session with the same name are not shown: a claimed record whose
process is gone while its pane runs something else is skipped.

Enter or a click focuses the row's pane in the same session: on Zellij the
pane is expanded in the stack, on tmux a parked pane is swapped into the main
slot. Hook rows go through the same focus path as `tongs-agents focus`;
hookless rows select their pane directly after checking that it is still
listed. Spawn failures appear as notifications.

### Renaming an agent

Agents started from the same profile all show the profile name until you
name them. `R` (shift+r) on a row opens a one-line prompt at the bottom of
the sidebar, prefilled with the agent's current name:

| Key | Action |
| --- | --- |
| `R` | Open the rename prompt for the selected agent. |
| Enter | Save the name. An empty name restores the default, the profile name. A refused name (more than 64 characters, a control character, a leading `-`, or `tongs-agents-sidebar` or `tongs-agents-home`) keeps the prompt open with the reason. |
| Escape | Close the prompt without saving. |

The name is a display label only: the profile, harness, session id, command
and folder stay as they are. It is kept in the agent's launch record (as
`label`), so it survives sidebar restarts, and it is shown in the sidebar
row, in `tongs-agents agent-status` (`PROFILE (NAME)`, and `label` in
`--json`) and in the MCP tools that list agents (`label` in each session of
the `fleet_*` tools and in `agents_status` and `agents_wait`). The
multiplexer pane is renamed too, best effort: Zellij with
`rename-pane --pane-id`, tmux by setting `@tongs_agents_name`, the pane title
(`select-pane -T`) and, in a `single` session, the window the pane is parked
in alone. When renaming the pane fails the name is still saved and a warning
says why; a program that sets its own pane title can overwrite the Zellij
title later.

Only an agent started in this workspace has a launch record to keep the name
in. Any other row, such as a discovered or hook-only session, is refused
with a short message in the prompt's place, and so is an agent that is still
starting (`STARTING`, its launcher has not claimed the record yet): rename it
a moment later.

Resuming a renamed agent from the sidebar (`u` on its row, `Resume session…`
in the palette or `:resume`) gives the new launch the same name, and names
its pane with it, as long as the earlier launch record is still kept (records
of ended agents are pruned after seven days). `tongs-agents resume` from a
shell starts the session under its profile name; rename it in the sidebar.

## tmux

`--multiplexer tmux` (or `multiplexer = "tmux"` in `[workspace]`) runs the
same verb on tmux 3.2 or newer. `tmux -V` is read before anything starts; a
missing or older tmux stops the command like a missing Zellij. Every tmux
call names the server socket with `-S`: the one in `TMUX` inside tmux,
otherwise tmux's default socket (`$TMUX_TMPDIR`, else `/tmp`, then
`tmux-<uid>/default`). Because `-S` skips tmux's own check of that default
directory, the workspace repeats it: `tmux-<uid>` is created as 0700 when
missing, and the command stops unless it is a real directory (not a symlink)
owned by you with no group or other access. Sessions are always named exactly
(`=<session>`), so a prefix never selects another session.

| Where you run it | Result |
| --- | --- |
| Inside tmux (`TMUX` is set) | Spawns the profiles into the current session and adds a sidebar pane if no live one is listed. It never nests, attaches or execs. A different `--session` and `--print-command` are refused. |
| Outside tmux, session running | Spawns the profiles into that session (and a sidebar if it has none), then runs `tmux -S <socket> attach-session -t =<session>`. |
| Outside tmux, no session | Creates the session detached (`new-session -d`) with the sidebar as its first pane, records `[workspace].layout` on it, spawns the profiles through the same spawn path, then attaches as above. |
| Inside Zellij | Refused with exit 2, so agent panes never inherit a Zellij pane identity. |

In a `single` session (the default for a new one) every agent pane opens in
its own detached window (`new-window -d`), named after its profile. Showing
it swaps it into the sidebar's window with `swap-pane` (the agent it replaces
moves into the parked window, which is renamed after it), or, when that
window holds only the sidebar because the shown agent exited, joins it beside
a sidebar about 40 columns wide with `join-pane`; then it is selected. Pane
ids move with their programs, so control and readback keep working on parked
panes. `tongs-agents focus` and the monitor swap a parked agent in before
they select its window. In a `split` session, or one the workspace did not
create, agent panes split the current window side by side and the row is
spread evenly; when the window has no room left, the pane opens in a new
window. Panes are named with the pane option `@tongs_agents_name`, which
programs cannot overwrite the way they overwrite pane titles.

tmux keeps no EXITED sessions, so `--resurrect` is ignored with a notice, and
there is no tongs tab, so `--tongs` is ignored with a notice. With
`--print-command` a missing session is created detached right away (with its
sidebar and profiles) and the attach command is printed; for a running
session it only prints the attach command. Nothing is removed when a later
step fails: the session stays and can be attached to. A new session name may
not contain `.` or `:`, because tmux up to 3.3 rewrites them.

The launcher records the pane from `TMUX_PANE` and the socket from `TMUX`
whenever `ZELLIJ` is not set; the session stays the workspace session the
launch was created for. Pane control (`tongs-agents send`, `read`, `wait`,
`agent-status`) goes through the same verification, per-pane lock and audit
log as on Zellij. A paste is loaded into a uniquely named tmux buffer from
stdin and pasted with `paste-buffer -p -r -d`: bracketed when the program
asked for it, line feeds kept, buffer deleted. Enter is sent separately with
`send-keys`, and `read` uses `capture-pane -p -J`. Focus shows a parked pane
(see above) or selects the window and pane, and reads the active pane back
with `display-message`; it never raises a client or an emulator window.
`subscribe` is unsupported on tmux.

The sidebar runs on whichever multiplexer encloses it (tmux when `TMUX` is
set and `ZELLIJ` is not). On tmux it lists the panes the launcher started in
its session and joins each one's hook or `tongs-agents notify` status
through the launch id, so working, done and needs-you reach the tmux sidebar,
its `n` key and its notifications as they do on Zellij.

`validation/tmux_workspace.py` checks all of this, parking and swapping
included, against real tmux servers on private sockets (a
`-L tongs-<token>` socket and the default socket under a private
`TMUX_TMPDIR`), and `.github/workflows/tmux-mux.yml` runs it on Ubuntu's apt
tmux and records the version. `validation/workspace.py` checks the Zellij
stack on each pinned release: every agent joins it, a row selection expands
its pane alone, and a full stack is refused with no pane left behind.

## Doctor

`tongs-agents doctor` adds:

- `workspace-zellij`: info when `zellij` is not on `PATH`, an error below
  0.44.2, a warning above 0.45.1.
- `workspace-python`: a warning when tongs-agents runs from an interpreter in
  a temporary directory, because layouts and agent panes start that
  interpreter. Checked only when `zellij` is present.

Missing hooks in a profile's config directory and wrapper commands whose name
hides the harness are reported by the core doctor checks and by
`tongs-agents profiles`.

## Limits

- A sandboxed profile uploads its folder only when its sandbox is created.
  When the sandbox already exists it is reused as it is, so `run` with
  another folder does not upload that folder; delete the sandbox, or give
  the profile its own `sandbox.name`, to start from a different folder.
- With `layout = "split"`, Zellij places new panes itself, so the sidebar
  shares the tab with the agent panes it spawns and its width depends on how
  many there are.
- On tmux the sidebar keeps its width when an agent is swapped in, but not
  when the terminal is resized: tmux then spreads the change over both
  panes. Parked agents show up as windows in tmux's status bar.
- Closing the Zellij home pane and every agent pane of the stack leaves the
  session placing new panes side by side, as a split session does, with a
  notice; start a new workspace session to get the stack back. A session
  created before the sidebar carried `--layout single` places new panes side
  by side as soon as its home pane is closed.
- A launch that fails before the launcher claims it shows in the sidebar only
  when that sidebar spawned it.
- Zellij restores layouts, not agent processes; resurrecting an EXITED
  session does not restart agents.
- When Zellij restarts a launched pane (for example after a resurrect), the
  launcher runs again with the same folder and the same arguments given to
  `run`. That is what you want for `--resume`; avoid passing a one-shot prompt
  as an argument if the pane may be restarted.
- Zellij 0.44.2 to 0.45.1 take pasted text only as a command argument
  (`zellij action paste --pane-id N -- <text>`), so text from
  `tongs-agents send`, MCP `agents_send` and chat wakes is readable in the
  process list (`/proc/<pid>/cmdline`) by other local users while that call
  runs, unless `/proc` is mounted with `hidepid`. Do not send secrets to
  Zellij panes on a shared host. The tmux adapter loads text from stdin and
  does not expose it this way.
- tmux closes a pane when its program exits (unless `remain-on-exit` is on),
  so a finished agent's launch is refused by `send`, `read` and `wait`
  (exit 5, pane not listed) instead of being reported as exited.
