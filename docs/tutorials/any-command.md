# Tutorial: any command as a pane

Claude Code, Codex and Pi report their own status through hooks. Anything
else (a test watcher, a build, a deploy script, an agent without hooks) can
run in the workspace too, as a profile with harness `other`, and report its
status with `tongs-agents notify`.

This tutorial needs only a shell, so it costs nothing to run. It assumes you
finished [your first workspace](first-workspace.md) up to step 2 (tongs-agents
installed and a Zellij available).

## 1. A pane that only shows its state

Add a profile to `~/.config/tongs-agents/config.toml`:

```toml
[profiles.scratch]
command = ["bash", "--norc"]
```

`command[0]` is not `claude`, `codex` or `pi`, so the harness is `other`.
Start it:

```bash
tongs-agents workspace --session tryout --profile scratch
```

The sidebar row for `scratch` shows the pane state only: `STARTING` until the
launcher has claimed the pane, `RUNNING` while `bash` runs, and `EXITED 0`
after you type `exit` (Zellij keeps the exited pane open so you can read it).

## 2. Report status from inside the pane

Every pane tongs-agents starts has `TONGS_AGENTS_LAUNCH_ID` set. In the
`scratch` pane, run:

```bash
tongs-agents notify working "compiling"
tongs-agents notify needs-you "approve the rollout"
tongs-agents notify done "build passed"
```

Watch the sidebar after each one. The row turns into a status row:

| You run | The row shows | Counts as |
| --- | --- | --- |
| `notify working` | WORKING | running |
| `notify needs-you` | NEEDS-YOU | waiting: `n` jumps to it, and a desktop notification is sent at once |
| `notify done` | DONE | idle: needs attention after two minutes |
| `notify failed` | FAILED | error |
| `notify quiet` | QUIET | nothing to report |

The message becomes the row's detail. Outside a launched pane, `notify`
refuses with exit 1:

```console
$ env -u TONGS_AGENTS_LAUNCH_ID tongs-agents notify done
tongs-agents notify: not in a pane started by tongs-agents (no valid TONGS_AGENTS_LAUNCH_ID)
```

## 3. A test runner that reports its own result

Put the reporting into the profile, so the pane tells you how the tests went
without you looking at it:

```toml
[profiles.tests]
command = ["sh", "-c", "tongs-agents notify working 'running tests'; if make test; then tongs-agents notify done 'tests passed'; else tongs-agents notify failed 'tests failed'; fi; exec sh"]
cwd = "~/src/example"
```

Replace `make test` with your own test command and `~/src/example` with a
project. Use `if`/`else` as shown rather than `make test && notify done ||
notify failed`, so a refused `notify done` is never reported as a failed run.
The final `exec sh` keeps the pane open, and the row with it.

Start it next to your agents from the sidebar: Ctrl+P, then
`New agent: tests`.

## 4. A wrapper around a harness

When you wrap a harness in your own script, set `harness` so tongs-agents
installs and reads the right hooks, and make the script `exec` the harness:

```toml
[profiles.claude-wrapped]
command = ["/opt/example/bin/claude-wrapper"]
harness = "claude"
```

```bash
#!/bin/sh
# /opt/example/bin/claude-wrapper
export EXAMPLE_PROJECT=1
exec claude "$@"
```

`exec` keeps the launched process id, so the session is found and focused.
Do not use `notify` in such a pane: it refuses in a pane whose profile names a
hooked harness (`claude`, `codex` or `pi`), because the hooks already report
its status.

`tongs-agents doctor` warns about every `other` profile that runs
unmonitored, and about wrappers whose name hides the harness.

## What you learned

- Any argv command can be a profile; it is never run through a shell unless
  you ask for one with `sh -c`.
- `tongs-agents notify` turns any launched pane into a status row with the
  same attention and notifications as an agent.
- Wrappers set `harness` and `exec` the real harness.

Reference: [reporting state from any command](../profiles.md#reporting-state-from-any-command).

Next: [one dotfiles directory for every harness](dotfiles.md).
