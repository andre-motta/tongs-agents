# App settings, command palette and theme

`tongs-agents monitor` and the workspace sidebar share one app shell: the same
header and footer, a command palette (`Ctrl+P`) and a theme. Their defaults,
and those of `tongs-agents workspace`, come from three sections of the
tongs-agents config file, the same `config.toml` that holds the
[profiles](profiles.md).

tongs-agents only reads this file; it never writes or reformats it. It is
read with the same checks as the profiles: a regular file (not a symlink)
owned by you, not writable by group or others, at most 64 KiB. Sections other
than `[profiles]`, `[workspace]`, `[ui]`, `[control]` and `[dotfiles]` are
ignored. Inside the three sections below an unknown key is an error, and every
error names the file and the key, for example
`.../config.toml: ui.notify_level: must be one of all, waiting, none`.

## Example

Every key is optional; the values shown are the defaults except where noted.

```toml
[workspace]
session = "agents"              # Zellij session opened by `tongs-agents workspace`
default_profiles = []           # profiles a new session starts, e.g. ["claude-alt"]
tongs_tab = true                # add a tongs tab when tongs is on PATH
multiplexer = "zellij"          # "zellij" or "tmux"; see below

[ui]
refresh_interval = 2            # seconds, 0.5 to 3600
notify_level = "all"            # "all", "waiting" or "none"
theme = "textual-dark"          # any Textual theme name
compact = false                 # always use compact rows in the monitor

[control]
mcp_enabled = false             # agent-to-agent MCP control tools; see mcp-control.md
send_delay_ms = 150             # 0 to 5000
```

`[control]` has three more keys for MCP agent control (`mcp_any_target`,
`mcp_nested_spawn` and `mcp_rate_per_minute`); see
[MCP agent control](mcp-control.md#turning-it-on).

## Precedence

A command-line flag always wins over the file, and the file over the built-in
default.

| Setting | Flag |
| --- | --- |
| `[workspace].session` | `workspace --session NAME` |
| `[workspace].default_profiles` | any `workspace --profile NAME` replaces the whole list |
| `[workspace].tongs_tab` | `workspace --tongs` / `--no-tongs` |
| `[workspace].multiplexer` | `workspace --multiplexer zellij` / `tmux` |
| `[ui].refresh_interval` | `monitor --refresh SECONDS` |
| `[ui].notify_level` | `monitor --notify LEVEL` |
| `[ui].theme` | `monitor --theme NAME` |
| `[ui].compact` | `monitor --compact` / `--no-compact`; `sidebar --compact` |

## Who reads what

- `tongs-agents workspace` uses `session`, `tongs_tab`, `default_profiles`
  and `multiplexer`.
  The default profiles start only when the command creates a new session.
  Attaching to a running session, or running the command inside the
  workspace's multiplexer,
  starts only the profiles named with `--profile`, so re-running the command
  never duplicates agents. An unknown default profile is a config error only
  when a new session would start it; attaching to a running session still
  works. See [workspace](workspace.md).
- `tongs-agents monitor` uses all of `[ui]`. An invalid file prints the error
  and the monitor starts with the default settings.
- The sidebar uses `refresh_interval`, `notify_level` and `theme`, and
  `compact` when the layout does not already pass `--compact`. It always works
  on the Zellij or tmux session it runs in, so `[workspace].session` does not
  apply to it; `default_profiles` lists those profiles first in its palette and
  `tongs_tab = false` hides `Open tongs tab`. An invalid file does not stop
  the sidebar, because it still has to start the requested agent panes: it
  runs with the defaults and shows the error as a notification.
- `tongs-agents send` and the MCP control tools read `[control]` on every
  call; see [driving agent panes](control.md) and
  [MCP agent control](mcp-control.md). `mcp_enabled` stays off by default.

## Command palette

`Ctrl+P` opens Textual's command palette in the monitor and the sidebar. Next
to Textual's own entries (theme switcher, keys, quit) it lists:

| Command | When offered | What it does |
| --- | --- | --- |
| `New agent: <profile>` | inside a Zellij or tmux session, one per profile | Starts the profile in a new pane of the current session through the same launcher path as `tongs-agents workspace`. `[workspace].default_profiles` come first, then the rest alphabetically. |
| `Open tongs tab` | inside a Zellij or tmux session, `tongs` on `PATH`, `tongs_tab` not false | Starts `tongs` in a new focused pane named `tongs` in the current session. |
| `Next needing attention` | always | Same as the `n` key. |
| `Focus session` | always | Focuses the selected agent, same as `Enter`. |
| `Toggle detail` | always | Shows or hides the detail panel, same as `d` in the monitor. |
| `Reload profiles` | always | Reads the profiles from the config file again; a broken file is shown as an error and its message is kept in this entry's help line. |

The monitor counts as inside a session when it runs in a Zellij pane (the
sidebar also detects tmux); started elsewhere it offers only the last four
commands. In the sidebar the
palette also has the chat entries (`Grant chat`, `Grant chat and wake`,
`Revoke chat`, `Post to chat`, `Toggle chat` and `Open chat pane`); see
[agent chat](chat.md). A spawn that fails is shown as an error notification and
its launch record is removed.

## Theme

`[ui].theme` (or `monitor --theme`) selects a built-in Textual theme, for
example `textual-dark` (the default), `textual-light`, `nord`, `gruvbox`,
`dracula`, `tokyo-night`, `monokai` or `solarized-light`. The available set
depends on the installed Textual release, so an unknown name does not stop the
app: it keeps `textual-dark` and shows a warning. The palette's own theme
switcher changes the theme for the running app only; it does not edit the
config file.

## Monitor keys

`tongs-agents monitor` is the fleet-wide view of every session on the machine.

| Key | Action |
| --- | --- |
| Enter or click | Focus the session's terminal |
| `/`, Esc | Search by repo, detail or status; clear |
| `1` `2` `3` `4`, `0` | Filter running, idle, waiting, error; show all |
| `s` | Cycle sort |
| `d` | Toggle the detail panel |
| `n`, `N` | Next or previous session needing attention |
| `o` | Open the session's directory |
| `c`, `C` | Copy a summary or the JSON record |
| `r`, `q` | Refresh; quit |

A session needs attention when it is `waiting`, or `idle` for more than two
minutes. With the default `--notify all`, the monitor sends a `notify-send`
alert for each one (Linux only) and rings the terminal bell for waiting
sessions. `--notify waiting` alerts on waiting sessions only; `--notify none`
turns alerts off. `[ui].notify_level` sets the default.

## Focusing a session from the command line

`tongs-agents focus <query>` matches exactly, then by substring, over the
session ID, repo and PID, and focuses the captured terminal. It reports
complete, partial, unavailable or failed, and lists the candidates when
several sessions match. What each terminal supports is in the
[support matrix](support-matrix.md).

## The tongs plugin

With the `tongs` extra installed (`pip install "tongs-agents[tongs]"`, or
`pip install -e ".[tongs]"` from source), tongs finds the plugin under the
`tongs.plugins` entry point `agents`. In tongs, open the command palette
(Ctrl+P) and choose "Agents" for the agents screen, or "Open agent workspace"
to open or join the workspace. Its `refresh_interval` and `notify_level`
settings live in `[plugins.agents]` in the tongs config.
