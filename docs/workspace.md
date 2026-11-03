# Agent workspace

`tongs-agents workspace` opens a Zellij session with the tongs-agents sidebar
and one pane per requested [profile](profiles.md). It needs Zellij 0.44.2 or
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
| Inside Zellij (`ZELLIJ` is set) | Spawns the profiles into the current session and adds a sidebar pane if no live one is listed. It never execs. A different `--session` is refused, and so is `--print-command`; `--resurrect` is ignored with a notice. |
| Outside Zellij, session running | Spawns the profiles into that session (and a sidebar if it has none), then runs `zellij attach <session>`. |
| Outside Zellij, no session | Writes a spawn request and a layout, then runs `zellij --session <session> --new-session-with-layout <layout>`. The sidebar in that layout spawns the profiles when it starts. |
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
  refused. The sidebar deletes it on start.

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

`tongs-agents sidebar [--spawn-request TOKEN] [--compact]` (started by the
layout as `python -m tongs_agents.commands.sidebar`) runs the compact agents
panel inside a Zellij pane named `tongs-agents-sidebar`. It refuses to start
outside Zellij. It shares the monitor's header, footer, theme and command
palette (`Ctrl+P`, with `New agent: <profile>` and `Open tongs tab` for its
own session) and reads its refresh interval, notify level and theme from
`[ui]`; see [app settings](app.md).

The sidebar is per-session; `tongs-agents monitor` remains the fleet-wide
view. It lists the hook sessions whose captured `ZELLIJ_SESSION_NAME` is its
session (and, when both the hook and the sidebar know a `ZELLIJ_SOCKET_DIR`, whose
socket dir matches) plus every pane the launcher started in its session, joined
by launch id. Agents in other Zellij sessions or other terminals appear only
in `tongs-agents monitor`.

| Row | Status column |
| --- | --- |
| A hook record carries the launch id | The hook status, as in `tongs-agents monitor` |
| No hook record (harness `other`, hooks not installed, not started yet) | The pane state: `RUNNING`, `STARTING` (launcher not claimed yet), or `EXITED <status>` |

Exited panes stay listed because Zellij keeps them open, so a launcher error
(exit 64) or a command that cannot start stays visible. Launch records of an
earlier session with the same name are not shown: a claimed record whose
process is gone while its pane runs something else is skipped.

Enter or a click focuses the row's pane in the same session. Hook rows go
through the same focus path as `tongs-agents focus`; hookless rows select
their pane directly after checking that it is still listed. Spawn failures
appear as notifications.

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
| Outside tmux, no session | Creates the session detached (`new-session -d`) with the sidebar as its first pane, spawns the profiles through the same spawn path, then attaches as above. |
| Inside Zellij | Refused with exit 2, so agent panes never inherit a Zellij pane identity. |

Agent panes split the current window side by side and the row is spread
evenly; when the window has no room left, the pane opens in a new window.
Panes are named with the pane option `@tongs_agents_name`, which programs
cannot overwrite the way they overwrite pane titles. Layouts beyond the
sidebar plus panes are out of scope.

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
`send-keys`, and `read` uses `capture-pane -p -J`. Focus selects the window
and pane and reads the active pane back with `display-message`; it never
raises a client or an emulator window. `subscribe` is unsupported on tmux.

The sidebar runs on whichever multiplexer encloses it (tmux when `TMUX` is
set and `ZELLIJ` is not). On tmux it lists the panes the launcher started in
its session; hook rows are matched by Zellij session, so they appear only in
`tongs-agents monitor`.

`validation/tmux_workspace.py` checks all of this against real tmux servers
on private sockets (a `-L tongs-<token>` socket and the default socket under
a private `TMUX_TMPDIR`), and `.github/workflows/tmux-mux.yml` runs it on
Ubuntu's apt tmux and records the version.

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

- Zellij places new panes itself, so the sidebar shares the tab with the
  agent panes it spawns and its width depends on how many there are.
- A launch that fails before the launcher claims it shows in the sidebar only
  when that sidebar spawned it.
- Zellij restores layouts, not agent processes; resurrecting an EXITED
  session does not restart agents.
- tmux closes a pane when its program exits (unless `remain-on-exit` is on),
  so a finished agent's launch is refused by `send`, `read` and `wait`
  (exit 5, pane not listed) instead of being reported as exited.
