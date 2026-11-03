---
title: "Tutorial: sandboxed agents with OpenShell"
description: "Run Claude Code in an NVIDIA OpenShell sandbox as a workspace pane, manage the sandbox and decide its access requests."
sidebar:
  order: 6
---

In this tutorial you prepare one Claude Code profile that runs inside an
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox, open it in
the workspace like any other agent, manage the sandbox from the command
line, and approve or reject a network access request from the sidebar.

**Unvalidated.** Every OpenShell call tongs-agents makes is checked against a
fake OpenShell CLI in the test suite. The live run on a real rootless Podman
gateway is still pending
([#109](https://github.com/andre-motta/tongs-agents/issues/109),
[#116](https://github.com/andre-motta/tongs-agents/issues/116)), so expect
differences from a real gateway and report them. The
[reference page](../sandbox.md) says which parts come from upstream source
only.

This tutorial assumes [your first workspace](first-workspace.md): tongs-agents
is installed, `tongs-agents install` has run and the workspace opens.

## Before you start

tongs-agents never installs OpenShell, never starts, stops or configures a
gateway, and never stores a credential. You bring:

- an OpenShell gateway that you run yourself, for example a rootless Podman
  gateway, and that `openshell status` reaches;
- the `openshell` CLI 0.1.6 or newer on `PATH` (older 0.1.x versions run with
  a warning, versions below 0.1.0 are refused);
- an OpenSSH client (`ssh`), which OpenShell uses to upload files;
- an image with POSIX `sh`, coreutils, `tar`, `bash` and the `claude` binary;
- an Anthropic API key in your shell environment. A Claude subscription
  login inside a sandbox is not supported; the key reaches the sandbox as an
  OpenShell provider, never through tongs-agents.

The examples use the image `registry.example.com/org/claude-agent:1` and the
project folder `~/src/example`. Replace both with your own.

## 1. Check the host

```bash
openshell --version
openshell status
tongs-agents doctor
```

Once `openshell` is on `PATH`, `doctor` adds lines for the CLI version, the
gateway, OpenSSH, Landlock, the optional `openshell-prover` and OpenShell's
telemetry. Fix any error it reports before you go on.

## 2. Run the setup wizard

```bash
cd ~/src/example
tongs-agents sandbox setup --harness claude --image registry.example.com/org/claude-agent:1 --network anthropic --network github
```

On a terminal this opens a wizard; add `--print` to get the same steps as
plain text. The wizard checks the host again, asks for the folder, harness,
profile name (here `claude-boxed`) and image, then the network presets. Each
preset allows a few hosts to named binaries, and everything else stays
denied. `anthropic` lets `claude` reach `api.anthropic.com`; `github` allows
read-only GitHub API calls and `git` clone and fetch, but no push.

The **Review** step shows the generated policy in full. Read it. From there
you can save it (`Save policy`) and, optionally, probe the image: the probe
starts one throwaway sandbox with exactly that policy, checks that `sh`,
`bash`, `tar`, the other tools and `claude` exist, and deletes it again.

The wizard only reads and prints. It never edits your config file, never
creates a provider and never touches the gateway. Its last step prints what
is left for you, which is the next three steps.

## 3. Create the providers

The wizard prints one `openshell provider create` command for each provider
your gateway does not have yet. For these presets:

```bash
openshell provider create --name claude-code --type claude-code --from-existing
openshell provider create --name github --type github --from-existing
```

`--from-existing` makes the OpenShell CLI read the key from your environment.
These commands were checked against upstream's CLI definition, not run
against a live gateway.

## 4. Add the profile

Save the policy if you did not save it in the wizard:

```bash
tongs-agents sandbox setup --print --harness claude --image registry.example.com/org/claude-agent:1 --network anthropic --network github --write-policy
```

It writes `~/.config/tongs-agents/policies/claude-boxed.yaml` with mode 0600
and never overwrites an existing file. Then add the printed profile to
`~/.config/tongs-agents/config.toml`. The wizard prints absolute paths; it
looks like this:

```toml
[profiles.claude-boxed]
command = ["claude"]
cwd = "~/src/example"

[profiles.claude-boxed.sandbox]
image = "registry.example.com/org/claude-agent:1"
providers = ["claude-code", "github"]
policy = "~/.config/tongs-agents/policies/claude-boxed.yaml"
```

A sandboxed profile refuses `env`, `config_dir` and `mcp_control`: secrets
go through providers, the harness config is the one tongs-agents uploads,
and MCP control is not available inside a sandbox. Check that it loads:

```bash
tongs-agents profiles
```

## 5. Launch it in the workspace

```bash
tongs-agents workspace --profile claude-boxed
```

The pane shows what the launcher does. It creates the sandbox
`tongs-claude-boxed` (the default name is `tongs-` plus the profile name),
labelled as a tongs-agents sandbox, with your providers and policy. It
uploads the contents of `~/src/example` to `/sandbox/workspace` and a hook
configuration to `/sandbox/workspace/.tongs-agents`, waits up to 180 seconds
for the sandbox to be Ready, and starts `claude` inside it. A failure stops
in the pane with the phase and the command to inspect or delete the sandbox.

The sidebar lists the agent like any other, with its sandbox name in a
`Sandbox` column. Its status comes from hook events that the sidebar copies
out of the sandbox on each poll. Typing in the pane and `tongs-agents send`
reach the agent as usual.

When the pane ends, the sandbox stays. Launching the profile again reuses it
with its files, uploads only a fresh hook configuration, and restarts the
agent in place.

## 6. Manage the sandbox

`tongs-agents sandbox` acts only on sandboxes carrying a tongs-agents label.
A sandbox you created yourself is refused by name and never listed.

```bash
tongs-agents sandbox list
tongs-agents sandbox status tongs-claude-boxed
tongs-agents sandbox logs tongs-claude-boxed -n 50 --since 10m
tongs-agents sandbox pull tongs-claude-boxed ./from-sandbox --path src
```

`list` shows each sandbox, its phase and whether an agent pane runs in it.
`logs` prints recent lines and never follows. `pull` copies from the
sandbox's working directory into a new or empty local directory, so it never
overwrites host files.

When you are done with the sandbox and its files:

```bash
tongs-agents sandbox delete tongs-claude-boxed
tongs-agents sandbox prune
```

`delete` asks first (off a terminal it needs `--yes`) and refuses while an
agent pane still runs in the sandbox unless you add `--force`. Deleting
discards every file in the sandbox, so `pull` what you want to keep first.
`prune` deletes the labelled sandboxes whose profile is no longer in your
config file, never one an agent pane runs in; off a terminal and without
`--yes` it only lists them.

## 7. Read and check the policy

```bash
tongs-agents sandbox policy show tongs-claude-boxed
tongs-agents sandbox policy check tongs-claude-boxed
```

`policy show` prints the active policy. `policy check` diffs the profile's
policy file (or `--policy FILE`) against the active one and, when
`openshell-prover` is on `PATH`, runs it on the candidate. Without the prover
the diff is shown unproved. Neither command applies a policy; edit the file
and relaunch, or use OpenShell yourself.

## 8. Turn on the policy advisor

With the advisor on, the agent can propose a network rule when it needs a
host the policy denies:

```bash
tongs-agents sandbox advisor on tongs-claude-boxed
```

It changes only this sandbox, never a global setting. To turn it on at every
launch, add `policy_advisor = true` to the profile's `sandbox` table.
`tongs-agents sandbox advisor off tongs-claude-boxed` turns it off again.

## 9. Approve or reject an access request

OpenShell drafts a rule for every connection the policy blocks and for every
proposal the agent submits. Approving one widens what the agent can reach,
so it is always your decision, one rule at a time. tongs-agents never sets an
approval mode and never approves everything at once.

In the sidebar, a row whose sandbox has pending requests reads as waiting.
It counts as needing you, `n` jumps to it, and a notice names the sandbox and
the endpoint. Then:

1. Press `a` (or pick **Review access requests** in the palette) to see the
   pending requests with their full text: rule, binary, rationale,
   `Security:` and `Prover:` notes, endpoints and hits.
2. To approve, press `a` on the request. **Approve this network access?**
   shows the full text again, security notes first. Only the **Yes,
   approve** button approves: click it, or Tab to it and press Enter. No
   single key approves, so a stray key cannot approve text you have not
   read. `n` or Escape cancels.
3. To reject, press `d`, type a reason (it is required) and press Enter.
   Escape cancels.

Before it acts, tongs-agents reads the pending list again and refuses a rule
that is no longer pending or whose text changed since you saw it. The rule
text is written by the agent, so read it as data: a rationale is a claim,
not a reason to approve.

From a terminal the same decisions are:

```bash
tongs-agents sandbox rules list tongs-claude-boxed
tongs-agents sandbox rules approve tongs-claude-boxed CHUNK_ID
tongs-agents sandbox rules reject tongs-claude-boxed CHUNK_ID --reason "not needed for this task"
```

`approve` shows the proposal and asks; it refuses to run off a terminal and
has no `--yes`. The format of OpenShell's pending rule list comes from
upstream source, not from a live gateway. Output tongs-agents cannot parse
still marks the row as needing you, but is shown raw and cannot be approved
from tongs-agents; decide it with `openshell rule approve` or `reject`
yourself.

## What you learned

- `tongs-agents sandbox setup` checks the host, shows a policy to review and
  prints the provider commands and profile snippet; you run and add them.
- A profile with a `sandbox` table opens in the workspace like any agent,
  and relaunching it reuses its sandbox.
- `sandbox list`, `status`, `logs`, `pull`, `delete` and `prune` manage only
  the sandboxes tongs-agents labelled.
- `policy show` and `policy check` read policies; `advisor` lets the agent
  propose rules.
- Only the **Yes, approve** button in the sidebar, or
  `sandbox rules approve` at a terminal, approves an access request.

Reference: [sandboxed profiles](../sandbox.md).
