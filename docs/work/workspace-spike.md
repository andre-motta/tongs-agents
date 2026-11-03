# Workspace spike: zellij mechanics and stack decisions

Issue: [#7](https://github.com/andre-motta/tongs-agents/issues/7) (M1-0,
investigation, tongs-agents initiative). Spike date: 2026-11-02. Repository
base: `663c81243ec5965fdec96911c3c2b389fcf1f348`. No product code changed.

This record answers the workspace mechanics questions with live evidence on
zellij 0.44.2 and 0.45.1, records the stack decisions Andre (CTO) already made,
and lists the remaining material choices for CTO confirmation. Later unit tests
should build hand-written fixtures from the [output shapes](#cli-output-shapes)
below; no captured upstream output is vendored.

## Evidence environment and method

| Item | Value |
| --- | --- |
| Host | Fedora Linux 44 KDE Plasma (Wayland), kernel 7.2.11-200.fc44.x86_64, x86_64 |
| Python | 3.14.8 (project venv) |
| tmux | 3.7c (private socket, `-f /dev/null`) |
| zellij 0.44.2 | `zellij-no-web-x86_64-unknown-linux-musl.tar.gz`, tarball sha256 `4af3ceefd4f8e9792144175234bdd8ffa96653209e9722613627f84f3d09ef83`, binary sha256 `79dd83f21d80204a5a3af4055e6ffd4b076347d4493f403859590190384a86e3` |
| zellij 0.45.1 | same asset name, tarball sha256 `d7bda1e18c30a688833ae7627f1d6a253bbba5349a4bc48e4f0ec008aaf75ed1`, binary sha256 `0ec6ef07b63c6355c02ce18343d40ef5ef5af19e25313ea9009c8fceda29e94f` |

Both tarballs were downloaded from the upstream GitHub release, matched the
checksums already recorded in the
[version floor evidence](../../validation/evidence/zellij-version-floor-fedora44.json),
were extracted into new empty scratch directories and were never installed.
The host's own 0.45.1 binary has the same sha256 as the upstream binary.

[`validation/workspace_spike.py`](../../validation/workspace_spike.py) ran once
per version (`--zellij-dir <extracted dir>`), plus two extra runs of the
resurrection step on 0.44.2 to rule out timing. Each run used a fresh temporary
directory holding a private `HOME`, `XDG_CONFIG_HOME`, `XDG_STATE_HOME`,
`XDG_CACHE_HOME`, `XDG_DATA_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR`,
`ZELLIJ_SOCKET_DIR` and `ZELLIJ_CONFIG_DIR`, uniquely named sessions, PTY
clients, and a private tmux socket. Agent panes run a small Python fixture that
records its pid, cwd and `ZELLIJ*`/`TMUX*`/`SPIKE_*` environment; the sidebar
pane runs a fixture that executes requested commands from inside the session,
as the real sidebar would. Every run reported zero surviving processes, zero
stray marker processes, unchanged user configuration and no entries in the
user's zellij cache. All results below are **live** unless labelled otherwise.

```console
python validation/workspace_spike.py --zellij-dir <dir with extracted zellij> --no-write
python validation/workspace_spike.py --zellij-dir <dir> --out <scratch>/spike.json
```

The script prints a redacted JSON record (`workspace-spike/1`); it refuses to
emit a record containing a home path, user name or host name. Like the other
terminal validators it is local-only and is never run in CI.

## Mechanics findings

Both versions behaved identically except where a row names a version.

### Spawning panes: `action new-pane`

| Question | Finding |
| --- | --- |
| Argv, outside the session | `zellij --session <s> action new-pane --name <n> --cwd <dir> -- <argv>...` |
| Argv, inside the session (sidebar) | `zellij action new-pane --name <n> -- <argv>...`; the pane inherits `ZELLIJ_SESSION_NAME` and `ZELLIJ_SOCKET_DIR`, so no `--session` is needed |
| Stdout | One line `terminal_<n>` (newline-terminated), exit 0 |
| `ZELLIJ_PANE_ID` in the spawned pane | Equals `<n>` (decimal, without the `terminal_` prefix) in every case |
| `--cwd` | Applied |
| Pane environment | The **server creator's** environment, not the caller's. A variable set only in the `action` caller (inside or outside) never reached the pane |
| Focus without `--no-focus` | The attached client moves to the new pane (readback by `list-clients`) |
| `--no-focus` | 0.45.1: the client keeps its pane. **0.44.2: rejected** (`Found argument '--no-focus' which wasn't expected`, exit 2, no pane) |
| `--near-current-pane` | Pane created, client focus unchanged on both versions |
| Unknown `--tab-id 999` | Exit 0 and a pane is still created elsewhere; the flag is not validated |
| Unknown `--session` | **Exit 0**, no pane id; stdout lists the active sessions with ANSI colour and stderr says `Session '<s>' not found. The following sessions are active:` |
| `--block-until-exit` | The action exits with the command's exit status (5 observed) |

Consequences: an action's exit status cannot prove that the session exists, so
callers must check existence first and treat stdout that is not exactly
`terminal_<digits>` as a failure. Profile environment must travel on the argv
(for example `env NAME=value <argv>`), because `new-pane` does not pass the
caller's environment.

### Exit behaviour and exec failures

| Case | Pane after exit | `list-panes --json` state |
| --- | --- | --- |
| Command exits 3, default | Kept (held) | `exited: true`, `exit_status: 3`, `is_held: true` |
| Command exits 0, `--close-on-exit` | Removed | absent |
| Missing executable, default | Kept (held), id returned | `exited: true`, `exit_status: 2`, `is_held: true` |
| Non-executable file, default | Kept (held), id returned | `exited: true`, `exit_status: null`, `is_held: true` |
| Missing executable, `--close-on-exit` | Removed; the action prints **no pane id** and still exits 0 | absent |

`action new-pane` exits 0 in every exec failure case. An exec failure is only
visible as a held pane with `exited: true`; with `--close-on-exit` it is not
visible at all.

### Creating, attaching and detecting sessions

| Argv | Observed |
| --- | --- |
| `zellij --session <s> --new-session-with-layout <layout.kdl>` (PTY) | Creates `<s>` from the layout and attaches |
| Same, when `<s>` exists | Exit 1, stdout `Session with name "<s>" already exists. Use attach command to connect to it or specify a different name.` |
| `zellij --layout <layout.kdl> attach --create-background <s>` | Exit 0, headless session created from the layout |
| `zellij attach --create-background <s> options --default-layout <layout.kdl>` | Exit 0, layout applied |
| `zellij attach --create-background <s>`, when `<s>` exists | Exit 1, stderr `Session already exists` |
| `zellij attach --create <s>` (PTY, new) | Creates with the default layout (one terminal pane running `/bin/sh`) |
| `zellij --layout <layout.kdl> attach --create <s>`, when `<s>` exists | Attaches as a second client; the layout is ignored and no session is created |
| `zellij --session <s> --layout <layout.kdl>`, `<s>` running | Adds the layout's tabs to `<s>`; stdout is two lines, `1` (apparently a tab id, not confirmed) then `terminal_<n>` |

`zellij list-sessions --short` lists **EXITED (resurrectable) sessions too**,
indistinguishable from running ones. `zellij list-sessions --no-formatting`
appends ` (EXITED - attach to resurrect)` to those lines. `zellij action ...`
against an EXITED session behaves like a missing session (exit 0, stderr
`not found`). Running sessions must therefore be detected from
`list-sessions --no-formatting` lines without the EXITED marker.

### Resurrection

With `session_serialization true` (the zellij default) a killed session stays
listed as EXITED.

| Resurrection path | Commands re-run? |
| --- | --- |
| `zellij attach <s>` | No: every command pane starts suspended; ENTER starts only the focused pane |
| `zellij attach --create-background <s>` | Resurrected silently as a running session (EXITED marker gone) with **every command still suspended** |
| `zellij attach --force-run-commands <s>` | 0.45.1: all panes re-run. 0.44.2: panes in the active tab re-run; the command in the inactive tab did not start within 4 s (three runs) |
| `zellij delete-session <s>` | Exit 0; the session is no longer listed |

### Focus from the sidebar with a `zellij watch` client

An interactive PTY client clicked the sidebar (SGR mouse press and release at
column 5, row 5) while a `zellij watch <s>` client was attached. The click
moved that client to the sidebar pane. `zellij action focus-pane-id
terminal_<n>`, run by the sidebar process without `--session`, exited 0 and
moved the clicking client to the target pane: `list-clients` readback and
keystroke routing both confirmed it. The watch client stayed alive but never
appears in `list-clients`, and its typed input reached no pane. Same result on
both versions. The earlier [zellij evidence](../../validation/evidence/zellij-fedora44.json)
still governs the multi-client rule: `focus-pane-id` moves only the last
client that sent input.

### Launching from inside tmux

A zellij client started from a tmux pane creates a server whose environment
includes `TMUX` and `TMUX_PANE`, so **every pane of that session inherits
them**, including panes spawned later with `action new-pane`.

| Record source | `capture_terminal_info()` label | Env-only detection |
| --- | --- | --- |
| Pane of a session created inside tmux | `zellij` (Linux ancestry reaches the zellij server); captured keys `ZELLIJ`, `ZELLIJ_PANE_ID`, `ZELLIJ_SESSION_NAME`, `ZELLIJ_SOCKET_DIR` | `tmux` (wrong: tmux wins the env order) |
| Pane spawned by `new-pane` from a tmux caller | same as above | `tmux` |
| Session created with `env -u TMUX -u TMUX_PANE` inside tmux | `zellij` | `zellij`; no `TMUX*` marker in the pane |

On Linux the hook record is already correct; elsewhere, or when the ancestry
walk fails, a leaked `TMUX` would label the agent `tmux`, and agents running
`tmux` commands would address the outer server.

## CLI output shapes

Synthetic examples for hand-written fixtures. Values are invented; only the
shapes were observed.

`action new-pane` stdout:

```text
terminal_7
```

`action list-clients` (whitespace-separated; the third column is the pane's
command split on spaces, so rows have a variable number of fields; `watch`
clients are not listed):

```text
CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND
1 terminal_3 claude --resume
```

`list-sessions --short` (running and EXITED names alike) and
`list-sessions --no-formatting` (the real output also ends every line but the
last with one trailing space after `ago]`, omitted here):

```text
agents
scratch
```

```text
agents [Created 2m 5s ago]
scratch [Created 40s ago] (EXITED - attach to resurrect)
```

`action list-panes --json` (identical keys with and without `--all` on both
versions) returns a JSON array of objects. Terminal panes carry every key
below; plugin panes have no `pane_command` or `pane_cwd`, a `plugin_url`
string, and `terminal_command: null`:

```json
[
  {
    "id": 3, "is_plugin": false, "title": "claude", "is_focused": false,
    "is_fullscreen": false, "is_floating": false, "is_suppressed": false,
    "is_selectable": true, "exited": false, "exit_status": null, "is_held": false,
    "pane_x": 30, "pane_y": 0, "pane_rows": 40, "pane_columns": 90,
    "pane_content_x": 31, "pane_content_y": 1, "pane_content_rows": 38,
    "pane_content_columns": 88, "cursor_coordinates_in_pane": [0, 3],
    "terminal_command": "claude", "pane_command": "claude",
    "pane_cwd": "/work/example", "plugin_url": null, "index_in_pane_group": {},
    "default_fg": null, "default_bg": null,
    "tab_id": 0, "tab_position": 0, "tab_name": "agents"
  }
]
```

`exit_status` is an integer once a held pane's command has exited and may be
`null` when it never started; `cursor_coordinates_in_pane` may be `null` (seen
on 0.44.2 for a pane with no content rows). The plain `list-panes` table header
is `PANE_ID TYPE TITLE`.

## Stack decisions

### Decided by the CTO before the spike (recorded, not re-opened)

1. **Config format: TOML, with `requires-python` raised to `>=3.11`** so the
   standard library `tomllib` reads it. No new dependency. Tongs already needs
   3.12, and Python 3.10 reaches end of life in October 2026. The floor change
   to `pyproject.toml`, the CI matrix (3.10 dropped) and the
   [SDLC profile](../SDLC.md) runtime row belong to a later item.
2. **Owned directories: hand-written XDG**, no `platformdirs` dependency:
   `config_dir = $XDG_CONFIG_HOME/tongs-agents` (default `~/.config/tongs-agents`),
   `state_dir = $XDG_STATE_HOME/tongs-agents` (default `~/.local/state/tongs-agents`),
   `runtime_dir = $XDG_RUNTIME_DIR/tongs-agents`. With the default XDG values
   none of these overlaps the frozen `FLEET_DIR` default `~/.claude/fleet`
   (they live under `~/.config`, `~/.local/state` and `/run/user/<uid>`). A
   non-default `XDG_*` value or `FLEET_DIR` override could still nest one in
   the other (for example `XDG_STATE_HOME=~/.claude/fleet`), so startup should
   refuse an owned dir that is equal to, inside or a parent of `FLEET_DIR`.
   Relative `XDG_*` values are ignored, as the XDG specification requires.
3. **Default session name: `agents`.**
4. **The tongs tab opens automatically when `tongs` is on `PATH`**, with
   `--no-tongs` to opt out.
5. **Static layout; the sidebar spawns agent panes at runtime** (one spawn
   path). The spike confirms it works on both versions: the sidebar runs
   `zellij action new-pane` without `--session` and gets the pane id on stdout.

Schema example with synthetic values (argv lists, never shell strings):

```toml
# $XDG_CONFIG_HOME/tongs-agents/config.toml
[workspace]
session = "agents"
tongs = true            # --no-tongs overrides

[profiles.claude]
command = ["claude"]

[profiles.claude-work]  # a second Claude with its own config dir
command = ["claude"]
env = { CLAUDE_CONFIG_DIR = "~/.claude-work" }

[profiles.claude-wrapped]  # or a wrapper that sets up its own environment
command = ["claude-sandbox", "--project", "example"]

[profiles.codex]
command = ["codex"]

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[profiles.pi-local]
command = ["pi", "--provider", "ollama", "--model", "qwen3-coder"]
cwd = "~/src/example"
```

### Recommendations for CTO confirmation

These follow from the evidence and are material to the next items:

- **Version floor stays 0.44.2** (the existing adapter floor). The design does
  not use `--no-focus`; a spawned agent pane takes the spawning client's focus
  on both versions, which matches a user starting an agent from the sidebar.
- **Profile environment on the argv.** Because panes receive the server
  creator's environment, `env` entries are applied by prefixing
  `env NAME=value ...` to the pane argv (POSIX `env`; the workspace targets
  Linux and macOS). No new dependency.
- **Clean creator environment.** The workspace launcher creates the session
  with `TMUX`, `TMUX_PANE` and any inherited `ZELLIJ*` variables removed, so
  agent panes never inherit an outer multiplexer.
- **Create and attach argv.** If `agents` is not running:
  `zellij --session agents --new-session-with-layout <layout.kdl>` from a
  terminal, or `zellij --layout <layout.kdl> attach --create-background agents`
  for a headless start. If it is running: `zellij attach agents`. Never rely on
  `--layout ... attach --create` for an existing session (the layout is
  silently ignored).
- **EXITED sessions.** Detect them through `list-sessions --no-formatting`.
  Resurrection leaves the sidebar suspended until ENTER, and
  `--force-run-commands` would restart every recorded agent command (and is
  incomplete on 0.44.2), so the launcher should `delete-session agents` and
  recreate it from the static template, after telling the user. The workspace
  layout may also set `session_serialization false` for its own session,
  subject to verification in the layout item.
- **Spawn validation.** Treat any `new-pane` stdout other than
  `terminal_<digits>` as failure, never pass `--close-on-exit` for agent panes
  (exec failures stay visible as held panes), and report `exited`/`exit_status`
  from `list-panes --json` in the sidebar.
- **Runtime dir fallback.** When `XDG_RUNTIME_DIR` is unset or not absolute
  (macOS, some containers), fall back to `state_dir/run`, created with mode
  0700. This detail was not part of the CTO decision and needs confirmation.

No new runtime dependency is proposed.

## Not covered

- Only Linux x86_64 with the musl no-web builds was exercised; macOS was not.
- The live default user socket directory and Andre's own sessions were never
  touched, so interaction with an existing user session named `agents` was not
  observed; the launcher must treat it as an existing session.
- The `(current)` marker that `list-sessions` may print when run from inside a
  session was not observed.
- The spec's ownership list named `docs/work/workspace-decisions.md`,
  `validation/workspace_probe.py`, committed evidence JSON and a unit test;
  this item was assigned `docs/work/workspace-spike.md` and
  `validation/workspace_spike.py` only, so the JSON records stay in scratch and
  the results are summarised here.
