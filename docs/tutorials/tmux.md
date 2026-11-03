---
title: "Tutorial: tmux instead of Zellij"
description: "Run the same workspace on tmux and learn what differs from Zellij."
sidebar:
  order: 7
---

Zellij is the default multiplexer, but the workspace also runs on tmux 3.2 or
newer. Profiles, the sidebar, `notify`, `send`, `read` and `wait` work the
same; a few Zellij features have no tmux counterpart. This tutorial
assumes the profiles from [your first workspace](first-workspace.md).

## 1. Check tmux

```bash
tmux -V
```

You need 3.2 or newer. `tongs-agents workspace` reads the version before it
starts anything and stops with a message when tmux is missing or older.

## 2. Open the workspace on tmux

```bash
tongs-agents workspace --multiplexer tmux --profile claude-main --profile scratch
```

Run it from a plain terminal, not from inside Zellij (that is refused, so
agent panes never inherit a Zellij pane identity). Outside tmux it creates
the session `agents` detached, with the sidebar as its first pane, starts one
pane per profile and attaches. Inside tmux it adds the panes to your current
session instead, without nesting.

To make tmux the default, add to `~/.config/tongs-agents/config.toml`:

```toml
[workspace]
multiplexer = "tmux"
```

Then plain `tongs-agents workspace` uses tmux, and `--multiplexer zellij`
switches back for one run.

## 3. What the sidebar shows

A new session shows one agent at a time: the sidebar's window holds the
sidebar and the agent in view, and every other agent waits in its own
window, named after its profile in the status bar. Select a row (Enter or a
click) and that agent is swapped in next to the sidebar; the one it replaces
goes back to a window of its own and keeps running. When the agent in view
exits, the next one you select is joined beside the sidebar. Panes are named
after their profile in the tmux pane option `@tongs_agents_name`.

To lay agents out side by side instead (a pane opens in a new window when
there is no room left), set this before you create the session:

```toml
[workspace]
layout = "split"
```

On tmux the sidebar lists the panes the launcher started. A pane whose agent
reports through hooks or `tongs-agents notify` shows that status (`RUNNING`,
`IDLE`, `WAITING`), joined through its launch id, so `n` jumps to an agent
that needs you and the sidebar notifies as on Zellij. A pane with no reported
status shows its pane state (`RUNNING`, `STARTING`). The palette's
`New agent: <profile>` starts a profile in the current tmux session.

## 4. Drive panes

The commands from [driving agents](driving-agents.md) work unchanged:

```bash
tongs-agents agent-status
tongs-agents send LAUNCH_ID "echo hello"
tongs-agents read LAUNCH_ID --lines 5
```

On tmux a paste goes through a uniquely named tmux buffer, bracketed when the
program asked for it, and Enter is sent separately. Every call is verified
and audited as on Zellij.

## 5. What differs from Zellij

| Feature | tmux |
| --- | --- |
| `--resurrect` | Ignored with a notice: tmux keeps no exited sessions |
| tongs tab | None; `--tongs` is ignored with a notice |
| An exited agent | tmux closes the pane unless `remain-on-exit` is on, so `send`, `read` and `wait` report the pane as not listed (exit 5) instead of exited |
| Chat wake | tmux marks the active pane of every window as focused, so a wake is almost always skipped; wake works on Zellij only for now |
| Web watching | Zellij only |
| Session names | No `.` or `:` in a new session name |
| `--print-command` | Creates a missing session detached right away, then prints the attach command |
| One agent at a time | Parked windows and a swap into the sidebar's window instead of a stack; the parked windows show in the status bar, and resizing the terminal also resizes the sidebar |

tmux calls always name the server socket with `-S`: the one in `TMUX` inside
tmux, else tmux's default socket. tongs-agents checks that the default
socket directory is yours and private before it uses it.

## What you learned

- `--multiplexer tmux`, or `[workspace] multiplexer = "tmux"`, runs the same
  workspace on tmux.
- A new tmux session shows one agent at a time and swaps the one you select
  into view; `layout = "split"` lays them out side by side.
- The sidebar shows hook and `notify` status on tmux, joined by launch id,
  and the pane state for panes that report none.
- Resurrect, the tongs tab, chat wake and web watching stay with Zellij.

Reference: [agent workspace on tmux](../workspace.md#tmux).
