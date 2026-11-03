"""Shared fixtures for the sandbox tests: the fake openshell CLI and a live pane process."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.launch_helpers import write_claim
from tongs_agents import discovery, paths
from tongs_agents.sandbox import state

FAKE_DIR = Path(__file__).parent / "fixtures" / "fake_openshell"
FAKE_OPENSHELL = FAKE_DIR / "openshell"
LAUNCH_ID = "0123456789abcdef0123456789abcdef"

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake CLI and spool need POSIX sh")


class Fake:
    """Handle on one fake openshell home."""

    def __init__(self, home: Path):
        self.home = home

    @property
    def executable(self) -> str:
        return str(FAKE_OPENSHELL)

    def calls(self) -> list[list[str]]:
        path = self.home / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line)["argv"] for line in path.read_text().splitlines()]

    def raw_calls(self) -> list[dict]:
        path = self.home / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def state(self) -> dict:
        path = self.home / "state.json"
        return json.loads(path.read_text()) if path.exists() else {"sandboxes": {}}

    def fs(self, sandbox: str, path: str = "") -> Path:
        return self.home / "fs" / sandbox / path.lstrip("/")

    def run(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        environment = {**os.environ, **(env or {})}
        return subprocess.run([self.executable, *args], env=environment, capture_output=True,
                              text=True, timeout=30)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = tmp_path / "fake-openshell"
    home.mkdir()
    monkeypatch.setenv("FAKE_OPENSHELL_HOME", str(home))
    monkeypatch.setenv("PATH", f"{FAKE_DIR}{os.pathsep}{os.environ.get('PATH', '')}")
    for key in ("FAKE_OPENSHELL_PHASES", "FAKE_OPENSHELL_FAIL", "FAKE_OPENSHELL_VERSION",
                "FAKE_OPENSHELL_RUN_TTY", "OPENSHELL_GATEWAY"):
        monkeypatch.delenv(key, raising=False)
    return Fake(home)


@pytest.fixture
def pane_process():
    """A live host process standing in for the pane's openshell CLI."""
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        deadline = time.monotonic() + 10
        info = discovery.get_process_info(process.pid)
        while info is None and time.monotonic() < deadline:
            time.sleep(0.05)
            info = discovery.get_process_info(process.pid)
        yield process, info
    finally:
        process.kill()
        process.wait()


def make_launch(info, *, harness="claude", profile="sandboxed", sandbox="tongs-sandboxed",
                launch_id=LAUNCH_ID, workdir="/sandbox/workspace", cwd="/home/user/project",
                mode="exec"):
    write_claim(launch_id, info.pid, info.start_token, harness=harness, profile=profile)
    spool = f"{workdir}/.tongs-agents/spool/{launch_id}"
    record = state.new(
        launch_id=launch_id, profile=profile, harness=harness, sandbox=sandbox, mode=mode,
        workdir=workdir, spool=spool, gateway=None, cwd=cwd, terminal="zellij",
        terminal_env={"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents", "ZELLIJ_PANE_ID": "3"},
    )
    return state.write(record)


def spool_dir(fake: Fake, launch) -> Path:
    path = fake.fs(launch.sandbox, launch.spool)
    path.mkdir(parents=True, exist_ok=True)
    return path


def stamp(index: int) -> str:
    return f"{int(time.time()) * 1_000_000_000 + index:019d}"


def sessions() -> list[dict]:
    directory = paths.sessions_dir()
    if not directory.exists():
        return []
    return [json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))]


def create_sandbox(fake: Fake, name="tongs-sandboxed", *, profile="sandboxed", phases=None):
    env = {"FAKE_OPENSHELL_PHASES": phases} if phases else None
    result = fake.run("sandbox", "create", "--name", name, "--detach",
                      "--label", f"tongs-agents.profile={profile}", env=env)
    assert result.returncode == 0, result.stderr
