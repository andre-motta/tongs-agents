# Harness registration and hook environment facts

Issue: [#5](https://github.com/andre-motta/tongs-agents/issues/5) (M0-E,
investigation). Repository base: `663c81243ec5965fdec96911c3c2b389fcf1f348`.
Probe date: 2026-11-03 (UTC). Consumers: M1-C, M1-D, M1-E, M1-G, M1-I and M2-0.

This record answers the harness questions the agent-workspace plan depends on
and recommends installer, launcher and doctor behaviour for each. It is
evidence for the CTO, not product code; nothing under `src/` changes.

## Evidence labels

| Label | Meaning here |
| --- | --- |
| **live** | The installed harness binary ran in isolated config dirs and the probe observed what it did: files it wrote, CLI output, its session init message, or hook subprocesses it spawned. Model turns used a synthetic provider on 127.0.0.1. |
| **instrumented** | Real code observed through a stand-in for part of the system. No answer below needed this label. |
| **unvalidated** | Read from shipped documentation, changelogs or binary strings, not exercised. |

## Environment

| Item | Value |
| --- | --- |
| Host | Fedora Linux 44, x86_64, Python 3.14.8 |
| Claude Code | 2.1.317 (native binary; the `claude` command is a symlink to `versions/2.1.317`) |
| Codex | codex-cli 0.166.0 (static musl standalone release) |
| Pi | `@earendil-works/pi-coding-agent` 1.5.0 (npm global) |
| Probe | [`validation/harness_facts.py`](../../validation/harness_facts.py), sha256 `c7e5a64b35e63b0651ae38bf69b2f50e118362b106a9ff6c13e52054f2037b4f`; recorded run 2026-11-03T02:49Z, exit 0 |

## Method

```console
.venv/bin/python validation/harness_facts.py --output /tmp/harness-facts.json
```

The probe is local-only and Linux-only. Each harness gets a private root with
its own HOME, XDG dirs, XDG_RUNTIME_DIR, TMPDIR, FLEET_DIR and one of
`CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `PI_CODING_AGENT_DIR`. The environment is
built from scratch (only PATH, USER and locale are carried over), so no
credential or API key reaches a harness.

- **Synthetic provider.** One loopback HTTP server speaks Anthropic Messages,
  OpenAI Responses and Chat Completions. A prompt naming `probe-tool` gets one
  tool call (a shell command for Claude and Codex, `read` for Pi), anything
  else a short text. The evidence lists only the three provider paths. No
  account, login or paid model is used. Claude runs with
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`; Codex runs with its plugin
  features disabled, because a default Codex daemon fetched a public plugin
  marketplace over git during development of the probe.
- **Recorder hook.** Every hook command is a small Python script that appends
  the event name, the values of `TONGS_AGENTS_LAUNCH_ID`,
  `TONGS_AGENTS_PROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and
  `PI_CODING_AGENT_DIR`, and its `/proc` ancestry to a private log. For Pi,
  the real `tongs-agents install --agent pi` installs the packaged extension
  with a recorder standing in as `tongs-agents-hook`.
- **Codex hook trust.** Codex runs a hook only after the user trusts it in
  `/hooks`. The probe reads each hook's key and `currentHash` through
  `codex app-server` `hooks/list` and writes them as `hooks.state` entries in
  the private `CODEX_HOME/config.toml`, the same entries the review writes. It
  never uses a trust bypass flag.
- **Identity.** Real `claude` and `codex` binaries are kept alive on stdin
  (`claude mcp serve`, `codex app-server`) behind each wrapper style, and
  `tongs_agents.discovery.get_process_info`, `process_matches_harness` and
  `find_ancestor_process` are called on them. The walk is stopped at the
  probe's own ancestry so a host agent session cannot satisfy it.
- **Safety.** The real `~/.claude/settings.json`, `settings.local.json`,
  `~/.codex/config.toml`, `hooks.json` and `~/.pi/agent/` files are hashed
  before and after; all were unchanged in the recorded run. `~/.claude.json`
  is reported separately as volatile: live Claude sessions on the host
  (including the session running this investigation) rewrite it, and it
  changed during one earlier development run although the probe never points
  a harness at it. It was unchanged in the recorded run.
  Only processes the probe started (PID plus start time), or whose
  environment names the private `CODEX_HOME`, are signalled; the run ended
  with no survivors. Evidence JSON replaces the private root, home, user and
  host names with placeholders and the probe refuses to write if any leak.

The evidence JSON is not committed by this item; rerun the command above to
regenerate it. Steps needing a real account were not required: every question
below was answered with the synthetic provider.

## Answers

### (a) Claude Code user MCP location and CLAUDE_CONFIG_DIR

| Question | Answer | Label |
| --- | --- | --- |
| Does Claude load user MCP from `<config_dir>/settings.json` `mcpServers`? | **No.** An entry there is absent from `claude mcp list`, `claude mcp get` exits 1, and a session's init message does not list it. The same holds for `$HOME/.claude/settings.json`. | live |
| Where does `claude mcp add --scope user` write? | `$CLAUDE_CONFIG_DIR/.claude.json` when the variable is set, otherwise `$HOME/.claude.json` (not `~/.claude/.claude.json`). Nothing is written to HOME when the variable is set. | live |
| Does a session load that entry? | **Yes.** The stream-json init message reports the `.claude.json` server as `connected`. | live |
| Does `CLAUDE_CONFIG_DIR` relocate settings.json hooks too? | **Yes.** All six hooks in `<config_dir>/settings.json` fired; hooks in `$HOME/.claude/settings.json` never fired. | live |

Consequence: today's `cmd_install` writes the fleet MCP entry to
`~/.claude/settings.json` `mcpServers`, which Claude Code 2.1.317 ignores.
On 2.1.317 the Fleet MCP tools are therefore silently unavailable in Claude
sessions configured by the current code. Hook registration in `settings.json` is
correct.

Side observation (live): `claude mcp list` and `claude mcp get` fire the
`SessionEnd` hook from the config dir's `settings.json`, without a session
behind them. M1-C and M1-E should check that such an event cannot end or
create a fleet record for an unrelated session.

**Recommended installer behaviour (M1-E, M2-0):** register Claude MCP with
`claude mcp add --scope user fleet -- <python> -m tongs_agents.mcp_server`,
running it with `CLAUDE_CONFIG_DIR=DIR` for `--claude-config-dir DIR` and
without it for the default install. Check ownership first with
`claude mcp get fleet`, as the Codex path already does, and remove only our
own entry with `claude mcp remove --scope user fleet`. Do not edit
`.claude.json` directly: Claude owns it and running sessions rewrite it.
Keep removing the legacy `settings.json` `mcpServers.fleet` entry on install
and uninstall, and have doctor report it as a legacy leftover.

### (b) Codex hooks, MCP and profiles

| Question | Answer | Label |
| --- | --- | --- |
| Is `$CODEX_HOME/hooks.json` honoured? | **Yes.** `hooks/list` reports all six events with source `user` and `sourcePath` in the private `CODEX_HOME`. | live |
| Do new hooks run immediately? | **No.** Every new hook is `untrusted` and a `codex exec` turn ran none of them. Trust is stored in `$CODEX_HOME/config.toml` as `[hooks.state."<hooks.json path>:<event>:<group>:<handler>"] trusted_hash = "sha256:..."`. A changed command yields a different hash, so moving the virtualenv (a new `tongs-agents-hook` path) needs a new review. | live |
| Does `SessionEnd` fire? | **Yes** in `codex exec`, in `codex --no-daemon` and with `daemon_auto_start` disabled and no daemon running. **No** when a default TUI attached to the shared daemon exits (runs tui-1 and tui-2). Codex also clamps its timeout to 3 s, matching our installer. | live |
| Does `PermissionRequest` fire? | **Yes** in the interactive TUI (`--no-daemon`) when the model requests an escalated command. Not in `codex exec`, which refuses approvals outright. | live |
| Does `codex mcp add` respect `CODEX_HOME`? | **Yes.** The entry lands in `$CODEX_HOME/config.toml` as `[mcp_servers.<name>]`, `codex mcp get --json` finds it, and no `$HOME/.codex` is created. | live |
| Does `codex --profile X` share hooks? | **Yes.** In 0.166.0 `--profile X` layers `$CODEX_HOME/X.config.toml` on the base config (the legacy `[profiles]` selector can no longer be written). A turn under `--profile probe` fired the base `hooks.json` hooks with the base trust entries. | live |

**Recommended installer behaviour:** keep both `SessionEnd` and
`PermissionRequest` in `CODEX_HOOK_EVENTS` (consistent with the CTO evidence
correction). Keep `--codex-home DIR` writing `DIR/hooks.json` and running
`codex mcp` with `CODEX_HOME=DIR`. A Codex profile needs no separate install
because it shares its `CODEX_HOME`. The installer must not write trust
entries itself: trust is Codex's security review. It should keep printing the
`/hooks` instruction, and doctor should report `untrusted` or `modified` hooks.
`codex app-server` `hooks/list` exposes `trustStatus` per hook, but the
app-server protocol is marked experimental, so doctor should treat it as
best-effort and fall back to "review with /hooks".

### (c) Hook environment inheritance

| Harness and mode | Hook sees the agent process env (`TONGS_AGENTS_LAUNCH_ID`, profile, config-dir var)? | Hook's nearest harness ancestor is the agent process? | Label |
| --- | --- | --- | --- |
| Claude Code (`claude -p`) | **Yes**, all six events | Yes (hook parent is `claude`) | live |
| Codex `exec` | **Yes**, all events fired | Yes | live |
| Codex TUI `--no-daemon` | **Yes** | Yes | live |
| Codex TUI with `--disable daemon_auto_start`, no daemon running | **Yes** | Yes | live |
| Codex TUI with `--disable daemon_auto_start`, daemon running | No hooks: blocked on a dialog (below) | n/a | live |
| Codex TUI default, first launch (starts the daemon) | Yes, but only because the daemon inherited this launch's env | **No**: the daemon's `codex app-server --listen` | live |
| Codex TUI default, later launch (daemon already running) | **No**: hooks saw the *first* launch's `TONGS_AGENTS_LAUNCH_ID` | **No**: the daemon, reparented to systemd | live |
| Pi extension hook spawn | **Yes**, all five observed events | Yes (hook parent is `pi`) | live |

`CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `PI_CODING_AGENT_DIR` were visible
wherever the launch id was.

The Codex 0.166.0 interactive TUI starts, by default, a shared background
`codex app-server` daemon (a managed copy under
`$CODEX_HOME/packages/app-server-daemon/`) and runs sessions, and therefore
hooks, inside it. Hook subprocesses inherit the daemon's environment and
ancestry, not the TUI's. For every Codex session after the first in one
`CODEX_HOME` this means the wrong launch id, the wrong terminal markers
(`ZELLIJ_PANE_ID`, `TMUX_PANE` and so on are also env) and a pid that resolves
to the daemon rather than the pane's agent. The plan's fallback of joining
records to panes by captured `ZELLIJ_PANE_ID` fails in the same way. This also
affects the shipped monitor for any user running plain `codex` today; that is
outside this item and should get its own issue.

With a daemon already running, `--disable daemon_auto_start` does not simply
run in-process: the TUI stops at an interactive dialog ("Background server has
incompatible feature settings") offering to run without the daemon this time,
to restart the shared daemon with the setting persisted for every client, or
to cancel. No turn ran and no hook fired (live). Only `--no-daemon` runs
in-process whether or not a daemon is running.

**Recommended behaviour:**

- **Launcher (M1-D):** a `codex` profile runs with `--no-daemon`. Either the
  launcher appends it to a harness `codex` argv when absent, or profile
  validation refuses a `codex` profile without it. The CTO should pick; adding
  it is the less surprising default. Do not rely on
  `features.daemon_auto_start = false`: it avoids starting a daemon, but with
  one running it blocks on the dialog above, and its "restart" choice would
  change shared daemon settings for other clients.
- **Hook (M1-C):** launch identity from env is reliable for Claude, Pi and
  non-daemon Codex. M1-C can rely on it in the M1 gate once the launcher
  forces `--no-daemon`.
- **Doctor (M1-G):** warn on a `codex` profile without `--no-daemon`, and
  (separately, for the shipped monitor) on a running daemon for a monitored
  `CODEX_HOME`.

### (d) Wrapper identity and profile guidance

What `process_matches_harness` sees for the process the launcher would start
(and keep, since it uses `os.execvpe`), and whether the hook's ancestor walk
from inside the harness finds a match. All rows are live.

| Wrapper style | Launched process (comm / exe / argv0) | Launched pid matches | Harness pid = launched pid | Hook walk finds the harness |
| --- | --- | --- | --- | --- |
| `claude` on PATH, or env-only profile `argv ['claude', ...]` with `CLAUDE_CONFIG_DIR` | `claude` / `2.1.317` / `claude` | yes (comm, argv0) | yes | yes |
| Exec wrapper script (`exec claude "$@"`) | `claude` / `2.1.317` / `claude` | yes | yes | yes |
| Non-exec wrapper script (`claude "$@"`) | `claude-plain` / `bash` / `sh` | **no** | **no** (harness is a child) | yes, at the child |
| Differently named symlink `claude-personal` | `claude-personal` / `2.1.317` / `claude-personal` | **no** | n/a | **no** |
| Resolved binary `versions/2.1.317` | `2.1.317` / `2.1.317` / `2.1.317` | **no** | n/a | **no** |
| Shell alias (`bash -c` with an alias to `claude`) | `claude` / `2.1.317` / `claude` (bash execs the last command) | yes | yes | yes |
| `codex` on PATH, env-only profile, exec wrapper, alias | `codex` / `codex` / `codex` | yes | yes | yes |
| Codex non-exec wrapper | `codex-plain` / `bash` / `sh` | **no** | **no** | yes, at the child |
| Codex differently named symlink `codex-personal` | `codex-personal` / `codex` / `codex-personal` | yes (exe only) | yes | yes |

The kernel names a process after the path it executed, not the symlink
target, so Claude matches only through the literal name `claude`: its exe
basename is a version string. Codex matches any symlink through its exe
basename `codex`. A non-exec wrapper keeps hook records working (the walk
finds the child) but breaks the launcher's pid/start claim, because the
launched pid is the shell. Aliases are not a launcher concern: profile argv
is never shell-parsed, so an alias must be written out as env plus argv.

**Profile guidance doctor enforces (M1-G):**

1. Prefer env-only profiles: `argv[0]` is `claude` or `codex` (a bare command
   or a path whose basename is exactly that), with `config_dir` or `env`
   carrying `CLAUDE_CONFIG_DIR` or `CODEX_HOME`. No warning.
2. A wrapper script is accepted only if it `exec`s the harness. Doctor reads
   the script (bounded size, text only) and warns when no line starts with
   `exec claude` or `exec codex` (allowing leading `VAR=value` assignments).
   It cannot prove the exec statically, so this is a warning, not an error.
3. `argv[0]` resolving to a symlink or file whose basename is not `claude`
   for a `claude` profile is a warning: such sessions are not discoverable and
   their hooks cannot resolve the agent pid. The same check for `codex` is
   informational only, because the exe basename still matches.
4. A `codex` profile without `--no-daemon` is a warning, per (c).

### (e) Pi native MCP floor and extension event names

| Question | Answer | Label |
| --- | --- | --- |
| First Pi release with native MCP | **0.99.0** (2026-09-29): "Added codemode, tool search, and MCP support as built-in extensions", with `mcp.json` and `pi mcp add/remove/list`. No earlier changelog entry mentions MCP. | unvalidated (changelog of the installed 1.5.0 package) |
| CI pin | `@earendil-works/pi-coding-agent@0.85.0` in `.github/workflows/pi.yml`, below the MCP floor | live (read from the repo) |
| Where does `pi mcp add` write? | `$PI_CODING_AGENT_DIR/mcp.json` on 1.5.0, with no `$HOME/.pi` created (default documented as `~/.pi/agent/mcp.json`) | live |
| Extension events delivered on 1.5.0 | `session_start`, `before_agent_start`, `tool_execution_start`, `agent_settled`, `session_shutdown` all reached the hook (`agent_end` is consumed for the outcome) | live |
| `ui_prompt_start` / `ui_prompt_end` | Documented in the installed `docs/extensions.md`, not exercised (needs an interactive `ctx.ui` prompt) | unvalidated |

All event names used by `fleet-monitor.js` are current on 1.5.0. One semantic
change to note: since 1.2.0 `before_agent_start` also fires for runs started by
extensions (`messageType: "custom"`), which the bridge reports as `running`,
which is the correct state for such runs.

**Recommended behaviour:** keep the lifecycle install floor at 0.84.4. Any Pi
MCP emission (M2-0) requires Pi 0.99.0 or newer and must check the version and
skip with a clear message below it. Raise or add a CI pin at or above 0.99.0
before Pi MCP emission ships; the lifecycle job can keep 0.85.0 as the floor
check. This is a CTO choice because it changes a hosted gate.

## Summary of recommendations

| Area | Recommendation |
| --- | --- |
| Claude MCP | `claude mcp add --scope user` (with `CLAUDE_CONFIG_DIR=DIR` per dir); stop writing `settings.json` `mcpServers`; doctor flags the legacy entry |
| Claude hooks | Unchanged: `<config_dir>/settings.json`; `CLAUDE_CONFIG_DIR` relocates them |
| Codex events | Keep `SessionEnd` and `PermissionRequest` |
| Codex hooks and MCP | `DIR/hooks.json` plus `codex mcp` with `CODEX_HOME=DIR`; never self-trust; doctor reports trust state |
| Codex profiles | Share hooks and trust with their `CODEX_HOME`; launcher forces `--no-daemon` |
| Launch identity | Reliable for Claude, Pi and non-daemon Codex |
| Wrappers | Env-only profiles preferred; wrappers must `exec`; Claude needs the literal `claude` name |
| Pi | Lifecycle floor 0.84.4; MCP floor 0.99.0; CI pin below the MCP floor |

## Limits

- One host and one version of each harness. The Codex daemon behaviour is new
  in recent releases; doctor should re-check the version it was verified on.
- Claude was exercised in print mode (`-p`). The interactive TUI uses the same
  process model for hooks, but it was not driven here.
- Codex `PermissionRequest` was observed with an escalated shell command only.
- The synthetic provider is not a real model; it proves which hooks the
  harness runs for a given turn shape, not how a model behaves.
