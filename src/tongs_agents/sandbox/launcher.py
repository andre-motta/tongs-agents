"""Prepare a sandboxed profile's pane command (docs/work/openshell-sandbox.md, rows 4, 5, 20).

``prepare`` runs in the pane, after the launch record is claimed and before
the launcher execs. It checks the CLI version, then creates the profile's
sandbox detached, or reuses it when one already carries this profile's
label (files and agent state survive a gateway drop, so a relaunched
profile restarts the agent in place). A sandbox of that name without the
label is never touched.

- ``exec`` mode (default): ``sandbox create --detach`` uploads the profile
  ``cwd`` to the working directory and the sandbox-flavoured harness config
  to ``<workdir>/.tongs-agents/config/<harness>``; the pane then execs
  ``openshell sandbox exec -n <name> --tty`` with the agent argv. A reused
  sandbox gets the config uploaded again, never the workspace.
- ``attach`` mode (opt-in): the agent is the sandbox's main process and the
  pane execs ``openshell sandbox connect``; its key caveats are printed first.

OpenShell extracts a directory upload at ``<dest>/<basename of the source>``,
like ``cp -r``, whether or not ``<dest>`` exists, and flat at ``<dest>`` only
for a source without a basename such as ``.`` (upstream ``ssh.rs``
``directory_upload_prefix``). So the workspace is uploaded as ``.`` with
``sandbox create`` running in ``cwd``, and the staged config directory is
named after its destination and uploaded to the destination's parent
(``upload_parent``), on create and on reuse alike.

Before the pane command runs the sandbox phase must be Ready; a transitional
phase is waited for with a visible line, anything else fails in the pane.
"""

from __future__ import annotations

import posixpath
import sys
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from tongs_agents.config_layer import schema
from tongs_agents.config_layer.emitters import _common
from tongs_agents.config_layer.emitters import sandbox as sandbox_emitter
from tongs_agents.launches import LaunchRecord
from tongs_agents.profiles import Profile, SandboxSpec
from tongs_agents.sandbox import state
from tongs_agents.sandbox.openshell import (
    LAUNCH_LABEL,
    PROFILE_LABEL,
    SANDBOX_DOCS_URL,
    OpenShell,
    OpenShellError,
    SandboxInfo,
    env_arg,
)
from tongs_agents.terminal_apis import capture_terminal_info

LAUNCH_ID_ENV = "TONGS_AGENTS_LAUNCH_ID"
PROFILE_ENV = "TONGS_AGENTS_PROFILE"
STATE_DIR_NAME = ".tongs-agents"
WORKSPACE_SOURCE = "."
READY_PHASE = "Ready"
WAITING_PHASES = frozenset({"Unknown", "Provisioning"})
READY_TIMEOUT = 180.0
POLL_INTERVAL = 1.0
NAME_PREFIX = "tongs-"
ATTACH_CAVEAT = (
    "attach mode: OpenShell takes Ctrl-D as detach and holds Ctrl-P for its Ctrl-P Ctrl-Q "
    "detach, which Zellij binds by default (Ctrl p pane mode, Ctrl q quit); "
    f"see {SANDBOX_DOCS_URL}"
)


class SandboxLaunchError(RuntimeError):
    """The sandbox cannot be prepared; the message is shown in the pane."""


@dataclass(frozen=True)
class Prepared:
    argv: list[str]
    env: dict[str, str]
    sandbox: str
    reused: bool


def sandbox_name(profile: Profile) -> str:
    spec = profile.sandbox
    if spec is not None and spec.name:
        return spec.name
    return (NAME_PREFIX + profile.name.replace("_", "-")).rstrip("-")


def layout(spec: SandboxSpec, harness: str, launch_id: str) -> tuple[str, str, str]:
    """``(state dir, config dir, spool)`` inside the sandbox for one launch."""
    base = f"{spec.workdir}/{STATE_DIR_NAME}"
    spool = f"{base}/spool/main" if spec.mode == "attach" else f"{base}/spool/{launch_id}"
    return base, f"{base}/config/{harness}", spool


def upload_parent(source: Path, dest: str) -> str:
    """The upload destination that lands directory ``source`` exactly at ``dest``.

    OpenShell places a directory upload at ``<dest>/<basename>``, so ``source``
    must carry ``dest``'s basename and goes to ``dest``'s parent.
    """
    parent, base = posixpath.split(dest)
    if not parent or not base or source.name != base:
        raise SandboxLaunchError(f"cannot upload {source} to {dest}: names differ")
    if ":" in str(source):
        raise SandboxLaunchError(f"cannot upload {source}: the path holds ':'")
    return parent


def create_cwd(profile: Profile) -> str | None:
    """Where ``sandbox create`` runs: the profile ``cwd`` when it uploads the workspace."""
    spec = profile.sandbox
    if spec is None or spec.mode != "exec" or not spec.upload or profile.cwd is None:
        return None
    return str(profile.cwd)


def _labels(profile: Profile, launch_id: str) -> list[str]:
    return ["--label", f"{LAUNCH_LABEL}={launch_id}",
            "--label", f"{PROFILE_LABEL}={profile.name}"]


def create_args(profile: Profile, launch_id: str, name: str, *, stage: Path | None,
                command: list[str]) -> list[str]:
    """The ``sandbox create`` arguments for a profile (never a secret, never auto approval).

    The workspace upload is ``.``, which resolves against ``create_cwd(profile)``.
    """
    spec = profile.sandbox
    assert spec is not None
    args = ["--name", name, "--detach", *_labels(profile, launch_id)]
    if spec.image:
        args += ["--from", spec.image]
    for provider in spec.providers:
        args += ["--provider", provider]
    if spec.policy is not None:
        args += ["--policy", str(spec.policy)]
    if spec.cpu:
        args += ["--cpu", spec.cpu]
    if spec.memory:
        args += ["--memory", spec.memory]
    if spec.driver_config:
        args += ["--driver-config-json", spec.driver_config]
    base, _config, spool = layout(spec, profile.harness, launch_id)
    if spec.mode == "attach":
        args += ["--env", env_arg("TONGS_AGENTS_SPOOL", spool), "--", *command]
        return args
    if spec.upload:
        args += ["--upload", f"{WORKSPACE_SOURCE}:{spec.workdir}"]
    if stage is not None:
        args += ["--upload", f"{stage}:{upload_parent(stage, base)}"]
    return args


def _canonical_config(out: TextIO):
    try:
        return schema.load_canonical()
    except schema.NoSourceError:
        return None
    except (schema.SchemaError, OSError) as error:
        print(f"tongs-agents: skills and instructions not copied: {error}", file=out)
        return None


def stage_tree(profile: Profile, into: Path, out: TextIO) -> Path | None:
    """Write the sandbox harness config to ``into/.tongs-agents``; None when there is none.

    The directory carries the basename of its destination,
    ``<workdir>/.tongs-agents``, so ``upload_parent`` can place it exactly.
    """
    config = _canonical_config(out)
    try:
        tree = sandbox_emitter.sandbox_files(profile.harness, config=config,
                                             profile=profile.name)
    except _common.EmitError as error:
        print(f"tongs-agents: skills and instructions not copied: {error}", file=out)
        tree = sandbox_emitter.sandbox_files(profile.harness)
    if not tree:
        return None
    root = into / STATE_DIR_NAME
    sandbox_emitter.write_tree(
        {f"config/{profile.harness}/{key}": value for key, value in tree.items()}, root
    )
    return root


def wait_ready(client: OpenShell, name: str, out: TextIO, *,
               timeout: float = READY_TIMEOUT, clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> SandboxInfo:
    """The sandbox once Ready; waits visibly through transitional phases only."""
    deadline = clock() + timeout
    shown = None
    while True:
        info = client.get(name)
        if info is None:
            raise SandboxLaunchError(f"sandbox {name} disappeared before it was ready")
        if info.phase == READY_PHASE:
            return info
        if info.phase not in WAITING_PHASES:
            raise SandboxLaunchError(
                f"sandbox {name} is not ready (phase {info.phase}); check it with "
                f"`openshell sandbox get {name}`, start it with `openshell sandbox start "
                f"{name}` or delete it"
            )
        if clock() >= deadline:
            raise SandboxLaunchError(
                f"sandbox {name} is not ready after {timeout:g}s (phase {info.phase})"
            )
        if info.phase != shown:
            print(f"tongs-agents: waiting for sandbox {name} ({info.phase})", file=out,
                  flush=True)
            shown = info.phase
        sleep(POLL_INTERVAL)


def prepare(profile: Profile, record: LaunchRecord, environ: Mapping[str, str], command: list[str],
            *, out: TextIO = sys.stderr, client: OpenShell | None = None,
            wait: Callable[..., SandboxInfo] = wait_ready) -> Prepared:
    """Create or reuse the profile's sandbox and return the pane argv and env.

    Raises SandboxLaunchError with a one-line message for the pane.
    """
    spec = profile.sandbox
    if spec is None:
        raise SandboxLaunchError(f"profile {profile.name} has no sandbox table")
    name = sandbox_name(profile)
    launch_id = record.launch_id
    try:
        client = client or OpenShell(gateway=spec.gateway, base_env=environ)
        _version, warnings = client.checked_version()
        for warning in warnings:
            print(f"tongs-agents: {warning}", file=out)
        existing = client.get(name)
        if existing is not None and existing.label(PROFILE_LABEL) != profile.name:
            raise SandboxLaunchError(
                f"sandbox {name} exists without the {PROFILE_LABEL}={profile.name} label; "
                "tongs-agents does not reuse it (set sandbox.name or delete it)"
            )
        base, _config, _spool = layout(spec, profile.harness, launch_id)
        with tempfile.TemporaryDirectory(prefix="tongs-agents-sandbox-") as temporary:
            stage = None
            if spec.mode == "exec":
                stage = stage_tree(profile, Path(temporary), out)
            if existing is None:
                workspace = create_cwd(profile)
                if workspace is not None and not Path(workspace).is_dir():
                    raise SandboxLaunchError(f"cannot upload {workspace}: not a directory")
                print(f"tongs-agents: creating sandbox {name}", file=out, flush=True)
                client.create(create_args(profile, launch_id, name, stage=stage,
                                          command=command), cwd=workspace)
                wait(client, name, out)
            else:
                print(f"tongs-agents: reusing sandbox {name}", file=out, flush=True)
                wait(client, name, out)
                if stage is not None:
                    client.upload(name, str(stage), upload_parent(stage, base),
                                  git_ignore=False)
        if spec.policy_advisor:
            client.set_policy_advisor(name, True)
    except OpenShellError as error:
        raise SandboxLaunchError(str(error)) from None
    _base, config_dir, spool = layout(spec, profile.harness, launch_id)
    terminal = capture_terminal_info()
    try:
        state.write(state.new(
            launch_id=launch_id, profile=profile.name, harness=profile.harness,
            sandbox=name, mode=spec.mode, workdir=spec.workdir, spool=spool,
            gateway=spec.gateway, cwd=str(profile.cwd or ""),
            terminal=terminal.get("terminal", ""),
            terminal_env=terminal.get("terminal_env", {}),
        ))
    except OSError as error:
        raise SandboxLaunchError(f"cannot record the sandbox launch: {error}") from None
    if spec.mode == "attach":
        print(f"tongs-agents: {ATTACH_CAVEAT}", file=out, flush=True)
        argv = client.connect_argv(name)
    else:
        env = [(LAUNCH_ID_ENV, launch_id), (PROFILE_ENV, profile.name),
               (sandbox_emitter.SPOOL_ENV, spool)]
        variable = sandbox_emitter.CONFIG_DIR_ENV_BY_HARNESS.get(profile.harness)
        if variable is not None:
            env.append((variable, config_dir))
        try:
            argv = client.exec_tty_argv(name, command, workdir=spec.workdir, env=env)
        except OpenShellError as error:
            raise SandboxLaunchError(str(error)) from None
    pane_env = dict(environ)
    pane_env[LAUNCH_ID_ENV] = launch_id
    pane_env[PROFILE_ENV] = profile.name
    return Prepared(argv=argv, env=pane_env, sandbox=name, reused=existing is not None)


__all__ = [
    "ATTACH_CAVEAT",
    "Prepared",
    "SandboxLaunchError",
    "create_args",
    "create_cwd",
    "layout",
    "prepare",
    "sandbox_name",
    "upload_parent",
    "wait_ready",
]
