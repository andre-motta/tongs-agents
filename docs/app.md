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
mcp_enabled = false             # agent-to-agent MCP control tools (not available yet)
send_delay_ms = 150             # 0 to 5000
```

## Precedence

A command-line flag always wins over the file, and the file over the built-in
default.

| Setting | Flag |
| --- | --- |
| `[workspace].session` | `workspace --session NAME` |
| `[workspace].default_profiles` | any `workspace --profile NAME` replaces the whole list |
| `[workspace].tongs_tab` | `workspace --tongs` / `--no-tongs` |
| `[ui].refresh_interval` | `monitor --refresh SECONDS` |
| `[ui].notify_level` | `monitor --notify LEVEL` |
| `[ui].theme` | `monitor --theme NAME` |
| `[ui].compact` | `monitor --compact` / `--no-compact`; `sidebar --compact` |

## Who reads what

- `tongs-agents workspace` uses `session`, `tongs_tab` and `default_profiles`.
  The default profiles start only when the command creates a new session.
  Attaching to a running session, or running the command inside Zellij,
  starts only the profiles named with `--profile`, so re-running the command
  never duplicates agents. An unknown default profile is a config error only
  when a new session would start it; attaching to a running session still
  works. See [workspace](workspace.md).
- `tongs-agents monitor` uses all of `[ui]`. An invalid file prints the error
  and the monitor starts with the default settings.
- The sidebar uses `refresh_interval`, `notify_level` and `theme`, and
  `compact` when the layout does not already pass `--compact`. It always works
  on the Zellij session it runs in, so `[workspace].session` does not apply to
  it; `default_profiles` lists those profiles first in its palette and
  `tongs_tab = false` hides `Open tongs tab`. An invalid file does not stop
  the sidebar, because it still has to start the requested agent panes: it
  runs with the defaults and shows the error as a notification.
- `[workspace].multiplexer` and all of `[control]` are parsed and validated
  now so that a typo is caught early, but nothing acts on them yet: the
  workspace always uses Zellij until the tmux adapter lands, and the MCP
  control tools are a later milestone. `mcp_enabled` stays off by default.

## Command palette

`Ctrl+P` opens Textual's command palette in the monitor and the sidebar. Next
to Textual's own entries (theme switcher, keys, quit) it lists:

| Command | When offered | What it does |
| --- | --- | --- |
| `New agent: <profile>` | inside a Zellij session, one per profile | Starts the profile in a new pane of the current session through the same launcher path as `tongs-agents workspace`. `[workspace].default_profiles` come first, then the rest alphabetically. |
| `Open tongs tab` | inside a Zellij session, `tongs` on `PATH`, `tongs_tab` not false | Starts `tongs` in a new focused pane named `tongs` in the current session. |
| `Next needing attention` | always | Same as the `n` key. |
| `Focus session` | always | Focuses the selected agent, same as `Enter`. |
| `Toggle detail` | always | Shows or hides the detail panel, same as `d` in the monitor. |
| `Reload profiles` | always | Reads the profiles from the config file again; a broken file is shown as an error and its message is kept in this entry's help line. |

The monitor counts as inside a Zellij session when it runs in a Zellij pane
(`ZELLIJ` and the session name are set); started elsewhere it offers only the
last four commands. A spawn that fails is shown as an error notification and
its launch record is removed.

## Theme

`[ui].theme` (or `monitor --theme`) selects a built-in Textual theme, for
example `textual-dark` (the default), `textual-light`, `nord`, `gruvbox`,
`dracula`, `tokyo-night`, `monokai` or `solarized-light`. The available set
depends on the installed Textual release, so an unknown name does not stop the
app: it keeps `textual-dark` and shows a warning. The palette's own theme
switcher changes the theme for the running app only; it does not edit the
config file.
