# Support matrix

This page holds the evidence-based harness and terminal support matrices for
tongs-agents. It describes what the code on `main` does today. Planned work is
tracked in the [agent workspace plan](work/agent-workspace-plan.md).

## Harness support

The support matrix separates lifecycle ingestion from process identity and
focus. A lifecycle event does not by itself prove that a process is
discoverable, that a terminal target can be selected, or that a desktop window
can be activated. See the [adapter author guide](harness-adapters.md) and
the implemented [event, identity and focus contract](work/harness-contract.md)
before adding another harness.

| Harness | Lifecycle ingestion | Process identity | Terminal selection | GUI window activation | MCP consumer access | Native harness MCP registration |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Code | Installed native hooks for start, prompt, tool, permission, stop, failure, elicitation and end | Discoverable by registered exact process names and ancestry; unresolved records are explicit when process evidence is unavailable | Captured terminal metadata is dispatched to the matching backend; result is complete, partial, unavailable or failed according to observed operations | Desktop conversation targets have no accepted backend; parent-terminal activation is backend-specific | The `tongs-agents` MCP server is available to configured Claude Code sessions | When the Claude Code CLI is available, the default installer registers it with `claude mcp add --scope user` |
| Codex | Installed native hooks for start, prompt, tool, permission, stop and end | Discoverable by registered exact process names and ancestry; unresolved records are explicit when process evidence is unavailable | Captured terminal metadata is dispatched to the matching backend; result is complete, partial, unavailable or failed according to observed operations | Desktop conversation targets have no accepted backend; parent-terminal activation is backend-specific | The `tongs-agents` MCP server is available to configured Codex sessions | When the Codex CLI is available, the default installer registers it with `codex mcp add` |
| Pi | Optional dependency-free extension emits normalized `fleet-event` records | Emitter supplies the exact Pi PID, native session ID, extension instance and sequence; Pi is not process-name discovered | Accepted live validation selected the exact tmux pane for a real Pi process; other terminal backends require their own evidence | No live desktop GUI result is claimed; detached tmux has no GUI parent and reports activation separately | Consumers can read Pi records, but Pi itself has no tongs-agents MCP client | None; Pi installation adds the lifecycle extension only |

The evidence behind this table has defined limits:

- The strict [shell validation](../validation/shells/README.md) runs the existing
  hook through nine real shells in rootless Podman. Its 18 lifecycle cases,
  nine manual quoted-path controls and 108 generated-command checks use
  synthetic payloads. They do not prove live Claude or Codex processes, TTY
  discovery, terminal focus or GUI activation.
- The [Pi validation](../validation/pi/README.md) runs a real Pi 0.85.0 process
  with Node.js 22.23.1 and Python 3.12.14 against a synthetic localhost
  provider. It validates lifecycle behavior and exact tmux pane selection in an
  isolated server. It does not claim a desktop window result.
- Platform-specific process and terminal behavior in the regular test suite is
  mocked. No live Claude or Codex runtime or desktop GUI acceptance is included
  in this matrix.

## Agent workspace, multiplexers and profiles

The [agent workspace](workspace.md) runs agents as panes of a Zellij session
next to the tongs-agents sidebar; [profiles](profiles.md) name the commands it
starts. The instrumented evidence comes from the
[workspace validator](../validation/README.md#agent-workspace-on-linux), which
runs the real `install`, `workspace`, sidebar, launcher and hook against real
Zellij servers and PTY clients in a private root, with fake harness fixtures
in place of agents. It runs locally and in the hosted `workspace-validation`
job on both pinned Zellij releases
([JSON](../validation/evidence/workspace-linux-x86_64.json)). The maintainer's
hands-on run with real profiles has not been recorded yet
([runbook](work/workspace-live-run.md)), so no workspace row is labelled live.

| Multiplexer | Workspace session and spawn request | In-session rerun | Row focus from the sidebar | Evidence |
| --- | --- | --- | --- | --- |
| **Zellij 0.44.2 and 0.45.1** (Linux x86_64) | Yes: `workspace --profile ... --no-tongs` creates the session in under 5 s with the sidebar plus one pane per profile; the sidebar consumes the spawn request once and removes the layout | Yes: run inside the session it adds panes to the same session, keeps one sidebar and starts no second server | Yes: Enter on a hook or launch row selects that pane for the most recent input client, confirmed by `list-clients` with a second client attached; window activation is skipped, never attempted | instrumented (validator on both pins, [JSON](../validation/evidence/workspace-linux-x86_64.json)); mocked; live run pending ([runbook](work/workspace-live-run.md)) |
| **Zellij older than 0.44.2** | No: `workspace` stops and `doctor` reports an error | -- | -- | mocked; the [version floor](../validation/evidence/zellij-version-floor-fedora44.json) is live for pane selection |
| **Zellij newer than 0.45.1** | Allowed with a `doctor` warning | Unvalidated | Unvalidated | mocked |
| **Zellij on macOS or Linux aarch64** | Not run | Unvalidated | Unvalidated | unvalidated |
| **tmux 3.2 or newer** | Yes: `workspace --multiplexer tmux` creates the session detached with the sidebar as its first pane, spawns the profiles and attaches | Yes: inside tmux it adds panes to the current session | The sidebar lists launch rows with the pane state; hook status shows in `monitor` and `agent-status`. Pane focus selects the window and pane with readback (validator, not through a sidebar row) | instrumented (validator on real tmux servers with fake REPL fixtures, [JSON](../validation/evidence/tmux-workspace-linux-x86_64.json)); mocked |
| **Windows** | No: `workspace` reports unsupported and exits 2 | -- | -- | mocked |

| Profile kind | What the launcher sets | Status in the sidebar | Evidence |
| --- | --- | --- | --- |
| Any profile in the workspace | `TONGS_AGENTS_LAUNCH_ID` and `TONGS_AGENTS_PROFILE`; `TMUX` dropped inside Zellij | A hook row joined to its launch record within 4 s of the pane appearing, named after the profile; a launch row with the pane state until the first hook event | instrumented (pi and Claude fixtures report the inherited launch identity and pane, and the hook record carries it); mocked |
| Claude Code with `config_dir` | `CLAUDE_CONFIG_DIR` | Hook status once `install --claude-config-dir DIR` (or a profile-driven `install`) wrote the hooks into that dir; without them only a `discovered` launch row | instrumented (a fixture runs the hooks installed in its own config dir; an uninstalled control dir stays `discovered`; MCP registration targeted the dir through a stub CLI); mocked (two dirs, HOME untouched); live pending |
| Codex with `config_dir` or `--profile` | `CODEX_HOME`; `--no-daemon` appended so hooks fire in-process | Hook status from the Codex hooks | mocked; live pending |
| pi with `config_dir`, `--provider` and `--model` | `PI_CODING_AGENT_DIR`; arguments passed as argv | Hook status from the pi extension's `fleet-event` records | instrumented (fixture sends `fleet-event` through `tongs-agents-hook` with its own pid); mocked; live pending |
| `other` (any command or wrapper) | Identity variables only | Launch row with the pane state (`starting`, `running`, `exited N`) unless the command runs a hook | mocked |

## Supported terminals

The table lists implemented backend capabilities and the strongest evidence
behind each claim. Runtime availability, selected-target proof and window
activation vary by environment and are reported as structured focus outcomes;
a successful command alone is never reported as focus. Evidence labels:
**live** (real program observed on Fedora 44 KDE Plasma Wayland),
**instrumented** (real program with tongs-agents calls recorded), **mocked** (pytest
with substituted OS calls), **inspected** (source or documentation only) and
**unvalidated** (not observed).

| Terminal | Tab or pane selection | Window raise | Nested support | Evidence |
|----------|:---:|:---:|:---:|---|
| **KDE Konsole** | Yes (qdbus, qdbus6 or qdbus-qt6, D-Bus readback) | Yes (KWin, one unique owned window, readback) | -- | live: Konsole 26.08 tab selection ([JSON](../validation/evidence/konsole-fedora44.json)); cross-window tab plus window confirmed hands-on through the TUI; mocked; as a tmux parent the Konsole service owned by the client's nearest ancestor is used (mocked) |
| **tmux** | Yes (stable IDs, readback) | Via parent terminal, per attached client; failures name the reason (no attached client, no parent emulator, unsupported or unconfirmed parent) | Yes | live: tmux 3.7c selection ([JSON](../validation/evidence/tmux-fedora44.json)); parent routing instrumented and mocked; failure details mocked |
| **zellij** | Yes (`focus-pane-id`, `list-clients` before and after readback); needs an attached client; custom socket dirs supported | Linux only, via parent terminal when exactly one client is attached (client found by its socket peer) | Yes | live: zellij 0.45.1 selection, custom and derived socket dirs, client mapping and refusals ([JSON](../validation/evidence/zellij-fedora44.json)); floor 0.44.2, validator passing on 0.44.2 to 0.45.1 ([JSON](../validation/evidence/zellij-version-floor-fedora44.json)); parent routing instrumented and mocked |
| **Kitty** | Yes, opt-in remote control with a `unix:` listen_on socket (`kitten @ ls` readback); needs the captured `KITTY_PID` to own the socket's window and the session | KDE Plasma only (verified on Plasma Wayland; X11 not verified): KWin activates the single normal window owned by the captured `KITTY_PID`, confirmed by kitty's OS window `is_focused` readback; needs one OS window per kitty process and a session started after upgrading | -- | live: kitty 0.47.1 on Plasma Wayland, selection and window raise verified between two probe instances ([JSON](../validation/evidence/kitty-fedora44.json)) |
| **GNU screen** | Yes (`-X select`, `-Q number` readback); needs an attached display, and with several only the most recently attached one switches | No (no GUI window) | -- | live: screen 5.0.1 ([JSON](../validation/evidence/screen-fedora44.json)) |
| **Ghostty (Linux)** | Best effort (ydotool/xdotool Alt+N), only after confirmed activation | Requires exactly one window-manager-managed window (minimized or on another desktop counts) owned by the Ghostty process the session descends from; KWin is asked first but success needs xdotool `getactivewindow` readback, so X11 only | -- | mocked and inspected; unvalidated on this branch (Ghostty not installed on the validation host) |
| **GNOME Terminal** | No | Exact owner window only: the single window-manager-managed window (minimized or on another desktop counts) of the session's gnome-terminal-server, X11 only (xdotool readback) | -- | mocked; not live-validated |
| **Terminator** | No (detection only, [claude-fleet-monitor#47](https://github.com/andre-motta/claude-fleet-monitor/issues/47)) | No | -- | detection live (terminator 2.1.5 [probe](../validation/evidence/terminal-probe-fedora44.json)); mocked; [claude-fleet-monitor#47](https://github.com/andre-motta/claude-fleet-monitor/issues/47) inspection |
| **iTerm2** | Yes (osascript; the session whose `unique ID` (from a well-formed `ITERM_SESSION_ID`) and `tty` both match the agent, readback of the current window's current session) | Yes (the target's own window, un-minimized; readback that iTerm2 is frontmost and its current window holds the session) | -- | mocked and inspected (iTerm2 source); unvalidated on macOS ([claude-fleet-monitor#55](https://github.com/andre-motta/claude-fleet-monitor/issues/55)); as a tmux parent the single session using the client's tty is matched (mocked) |
| **macOS Terminal** | Yes (osascript; tab matched by the agent's controlling tty from `ps`, readback of the front window's selected tab) | Yes (the target's own window, un-minimized; readback that Terminal is frontmost and its front window holds the tab) | -- | mocked and inspected; unvalidated on macOS ([claude-fleet-monitor#55](https://github.com/andre-motta/claude-fleet-monitor/issues/55)); as a tmux parent the client's tty is matched (mocked) |
| **Windows Terminal** | No (detection only) | No (owner window not identifiable) | -- | mocked; unvalidated on Windows |
| **WezTerm** | No (no adapter) | No | -- | not validated (skipped); labelled generic, also when launched from a supported terminal (mocked) |
| **Generic fallback** | No | No | -- | mocked |

"Yes" means the operation is attempted and reported as succeeded only after
independent readback. Version support is the one version validated on the
host, except zellij: the isolated validator passes on 0.44.2 through 0.45.1.
zellij 0.44.1, the first release with `focus-pane-id`, selects panes but did
not pass the two-client check; 0.44.0 and older have no `focus-pane-id`, so
selection reports a failure instead of a guess.

On macOS, captured identifiers are validated (`ITERM_SESSION_ID` must be
`w<N>t<N>p<N>:<UUID>`) and reach `osascript` only as arguments to the
script's run handler, never as AppleScript source. Both adapters match the
agent's controlling tty, read with `ps` at focus time; an agent without one has
no target. When a session ID was captured, iTerm2 also requires it to match,
so an `ITERM_SESSION_ID` inherited by a program launched from an iTerm2 shell
cannot select the wrong session; without one (nested tmux parent routing) the
tty alone must identify exactly one session. Terminal.app's AppleScript exposes no session ID, so its tab is
matched by tty alone. Readback is part of the design, but neither
adapter has been run on a Mac yet; see
[claude-fleet-monitor#55](https://github.com/andre-motta/claude-fleet-monitor/issues/55).

Nested terminals (e.g. tmux inside Konsole) are handled automatically: the
focus command selects the exact pane, then routes window activation to the
terminal emulator hosting a client that is attached to that pane. The
structured result reports selection and activation separately, so a detached
session or an unsupported parent gives an honest partial result. Kitty and
Terminator stop the parent walk and report activation unsupported rather than
climbing to whatever launched them (a multiplexer client carries no kitty
socket or `KITTY_PID`, so kitty cannot raise its window as a parent). A
zellij server is daemonized, so its
ancestry never reaches a GUI. On Linux, tongs-agents instead finds the server in the
agent's ancestry, maps its accepted sockets to client PIDs (a `sock_diag`
netlink dump plus `/proc/<pid>/fd` links of your own `zellij` processes) and
routes activation to the emulator hosting the one attached client. Only a
plain `zellij` client or `zellij attach` counts as interactive and transient
`zellij action` calls are ignored. Any other connected zellij process (`web`,
`watch`, `pipe`, `subscribe`, `run`, `edit`, `plugin` or an unrecognized
command line) is refused, because it may show the session somewhere else. It
also refuses when several clients are attached, when the client processes do
not match `list-clients`, when the session was renamed, or when the client
exited meanwhile.

tmux selection uses stable session, window and pane IDs with selection readback.
Parent-window routing considers only clients attached to the target session;
detached sessions can still select their pane without activating a GUI window.
When several attached clients show the target pane in different emulator
windows, each client's parent is tried in turn. Routing stops after the
first parent that sent a raise request (a KWin activation script that ran, an
`xdotool windowactivate`, or a call to a parent's raise), even when the window
or tab could not be confirmed afterwards. Focus then reports why, for example
`parent terminal konsole may have raised its window but could not confirm it
(...)`, instead of trying another window. A parent that fails before sending
any request (no unique GNOME or Ghostty X11 window, Konsole D-Bus owner
unreadable, qdbus missing) lets the next client be tried. For Konsole, window
matching happens inside the KWin script, so a script that ran but matched no
unique window also stops routing. One rare case can still raise two windows: a KWin script run call that
fails or times out is treated as no request, although KWin may have run it.
See the [isolated tmux validation](../validation/README.md#stable-tmux-selection-and-client-routing-on-linux)
for the tested routing behavior and its GUI evidence limits.

Terminator is detection only ([claude-fleet-monitor#47](https://github.com/andre-motta/claude-fleet-monitor/issues/47)).
Sessions are labelled `terminator` and capture
`TERMINATOR_UUID`, `TERMINATOR_DBUS_NAME` and `TERMINATOR_DBUS_PATH`, but
focus reports an honest unsupported failure: Terminator's D-Bus interface
cannot select a terminal or raise a window.

### Kitty remote control

Kitty selection needs remote control, which tongs-agents never enables for you and
never writes into `kitty.conf`. Add both settings yourself and restart kitty:

```conf
allow_remote_control socket-only
listen_on unix:${XDG_RUNTIME_DIR}/kitty-{kitty_pid}
```

Kitty then exports `KITTY_LISTEN_ON`, which the hook captures with
`KITTY_WINDOW_ID`. Only `unix:` socket addresses (an absolute path or an
`@abstract` name) are used; `tcp:`, `fd:` and relative addresses are dropped
at capture. Without remote control or a `unix:` listen_on socket, focus reports
"kitty remote control unavailable: needs allow_remote_control and a unix:
listen_on socket" and changes nothing.

Selection also needs `KITTY_PID`, which the hook captures:
the kitty process it names must be an ancestor of the agent process and of
the target window's child process that `kitten @ ls` reports. This keeps
kitty variables leaked into another terminal, or a nested kitty that
inherited an outer `KITTY_LISTEN_ON`, from selecting a same-numbered window
in the wrong kitty. Sessions recorded by older hooks have no `KITTY_PID` and report
"restart the agent session to enable kitty focus" without selecting.

**Upgrade note:** kitty sessions recorded by older hooks lose selection
until the agent session is restarted, and kitty versions that do not export
`KITTY_PID` lose selection.

**Security warning:** with `allow_remote_control` enabled, any local process
that can reach the socket can control kitty, including sending text to its
windows and running commands. Prefer `socket-only` over `yes`, keep the socket
in a directory only you can access (as `$XDG_RUNTIME_DIR` above), avoid
abstract `@` sockets on shared hosts, and turn remote control off when you do
not need it. tongs-agents only runs `ls` and `focus-window` and never sends a remote
control password; if kitty requires one, focus reports the refusal instead of
selecting.

Kitty window raise works on KDE Plasma only and needs, besides the selection
requirements above:

- Exactly one OS window in that kitty process. KWin sees every OS window of a
  kitty process (including `--single-instance` windows) under one pid and
  cannot tell them apart, so focus then selects the tab but reports activation
  unavailable.
- qdbus and KWin on the session bus. KWin activates the single normal window
  owned by `KITTY_PID`; success needs the script to confirm KWin accepted
  that window as active and kitty to report the OS window focused within one
  second and still focused 0.2 s later.

### Terminal detection limits

Terminal markers leak into child processes: Kitty started from a Konsole
shell inherits `KONSOLE_*`, anything started from a Terminator shell inherits
`TERMINATOR_UUID`, and a terminal launched from a tmux pane inherits `TMUX`.
On Linux the hook therefore first walks its own process ancestry, reading only
the process name and parent PID from `/proc/<pid>/stat` (never the environment
or command line of another process, at most 64 steps). The nearest Konsole,
Ghostty, GNOME Terminal, Terminator, Kitty, tmux, zellij or GNU screen ancestor
sets the label, and that adapter records whichever of its markers are present.
So Kitty started from Konsole is labelled `kitty`, GNU screen or zellij inside
tmux or Konsole is labelled `screen` or `zellij`, and a Konsole window opened
from a tmux pane is labelled `konsole`. A known emulator without a tongs-agents
adapter also stops the walk and labels the session `generic`, which never
selects or activates: Alacritty, foot, xterm, rxvt-unicode, WezTerm, xfce4-terminal,
MATE Terminal, LXTerminal, QTerminal, Tilix, Ptyxis, GNOME Console (kgx),
Terminology, Sakura, Tilda, Guake, cool-retro-term, Contour and the VS Code,
VSCodium and Cursor terminals. So Alacritty launched from a tmux pane, foot
launched from Kitty or WezTerm launched from Ghostty is labelled `generic`
instead of targeting the launching terminal through its leaked markers.
Yakuake, Kate and Dolphin embed Konsole, so they are not on that list. When
the walk finds no known terminal or cannot read `/proc`, detection falls back
to the fixed environment-variable order. Remaining limits:

- On macOS and Windows detection uses environment variables only, so the
  leaked-marker cases above keep the label of the launching terminal.
- A terminal adapter selected by ancestry can lack its own markers (for
  example Kitty without remote control), so focus reports its usual
  unavailable or unsupported failure rather than using the leaked markers.
- The same list stops the tmux and zellij parent walk: a multiplexer client
  inside one of these emulators reports that its parent terminal emulator has
  no tongs-agents adapter instead of raising the emulator that launched it. On macOS
  and Windows only the parent walk uses the list, and only names that match
  there (for example Alacritty and WezTerm; the VS Code family runs under
  different process names on macOS).
- An emulator missing from both lists is passed over, so the walk and the
  leaked markers can still reach the terminal that launched it.
- Records captured before an upgrade keep their old label and environment;
  start a fresh agent session to pick up new capture keys such as
  `ZELLIJ_PANE_ID`.

## Terminal detection flow

```
Hook fires inside session
  --> Linux: nearest known terminal or multiplexer ancestor (/proc/<pid>/stat only)
  --> otherwise detect terminal via env vars (TMUX, KONSOLE_VERSION, ITERM_SESSION_ID, ...)
  --> capture terminal-specific env (socket paths, DBus service, session IDs)
  --> store in the session JSON: {"terminal": "tmux", "terminal_env": {"TMUX": "..."}}

Focus command reads session JSON
  --> get_terminal_api("tmux") --> TmuxAPI
  --> find_tab(pid) --> switch_tab() --> raise_window()
  --> for nested terminals: detect parent terminal, chain to parent API
```

## Known limitations

- **Same-title tabs in different windows (Konsole/KDE):** Konsole window activation must match exactly one window of the owning Konsole process. When two windows show the same caption, focus selects the tab but reports activation as failed ("no unique Konsole window matched the tab title") instead of raising a guess.
- **Wayland window activation:** On Wayland/KDE, window raising uses KWin scripting via DBus. Other Wayland compositors may not support programmatic window activation.
- **GNOME Terminal:** No tab switching API available; window raise only for the exact owning window on X11. A server with several windows, including minimized ones or ones on other desktops, reports activation unavailable.
- **Ghostty:** Window activation requires a single managed Ghostty window (minimized and other-desktop windows count) and an X11 readback; with several windows, or on Wayland without xdotool readback, activation fails with a detail and no Alt+N keys are sent. This path is mocked only.
- **Windows Terminal:** Detection only. Its window titles follow the active tab and all windows usually share one process, so the session window cannot be identified without Win32 enumeration validated on Windows.
- **Terminator:** Detection only. Its D-Bus interface has no select or raise method, so focus reports an unsupported failure until Terminator gains one ([claude-fleet-monitor#47](https://github.com/andre-motta/claude-fleet-monitor/issues/47)).
- **Kitty windows:** Window raise is KDE Plasma (KWin) only, verified on Plasma Wayland (Plasma X11 and XWayland kitty are not verified), and needs a single OS window per kitty process; other desktops, several OS windows (including `--single-instance`), and sessions recorded before `KITTY_PID` was captured report activation unavailable with the reason.
- **GNU screen windows:** Selection is read back, but no GUI window activation is claimed. A screen session owns no window.
- **zellij windows:** Window activation is Linux only and needs exactly one attached interactive client; with several clients, or a `zellij watch`, `zellij web` or other non-interactive zellij process connected, tongs-agents cannot tell which window shows the pane and reports activation unavailable. Elsewhere zellij activation is unavailable. Real GUI activation through the client's emulator is not live-validated yet: the live validator routes to an instrumented parent only.
- **GNU screen and zellij with several displays or clients:** GNU screen switches only the most recently attached display, and zellij moves only the client that most recently received input; other displays and clients keep their own window or pane. Both need an attached display or client: a detached session reports a failed selection. One exception was observed by the workspace validator on zellij 0.44.2 and 0.45.1: with zellij's default `stacked_resize`, another client that sits on a pane in the same stack as the target follows the expanded pane.
- **zellij socket directory:** Every zellij call targets the session's socket directory: on Linux it is derived from the zellij server's command line, otherwise the `ZELLIJ_SOCKET_DIR` captured at hook time is used, and without either zellij's default applies. A session that cannot be found reports which socket directory was searched.
- **zellij selection proof:** Selection succeeds only when a client that was elsewhere moves to the pane, or when every attached client already showed it ("pane already focused"). An idle client that already showed the pane while the active client did not move is a failure.
- **Generic fallback:** Unknown terminals get no selection or window raise; the earlier best-effort raise could activate an unrelated window and was removed.
