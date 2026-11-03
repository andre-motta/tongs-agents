---
title: "Installing tongs-agents"
description: "Install tongs-agents and see what install changes in Claude Code, Codex and Pi files."
---

## Get the package

tongs-agents needs Python 3.11 or newer. Its runtime dependencies are
`textual` and `mcp`. Install it from PyPI as a tool:

```bash
pipx install tongs-agents     # or: uv tool install tongs-agents
```

`pip install tongs-agents` into a virtual environment works too. The tongs
plugin needs the `[tongs]` extra in the environment tongs runs from:
`pip install "tongs-agents[tongs]"` there, or `pipx inject tongs tongs-agents`
when tongs came from pipx.

To work on tongs-agents itself, install it editable from a clone:

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
```

Hooks and MCP registrations point at the entry points of the environment you
installed into (`tongs-agents`, `tongs-agents-hook` and its Python): the pipx
or uv tool environment, your virtual environment, or the clone's `.venv`. If
you move or recreate the environment, run `tongs-agents install` again.

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
when that harness's CLI is on `PATH`, and with Pi installs the packaged
extension ([Pi](pi.md)). Claude Code gets the server through
`claude mcp add --scope user`. Codex gets one `[mcp_servers.tongs-agents]`
table in its `config.toml`, written by tongs-agents itself (see
[Codex config.toml](#codex-configtoml)). It is idempotent. The summary at
the end names the harnesses actually set up, and any that got hooks only,
either because their CLI was missing or because the MCP server could not be
registered (the messages above it say why). Restart running agent sessions afterwards; in
Codex, review and trust the new hooks with `/hooks`.

A profile with a `sandbox` table is skipped: its agent runs inside OpenShell,
whose image carries its own hooks ([sandbox](sandbox.md)), so install,
uninstall and doctor do not treat the host config home as its home.

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
pipx uninstall tongs-agents         # or uv tool uninstall, or pip uninstall, matching how you installed it
```

The rest of this page covers how `install` and `uninstall` change files and
what is left behind.

## Order of operations

`install` runs in this order:

1. Every hook file it targets (Claude Code `settings.json` and Codex
   `hooks.json`, in the default homes, in `--claude-config-dir` and
   `--codex-home` dirs and in the config home of every profile) is staged
   into one change set and committed as one unit.
2. Then, per target, the `tongs-agents` MCP server is registered, when that
   harness's CLI is on `PATH`: through `claude mcp add --scope user` for
   Claude Code, and as the `[mcp_servers.tongs-agents]` table in Codex's
   `config.toml`.
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
set, then MCP entries are removed per target. Our Codex tables are removed
from `config.toml` whether or not the Codex CLI is on `PATH`.

## Codex config.toml

Codex keeps MCP servers as `[mcp_servers.<name>]` tables in
`$CODEX_HOME/config.toml` (`~/.codex/config.toml` by default). `install`
and `uninstall` never run `codex mcp add` or `codex mcp remove`, which drop
every user comment in all `[mcp_servers.*]` tables. They edit only these
tables, with the same owned-table writer `config apply` uses:

- `[mcp_servers.tongs-agents]`, added or replaced when it starts
  `python -m tongs_agents.mcp_server` and differs from what install writes,
  and removed by `uninstall`; an `enabled = false` in it is kept;
- `[mcp_servers.fleet]`, removed when it starts the claude-fleet-monitor
  server.

A `tongs-agents` table that starts anything else is the user's: install
keeps it and says so. Every other byte of the file, comments included, stays
as it was. When the file defines our table in a form the writer cannot edit
exactly (an inline table or dotted keys), or when it is not valid TOML,
install leaves the file alone and prints
`Could not register the Codex MCP server: <path>: <reason>`.

A symlinked `config.toml` (for example one managed by stow from a dotfiles
repository) is left untouched as well: install refuses to write through the
link, and `config apply` follows the same rule. Add the table to the file the
link points at by hand, with the Python that has tongs-agents installed as
`command` (`python3 -c 'import sys; print(sys.executable)'` from that
environment prints it):

```toml
[mcp_servers.tongs-agents]
command = "/path/to/python"
args = ["-m", "tongs_agents.mcp_server"]
env_vars = ["TONGS_AGENTS_LAUNCH_ID", "TONGS_AGENTS_PROFILE", "ZELLIJ_SESSION_NAME", "ZELLIJ_SOCKET_DIR", "TMUX", "TMUX_PANE", "XDG_STATE_HOME", "TONGS_AGENTS_STATE_DIR", "XDG_CONFIG_HOME", "TONGS_AGENTS_CONFIG_DIR", "XDG_RUNTIME_DIR"]
```

While the link stays, every install prints the refusal and its summary
lists Codex as hooks only, even when the table is already in place.

Codex starts a stdio MCP server with only `HOME`, `LANG`, `PATH`, `TMPDIR`
and `USER` plus the variables named in its `env_vars`. The table therefore
lists the variables the server reads:

| Variables | Why |
| --- | --- |
| `TONGS_AGENTS_LAUNCH_ID`, `TONGS_AGENTS_PROFILE` | Launch identity for the chat tools and the `agents_*` caller |
| `ZELLIJ_SESSION_NAME`, `ZELLIJ_SOCKET_DIR`, `TMUX`, `TMUX_PANE` | The workspace pane and session the agent runs in |
| `XDG_STATE_HOME`, `TONGS_AGENTS_STATE_DIR` | The sessions and launches the hooks write |
| `XDG_CONFIG_HOME`, `TONGS_AGENTS_CONFIG_DIR` | Profiles and `[control]` settings |
| `XDG_RUNTIME_DIR` | Control locks and rate limits shared with the CLI |

`config apply` writes the same table, so either command adopts what the
other wrote.

## Backups

Before the first edit of an existing `settings.json`, `hooks.json` or Codex
`config.toml`, the owned-file writer keeps a copy of it next to the file as
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
