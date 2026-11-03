"""The one wrapper around the ``openshell`` CLI (docs/work/openshell-sandbox.md, rows 1, 2).

Every call is an argv list (never a shell) with ``NO_COLOR=1``, a timeout,
closed stdin unless input is given, and bounded output. Argv is built here
from validated profile fields and fixed tongs-agents values only: ``--env``
carries only the names in ``SANDBOX_ENV_NAMES`` and never a secret.

The argument shapes follow the OpenShell CLI at upstream ``ba302a6`` (equal to
v0.1.6 and midstream ``v0.1.6-rhaiv.4``): ``-g/--gateway`` is global,
``sandbox create`` takes ``--name``, ``sandbox get|upload|connect|start``
take the name as their first positional, ``sandbox exec`` takes ``-n``, and
``settings set <name> --key K --value V`` scopes a setting to one sandbox.
M7-B adds ``sandbox list -o json`` (a JSON array of the ``get`` objects),
``sandbox get <name> --policy-only`` (the active policy YAML), ``sandbox
start|stop|delete <name>``, ``sandbox download <name> <path> <dest>``,
``policy get <name>``, ``logs <name> -n N`` (never ``--tail``) and
``status -o json``. They are unvalidated against a live gateway until the maintainer's run
(row 21); the fake CLI under tests/fixtures pins them.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

EXECUTABLE = "openshell"
DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ERROR_TEXT = 400
MINIMUM_VERSION = (0, 1, 0)
SUPPORTED_FLOOR = (0, 1, 6)
VALIDATED_MINOR = (0, 1)
VERSION_RE = re.compile(r"(?<![0-9.])(\d{1,4})\.(\d{1,4})\.(\d{1,6})")
LAUNCH_LABEL = "tongs-agents.launch"
PROFILE_LABEL = "tongs-agents.profile"
OWNED_LABELS = (PROFILE_LABEL, LAUNCH_LABEL)
LIST_LIMIT = 1000
STATUS_TIMEOUT = 5.0
POLICY_ADVISOR_KEY = "agent_policy_proposals_enabled"
# Absolute: a wheel install ships no docs/ folder.
SANDBOX_DOCS_URL = "https://github.com/andre-motta/tongs-agents/blob/main/docs/sandbox.md"
SANDBOX_ENV_NAMES = frozenset({
    "TONGS_AGENTS_LAUNCH_ID",
    "TONGS_AGENTS_PROFILE",
    "TONGS_AGENTS_SPOOL",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "PI_CODING_AGENT_DIR",
})
NOT_FOUND_RE = re.compile(r"not ?found", re.IGNORECASE)


class OpenShellError(RuntimeError):
    """The CLI is missing, too old, timed out or failed; the message says which."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error_text(self) -> str:
        text = " ".join((self.stderr or self.stdout).split())
        return text[:MAX_ERROR_TEXT] or f"exit status {self.returncode}"


@dataclass(frozen=True)
class SandboxInfo:
    """The fields of ``sandbox get -o json`` tongs-agents reads."""

    name: str
    phase: str
    labels: Mapping[str, str]
    created_at: str = ""
    policy_version: int | None = None

    def label(self, key: str) -> str | None:
        value = self.labels.get(key)
        return value if isinstance(value, str) else None

    @property
    def owned(self) -> bool:
        """Row 20: tongs-agents manages only sandboxes carrying one of its labels."""
        return any(self.label(key) for key in OWNED_LABELS)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The leading ``X.Y.Z`` of ``openshell --version`` (``0.1.6-rhaiv.4`` gives 0.1.6)."""
    match = VERSION_RE.search(text or "")
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_problems(version: tuple[int, int, int] | None) -> tuple[str | None, list[str]]:
    """``(error, warnings)`` for a parsed version (row 2)."""
    if version is None:
        return "cannot read the openshell version", []
    shown = ".".join(map(str, version))
    if version < MINIMUM_VERSION:
        return f"openshell {shown} is older than 0.1.0 and not supported", []
    warnings = []
    if version < SUPPORTED_FLOOR:
        floor = ".".join(map(str, SUPPORTED_FLOOR))
        warnings.append(f"openshell {shown} is older than the supported {floor}")
    if version[:2] > VALIDATED_MINOR:
        validated = ".".join(map(str, VALIDATED_MINOR))
        warnings.append(f"openshell {shown} is newer than the validated {validated}.x")
    return None, warnings


def flavour(version_text: str) -> str:
    """``midstream`` for an ``-rhaiv`` build, else ``upstream``; for reporting only."""
    return "midstream" if "-rhaiv" in (version_text or "") else "upstream"


def _check_arg(value: str) -> str:
    if not isinstance(value, str) or "\0" in value:
        raise OpenShellError(f"invalid openshell argument {value!r}")
    return value


def env_arg(name: str, value: str) -> str:
    """One ``--env`` value; only tongs-agents identity and config-dir names pass."""
    if name not in SANDBOX_ENV_NAMES:
        raise OpenShellError(f"refusing to pass {name} into a sandbox")
    return _check_arg(f"{name}={value}")


def _read_bounded(stream, sink: list[bytes], limit: int, overflow: threading.Event,
                  process: subprocess.Popen) -> None:
    total = 0
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            total += len(chunk)
            if total > limit:
                overflow.set()
                try:
                    process.kill()
                except OSError:
                    pass
                return
            sink.append(chunk)
    except (OSError, ValueError):
        return


def run_bounded(argv: Sequence[str], *, env: Mapping[str, str], timeout: float,
                max_bytes: int = MAX_OUTPUT_BYTES, input_bytes: bytes | None = None,
                cwd: str | None = None) -> Result:
    """Run ``argv`` with bounded stdout and stderr; raises OpenShellError on failure to run."""
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
        )
    except OSError as error:
        raise OpenShellError(f"cannot run {argv[0]}: {error.strerror or error}") from None
    out: list[bytes] = []
    err: list[bytes] = []
    overflow = threading.Event()
    readers = [
        threading.Thread(target=_read_bounded, args=(process.stdout, out, max_bytes,
                                                     overflow, process), daemon=True),
        threading.Thread(target=_read_bounded, args=(process.stderr, err, max_bytes,
                                                     overflow, process), daemon=True),
    ]
    for reader in readers:
        reader.start()
    if input_bytes is not None:
        try:
            process.stdin.write(input_bytes)
        except OSError:
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(1)
        raise OpenShellError(f"{' '.join(argv[1:4])} timed out after {timeout:g}s") from None
    for reader in readers:
        reader.join(5)
    for stream in (process.stdout, process.stderr):
        try:
            stream.close()
        except OSError:
            pass
    if overflow.is_set():
        raise OpenShellError(f"{' '.join(argv[1:4])} printed more than {max_bytes} bytes")
    return Result(
        process.returncode,
        b"".join(out).decode("utf-8", errors="replace"),
        b"".join(err).decode("utf-8", errors="replace"),
    )


def _labels(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items()
            if isinstance(key, str) and isinstance(item, str)}


def _sandbox_from(data) -> SandboxInfo | None:
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    phase = data.get("phase")
    if not isinstance(name, str) or not isinstance(phase, str):
        return None
    created = data.get("created_at")
    version = data.get("current_policy_version")
    return SandboxInfo(
        name=name, phase=phase, labels=_labels(data.get("labels")),
        created_at=created if isinstance(created, str) else "",
        policy_version=version if type(version) is int else None,
    )


def parse_sandbox(text: str) -> SandboxInfo | None:
    """The name, phase and labels of one ``sandbox get -o json`` object."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return _sandbox_from(data)


def parse_sandbox_list(text: str) -> list[SandboxInfo] | None:
    """The sandboxes of ``sandbox list -o json`` (a JSON array); None when unrecognized."""
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    sandboxes = [_sandbox_from(item) for item in data]
    return [info for info in sandboxes if info is not None]


class OpenShell:
    """One ``openshell`` executable, optionally bound to a named gateway."""

    def __init__(self, executable: str | None = None, *, gateway: str | None = None,
                 base_env: Mapping[str, str] | None = None) -> None:
        found = executable or shutil.which(EXECUTABLE)
        if not found:
            raise OpenShellError(f"openshell CLI not found on PATH (see {SANDBOX_DOCS_URL})")
        # Absolute, because create runs with the profile cwd as its cwd.
        self.executable = os.path.abspath(found)
        self.gateway = gateway
        self._base_env = dict(os.environ if base_env is None else base_env)

    def env(self) -> dict[str, str]:
        env = dict(self._base_env)
        env["NO_COLOR"] = "1"
        return env

    def argv(self, *args: str) -> list[str]:
        prefix = [self.executable]
        if self.gateway:
            prefix += ["-g", _check_arg(self.gateway)]
        return prefix + [_check_arg(arg) for arg in args]

    def run(self, *args: str, timeout: float = DEFAULT_TIMEOUT,
            input_bytes: bytes | None = None, max_bytes: int = MAX_OUTPUT_BYTES,
            cwd: str | None = None) -> Result:
        return run_bounded(self.argv(*args), env=self.env(), timeout=timeout,
                           max_bytes=max_bytes, input_bytes=input_bytes, cwd=cwd)

    def check(self, *args: str, timeout: float = DEFAULT_TIMEOUT,
              cwd: str | None = None) -> Result:
        result = self.run(*args, timeout=timeout, cwd=cwd)
        if not result.ok:
            raise OpenShellError(f"openshell {' '.join(args[:2])} failed: {result.error_text()}")
        return result

    # Version (row 2)

    def version_text(self) -> str:
        result = run_bounded([self.executable, "--version"], env=self.env(), timeout=10,
                             max_bytes=4096)
        return (result.stdout or result.stderr).strip()

    def checked_version(self) -> tuple[tuple[int, int, int], list[str]]:
        text = self.version_text()
        version = parse_version(text)
        error, warnings = version_problems(version)
        if error:
            raise OpenShellError(error)
        return version, warnings  # type: ignore[return-value]

    # Sandboxes

    def get(self, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> SandboxInfo | None:
        """The sandbox ``name``, or None when the gateway reports it absent."""
        result = self.run("sandbox", "get", name, "-o", "json", timeout=timeout)
        if not result.ok:
            if NOT_FOUND_RE.search(result.stderr or result.stdout):
                return None
            raise OpenShellError(f"openshell sandbox get {name} failed: {result.error_text()}")
        info = parse_sandbox(result.stdout)
        if info is None:
            raise OpenShellError(f"openshell sandbox get {name}: unrecognized JSON output")
        return info

    def create(self, args: Sequence[str], *, timeout: float = 600.0,
               cwd: str | None = None) -> Result:
        """``sandbox create``; ``cwd`` is where relative ``--upload`` sources resolve."""
        return self.check("sandbox", "create", *args, timeout=timeout, cwd=cwd)

    def upload(self, name: str, local: str, dest: str, *, git_ignore: bool = True,
               timeout: float = 600.0) -> Result:
        args = ["sandbox", "upload", name, local, dest]
        if not git_ignore:
            args.append("--no-git-ignore")
        return self.check(*args, timeout=timeout)

    def exec_capture(self, name: str, command: Sequence[str], *, workdir: str | None = None,
                     timeout: float = DEFAULT_TIMEOUT,
                     max_bytes: int = MAX_OUTPUT_BYTES) -> Result:
        """Run ``command`` in the sandbox without a TTY or login shell."""
        args = ["sandbox", "exec", "-n", name, "--no-tty", "--no-login-shell"]
        if workdir:
            args += ["--workdir", workdir]
        return self.run(*args, "--", *command, timeout=timeout, max_bytes=max_bytes)

    def list_sandboxes(self, *, timeout: float = DEFAULT_TIMEOUT) -> list[SandboxInfo]:
        """Every sandbox the gateway lists (labelled or not; callers filter, row 20)."""
        result = self.check("sandbox", "list", "--limit", str(LIST_LIMIT), "-o", "json",
                            timeout=timeout)
        sandboxes = parse_sandbox_list(result.stdout)
        if sandboxes is None:
            raise OpenShellError("openshell sandbox list: unrecognized JSON output")
        return sandboxes

    def start(self, name: str, *, timeout: float = 120.0) -> Result:
        return self.check("sandbox", "start", name, timeout=timeout)

    def stop(self, name: str, *, timeout: float = 120.0) -> Result:
        return self.check("sandbox", "stop", name, timeout=timeout)

    def delete(self, name: str, *, timeout: float = 120.0) -> Result:
        return self.check("sandbox", "delete", name, timeout=timeout)

    def download(self, name: str, sandbox_path: str, dest: str, *,
                 timeout: float = 600.0) -> Result:
        """``sandbox download``; OpenShell reads only inside the sandbox working directory."""
        return self.check("sandbox", "download", name, sandbox_path, dest, timeout=timeout)

    def active_policy(self, name: str, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        """The sandbox's active policy YAML (``sandbox get --policy-only``)."""
        return self.check("sandbox", "get", name, "--policy-only", timeout=timeout).stdout

    def policy_get(self, name: str, *, full: bool = False,
                   timeout: float = DEFAULT_TIMEOUT) -> Result:
        args = ["policy", "get", name]
        if full:
            args.append("--full")
        return self.check(*args, timeout=timeout)

    def logs(self, name: str, *, lines: int, since: str | None = None,
             source: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> Result:
        """Recent log lines; never ``--tail``, which would follow forever."""
        args = ["logs", name, "-n", str(int(lines))]
        if since:
            args += ["--since", since]
        if source:
            args += ["--source", source]
        return self.check(*args, timeout=timeout)

    def status(self, *, timeout: float = STATUS_TIMEOUT) -> Result:
        """``status -o json`` for gateway reachability; never raises on a failed status."""
        return self.run("status", "-o", "json", timeout=timeout, max_bytes=64 * 1024)

    def set_policy_advisor(self, name: str, enabled: bool) -> Result:
        """Row 16: this sandbox only; never ``--global``."""
        return self.check("settings", "set", name, "--key", POLICY_ADVISOR_KEY,
                          "--value", "true" if enabled else "false")

    def exec_tty_argv(self, name: str, command: Sequence[str], *, workdir: str,
                      env: Sequence[tuple[str, str]]) -> list[str]:
        """The pane argv for exec mode (row 4)."""
        args = ["sandbox", "exec", "-n", name, "--tty", "--workdir", workdir]
        for key, value in env:
            args += ["--env", env_arg(key, value)]
        return self.argv(*args, "--", *command)

    def connect_argv(self, name: str) -> list[str]:
        """The pane argv for attach mode."""
        return self.argv("sandbox", "connect", name)
