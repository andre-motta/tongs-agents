---
title: "Tutorial: your first workspace"
description: "Install tongs-agents, describe your agents as profiles, install hooks, open the workspace and add agents to it."
sidebar:
  order: 1
---

In this tutorial you install tongs-agents, describe four agents as profiles
(two Claude Code config dirs, Codex and Pi), install their hooks, check the
setup with `doctor`, open the workspace and add agents to it in the folders
you work in.

It takes about 15 minutes. You need Linux, Python 3.11 or newer and
[pipx](https://pipx.pypa.io/) or [uv](https://docs.astral.sh/uv/).
Agents are optional: every step works with the agents you have, and the
[free walkthrough](#no-agent-installed-yet) at the end uses a plain shell
instead.

## 1. Install tongs-agents

```bash
pipx install tongs-agents        # or: uv tool install tongs-agents
tongs-agents --help
```

The hooks and MCP registrations you install later point at this tool
environment. If you move or recreate it, run `tongs-agents install` again.
`pip install tongs-agents` into a virtual environment works too; then keep
that environment. To work from a clone instead, see
[Installing](../install.md#get-the-package).

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

A profile is a template for an agent: the command, the harness, the config
dir, the model. You choose the folder, and any extra arguments such as
`--resume`, each time you start one. Create
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

# Pi with a local model through Ollama (no API cost). cwd is only a default
# folder (the palette's "New agent: pi-local" starts there); replace it with
# a project directory you have, or drop it.
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
- the `tongs-agents` MCP server, through `claude mcp add --scope user` for
  Claude Code and as a table in Codex's `config.toml`, when those CLIs are on
  `PATH`;
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
- the Pi extension is not installed because Pi is missing or older than
  0.84.4 (a warning): install Pi and run `tongs-agents install` again, or
  remove the `pi-local` profile;
- a profile's `cwd` is not a directory (a warning): create it or change `cwd`
  in step 3;
- `zellij` is missing or too old: see step 2;
- a profile with harness `other` has no hooks (a note): expected, it reports
  with `tongs-agents notify`, see [any command as a pane](any-command.md).

Run it until it reports no errors. It exits 0 when only notes remain, 1 with
warnings and 2 with errors.

## 6. Open the workspace

```bash
tongs-agents
```

This creates a Zellij session named `agents` with only the sidebar, then
attaches to it. If `tongs` is on `PATH`, a `tongs` tab is added. No agent
starts yet; you add them next. Run `tongs-agents` again later and it attaches
to the same session.

## 7. Add agents

Open a shell pane in the workspace (in Zellij, Ctrl p then n) and start
agents in the folders you work in:

```bash
cd ~/src/example
tongs-agents run claude-main                       # in the current directory
tongs-agents run claude-alt ~/src/other            # in another folder
tongs-agents run codex-review ~/src/example -- resume --last
```

Each command adds one pane and returns. Everything after `--` is appended to
the profile's command for that launch only, so `-- --resume` continues a
Claude Code conversation and `-- resume --last` the last Codex one. The
folder must exist. `run` works from a terminal outside the workspace too: it
adds the pane to the running session, or opens the workspace with that pane
when none is running.

Or stay in the sidebar: press `+` (or Ctrl+P, then `Add agent…`), pick a
profile, type a folder (Right arrow completes directory names), optionally
type arguments such as `--resume`, check the command shown under the fields
and choose Start.

A new Claude config dir starts without your settings and may ask you to sign
in inside its pane.

## 8. Use the sidebar

Each row is one pane. Rows with hooks show the agent's status (`RUNNING`,
`IDLE`, `WAITING` and so on); a pane without hook data yet shows its pane
state (`STARTING`, `RUNNING` or `EXITED`).

| Key | Action |
| --- | --- |
| Enter or click | Focus that agent's pane |
| `n` | Jump to the next agent that needs you (waiting, or idle for more than two minutes) |
| `+` | Add an agent: profile, folder and arguments |
| `m` | Show or hide the chat panel ([tutorial 5](chat-and-web.md)) |
| `r` | Refresh |
| `q` | Quit the sidebar |
| Ctrl+P | Command palette |

Give an agent a prompt in its pane, then watch its row go from `RUNNING` to
`IDLE`. The palette's `New agent: pi-local` starts a profile in its default
folder in one step.

## 9. Leave and come back

Detach from Zellij with its own keys (Ctrl o, then d by default); the agents
keep running. Run `tongs-agents` again to attach; it never starts agents.

When you quit the session, Zellij keeps it as `EXITED`. The next
`tongs-agents` deletes exactly that exited session and starts it fresh;
`tongs-agents workspace --resurrect` attaches to it instead (agents are not
restarted). Prefer the same set of agents every time? `tongs-agents workspace
--profile NAME` (or `[workspace].default_profiles`) starts profiles in their
default folders with a new session. See [agent workspace](../workspace.md).

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
tongs-agents run --session tryout scratch /tmp
```

This opens a workspace named `tryout` with the sidebar and the `scratch`
pane in `/tmp`. The sidebar shows `scratch` as `RUNNING` until you type
`exit` in its pane,
then as `EXITED 0`. The next tutorial makes such panes report real status.

## Next

- [Any command as a pane](any-command.md)
- [One dotfiles directory for every harness](dotfiles.md)
- [Driving agents](driving-agents.md)
