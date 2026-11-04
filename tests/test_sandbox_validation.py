"""The OpenShell validator, run instrumented against the fake CLI (never a real gateway)."""

import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.test_sandbox_support import FAKE_DIR, fake, posix_only  # noqa: F401
from tongs_agents import launches
from tongs_agents.profiles import parse_profile
from tongs_agents.sandbox import launcher
from tongs_agents.sandbox.openshell import OpenShell

ROOT = Path(__file__).resolve().parents[1]
FETCHER = shutil.which("curl") or shutil.which("wget")


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "openshell_validation", ROOT / "validation" / "openshell.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_validator(tmp_path, *extra, env=None):
    environment = {
        key: value for key, value in os.environ.items()
        if key not in ("FAKE_OPENSHELL_PHASES", "FAKE_OPENSHELL_FAIL")
    }
    environment.update({
        "FAKE_OPENSHELL_HOME": str(tmp_path / "fake"),
        "FAKE_OPENSHELL_RUN_TTY": "1",
        "PATH": f"{FAKE_DIR}{os.pathsep}{os.environ.get('PATH', '')}",
        "TONGS_AGENTS_STATE_DIR": str(tmp_path / "unused-state"),
        "TONGS_AGENTS_CONFIG_DIR": str(tmp_path / "unused-config"),
        **(env or {}),
    })
    output = tmp_path / "evidence.json"
    result = subprocess.run(
        [sys.executable, str(ROOT / "validation" / "openshell.py"),
         "--image", "example/fake-agent:1", "--deny-host", "127.0.0.1:9",
         "--deny-path", "/proc/tongs-agents-denied", "--output", str(output), *extra],
        env=environment, capture_output=True, text=True, timeout=120, cwd=tmp_path,
    )
    evidence = json.loads(output.read_text()) if output.exists() else None
    return result, evidence


def remaining_sandboxes(tmp_path):
    return json.loads((tmp_path / "fake" / "state.json").read_text())["sandboxes"]


@posix_only
def test_validator_passes_instrumented_against_the_fake_cli(tmp_path):
    result, evidence = run_validator(tmp_path)
    assert result.returncode == 0, (result.stderr, evidence)
    assert evidence["label"] == "instrumented"
    assert evidence["status"] == "complete"
    names = {item["name"]: item["passed"] for item in evidence["checks"]}
    assert names["hook status bridged to the store"] is True
    assert names["stop hook reaches idle"] is True
    assert names["record carries the host launch identity"] is True
    assert names["workspace and harness config uploaded"] is True
    assert names["allowed write in the working directory"] is True
    assert names["denied host 127.0.0.1:9 refused"] is None
    assert names["denied path /proc/tongs-agents-denied refused"] is None
    assert names["relaunch reuses the labelled sandbox"] is True
    assert names["harness config uploaded again on relaunch"] is True
    order = [item["name"] for item in evidence["checks"]]
    assert order.index("relaunch reuses the labelled sandbox") < order.index(
        "harness config uploaded again on relaunch")
    assert evidence["cleanup"] == {"sandbox_deleted": True}
    assert not (tmp_path / "unused-state").exists()
    assert remaining_sandboxes(tmp_path) == {}


@posix_only
def test_the_relaunch_listing_sees_only_the_relaunch_upload(fake, tmp_path):
    validator = load_validator()
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("x\n")
    profile = parse_profile("validator", {
        "command": ["claude"], "harness": "claude", "cwd": str(project),
        "sandbox": {"name": "tongs-validate-unit", "image": "example/fake-agent:1"},
    })
    client = OpenShell()

    def prepare():
        record = launches.create_pending(profile, "validator", "none")
        launcher.prepare(profile, record, os.environ, ["sh", "-c", "true"],
                         out=io.StringIO(), client=client)
        return record

    first = prepare()
    evidence = {"checks": []}
    assert validator.clear_config(client, profile.sandbox.name, profile, first.launch_id)
    validator.config_check(evidence, "stale", client, profile.sandbox.name, profile,
                           first.launch_id)
    second = prepare()
    validator.config_check(evidence, "relaunch", client, profile.sandbox.name, profile,
                           second.launch_id)
    assert [(item["name"], item["passed"]) for item in evidence["checks"]] == [
        ("stale", False), ("relaunch", True),
    ]


@posix_only
def test_a_failed_prepare_after_create_still_deletes_the_sandbox(tmp_path):
    result, evidence = run_validator(tmp_path, env={"FAKE_OPENSHELL_PHASES": "Error"})
    assert result.returncode == 1, (result.stderr, evidence)
    assert evidence["status"] == "failed"
    [failed] = [item for item in evidence["checks"] if item["passed"] is False]
    assert "not ready (phase Error)" in failed["detail"]
    assert evidence["cleanup"] == {"sandbox_deleted": True}
    assert remaining_sandboxes(tmp_path) == {}


@posix_only
def test_keep_leaves_the_sandbox(tmp_path):
    result, evidence = run_validator(tmp_path, "--keep")
    assert result.returncode == 0, (result.stderr, evidence)
    assert "cleanup" not in evidence
    assert len(remaining_sandboxes(tmp_path)) == 1


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def allowed_url(tmp_path):
    (tmp_path / "www").mkdir()
    handler = lambda *a, **k: _Quiet(*a, directory=str(tmp_path / "www"), **k)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


@posix_only
@pytest.mark.skipif(FETCHER is None, reason="needs curl or wget on the host")
def test_policy_runs_positive_controls_before_the_deny_checks(tmp_path, allowed_url):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n")
    result, evidence = run_validator(tmp_path, "--policy", str(policy),
                                     "--allow-host", allowed_url)
    assert result.returncode == 0, (result.stderr, evidence)
    names = [item["name"] for item in evidence["checks"]]
    passed = {item["name"]: item["passed"] for item in evidence["checks"]}
    control = f"allowed host {allowed_url} reachable"
    assert names.index("allowed write in the working directory") < names.index(
        "denied path /proc/tongs-agents-denied refused")
    assert names.index(control) < names.index("denied host 127.0.0.1:9 refused")
    assert passed[control] is True
    assert passed["denied host 127.0.0.1:9 refused"] is True
    assert passed["denied path /proc/tongs-agents-denied refused"] is True


@posix_only
@pytest.mark.skipif(FETCHER is None, reason="needs curl or wget on the host")
def test_a_failed_positive_control_makes_the_deny_check_not_applicable(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n")
    result, evidence = run_validator(tmp_path, "--policy", str(policy),
                                     "--allow-host", "http://127.0.0.1:9/")
    assert result.returncode == 1, (result.stderr, evidence)
    passed = {item["name"]: item for item in evidence["checks"]}
    assert passed["allowed host http://127.0.0.1:9/ reachable"]["passed"] is False
    host = passed["denied host 127.0.0.1:9 refused"]
    assert host["passed"] is None
    assert "allowed fetch" in host["detail"]


def test_policy_without_an_allowed_host_is_a_usage_error(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "validation" / "openshell.py"), "--image", "x",
         "--policy", str(tmp_path / "p.yaml")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "--allow-host" in result.stderr
