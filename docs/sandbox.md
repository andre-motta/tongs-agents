---
title: "Sandboxed profiles (OpenShell)"
description: "Reference for profiles that run their agent in an NVIDIA OpenShell sandbox: keys, launch, sandbox commands, access requests and the setup wizard."
---

A profile with a `sandbox` table runs its agent inside an
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox, still in its
own Zellij pane, with its status in the sidebar like any other agent. This
page is the reference for the profile keys, the launcher, the `sandbox`
commands, access requests and the setup wizard. For a step-by-step
introduction, follow the [sandboxed agents tutorial](tutorials/sandbox.md).
The design and its approval record are in
[openshell-sandbox.md](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/openshell-sandbox.md) (#34).

Status: **instrumented only.** Every OpenShell call has been exercised against
the fake CLI in `tests/fixtures/fake_openshell/`, which reproduces the argument
shapes and `sandbox get -o json` keys of the OpenShell CLI at upstream
`ba302a6` (equal to v0.1.6 and midstream v0.1.6-rhaiv.4). The live check on a
rootless Podman gateway with upstream v0.1.6 and a midstream build is pending
the maintainer's run (row 21, see [Live validation](#live-validation);
[#109](https://github.com/andre-motta/tongs-agents/issues/109),
[#116](https://github.com/andre-motta/tongs-agents/issues/116)).

## Before you start

`tongs-agents sandbox setup` walks through everything below and prints what
is left to do; see [Setup wizard](#setup-wizard).

tongs-agents never installs OpenShell, starts, stops or configures a gateway,
or changes system services. You need:

- the `openshell` CLI on `PATH`, version 0.1.0 or newer (0.1.6 is the
  supported floor and older versions get a warning; a newer minor than 0.1.x
  is accepted with a warning);
- a gateway the CLI already reaches (`openshell status`);
- OpenSSH on the host, which `openshell` uses for uploads;
- an image with POSIX `sh`, coreutils, `tar`, `bash` (for `sandbox exec`'s
  login shell) and the harness binary;
- credentials as OpenShell providers. tongs-agents never passes a secret
  through `--env`, argv or an upload, so a profile's `env` table is refused on
  a sandboxed profile. A Claude subscription login inside a sandbox is not
  supported in v0.10.0; use an API key provider.

## Setup wizard

```console
tongs-agents sandbox setup [-g GATEWAY] [--harness claude|codex|pi] [--folder DIR]
    [--profile NAME] [--image IMAGE] [--network PRESET]... [--provider NAME]...
    [--chat] [--policy-file FILE] [--write-policy] [--probe [--yes]] [--print]
```

On a terminal it opens a Textual wizard; with `--print`, `--probe`,
`--write-policy` or without a terminal it prints the same steps. Either way
it never writes the tongs-agents config file, never installs anything, and
never starts, stops or configures a gateway (row 18): it prints commands for
you to run.

1. **Host.** The `openshell` CLI, its version and flavour (upstream, or
   midstream for `-rhaiv` builds), the gateway (`openshell [-g G] status -o
   json`), which local gateway drivers the host could use (Podman, Docker or
   `/dev/kvm`, a hint only), Landlock, OpenSSH, the optional prover and the
   telemetry disclosure, the same lines as `doctor`. When the gateway answers,
   `openshell provider list -o json` shows which providers exist (names and
   types only; OpenShell prints key names, never values). A missing or too old
   CLI, or a gateway that does not answer, comes with the commands to check or
   start it yourself. The gateway field must be a gateway name (letters,
   digits, `.`, `_` and `-`, at most 64); anything else is shown as a field
   error and no `openshell` command runs.
2. **Folder and harness.** The project folder that is uploaded, the harness
   (`claude`, `codex` or `pi`), the profile name (default `<harness>-boxed`)
   and an optional image. The upload skips only what `.gitignore` lists, so
   the folder is refused when it is `/`, your home folder or a folder that
   holds it (also through a symlink): uploading it would copy `~/.ssh`,
   cloud credentials and harness logins where the agent can read them. The
   folder defaults to the current directory unless it is one of those; then
   the field starts empty and `--print` needs `--folder`. When the folder
   holds `.ssh`, `.aws`, `.gnupg`, `.kube`, `.docker`, `.env`, `.netrc` or
   `.git-credentials`, the final notes say so.
3. **Network and credentials.** Network presets for the generated policy;
   everything else stays denied and goes through
   [access requests](#access-requests). The harness's model API is checked
   by default; `--network none` selects no preset, so all egress is denied. Credentials are OpenShell provider names (row 13), suggested
   per preset:

   | Preset | Allows | Provider (type) |
   | --- | --- | --- |
   | `anthropic` | `api.anthropic.com` (REST, read-write) for `claude` | `claude-code` (`claude-code`) |
   | `openai` | `api.openai.com`, `auth.openai.com`, `chatgpt.com` for `codex` | `codex` (`codex`) |
   | `openrouter` | `openrouter.ai` for `node` (Pi) | `openrouter` (`generic`, `OPENROUTER_API_KEY`) |
   | `github` | `api.github.com` read-only, `github.com` clone and fetch, no push, for `git` and `gh` | `github` (`github`) |
   | `pypi` | `pypi.org`, `files.pythonhosted.org` for Python, pip and uv | none |
   | `npm` | `registry.npmjs.org` for node and npm | none |

   Only `anthropic`, `openai` and `github` come from upstream provider
   profiles (`claude-code`, `codex` and `github` in `providers/*.yaml` of the
   OpenShell repository): a subset of their endpoints, with their binary
   paths. `pypi` takes its first two hosts from upstream's `pypi` profile but
   uses its own binary paths; `openrouter` and `npm` are tongs-agents' own.
   Every provider type is one upstream's CLI accepts (`normalize_provider_type`
   in `crates/openshell-providers/src/lib.rs`). OpenShell has no `openrouter`
   provider type, so the OpenRouter credential is a `generic` provider holding
   `OPENROUTER_API_KEY`, the variable Pi reads; a gateway with its own
   `openrouter` provider profile can use that instead. An image that installs
   a harness elsewhere needs its own policy. **Chat** only adds `tongs-agents` to
   the image probe; chat is still granted per pane from the sidebar.
4. **Review.** The generated policy (schema version 1: OpenShell's default
   filesystem, Landlock `best_effort` and `sandbox` user, plus one
   `network_policies` entry per preset) is shown in full. Only from here can
   you save it (`Save policy`, or `--write-policy`; a new file with mode
   0600, never over an existing one, by default
   `<config dir>/policies/<profile>.yaml`) or probe the image. The probe
   starts one throwaway sandbox from the image with exactly the reviewed
   policy, labelled `tongs-agents.launch=<random id>`, with no provider and no
   upload, waits for Ready, runs `sh -c` with `command -v` through `sandbox
   exec --no-tty --no-login-shell` for `sh`, `bash`, `tar`, `cat`, `mv`,
   `head`, `base64`, the harness binary and, with chat, `tongs-agents`, then
   deletes the sandbox. While the probe runs the wizard names its sandbox and
   cannot be cancelled or finished (`ctrl+q` and Cancel say why and print the
   delete command), so no create is left running unseen. If the delete
   fails the wizard says so and prints
   `tongs-agents sandbox delete [--gateway <name>] <sandbox>` (with the
   wizard's gateway when one is set), or `prune` removes it later. Off a
   terminal `--probe` needs `--yes`. Changing the presets or the profile name after the
   review asks for a new one before anything is saved or probed.
5. **Commands and profile.** What is left for you: the CLI or gateway
   commands, `openshell provider create --name <name> --type <type>
   --from-existing` for each provider the gateway lacks, or `--credential
   <VARIABLE>` instead of `--from-existing` for a `generic` one such as
   `openrouter` (either way the CLI reads the key from your environment;
   nothing passes through tongs-agents), saving the
   policy, the `[profiles.<name>]` snippet to add to the config file yourself
   (it is checked with the profile loader first), and the spawn command,
   `tongs-agents workspace --profile <name>`. The wizard prints this text
   again when it exits.

Exit status 0 means the CLI and gateway are ready (and a requested probe found
everything); 1 otherwise, or when the wizard was cancelled.

`provider list -o json` and the probe are instrumented against the fake CLI
only; their shapes follow upstream source and are unvalidated against a live
gateway until the maintainer's run (row 21). The printed `provider create`
commands were checked against upstream's CLI definition (`ProviderCommands::Create`
in `crates/openshell-cli/src/main.rs`) and provider list, not run.

## Profile keys

```toml
[profiles.claude-boxed]
command = ["claude"]
cwd = "~/src/project"          # its contents are uploaded into workdir

[profiles.claude-boxed.sandbox]
image = "registry.example.com/org/claude-agent:1"   # --from
providers = ["claude-code", "github"]               # --provider, repeatable
policy = "~/policies/claude.yaml"                   # --policy (a host file)
# name = "my-box"            # default: tongs-<profile>, dashes for underscores
# workdir = "/sandbox/workspace"
# mode = "exec"              # or "attach"
# upload = true              # exec mode default; attach mode needs false
# gateway = "local"          # openshell -g
# cpu = "2"
# memory = "4Gi"
# policy_advisor = false     # per-sandbox agent_policy_proposals_enabled
# [profiles.claude-boxed.sandbox.driver_config]   # --driver-config-json, unchanged
```

Unknown keys are errors. A sandboxed profile also refuses `env`, `config_dir`
(the harness config is the one tongs-agents uploads) and `mcp_control` (MCP
control and the fleet tools are not available inside a sandbox). `cwd` is
required while `upload` is true. `workdir` is an absolute path inside the
sandbox and the agent's working directory: the contents of `cwd` land
directly in it. `policy` is a file on the host.

## What a launch does

The in-pane launcher claims its launch record as usual, then:

1. checks `openshell --version`;
2. looks the sandbox up with `openshell sandbox get <name> -o json`. A sandbox
   carrying `tongs-agents.profile=<profile>` is **reused**: its files and agent
   state survive a gateway drop, so relaunching the profile restarts the agent
   in place. A sandbox of that name without the label is never touched;
3. otherwise creates it detached, running the command in `cwd`:
   `openshell sandbox create --name <name> --detach --label tongs-agents.launch=<id>
   --label tongs-agents.profile=<profile> [--from ...] [--provider ...] [--policy ...]
   --upload .:<workdir> --upload <staging dir>/.tongs-agents:<workdir>`.
   Approval mode is never set, so OpenShell's manual default stays;
4. waits for phase `Ready`, printing one line per transitional phase
   (`Unknown`, `Provisioning`) for at most 180 seconds. Any other phase, or
   the deadline, fails in the pane with the phase and the command to inspect,
   start or delete the sandbox. Nothing is retried silently;
5. on reuse, uploads the harness config again (never the workspace) with
   `openshell sandbox upload <name> <staging dir>/.tongs-agents <workdir>
   --no-git-ignore`;
6. with `policy_advisor = true`, runs
   `openshell settings set <name> --key agent_policy_proposals_enabled --value true`
   (never `--global`);
7. records the launch for the bridge in `$XDG_STATE_HOME/tongs-agents/sandbox/`
   with the pane's terminal identity, then execs the pane command.

**exec mode** (default) execs
`openshell sandbox exec -n <name> --tty --workdir <workdir> --env TONGS_AGENTS_LAUNCH_ID=<id>
--env TONGS_AGENTS_PROFILE=<profile> --env TONGS_AGENTS_SPOOL=<spool> --env <config var>=<config dir>
-- <agent argv>`. The keyboard is untouched, `send` reaches the agent and the
pane ends with the agent. The Zellij pane survives client detach; a lost
gateway connection ends the agent, and relaunching the profile reuses the
sandbox.

**attach mode** (opt-in) creates the sandbox with the agent as its main
process and execs `openshell sandbox connect <name>`, which replays output on
reattach. Caveats, also printed in the pane: OpenShell consumes Ctrl-D as
detach and holds Ctrl-P for its Ctrl-P Ctrl-Q detach, which Zellij binds by
default (Ctrl p pane mode, Ctrl q quit). OpenShell cannot upload with a main
process, so the image must hold the workspace, and no harness config is
uploaded: status comes from hooks the image configures to spool into
`TONGS_AGENTS_SPOOL`, else from the sandbox phase.

Exit status 69 from the launcher means the sandbox could not be prepared.

OpenShell places a directory upload at `<dest>/<directory name>/`, like
`cp -r`, whether or not `<dest>` exists, and flat at `<dest>` only for a
source without a name such as `.` (upstream `ssh.rs`,
`directory_upload_prefix`). That is why the workspace is uploaded as `.` from
`cwd`, landing exactly at `workdir`, and why the harness config is staged in
a directory named `.tongs-agents` and uploaded to `workdir`, landing at
`<workdir>/.tongs-agents` on create and on reuse alike.

## Inside the sandbox

Everything tongs-agents puts in the sandbox lives under
`<workdir>/.tongs-agents/`:

| Path | Contents |
| --- | --- |
| `config/<harness>/` | `CLAUDE_CONFIG_DIR`, `CODEX_HOME` or `PI_CODING_AGENT_DIR` |
| `spool/<launch id>/` | one file per hook event (`spool/main` in attach mode) |

The sandbox flavour of the emitters writes, per harness:

- Claude Code: `settings.json` with spool hooks; Codex: `hooks.json`;
- Pi: `extensions/tongs-agents.js`, the packaged extension, which writes spool
  files itself when `TONGS_AGENTS_SPOOL` is set (Pi's extension auto-discovery
  in a fresh agent directory is unvalidated);
- skills and instructions from the dotfiles source, scoped by harness and
  profile as on the host, when one is configured;
- no MCP server (neither the tongs-agents fleet and control tools nor your
  own, whose commands are host paths), no credentials, no host paths.

A spool hook is `sh -c '<script>' tongs-agents-spool <event>`: it writes the
raw payload with `cat` to a dot-prefixed temporary file and renames it to
`<19-digit nanoseconds>-<pid>-<event>.json`, and always exits 0.

## The bridge

The sidebar's poll loop (and `python -m tongs_agents.sandbox sync` for headless
use) runs the bridge before session maintenance. For each recorded launch whose
pane process is still the claimed one, one
`openshell sandbox exec -n <name> --no-tty --no-login-shell -- sh -c ...` prints
at most 64 spool files, oldest first, base64 encoded, and removes them; a flood
drains over several polls. Inside the sandbox, symlinks, non-regular entries,
names outside `[A-Za-z0-9._-]` and files over 64 KiB are removed unread; on the
host every file is checked again (name pattern, event known to the harness,
size, JSON object) and parsed with the hook's own normalisation. Payload pids,
launch ids and terminal fields are discarded: the record takes the pane
process's pid and start token, the launch id and profile, the host `cwd` and the
pane's terminal identity, and carries `source: sandbox` and `sandbox: <name>`.
Records are written through the session store's import writer, the only place
the bridge writes. Because the spool is writable by the agent, one launch
keeps at most 4 distinct session records (any status, until maintenance
removes them); events for further session ids are dropped, with one
diagnostic per launch and sync. Ended sessions count until maintenance
removes them (at least 5 minutes), so several `/clear` restarts in a few
minutes can use up the slots for a while. The bridge reads the session store once per
sync.

Until the first hook event arrives, a `sandbox-<launch id>` record shows the
sandbox phase: `Unknown` and `Provisioning` as started, `Ready` as started
(waiting for the agent), `Error` as error, and `Completed`, `Stopped`,
`Suspending`, `Suspended` and `Deleting` as ended.

An imported record is maintained like any other: it lives while the pane's
`openshell` process does (session maintenance accepts that process for a record
only when the launch record claims exactly that pid and start token) and is
removed after it exits. Launch records for the bridge are removed once their
launch is gone.

Process discovery on Linux skips a process whose pid namespace is neither
the sidebar's nor the init pid namespace, so a containerised `claude` on a Docker or
Podman driver is not also listed as `discovered` (unvalidated against a live
gateway), while a sidebar that itself runs in a pid namespace of its own
still discovers agents on the host. The init namespace is read from
`/proc/1/ns/pid`, which usually needs privileges; when it is unreadable the
kernel's fixed init namespace inode (`pid:[4026531836]`) stands in. An
unreadable namespace of the sidebar or of the process never skips it.

Chat in sandboxed sessions (row 11) is M5 work; the bridge has the seam
(`relay_chat`) but relays nothing yet.

## Managing sandboxes

`tongs-agents sandbox` manages the sandboxes tongs-agents created. Every
action touches only a sandbox carrying a `tongs-agents.profile` or
`tongs-agents.launch` label: an unlabelled sandbox is refused by name and
skipped by `list` and `prune` (row 20). `NAME` is the sandbox name (`list`
shows it); the gateway is `-g`, else the profile's `sandbox.gateway`, else
openshell's default.

| Command | What it runs |
| --- | --- |
| `sandbox list [--json]` (default) | `openshell sandbox list --limit 1000 -o json` on the default gateway and every profile gateway, filtered to labelled sandboxes, with whether an agent pane runs in each |
| `sandbox status NAME [--json]` | `openshell sandbox get NAME -o json` |
| `sandbox start NAME` | `openshell sandbox start NAME`, unless it is already Ready (unvalidated, see below) |
| `sandbox stop NAME [--force]` | `openshell sandbox stop NAME`, unless it is already stopped (unvalidated, see below) |
| `sandbox delete NAME... [--yes] [--force]` | `openshell sandbox delete NAME`; an absent sandbox is reported and skipped |
| `sandbox prune [--all] [--yes]` | deletes labelled sandboxes whose profile is no longer configured (with `--all`, every labelled one); never one an agent pane runs in |
| `sandbox pull NAME DEST [--path P] [--workdir W]` | `openshell sandbox download NAME <workdir>/<P> DEST` |
| `sandbox logs NAME [-n N] [--since S] [--source all\|gateway\|sandbox]` | `openshell logs NAME -n N` (never `--tail`) |
| `sandbox policy show NAME [--full]` | `openshell policy get NAME` |
| `sandbox policy check [NAME] [--policy F] [--boundary F]`, alias `sandbox diff` | a diff, then `openshell-prover check F --boundary <active policy>` |
| `sandbox advisor on\|off NAME` | `openshell settings set NAME --key agent_policy_proposals_enabled --value true\|false` |
| `sandbox sync` | one bridge pass, like `python -m tongs_agents.sandbox sync` |
| `sandbox setup [...]` | the [setup wizard](#setup-wizard): read-only checks, a policy to review, an optional image probe, printed commands and snippet |
| `sandbox rules list NAME [--status pending\|approved\|rejected]` | `openshell rule get NAME --status pending`, every proposal in full |
| `sandbox rules approve NAME CHUNK` | shows the proposal, asks at a terminal, then `openshell rule approve NAME --chunk-id CHUNK` |
| `sandbox rules reject NAME CHUNK --reason R [--yes]` | shows the proposal, asks (or `--yes`), then `openshell rule reject NAME --chunk-id CHUNK --reason R` |

Lifecycle commands are idempotent: starting a Ready sandbox, stopping a
stopped one (`Stopped`, `Suspending`, `Suspended`, `Completed`), deleting an
absent one and pruning twice all succeed without calling OpenShell again.
`start` and `stop` read the sandbox again afterwards and report success only
for `Ready` (or `Provisioning`, reported as starting) and a stopped phase
respectively; any other phase, or a sandbox that vanished, exits 1 with the
phase it reports. **Unvalidated:** the design record lists `sandbox start`
and `stop` from upstream source, but an upstream main checked for this
change has neither subcommand, and its `SandboxPhase`
(`proto/openshell.proto`) has only `Unknown`, `Provisioning`, `Ready`,
`Error` and `Deleting`. The stopped phase names and both subcommands are
kept unvalidated pending the maintainer's live run (row 21); on a CLI
without them `start` and `stop` fail with OpenShell's error.
`stop` and `delete` refuse a sandbox an agent pane is still running in (a
recorded launch whose pane process is alive) unless `--force`. Deleting
discards every file in the sandbox, so `delete` asks first, and off a
terminal needs `--yes`; `prune` without `--yes` off a terminal only lists
what it would delete. `prune` refuses to run while the config file has an
invalid profile, so a typo never makes configured sandboxes look orphaned;
`list` then warns and queries only the default gateway.

`pull` copies out of the sandbox's working directory (the profile's
`sandbox.workdir`, else the one recorded at launch, else
`/sandbox/workspace`); `--path` is relative to it and may not use `..`.
OpenShell itself only downloads from inside that directory. `DEST` must not
exist or must be an empty directory, so a pull never overwrites host files.

`policy check` compares a candidate policy, `--policy` or the profile's
`sandbox.policy`, with the sandbox's active policy
(`openshell sandbox get NAME --policy-only`) or with `--boundary`, prints a
unified diff, then runs `openshell-prover check <candidate> --boundary
<active policy>` (row 17). The prover is optional: without it on `PATH` the
diff is shown unproved and the command still succeeds. A non-zero prover exit
(findings or failure) makes the command exit 1. Nothing here applies a
policy, changes the approval mode or writes a `--global` setting (rows 14,
16); `advisor` changes only the named sandbox. Rules are approved one at a
time, only as described in [Access requests](#access-requests).

Text from the gateway and the sandbox (logs, policies, labels) is printed
with terminal escape sequences and control characters removed, because an
agent can write it.

### Doctor

When a profile is sandboxed or `openshell` is on `PATH`, `tongs-agents doctor`
adds:

- `openshell`: the CLI's path, version and flavour (an error below 0.1.0, for
  an unreadable version or when a sandboxed profile has no CLI, a warning
  below 0.1.6 or above 0.1.x). With no sandboxed profile an unusable
  `openshell` on `PATH` is only a warning, since nothing needs it;
  the gateway is asked only when the CLI is usable;
- `openshell-gateway`: `openshell [-g G] status -o json` (5 second timeout)
  for each gateway a sandboxed profile uses, a warning when it fails.
  tongs-agents never starts or logs in to a gateway;
- `openssh`: an `ssh` client, which uploads, `create --upload` and `pull`
  need;
- `landlock` (Linux): the kernel's Landlock ABI through
  `landlock_create_ruleset`; OpenShell needs 3 or newer;
- `openshell-prover`: an info line when the prover is not installed;
- `openshell-telemetry`: upstream gateways send anonymous telemetry by
  default and midstream (`-rhaiv`) images build it out; the opt-out is
  `OPENSHELL_TELEMETRY_ENABLED=false` in the gateway's own environment.
  tongs-agents adds no telemetry and never changes gateway configuration.

### Sidebar

While any listed agent runs in a sandbox, the agents table gains a last
column: `Sandbox` with the sandbox name at full width, `Box` with a `box`
marker in compact rows. Rows are matched to sandboxes through their launch id
and the launch records under `$XDG_STATE_HOME/tongs-agents/sandbox/`; without
sandboxed agents the table keeps its usual columns.

Every command above is instrumented against the fake CLI and a fake
`openshell-prover` (`tests/fixtures/fake_openshell/`). The shapes of
`sandbox list|start|stop|download`, `sandbox get --policy-only`, `policy get`,
`logs`, `status -o json` and `openshell-prover check` and its exit status are
unvalidated against a live gateway and a prover release until the
maintainer's run (row 21).

### Access requests

OpenShell drafts a network rule for every connection a sandbox's policy
blocks and, with the policy advisor on, for each proposal the agent submits.
Approving one widens what the agent can reach, so it stays a decision for you
(rows 14 and 15): tongs-agents never sets an approval mode, never runs `rule
approve-all`, and offers approval in no MCP tool. Approvals happen in the
sidebar or with `tongs-agents sandbox rules`, one rule at a time.

**Sidebar.** Every 5 seconds the sidebar runs
`openshell [-g G] rule get <sandbox> --status pending` (with `NO_COLOR=1`, 15
second timeout) for the sandboxes its listed rows run in, and nothing at all
while no row is sandboxed. Only one poll runs at a time: while a slow or
unreachable gateway holds one, later ticks wait and one more poll follows it,
and a poll whose worker was cancelled is dropped, so older results never
replace newer ones. A row whose sandbox has pending rules reads as
waiting, so it counts as needing you, `n` jumps to it and the usual desktop
notification fires; its detail says how many requests wait. Each new request
also raises a notice in the sidebar naming the sandbox and the proposal's
`Rule:` line, marked when the proposal has `Security:` notes. Then:

- `a` (or the palette's **Review access requests**) opens the requests of the
  selected row's sandbox (or the first sandbox with requests), with each
  proposal's full text: rule, binary, confidence, rationale, `Security:`,
  `Prover:`, endpoints, binaries and hits;
- `a` in that view opens **Approve this network access?** with the full text
  again in printed order, after a note naming any `Security:`, `Prover:` or
  `Application:` lines to read, in a scrollable body whose height is capped
  so the Yes and No buttons stay on screen at any terminal size. Only the **Yes, approve** button approves (a click, or Tab to it and
  Enter); no single key does, so a stray key never approves text still
  scrolled out of view. `n` or Escape cancels;
- `d` asks for a reason, which is required, and rejects.

Before acting, tongs-agents reads the pending list again: a rule that is no
longer pending is refused, and an approval is refused when the proposal
changed since it was shown (a growing hit count does not count as a change).
A failed `rule get` (gateway unreachable, not logged in) shows a warning and
does not mark the row.

**What you can trust.** OpenShell prints the agent's rationale as it is, so a
rationale with line breaks can add lines that look like the fields printed
after it (`Security:`, `Prover:`, `Application:`, `Candidate:`,
`Endpoints:`, `Binaries:`, `Hits:`). Only the lines before `Rationale:`
(chunk, status, rule, binary and confidence) are certainly OpenShell's. Every
view, the CLI included, therefore shows the `Rationale:` line and all lines
after it under the heading "From Rationale on, any line may be the agent's
text", each prefixed with `> `, and never moves them up; the notices name the
`Rule:` line. A rationale that repeats a label OpenShell also printed fails
the parse (see below), so it can add lines but never replace OpenShell's.
Check the `Rule:` line against the endpoints before you approve.

**CLI.** `sandbox rules list NAME` prints the proposals. `sandbox rules
approve NAME CHUNK` prints the proposal and asks; it refuses to run off a
terminal and has no `--yes`. `sandbox rules reject NAME CHUNK --reason R`
prints it and asks, or takes `--yes`. Both refuse an unlabelled sandbox.

**Parsing.** `rule get` has no JSON output, so its text is parsed strictly:
the known first line, exactly the number of rules the header announces, and
only the labels upstream `sandbox_draft_get` prints, in its order, each once
(`Validation:` is read as the older name of `Prover:`). Escape sequences and
control characters are removed first, because the agent writes the
rationale. Output that does not match, such as a new label or a rationale
that forges a second rule, still marks the row as needing you, but the
review view and `rules list` show the raw text and nothing can be approved
from tongs-agents; decide with `openshell rule approve` or `reject` yourself.
An upstream `--output json` for `rule get` would remove this parser; asking
for it is the maintainer's call (row 15).

The text format is taken from upstream source (`run.rs` at `ba302a6`), not
from a live gateway: the fixtures in `tests/fixtures/openshell_rules/` are
built by hand, and the live check is pending the maintainer's run (row 21).

## Live validation

```console
python validation/openshell.py --image <image> [--gateway <name>] [--provider <p>]
    [--policy <file> --allow-host <host>] [--deny-host <host>] [--deny-path <path>]
    [--approval-host <host> [--reject-host <host>]] [--keep] [--output <file>]
```

Run it against a gateway you started yourself, for example the rootless Podman
gateway described in the design record. It creates one uniquely named,
labelled sandbox through the launcher, runs the pane command in a
pseudo-terminal with a scripted fake agent (no model session), and checks that
hook status reaches the store within 4 seconds, that the workspace and config
landed in the working directory, and that a relaunch reuses the sandbox.
Positive controls run first: a write in the working directory and, with
`--policy`, a fetch of `--allow-host` (a host or URL the policy allows) must
succeed. Only then are the denied host and path checked; without `--policy`,
or when the matching control did not pass, those checks are recorded as not
applicable (`passed: null`). With `--approval-host` (a host the sandbox
policy denies), it also checks access requests end to end: a fetch of that
host is denied, the drafted rule shows as a needs-you row in a headless
sidebar, the validator approves it there with the same keys you would use
(`a`, `a`, then the Yes button), and the retry passes once the rule hot-reloads; then a fetch
of `--reject-host` (default `example.net`) is denied, rejected there with a
reason (`a`, `d`) and stays denied. This is the one place tongs-agents
presses approve for you, and only in the throwaway validation sandbox you
asked for. tongs-agents state and config go to a temporary
directory; the sandbox is deleted whenever it exists at the end, even when
the launch failed after creating it, unless `--keep`. The test suite runs the same script
against the fake CLI and labels that evidence `instrumented`.
