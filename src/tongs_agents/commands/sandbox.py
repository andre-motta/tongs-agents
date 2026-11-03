"""``tongs-agents sandbox``: manage the OpenShell sandboxes tongs-agents labelled (#36).

Every action goes through ``sandbox.openshell`` (argv only, ``NO_COLOR=1``,
bounded output) and touches only sandboxes carrying a tongs-agents label
(``tongs-agents.profile`` or ``tongs-agents.launch``, row 20): an unlabelled
sandbox is refused by name and skipped by ``list`` and ``prune``.

- ``list``, ``status``, ``logs`` and ``policy show`` only read.
- ``start``, ``stop``, ``delete`` and ``prune`` are idempotent: starting a
  Ready sandbox, stopping a stopped one or deleting an absent one succeeds
  without calling OpenShell. ``stop`` and ``delete`` refuse a sandbox an
  agent pane is running in unless ``--force``; ``delete`` and ``prune``
  confirm first (``--yes`` when not on a terminal).
- ``pull`` wraps ``sandbox download`` and refuses a destination that exists
  and is not an empty directory, so it never overwrites host files.
- ``policy check`` (alias ``diff``) shows the difference between a candidate
  policy and the sandbox's active one, then runs ``openshell-prover check``
  on it when the prover is installed (row 17; optional, never required).
- ``advisor on|off`` sets ``agent_policy_proposals_enabled`` on one sandbox,
  never ``--global`` (row 16).
- ``rules list|approve|reject`` (#37) shows a sandbox's pending access
  requests through ``sandbox.approvals`` and decides one at a time. Approval
  stays manual (row 14): ``approve`` prints the full proposal and needs a yes
  typed at a terminal (there is no ``--yes``); ``reject`` needs ``--reason``
  and a yes or ``--yes``. Nothing here approves all rules or changes the
  approval mode.

tongs-agents never installs OpenShell, starts gateways or edits OpenShell
configuration (row 18); ``doctor`` only reports.
"""

from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from tongs_agents.commands.doctor import Check
from tongs_agents.profiles import (
    DEFAULT_SANDBOX_WORKDIR,
    SANDBOX_NAME_RE,
    Profile,
    ProfileError,
    load_profiles,
)
from tongs_agents.sandbox import approvals, bridge, state
from tongs_agents.sandbox.launcher import READY_PHASE, sandbox_name
from tongs_agents.sandbox.openshell import (
    LAUNCH_LABEL,
    PROFILE_LABEL,
    OpenShell,
    OpenShellError,
    SandboxInfo,
    flavour,
    parse_version,
    run_bounded,
    version_problems,
)

NAME = "sandbox"
HELP = "Manage tongs-agents' OpenShell sandboxes (list, status, pull, policy, rules)"

PROVER = "openshell-prover"
SSH = "ssh"
PROVER_TIMEOUT = 120.0
MAX_POLICY_BYTES = 1024 * 1024
MAX_LOG_LINES = 10000
LANDLOCK_MIN_ABI = 3
STOPPED_PHASES = frozenset({"Stopped", "Suspending", "Suspended", "Completed"})
LOG_SOURCES = ("all", "gateway", "sandbox")
# landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) returns the ABI.
LANDLOCK_SYSCALL = {"x86_64": 444, "aarch64": 444, "arm64": 444}
LANDLOCK_CREATE_RULESET_VERSION = 1
TELEMETRY_NOTE = (
    "Upstream OpenShell gateways send anonymous telemetry by default; midstream "
    "(-rhaiv) images build it out. tongs-agents adds none and never changes gateway config"
)
TELEMETRY_OPT_OUT = (
    "To opt out, set OPENSHELL_TELEMETRY_ENABLED=false in the gateway's own environment"
)
ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class SandboxCommandError(RuntimeError):
    """A refused or failed action; the message is printed and the exit status is 1."""


def clean(text: str) -> str:
    """Gateway and sandbox text with terminal escapes and control characters removed."""
    return CONTROL_RE.sub("", ESCAPE_RE.sub("", text or ""))


def _sandbox_name_arg(value: str) -> str:
    if not SANDBOX_NAME_RE.match(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a sandbox name (lowercase letters, digits and dashes)"
        )
    return value


def _lines_arg(value: str) -> int:
    try:
        lines = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number") from None
    if not 1 <= lines <= MAX_LOG_LINES:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_LOG_LINES}")
    return lines


def _chunk_arg(value: str) -> str:
    try:
        return approvals.check_chunk_id(value)
    except approvals.ApprovalError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def add_arguments(parser):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-g", "--gateway", default=argparse.SUPPRESS,
        help="OpenShell gateway (default: the profile's, else openshell's default)",
    )
    parser.add_argument(
        "-g", "--gateway", default=None,
        help="OpenShell gateway (default: the profile's, else openshell's default)",
    )
    actions = parser.add_subparsers(dest="sandbox_action", metavar="ACTION")

    def action(name, help_text, **kwargs):
        return actions.add_parser(name, help=help_text, parents=[common], **kwargs)

    def named(sub, nargs=None):
        sub.add_argument("name", type=_sandbox_name_arg, nargs=nargs, metavar="NAME",
                         help="Sandbox name")

    listing = action("list", "List labelled sandboxes (default)")
    listing.add_argument("--json", action="store_true", help="Print JSON")

    status = action("status", "Show one labelled sandbox")
    named(status)
    status.add_argument("--json", action="store_true", help="Print JSON")

    named(action("start", "Start a labelled sandbox (no-op when Ready)"))
    stop = action("stop", "Stop a labelled sandbox (no-op when stopped)")
    named(stop)
    stop.add_argument("--force", action="store_true",
                      help="Stop even while an agent pane runs in it")

    delete = action("delete", "Delete labelled sandboxes and their files (no-op when absent)")
    named(delete, nargs="+")
    delete.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    delete.add_argument("--force", action="store_true",
                        help="Delete even while an agent pane runs in it")

    prune = action("prune", "Delete labelled sandboxes no configured profile uses")
    prune.add_argument("--all", action="store_true",
                       help="Also delete sandboxes of configured profiles (never in-use ones)")
    prune.add_argument("--yes", action="store_true",
                       help="Delete without asking; without it and off a terminal, only list")

    pull = action("pull", "Download files from a sandbox's working directory")
    named(pull)
    pull.add_argument("dest", metavar="DEST",
                      help="Local destination; must not exist or be an empty directory")
    pull.add_argument("--path", default=".",
                      help="Path relative to the sandbox working directory (default: all)")
    pull.add_argument("--workdir", default=None,
                      help="The sandbox working directory (default: the profile's)")

    logs = action("logs", "Print recent sandbox logs (never follows)")
    named(logs)
    logs.add_argument("-n", dest="lines", type=_lines_arg, default=200,
                      help="Number of lines (default 200)")
    logs.add_argument("--since", default=None, help="Only logs newer than this (5m, 1h)")
    logs.add_argument("--source", choices=LOG_SOURCES, default=None)

    policy = action("policy", "Show or check a sandbox policy")
    policy_actions = policy.add_subparsers(dest="policy_action", metavar="ACTION")
    show = policy_actions.add_parser("show", help="Print the sandbox's policy",
                                     parents=[common])
    named(show)
    show.add_argument("--full", action="store_true",
                      help="Include provider-composed entries")
    for parent, name in ((policy_actions, "check"), (actions, "diff")):
        check = parent.add_parser(
            name, parents=[common],
            help="Diff a candidate policy against the active one and run the prover",
        )
        named(check, nargs="?")
        check.add_argument("--policy", type=Path, default=None,
                           help="Candidate policy file (default: the profile's sandbox.policy)")
        check.add_argument("--boundary", type=Path, default=None,
                           help="Compare against this file instead of the active policy")
        check.set_defaults(sandbox_action="policy", policy_action="check")

    advisor = action("advisor", "Turn the policy advisor on or off for one sandbox")
    advisor.add_argument("state", choices=("on", "off"))
    named(advisor)

    action("sync", "Import hook events from sandboxed agents once (headless use)")

    rules = action("rules", "Review and decide a sandbox's pending access requests")
    rule_actions = rules.add_subparsers(dest="rules_action", metavar="ACTION")
    shown = rule_actions.add_parser("list", parents=[common],
                                    help="Print pending (or --status) network rules")
    named(shown)
    shown.add_argument("--status", choices=approvals.STATUSES, default=approvals.PENDING)
    decide = rule_actions.add_parser(
        "approve", parents=[common],
        help="Approve one pending rule after showing it (asks at a terminal)",
    )
    named(decide)
    decide.add_argument("chunk", type=_chunk_arg, metavar="CHUNK", help="Chunk id")
    refuse = rule_actions.add_parser("reject", parents=[common],
                                     help="Reject one pending rule with a reason")
    named(refuse)
    refuse.add_argument("chunk", type=_chunk_arg, metavar="CHUNK", help="Chunk id")
    refuse.add_argument("--reason", required=True, help="Why (sent to OpenShell)")
    refuse.add_argument("--yes", action="store_true", help="Do not ask for confirmation")


# Resolution


def _strict_sandboxed_profiles() -> dict[str, Profile]:
    """Sandboxed profiles; an invalid config raises instead of reading as none.

    ``prune`` relies on this: treating a broken config as "no profiles" would
    make every labelled sandbox look orphaned and delete its files.
    """
    try:
        profiles = load_profiles()
    except ProfileError as error:
        raise SandboxCommandError(f"cannot read profiles: {error}") from None
    return {name: profile for name, profile in profiles.items() if profile.sandbox is not None}


def _sandboxed_profiles() -> dict[str, Profile]:
    """Sandboxed profiles for read-only views; an invalid config yields none."""
    try:
        return _strict_sandboxed_profiles()
    except SandboxCommandError:
        return {}


def profile_for(name: str, profiles: dict[str, Profile] | None = None) -> Profile | None:
    """The configured sandboxed profile whose sandbox is ``name``."""
    profiles = _sandboxed_profiles() if profiles is None else profiles
    return next((profile for profile in profiles.values() if sandbox_name(profile) == name),
                None)


def _records_for(name: str) -> list[state.SandboxLaunch]:
    return [record for record in state.list_records() if record.sandbox == name]


def gateway_for(name: str, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    profile = profile_for(name)
    if profile is not None:
        return profile.sandbox.gateway
    records = _records_for(name)
    return records[-1].gateway if records else None


def workdir_for(name: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    profile = profile_for(name)
    if profile is not None:
        return profile.sandbox.workdir
    records = _records_for(name)
    return records[-1].workdir if records else DEFAULT_SANDBOX_WORKDIR


def in_use(name: str) -> list[str]:
    """Launch ids whose live agent pane runs in the sandbox ``name``."""
    return [record.launch_id for record in _records_for(name)
            if bridge.claimed_process(record.launch_id) is not None]


def _client(gateway: str | None) -> OpenShell:
    try:
        return OpenShell(gateway=gateway)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None


def owned(client: OpenShell, name: str) -> SandboxInfo | None:
    """The sandbox ``name``, None when absent; refuses one without a tongs-agents label."""
    try:
        info = client.get(name)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    if info is not None and not info.owned:
        raise SandboxCommandError(
            f"sandbox {name} carries no {PROFILE_LABEL} or {LAUNCH_LABEL} label; "
            "tongs-agents does not manage it"
        )
    return info


def _existing(client: OpenShell, name: str) -> SandboxInfo:
    info = owned(client, name)
    if info is None:
        raise SandboxCommandError(f"no sandbox named {name}")
    return info


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _gateways(explicit: str | None, profiles: dict[str, Profile]) -> list[str | None]:
    if explicit:
        return [explicit]
    gateways: list[str | None] = [None]
    for profile in profiles.values():
        if profile.sandbox.gateway not in gateways:
            gateways.append(profile.sandbox.gateway)
    return gateways


def _listed(explicit: str | None,
            profiles: dict[str, Profile]) -> tuple[list[tuple[str | None, SandboxInfo]], list[str]]:
    """Labelled sandboxes on every relevant gateway, and per-gateway errors."""
    found: list[tuple[str | None, SandboxInfo]] = []
    errors = []
    seen = set()
    for gateway in _gateways(explicit, profiles):
        try:
            sandboxes = _client(gateway).list_sandboxes()
        except OpenShellError as error:
            errors.append(f"gateway {gateway or 'default'}: {error}")
            continue
        for info in sandboxes:
            key = (info.name, info.label(LAUNCH_LABEL), info.created_at)
            if info.owned and key not in seen:
                seen.add(key)
                found.append((gateway, info))
    return found, errors


def describe(gateway: str | None, info: SandboxInfo, launches: list[str]) -> dict:
    return {
        "name": info.name,
        "phase": info.phase,
        "profile": info.label(PROFILE_LABEL),
        "launch": info.label(LAUNCH_LABEL),
        "created_at": info.created_at,
        "policy_version": info.policy_version,
        "gateway": gateway,
        "in_use": launches,
    }


# Actions


def do_list(args) -> int:
    try:
        profiles = _strict_sandboxed_profiles()
    except SandboxCommandError as error:
        print(f"tongs-agents: warning: {clean(str(error))}; listing the default gateway only",
              file=sys.stderr)
        profiles = {}
    found, errors = _listed(getattr(args, "gateway", None), profiles)
    rows = [describe(gateway, info, in_use(info.name)) for gateway, info in found]
    for error in errors:
        print(f"tongs-agents: {clean(error)}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2))
        return 1 if errors else 0
    if not rows:
        print("No tongs-agents sandboxes.")
        return 1 if errors else 0
    table = [("NAME", "PROFILE", "PHASE", "IN USE", "GATEWAY")] + [
        (clean(row["name"]), clean(row["profile"] or "-"), clean(row["phase"]),
         "yes" if row["in_use"] else "no", clean(row["gateway"] or "default"))
        for row in rows
    ]
    widths = [max(len(line[index]) for line in table) for index in range(4)]
    for line in table:
        print("  ".join(value.ljust(width) for value, width in zip(line, widths))
              + "  " + line[4])
    return 1 if errors else 0


def do_status(args) -> int:
    gateway = gateway_for(args.name, args.gateway)
    info = _existing(_client(gateway), args.name)
    row = describe(gateway, info, in_use(args.name))
    profile = profile_for(args.name)
    row["configured_profile"] = profile.name if profile is not None else None
    if args.json:
        print(json.dumps(row, indent=2))
        return 0
    for key, value in row.items():
        if isinstance(value, list):
            value = ", ".join(value) or "no"
        print(f"{key}: {clean('' if value is None else str(value))}")
    return 0


def do_start(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    info = _existing(client, args.name)
    if info.phase == READY_PHASE:
        print(f"Sandbox {args.name} is already running.")
        return 0
    try:
        client.start(args.name)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Started sandbox {args.name}.")
    return 0


def _refuse_in_use(name: str, force: bool, verb: str) -> None:
    launches = in_use(name)
    if launches and not force:
        raise SandboxCommandError(
            f"an agent pane is running in sandbox {name} (launch {', '.join(launches)}); "
            f"end the agent first or pass --force to {verb} it anyway"
        )


def do_stop(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    info = _existing(client, args.name)
    if info.phase in STOPPED_PHASES:
        print(f"Sandbox {args.name} is already stopped ({info.phase}).")
        return 0
    _refuse_in_use(args.name, args.force, "stop")
    try:
        client.stop(args.name)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Stopped sandbox {args.name}.")
    return 0


def delete_one(client: OpenShell, name: str, *, force: bool, confirmed: bool) -> bool:
    """Delete one labelled sandbox; True when it is gone afterwards."""
    info = owned(client, name)
    if info is None:
        print(f"Sandbox {name} is already absent.")
        return True
    _refuse_in_use(name, force, "delete")
    if not confirmed and not _confirm(
        f"Delete sandbox {name} and every file in it? [y/N] "
    ):
        raise SandboxCommandError(f"not deleting {name}: pass --yes to confirm")
    try:
        client.delete(name)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Deleted sandbox {name}.")
    return True


def do_delete(args) -> int:
    failed = 0
    for name in dict.fromkeys(args.name):
        try:
            delete_one(_client(gateway_for(name, args.gateway)), name,
                       force=args.force, confirmed=args.yes)
        except SandboxCommandError as error:
            print(f"tongs-agents: {clean(str(error))}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def _orphan(info: SandboxInfo, profiles: dict[str, Profile]) -> bool:
    profile = profiles.get(info.label(PROFILE_LABEL) or "")
    return profile is None or sandbox_name(profile) != info.name


def do_prune(args) -> int:
    try:
        profiles = _strict_sandboxed_profiles()
    except SandboxCommandError as error:
        raise SandboxCommandError(
            f"{error}; fix the config first, nothing was pruned"
        ) from None
    found, errors = _listed(args.gateway, profiles)
    for error in errors:
        print(f"tongs-agents: {clean(error)}", file=sys.stderr)
    candidates = [
        (gateway, info) for gateway, info in found
        if (args.all or _orphan(info, profiles)) and not in_use(info.name)
    ]
    if not candidates:
        print("Nothing to prune.")
        return 1 if errors else 0
    for gateway, info in candidates:
        print(f"  {clean(info.name)} (profile {clean(info.label(PROFILE_LABEL) or '-')}, "
              f"{clean(info.phase)}, gateway {clean(gateway or 'default')})")
    confirmed = args.yes or _confirm(f"Delete these {len(candidates)} sandbox(es)? [y/N] ")
    if not confirmed:
        print("Not deleted; pass --yes to delete them.")
        return 1 if errors else 0
    failed = 0
    for gateway, info in candidates:
        try:
            delete_one(_client(gateway), info.name, force=False, confirmed=True)
        except SandboxCommandError as error:
            print(f"tongs-agents: {clean(str(error))}", file=sys.stderr)
            failed += 1
    return 1 if failed or errors else 0


def sandbox_path(workdir: str, relative: str) -> str:
    """``relative`` inside ``workdir``; refuses absolute paths and ``..``."""
    if not workdir.startswith("/") or posixpath.normpath(workdir) != workdir or workdir == "/":
        raise SandboxCommandError(f"invalid sandbox working directory {workdir!r}")
    if relative.startswith("/") or ".." in relative.split("/") or "\0" in relative:
        raise SandboxCommandError(
            f"--path {relative!r} must be relative to the working directory, without '..'"
        )
    path = posixpath.normpath(posixpath.join(workdir, relative))
    if path != workdir and not path.startswith(workdir + "/"):
        raise SandboxCommandError(f"--path {relative!r} leaves the working directory")
    return path


def pull_destination(dest: str) -> Path:
    """An absolute destination that is absent or an empty directory, never overwritten."""
    path = Path(dest).expanduser().absolute()
    try:
        info = path.lstat()
    except FileNotFoundError:
        return path
    except OSError as error:
        raise SandboxCommandError(f"cannot use {path}: {error.strerror or error}") from None
    if not stat.S_ISDIR(info.st_mode):
        raise SandboxCommandError(f"refusing to overwrite {path}: it exists")
    try:
        empty = not any(path.iterdir())
    except OSError as error:
        raise SandboxCommandError(f"cannot use {path}: {error.strerror or error}") from None
    if not empty:
        raise SandboxCommandError(
            f"refusing to pull into {path}: it is not empty (choose a new directory)"
        )
    return path


def do_pull(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    _existing(client, args.name)
    source = sandbox_path(workdir_for(args.name, args.workdir), args.path)
    dest = pull_destination(args.dest)
    try:
        client.download(args.name, source, str(dest))
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Pulled {source} from sandbox {args.name} into {dest}.")
    return 0


def do_logs(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    _existing(client, args.name)
    try:
        result = client.logs(args.name, lines=args.lines, since=args.since, source=args.source)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    sys.stdout.write(clean(result.stdout))
    return 0


def _read_policy(path: Path) -> str:
    try:
        resolved = path.expanduser()
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            raise SandboxCommandError(f"{path} is not a regular file")
        if info.st_size > MAX_POLICY_BYTES:
            raise SandboxCommandError(f"{path} is larger than {MAX_POLICY_BYTES} bytes")
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SandboxCommandError(f"cannot read {path}: {error}") from None


def _policy_show(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    _existing(client, args.name)
    try:
        result = client.policy_get(args.name, full=args.full)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    sys.stdout.write(clean(result.stdout))
    return 0


def run_prover(prover: str, candidate: Path, boundary: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    try:
        result = run_bounded([prover, "check", str(candidate), "--boundary", str(boundary)],
                             env=env, timeout=PROVER_TIMEOUT, max_bytes=1024 * 1024)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    return result.returncode, clean(result.stdout + result.stderr)


def _policy_check(args) -> int:
    name = args.name
    candidate_path = args.policy
    if name is not None:
        client = _client(gateway_for(name, args.gateway))
        _existing(client, name)
        if candidate_path is None:
            profile = profile_for(name)
            candidate_path = profile.sandbox.policy if profile is not None else None
    if candidate_path is None:
        raise SandboxCommandError(
            "no candidate policy: pass --policy FILE or set sandbox.policy in the profile"
        )
    candidate_path = Path(candidate_path).expanduser().absolute()
    candidate = _read_policy(candidate_path)
    if args.boundary is not None:
        boundary_label = str(args.boundary)
        boundary = _read_policy(args.boundary)
    elif name is not None:
        boundary_label = f"{name} (active policy)"
        try:
            boundary = client.active_policy(name)
        except OpenShellError as error:
            raise SandboxCommandError(str(error)) from None
    else:
        raise SandboxCommandError("pass a sandbox NAME or --boundary FILE to compare against")
    diff = list(difflib.unified_diff(
        boundary.splitlines(keepends=True), candidate.splitlines(keepends=True),
        fromfile=boundary_label, tofile=str(candidate_path),
    ))
    if diff:
        for line in diff:
            sys.stdout.write(clean(line.rstrip("\n")) + "\n")
    else:
        print(f"No policy changes against {clean(boundary_label)}.")
    prover = shutil.which(PROVER)
    if prover is None:
        print(f"tongs-agents: {PROVER} is not on PATH, so the change was not proved "
              "(optional; see docs/sandbox.md)", file=sys.stderr)
        return 0
    with tempfile.TemporaryDirectory(prefix="tongs-agents-policy-") as temporary:
        if args.boundary is not None:
            boundary_path = Path(args.boundary).expanduser().absolute()
        else:
            boundary_path = Path(temporary) / "active-policy.yaml"
            boundary_path.write_text(boundary, encoding="utf-8")
        code, output = run_prover(prover, candidate_path, boundary_path)
    if output:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")
    if code != 0:
        print(f"tongs-agents: {PROVER} reported findings or failed (exit {code})",
              file=sys.stderr)
        return 1
    print(f"{PROVER}: no findings.")
    return 0


def do_policy(args) -> int:
    if getattr(args, "policy_action", None) == "show":
        return _policy_show(args)
    if getattr(args, "policy_action", None) == "check":
        return _policy_check(args)
    raise SandboxCommandError("choose 'policy show NAME' or 'policy check NAME'")


def do_advisor(args) -> int:
    client = _client(gateway_for(args.name, args.gateway))
    _existing(client, args.name)
    enabled = args.state == "on"
    try:
        client.set_policy_advisor(args.name, enabled)
    except OpenShellError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Policy advisor {args.state} for sandbox {args.name} (this sandbox only).")
    return 0


def do_sync(args) -> int:
    result = bridge.sync_all()
    print(
        f"{result.launches} sandboxed launch(es): {result.imported} event(s) imported, "
        f"{result.rejected} rejected, {result.phase_records} phase update(s)"
    )
    for error in result.errors:
        print(f"  {clean(error)}", file=sys.stderr)
    return 1 if result.errors else 0


def _pending(client: OpenShell, name: str, status: str = approvals.PENDING
             ) -> approvals.RuleListing:
    listing = approvals.list_rules(client, name, status=status)
    if listing.failure is not None:
        raise SandboxCommandError(listing.failure)
    return listing


def _unreadable(listing: approvals.RuleListing) -> SandboxCommandError:
    print(listing.raw, file=sys.stderr)
    return SandboxCommandError(
        f"cannot read openshell's rule output for {listing.sandbox} ({listing.parse_error}); "
        "the raw text is above. Decide with openshell rule approve or reject yourself"
    )


def _print_proposal(proposal: approvals.Proposal) -> None:
    for line in proposal.text.split("\n"):
        print(f"  {line}")


def _reviewed(client: OpenShell, name: str, chunk: str) -> approvals.Proposal:
    listing = _pending(client, name)
    if listing.parse_error is not None:
        raise _unreadable(listing)
    proposal = listing.proposal(chunk)
    if proposal is None:
        raise SandboxCommandError(f"no pending rule {chunk} for sandbox {name}")
    print(f"Sandbox {name}:")
    _print_proposal(proposal)
    return proposal


def do_rules(args) -> int:
    action = getattr(args, "rules_action", None)
    if action is None:
        raise SandboxCommandError("choose rules list, approve or reject")
    client = _client(gateway_for(args.name, args.gateway))
    _existing(client, args.name)
    if action == "list":
        listing = _pending(client, args.name, args.status)
        if listing.parse_error is not None:
            raise _unreadable(listing)
        if not listing.proposals:
            print(f"No {args.status} access requests for sandbox {args.name}.")
            return 0
        for index, proposal in enumerate(listing.proposals):
            if index:
                print()
            _print_proposal(proposal)
        return 0
    if action == "approve":
        if not sys.stdin.isatty():
            raise SandboxCommandError(
                "approving access needs a person at a terminal; run it interactively "
                "or approve from the sidebar"
            )
        proposal = _reviewed(client, args.name, args.chunk)
        if not _confirm(f"Approve this network access for {args.name}? [y/N] "):
            print("Not approved.")
            return 1
        try:
            message = approvals.approve(client, args.name, proposal)
        except approvals.ApprovalError as error:
            raise SandboxCommandError(str(error)) from None
        print(message)
        return 0
    try:
        reason = approvals.check_reason(args.reason)
    except approvals.ApprovalError as error:
        raise SandboxCommandError(str(error)) from None
    proposal = _reviewed(client, args.name, args.chunk)
    if not args.yes and not _confirm(f"Reject it for {args.name}? [y/N] "):
        raise SandboxCommandError("not rejected: pass --yes to confirm off a terminal")
    try:
        message = approvals.reject(client, args.name, proposal, reason)
    except approvals.ApprovalError as error:
        raise SandboxCommandError(str(error)) from None
    print(message)
    return 0


ACTIONS = {
    None: do_list,
    "list": do_list,
    "status": do_status,
    "start": do_start,
    "stop": do_stop,
    "delete": do_delete,
    "prune": do_prune,
    "pull": do_pull,
    "logs": do_logs,
    "policy": do_policy,
    "advisor": do_advisor,
    "sync": do_sync,
    "rules": do_rules,
}


def run(args) -> int:
    handler = ACTIONS[getattr(args, "sandbox_action", None)]
    try:
        return handler(args)
    except SandboxCommandError as error:
        print(f"tongs-agents: {clean(str(error))}", file=sys.stderr)
        return 1


# Doctor (M7-B doctor lines; rows 2, 17, 18, 19)


def landlock_abi() -> int | None:
    """The kernel's Landlock ABI version, or None when unknown or unsupported."""
    number = LANDLOCK_SYSCALL.get(platform.machine())
    if sys.platform != "linux" or number is None:
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        value = libc.syscall(ctypes.c_long(number), None, ctypes.c_size_t(0),
                             ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    except (OSError, AttributeError, TypeError, ValueError):
        return None
    return value if isinstance(value, int) and value > 0 else None


def _version_checks(client: OpenShell) -> list[Check]:
    try:
        text = client.version_text()
    except OpenShellError as error:
        return [Check("openshell", "error", f"openshell --version failed: {error}")]
    version = parse_version(text)
    error, warnings = version_problems(version)
    if error:
        return [Check("openshell", "error", error,
                      "Install a supported OpenShell CLI yourself (see docs/sandbox.md)")]
    shown = ".".join(map(str, version))
    checks = [Check("openshell", "ok",
                    f"openshell {shown} ({flavour(text)}) at {client.executable}")]
    checks += [Check("openshell", "warn", warning, "See docs/sandbox.md for supported versions")
               for warning in warnings]
    return checks


def _gateway_checks(client_for, gateways: Iterable[str | None]) -> list[Check]:
    checks = []
    for gateway in gateways:
        label = gateway or "default"
        try:
            result = client_for(gateway).status()
        except OpenShellError as error:
            result = None
            detail = str(error)
        else:
            detail = result.error_text()
        if result is not None and result.ok:
            checks.append(Check("openshell-gateway", "ok", f"Gateway {label} is reachable"))
            continue
        checks.append(Check(
            "openshell-gateway", "warn",
            f"Gateway {label} is not reachable: {clean(detail)}",
            "Start or log in to the gateway yourself (`openshell status`); "
            "tongs-agents never starts gateways",
        ))
    return checks


def doctor_checks():
    """OpenShell lines; nothing unless a profile is sandboxed or the CLI is installed.

    ``status -o json`` asks the gateway (normally on 127.0.0.1) and runs only
    for configured sandboxed profiles, with a short timeout.
    """
    profiles = _sandboxed_profiles()
    executable = shutil.which("openshell")
    if not profiles and executable is None:
        return []
    if executable is None:
        return [Check(
            "openshell", "error",
            f"openshell CLI not on PATH; sandboxed profiles cannot launch: "
            f"{', '.join(sorted(profiles))}",
            "Install the OpenShell CLI yourself (see docs/sandbox.md); "
            "tongs-agents never installs it",
        )]
    client = OpenShell(executable)
    checks = _version_checks(client)
    if profiles:
        gateways = list(dict.fromkeys(profile.sandbox.gateway for profile in profiles.values()))
        checks += _gateway_checks(lambda gateway: OpenShell(executable, gateway=gateway),
                                  gateways)
    if shutil.which(SSH):
        checks.append(Check("openssh", "ok", "OpenSSH client found (uploads and pull use it)"))
    else:
        checks.append(Check(
            "openssh", "warn", "No ssh client on PATH; openshell uploads and downloads need it",
            "Install the OpenSSH client",
        ))
    if sys.platform == "linux":
        abi = landlock_abi()
        if abi is None:
            checks.append(Check(
                "landlock", "warn", "Cannot read the Landlock ABI; OpenShell sandboxes need 3+",
                "Check that the kernel enables Landlock (lsm=...,landlock)",
            ))
        elif abi < LANDLOCK_MIN_ABI:
            checks.append(Check(
                "landlock", "warn",
                f"Landlock ABI {abi} is older than the {LANDLOCK_MIN_ABI} OpenShell needs",
                "Use a newer kernel for the gateway host",
            ))
        else:
            checks.append(Check("landlock", "ok", f"Landlock ABI {abi}"))
    if shutil.which(PROVER) is None:
        checks.append(Check(
            "openshell-prover", "info",
            f"{PROVER} not on PATH; `tongs-agents sandbox policy check` shows diffs unproved",
        ))
    checks.append(Check("openshell-telemetry", "info", TELEMETRY_NOTE, TELEMETRY_OPT_OUT))
    return checks
