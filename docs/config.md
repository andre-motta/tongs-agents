# Config layer: `tongs-agents config`

`tongs-agents config` emits one harness-agnostic dotfiles directory into the
config home of every Claude Code, Codex and Pi profile, reports drift, and
captures harness-side edits back. The directory format is
[schema `tongs-agents-dotfiles/1`](work/dotfiles-schema-draft.md); where each
item lands per harness is the approved
[emitter strategy](work/emitter-strategy.md).

The engine and the verbs ship first. The per-harness emitters arrive
separately; until they are installed, `status` and `apply` report
"No config emitters are installed" and change nothing. Only a missing
`config_layer.emitters` package counts as "not installed": any other import
failure (a broken emitter module) is reported as an error and exits 1. This is
stricter than the M2 plan, which treated every ImportError as an empty
registry, so a broken install is never mistaken for a missing one.

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
| `config status [--json]` | Lists every managed entry and its state | 0 when every entry is in sync, disabled or adopted; 3 otherwise; 1 on an error |
| `config apply` | Writes every pending entry as one change set | 0; 3 when drifted entries were skipped; 1 on an error or a foreign path |
| `config apply --dry-run` | Prints a unified diff of what apply would write, and the entry list; writes nothing | as `apply` |
| `config apply --force` | Also overwrites drifted entries, keeping each previous file as `<name>.tongs-agents.drift.bak` (an existing backup with other content is never replaced; the next one gets a `.1`, `.2`, ... suffix), and clears orphans a plain apply leaves recorded (see below) | as `apply` |
| `config capture HARNESS --into DIR [--force]` | Reads the managed entries of HARNESS (claude, codex or pi) from every target and writes a dotfiles directory into DIR | 0; 1 on an error or a non-empty DIR without `--force` |

`apply` is idempotent: a second run with nothing changed writes 0 bytes and
prints `Nothing to change.`

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
mapping by harness). `plan(config, target)` receives a
`config_layer.schema.CanonicalConfig`; `config.profiles_for(target)`,
`config.skills_for_target(target)`, `config.mcp_servers_for_target(target)`
and `config.instructions_for(harness)` apply the `harnesses` and `profiles`
narrowing. Plans never write; the engine applies them. Two plans for the same
path must agree, or the apply is refused.

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
`mcp_servers` (`{name: schema v1 server table}`) and `warnings` (lines).
