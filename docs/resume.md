---
title: "Move your open sessions into the workspace"
description: "Resume Claude Code, Codex and Pi sessions that run in other terminals as panes of the workspace with tongs-agents resume."
---

You already have agent sessions open in other terminals and want them as
panes of the workspace, with the sidebar watching them. `tongs-agents resume`
starts each one again in the workspace through the harness's own resume:
the conversation continues, in the folder it ran in, with the profile that
uses its config dir.

## Quick start

```console
tongs-agents resume                 # list the sessions it can resume
tongs-agents resume 3f2a            # resume one; a unique prefix of the id is enough
```

The list looks like this:

```text
HARNESS  SESSION                               PROFILE/CONFIG  STATUS  AGE  FOLDER
claude   3f2a9c1e-0000-4000-8000-000000000001  claude-main     idle    4m   ~/src/app
codex    019a1b2c-0000-7000-8000-000000000002  codex-review    running 12s  ~/src/api
```

Then, for each session:

1. Run `tongs-agents resume ID`. Inside the workspace (from a shell pane or
   the sidebar) the pane is added to the current session. Outside, it is
   added to the running workspace session, or the workspace starts with this
   one pane, exactly like [`tongs-agents run`](workspace.md#adding-agents-run).
2. `resume` warns that the session still runs as a pid in another terminal.
   Two processes on one session conflict, so quit the agent in its old
   terminal (`/exit`, or Ctrl+D) before you type in the new pane.
   tongs-agents never stops it for you.

A session whose process already runs in the workspace session it would join
is refused (exit 2) instead: it already has a pane there. Select its row in
the sidebar, or run `tongs-agents focus ID`, to bring that pane up. Inside
tmux this compares the tmux session the command runs in. A resume of the
session that was just started there counts as running too, before the
harness has reported anything: its launch is pending (for up to 30 seconds)
or its process runs. A second `resume ID` right after the first is
therefore refused rather than starting a second pane.

Resume while the old agent still runs: once its process is gone, the
session store drops its record within seconds, and it is no longer listed.

## In the sidebar

| Where | What it does |
| --- | --- |
| `u` on a row | Resumes the selected agent's session in a new pane of the sidebar's session. |
| `Ctrl+P`, `Resume in workspace` | The same as `u`. |
| `Ctrl+P`, `Resume session…` | Lists the recent sessions of the store (harness, short id, status, age, folder); Enter resumes the chosen one, Escape closes the list. |
| `:` then `resume [ID] [--profile NAME]` | The CLI verb in the [sidebar command line](app.md#sidebar-command-line): without ID it lists the sessions under the prompt. |

The sidebar only lists the panes of its own session, so `Resume session…`
(or `:resume`) is the way to reach sessions that run in other terminals;
`u` is for a row whose agent has ended in its pane. Before anything starts,
the sidebar checks where the session still runs:

- In this workspace: it is not resumed again. The sidebar says so and offers
  to show that pane instead (Yes brings it up, No leaves everything as it is).
- Elsewhere (another terminal or session): a dialog names the process and
  asks whether to resume it here anyway; No starts nothing. After Yes, the
  warning is shown again next to the result.
- Nowhere: it is resumed right away.

While a session is being resumed (or its dialog is open), asking to resume
it again, with `u`, the list or `:resume`, is ignored with a short notice, so
a double key press starts one pane; once its pane is started, the check
above refuses it. `:resume ID` works like the CLI: it refuses a session that
runs (or is being resumed) in this workspace and warns, without asking,
about one that runs elsewhere. A failure
(no matching profile, a folder that no longer exists) is shown as an error
notification.

## What it starts

`resume` adds one pane through the same path as `run`, with:

| | |
| --- | --- |
| Folder | The session's recorded folder. A folder that no longer exists is refused. |
| Arguments | Claude Code `--resume ID`, Codex `resume ID`, Pi `--session ID`, appended to the profile's command for this launch only. |
| Profile | `--profile NAME` when given (it must run the same harness). Otherwise the profile recorded when the workspace started the session. Otherwise the one profile of that harness whose config dir holds the session's transcript. |

The transcript is how `resume` tells your config dirs apart, because the
session record does not store one. It looks for:

- Claude Code: `projects/*/ID.jsonl` under `config_dir`, else
  `CLAUDE_CONFIG_DIR`, else `~/.claude`;
- Codex: `sessions/YYYY/MM/DD/rollout-*-ID.jsonl` under `config_dir`, else
  `CODEX_HOME`, else `~/.codex`. The days around the session's recorded
  start and last activity (as UTC and local dates, a day either side) are
  read first, then the other days newest first, within a bound of 20000
  directory entries, so a long Codex history is not read in full;
- Pi: `sessions/*/*_ID.jsonl` under `config_dir`, else
  `PI_CODING_AGENT_DIR`, else `~/.pi/agent`.

When no profile's config dir holds it, or several do (two profiles on the
same config dir), `resume` refuses and names `--profile`:

```console
tongs-agents resume 3f2a --profile claude-alt
```

Sandboxed profiles are used only when you name them or when the workspace
recorded them for the session. The `PROFILE/CONFIG` column shows the profile
`resume` would use, else the config dir that holds the transcript, else `?`.

Pi's flag comes from Pi's own documentation (`docs/cli.md` of Pi 1.5.0:
`--session <path|id>` "opens a session by file path or full/partial ID, and
resumes it in its working directory"), and Pi prints the same command as its
own resume hint when it exits. A Pi session stored in a custom `sessionDir`
is not found in the config dir; pass `--profile`.

## Which sessions are listed

`resume` only reads the [session store](sessions.md); it never changes it.
The store holds the sessions whose agent process still runs: maintenance
(run by the sidebar and the monitor every few seconds) removes a record once
its process is gone. A session that no hook reported (shown as `discovered`)
has no harness session id. Neither can be resumed from the list; for those,
use the harness's own picker in a new pane:

```console
tongs-agents run claude-main ~/src/app -- --resume
tongs-agents run codex-review ~/src/api -- resume --last
```

## Options

| Option | Meaning |
| --- | --- |
| `ID` | The session id, or a unique prefix of it. Without it, the sessions are listed (at most 50, newest first). |
| `--profile NAME` | The profile to resume with. |
| `--session NAME` | The workspace session when run outside one, as for `run`. |
| `--multiplexer {zellij,tmux}` | The multiplexer when run outside one, as for `run`. |

Exit status: 0 when the pane started or the list was printed, 2 when the
session or profile was refused (nothing starts, including a session that
already runs in this workspace) or on Windows, 1 for a usage error and other
errors.
