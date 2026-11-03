# Workspace live run (M1 exit gate)

Tracking: [issue #16](https://github.com/andre-motta/tongs-agents/issues/16),
milestone M1 of the [agent workspace plan](agent-workspace-plan.md).

The M1 gate needs two kinds of evidence:

| Evidence | Label | Status |
| --- | --- | --- |
| `validation/workspace.py` on Zellij 0.44.2 and 0.45.1 with fake harness fixtures, locally and in the hosted `workspace-validation` job | instrumented | Recorded in [workspace-linux-x86_64.json](../../validation/evidence/workspace-linux-x86_64.json); see the [validation guide](../../validation/README.md#agent-workspace-on-linux) |
| The maintainer's hands-on run with real profiles | live | **Not recorded yet.** See the placeholder below |

## Live run record

> **PLACEHOLDER: no live run has been recorded.** This section is filled in
> by the maintainer after running the runbook below with real agents. Nothing
> here may be written by an agent or copied from the instrumented validator.
> Until it is filled in, the support matrix labels the workspace
> instrumented, not live.

| Field | Value |
| --- | --- |
| Date | _pending_ |
| Run by | _pending (maintainer)_ |
| tongs-agents commit | _pending_ |
| Zellij version | _pending_ |
| Host | _pending (OS, desktop, session type)_ |
| Profiles run | _pending_ |
| Result per check (1 to 8 below) | _pending_ |
| Deviations and follow-up issues | _pending_ |

## Runbook

The run uses real Claude Code, Codex and pi sessions, so it costs real model
usage and must be run by the maintainer. Agents never run it. Commands below
use synthetic profile names and `<placeholders>`; replace them with your own
and redact personal paths before recording anything.

### 1. Prepare

1. Check out the commit under test and install it in a permanent virtual
   environment (not one under `/tmp`; `tongs-agents doctor` warns about that).
2. Confirm `zellij --version` prints 0.44.2 or 0.45.1 and that
   `tongs-agents doctor` reports `workspace-zellij` as ok.
3. Keep a copy of your current agent configuration (for example the Claude
   `settings.json` of each config dir and `~/.codex/hooks.json`) so you can
   compare it afterwards.

### 2. Profiles

Add four profiles to `~/.config/tongs-agents/config.toml` (two Claude
commands with separate config dirs, Codex with `--profile`, pi with
`--provider` and `--model`):

```toml
[profiles.claude-main]
command = ["claude"]
config_dir = "<first Claude config dir>"

[profiles.claude-alt]
command = ["claude"]
config_dir = "<second Claude config dir>"

[profiles.codex-review]
command = ["codex", "--profile", "<codex profile>"]

[profiles.pi-local]
command = ["pi", "--provider", "<provider>", "--model", "<model>"]
```

`tongs-agents profiles` must list all four with no error.

### 3. Install

```console
tongs-agents install --claude-config-dir <first Claude config dir> \
  --claude-config-dir <second Claude config dir>
tongs-agents install --agent codex
tongs-agents install --agent pi
tongs-agents doctor
```

`doctor` must report hooks installed for both Claude config dirs, Codex and pi.
Diff the files saved in step 1.3: only tongs-agents hook groups and MCP
entries may have changed.

### 4. Run and check

Outside Zellij, with the profiles above:

```console
tongs-agents workspace --session agents-live --no-tongs \
  --profile claude-main --profile claude-alt
```

Record the result of each check:

1. The session opens within 5 seconds with the sidebar and one pane per
   profile (`zellij --session agents-live action list-panes --json`).
2. After the first prompt in each pane, the sidebar shows hook status (not
   `discovered`) within 4 seconds, named after the profile. The two Claude
   rows must not share a status: prompt only one of them first.
3. From a shell pane inside the session, run
   `tongs-agents workspace --profile codex-review --profile pi-local`. It adds
   two panes to the same session, exits 0 and opens no nested Zellij.
4. Codex and pi rows reach hook status within 4 seconds of their first
   prompt; the pi row shows the provider's model, and the Codex session uses
   the selected Codex profile.
5. Attach a second client from another terminal window
   (`zellij attach agents-live`), type in it, then select a row in the
   sidebar of the first window and press Enter. The second client moves to
   that pane (`list-clients` shows it) and no terminal window is raised or
   activated.
6. Close one agent (`/exit` or Ctrl+D) and check that its row reports
   ended or the pane's exit status.
7. `zellij kill-session agents-live` leaves no agent process running.
8. The configuration diff from step 3 is unchanged after the run.

### 5. Record

Fill in the placeholder table above with the date, commit, Zellij version,
host, the profile names (not their config paths) and each check's result.
Redact home directories, user names, host names, config dir paths, session
IDs and anything a prompt or reply contained. Label the record **live**, then
update the workspace rows of the [support matrix](../support-matrix.md) and
the README to cite it.
