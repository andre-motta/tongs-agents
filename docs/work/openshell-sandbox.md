# OpenShell sandbox integration

Status: approved by the maintainer on 2026-11-03. Every checklist row is
approved as recommended. On row 4 the maintainer asked for reattachable
sandboxed agents: `exec` stays the default because the agent's Zellij pane
survives client detach and the Zellij session is reattached as usual, and
M7-A must reuse the labelled sandbox when the profile is relaunched after a
gateway drop. `attach` mode stays a per-profile opt-in with its key caveats.
Review corrections applied the same day (Ready phase before exec,
`rule approve-all` in the surface list).

Issue: [#34](https://github.com/andre-motta/tongs-agents/issues/34) (M7-0,
investigation). Repository base: `0be4fed`. Research date: 2026-11-03 (UTC).
Depends on #11 (M1-D, profiles and launcher) and #27 (M4-A, read-only readers
and one cleanup owner). Consumers: M7-A sandboxed profiles (#35), M7-B policy
and sandbox commands (#36), M7-C approvals in the sidebar (#37), M7-D setup
wizard (#38).

This record decides how tongs-agents runs Claude Code, Codex and Pi inside
NVIDIA OpenShell sandboxes as real Zellij panes, how status, hooks and chat
cross the sandbox boundary, and how access requests reach the maintainer. It
targets the upstream OpenShell CLI and stays compatible with the
opendatahub-io midstream fork, as the maintainer decided. Nothing under `src/`
changes in this item. Evidence:
[`openshell-investigation-fedora44.json`](../../validation/evidence/openshell-investigation-fedora44.json).

## Summary

- Drive OpenShell through its `openshell` CLI as an argv subprocess. Do not add
  the `openshell` Python SDK, even as an extra: it is Alpha, pulls `grpcio`,
  `protobuf`, `httpx`, `cloudpickle` and `googleapis-common-protos`, must match
  the gateway release, and its curated API has no terminal attach, upload or
  rule approval.
- Run the agent pane as `openshell sandbox exec --tty` against a sandbox the
  launcher created detached, with the project uploaded first. The alternative,
  the agent as the sandbox's main process plus `sandbox connect`, loses Ctrl-D
  to OpenShell's detach filter and its Ctrl-P Ctrl-Q detach collides with
  Zellij's default Ctrl p (pane mode) and Ctrl q (Quit) bindings.
- Workspaces are uploaded, not mounted. Host directory mounts need the gateway
  operator to enable bind mounts and disable resource admission, which OpenShell
  documents as able to bypass filesystem policy; tongs-agents never asks for it.
- The host bridge is host-initiated: hooks inside the sandbox spool raw events
  under the sandbox working directory, and the sidebar pulls them over
  `sandbox exec` or `sandbox download`, validates them and writes session
  records on the host keyed to the pane's launch record. No host listener, no
  bind mount, no network policy exception.
- Granted chat uses the same bridge (outbox pulled, inbox pushed). MCP control
  and fleet tools are not offered inside a sandbox.
- Credentials come only from OpenShell providers. Approvals stay manual and
  human-only: the sidebar lists pending proposals and approves or rejects
  through the CLI, never through MCP, and never switches a sandbox to automatic
  approval.
- The CLI argument surface is identical in upstream v0.1.6, upstream main and
  midstream v0.1.6-rhaiv.4. Upstream gateways send anonymous telemetry by
  default; midstream images compile it out. tongs-agents reports this and never
  changes it.

## Approval checklist

The maintainer approves or changes each row. Rows marked **sign-off** depart
from shipped or previously approved behaviour.

| # | Decision | Recommendation | Needs |
| --- | --- | --- | --- |
| 1 | Integration surface | `openshell` CLI as an argv subprocess (never a shell), `-o json` where the command has it; no Python SDK extra | approval |
| 2 | Supported OpenShell | Floor 0.1.6; parse the leading `X.Y.Z` of `openshell --version` so midstream `0.1.6-rhaiv.N` passes; refuse anything below 0.1.0; warn on a newer minor than the one validated | approval |
| 3 | Profile shape | A `sandbox` table on a profile (image, providers, policy file, upload, workdir, mode, gateway, cpu, memory, policy advisor), validated like other profile keys | approval; amends the M1-D profile schema |
| 4 | Pane mode | Default `exec`: `sandbox create --detach --upload` then the pane execs `sandbox exec --tty`; opt-in `attach`: agent as main process plus `sandbox connect` | **sign-off**: `exec` has no reattach or output replay |
| 5 | Workspace in | Upload the profile `cwd` with OpenShell's `.gitignore` filtering; tongs-agents never configures mounts; a profile may pass an operator-approved mount through `--driver-config-json` | approval |
| 6 | Workspace out | `tongs-agents sandbox pull` wrapping `sandbox download` (working-directory scoped); git push through a GitHub provider is the agent's own route | approval |
| 7 | Host bridge transport | Host-initiated pull of a spool under the sandbox working directory over `sandbox exec` or `sandbox download`; no host listener, bind mount or `host.openshell.internal` rule | **sign-off** |
| 8 | Hook inside the sandbox | Emitted sandbox hooks spool the raw hook payload with POSIX `sh` and coreutils; the host runs the normal hook parsing on import; the Pi extension writes the spool itself | **sign-off**: hooks no longer write session records directly in this mode |
| 9 | Session identity | Imported records carry the launch id, the host pane process pid from the launch record, the sandbox name and source `sandbox`; sandbox pids and terminal fields are discarded | approval |
| 10 | Process discovery | Ignore processes in another pid namespace so a containerised `claude` is not also listed as `discovered` | approval; behaviour unvalidated |
| 11 | Chat in sandboxed sessions | Grants allowed; the in-sandbox MCP server runs in a spool-only chat mode (outbox pulled, inbox pushed by the bridge) and never opens the SQLite file; needs tongs-agents in the image | **sign-off**: amends the M5 chat design |
| 12 | MCP control and fleet tools | Not emitted into sandboxed harness configs; unavailable inside a sandbox | approval |
| 13 | Credentials | OpenShell providers only; never secrets in `--env`, argv or uploads; Claude subscription login inside a sandbox is unsupported in v0.10.0 | approval; OAuth stance is a maintainer decision |
| 14 | Approval authority | Manual only: never set `proposal_approval_mode=auto` or `--approval-mode auto`, never run `rule approve-all`; approve and reject only from the sidebar or CLI by the maintainer, never via MCP | approval |
| 15 | Approval data source | Parse `openshell rule get <sandbox> --status pending` (no JSON output exists) with `NO_COLOR=1`; show the full text before approving; suggest `--output json` upstream | approval; filing upstream is the maintainer's call |
| 16 | Policy advisor | Per-profile opt-in that sets `agent_policy_proposals_enabled` on that sandbox only, never `--global` | approval |
| 17 | Policy prover | Optional `openshell-prover check <policy> --boundary <file>` in `sandbox policy check` and `doctor`; never required | approval |
| 18 | Gateway ownership | tongs-agents never installs OpenShell, never starts, stops or enables gateway services, never edits `gateway.toml` or `gateway.env`; the wizard prints commands | approval |
| 19 | Telemetry | `doctor` and the wizard disclose upstream's default-on gateway telemetry and its opt-out; tongs-agents itself adds none | approval |
| 20 | Sandbox lifecycle | Sandboxes are kept after the pane exits (OpenShell's default) and labelled; `list`, `delete` and `prune` touch only sandboxes carrying the tongs-agents label | approval |
| 21 | Live gate for M7 | Maintainer live run on a rootless Podman gateway with upstream v0.1.6 and a midstream build; a hosted OpenShell job is deferred | approval; a hosted gate would be **sign-off** |
| 22 | New dependencies | None | none |

## Evidence labels

| Label | Meaning here |
| --- | --- |
| **live** | Observed on this host in this run. |
| **instrumented** | Exercised by a scripted probe with fakes. None was produced in this run. |
| **unvalidated** | Read from OpenShell source or published docs at the pinned commits below, not exercised. Each such finding says what a live check needs. |

## Sources and method

| Item | Value |
| --- | --- |
| Host | Fedora Linux 44, x86_64, kernel 7.2.11 |
| Upstream | [NVIDIA/OpenShell](https://github.com/NVIDIA/OpenShell) main `ba302a6` (2026-11-03), latest stable `v0.1.6` (2026-10-27) |
| Midstream | [opendatahub-io/openshell](https://github.com/opendatahub-io/openshell) main `71b5bdb` = tag `v0.1.6-rhaiv.4` (2026-11-02), upstream merge base `634ca7d` |
| Release assets | Upstream v0.1.6 `openshell` and `openshell-prover` x86_64 musl tarballs, checksum verified (live), not executed |

- **Clones.** The existing local clones were stale (upstream from
  2026-07-22, midstream from 2026-10-09), so fresh public clones of both
  repositories were read. Nothing was built.
- **Not executed.** Running the downloaded OpenShell binaries was refused by
  this session's permission policy. Every OpenShell behaviour in this record is
  therefore **unvalidated**, read from source and docs. No gateway, sandbox,
  agent or model session ran.
- **Live checks.** Landlock ABI through `landlock_create_ruleset` (ABI 10;
  OpenShell needs 3 or newer), the installed Podman (5.8.9; Docker is not
  installed), and Zellij 0.45.1's default keybindings from
  `zellij setup --dump-config` with private HOME, XDG and `ZELLIJ_CONFIG_DIR`
  and no session.
- **Source identity.** `crates/openshell-cli/src/main.rs` is unchanged from
  v0.1.6 to upstream main, and the midstream has no changes under `crates/`,
  `proto/` or `python/` against its upstream merge base. A finding read at
  upstream main therefore holds for v0.1.6 and the midstream CLI unless noted.

## Findings

### 1. Integration surface

**Unvalidated (source).** The CLI covers everything M7 needs: `sandbox create`
(`--name`, `--from`, `--provider`, `--policy`, `--upload`, `--detach`,
`--label`, `--annotation`, `--env`, `--env-from`, `--approval-mode`,
`--driver-config-json`, `--cpu`, `--memory`, `-o json`), `sandbox get|list -o
json`, `sandbox exec`, `connect`, `upload`, `download`, `stop`, `start`,
`delete`, `rule get|approve|reject|history|approve-all` (`approve-all` has an
`--include-security-flagged` switch; row 14 forbids it), `settings get|set`, `policy
get|set|update`, `provider`, `logs`, `status -o json` and `term`
([main.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/main.rs#L1445-L1905)).

The Python package is `openshell` on PyPI, classified Alpha, with five runtime
dependencies
([pyproject.toml](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/pyproject.toml#L16-L24)).
Its curated client creates, lists, stops and execs non-interactively
([sandbox.py](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/python/openshell/sandbox.py)),
and the docs ask for bindings from the same release as the gateway. A pane
still needs the CLI for a terminal, so the SDK would add weight without
removing the CLI. Runtime dependencies stay `textual` and `mcp` (accepted plan
compromise 14).

Gaps: `rule get` has no `--output` flag
([run.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/run.rs#L6731-L6839));
it prints labelled lines (`Chunk:`, `Status:`, `Rule:`, `Binary:`,
`Rationale:`, `Security:`, `Prover:`, `Endpoints:`, `Binaries:`). M7-C parses
these strictly and falls back to showing the raw text. A `--output json` flag
upstream would remove the parser; filing that request is the maintainer's
choice (row 15).

### 2. Interactive harness TUI in a Zellij pane

**Unvalidated (source and docs), Zellij bindings live.**

- `sandbox connect` attaches to the sandbox's canonical main process. It
  replays up to 1 MiB of output on reattach and retries a dropped transport for
  60 seconds
  ([sandbox overview](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/sandboxes/overview.mdx#L270-L311)).
- On that main attachment the supervisor consumes byte `0x04` (Ctrl-D) as a
  detach, holds `0x10` (Ctrl-P) until the next byte and detaches on Ctrl-P
  Ctrl-Q; exec channels are not filtered
  ([ssh.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-supervisor-process/src/ssh.rs#L44-L76),
  same at v0.1.6). An agent run this way never receives Ctrl-D, and Pi's
  Ctrl-P is delayed by one keystroke.
- **Live:** Zellij 0.45.1's default config binds `Ctrl p` to pane mode and
  `Ctrl q` to `Quit` in every mode except locked; Ctrl d is unbound. In a
  default-configured workspace the documented detach sequence never reaches
  OpenShell and the second key quits Zellij. 0.44.2 was not dumped.
- `connect`, `upload` and `download` run the host's OpenSSH `ssh` client with
  an `openshell` ProxyCommand; `connect` also forces `TERM=xterm-256color`
  ([ssh.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/ssh.rs#L216-L294),
  [ssh.rs upload and download](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/ssh.rs#L1518-L1831)).
  Depending on the path the CLI replaces itself with `ssh` or supervises it as
  a child. OpenSSH stays a host prerequisite (M7-D checks it) because
  `upload` and `download` need it, including `create --upload`.
- `sandbox exec` does not use `ssh`. The CLI dispatches it to
  `run::sandbox_exec_grpc`
  ([main.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/main.rs#L3650-L3689)),
  which with a terminal on stdin streams over the gateway's
  `ExecSandboxInteractive` gRPC call (unary `ExecSandbox` otherwise) while the
  `openshell` process itself puts the local TTY in raw mode and forwards input
  and resizes
  ([run.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/run.rs#L1987-L2125),
  [run.rs streaming](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/run.rs#L2580-L2851)).
  The ssh-based `ssh::sandbox_exec` is re-exported but has no caller. Inside
  the sandbox `TERM` is `xterm-256color` for a PTY and `dumb` otherwise
  ([boundary_exec.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-sandbox/src/boundary_exec.rs#L160-L172)).
  The launched pid in an `exec` pane is therefore the `openshell` CLI process,
  with no `ssh` child, and it stays alive until the remote command exits or
  the gateway stream ends.
- `sandbox exec --tty` opens an independent PTY with resize, a full keyboard
  and the command's exit code; argv is shell-escaped and run through `bash -lc`
  unless `--no-login-shell`. It has no reattach or replay, so a lost connection
  is expected to end the agent process.
- `sandbox create --upload` conflicts with a trailing command
  ([main.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-cli/src/main.rs#L1478-L1485)),
  so an agent that is the main process cannot have the project uploaded before
  it starts.

Recommendation (row 4): `exec` mode by default. The launcher runs `sandbox
create --name <n> --detach --upload <cwd>:<dest> --label
tongs-agents.launch=<id> [--provider ...] [--policy ...]` with no command (the
main process is a retained login shell), then `os.execvpe`s `openshell sandbox
exec -n <n> --tty --workdir <dest> --env TONGS_AGENTS_LAUNCH_ID=<id> --env
TONGS_AGENTS_SPOOL=<dest>/.tongs-agents -- <agent argv>`. The pane behaves like
an unsandboxed one: the keyboard is untouched, M3 `send` reaches the agent, and
the pane ends when the agent exits. Zellij panes survive client detach, so the
missing reattach only matters when the local gateway connection drops. `attach`
mode stays available for images that already contain the workspace, with the
key caveats shown in the pane and in `doctor`.

Implementation note (M7-A review): OpenShell places a directory upload at
`<dest>/<directory name>/` (flat only for a source such as `.`), so M7-A
uploads the workspace as `.` from `cwd` and the staged `.tongs-agents`
directory to the parent of its destination, `<dest>`, so it lands at
`<dest>/.tongs-agents`; see [sandbox.md](../sandbox.md#what-a-launch-does).

### 3. Workspace access

**Unvalidated (docs).**

- `sandbox upload` streams a tree into the sandbox, honours `.gitignore` inside
  a Git work tree, preserves symlinks and merges into an existing destination;
  `sandbox download` only reads paths that resolve inside the sandbox's
  canonical working directory
  ([transfer files](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/sandboxes/overview.mdx#L912-L983)).
  Both run `tar` inside the workload, so the image needs it.
- Mounts go through `--driver-config-json`, disabled unless the operator sets
  `allow_driver_config = true`. Named Docker or Podman volumes need operator
  labels for resource admission. Host bind mounts additionally need
  `enable_bind_mounts = true` and resource admission turned off, and OpenShell
  warns they "can bypass workspace isolation and filesystem policy". VM mounts
  share the same opt-in, and libkrun may not confine a compromised guest kernel
  to the source directory
  ([runtimes](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/sandboxes/runtimes.mdx#L150-L275)).
- Docker and Podman workspaces live in the container's writable layer:
  they survive stop and start but not deletion.

Recommendation (rows 5, 6): upload by default; results leave through
`tongs-agents sandbox pull` or the agent's own git push via a GitHub provider.
tongs-agents passes a profile's mount through unchanged but never writes gateway
config to enable one.

### 4. Host bridge for hooks, status and chat

**Unvalidated (docs and source).** A sandboxed workload's only egress is the
supervisor, which applies policy (Docker and Podman workloads run with
`network=none`, VM guests have no network device). Network policy never
authorizes loopback, link-local or unspecified
destinations
([schema](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/policies/schema.mdx#L141-L150)).
`host.openshell.internal` reaches the gateway host only on a bridge-reachable
address, and its trusted exemption applies only to a link-local pin
([proxy.rs](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/crates/openshell-supervisor-network/src/proxy.rs#L263-L268)).
So a hook in the sandbox cannot write `$XDG_STATE_HOME/tongs-agents/sessions`
on the host, and an in-sandbox MCP server cannot open the host chat file.

| Option | How | Verdict |
| --- | --- | --- |
| A. Host-initiated pull (recommended) | Hooks spool under `<workdir>/.tongs-agents/`; the sidebar pulls with `sandbox exec --no-tty --no-login-shell` or `sandbox download`, imports and trims | No new trust surface; works on every driver; latency unmeasured |
| B. Mounted state path | Bind mount a host state dir into the sandbox | Needs operator to disable admission; documented as bypassing filesystem policy; rejected as a default |
| C. Host endpoint | Host listener reached through `host.openshell.internal` plus a policy rule | Listener must leave loopback; differs per driver; adds an authenticated service; rejected |
| D. Named volume | Labelled Docker or Podman volume read on the host | Host read path differs per runtime and uid mapping; unvalidated; not pursued |

Design for option A (rows 7 to 12):

- **Spool.** `<workdir>/.tongs-agents/spool/` inside the working directory,
  which the default policy makes read-write (`include_workdir`) and `download`
  may read. Each event is one file, written to a temporary name and renamed.
  The agent can write there too, so everything pulled is untrusted data.
- **Hook command (row 8).** Sandbox-flavoured emitters write a hook command
  that only spools the raw payload with POSIX `sh`, `cat` and `mv`, so images
  need no Python. The Pi extension writes spool files directly when
  `TONGS_AGENTS_SPOOL` is set. The exact command is M7-A's.
- **Import.** The sidebar (and `tongs-agents sandbox sync` for headless use)
  pulls each active sandboxed launch, rejects symlinks, non-regular files,
  oversized files and bad names, parses payloads with the existing hook
  normalisation, and writes session records through the M4-A writer. The record
  takes its pid, terminal and pane identity from the host launch record; pids
  from inside the sandbox are discarded (row 9). No daemon: the pull runs in
  the sidebar's poll loop. A long-lived `exec` stream per sandbox is the
  fallback if polling misses the 4-second status target.
- **Sandbox phase.** When no hook data exists, `sandbox get -o json` phase maps
  to status (`Provisioning` and `Starting` show as started, `Error` as error,
  `Completed` as ended).
- **Discovery (row 10).** On Docker and Podman a container's `claude` is a
  host process in another pid namespace. Discovery should skip processes whose
  `/proc/<pid>/ns/pid` differs from its own, or each sandboxed agent may also
  appear as a `discovered` row.
- **Chat (row 11).** Chat stays off by default and granted per pane by the
  maintainer. For a granted sandboxed pane the in-sandbox MCP server runs in a
  spool-only chat mode: posts go to an outbox file that the bridge imports into
  the host store as that session, and the bridge pushes the session's inbox
  with `sandbox exec` and stdin. This needs tongs-agents (Python and `mcp`) in
  the image; M7-D checks it. Wake-on-mention uses the host pane send, which
  reaches the agent in both pane modes.
- **Control and fleet tools (row 12).** Inside a sandbox the fleet tools would
  see only that sandbox's spool, and MCP control would let a sandboxed agent
  drive host panes. Neither is emitted.

### 5. Providers and credentials

**Unvalidated (docs).** Providers store credentials in the gateway and the
supervisor adds them only to requests bound for profile-authorized endpoints;
the agent sees placeholders
([providers](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/providers/overview.mdx)).
Example profiles exist for `claude-code` (API key only, sent as `x-api-key` to
`api.anthropic.com`), `codex` (OAuth tokens injected for the CLI) and
`openrouter`, used by the upstream Pi tutorial
([providers/](https://github.com/NVIDIA/OpenShell/tree/ba302a6b0481ae02b647a5c84302aa201199f9da/providers)).
They must be imported and edited for the image's binary paths. `--env` values
are visible to the agent and trigger only a warning.

Recommendation (row 13): profiles name providers; tongs-agents never passes a
secret through `--env`, argv or an upload, and the sandbox flavour of the M2
emitters writes no credential files. A Claude subscription login inside a
sandbox would leave the OAuth token readable by the agent, so v0.10.0 documents
it as unsupported and uses an API key provider; the maintainer decides whether
to revisit.

### 6. Policy advisor, prover and approvals

**Unvalidated (docs).**

- OpenShell drafts a narrow rule for every blocked connection, whether or not
  the advisor is on. With `agent_policy_proposals_enabled`, the agent also
  gets a guide and the in-sandbox `http://policy.local` API to submit proposals
  and wait for decisions. Proposals only add network rules; they cannot change
  filesystem, Landlock or process settings
  ([advisor](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/policies/advisor.mdx)).
- Every proposal goes through the prover's risk check (link-local reach,
  uninspectable credentialed traffic, credential reach expansion, new HTTP
  methods) and destination flags. In `auto` mode unflagged proposals are
  approved without review, including access to new public hosts.
- The maintainer reviews with `rule get --status pending`, then `rule approve
  --chunk-id` or `rule reject --chunk-id --reason`. Approved rules hot-reload.
  `openshell term` also reviews, but it rejects without sending a reason.
- `openshell-prover check <candidate> --boundary <boundary>` runs offline
  against local files and covers filesystem, process, Landlock, L4 and REST
  rules; it refuses policies with GraphQL or MCP rules rather than ignoring
  them ([prover](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/how-it-works/policies/prover.mdx)).

Recommendation (rows 14 to 17): approvals are a security decision and stay with
the maintainer. tongs-agents shows pending proposals as NEEDS-YOU on the
sandboxed row, displays the full proposal text with its Security and Prover
lines, and requires a reason to reject. It never enables `auto`, never calls
`approve-all`, and exposes approval in no MCP tool, so a sandboxed or
unsandboxed agent cannot approve its own access.

### 7. Upstream, midstream and telemetry

**Unvalidated (source), release facts live.**

- Upstream ships weekly stable releases; Stable interfaces stay compatible
  across patches, minors may break with notice, and N and N-1 minors get fixes
  ([support matrix](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/about/support-matrix.mdx#L11-L50)).
  Pre-0.1.0 releases had breaking changes. v0.1.6 is the latest stable.
- The midstream main is upstream main of 2026-11-02 plus 147 non-merge
  commits that touch only build, CI, packaging, installer and ODH tooling, e2e
  tests, one skill and one Kubernetes docs page. It has no GitHub releases;
  its CLI is built against glibc into the
  `odh-openshell-cli` container image
  ([Dockerfile.konflux.cli](https://github.com/opendatahub-io/openshell/blob/71b5bdbf4222b593e397609821726abaa229fa9d/deploy/docker/Dockerfile.konflux.cli#L85-L96))
  and reports versions such as `0.1.6-rhaiv.4`.
- Telemetry is a default-on gateway feature, propagated to supervisors and
  disabled with `OPENSHELL_TELEMETRY_ENABLED=false` or compiled out
  ([telemetry](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/observability/telemetry.mdx)).
  The CLI source has no telemetry call sites. The midstream images build the
  gateway and supervisor with `defaults-without-telemetry`
  ([Dockerfile.konflux.gateway](https://github.com/opendatahub-io/openshell/blob/71b5bdbf4222b593e397609821726abaa229fa9d/deploy/docker/Dockerfile.konflux.gateway#L77)),
  while its Helm default and README still read as upstream's. "Telemetry off by
  default" therefore holds for the midstream images, not for a midstream source
  build with default features.

Recommendation (rows 2, 18, 19): accept both version forms, detect upstream
versus midstream from the version suffix only for reporting, and disclose the
telemetry default in `doctor` and the wizard with the opt-out, without changing
gateway configuration.

### 8. Host prerequisites

**Live:** Landlock ABI 10 and Podman 5.8.9 on this host. **Unvalidated
(docs):** a gateway needs Docker 28 or newer, Podman 5 with cgroups v2 and a
user socket, or KVM for the MicroVM driver; the sandbox needs Landlock ABI 3,
nested seccomp user notification and `SO_BINDTODEVICE`, and fails closed
without them
([kernel requirements](https://github.com/NVIDIA/OpenShell/blob/ba302a6b0481ae02b647a5c84302aa201199f9da/docs/about/support-matrix.mdx#L175-L200)).
Package installs run the gateway as a systemd user service on
`https://127.0.0.1:17670`. macOS runs it under Homebrew with Docker Desktop;
native Windows is a development branch. M7 is Linux x86_64 first, like the
rest of the plan.

## Alternatives considered

| Alternative | Why not |
| --- | --- |
| Python SDK as an optional extra | Alpha, five heavy dependencies, release-locked bindings, no terminal attach, upload or curated rule approval |
| Agent as main process by default | Ctrl-D swallowed, Ctrl-P delayed, detach keys collide with Zellij defaults, no upload before start |
| Bind-mounted host state or workspace | Requires disabling OpenShell's resource admission; documented filesystem policy bypass |
| Host HTTP endpoint for hooks and chat | Needs a non-loopback listener, per-driver host addressing and its own authentication |
| tongs-agents required in every image | Images for Node harnesses may lack Python; host and image versions would drift |
| Agent-approvable rules (auto mode or MCP tool) | Lets a sandboxed agent widen its own access |
| Chat refused for sandboxed sessions | Simpler, but v0.10.0 is the complete release; kept as the fallback if row 11 is declined |

## Proposed breakdown

### M7-A (#35): sandboxed profiles and the host bridge

Risk: **high** (crosses the sandbox trust boundary and writes the session store).

- Profile `sandbox` table (row 3), validated with the existing profile loader
  rules; unknown keys are errors.
- Launcher path for sandboxed profiles: create detached with labels
  `tongs-agents.launch` and `tongs-agents.profile`, upload `cwd`, upload the
  sandbox-flavoured harness config produced by the M2 emitters, then exec the
  pane (rows 4, 5). `attach` mode behind the profile key. Before exec,
  confirm the sandbox phase is Ready (`sandbox get -o json`); show a
  not-ready error in the pane instead of retrying silently. A relaunched
  profile reuses its labelled sandbox (files and state survive a gateway
  drop), so the agent can be restarted in place.
- A small `openshell` CLI wrapper module: argv only, `NO_COLOR=1`, bounded
  output, timeouts, version floor (row 2), never secrets in argv.
- Spool hook commands for Claude Code and Codex, and spool mode for the Pi
  extension (row 8); bridge import with launch identity (row 9) through the
  M4-A writer; phase fallback; pid-namespace filter in discovery (row 10).
- Chat spool mode and relay if row 11 is approved.
- Checks: mocked wrapper tests asserting exact argv; hostile spool tests
  (symlinks, oversized files, traversal names, bad JSON, floods); bridge never
  writes outside the sessions dir; live validator with a fake agent showing
  status within 4 s on upstream v0.1.6 and a midstream build.
- Owns: a new `src/tongs_agents/sandbox/` package, `profiles.py`, `launch.py`,
  emitter sandbox flavour, `discovery.py` filter, tests, `docs/sandbox.md`,
  `validation/openshell.py`.

### M7-B (#36): policy and sandbox commands

Risk: standard.

- `tongs-agents sandbox list|status|pull|delete|prune|logs`, restricted to
  labelled sandboxes (row 20); `sandbox policy show` (`policy get`) and
  `sandbox policy check` (prover, row 17); `sandbox advisor on|off` per sandbox
  (row 16).
- `doctor` lines for CLI presence, version, gateway reachability
  (`status -o json`), OpenSSH, Landlock and telemetry disclosure (row 19).
- Checks: mocked CLI tests; label filter refuses unlabelled sandboxes; no
  `--global` settings writes.

### M7-C (#37): approvals in the sidebar

Risk: **high** (security decision UI).

- Poll `rule get --status pending` per sandboxed row, strict parser with raw
  fallback (row 15); NEEDS-YOU attention and desktop notification; detail view
  with the full proposal; approve and reject (reason required) from the
  sidebar and from `tongs-agents sandbox rules approve|reject`.
- No auto mode, no approve-all, no MCP exposure (row 14).
- Checks: parser tests on captured text fixtures from a live run; tests that
  no MCP tool and no code path calls approve without a sidebar or CLI action;
  live validator where a fake agent is denied, a proposal appears in the
  sidebar, approval hot-reloads and the retry passes.

### M7-D (#38): setup wizard

Risk: standard.

- `tongs-agents sandbox setup`: detects the CLI and its flavour, checks the
  gateway, runtime hints, Landlock, OpenSSH and telemetry, lists provider
  profiles a profile needs, and probes a chosen image through `sandbox exec`
  for `sh`, `tar`, the harness binary and, for chat, tongs-agents.
- Prints commands and a profile snippet; never installs packages, edits
  OpenShell config, starts services or rewrites the tongs-agents config file
  (row 18).
- Checks: mocked journeys for missing CLI, old version, unreachable gateway and
  a ready host; live run on this host.

## What a live check needs

- Permission to run the upstream v0.1.6 `openshell` and `openshell-prover`
  release binaries (checksums recorded in the evidence file) from a temporary
  directory with private HOME and XDG dirs.
- A rootless gateway: the upstream `openshell-gateway` binary with the Podman
  driver against a Podman API socket started by hand for the run (no systemd
  unit changes), bound to 127.0.0.1, plus the upstream gateway, supervisor and
  default workload images. Whether the supervisor container needs capabilities
  beyond rootless defaults is unverified and must be checked before running.
- A midstream CLI: a source build of `openshell-cli` at the midstream tag, or
  the `odh-openshell-cli` image if pulling it is approved.
- A fake agent image (POSIX `sh`, `tar`, a scripted TUI) so no model session
  starts; denied and approved network rules against a local test endpoint.
- To confirm: Ctrl-D and Ctrl-P handling in both pane modes inside Zellij
  0.44.2 and 0.45.1, `exec` behaviour when the gateway restarts, spool pull
  latency, discovery duplicates on Podman, and the exact `rule get` text.

## Risks and open questions

- Pull latency over `exec` is unmeasured; the 4-second status target may need
  the streaming fallback.
- The `rule get` text format is not a Stable interface; the parser must fail
  visibly, not silently.
- OpenShell moves fast (weekly releases, a 0.1.7 prerelease in flight); the
  floor and the validated minor need review at each M7 item.
- Approving or rejecting through the CLI depends on the maintainer's gateway
  login; OIDC gateways may prompt in the sidebar's subprocess and must be
  handled as an error, not a hang.
- A hosted OpenShell gate on GitHub runners (kernel Landlock and seccomp
  support there) is unverified.
