# Emitter strategy and per-harness config surfaces

Status: approved by the maintainer on 2026-11-03. Every checklist row is
approved as recommended: row 7 takes option C (owned-table writer), and row 11
adds a second hosted Pi pin at 0.99.0 or newer next to 0.85.0. Row 2 is
confirmed by a fake-provider session check in M2-C before it ships.

Issue: [#17](https://github.com/andre-motta/tongs-agents/issues/17) (M2-0,
investigation). Repository base: `784dfa8`. Probe date: 2026-11-03 (UTC).
Consumers: M2-A owned-file writer (#18), M2-B config engine and verbs (#19),
M2-C Claude, Codex and Pi emitters (#20).

This record decides, per harness and per artefact, whether tongs-agents edits
a shared harness file with keyed ownership or writes only files it owns that
the harness discovers by itself. It gives exact target paths, the Codex TOML
choice, and the capture and drift semantics the engine implements. The
companion [dotfiles schema v1 draft](dotfiles-schema-draft.md) defines
the canonical input. Nothing under `src/` changes in this item.

## Approval checklist

The maintainer approves or changes each row. Rows marked **sign-off** depart
from shipped or previously approved behaviour.

| # | Decision | Recommendation | Needs |
| --- | --- | --- | --- |
| 1 | Claude skills and agents | Owned skills-dir plugin at `<claude>/skills/tongs-agents/` | approval |
| 2 | Claude instructions | Owned `<claude>/tongs-agents/instructions.md` plus one managed `@` import line in `<claude>/CLAUDE.md` | approval; load path unvalidated |
| 3 | Claude MCP | `claude mcp add --scope user` and `remove`, as M0-E verified and M1-E ships | approval |
| 4 | Claude hooks | Unchanged: keyed entries in `<claude>/settings.json` | approval |
| 5 | Codex skills | Owned dir `$CODEX_HOME/skills/tongs-agents/` | approval |
| 6 | Codex instructions | Managed marker block in `$CODEX_HOME/AGENTS.md` | approval |
| 7 | Codex MCP | Minimal owned-section TOML writer for `[mcp_servers.<name>]` tables, stdlib only | **sign-off** |
| 8 | Codex hooks | Unchanged: keyed entries in `$CODEX_HOME/hooks.json`, user trust review | approval |
| 9 | Pi skills | Owned dir `<pi>/skills/tongs-agents/` | approval |
| 10 | Pi instructions | Managed marker block in `<pi>/AGENTS.md` | approval |
| 11 | Pi MCP | `pi mcp add` and `remove`, only on Pi 0.99.0 or newer | approval; CI pin is a hosted-gate change (**sign-off**, carried from M0-E) |
| 12 | Pi hooks | Unchanged: packaged extension and owned `tongs-agents.json` | approval |
| 13 | Agents (all harnesses) | Not in schema v1; Claude bundle supports them when v2 adds them | approval |
| 14 | Copy or symlink | Copy, rendered per config dir | approval |
| 15 | Ownership marker for edited TOML | Table key plus manifest hash, never comment markers | approval; amends the M2-A spec wording |
| 16 | New dependencies | None. No TOML writer library is proposed | none |
| 17 | Existing M1 installs | Adopt the reserved `tongs-agents` MCP entry (Claude, Codex, Pi) and our hook entries into the manifest on first apply when they match what `install` writes; anything else under that name is a conflict | approval |

`<claude>` is `$CLAUDE_CONFIG_DIR` when the profile sets a config dir,
otherwise `~/.claude`. `<pi>` is `$PI_CODING_AGENT_DIR`, otherwise
`~/.pi/agent`. `$CODEX_HOME` defaults to `~/.codex`. Targets come from
profiles and explicit flags only, never from the caller's environment
(accepted plan decision).

## Evidence labels

| Label | Meaning here |
| --- | --- |
| **live** | The installed harness binary, or the harness's own loader code, ran in isolated config dirs and the probe observed its output or the files it wrote. |
| **unvalidated** | Read from public harness documentation or carried over from M0-E, and not exercised by this probe. |
| **docs only** | Used for a harness that was not installed. Not needed: all three were installed. |

## Environment and method

| Item | Value |
| --- | --- |
| Host | Fedora Linux 44, x86_64 |
| Claude Code | 2.1.317 |
| Codex | codex-cli 0.166.0 |
| Pi | `@earendil-works/pi-coding-agent` 1.5.0 |
| Evidence | [`harness-config-claude-fedora44.json`](../../validation/evidence/harness-config-claude-fedora44.json), [`harness-config-codex-fedora44.json`](../../validation/evidence/harness-config-codex-fedora44.json), [`harness-config-pi-fedora44.json`](../../validation/evidence/harness-config-pi-fedora44.json), one run at 2026-11-03T13:17Z |

- **No model session.** Only offline commands ran: `claude mcp`,
  `claude plugin list|details|disable|enable|validate`, `codex mcp`,
  `codex plugin`, `codex debug prompt-input` (renders the model-visible
  prompt without a model call), `codex app-server` limited to `config/read`,
  `skills/list` and `mcpServerStatus/list`, `pi mcp`, `pi install` of a local
  directory, and Pi's own `DefaultResourceLoader` called from Node with no
  session. Every command and exit code is listed under `result.commands` in
  each evidence file.
- **Isolation.** The probe ran in `unshare --user --map-current-user --net`
  with loopback down; the evidence records the network as unreachable. Each
  harness got a private HOME, XDG dirs, TMPDIR and one of `CLAUDE_CONFIG_DIR`,
  `CODEX_HOME` or `PI_CODING_AGENT_DIR`. The environment was built from
  scratch with two synthetic variables, `PROBE_SECRET` and `PROBE_INHERIT`.
- **Synthetic MCP server.** A small stdio server recorded which environment
  variable names it received and whether `PROBE_SECRET` arrived expanded, as
  the literal text `${PROBE_SECRET}`, or not at all. It never records values.
- **Safety.** The maintainer's real Claude, Codex and Pi config files and
  skill directory listings were hashed before and after; all were unchanged
  (`user_config` in each evidence file). The private reference config was not
  read; only public harness documentation and isolated dirs were used.
- **Probe source.** The probe script is not committed because this item owns
  only the docs and evidence paths. Its sha256 values are in `probe_sha256`,
  and the recorded commands are enough to repeat each check by hand.

## Decision rules

1. **Own files the harness discovers by itself.** A file or directory that
   only tongs-agents writes needs no merge with user content and can be
   replaced atomically.
2. **Edit a shared file only where no owned surface exists**, and then
   through the harness's own CLI when that CLI was observed to be lossless,
   otherwise through the M2-A writer with keyed ownership.
3. **Key ownership by name and manifest, not by comments**, in any file a
   harness CLI also rewrites. Codex 0.166.0 dropped a begin-marker comment
   and moved the end marker to the end of the file after one
   `codex mcp add` (live, `owned_block_survives_cli`). Marker blocks remain
   fine in Markdown instruction files, which no harness CLI rewrites.
4. **Environment by name only.** Emitters never write an environment value.
   Each harness gets the mechanism that passes a name through (below).
5. **Copy, not symlink**, for emitted content (see [Copy or symlink](#copy-or-symlink)).

## What the harnesses do

### Environment reaching stdio MCP servers (live)

| Harness | Inherits the harness environment | `${NAME}` in `env` | Pass a name through |
| --- | --- | --- | --- |
| Claude Code | **Yes**, full environment (user scope and plugin `.mcp.json`) | Expanded | Nothing to write; `${NAME}` also works |
| Codex | **No.** Only `HOME`, `LANG`, `PATH`, `TMPDIR`, `USER` plus configured entries | **Not** expanded: the server sees the literal text | `env_vars = ["NAME"]` (expanded, live), also honoured in a plugin `.mcp.json` |
| Pi | **Yes**, full environment | Expanded | Nothing to write; `${NAME}` also works |

### Shared-file CLIs (live)

| CLI | Same name again | Keeps user content | Can express env by name |
| --- | --- | --- | --- |
| `claude mcp add --scope user` | Refused (exit 1, "already exists"); replace is get, remove, add | Claude owns `.claude.json`; not edited by us | Not needed (inherits) |
| `codex mcp add` / `remove` | Replaced in place | Keeps top-level comments and unrelated tables, but **drops every user comment above, inside or inline in `[mcp_servers.*]` tables**, on each add and remove | **No**: stdio servers take only literal `--env KEY=VALUE` |
| `codex plugin marketplace add` / `plugin add` | Not probed | Appended two tables, kept every user comment including those in `[mcp_servers.*]` | Through the bundle's `.mcp.json` `env_vars` |
| `pi mcp add` / `remove` | Replaced | Keeps other entries, unknown keys at both levels and 4-space indentation | Not needed (inherits) |

### Owned surfaces each harness discovers (live unless marked)

| Harness | Skills | Instructions | Bundle |
| --- | --- | --- | --- |
| Claude Code | `<claude>/skills/<plugin>/` with `.claude-plugin/plugin.json` loads as `<plugin>@skills-dir`, copied or symlinked, with skills, agents, hooks and `.mcp.json` (`claude plugin details`). Plain skill dirs are documented but not observable offline (unvalidated) | `<claude>/CLAUDE.md` with `@path` imports (unvalidated: no offline renderer) | The skills-dir plugin itself |
| Codex | `$CODEX_HOME/skills/**/SKILL.md` and `~/.agents/skills/**`, nested dirs and symlinks followed, scope `user` (`skills/list`, `prompt-input`). Codex keeps its own skills in `$CODEX_HOME/skills/.system/` | `$CODEX_HOME/AGENTS.md` only. No `@` import. `AGENTS.override.md` replaces it entirely. A symlinked `AGENTS.md` loads. `-c developer_instructions=...` reaches the prompt | Local marketplace with the **Claude plugin layout** (`.claude-plugin/marketplace.json`): skills appear as `tongs-agents:<skill>`, MCP servers start; installed as a **copy** under `plugins/cache/<market>/<plugin>/<version>/` |
| Pi | `<pi>/skills/**`, `~/.agents/skills/**`, `settings.json` `skills` paths, local packages (`pi install <dir>` writes a path relative to `settings.json`), symlinks followed (`DefaultResourceLoader`) | `<pi>/AGENTS.md` (no `@` import, symlink loads) and `<pi>/APPEND_SYSTEM.md` | A local Pi package |

There is no TOML include in Codex 0.166.0: a top-level `include = [...]` was
ignored, and `config/read` reports only the user and system layers (profile
files layer only under `--profile`, one at a time, and user profiles already
use that slot).

## Decisions per harness

### Claude Code

| Artefact | Recommended | Exact target | Alternatives |
| --- | --- | --- | --- |
| Skills | Owned skills-dir plugin, copied | `<claude>/skills/tongs-agents/.claude-plugin/plugin.json` and `<claude>/skills/tongs-agents/skills/<skill>/SKILL.md` (plus the skill's files) | (a) Plain dirs `<claude>/skills/tongs-agents-<skill>/`: no namespace, mixes with the user's own skills, load unvalidated. (b) `claude plugin marketplace add` and `install`: copies into Claude's plugin cache, needs a version bump per change and writes settings. |
| Instructions | Owned file plus one managed import line | `<claude>/tongs-agents/instructions.md` (owned, 0600) and the exact line `@tongs-agents/instructions.md` in `<claude>/CLAUDE.md`, created with only that line when absent | (a) Marker block in `CLAUDE.md`. (b) Skills only, no always-on text. (c) Launcher `--append-system-prompt-file`, workspace launches only. |
| MCP | `claude mcp add --scope user` and `claude mcp remove --scope user` with `CLAUDE_CONFIG_DIR` set per target (M0-E). Update is get, compare, remove, add. No `env` written | `<claude>/.claude.json` (default target: `~/.claude.json`), written by Claude | Bundle `<claude>/skills/tongs-agents/.mcp.json`: owned file, servers listed as `plugin:tongs-agents:<name>`, environment inherited and expanded (live). Departs from M0-E, so it needs sign-off; not recommended now. |
| Agents | Not in schema v1. When added: bundle `agents/<name>.md` (live inventory) | `<claude>/skills/tongs-agents/agents/<name>.md` | `<claude>/agents/tongs-agents-<name>.md` (unvalidated) |
| Hooks | Unchanged | `<claude>/settings.json`, keyed entries as shipped | Bundle `hooks/hooks.json`: listed by `plugin details`, did not fire on offline `claude mcp list` where M0-E saw `settings.json` `SessionEnd` fire; firing in a session not probed. Revisit after v0.10.0. |

Notes:

- A user can disable the bundle with `claude plugin disable
  tongs-agents@skills-dir`; that lands in `<claude>/settings.json`
  `enabledPlugins` (live). Apply must leave it disabled and doctor reports it.
- `claude plugin validate` accepts the bundle, including a `.mcp.json` that
  uses Codex's `env_vars` key (live), so one bundle layout can serve both
  harnesses if the maintainer later prefers bundles.
- Row 2 is the only Claude decision without live evidence. M2-C should confirm
  the import with a fake-provider session in its validator before shipping,
  or the maintainer accepts the documented behaviour.

### Codex

| Artefact | Recommended | Exact target | Alternatives |
| --- | --- | --- | --- |
| Skills | Owned dir, copied | `$CODEX_HOME/skills/tongs-agents/<skill>/SKILL.md` | (a) `~/.agents/skills/tongs-agents-<skill>/`: shared with Pi and other Agent Skills tools, but per HOME, not per profile. (b) Plugin bundle through a local marketplace (live). |
| Instructions | Managed marker block, file created when absent | `$CODEX_HOME/AGENTS.md`, block between `<!-- tongs-agents:begin -->` and `<!-- tongs-agents:end -->` | (a) `developer_instructions` in `config.toml` (needs the TOML writer; one string shared with the user). (b) Launcher `-c developer_instructions=...` (live; workspace launches only, overrides a user value). (c) Symlink `AGENTS.md` to an owned file when absent (live). |
| MCP | **Option C below**, minimal owned-section writer | `$CODEX_HOME/config.toml`, tables `[mcp_servers.<name>]` and `[mcp_servers.<name>.env]` for manifest names only | Options A and B below |
| Agents | Not in schema v1; no emitter | (none) | Codex custom agent roles were not probed (unvalidated) |
| Hooks | Unchanged | `$CODEX_HOME/hooks.json`, keyed entries; trust stays the user's `/hooks` review (M0-E) | Plugin hooks not probed |

Doctor additions: warn when `$CODEX_HOME/AGENTS.override.md` exists, because it
hides the managed block (live: the override replaced `AGENTS.md`).

#### Codex TOML: the three options

| | A. `codex mcp` CLI | B. Plugin bundle (nearest owned include) | C. Minimal owned-section writer |
| --- | --- | --- | --- |
| What it is | Shipped M1-E path | Owned `$XDG_DATA_HOME/tongs-agents/codex/marketplace/` with the Claude plugin layout, registered by `codex plugin marketplace add` and `codex plugin add` | Stdlib code in the M2-A writer that appends or replaces only our tables |
| Env by name | **Impossible**: only literal values, which the schema forbids | `env_vars` in the bundle `.mcp.json` (live) | `env_vars = [...]` (live) |
| User content | Drops user comments in all `[mcp_servers.*]` tables on every add and remove (live) | Two appended tables; all comments kept (live) | Only our table byte ranges change; everything else byte-identical by construction and checked |
| Update | Replace in place | Copy into a version-keyed cache; an update needs a version bump and re-add (re-add not probed) | Replace our range |
| Other costs | none | Depends on the `plugins` feature (on by default, but M0-E disabled it to stop a public marketplace fetch); server names are not namespaced | New code that edits a user TOML file; must refuse inline or dotted `mcp_servers` definitions it cannot locate |
| Sign-off | none (shipped) | **yes** | **yes** |

**Recommendation: C**, for every server tongs-agents emits into Codex,
including its own `tongs-agents` server. Rules for the writer:

- Values limited to strings, arrays of strings, booleans and integers. Keys
  are bare (`[A-Za-z0-9_-]+`) or quoted basic strings. Control characters are
  refused rather than escaped.
- A table is ours only if its name is in the manifest for this target and its
  current parsed value hashes to the manifest value (see drift). A name present in the
  file but absent from the manifest is the user's: refuse with a conflict.
- Locate a table by its header line through the next header. Refuse when
  `tomllib` sees `mcp_servers.<name>` but no header for it exists (inline
  table or dotted keys).
- After rendering, `tomllib` must parse the result, and the parsed document
  must equal the parsed original with only our tables added, changed or
  removed. Otherwise nothing is written.
- Atomic write, backup before the first edit and multi-file rollback come
  from M2-A. No comment markers are written in `config.toml`.

Option A stays the fallback if C is not approved; it keeps the comment loss
and cannot pass `XDG_STATE_HOME` to the tongs-agents server. Option B is the
fallback if the maintainer prefers no TOML editing at all.

### Pi

| Artefact | Recommended | Exact target | Alternatives |
| --- | --- | --- | --- |
| Skills | Owned dir, copied | `<pi>/skills/tongs-agents/<skill>/SKILL.md` | (a) `settings.json` `skills` entry for an owned dir (keyed JSON edit). (b) A second local package via `pi install` (writes `settings.json` `packages`). (c) `~/.agents/skills/` (per HOME, shared with Codex). |
| Instructions | Managed marker block, file created when absent | `<pi>/AGENTS.md`, same markers as Codex | (a) Own `<pi>/APPEND_SYSTEM.md` when absent (live load; single file the user may own). (b) Launcher `--append-system-prompt <owned file>` (documented, repeatable; workspace launches only). (c) The tongs-agents Pi extension injects an owned file at `before_agent_start` (code in the JS bridge; not probed). |
| MCP | `pi mcp add` and `pi mcp remove` with `PI_CODING_AGENT_DIR` per target, after checking the Pi version is at least 0.99.0; below it, skip with one clear line. No `env` written | `<pi>/mcp.json`, written by Pi | Keyed JSON edit of `mcp.json` through the M2-A `edit_json_entries`. |
| models.json | Not in schema v1 | (none) | Keyed provider entries via `edit_json_entries`, `apiKey` only as `$NAME` |
| Agents | Not applicable | (none) | Pi has no subagent files |
| Hooks | Unchanged | Packaged extension plus `<pi>/tongs-agents.json` | (none) |

Pi MCP emission requires the native-MCP floor: `pi mcp` and `mcp.json` exist
from 0.99.0 (changelog, unvalidated; only 1.5.0 was run). The Pi CI job pins
0.85.0, below that floor, so an emitter test against a real Pi needs a second
pin at or above 0.99.0. That changes a hosted gate and needs the maintainer.

## Copy or symlink

Copy is recommended for every owned file and directory:

- each target is rendered for its own config dir (paths, the Python
  interpreter of the install), so content can differ per profile;
- an owned directory is replaced atomically by writing a sibling and renaming
  it, and its bytes hash into the manifest for drift detection;
- a link into a virtualenv or data dir dangles silently when that moves;
- the M2-A writer refuses symlinked targets, and Codex copies plugins into its
  cache anyway.

This does not change the existing rule that hooks and MCP servers point at
pip-installed entry points: the copies are user content from the canonical
directory (skills, instructions), not tongs-agents code.

Symlinks were observed to work in all three harnesses (live: Claude plugin
dir, Codex and Pi skill dirs, Codex and Pi `AGENTS.md`), so a single shared
source stays possible if the maintainer prefers it.

## Capture and drift semantics

Terms: **canonical** is the dotfiles directory; a **target** is one
harness config dir reached through a profile; an **entry** is one managed
item in a target (owned file or directory, JSON entry, TOML table, Markdown
block or line, CLI-managed MCP server).

### Manifest

`state_dir()/emit/manifest.json` (M2-A) records, per target and entry: kind,
path and key, the canonical item it came from, and the sha256 of what was
emitted. For CLI-managed entries the hash covers the canonical JSON of the
fields tongs-agents set, as read back by `claude mcp get` or the `mcp.json`
entry. For a Markdown block it covers the bytes between the markers; for a
TOML table, the canonical JSON of its
`tomllib`-parsed value, not its bytes: `codex mcp add` and `remove` re-serialize
the whole `[mcp_servers.*]` region and move whitespace and comments
(`owned_block_survives_cli`), so a byte hash would report drift after an
unrelated user edit.

### Entry states

| State | Meaning | `config apply` | `config diff` and doctor |
| --- | --- | --- | --- |
| in sync | Target bytes match the manifest and the rendered canonical | No write | Nothing |
| pending | Canonical changed; target still matches the manifest | Rewrite | Shows the change |
| drifted | Target bytes differ from the manifest (user or harness edit) | Refuse this entry; others proceed. `--force` overwrites after a backup | Shows the target-side diff and suggests `capture` |
| missing | In the manifest, absent from the target | Recreate, unless the harness records it as disabled | Reports it |
| disabled | Claude `enabledPlugins` false, or `"enabled": false` on a Pi or Codex server | Leave disabled; content may still be updated | Reports it |
| foreign | Key exists in the target but not in the manifest | Never overwritten: conflict, exit non-zero | Names the key and the canonical item |
| adopted | The reserved `tongs-agents` entry that an M1 `install` wrote before any manifest existed, and whose parsed value matches what `install` writes | Recorded in the manifest on first apply, then treated as in sync | Reports the adoption once |
| orphaned | In the manifest, no longer in canonical | Remove if in sync; if drifted, leave it and report | Reports it |

A second `config apply` with nothing changed writes 0 bytes (M2 exit gate).

### Capture

`tongs-agents config capture` reads managed entries back from the targets
and maps them to canonical fields, the reverse of each emitter:

- it prints a diff against the canonical directory and writes it only with
  `--write`; it never changes harness files;
- environment values are never read or captured: Codex `env_vars` become
  names; `${NAME}` values become names; any literal value is skipped with a
  warning naming the server and variable;
- unmanaged items (the user's own skills or MCP servers) are captured only
  with `--include-unmanaged`, under the same rules;
- after a successful capture with `--write`, the next `apply` sees in-sync
  entries and updates the manifest hashes.

## Side observations for follow-up issues

1. **Shipped Codex install drops user comments (live).** The M1 installer
   registers the `tongs-agents` server with `codex mcp add` and `remove`,
   which on 0.166.0 delete every user comment above, inside and inline in
   all `[mcp_servers.*]` tables. Values survive. This is damage to user
   configuration under the pre-v0.10.0 rule; option C removes it.
2. **The tongs-agents MCP server under Codex may read the wrong state dir.**
   Codex gives stdio servers only `HOME`, `LANG`, `PATH`, `TMPDIR` and `USER`
   (live), so `XDG_STATE_HOME` and `TONGS_AGENTS_STATE_DIR` do not reach the
   server while they do reach the hooks. With a non-default state dir the
   server would list a different sessions directory than the hooks write.
   Inferred from the environment observation; not reproduced with the real
   server. Fix: `env_vars = ["XDG_STATE_HOME", "TONGS_AGENTS_STATE_DIR"]`,
   which needs option B or C.
3. Claude plugin hooks did not fire on offline `claude mcp list`, unlike
   `settings.json` hooks in M0-E (live).

## Limits

- One host and one version of each harness; Codex and Claude config formats
  change often, so doctor should record the versions it checked.
- Claude `CLAUDE.md` imports, plain Claude skill and agent dirs, and Codex
  custom agents were not exercised (unvalidated).
- Hooks in plugin bundles were not exercised in a session.
- `codex plugin add` after a bundle version bump was not probed.
- The Pi MCP floor is from the changelog; no Pi below 1.5.0 was run.
