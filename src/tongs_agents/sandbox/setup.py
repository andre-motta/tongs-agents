"""The sandbox setup wizard's logic (#38; docs/work/openshell-sandbox.md, M7-D).

Shared by ``tongs-agents sandbox setup`` and the Textual wizard in
``views.sandbox_wizard``. In order the wizard:

1. inspects the host (``inspect_host``): the ``openshell`` CLI, its version
   and flavour, the gateway (``status -o json``), runtime hints, Landlock,
   OpenSSH and the telemetry disclosure (rows 2, 18, 19), and when the
   gateway answers, the providers it already has (``provider list -o json``);
2. takes a ``Plan``: a profile name, a host folder, a harness, network
   presets, provider names (credentials stay OpenShell providers, row 13),
   an optional image and gateway. The folder is uploaded into the sandbox,
   so ``/``, the home folder and any folder holding it are refused
   (``unsafe_folder``), and the current directory is never the default
   when it is one of them;
3. renders the plan's policy (``policy_text``) for review. ``review`` turns
   it into a ``ReviewedPolicy``, which is the only thing ``probe_image`` and
   ``write_policy`` accept, so no sandbox starts before the policy was shown;
4. on request, probes the image in a throwaway labelled sandbox through
   ``sandbox exec`` for ``sh``, ``tar`` and the other tools tongs-agents uses
   inside, the harness binary and, for chat, ``tongs-agents``; then deletes it;
5. prints the commands left to run and a profile snippet (``finish``).

Nothing here installs packages, starts or configures a gateway, edits
OpenShell config or writes the tongs-agents config file (row 18). The one
file it may write is the reviewed policy, at a path the user names, never
over an existing file.
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import platform
import secrets
import shutil
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from tongs_agents import paths
from tongs_agents.commands.doctor import Check
from tongs_agents.profiles import (
    PROFILE_NAME_RE,
    SANDBOX_IMAGE_RE,
    SANDBOX_TOKEN_RE,
    ProfileError,
    parse_profile,
    profiles_path,
)
from tongs_agents.sandbox.approvals import clean
from tongs_agents.sandbox.launcher import READY_TIMEOUT, SandboxLaunchError, wait_ready
from tongs_agents.sandbox.openshell import (
    LAUNCH_LABEL,
    OpenShell,
    OpenShellError,
    flavour,
    parse_version,
    version_problems,
)

SETUP_HARNESSES: tuple[str, ...] = ("claude", "codex", "pi")
SSH = "ssh"
PROVER = "openshell-prover"
RUNTIMES = ("podman", "docker")
KVM_DEVICE = "/dev/kvm"
LANDLOCK_MIN_ABI = 3
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
INSTALL_HINT = (
    "Install the OpenShell CLI and gateway yourself: "
    "https://github.com/NVIDIA/OpenShell/releases (tongs-agents never installs it)"
)
PROVIDER_LIST_TIMEOUT = 10.0
MAX_PROVIDER_OUTPUT = 256 * 1024
PROBE_PREFIX = "tongs-setup-"
PROBE_TIMEOUT = 60.0
MAX_PROBE_OUTPUT = 64 * 1024
MAX_PROBE_PATH = 200
# Run as ``sh -c PROBE_SCRIPT tongs-agents-probe <name>...``; names are arguments, never code.
PROBE_SCRIPT = (
    'for name do\n'
    '  if found=$(command -v "$name" 2>/dev/null); then\n'
    '    printf "found %s %s\\n" "$name" "$found"\n'
    '  else\n'
    '    printf "missing %s\\n" "$name"\n'
    '  fi\n'
    'done\n'
)
# The pane's ``sandbox exec`` uses bash as a login shell; spool hooks use cat
# and mv; the bridge pulls with head and base64; uploads need tar.
IMAGE_TOOLS: tuple[str, ...] = ("sh", "bash", "tar", "cat", "mv", "head", "base64")
CHAT_TOOL = "tongs-agents"
# The upload skips only what .gitignore lists; the wizard warns about these.
SECRET_NAMES: tuple[str, ...] = (".ssh", ".aws", ".gnupg", ".kube", ".docker", ".env",
                                 ".netrc", ".git-credentials")


class SetupError(ValueError):
    """A plan value is invalid, or a setup action was refused; the message says which."""


# Host


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


def version_checks(client: OpenShell, *, required: bool = True) -> tuple[list[Check], str]:
    """The ``openshell`` lines for ``client`` and its raw ``--version`` text.

    An unusable CLI is an error when something needs it (``required``), else
    a warning: an openshell on PATH that no sandboxed profile uses breaks nothing.
    """
    level = "error" if required else "warn"
    try:
        text = client.version_text()
    except OpenShellError as error:
        return [Check("openshell", level, f"openshell --version failed: {error}")], ""
    version = parse_version(text)
    error, warnings = version_problems(version)
    if error:
        return [Check("openshell", level, error,
                      "Install a supported OpenShell CLI yourself (see docs/sandbox.md)")], text
    shown = ".".join(map(str, version))
    checks = [Check("openshell", "ok",
                    f"openshell {shown} ({flavour(text)}) at {client.executable}")]
    checks += [Check("openshell", "warn", warning, "See docs/sandbox.md for supported versions")
               for warning in warnings]
    return checks, text


def gateway_check(client: OpenShell, label: str) -> Check:
    """``status -o json`` for one gateway; a warning when it does not answer."""
    try:
        result = client.status()
    except OpenShellError as error:
        result = None
        detail = str(error)
    else:
        detail = result.error_text()
    if result is not None and result.ok:
        return Check("openshell-gateway", "ok", f"Gateway {label} is reachable")
    return Check(
        "openshell-gateway", "warn",
        f"Gateway {label} is not reachable: {clean(detail)}",
        "Start or log in to the gateway yourself (`openshell status`); "
        "tongs-agents never starts gateways",
    )


def ssh_check(which: Callable[[str], str | None] = shutil.which) -> Check:
    if which(SSH):
        return Check("openssh", "ok", "OpenSSH client found (uploads and pull use it)")
    return Check("openssh", "warn",
                 "No ssh client on PATH; openshell uploads and downloads need it",
                 "Install the OpenSSH client")


def landlock_check(abi: int | None) -> Check:
    if abi is None:
        return Check("landlock", "warn",
                     "Cannot read the Landlock ABI; OpenShell sandboxes need 3+",
                     "Check that the kernel enables Landlock (lsm=...,landlock)")
    if abi < LANDLOCK_MIN_ABI:
        return Check("landlock", "warn",
                     f"Landlock ABI {abi} is older than the {LANDLOCK_MIN_ABI} OpenShell needs",
                     "Use a newer kernel for the gateway host")
    return Check("landlock", "ok", f"Landlock ABI {abi}")


def prover_check(which: Callable[[str], str | None] = shutil.which) -> Check | None:
    if which(PROVER) is not None:
        return None
    return Check("openshell-prover", "info",
                 f"{PROVER} not on PATH; `tongs-agents sandbox policy check` shows diffs unproved")


def telemetry_check() -> Check:
    return Check("openshell-telemetry", "info", TELEMETRY_NOTE, TELEMETRY_OPT_OUT)


def runtime_check(which: Callable[[str], str | None] = shutil.which,
                  kvm: Callable[[], bool] | None = None) -> Check:
    """Which local gateway drivers this host could run; a hint only, never required."""
    found = [name for name in RUNTIMES if which(name)]
    has_kvm = (kvm or (lambda: sys.platform == "linux" and os.path.exists(KVM_DEVICE)))()
    if has_kvm:
        found.append("KVM")
    if found:
        return Check("openshell-runtime", "info",
                     f"Local gateway drivers this host could use: {', '.join(found)}")
    return Check(
        "openshell-runtime", "info",
        "No Podman, Docker or KVM found; a local gateway needs one (a remote gateway does not)",
    )


def parse_providers(text: str) -> dict[str, str] | None:
    """Provider name to type from ``provider list -o json``; None when unrecognized."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    providers = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        name, kind = item.get("name"), item.get("type")
        if isinstance(name, str) and SANDBOX_TOKEN_RE.match(name):
            providers[name] = clean(kind) if isinstance(kind, str) else ""
    return providers


def list_providers(client: OpenShell) -> tuple[dict[str, str] | None, str | None]:
    """``(providers, error)``: the gateway's providers by name, keys only, never values."""
    try:
        result = client.run("provider", "list", "-o", "json", timeout=PROVIDER_LIST_TIMEOUT,
                            max_bytes=MAX_PROVIDER_OUTPUT)
    except OpenShellError as error:
        return None, str(error)
    if not result.ok:
        return None, f"openshell provider list failed: {clean(result.error_text())}"
    providers = parse_providers(result.stdout)
    if providers is None:
        return None, "openshell provider list: unrecognized JSON output"
    return providers, None


@dataclass(frozen=True)
class HostReport:
    """What the wizard learnt about the host; ``ready`` means a sandbox can be created."""

    checks: tuple[Check, ...]
    executable: str | None = None
    version_text: str = ""
    gateway: str | None = None
    gateway_ok: bool = False
    providers: Mapping[str, str] | None = None
    provider_error: str | None = None

    @property
    def cli_ok(self) -> bool:
        return self.executable is not None and not any(
            check.name == "openshell" and check.level == "error" for check in self.checks
        )

    @property
    def ready(self) -> bool:
        return self.cli_ok and self.gateway_ok

    @property
    def flavour(self) -> str | None:
        return flavour(self.version_text) if self.version_text else None


def inspect_host(gateway: str | None = None, *, executable: str | None = None,
                 which: Callable[[str], str | None] = shutil.which,
                 landlock: Callable[[], int | None] = landlock_abi,
                 kvm: Callable[[], bool] | None = None) -> HostReport:
    """Read-only host checks; the gateway is only asked when the CLI version passes."""
    found = executable or which("openshell")
    label = gateway or "default"
    checks: list[Check] = []
    if not found:
        checks.append(Check("openshell", "error", "openshell CLI not found on PATH",
                            INSTALL_HINT))
        report = {"executable": None}
    else:
        client = OpenShell(found, gateway=gateway)
        version, text = version_checks(client)
        checks += version
        report = {"executable": client.executable, "version_text": text}
        if not any(check.level == "error" for check in version):
            status = gateway_check(client, label)
            checks.append(status)
            report["gateway_ok"] = status.level == "ok"
            if report["gateway_ok"]:
                report["providers"], report["provider_error"] = list_providers(client)
    checks.append(runtime_check(which, kvm))
    if sys.platform == "linux":
        checks.append(landlock_check(landlock()))
    checks.append(ssh_check(which))
    prover = prover_check(which)
    if prover is not None:
        checks.append(prover)
    checks.append(telemetry_check())
    return HostReport(checks=tuple(checks), gateway=gateway, **report)


def host_commands(report: HostReport) -> list[str]:
    """What to run yourself before a sandbox can start; tongs-agents runs none of it."""
    steps = []
    if report.executable is None or not report.cli_ok:
        steps.append(INSTALL_HINT)
        steps.append("openshell --version")
        return steps
    if not report.gateway_ok:
        gateway = f" -g {report.gateway}" if report.gateway else ""
        steps.append(f"openshell{gateway} status")
        if sys.platform == "linux":
            steps.append("systemctl --user status openshell-gateway")
            steps.append("systemctl --user start openshell-gateway")
        elif sys.platform == "darwin":
            steps.append("brew services restart openshell")
    return steps


# Network presets (OpenShell policy schema, version 1)


@dataclass(frozen=True)
class Preset:
    """A named network policy entry and the OpenShell provider its credentials come from.

    ``provider`` is the suggested provider name and ``provider_type`` a type
    upstream ``normalize_provider_type`` accepts. ``credential`` names the
    environment variable ``provider create --credential`` reads for a type
    whose ``--from-existing`` discovery finds nothing (``generic``).
    """

    key: str
    title: str
    endpoints: tuple[Mapping, ...]
    binaries: tuple[str, ...]
    provider: str | None = None
    provider_type: str | None = None
    credential: str | None = None


def _rest(host: str, access: str) -> dict:
    return {"host": host, "port": 443, "protocol": "rest", "enforcement": "enforce",
            "access": access}


def _l4(host: str) -> dict:
    return {"host": host, "port": 443}


NODE_BINARIES = ("/usr/bin/node", "/usr/local/bin/node")

# anthropic, openai and github take their endpoints (a subset) and binaries
# from the upstream provider profiles claude-code, codex and github
# (providers/*.yaml in NVIDIA/OpenShell); pypi takes its first two hosts from
# upstream's pypi profile but uses its own binary paths. openrouter and npm
# have no upstream profile: their hosts and binaries are tongs-agents' own.
# Provider types are ones upstream's CLI accepts (claude-code, codex, github,
# generic); OpenRouter has no provider type upstream, so it is a generic
# provider holding OPENROUTER_API_KEY, the variable Pi reads.
PRESETS: dict[str, Preset] = {preset.key: preset for preset in (
    Preset("anthropic", "Anthropic API (Claude Code)",
           (_rest("api.anthropic.com", "read-write"),),
           ("/usr/bin/claude", "/usr/local/bin/claude"), "claude-code", "claude-code"),
    Preset("openai", "OpenAI API (Codex)",
           (_rest("api.openai.com", "read-write"), _rest("auth.openai.com", "read-write"),
            _rest("chatgpt.com", "read-write")),
           ("/usr/bin/codex", "/usr/local/bin/codex", "/usr/lib/node_modules/@openai/**"),
           "codex", "codex"),
    Preset("openrouter", "OpenRouter API (Pi models)",
           (_rest("openrouter.ai", "read-write"),), NODE_BINARIES,
           "openrouter", "generic", "OPENROUTER_API_KEY"),
    Preset("github", "GitHub: API read-only, git clone and fetch (no push)",
           (_rest("api.github.com", "read-only"),
            {"host": "github.com", "port": 443, "protocol": "rest", "enforcement": "enforce",
             "rules": ({"allow": {"method": "GET", "path": "**"}},
                       {"allow": {"method": "HEAD", "path": "**"}},
                       {"allow": {"method": "OPTIONS", "path": "**"}},
                       {"allow": {"method": "POST", "path": "/**/git-upload-pack"}})}),
           ("/usr/bin/gh", "/usr/local/bin/gh", "/usr/bin/git", "/usr/local/bin/git"),
           "github", "github"),
    Preset("pypi", "PyPI packages (pip, uv)",
           (_l4("pypi.org"), _l4("files.pythonhosted.org")),
           ("/usr/bin/python3*", "/usr/local/bin/python3*", "/usr/bin/pip*",
            "/usr/local/bin/pip*", "/usr/local/bin/uv", "/sandbox/**/.venv/bin/*")),
    Preset("npm", "npm registry", (_l4("registry.npmjs.org"),),
           NODE_BINARIES + ("/usr/bin/npm", "/usr/local/bin/npm")),
)}
HARNESS_PRESET = {"claude": "anthropic", "codex": "openai", "pi": "openrouter"}
BASE_POLICY = {
    "version": 1,
    "filesystem_policy": {
        "include_workdir": True,
        "read_only": ("/usr", "/lib", "/proc", "/dev/urandom", "/app", "/etc", "/var/log"),
        "read_write": ("/sandbox", "/tmp", "/dev/null"),
    },
    "landlock": {"compatibility": "best_effort"},
    "process": {"run_as_user": "sandbox", "run_as_group": "sandbox"},
}


def _scalar(value) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def _yaml(value, indent: int = 0) -> list[str]:
    """Block YAML for nested mappings and sequences; strings are double-quoted (JSON)."""
    pad = " " * indent
    lines = []
    if isinstance(value, Mapping):
        if not value:
            return [pad + "{}"]
        for key, item in value.items():
            if isinstance(item, (Mapping, list, tuple)) and item:
                lines.append(f"{pad}{key}:")
                lines += _yaml(item, indent + 2)
            elif isinstance(item, Mapping):
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(item, (list, tuple)):
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return lines
    for item in value:
        if isinstance(item, Mapping) and item:
            nested = _yaml(item, indent + 2)
            lines.append(pad + "- " + nested[0][indent + 2:])
            lines += nested[1:]
        else:
            lines.append(f"{pad}- {_scalar(item)}")
    return lines


# Plan


def _home() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, KeyError):
        return None


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def unsafe_folder(folder: Path, home: Path | None = None) -> str | None:
    """Why ``folder`` must not be uploaded into a sandbox, or None when it may be.

    The upload skips only .gitignore'd files, and a home folder is rarely a git
    repository, so uploading ``/``, the home folder or a folder holding it would
    copy SSH keys and harness credentials where the agent can read them.
    """
    home = _home() if home is None else home
    if home is not None and not home.is_absolute():
        home = None
    folders = {Path(folder), _resolved(Path(folder))}
    homes = {home, _resolved(home)} if home is not None else set()
    for candidate in folders:
        if candidate.anchor and candidate == Path(candidate.anchor):
            return f"{folder} is the filesystem root"
        for place in homes:
            if candidate == place:
                return f"{folder} is your home folder"
            if candidate in place.parents:
                return f"{folder} holds your home folder"
    return None


def secret_paths(folder: Path) -> tuple[str, ...]:
    """Well-known credential files or folders directly inside ``folder``."""
    found = []
    for name in SECRET_NAMES:
        try:
            if (Path(folder) / name).exists():
                found.append(name)
        except OSError:
            continue
    return tuple(found)


@dataclass(frozen=True)
class Plan:
    """The wizard's answers; ``check`` validates them."""

    profile: str
    harness: str
    folder: Path | None
    presets: tuple[str, ...] = ()
    providers: tuple[str, ...] = ()
    image: str | None = None
    gateway: str | None = None
    chat: bool = False
    policy_path: Path | None = None

    @property
    def command(self) -> str:
        return self.harness

    def resolved_policy_path(self) -> Path:
        if self.policy_path is not None:
            return self.policy_path
        return paths.config_dir() / "policies" / f"{self.profile}.yaml"

    def check(self) -> Plan:
        """This plan, or SetupError naming the first invalid answer."""
        if not isinstance(self.profile, str) or not PROFILE_NAME_RE.match(self.profile):
            raise SetupError(
                "profile name: lowercase letters, digits, '-' and '_' (at most 32)"
            )
        if self.harness not in SETUP_HARNESSES:
            raise SetupError(f"harness: one of {', '.join(SETUP_HARNESSES)}")
        if self.folder is None or not str(self.folder).strip():
            raise SetupError("folder: name the project folder to upload into the sandbox")
        folder = Path(self.folder).expanduser()
        if not folder.is_absolute() or "\0" in str(folder):
            raise SetupError("folder: an absolute path")
        if not folder.is_dir():
            raise SetupError(f"folder: {folder} is not a directory")
        unsafe = unsafe_folder(folder)
        if unsafe is not None:
            raise SetupError(f"folder: {unsafe}; everything in it would be uploaded into "
                             "the sandbox, credentials included. Pick a project folder")
        unknown = [key for key in self.presets if key not in PRESETS]
        if unknown:
            raise SetupError(f"network preset {unknown[0]!r}: one of {', '.join(PRESETS)}")
        for name in self.providers:
            if not SANDBOX_TOKEN_RE.match(name):
                raise SetupError(f"provider {name!r}: letters, digits, '.', '_' and '-'")
        if self.image is not None and not SANDBOX_IMAGE_RE.match(self.image):
            raise SetupError("image: an image or template name")
        if self.gateway is not None and not SANDBOX_TOKEN_RE.match(self.gateway):
            raise SetupError("gateway: a gateway name")
        if self.policy_path is not None:
            policy = Path(self.policy_path).expanduser()
            if not policy.is_absolute() or "\0" in str(policy):
                raise SetupError("policy file: an absolute path")
            if policy.suffix not in (".yaml", ".yml"):
                raise SetupError("policy file: a .yaml or .yml file name")
        return replace(self, folder=folder, presets=tuple(dict.fromkeys(self.presets)),
                       providers=tuple(dict.fromkeys(self.providers)),
                       policy_path=(Path(self.policy_path).expanduser()
                                    if self.policy_path is not None else None))


def suggested_presets(harness: str) -> tuple[str, ...]:
    preset = HARNESS_PRESET.get(harness)
    return (preset,) if preset else ()


def suggested_providers(harness: str, presets: Iterable[str] = ()) -> tuple[str, ...]:
    """Provider names a plan needs, each preset's suggested name."""
    keys = list(suggested_presets(harness)) + [key for key in presets if key in PRESETS]
    return tuple(dict.fromkeys(
        PRESETS[key].provider for key in keys if PRESETS[key].provider
    ))


def default_folder() -> Path | None:
    """The current directory, unless uploading it is refused (``unsafe_folder``)."""
    try:
        cwd = Path.cwd()
    except OSError:
        return None
    return None if unsafe_folder(cwd) is not None else cwd


def default_plan(harness: str = "claude", folder: Path | None = None,
                 profile: str | None = None) -> Plan:
    """The wizard's starting answers; no folder when the current one may not be uploaded."""
    presets = suggested_presets(harness)
    return Plan(
        profile=profile or f"{harness}-boxed",
        harness=harness,
        folder=folder if folder is not None else default_folder(),
        presets=presets,
        providers=suggested_providers(harness, presets),
    )


def policy_document(plan: Plan) -> dict:
    policies = {}
    for key in plan.presets:
        preset = PRESETS[key]
        policies[f"tongs_{key}"] = {
            "name": f"tongs-{key}",
            "endpoints": list(preset.endpoints),
            "binaries": [{"path": path} for path in preset.binaries],
        }
    return {**BASE_POLICY, "network_policies": policies}


def policy_text(plan: Plan) -> str:
    """The plan's OpenShell policy YAML (schema version 1); deterministic."""
    header = [
        f"# OpenShell sandbox policy for the tongs-agents profile {plan.profile}.",
        "# Generated by `tongs-agents sandbox setup`; review it before any sandbox starts.",
        "# Network entries: " + (", ".join(plan.presets) or "none (all egress denied)") + ".",
    ]
    return "\n".join(header + _yaml(policy_document(plan))) + "\n"


@dataclass(frozen=True)
class ReviewedPolicy:
    """A policy text that was shown to the user.

    It still applies to a plan whose policy renders to the same text, so
    changing only the folder, image or providers keeps the review, while
    changing the presets or profile name needs a new one.
    """

    plan: Plan
    text: str

    def matches(self, plan: Plan) -> bool:
        return self.text == policy_text(plan)


def review(plan: Plan) -> ReviewedPolicy:
    """Mark the policy of ``plan`` as shown; callers display ``.text`` first."""
    plan = plan.check()
    return ReviewedPolicy(plan, policy_text(plan))


def _require_reviewed(plan: Plan, reviewed: ReviewedPolicy | None) -> Plan:
    plan = plan.check()
    if reviewed is None or not reviewed.matches(plan):
        raise SetupError("review the generated policy first; it changed or was never shown")
    return plan


# Providers


@dataclass(frozen=True)
class ProviderNeed:
    """One provider a plan names; ``exists`` is None when the gateway was not asked."""

    name: str
    type: str | None
    exists: bool | None
    credential: str | None = None

    def command(self) -> str | None:
        """``openshell provider create`` as upstream's CLI takes it, or None when present.

        ``--from-existing`` lets the CLI discover the key from the caller's
        environment for the type; a ``generic`` provider discovers nothing, so
        it names the variable with ``--credential KEY`` (an environment lookup,
        never a value).
        """
        if self.exists:
            return None
        kind = self.type or "<type>"
        source = f"--credential {self.credential}" if self.credential else "--from-existing"
        return f"openshell provider create --name {self.name} --type {kind} {source}"


def provider_needs(plan: Plan, report: HostReport | None = None) -> list[ProviderNeed]:
    suggested = {
        PRESETS[key].provider: PRESETS[key]
        for key in (*suggested_presets(plan.harness), *plan.presets)
        if key in PRESETS and PRESETS[key].provider
    }
    known = report.providers if report is not None else None
    needs = []
    for name in plan.providers:
        exists = None if known is None else name in known
        preset = suggested.get(name)
        kind = (known or {}).get(name) or (preset.provider_type if preset else None)
        credential = None
        if preset is not None and kind == preset.provider_type:
            credential = preset.credential
        needs.append(ProviderNeed(name, kind or None, exists, credential))
    return needs


# Output


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def profile_snippet(plan: Plan) -> str:
    """The ``[profiles.<name>]`` TOML to add yourself; validated like a loaded profile."""
    plan = plan.check()
    lines = [
        f"[profiles.{plan.profile}]",
        f"command = [{_toml_string(plan.command)}]",
        f"cwd = {_toml_string(str(plan.folder))}",
        "",
        f"[profiles.{plan.profile}.sandbox]",
    ]
    if plan.image:
        lines.append(f"image = {_toml_string(plan.image)}")
    providers = ", ".join(_toml_string(name) for name in plan.providers)
    lines.append(f"providers = [{providers}]")
    lines.append(f"policy = {_toml_string(str(plan.resolved_policy_path()))}")
    if plan.gateway:
        lines.append(f"gateway = {_toml_string(plan.gateway)}")
    text = "\n".join(lines) + "\n"
    try:
        table = tomllib.loads(text)["profiles"][plan.profile]
        parse_profile(plan.profile, table)
    except (tomllib.TOMLDecodeError, KeyError, ProfileError) as error:
        raise SetupError(f"cannot build a valid profile: {error}") from None
    return text


def spawn_command(plan: Plan) -> str:
    return f"tongs-agents workspace --profile {plan.profile}"


@dataclass(frozen=True)
class Finish:
    """Everything the wizard prints at the end."""

    host_steps: tuple[str, ...]
    provider_steps: tuple[str, ...]
    policy_path: Path
    policy_written: bool
    snippet: str
    config_path: Path
    spawn: str
    notes: tuple[str, ...]

    def text(self) -> str:
        out = []
        number = 1

        def step(title: str, body: Iterable[str]) -> None:
            nonlocal number
            out.append(f"{number}. {title}")
            out.extend(f"   {line}" if line else "" for line in body)
            out.append("")
            number += 1

        if self.host_steps:
            step("Prepare OpenShell yourself (tongs-agents never installs or starts it):",
                 self.host_steps)
        if self.provider_steps:
            step("Create the missing providers from your shell environment "
                 "(the CLI reads the keys; nothing passes through tongs-agents):",
                 self.provider_steps)
        if not self.policy_written:
            step(f"Save the reviewed policy above as {self.policy_path}:",
                 ["press 'Save policy' in the wizard, or rerun `tongs-agents sandbox "
                  "setup --print` with the same options and --write-policy"])
        step(f"Add this profile to {self.config_path} (the wizard never edits it):",
             [""] + self.snippet.rstrip("\n").split("\n"))
        step("Spawn the agent into its sandbox:", [self.spawn])
        if self.notes:
            out.append("Notes:")
            out.extend(f"- {note}" for note in self.notes)
            out.append("")
        return "\n".join(out).rstrip("\n") + "\n"


def finish(plan: Plan, report: HostReport | None, reviewed: ReviewedPolicy | None, *,
           policy_written: bool = False) -> Finish:
    """The commands and snippet left for the user; needs the reviewed policy."""
    plan = _require_reviewed(plan, reviewed)
    notes = [TELEMETRY_NOTE + ". " + TELEMETRY_OPT_OUT + "."]
    if plan.harness == "claude":
        notes.append("A Claude subscription login inside a sandbox is not supported; "
                     "use an API key provider (claude-code).")
    if plan.chat:
        notes.append("Chat needs tongs-agents installed in the image; grant chat per pane "
                     "from the sidebar.")
    if report is not None and report.provider_error:
        notes.append(f"Could not list providers: {report.provider_error}")
    secrets_found = secret_paths(plan.folder)
    if secrets_found:
        notes.append(f"{plan.folder} holds {', '.join(secrets_found)}. The upload skips only "
                     "what .gitignore lists, so unless it lists them the agent can read "
                     "these; move them out or pick a narrower folder.")
    if not plan.presets:
        notes.append("The policy allows no network access; the agent cannot reach its model "
                     "API until you approve a rule.")
    return Finish(
        host_steps=tuple(host_commands(report)) if report is not None else (),
        provider_steps=tuple(need.command() for need in provider_needs(plan, report)
                             if need.command()),
        policy_path=plan.resolved_policy_path(),
        policy_written=policy_written,
        snippet=profile_snippet(plan),
        config_path=profiles_path(),
        spawn=spawn_command(plan),
        notes=tuple(notes),
    )


def write_policy(reviewed: ReviewedPolicy, path: Path) -> Path:
    """Write the reviewed policy to a new private file; never overwrites one."""
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise SetupError(f"policy file {target}: an absolute path")
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        raise SetupError(f"refusing to overwrite {target}; choose a new file") from None
    except OSError as error:
        raise SetupError(f"cannot write {target}: {error.strerror or error}") from None
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(reviewed.text)
    return target


# Image probe


def probe_delete_command(sandbox: str, gateway: str | None = None) -> str:
    """The command that removes a probe sandbox left behind."""
    where = f" --gateway {gateway}" if gateway else ""
    return f"tongs-agents sandbox delete{where} {sandbox}"


def probe_name() -> str:
    """A fresh probe sandbox name; the wizard picks it first so it can always name it."""
    return PROBE_PREFIX + secrets.token_hex(4)


@dataclass(frozen=True)
class ProbeResult:
    sandbox: str
    found: Mapping[str, str]
    missing: tuple[str, ...]
    deleted: bool
    error: str | None = None
    gateway: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing

    def lines(self) -> list[str]:
        out = [f"{name}: {path}" for name, path in self.found.items()]
        out += [f"{name}: MISSING" for name in self.missing]
        if self.error:
            out.append(f"probe failed: {self.error}")
        if not self.deleted:
            out.append(f"the probe sandbox {self.sandbox} was not deleted; "
                       f"run: {probe_delete_command(self.sandbox, self.gateway)}")
        return out


def probe_tools(plan: Plan) -> tuple[str, ...]:
    return IMAGE_TOOLS + (plan.command,) + ((CHAT_TOOL,) if plan.chat else ())


def parse_probe(text: str, tools: Iterable[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Found tools with their paths, and the missing ones; output is untrusted (the image's)."""
    wanted = list(tools)
    found: dict[str, str] = {}
    for line in clean(text).splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] == "found" and parts[1] in wanted:
            found.setdefault(parts[1], parts[2][:MAX_PROBE_PATH])
    return ({name: found[name] for name in wanted if name in found},
            tuple(name for name in wanted if name not in found))


def probe_image(plan: Plan, reviewed: ReviewedPolicy | None, *,
                client: OpenShell | None = None, out=None,
                timeout: float = READY_TIMEOUT, name: str | None = None) -> ProbeResult:
    """Start a throwaway labelled sandbox from the plan's image with the reviewed policy,
    look for the tools tongs-agents needs inside, then delete it.

    No provider is attached and nothing is uploaded, so no credential reaches it.
    """
    plan = _require_reviewed(plan, reviewed)
    if client is None:
        try:
            client = OpenShell(gateway=plan.gateway)
        except OpenShellError as error:
            raise SetupError(str(error)) from None
    if name is None:
        name = probe_name()
    elif not name.startswith(PROBE_PREFIX) or not SANDBOX_TOKEN_RE.match(name):
        raise SetupError(f"probe sandbox name {name!r}: {PROBE_PREFIX}<hex>")
    gateway = client.gateway
    args = ["--name", name, "--detach", "--label", f"{LAUNCH_LABEL}={secrets.token_hex(16)}"]
    if plan.image:
        args += ["--from", plan.image]
    tools = probe_tools(plan)
    with tempfile.TemporaryDirectory(prefix="tongs-agents-setup-") as directory:
        policy = Path(directory) / "policy.yaml"
        policy.write_text(reviewed.text, encoding="utf-8")
        args += ["--policy", str(policy)]
        try:
            client.create(args)
        except OpenShellError as error:
            return ProbeResult(name, {}, tools, deleted=not _exists(client, name),
                               error=clean(str(error)), gateway=gateway)
    error = None
    found: dict[str, str] = {}
    missing = tools
    try:
        wait_ready(client, name, out if out is not None else io.StringIO(), timeout=timeout)
        result = client.exec_capture(
            name, ["sh", "-c", PROBE_SCRIPT, "tongs-agents-probe", *tools],
            timeout=PROBE_TIMEOUT, max_bytes=MAX_PROBE_OUTPUT,
        )
        if result.ok:
            found, missing = parse_probe(result.stdout, tools)
        else:
            error = f"sandbox exec failed: {clean(result.error_text())}"
    except (OpenShellError, SandboxLaunchError) as failure:
        error = clean(str(failure))
    deleted = True
    try:
        client.delete(name)
    except OpenShellError:
        deleted = not _exists(client, name)
    return ProbeResult(name, found, missing, deleted=deleted, error=error, gateway=gateway)


def _exists(client: OpenShell, name: str) -> bool:
    try:
        return client.get(name) is not None
    except OpenShellError:
        return True
