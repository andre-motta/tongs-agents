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

- ``setup`` (#38) checks the host, builds a policy from network presets for
  review, optionally probes the image in a throwaway labelled sandbox (only
  after the policy was shown) and prints the commands left to run and a
  profile snippet. On a terminal it runs the Textual wizard
  (``views.sandbox_wizard``); ``--print`` or no terminal prints instead. It
  never writes the tongs-agents config file; ``--write-policy`` writes the
  reviewed policy to a new file only.

tongs-agents never installs OpenShell, starts gateways or edits OpenShell
configuration (row 18); ``doctor`` and ``setup`` only report.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
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
from tongs_agents.sandbox import setup as sandbox_setup
from tongs_agents.sandbox.launcher import READY_PHASE, sandbox_name
from tongs_agents.sandbox.openshell import (
    LAUNCH_LABEL,
    PROFILE_LABEL,
    OpenShell,
    OpenShellError,
    SandboxInfo,
    run_bounded,
)
from tongs_agents.sandbox.setup import (
    PRESETS,
    SETUP_HARNESSES,
    SetupError,
    landlock_abi,
)
from tongs_agents.views.sandbox_wizard import SandboxWizardApp

NAME = "sandbox"
HELP = "Set up and manage tongs-agents' OpenShell sandboxes (setup, list, pull, policy, rules)"

PROVER = sandbox_setup.PROVER
PROVER_TIMEOUT = 120.0
MAX_POLICY_BYTES = 1024 * 1024
MAX_LOG_LINES = 10000
# Unvalidated (row 21): an upstream main checked for M7-B has no ``sandbox
# start`` or ``stop`` and its SandboxPhase (proto/openshell.proto) has only
# Unknown, Provisioning, Ready, Error and Deleting. These shapes are kept
# pending the maintainer's live run; start and stop report success only for
# a phase listed here (or Ready), never for one they do not know.
STOPPED_PHASES = frozenset({"Stopped", "Suspending", "Suspended", "Completed"})
STARTING_PHASES = frozenset({"Provisioning"})
LOG_SOURCES = ("all", "gateway", "sandbox")
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

    wizard = action(
        "setup",
        "Check the host, review a generated policy, print commands and a profile snippet "
        "(never edits config)",
    )
    wizard.add_argument("--harness", choices=SETUP_HARNESSES, default="claude")
    wizard.add_argument("--folder", type=Path, default=None,
                        help="Project folder to upload (default: the current directory)")
    wizard.add_argument("--profile", dest="profile_name", default=None, metavar="NAME",
                        help="Profile name for the snippet (default: <harness>-boxed)")
    wizard.add_argument("--image", default=None, help="Sandbox image (--from)")
    wizard.add_argument("--network", dest="presets", action="append", default=None,
                        choices=[*PRESETS, "none"], metavar="PRESET",
                        help=f"Network preset, repeatable: {', '.join(PRESETS)} or none "
                             "(default: the harness's model API)")
    wizard.add_argument("--provider", dest="providers", action="append", default=None,
                        metavar="NAME",
                        help="OpenShell provider name, repeatable (default: by preset)")
    wizard.add_argument("--chat", action="store_true",
                        help="Check that the image has tongs-agents for chat")
    wizard.add_argument("--policy-file", type=Path, default=None, metavar="FILE",
                        help="Where the policy goes "
                             "(default: <config dir>/policies/<profile>.yaml)")
    wizard.add_argument("--write-policy", action="store_true",
                        help="Write the reviewed policy to the policy file (never overwrites)")
    wizard.add_argument("--probe", action="store_true",
                        help="Probe the image in a throwaway sandbox after showing the policy")
    wizard.add_argument("--yes", action="store_true",
                        help="Probe without asking (off a terminal)")
    wizard.add_argument("--print", dest="print_only", action="store_true",
                        help="Print instead of running the interactive wizard")

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
    phase = _phase_after(client, args.name, "start")
    if phase == READY_PHASE:
        print(f"Started sandbox {args.name}.")
    elif phase in STARTING_PHASES:
        print(f"Starting sandbox {args.name} ({phase}); "
              f"check with tongs-agents sandbox status {args.name}.")
    else:
        raise SandboxCommandError(_unexpected_phase(args.name, "start", phase))
    return 0


def _phase_after(client: OpenShell, name: str, verb: str) -> str | None:
    """The phase once ``openshell sandbox <verb>`` returned; None when the sandbox is gone."""
    try:
        info = owned(client, name)
    except SandboxCommandError as error:
        raise SandboxCommandError(f"openshell sandbox {verb} {name} returned, but reading "
                                  f"the sandbox again failed: {error}") from None
    return None if info is None else info.phase


def _unexpected_phase(name: str, verb: str, phase: str | None) -> str:
    shown = "it no longer exists" if phase is None else f"it reports phase {clean(phase)!r}"
    return (f"openshell sandbox {verb} {name} returned, but {shown}; not reporting "
            f"success (check with tongs-agents sandbox status {name})")


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
    phase = _phase_after(client, args.name, "stop")
    if phase not in STOPPED_PHASES:
        raise SandboxCommandError(_unexpected_phase(args.name, "stop", phase))
    print(f"Stopped sandbox {args.name} ({phase}).")
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


def _setup_plan(args) -> sandbox_setup.Plan:
    """The plan from the setup options, not validated yet."""
    plan = sandbox_setup.default_plan(args.harness, args.folder, args.profile_name)
    presets = plan.presets
    if args.presets is not None:
        presets = tuple(key for key in args.presets if key != "none")
    providers = (tuple(args.providers) if args.providers is not None
                 else sandbox_setup.suggested_providers(args.harness, presets))
    return sandbox_setup.replace(
        plan, presets=presets, providers=providers, image=args.image,
        gateway=args.gateway, chat=args.chat, policy_path=args.policy_file,
    )


def _inspect(gateway: str | None) -> sandbox_setup.HostReport:
    return sandbox_setup.inspect_host(gateway, landlock=landlock_abi)


def _print_checks(report: sandbox_setup.HostReport) -> None:
    print("Host:")
    for check in report.checks:
        print(f"  {check.level.upper():5} {check.name}: {clean(check.message)}")
        if check.remediation and check.level != "ok":
            print(f"        {clean(check.remediation)}")
    if report.providers is not None:
        print(f"  Providers on the gateway: {', '.join(sorted(report.providers)) or 'none'}")
    print()


def _setup_print(args) -> int:
    try:
        plan = _setup_plan(args).check()
        reviewed = sandbox_setup.review(plan)
    except SetupError as error:
        raise SandboxCommandError(str(error)) from None
    report = _inspect(plan.gateway)
    _print_checks(report)
    print("Generated policy (review it before any sandbox starts):")
    print()
    sys.stdout.write(reviewed.text)
    print()
    written = False
    if args.write_policy:
        try:
            path = sandbox_setup.write_policy(reviewed, plan.resolved_policy_path())
        except SetupError as error:
            raise SandboxCommandError(str(error)) from None
        print(f"Saved the policy as {path}.")
        print()
        written = True
    probed_ok = True
    if args.probe:
        probed_ok = _setup_probe(plan, reviewed, report, args.yes)
    sys.stdout.write(clean(sandbox_setup.finish(plan, report, reviewed,
                                                policy_written=written).text()))
    return 0 if report.ready and probed_ok else 1


def _setup_probe(plan, reviewed, report, confirmed: bool) -> bool:
    if not report.ready:
        print("Not probing the image: the OpenShell CLI or gateway is not ready.")
        print()
        return False
    if not confirmed and not _confirm(
        "Start a throwaway sandbox with the policy above to probe the image? [y/N] "
    ):
        print("Not probing the image; pass --yes to probe off a terminal.")
        print()
        return False
    try:
        result = sandbox_setup.probe_image(plan, reviewed, out=sys.stderr)
    except SetupError as error:
        raise SandboxCommandError(str(error)) from None
    print(f"Image probe (sandbox {result.sandbox}, deleted afterwards):")
    for line in result.lines():
        print(f"  {clean(line)}")
    print()
    return result.ok


def do_setup(args) -> int:
    interactive = (not (args.print_only or args.probe or args.write_policy)
                   and sys.stdin.isatty() and sys.stdout.isatty())
    if not interactive:
        return _setup_print(args)
    text = SandboxWizardApp(_setup_plan(args), inspect=_inspect).run()
    if not text:
        print("Sandbox setup cancelled; nothing was changed.")
        return 1
    sys.stdout.write(clean(text))
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
    "setup": do_setup,
}


def run(args) -> int:
    handler = ACTIONS[getattr(args, "sandbox_action", None)]
    try:
        return handler(args)
    except SandboxCommandError as error:
        print(f"tongs-agents: {clean(str(error))}", file=sys.stderr)
        return 1


# Doctor (M7-B doctor lines; rows 2, 17, 18, 19; shared with the setup wizard)


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
    checks, _text = sandbox_setup.version_checks(OpenShell(executable),
                                                 required=bool(profiles))
    if profiles and not any(check.level == "error" for check in checks):
        gateways = list(dict.fromkeys(profile.sandbox.gateway for profile in profiles.values()))
        checks += [sandbox_setup.gateway_check(OpenShell(executable, gateway=gateway),
                                               gateway or "default")
                   for gateway in gateways]
    checks.append(sandbox_setup.ssh_check())
    if sys.platform == "linux":
        checks.append(sandbox_setup.landlock_check(landlock_abi()))
    prover = sandbox_setup.prover_check()
    if prover is not None:
        checks.append(prover)
    checks.append(sandbox_setup.telemetry_check())
    return checks
