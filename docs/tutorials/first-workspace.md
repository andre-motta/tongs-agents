---
title: "Tutorial: your first workspace"
description: "Install tongs-agents, describe your agents as profiles, install hooks and open a workspace with the sidebar."
sidebar:
  order: 1
---

In this tutorial you install tongs-agents, describe four agents as profiles
(two Claude Code config dirs, Codex and Pi), install their hooks, check the
setup with `doctor`, and open a workspace with the sidebar.

It takes about 15 minutes. You need Linux, Python 3.11 or newer and git.
Agents are optional: every step works with the agents you have, and the
[free walkthrough](#no-agent-installed-yet) at the end uses a plain shell
instead.

## 1. Install tongs-agents

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
tongs-agents --help
```

Keep this virtual environment: the hooks you install later point at it. If
you recreate it, run `tongs-agents install` again.

## 2. Get a multiplexer

The workspace runs inside Zellij 0.44.2 or newer. If you already have one on
`PATH`, skip this step. On Linux x86_64 tongs-agents can download the pinned,
checksum-verified build into its state directory:

```bash
tongs-agents doctor --fix zellij
tongs-agents zellij
```

The second command shows which `zellij` the workspace will use. Prefer tmux?
Follow this tutorial, then [tmux instead of Zellij](tmux.md).

## 3. Describe your agents

A profile is a named command that tongs-agents can start in a pane. Create
`~/.config/tongs-agents/config.toml` (or `$XDG_CONFIG_HOME/tongs-agents/config.toml`)
with the agents you have; drop the ones you do not:

```toml
# Claude Code with your normal config dir (~/.claude).
[profiles.claude-main]
command = ["claude"]

# Claude Code with a second config dir, for example another account.
[profiles.claude-alt]
command = ["claude"]
config_dir = "~/.claude-alt"

# Codex. The launcher adds --no-daemon so its hooks run in-process.
[profiles.codex-review]
command = ["codex"]

# Pi with a local model through Ollama (no API cost).
[profiles.pi-local]
command = ["pi", "--provider", "ollama", "--model", "qwen3-coder"]
cwd = "~/src/example"
```

Keep `command[0]` exactly `claude`, `codex` or `pi`: that is how tongs-agents
knows which harness runs. `config_dir` sets `CLAUDE_CONFIG_DIR`, `CODEX_HOME`
or `PI_CODING_AGENT_DIR` for that one profile. The file must belong to you
and must not be writable by group or others. Every key is described in
[agent profiles](../profiles.md).

Check what tongs-agents read:

```bash
tongs-agents profiles
tongs-agents profiles show claude-alt
```

`profiles` lists each profile with its harness, the config home it uses and
its command. A typo in a key is an error that names the file and the key.

## 4. Install the hooks

```bash
tongs-agents install
```

Without options, `install` covers the Claude Code and Codex default homes and
the config home of every profile, so `~/.claude-alt` gets hooks too. Pi is
covered because `pi-local` is a profile; without such a profile, add
`--agent pi` (or `--agent all`). Pi needs version 0.84.4 or newer.

What it writes:

- hooks in `settings.json` (Claude Code) and `hooks.json` (Codex) of each
  home, after keeping a one-time `<name>.tongs-agents.bak` copy of a file
  that already existed;
- the `tongs-agents` MCP server, through `claude mcp add --scope user` and
  `codex mcp add`, when those CLIs are on `PATH`;
- the packaged Pi extension, through Pi itself.

Restart agents that were already running. The first time you start Codex
after this, open `/hooks` and trust the new hooks; Codex does not run them
until you do. Details are in [installing](../install.md).

## 5. Check the setup

```bash
tongs-agents doctor
```

`doctor` only reads. Each finding has a level (`INFO`, `WARN`, `ERROR`) and a
fix. Typical findings at this point:

- a profile's config home is missing hooks: run `tongs-agents install` again;
- `zellij` is missing or too old: see step 2;
- a profile with harness `other` runs unmonitored: expected, see
  [any command as a pane](any-command.md).

Run it until it reports no errors. It exits 0 when only notes remain, 1 with
warnings and 2 with errors.

## 6. Open the workspace

```bash
tongs-agents workspace --profile claude-main --profile claude-alt --profile codex-review --profile pi-local
```

This creates a Zellij session named `agents` with the sidebar and one pane
per profile, then attaches to it. If `tongs` is on `PATH`, a `tongs` tab is
added; `--no-tongs` leaves it out.

To start the same set every time, put it in the config file and run plain
`tongs-agents workspace`:

```toml
[workspace]
default_profiles = ["claude-main", "codex-review"]
```

A new Claude config dir starts without your settings and may ask you to sign
in inside its pane.

## 7. Use the sidebar

Each row is one pane. Rows with hooks show the agent's status (`RUNNING`,
`IDLE`, `WAITING` and so on); a pane without hook data yet shows its pane
state (`STARTING`, `RUNNING` or `EXITED`).

| Key | Action |
| --- | --- |
| Enter or click | Focus that agent's pane |
| `n` | Jump to the next agent that needs you (waiting, or idle for more than two minutes) |
| `m` | Show or hide the chat panel ([tutorial 5](chat-and-web.md)) |
| `r` | Refresh |
| `q` | Quit the sidebar |
| Ctrl+P | Command palette |

Give an agent a prompt in its pane, then watch its row go from `RUNNING` to
`IDLE`. Open the palette with Ctrl+P and choose `New agent: pi-local` to add
another pane to the running session.

## 8. Leave and come back

Detach from Zellij with its own keys (Ctrl o, then d by default); the agents
keep running. Run `tongs-agents workspace` again to attach. Running it with
`--profile` while the session runs adds those panes; it never duplicates the
default profiles.

When you quit the session, Zellij keeps it as `EXITED`. The next
`tongs-agents workspace` deletes exactly that exited session and starts it
fresh; `--resurrect` attaches to it instead (agents are not restarted).
See [agent workspace](../workspace.md).

To see every agent on the machine, including ones started outside any
workspace, run `tongs-agents monitor` or `tongs-agents status`.

## No agent installed yet

You can try the workspace with a shell standing in for an agent. Add this
profile; its harness is `other` because `bash` is not a known harness:

```toml
[profiles.scratch]
command = ["bash", "--norc"]
```

```bash
tongs-agents workspace --session tryout --profile scratch
```

The sidebar shows `scratch` as `RUNNING` until you type `exit` in its pane,
then as `EXITED 0`. The next tutorial makes such panes report real status.

## Next

- [Any command as a pane](any-command.md)
- [One dotfiles directory for every harness](dotfiles.md)
- [Driving agents](driving-agents.md)
