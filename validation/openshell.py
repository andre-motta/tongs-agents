"""Validate a sandboxed profile against an OpenShell gateway the maintainer runs.

Pending: the maintainer's live run on a rootless Podman gateway with upstream
v0.1.6 and a midstream build (docs/work/openshell-sandbox.md, row 21). This
script never installs OpenShell, starts or stops a gateway, or changes system
services; it needs a gateway that is already reachable through ``openshell``.

    python validation/openshell.py --image IMAGE [--gateway NAME] [--provider P]
        [--policy FILE --allow-host HOST] [--deny-host HOST] [--deny-path PATH]
        [--keep] [--output FILE] [--openshell PATH]

The image needs POSIX ``sh``, coreutils and ``tar``; no agent or model runs.
It creates one uniquely named, labelled sandbox through the M7-A launcher
(exec mode, workspace and harness config uploaded), runs the pane command in a
pseudo-terminal with a scripted fake agent that fires the emitted spool hook,
and checks:

- the hook status reaches the session store through the bridge within 4 s;
- the uploaded harness config and spool sit in the sandbox working directory;
- positive controls first: a write in the working directory and, with
  ``--policy``, a fetch of ``--allow-host`` succeed;
- with ``--policy``, a denied host and a denied path are refused inside the
  sandbox. Without it, or when the matching positive control did not pass,
  the deny checks are recorded as not applicable (``passed: null``).

tongs-agents state and config go to a temporary directory; the openshell CLI
keeps the caller's own gateway configuration. Whenever the sandbox exists at
the end, even when the launch failed after creating it, it is deleted unless
``--keep``. Evidence JSON goes to stdout (or ``--output``) and is
labelled ``live``, or ``instrumented`` when run against the fake CLI of the
test suite.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pty
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tongs_agents import discovery, launches, paths
from tongs_agents.config_layer.emitters import sandbox as sandbox_emitter
from tongs_agents.profiles import parse_profile
from tongs_agents.sandbox import bridge, launcher
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError, flavour

STATUS_TARGET_SECONDS = 4.0
WAIT_SECONDS = 30.0
SESSION_ID = "tongs-agents-validator"
WRITE_PROBE = ".tongs-agents-validate-write"


def fake_agent_command() -> list[str]:
    """A scripted agent: fires SessionStart, PreToolUse and Stop through the spool hooks."""
    def fire(event: str, extra: str = "") -> str:
        payload = (
            f'printf \'{{"session_id":"{SESSION_ID}","cwd":"%s"{extra}}}\' "$PWD"'
        )
        return f"{payload} | {sandbox_emitter.spool_hook_command(event)}"

    script = "; ".join([
        fire("session-start"),
        "sleep 1",
        fire("tool-use", ',"tool_name":"Bash"'),
        "sleep 1",
        fire("stop", ',"last_assistant_message":"validator done"'),
        "sleep 60",
    ])
    return ["sh", "-c", script]


def fetch_command(host: str) -> list[str]:
    """Fetch ``https://<host>`` (or ``host`` itself when it is a URL); exit 3 without a client."""
    url = host if "://" in host else f"https://{host}"
    script = (
        'if command -v curl >/dev/null 2>&1; then curl -sS -m 5 -o /dev/null "$1"; '
        'elif command -v wget >/dev/null 2>&1; then wget -q -T 5 -O /dev/null "$1"; '
        "else exit 3; fi"
    )
    return ["sh", "-c", script, "sh", url]


def write_command(path: str) -> list[str]:
    return ["sh", "-c", 'touch -- "$1" && rm -f -- "$1"', "sh", path]


def policy_checks(evidence: dict, client: OpenShell, name: str, workdir: str,
                  args: argparse.Namespace) -> None:
    """Positive controls first, then the deny checks they make meaningful."""
    write = client.exec_capture(name, write_command(f"{workdir}/{WRITE_PROBE}"),
                                workdir=workdir)
    check(evidence, "allowed write in the working directory", write.ok,
          detail="" if write.ok else write.error_text())
    host_name = f"denied host {args.deny_host} refused"
    path_name = f"denied path {args.deny_path} refused"
    if not args.policy:
        for label in (host_name, path_name):
            check(evidence, label, None, detail="not applicable without --policy")
        return
    fetch = client.exec_capture(name, fetch_command(args.allow_host), timeout=60)
    fetched = None if fetch.returncode == 3 else fetch.ok
    check(evidence, f"allowed host {args.allow_host} reachable", fetched,
          detail="no curl or wget in the image" if fetched is None
          else "" if fetch.ok else fetch.error_text())
    if fetched:
        host = client.exec_capture(name, fetch_command(args.deny_host), timeout=60)
        check(evidence, host_name, None if host.returncode == 3 else not host.ok)
    else:
        check(evidence, host_name, None, detail="the allowed fetch did not succeed")
    if write.ok:
        path = client.exec_capture(name, write_command(args.deny_path))
        check(evidence, path_name, not path.ok)
    else:
        check(evidence, path_name, None, detail="the allowed write did not succeed")


def cleanup(evidence: dict, client: OpenShell, name: str) -> None:
    """Delete the sandbox whenever it exists, whatever failed before."""
    try:
        exists = client.get(name) is not None
    except OpenShellError as error:
        evidence["cleanup"] = {"sandbox_deleted": False, "detail": str(error)}
        return
    if not exists:
        evidence["cleanup"] = {"sandbox_existed": False}
        return
    try:
        deleted = client.run("sandbox", "delete", name, timeout=120)
    except OpenShellError as error:
        evidence["cleanup"] = {"sandbox_deleted": False, "detail": str(error)}
        return
    evidence["cleanup"] = {"sandbox_deleted": deleted.ok}


def check(evidence: dict, name: str, passed: bool | None, **detail) -> None:
    evidence["checks"].append({"name": name, "passed": passed, **detail})


def wait_for(predicate, timeout: float, interval: float = 0.25):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def validator_record(status: str | None = None) -> dict | None:
    bridge.sync_all()
    for record in discovery.read_stored_sessions():
        if record.get("session_id") == SESSION_ID and (
            status is None or record.get("status") == status
        ):
            return record
    return None


def run(args: argparse.Namespace) -> dict:
    instrumented = bool(os.environ.get("FAKE_OPENSHELL_HOME"))
    evidence: dict = {
        "schema": "openshell-validation/1",
        "date": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d"),
        "issue": 35,
        "item": "M7-A",
        "label": "instrumented" if instrumented else "live",
        "status": "complete",
        "checks": [],
    }
    client = OpenShell(args.openshell, gateway=args.gateway)
    version_text = client.version_text()
    version, warnings = client.checked_version()
    evidence["openshell"] = {"version": version_text, "flavour": flavour(version_text),
                             "warnings": warnings}
    status = client.run("status", "-o", "json", timeout=30)
    check(evidence, "gateway reachable", status.ok,
          detail="" if status.ok else status.error_text())
    if not status.ok:
        evidence["status"] = "gateway unreachable; start it yourself and rerun"
        return evidence

    work = Path(tempfile.mkdtemp(prefix="tongs-agents-openshell-"))
    project = work / "project"
    project.mkdir()
    (project / "README.md").write_text("tongs-agents OpenShell validation\n")
    os.environ[paths.STATE_DIR_ENV] = str(work / "state")
    os.environ[paths.CONFIG_DIR_ENV] = str(work / "config")
    name = f"tongs-validate-{secrets.token_hex(4)}"
    sandbox_table: dict = {"name": name, "image": args.image}
    if args.gateway:
        sandbox_table["gateway"] = args.gateway
    if args.provider:
        sandbox_table["providers"] = args.provider
    if args.policy:
        sandbox_table["policy"] = str(Path(args.policy).resolve())
    profile = parse_profile("validator", {
        "command": ["claude"], "harness": "claude", "cwd": str(project),
        "sandbox": sandbox_table,
    })
    record = launches.create_pending(profile, "validator", "none")
    pane = None
    primary = None
    try:
        started = time.monotonic()
        prepared = launcher.prepare(profile, record, os.environ, fake_agent_command(),
                                    out=sys.stderr, client=client)
        evidence["sandbox"] = {"name": name, "reused": prepared.reused,
                               "ready_seconds": round(time.monotonic() - started, 2)}
        primary, secondary = pty.openpty()
        pane_started = time.monotonic()
        pane = subprocess.Popen(prepared.argv, env=prepared.env, stdin=secondary,
                                stdout=secondary, stderr=secondary, close_fds=True)
        os.close(secondary)
        info = wait_for(lambda: discovery.get_process_info(pane.pid), 5)
        launches.claim(record.launch_id, pane.pid, info.start_token if info else None, None)
        first = wait_for(validator_record, WAIT_SECONDS)
        latency = round(time.monotonic() - pane_started, 2)
        check(evidence, "hook status bridged to the store", first is not None,
              seconds_from_pane_start=latency)
        check(evidence, f"status within {STATUS_TARGET_SECONDS:g}s of pane start",
              first is not None and latency <= STATUS_TARGET_SECONDS, seconds=latency)
        idle = wait_for(lambda: validator_record("idle"), WAIT_SECONDS)
        check(evidence, "stop hook reaches idle", idle is not None)
        if first is not None:
            check(evidence, "record carries the host launch identity",
                  first.get("pid") == str(pane.pid)
                  and (first.get("launch") or {}).get("launch_id") == record.launch_id
                  and first.get("source") == "sandbox")
        base, config_dir, _spool = launcher.layout(profile.sandbox, "claude",
                                                   record.launch_id)
        listing = client.exec_capture(name, ["sh", "-c", 'test -f "$1/settings.json" && '
                                             'test -f README.md', "sh", config_dir],
                                      workdir=profile.sandbox.workdir)
        check(evidence, "workspace and harness config uploaded", listing.ok,
              detail=listing.error_text() if not listing.ok else base)
        policy_checks(evidence, client, name, profile.sandbox.workdir, args)
        relaunch = launches.create_pending(profile, "validator", "none")
        again = launcher.prepare(profile, relaunch, os.environ, fake_agent_command(),
                                 out=sys.stderr, client=client)
        check(evidence, "relaunch reuses the labelled sandbox", again.reused)
    except (launcher.SandboxLaunchError, OpenShellError, OSError) as error:
        check(evidence, "sandboxed launch", False, detail=str(error))
    finally:
        if pane is not None:
            pane.terminate()
            try:
                pane.wait(10)
            except subprocess.TimeoutExpired:
                pane.kill()
                pane.wait()
        if primary is not None:
            os.close(primary)
        if not args.keep:
            cleanup(evidence, client, name)
        shutil.rmtree(work, ignore_errors=True)
    if any(item["passed"] is False for item in evidence["checks"]):
        evidence["status"] = "failed"
    evidence["command"] = shlex.join(["python", "validation/openshell.py", "--image",
                                      args.image])
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--image", required=True, help="sandbox image or template (--from)")
    parser.add_argument("--gateway", help="openshell gateway name (-g)")
    parser.add_argument("--provider", action="append", help="provider to attach")
    parser.add_argument("--policy", help="policy file for the sandbox")
    parser.add_argument("--allow-host",
                        help="a host (or URL) the policy allows; needed with --policy")
    parser.add_argument("--deny-host", default="example.com",
                        help="a host the policy denies (default example.com)")
    parser.add_argument("--deny-path", default="/etc/tongs-agents-denied",
                        help="a path the policy keeps read-only")
    parser.add_argument("--keep", action="store_true", help="keep the sandbox afterwards")
    parser.add_argument("--output", help="write evidence JSON here")
    parser.add_argument("--openshell", help="openshell executable (default: PATH)")
    args = parser.parse_args(argv)
    if args.policy and not args.allow_host:
        parser.error("--policy needs --allow-host (the allowed fetch is the positive control)")
    try:
        evidence = run(args)
    except OpenShellError as error:
        print(f"openshell validation: {error}", file=sys.stderr)
        return 2
    text = json.dumps(evidence, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0 if evidence["status"] == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
