# Agent workspace

`tongs-agents workspace` opens a Zellij session with the tongs-agents sidebar
and one pane per requested [profile](profiles.md). It needs Zellij 0.44.2 or
newer (0.45.1 is the newest validated release) on Linux or macOS; on Windows
it prints that the workspace is unsupported and exits 2.

```console
tongs-agents workspace --profile claude-alt --profile codex-review
tongs-agents workspace --session work --profile pi-local --no-tongs
tongs-agents workspace --resurrect
tongs-agents workspace --print-command --profile claude-alt
tongs-agents workspace --print-alias --profile claude-alt
```

| Option | Meaning |
| --- | --- |
| `--profile NAME` | Start an agent pane for this profile. Repeatable. Unknown names are refused before anything starts. Without it only the sidebar opens. |
| `--session NAME` | The Zellij session, `agents` by default. Names use letters, digits, `.`, `_` and `-` (at most 64 characters). |
| `--no-tongs` | Do not add the `tongs` tab. Without it the tab opens when `tongs` is on `PATH`. |
| `--resurrect` | When the session is EXITED, attach to it instead of deleting it and starting it fresh. Starts no new panes; without an EXITED session it changes nothing. |
| `--print-command` | Print the `zellij` command instead of running it. See [Printing the command](#printing-the-command). |
| `--print-alias` | Print a shell alias for the same workspace command. Nothing else runs and no file is edited; add the line to your shell rc yourself. |

When `zellij` is missing or older than 0.44.2 the command stops and points at
`tongs-agents doctor`.

## What it does

Every agent pane runs the launcher,
`<python> -m tongs_agents.launch <profile> --launch-id <id>`, for a pending
launch record created just before; the launcher claims the record and execs
the profile command. Panes are named after their profile.

| Where you run it | Result |
| --- | --- |
| Inside Zellij (`ZELLIJ` is set) | Spawns the profiles into the current session and adds a sidebar pane if no live one is listed. It never execs. A different `--session` is refused, and so are `--print-command` and `--resurrect`. |
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
outside Zellij.

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
