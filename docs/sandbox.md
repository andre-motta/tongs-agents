# Sandboxed profiles (OpenShell)

A profile with a `sandbox` table runs its agent inside an
[NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandbox, still in its
own Zellij pane, with its status in the sidebar like any other agent. The
design and its approval record are in
[openshell-sandbox.md](work/openshell-sandbox.md) (#34); this page describes
what M7-A (#35) implements.

Status: **instrumented only.** Every OpenShell call has been exercised against
the fake CLI in `tests/fixtures/fake_openshell/`, which reproduces the argument
shapes and `sandbox get -o json` keys of the OpenShell CLI at upstream
`ba302a6` (equal to v0.1.6 and midstream v0.1.6-rhaiv.4). The live check on a
rootless Podman gateway with upstream v0.1.6 and a midstream build is pending
the maintainer's run (row 21, see [Live validation](#live-validation)).

## Before you start

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

## Profile keys

```toml
[profiles.claude-boxed]
command = ["claude"]
cwd = "~/src/project"          # its contents are uploaded into workdir

[profiles.claude-boxed.sandbox]
image = "registry.example.com/org/claude-agent:1"   # --from
providers = ["anthropic", "github"]                 # --provider, repeatable
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

## Live validation

```console
python validation/openshell.py --image <image> [--gateway <name>] [--provider <p>]
    [--policy <file> --allow-host <host>] [--deny-host <host>] [--deny-path <path>]
    [--keep] [--output <file>]
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
applicable (`passed: null`). tongs-agents state and config go to a temporary
directory; the sandbox is deleted whenever it exists at the end, even when
the launch failed after creating it, unless `--keep`. The test suite runs the same script
against the fake CLI and labels that evidence `instrumented`.
