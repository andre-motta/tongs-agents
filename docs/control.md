---
title: "Driving agent panes"
description: "Paste prompts into agent panes, read their screens and wait for them with send, read, wait and agent-status."
---

`tongs-agents send`, `read`, `wait` and `agent-status` let a person or a
script drive agents, but only in panes that tongs-agents spawned and can verify
right now. A pane is named by its launch id: the 32-character hex id of its
launch record, shown by `tongs-agents agent-status` and set as
`TONGS_AGENTS_LAUNCH_ID` inside the pane.

Control works with Zellij and tmux panes started by `tongs-agents workspace`
or the sidebar (on tmux see [tmux](workspace.md#tmux) for how pastes and
reads are done). A launch whose multiplexer has no pane control reports
"unsupported" (exit 6).

## Commands

```console
tongs-agents send LAUNCH_ID "Summarize the failing test"
printf 'line one\nline two\n' | tongs-agents send LAUNCH_ID -
tongs-agents send --no-submit LAUNCH_ID "draft, not submitted"
tongs-agents read LAUNCH_ID --lines 40
tongs-agents wait LAUNCH_ID --until done --timeout 300
tongs-agents agent-status
tongs-agents agent-status LAUNCH_ID --json
```

- `send` pastes the text into the pane as one bracketed paste, waits
  `send_delay_ms`, then presses Enter as a separate key, so multi-line text is
  one prompt. With `-` the text is read from standard input (CRLF becomes LF
  and one trailing newline is dropped). Text is at most 64 KiB and may hold
  tabs and newlines but no other control characters (no escape sequences).
- `read` prints the last 1 to 200 lines (default 200) of the pane's screen and
  scrollback.
- `wait` polls until the agent reaches `--until`: `done` (default),
  `needs-you`, `ended`, or `any` (done, needs-you, failed or ended). It waits
  at most 600 s (`--timeout`, default 600). After a `send`, status updates
  written before that send do not count, so the previous turn's `done` never
  ends a wait for the next one. Panes without hooks only reach `ended`.
  Waiting for `done` ends early with 3 when the agent needs you, 1 when the
  turn failed and 4 when it exited.
- `agent-status` prints one launch's state, or every claimed launch with
  refusals shown as rows. States are `working`, `needs-you`, `done`, `failed`,
  `quiet` (no status reported) and `ended` (the pane's program exited).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | ok (for `wait`: the state was reached) |
| 1 | error (usage errors and bad arguments, text refused, multiplexer call failed) |
| 2 | timeout (`wait`) |
| 3 | blocked: the agent needs you (`wait --until done`) |
| 4 | the agent exited |
| 5 | refused: not a pane tongs-agents spawned and verified |
| 6 | the launch's multiplexer has no pane control |

A usage error, such as an unknown option, a missing launch id or an invalid
`--until` value, prints the usage line and exits 1, never 2, so 2 always
means a `wait` timeout. Every `tongs-agents` verb follows this rule: no verb
uses argparse's usual exit 2 for usage errors.

## What is verified

Every call checks, in this order, and refuses (exit 5) when any check fails:

1. The launch record exists, was claimed by its pane and was not replaced by a
   later claim.
2. Its pane is listed in the same Zellij or tmux session, served from the
   same socket (dir).
3. The pane's program has not exited (otherwise exit 4).
4. The claimed process is still alive with the start time recorded at claim
   time. A launch from a session that was killed and recreated with the same
   name fails here even when its pane id was reused by another program.
5. No hook session record carrying the launch id names another pane.

Step 3 runs before step 4 because an exited program has no process left
to compare. That leaves one ambiguity: when a session was killed and recreated
with the same name, and the pane that reuses the stale launch's pane id has
already exited, the call reports that pane's exit (exit 4, and `ended` in
`agent-status`) instead of refusing the stale launch (exit 5). Nothing is sent
or read in either case. Zellij reports no pane start time to compare with the
claim, so the two cases cannot be told apart.

A pane started any other way (for example `zellij action new-pane`) has no
launch record and is always refused.

## Concurrency and timing

Sends to one pane take a lock file in `$XDG_RUNTIME_DIR/tongs-agents/locks/`
(or the state dir's `run/locks/`), named by the SHA-256 of the session and
pane id, so concurrent senders, including separate processes, arrive whole and
in turn. The delay between paste and Enter comes from the config file:

```toml
[control]
send_delay_ms = 150   # 0 to 5000
```

## Audit log

Every call, including refusals, appends one JSON line to
`$XDG_STATE_HOME/tongs-agents/audit/control.jsonl` (0600, rotated to
`control.jsonl.1` at 1 MiB) with the time, actor (`cli`), the caller's own
launch id when it runs in a launched pane, the target launch id, the action,
the byte count and SHA-256 of the text sent or read, the outcome and the exit
code. Sent and read text is never written to the log.

## Validation

`validation/send.py` checks the commands against a real, isolated, headless
Zellij session with a fake REPL fixture that turns on bracketed paste and
counts submitted prompts: one prompt per multi-line send, four concurrent
sends serialized and intact, `read` returning the tail, refusals (exit 5) for
panes tongs-agents did not spawn and for a stale launch after the session is
recreated, and an audit log without text. The hosted `send-validation` job
runs it on each pinned Zellij release; results are in
[the evidence](../validation/evidence/send-linux-x86_64.json).

```console
PATH=<dir with a pinned zellij>:$PATH python validation/send.py --no-write
```

Without `--no-write` the run replaces its Zellij version's entry in the
evidence file.
