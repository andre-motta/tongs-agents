# Config layer: `tongs-agents config`

`tongs-agents config` emits one harness-agnostic dotfiles directory into the
config home of every Claude Code, Codex and Pi profile, reports drift, and
captures harness-side edits back. The directory format is
[schema `tongs-agents-dotfiles/1`](work/dotfiles-schema-draft.md); where each
item lands per harness is the approved
[emitter strategy](work/emitter-strategy.md).

A broken emitter module is reported as an error and exits 1. Any unexpected
failure in a verb prints one line, `tongs-agents config: internal error: ...`,
and exits 1.

## Choosing the dotfiles directory

Every verb takes `--source DIR` (a directory holding `agentic.toml`, or the
file itself). Without it, the path comes from the tongs-agents config file
(`$TONGS_AGENTS_CONFIG_DIR/config.toml`, by default
`~/.config/tongs-agents/config.toml`):

```toml
[dotfiles]
path = "~/src/agent-dotfiles"
```

The path must be absolute or start with `~/`. Profiles are read from
`agentic.toml`; a profile with the same name in `config.toml` replaces it as
a local override.

`agentic.toml` is read under the same limits as `config.toml`: a regular file
owned by you, not group or world writable, at most 64 KiB. Unknown keys are
errors at every level, and each error names the key, for example
`mcp_servers.issues.env: list variable names only`. Paths stay inside the
dotfiles directory: absolute paths, `..` and symlinks that leave it are
refused, and skills may not contain symlinks.

## Targets

A target is one harness config home. The engine plans for:

- the Claude Code and Codex default homes, and the Pi default home when
  tongs-agents is installed there;
- every home recorded by `tongs-agents install --claude-config-dir`,
  `--codex-home` or `--pi-agent-dir`;
- the config home of every profile (`config_dir`, or `CLAUDE_CONFIG_DIR`,
  `CODEX_HOME` or `PI_CODING_AGENT_DIR` in its `env`).

Items narrowed with `profiles = [...]` reach only the homes of those profiles;
items narrowed with `harnesses = [...]` reach only those harnesses.

## Verbs

| Command | What it does | Exit status |
| --- | --- | --- |
| `config status [--json]` | Lists every managed entry and its state | 0 when every entry is in sync, disabled, adopted or skipped; 3 otherwise; 1 on an error |
| `config apply` | Writes every pending entry as one change set, then runs the harness CLI steps (below) | 0; 3 when drifted entries were skipped; 1 on an error, a foreign path or entry, or a failed CLI step |
| `config apply --dry-run` | Prints a unified diff of what apply would write, the entry list and each CLI step it would run (`would run ...`); writes and runs nothing | as `apply` |
| `config apply --force` | Also overwrites drifted entries, keeping each previous file as `<name>.tongs-agents.drift.bak` (an existing backup with other content is never replaced; the next one gets a `.1`, `.2`, ... suffix), and clears orphans a plain apply leaves recorded (see below). A drifted keyed entry (a Codex `config.toml` table, the `AGENTS.md` block, a Claude Code or Pi MCP server) backs up the whole file that holds it (`config.toml`, `AGENTS.md`, `.claude.json`, `mcp.json`) the same way before it is replaced | as `apply` |
| `config capture HARNESS --into DIR [--force]` | Reads the managed entries of HARNESS (claude, codex or pi) from every target and writes a dotfiles directory into DIR | 0; 1 on an error or a non-empty DIR without `--force` |

`apply` is idempotent: a second run with nothing changed writes 0 bytes,
runs no CLI step and prints `Nothing to change.` Every verb also prints the
emitters' notes as `note: <target>: ...` lines, for example an HTTP MCP server
a harness does not get yet.

## Harness CLI steps

Claude Code and Pi keep MCP servers in files they own, so those are changed
through the harness CLI, as the [emitter strategy](work/emitter-strategy.md)
decides:

- Claude Code: `claude mcp add --scope user` and `claude mcp remove --scope
  user`, with `CLAUDE_CONFIG_DIR` set to the target (unset for the default
  home). A changed server is removed and added again.
- Pi 0.99.0 or newer: `pi mcp add` and `pi mcp remove` with
  `PI_CODING_AGENT_DIR` set to the target. `pi mcp add` replaces a server of
  the same name in place, so a changed server is one add. With an older Pi, or
  one whose version is not recognized, each server stays `pending` with the
  reason (for example `Pi 0.99.0 or newer is required (found 0.90.0)`), so
  `status` is not clean until Pi is upgraded; apply runs nothing for it.
- Pi: the tongs-agents Pi extension is installed when it is missing or stale,
  as `tongs-agents install` does. It is `pending` with the reason when Pi is
  older than 0.84.4 or its version is not recognized, and `in sync` once
  installed.

The steps run only after the file change set has committed, one target at a
time, and each prints `ran <target>: <step>`. A failed step prints the step and
its error and makes apply exit 1. The files already written stay; the step's
entry is not recorded as applied, so `status` keeps reporting it and the next
`apply` retries it. Before each step runs, the value it sets is recorded as
pending, so a step that took effect without reporting success (a CLI that
wrote the server and then failed, or an interrupted apply) is recognised as
tongs-agents' own by the next run instead of as foreign. No environment value
is ever passed: stdio servers inherit the harness environment.

When `claude` or `pi` is not on PATH, that target's CLI-managed entries are
`skipped` (with a `note: ... skipped: claude not on PATH ...` line), apply runs
no step for them, and they do not make `status` or `apply` fail, so a machine
without that harness can be in sync. Its file entries are still applied when
its config dir exists; when the dir does not exist either, nothing is planned
for that target and no config home is created. Codex follows the same rule:
without `codex` on PATH, a Codex home that does not exist is not created (a
note says so), and one that exists is written as usual. The CLIs are looked up
on PATH only, as `tongs-agents` itself sees it: a shell alias or function
named `claude`, `codex` or `pi` is not seen.

`status` reads each CLI-managed server back from the harness file
(`.claude.json` for Claude Code, `mcp.json` for Pi) and reports it as an entry
of its own, such as `.claude.json [mcpServers.issues]`: missing when it was
removed, drifted when it was edited since the last apply (left alone unless
`--force`), foreign when a server of that name exists that tongs-agents did not
write (the whole apply is refused). A `.claude.json` or `mcp.json` that exists
but cannot be read or parsed is foreign too, never read as holding no
servers. The `tongs-agents` server an earlier
`tongs-agents install` registered is adopted. Codex servers live in
`config.toml` tables, written by the owned-file writer and reported the same
way, keyed `[mcp_servers.<name>]`.

Every write goes through the owned-file writer: atomic, 0600 for files
tongs-agents owns, the previous mode kept for shared files, symlinks refused,
and a `.tongs-agents.bak` copy before the first edit of a shared file (taken
once and never removed; see [install details](install.md#backups)). When
any write fails, every file the apply touched is restored and the command
exits 1.

`capture` never changes harness files and writes only inside DIR. It never
reads environment values: a server environment entry becomes its variable
name, and a literal value is skipped with a warning. When several targets
hold different values for the same item, the first one is kept and the rest
are reported. Review the result and copy what you want into your dotfiles
directory.

`tongs-agents doctor` includes the same drift report when `[dotfiles] path`
is set: a warning for drifted or foreign paths, a note for pending ones.

## Entry states

| State | Meaning | `apply` |
| --- | --- | --- |
| in sync | The file matches the last apply and the dotfiles | Writes nothing |
| pending | The dotfiles changed, or the entry is new | Writes it |
| drifted | The file changed since the last apply | Leaves it and exits 3; `--force` overwrites after a backup |
| missing | Recorded by the last apply, now absent | Recreates it |
| disabled | Disabled in the harness (for example a disabled plugin) | Keeps it disabled |
| foreign | The path exists without the tongs-agents marker | Refuses the whole apply; nothing is written |
| adopted | Already holds exactly what apply would write, with no record yet (for example what `tongs-agents install` wrote) | Records it, writes nothing to the harness |
| skipped | A CLI-managed entry whose harness CLI is not on PATH | Runs nothing for it |
| orphaned | Recorded by the last apply, no longer in the dotfiles | Removes a file tongs-agents owns when it is unchanged; leaves an edited one, or one in a shared file, until `--force` |

An orphan that `apply` leaves in place stays recorded, so `status` and
`doctor` keep reporting it (exit 3) until it is resolved. `config apply
--force` resolves it: an edited file tongs-agents owns is backed up as
`<name>.tongs-agents.drift.bak` and removed; for a shared file (one the
emitter edits by key, such as `settings.json`) the record is forgotten and the
file is left exactly as it is, so remove any entries you no longer want by
hand. The entry detail in `config status` names the remedy.

Drift follows meaning, not bytes, where an emitter says what its entries are:
for a TOML table it compares the `tomllib`-parsed value, so a comment or
whitespace edit is not drift. For a file tongs-agents owns, drift compares
its bytes (its parsed value for `.toml`). The user's own entries in a shared
file are never drift.

## Where the engine keeps state

- `state_dir()/emit/manifest.json`: the managed paths per target (the
  owned-file writer's manifest).
- `state_dir()/emit/entries.json`: the drift digest per managed path, written
  in the same change set.

Both are owned files (0600) in a private directory. Deleting them makes the
next apply treat matching files as adopted, other files carrying the
tongs-agents marker as pending, and any existing file without the marker as
foreign.

## For emitter authors

An emitter implements `config_layer.protocol.Emitter` and is listed in
`config_layer/emitters/__init__.py` as `EMITTERS` (a tuple of emitters or a
mapping by harness). The engine calls `plan_full(config, target, force=...)`
when the emitter has it (the shipped ones derive from
`emitters.base.BaseEmitter`), else the protocol's `plan(config, target)`. A
full plan adds to the file changes: `items` (the state of keyed entries, such
as a CLI-managed server, which become entries), `entries` (recorded in the
owned-file manifest in the same change set, so the next plan knows what it
wrote), `notes`, and `commands`, the CLI steps the engine runs after the
commit through `emitter.run_step(step)`. Both receive a
`config_layer.schema.CanonicalConfig`; `config.profiles_for(target)`,
`config.skills_for_target(target)`, `config.mcp_servers_for_target(target)`
and `config.instructions_for(harness)` apply the `harnesses` and `profiles`
narrowing. Plans never write; the engine applies them. Two plans for the same
path must agree, or the apply is refused. A plan lists every path the emitter
manages, with an `unchanged` change for a shared file that is already in sync:
a recorded path missing from the plan is an orphan.

Optional methods:

- `entry_values(target, path, data)` returns the parsed values of the
  emitter's entries in `path` (None for "the whole file");
  `engine.toml_entry_values(data, ["mcp_servers.name"])` does this for TOML
  tables. Drift then hashes those values only.
- `disabled(target, path, data)` returns True when the user disabled the
  entry in the harness.

An exception raised by either hook becomes a problem naming the target
(`status` exits 1 and `apply` writes nothing), never a traceback. `plan`
reports `OSError` and `ValueError` the same way.

`capture(target)` returns a `CanonicalFragment` whose `data` may hold
`instructions` (text), `skills` (`{name: {relative path: bytes or text}}`),
`mcp_servers` (`{name: schema v1 server table}`) and `warnings` (lines). The
shipped emitters capture the MCP servers the manifest records as theirs.
