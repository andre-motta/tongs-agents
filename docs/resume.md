---
title: "Resume your sessions in the workspace"
description: "Resume Claude Code, Codex and Pi sessions, open in other terminals or closed long ago, as panes of the workspace with tongs-agents resume."
---

You have agent sessions open in other terminals, or closed days ago, and
want them as panes of the workspace, with the sidebar watching them.
`tongs-agents resume` starts each one again in the workspace through the
harness's own resume: the conversation continues, in the folder it ran in,
with the profile that uses its config dir. Past sessions come from the
harnesses' own history, so a session started before tongs-agents was
installed can be resumed too.

## Quick start

```console
tongs-agents resume                 # list the sessions it can resume
tongs-agents resume 3f2a            # resume one; a unique prefix of the id is enough
tongs-agents resume 3f2a ~/src/app2 # resume it in another folder
```

The list looks like this:

```text
HARNESS  SESSION                               PROFILE/CONFIG  STATUS   AGE  FOLDER     TITLE
claude   3f2a9c1e-0000-4000-8000-000000000001  claude-main     idle     4m   ~/src/app  Fix the flaky login test
codex    019a1b2c-0000-7000-8000-000000000002  codex-review    running  12s  ~/src/api  Review the pagination change
pi       01a0c4d5-0000-7000-8000-00000000000d  pi              -        3d   ~/docs     Docs pass
```

`STATUS` is the last hook status of a session whose agent still runs, and
`-` for a past session read from history.

Then, for each session:

1. Run `tongs-agents resume ID`. Inside the workspace (from a shell pane or
   the sidebar) the pane is added to the current session. Outside, it is
   added to the running workspace session, or the workspace starts with this
   one pane, exactly like [`tongs-agents run`](workspace.md#adding-agents-run).
2. `resume` warns that the session still runs as a pid in another terminal.
   Two processes on one session conflict, so quit the agent in its old
   terminal (`/exit`, or Ctrl+D) before you type in the new pane.
   tongs-agents never stops it for you. A past session that no hook
   reported, whose transcript changed in the last two minutes, gets the
   same warning ("may still run in another terminal"): the harness may
   still be writing it without hooks.

A session whose process already runs in the workspace session it would join
is refused (exit 2) instead: it already has a pane there. Select its row in
the sidebar, or run `tongs-agents focus ID`, to bring that pane up. Inside
tmux this compares the tmux session the command runs in. A resume of the
session that was just started there counts as running too, before the
harness has reported anything: its launch is pending (for up to 30 seconds)
or its process runs. A second `resume ID` right after the first is
therefore refused rather than starting a second pane.

Once the old agent's process is gone, the session store drops its record
within seconds; the session stays listed from the harness's history, as a
past session, and resumes the same way.

## In the sidebar

| Where | What it does |
| --- | --- |
| `u` on a row | Resumes the selected agent's session in a new pane of the sidebar's session. |
| `Ctrl+P`, `Resume in workspace` | The same as `u`. |
| `Ctrl+P`, `Resume session…` | Lists the sessions it can resume, newest first: running ones (marked `●`) and past ones from history, each with its title, folder, profile and age. Typing filters the list (every word must appear in the title, folder, profile, harness or id); Up and Down move, Enter resumes the chosen one, Escape closes the list. |
| `:` then `resume` | The same list. |
| `:` then `resume ID [DIR] [--profile NAME]` | The CLI verb in the [sidebar command line](app.md#sidebar-command-line). A relative DIR is resolved from the folder the sidebar started in. |

The sidebar only lists the panes of its own session, so `Resume session…`
(or `:resume`) is the way to reach sessions that run in other terminals or
that ended; `u` is for a row whose agent has ended in its pane. A resumed
pane is named as described under [What it starts](#what-it-starts). Before
anything starts, the sidebar checks where the session still runs:

- In this workspace: it is not resumed again. From the list, Enter shows
  that pane. Otherwise the sidebar says so and offers to show that pane
  (Yes brings it up, No leaves everything as it is).
- Elsewhere (another terminal or session), or possibly elsewhere (a past
  session whose transcript changed in the last two minutes): a dialog says
  so and asks whether to resume it here anyway; No starts nothing. After
  Yes, the warning is shown again next to the result.
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
| Folder | DIR when given, else the session's recorded folder (from the store, else the transcript's working directory). A folder that no longer exists or is not a folder is refused, and so is a session with none; give DIR. A recorded folder that is not an absolute, printable path (a control character, for example) counts as none. Claude Code looks a session up in the project folder of its working directory, so resuming one elsewhere may not find it. |
| Arguments | Claude Code `--resume ID`, Codex `resume ID`, Pi `--session ID`, appended to the profile's command for this launch only. |
| Name | The name of an earlier launch of the session that was [renamed](workspace.md#renaming-an-agent), else the session's title (cut to 64 characters), else the profile name; from the sidebar and from the CLI alike. |
| Profile | `--profile NAME` when given (it must run the same harness). Otherwise the profile recorded when the workspace started the session. Otherwise the one profile of that harness whose config dir holds the session's transcript; when several do (profiles sharing a config dir), the one the newest resume launch of that session used. |

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
same config dir) and no resume launch of the session picks one, `resume`
refuses and names `--profile`:

```console
tongs-agents resume 3f2a --profile claude-alt
```

Sandboxed profiles are used only when you name them or when the workspace
recorded them for the session. The `PROFILE/CONFIG` column shows the profile
`resume` would use, else the config dir that holds the transcript, else `?`.

Pi's flag comes from Pi's own documentation (`docs/cli.md` of Pi 1.5.0:
`--session <path|id>` "opens a session by file path or full/partial ID, and
resumes it in its working directory"), and Pi prints the same command as its
own resume hint when it exits. Pi's sessions are read from its session dir:
`PI_CODING_AGENT_SESSION_DIR` from the profile's `env`, else from the
environment `tongs-agents` runs in, else `sessionDir` in the agent dir's
`settings.json` (a regular file, never read through a symlink), else
`sessions` in the agent dir. A leading `~` is expanded as Pi does. A
relative path is resolved by Pi from the folder it starts in, so it cannot
be known: that profile lists no past Pi sessions. A project's
`.pi/settings.json` and `--session-dir` in the profile's command are not
read; a session stored there is listed only while its agent runs (from the
store). A Pi session id ending in `.jsonl` (possible with Pi's
`--session-id`) is never resumed: Pi reads `--session notes.jsonl` as a
file in the folder it starts in.

## Which sessions are listed

`resume` only reads; it never changes the [session store](sessions.md) or a
harness's history. It lists:

- the sessions of the store, whose agent process still runs (maintenance,
  run by the sidebar and the monitor every few seconds, removes a record
  once its process is gone);
- the past sessions in the history of each profile's config dir (sandboxed
  profiles excluded), newest first by the time their transcript last
  changed.

| Harness | History read | Title |
| --- | --- | --- |
| Claude Code | `projects/<project>/<id>.jsonl`; the folder is the first `cwd` of its records, since the project folder name is lossy | Its name (`custom-title`, or `agent-name` from `--name`), else its latest `ai-title`, else `summary`, else the last prompt, else the first prompt |
| Codex | `sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`, first line `session_meta` (id, `cwd`); subagent and review threads are not listed | The thread name in `session_index.jsonl`, else the first prompt that is not injected context |
| Pi | `sessions/<folder>/<time>_<id>.jsonl`, header `session` (id, `cwd`) | Its `/name` (`session_info`), else the first prompt |

Reading is bounded so a long history stays fast: at most 20000 directory
entries per config dir; transcripts are read newest first until 200
sessions are kept, at most 400 of them (Codex: 4000, since most rollouts
are subagent threads that only their first line rules out); per transcript
only its first and last 64 KiB (Codex: its first line, up to 256 KiB, then
64 KiB for a kept session without a thread name). Symlinks inside a
history folder are skipped, unreadable or malformed files and lines are
skipped, and no message text is shown except the title, cut to 64
characters with control characters removed. Older Claude Code subagent
transcripts (`agent-*.jsonl`) are not listed. A full session id beyond those
bounds is still found by `resume ID` (a Codex rollout whose file name does
not end with its id, by the id in its first line).

A session that no hook reported (shown as `discovered`) has no harness
session id, and a store record whose harness wrote no history yet is listed
without a title. For a session that is not listed, use the harness's own
picker in a new pane:

```console
tongs-agents run claude-main ~/src/app -- --resume
tongs-agents run codex-review ~/src/api -- resume --last
```

## Options

| Option | Meaning |
| --- | --- |
| `ID` | The session id, or a unique prefix of it. Without it, the sessions are listed (at most 50, newest first). |
| `DIR` | The folder to resume it in, instead of the one it ran in. |
| `--profile NAME` | The profile to resume with. |
| `--session NAME` | The workspace session when run outside one, as for `run`. |
| `--multiplexer {zellij,tmux}` | The multiplexer when run outside one, as for `run`. |

Exit status: 0 when the pane started or the list was printed, 2 when the
session or profile was refused (nothing starts, including a session that
already runs in this workspace) or on Windows, 1 for a usage error and other
errors.
