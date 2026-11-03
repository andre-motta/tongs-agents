# Agent profiles

A profile names a command that tongs-agents can start in a multiplexer pane.
Any argv command can be a profile: a harness on `PATH`, a harness with its own
config directory, or a wrapper script that execs the harness.

Profiles live in the `[profiles]` section of the tongs-agents config file:

| Location | When |
| --- | --- |
| `$TONGS_AGENTS_CONFIG_DIR/config.toml` | the variable is set |
| `$XDG_CONFIG_HOME/tongs-agents/config.toml` | `XDG_CONFIG_HOME` is set and absolute |
| `~/.config/tongs-agents/config.toml` | otherwise |

The profile loader reads only `[profiles]`. Other sections (`[workspace]`,
`[ui]`, `[control]`) have their own loaders, and tongs-agents never rewrites
the file. Python 3.11 or newer reads it with the standard library `tomllib`.

## Example

All names and paths below are synthetic.

```toml
# A Claude Code profile with its own config directory. argv[0] stays the
# literal `claude` so the session is discoverable; config_dir sets
# CLAUDE_CONFIG_DIR for it.
[profiles.claude-alt]
command = ["claude"]
config_dir = "~/.claude-alt"
model_args = ["--model", "opus"]

# A Codex profile layered on $CODEX_HOME/review.config.toml. The launcher
# appends --no-daemon unless it is already present, so hooks fire in-process.
[profiles.codex-review]
command = ["codex", "--profile", "review"]

# A separate Codex home.
[profiles.codex-alt]
command = ["codex"]
config_dir = "~/.codex-alt"

# Pi with a local model, started in a project directory.
[profiles.pi-local]
command = ["pi", "--provider", "ollama", "--model", "qwen3-coder"]
cwd = "~/src/example"

# A wrapper that sets up its own environment. It must `exec claude "$@"`, so
# the launched pid stays the agent's.
[profiles.claude-sandbox]
command = ["/opt/example/bin/claude-sandbox", "--project", "example"]
harness = "claude"
env = { EXAMPLE_SANDBOX = "1" }

# Anything else.
[profiles.example-agent]
command = ["example-agent", "--interactive"]
```

## Keys

| Key | Type | Meaning |
| --- | --- | --- |
| `command` | list of strings, required | The argv. `command[0]` is looked up on `PATH` and must not be empty. It is never shell-parsed: `"a b"` is one argument and `$(...)` is literal text. |
| `harness` | `claude`, `codex`, `pi` or `other` | Inferred from the basename of `command[0]` when it is exactly `claude`, `codex` or `pi`; otherwise `other`. Set it for wrappers. |
| `env` | table of strings | Extra environment. Names match `^[A-Z_][A-Z0-9_]*$`, values are under 4 KiB with no NUL, and `TONGS_AGENTS_*` names are reserved. Values are passed verbatim, with no `~` or `$VAR` expansion. |
| `config_dir` | path | Sets `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `PI_CODING_AGENT_DIR` for the harness. Needs harness `claude`, `codex` or `pi` and must not also appear in `env`. |
| `cwd` | path | Working directory for the agent. |
| `model_args`, `effort_args` | list of strings | Appended after `command`, in that order. |
| `mcp_control` | boolean, default `false` | Allows the profile to be driven through the MCP control tools once they exist. |

Profile names match `^[a-z0-9][a-z0-9_-]{0,31}$`. Paths must be absolute or
start with `~/`. Unknown keys are errors, so typos are caught.

The file is refused when it is a symlink, not a regular file, owned by another
user, writable by group or others, or larger than 64 KiB. Errors name the file
and the key, for example
`config.toml: profiles.claude-alt.env.lower: name must match ^[A-Z_][A-Z0-9_]*$`.

## Identity and wrappers

Claude Code is recognised only when the process is literally named `claude`
(its executable is named after its version). Prefer env-only profiles:
`command = ["claude"]` with `config_dir`, rather than a symlink called
`claude-alt`, whose sessions cannot be discovered. A wrapper script must
`exec` the harness; a wrapper that runs it as a child keeps hooks working but
the launch record then names the wrapper shell, not the agent.

## How a profile is launched

Every agent pane runs

```
<python> -m tongs_agents.launch <profile> --launch-id <32 hex>
```

where the spawner has already written a pending launch record. The launcher:

1. loads and validates the profile and the launch id;
2. claims the record once, writing its own pid, process start token and pane
   identity (`ZELLIJ_SESSION_NAME`, `ZELLIJ_PANE_ID`, `ZELLIJ_SOCKET_DIR`, or
   `TMUX_PANE`). Claiming an id that was already claimed, for example from a
   resurrected pane, writes a fresh record whose `replaces` names the old id;
3. builds the environment: inherited variables, the profile `env`, the
   harness config-dir variable, then `TONGS_AGENTS_LAUNCH_ID` and
   `TONGS_AGENTS_PROFILE`; `TMUX` and `TMUX_PANE` are removed when `ZELLIJ`
   is set;
4. changes to `cwd` and calls `os.execvpe`, so the agent keeps the launcher's
   pid.

It exits 64 for a bad profile or launch id and 127 when the command cannot be
executed, printing one line and, on a terminal, waiting for Enter so the pane
stays readable.

Launch records are stored in `state_dir()/launches/<launch_id>.json` (0600,
in a 0700 directory), written atomically and read without following symlinks.
`launches.prune()` removes records older than seven days whose process has exited.
