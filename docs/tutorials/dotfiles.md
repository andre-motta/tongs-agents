# Tutorial: one dotfiles directory for every harness

Claude Code, Codex and Pi each keep instructions, skills and MCP servers in
their own files and formats. With tongs-agents you write them once, in one
harness-agnostic directory, and `tongs-agents config apply` writes them into
the config home of every harness and every profile.

In this tutorial you create that directory, preview and apply it, change a
file by hand to see drift, and capture an existing setup back into a
dotfiles directory. It assumes the profiles from
[your first workspace](first-workspace.md).

## 1. Create the dotfiles directory

```bash
mkdir -p ~/src/agent-dotfiles/instructions ~/src/agent-dotfiles/skills/release-notes
cd ~/src/agent-dotfiles
```

Create `agentic.toml`:

```toml
schema = "tongs-agents-dotfiles/1"

[instructions]
files = ["instructions/common.md"]

[instructions.harness.codex]
files = ["instructions/codex.md"]

[skills]
dirs = ["skills"]

[mcp_servers.issues]
command = ["example-issues-mcp", "--stdio"]
env = ["EXAMPLE_ISSUES_TOKEN"]     # names only, never values
profiles = ["claude-main", "codex-review"]
```

Then the files it names:

```bash
printf 'Run the tests before you say a change is done.\n' > instructions/common.md
printf 'Prefer small commits.\n' > instructions/codex.md
cat > skills/release-notes/SKILL.md <<'EOF'
---
name: release-notes
description: Draft release notes from the git log since the last tag.
---

Read `git log` since the last tag and group the changes by type.
EOF
```

What each part does:

- `instructions` are always-on instructions. Every harness gets
  `common.md`; Codex also gets `codex.md`.
- Every directory under `skills/` with a `SKILL.md` is one skill in the Agent
  Skills format. Its directory name must equal the `name` in the front
  matter.
- `mcp_servers` are MCP servers. `env` lists the variable names a server
  needs; their values come from the agent's environment and are never
  written anywhere. `profiles = [...]` (or `harnesses = [...]`) narrows an
  item to some targets.

The directory holds no secrets, so you can keep it in a public dotfiles
repository. `agentic.toml` must belong to you and must not be writable by
group or others.

## 2. Point tongs-agents at it

Add to `~/.config/tongs-agents/config.toml`:

```toml
[dotfiles]
path = "~/src/agent-dotfiles"
```

Every `config` verb also takes `--source DIR` instead. Keep your profiles in
`config.toml`: the workspace reads them from there, and the config layer
reads them too, so `claude-alt`'s `~/.claude-alt` is a target.

## 3. Preview

```bash
tongs-agents config status
tongs-agents config apply --dry-run
```

`status` lists every managed entry per target (each Claude, Codex and Pi
config home) with its state; new entries are `pending`, and the exit status
is 3 until everything is in sync. `apply --dry-run` prints the unified diff
that `apply` would write, and writes nothing.

## 4. Apply

```bash
tongs-agents config apply
tongs-agents config apply
```

The first run writes every pending entry as one change set. A failed write
restores every file it touched. Shared files such as `settings.json` are
edited by key, and a one-time `.tongs-agents.bak` copy is kept before the
first edit. After the files are written, `apply` registers MCP servers with
each harness's own CLI (`claude mcp add --scope user`, and `pi mcp add` on Pi
0.99.0 or newer); Codex servers are written straight into `config.toml`. A
harness whose CLI is not on `PATH` is reported as `skipped`, and a failed
step exits 1 naming the step and is retried by the next `apply`
(`apply --dry-run` lists these steps as `would run`). The second run prints
`Nothing to change.`: `apply` is idempotent.

Where things land, in short: Claude Code gets an owned
`tongs-agents/instructions.md` plus one import line, Codex and Pi get a
managed block in `AGENTS.md`; skills and MCP servers go where each harness
looks for them. Restart running agents to pick the changes up.

## 5. See drift

Edit a file that `apply` wrote, for example the managed block in
`~/.codex/AGENTS.md`, then:

```bash
tongs-agents config status
tongs-agents config apply
tongs-agents config apply --force
```

`status` reports the entry as `drifted` (exit 3). A plain `apply` leaves a
drifted entry alone and exits 3. `--force` overwrites it after keeping your
version as `<name>.tongs-agents.drift.bak`. A comment or whitespace edit in a
TOML table is not drift. `tongs-agents doctor` shows the same drift report
while `[dotfiles] path` is set.

Removing an item from `agentic.toml` makes its entry `orphaned`: the next
`apply` removes a file tongs-agents owns when it is unchanged.

## 6. Capture an existing setup

Already have instructions and MCP servers in one harness? Capture them into
a new directory, review it, and copy what you want:

```bash
tongs-agents config capture claude --into ~/src/captured-claude
```

`capture` reads the managed entries of that harness from every target and
writes only inside `--into`. It never reads environment values: a server's
environment entry becomes its variable name, and a literal value is skipped
with a warning. A non-empty `--into` directory needs `--force`.

## What you learned

- One `agentic.toml` describes instructions, skills and MCP servers for every
  harness, without secrets.
- `config status`, `config apply --dry-run` and `config apply` show, preview
  and write it; drift is reported, never silently overwritten.
- `config capture` turns a harness setup into a dotfiles directory.

Reference: [config layer](../config.md).

Next: [driving agents](driving-agents.md).
