---
title: "Pi lifecycle integration"
description: "Report Pi agent status to tongs-agents through its packaged extension."
---

Pi support is optional. Install it after installing both Pi and
tongs-agents:

```bash
tongs-agents install --agent pi
```

The installer requires Pi 0.84.4 or newer. It checks the version with a temporary
offline Pi configuration, then asks Pi to install the JavaScript extension that
is packaged with Tongs Agents. The extension has no npm dependencies.
It does not add an MCP server to Pi.

The default `tongs-agents install` command configures Claude Code, Codex and
the config home of every profile, including `pi` profiles. When `pi` or
`tongs-agents-hook` is not installed, a Pi home that only a profile asked for is
skipped with a warning naming that profile, and install still succeeds. Use
`--agent all` to configure all three agents, or select one of `claude`, `codex`
and `pi`; those requests fail when Pi cannot be installed.

Restart Pi sessions that were already running when you installed the extension
so they load the monitoring bridge.

## Data and privacy

The extension reports lifecycle state through the installed
`tongs-agents-hook` executable. It sends the Pi process ID, native session ID,
working directory, a random extension-runtime instance ID, a monotonic sequence
and minimal status metadata. Tool events include the tool name only.

The extension never sends prompts, assistant messages, tool arguments, tool
results, UI prompt titles, provider responses or raw error messages. Provider
failures are reported as `turn failed`, and aborted turns as `turn aborted`.

Events are serialized through a queue limited to 128 waiting entries. An event
send is stopped after 1.5 seconds. Missing or failing tongs-agents hooks do not stop Pi.
On shutdown, the bridge spends at most 1.5 seconds draining intermediate events,
drops any remaining intermediate queue, and reserves up to 1.5 seconds for the
final ended event. An abruptly killed Pi process is removed by tongs-agents' PID and
process-start validation on the next store maintenance: the sidebar or monitor
while one runs, `tongs-agents cleanup` or the `fleet_cleanup` MCP tool.

## Lifecycle behavior

| Pi event | Session status |
|----------|-------------|
| `session_start` | `started` |
| `before_agent_start` | `running` |
| `tool_execution_start` | `running`, with tool name |
| `agent_settled` | `idle`, `error`, or aborted `idle` |
| `ui_prompt_start` | `waiting` |
| `ui_prompt_end` | Restores the prior state |
| `session_shutdown` | `ended` |

Pi can emit duplicate `session_start` events for one newly created session. The
extension ignores repeated starts while that session is active. New, resumed
and reloaded sessions retain one random instance ID for the Pi process, while
each native session has its own increasing event sequence. Events capture their
session identity before entering the queue, so delayed events cannot be applied
to a later session.

tongs-agents records the terminal environment inherited by Pi. For example, a Pi
process inside tmux can be associated with its exact pane. Desktop window
activation still depends on the captured parent terminal and available display
integration.

## Upgrade and removal

Run the Pi install command again after moving or recreating the Python virtual
environment that contains Tongs Agents:

```bash
tongs-agents install --agent pi
```

Tongs Agents stores its exact hook and extension paths in
`$PI_CODING_AGENT_DIR/tongs-agents.json`, or `~/.pi/agent/tongs-agents.json`
by default. The file carries the ownership marker `tongs-agents`, and the
loaded extension keeps its state under the process-wide symbol
`tongs-agents.pi.runtime.v1`. The Pi package is named `tongs-agents-pi`. A
repeated install updates those paths, installs the
new package path first, and removes old owned references. A failed old-reference
removal remains recorded so a later install or uninstall can retry it.

If that config file exists without the `tongs-agents` ownership marker, installation and
removal stop without replacing it. Uninstall passes only paths recorded in the
owned file to Pi, preserving every unrelated package and extension:

```bash
tongs-agents uninstall --agent pi
```

Install and uninstall also remove the extension a claude-fleet-monitor 0.9.x
install registered, using the paths recorded in its own config file (marked
`claude-fleet-monitor`), and then delete that file. A failed removal keeps it
for a later retry; a file without that marker is left alone. Details are in
the [migration guide](migration.md).

Restart or exit Pi sessions that were already running when you uninstalled the
extension. A running process keeps its loaded monitoring bridge until then.

Removing only Pi does not delete the shared sessions directory. The default
uninstall remains the Claude Code and Codex uninstall. Use `--agent all` to
remove all three integrations and shared status data, or add `--keep-data` to
retain the data.

## Validation

The reproducible validation under `validation/pi/` uses a temporary home, Pi
configuration and state directory. It runs a local synthetic provider and does
not send traffic to an external model provider. See
[`validation/pi/README.md`](../validation/pi/README.md) for the exact command and
coverage.
