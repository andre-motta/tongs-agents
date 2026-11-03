# Installing tongs-agents

## Get the package

tongs-agents needs Python 3.11 or newer. Its runtime dependencies are
`textual` and `mcp`. Install it from source into a virtual environment:

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e .              # or: pip install -e ".[tongs]" for the tongs plugin
```

Hooks and MCP registrations point at the entry points of this environment
(`tongs-agents`, `tongs-agents-hook` and its Python). If you move or recreate
the environment, run `tongs-agents install` again.

The workspace needs a multiplexer: Zellij 0.44.2 or newer, or tmux 3.2 or
newer. On Linux x86_64, `tongs-agents doctor --fix zellij` downloads the
pinned Zellij build into the state directory; see
[Zellij pins](zellij-pins.md#managed-download). Terminal focus uses optional
tools when present (`qdbus`, `xdotool`, `kitten`, the `tmux`, `zellij` and
`screen` CLIs); desktop notifications use `notify-send` on Linux.

## Install the hooks

```bash
tongs-agents install                          # Claude Code, Codex and every profile's config dir
tongs-agents install --agent pi               # also Pi (0.84.4 or newer); or --agent all
tongs-agents install --claude-config-dir DIR  # only DIR, instead of the defaults; also --codex-home, --pi-agent-dir
tongs-agents doctor                           # read-only report of what is missing
```

`install` writes Claude Code hooks to `settings.json` and Codex hooks to
`hooks.json` in each config home, registers the `tongs-agents` MCP server
through `claude mcp add --scope user` and `codex mcp add` when those CLIs are
on `PATH`, and with Pi installs the packaged extension ([Pi](pi.md)). It is
idempotent. Restart running agent sessions afterwards; in Codex, review and
trust the new hooks with `/hooks`.

Session records go to `$XDG_STATE_HOME/tongs-agents/sessions/`
(`~/.local/state/tongs-agents/sessions/` by default), a private 0700
directory. Set `TONGS_AGENTS_STATE_DIR` to move the whole state directory.

`doctor` only reads. It exits 0 when nothing is worse than a note, 1 for a
warning and 2 for an error. Each finding names its fix, for example a missing
hook in a profile's config dir or a missing Zellij. `--json` prints the checks
as JSON, and `--config-dir KIND=DIR` also checks one more config dir.

## Uninstall

```bash
tongs-agents uninstall              # hooks, MCP entries and session records
tongs-agents uninstall --keep-data  # keep the sessions directory
tongs-agents uninstall --agent pi   # only the Pi extension
pip uninstall tongs-agents
```

The rest of this page covers how `install` and `uninstall` change files and
what is left behind.

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
