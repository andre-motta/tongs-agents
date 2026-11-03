# tongs-agents dotfiles schema v1 (draft)

Status: proposed, awaiting maintainer approval.

Issue: [#17](https://github.com/andre-motta/tongs-agents/issues/17) (M2-0).
Companion to the [emitter strategy](emitter-strategy.md), which says where
each field lands in each harness and defines capture and drift.

A tongs-agents dotfiles repo is a public, harness-agnostic description of an agent
setup: profiles, skills, instructions, MCP servers and workspace layouts.
It is meant to live in a user's dotfiles repository and be safe to publish:
it holds no secret values, only the names of environment variables.
`tongs-agents config apply` emits it into each profile's harness config dir;
`tongs-agents config capture` proposes changes back. All names, paths and
servers below are synthetic.

## Layout

```text
dotfiles/
  agentic.toml              # this schema
  instructions/
    common.md               # always-on instructions for every harness
    codex.md                # optional per-harness addition
  skills/
    release-notes/
      SKILL.md              # Agent Skills format: name and description frontmatter
      template.md
    pr-review/
      SKILL.md
```

The directory can sit anywhere. The tongs-agents config file points at it
(see [Open questions](#open-questions)). Every path in `agentic.toml` is
relative to the directory that holds it and must stay inside it: absolute
paths, `..` components and symlinks that leave the directory are refused.

## Example

```toml
schema = "tongs-agents-dotfiles/1"

# Profiles use the M1 format unchanged (docs/profiles.md), so the same table
# can move between config.toml and agentic.toml.
[profiles.claude-main]
command = ["claude"]

[profiles.claude-alt]
command = ["claude"]
config_dir = "~/.claude-alt"
model_args = ["--model", "opus"]

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[profiles.pi-local]
command = ["pi", "--provider", "ollama", "--model", "qwen3-coder"]

[instructions]
files = ["instructions/common.md"]

[instructions.harness.codex]
files = ["instructions/codex.md"]

[skills]
dirs = ["skills"]                 # every child directory with a SKILL.md

[skills.overrides.pr-review]
harnesses = ["claude", "codex"]   # not emitted for pi

[mcp_servers.issues]
command = ["example-issues-mcp", "--stdio"]
env = ["EXAMPLE_ISSUES_TOKEN"]    # names only; values come from the agent's environment

[mcp_servers.notes]
command = ["~/bin/example-notes-mcp"]
cwd = "~/notes"
profiles = ["claude-alt", "pi-local"]

[mcp_servers.docs]
url = "https://mcp.example.invalid/mcp"
bearer_token_env = "EXAMPLE_DOCS_TOKEN"

[layouts.review]
session = "review"
profiles = ["claude-alt", "codex-review"]
tongs = false
```

## Top level

| Key | Type | Meaning |
| --- | --- | --- |
| `schema` | string, required | Exactly `tongs-agents-dotfiles/1`. Any other value is refused, so a v2 file is never misread. |
| `profiles` | table of tables | M1 profiles, same keys and validation as [docs/profiles.md](../profiles.md). |
| `instructions` | table | Always-on instructions. |
| `skills` | table | Skill directories in the Agent Skills format. |
| `mcp_servers` | table of tables | MCP servers. |
| `layouts` | table of tables | Named workspaces. |

Unknown keys are errors at every level, as in the M1 profile loader. Names of
profiles, servers and layouts match `^[a-z0-9][a-z0-9_-]{0,31}$`. The file is
read with `tomllib` under the same limits as `config.toml` (regular file,
owned by the user, not group or world writable, at most 64 KiB).

## Targets

A **target** is one harness config dir: each profile whose harness is
`claude`, `codex` or `pi` names one, its `config_dir` or the harness default.
Two profiles with the same harness and config dir share a target. Profiles
with harness `other` get no emission. Targets come only from profiles and
explicit flags, never from the caller's environment.

Every skill, instruction file and MCP server goes to every target unless it
narrows itself with:

| Key | Type | Meaning |
| --- | --- | --- |
| `harnesses` | list of `claude`, `codex`, `pi` | Only targets of these harnesses. |
| `profiles` | list of profile names | Only these profiles' targets. Unknown names are errors. |

When both are given, a target must satisfy both.

## instructions

| Key | Type | Meaning |
| --- | --- | --- |
| `files` | list of paths | Markdown files concatenated in order, separated by one blank line. |
| `harness.<name>.files` | list of paths | Appended after `files` for that harness only. |

Each target receives one rendered document, at most 64 KiB. Emission per
harness: the owned `tongs-agents/instructions.md` plus one import line for
Claude Code, a managed block in `AGENTS.md` for Codex and Pi.

## skills

| Key | Type | Meaning |
| --- | --- | --- |
| `dirs` | list of paths | Each child directory that contains `SKILL.md` is one skill. |
| `overrides.<skill>` | table | `harnesses` or `profiles` for one skill. |

A skill directory name matches `^[a-z0-9][a-z0-9-]{0,63}$` and equals the
`name` in its `SKILL.md` frontmatter, which also needs a non-empty
`description` (the Agent Skills specification; Pi only warns on a mismatch,
other harnesses may refuse). The whole directory is copied. Limits: 256 files
and 4 MiB per skill; symlinks, devices and FIFOs inside a skill are refused.
Two skills with the same name are an error.

## mcp_servers

A server has exactly one of `command` (stdio) or `url` (streamable HTTP).

| Key | Type | Meaning |
| --- | --- | --- |
| `command` | list of strings | argv. `command[0]` is looked up on `PATH` at agent start, or is an absolute or `~/` path. Never shell-parsed. |
| `cwd` | path | Working directory for a stdio server (absolute or `~/`). |
| `env` | list of names | Environment variables the server needs. Names match `^[A-Z_][A-Z0-9_]*$`. **Values are never written.** A table (`env = { NAME = "value" }`) is refused with a message saying that values belong in the agent's environment. |
| `url` | string | `https://` URL, or `http://` only for `localhost`, `127.0.0.1` or `[::1]`. |
| `bearer_token_env` | name | Sends `Authorization: Bearer $NAME` on HTTP servers. |
| `enabled` | boolean, default `true` | `false` keeps the definition but removes it from targets. |
| `harnesses`, `profiles` | lists | See [Targets](#targets). |

The server name `tongs-agents` is reserved for the tongs-agents MCP server,
which install manages.

How `env` reaches the server (live, see the emitter strategy):

| Harness | Emitted |
| --- | --- |
| Claude Code | Nothing: stdio servers inherit Claude's environment. |
| Codex | `env_vars = [...]`: Codex passes only `HOME`, `LANG`, `PATH`, `TMPDIR`, `USER` otherwise. |
| Pi | Nothing: stdio servers inherit Pi's environment. |

Doctor warns when a named variable is unset in a profile's launch
environment. HTTP servers are part of v1, but each emitter may refuse them
until its harness's header handling has live evidence (not probed in M2-0).

## layouts

A layout names a workspace so `tongs-agents workspace --layout NAME` can
replace a list of flags. It does not describe pane geometry; the workspace
layout stays the packaged static template.

| Key | Type | Meaning |
| --- | --- | --- |
| `session` | string | Zellij session name, same rules as `--session`. Defaults to the layout name. |
| `profiles` | list of profile names, required | One agent pane each, in order. |
| `tongs` | boolean, default `true` | Whether the tongs tab opens when tongs is installed. |

## Capture mapping

`config capture` reverses each emitter. It never reads environment values.

| Canonical field | Read back from |
| --- | --- |
| `instructions` | Claude `tongs-agents/instructions.md`; Codex and Pi managed block. A capture that differs from the rendered concatenation is offered as a change to the last contributing file, `harness.<name>.files` when the difference is harness-specific. |
| `skills` | The owned skill directories, file by file. |
| `mcp_servers` | `claude mcp get`, Codex owned tables, Pi `mcp.json` entries. Codex `env_vars`, and `${NAME}` values elsewhere, become `env` names; literal values are skipped with a warning. |
| `profiles`, `layouts` | Not captured; they are not emitted into harness files. |

## Open questions

1. **Where profiles live.** Recommended: `config.toml` gains
   `[dotfiles] path = "~/src/agent-dotfiles"`; profiles are read from
   `agentic.toml`, and a profile of the same name in `config.toml` wins as a
   local override. Alternative: profiles stay only in `config.toml` and
   `agentic.toml` carries no `profiles`.
2. **Agents and hooks.** Not in v1. Claude subagents can later ship in the
   Claude bundle; Codex roles need evidence first; Pi has none.
3. **Pi `models.json`.** Not in v1; providers carry credentials and need
   their own design.
4. **Schema publication.** A JSON Schema for editors could be generated from
   the loader once v1 is approved.
