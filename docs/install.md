# How `tongs-agents install` changes harness files

The README covers what `install` and `uninstall` do; this page covers how
they change files and what is left behind.

## Order of operations

`install` runs in this order:

1. Every hook file it targets (Claude Code `settings.json` and Codex
   `hooks.json`, in the default homes, in `--claude-config-dir` and
   `--codex-home` dirs and in the config home of every profile) is staged
   into one change set and committed as one unit.
2. Then, per target, the `tongs-agents` MCP server is registered through
   `claude mcp add --scope user` or `codex mcp add`, when that CLI is on
   `PATH`.
3. Then the Pi extension, for Pi targets.
4. Finally the install record (`installs.json` in the state directory) is
   saved.

A refusal or a failed write in step 1 restores every hook file and exits 1
before any MCP registration runs. An MCP registration that fails is reported
on the terminal; the hook files are already written by then and stay
written. Because every hook file is committed before any MCP
registration, an unexpected error (a crash, not a refusal) during
registration can leave hooks installed in every target with MCP registered in
only some of them. Running `tongs-agents install` again finishes the job; it
is idempotent.

`uninstall` follows the same order: every hook file is edited in one change
set, then MCP entries are removed per target.

## Backups

Before the first edit of an existing `settings.json` or `hooks.json`, the
owned-file writer keeps a copy of it next to the file as
`<name>.tongs-agents.bak` (mode 0600). The copy is taken only once:

- a later `install`, or a `config apply` that edits the same file, never
  refreshes it, so it keeps the file as it was before tongs-agents first
  touched it;
- `uninstall` leaves it in place, and `doctor` does not report it.

Delete these copies yourself once you no longer need them. A file that did
not exist before `install` gets no backup.

## Refusals

The installer never follows a symlinked config file, never replaces a file
it cannot parse as a JSON object, and keeps each edited file's mode. Such a
refusal prints one line, `tongs-agents: <path>: <reason>`, writes nothing
and exits 1.

For contributors: the refusal type, `cli.ConfigError`, is
`owned.OwnedFileError`, which subclasses `OSError`. A broad
`except OSError` around installer code also catches refusals and would report
them as ordinary I/O errors, so catch `ConfigError` first wherever a refusal
must surface as one.
