"""The real tongs-agents MCP server under Codex's environment filter (Linux).

Codex starts a stdio MCP server with only HOME, LANG, PATH, TMPDIR and USER
plus the names in that server's ``env_vars`` (docs/work/emitter-strategy.md,
live on Codex 0.166.0). A fake ``codex`` wrapper process stands in for the
claimed agent process of a launch: it reads ``[mcp_servers.tongs-agents]``
from the config.toml that ``install`` wrote and starts that server with the
filtered environment, so the server's identity check walks real ``/proc``
ancestry. tmux is a fake CLI that lists the launch's pane. No Codex, tmux,
agent or model is started.

The state, config and runtime dirs are deliberately outside HOME, the way
``TONGS_AGENTS_STATE_DIR``, ``XDG_CONFIG_HOME`` or ``XDG_RUNTIME_DIR`` move
them for a user, so a server that loses those variables reads other dirs.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tongs_agents import cli, discovery, launches, paths
from tongs_agents.chat.store import ChatStore
from tests.test_chat_mcp_stdio import CHAT_TOOLS, Client

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="reads real process ancestry from /proc"
)

SOURCE = Path(__file__).resolve().parents[1] / "src"
CODEX_BASE_ENV = ("HOME", "LANG", "PATH", "TMPDIR", "USER")
CONTROL_TOOLS = {"agents_spawn", "agents_send", "agents_status", "agents_wait"}
PROFILE = "codex-lead"
SESSION = "agents"
PANE = "%1"

# Stands in for codex: waits for the launch record, then starts the configured
# server with only the variables Codex passes. PYTHONPATH is the one addition:
# it only points the interpreter at this source tree.
FAKE_CODEX = """\
import os, subprocess, sys, time, tomllib
go, source = sys.argv[1], sys.argv[2]
while not os.path.exists(go):
    time.sleep(0.02)
with open(os.path.join(os.environ["CODEX_HOME"], "config.toml"), "rb") as stream:
    table = tomllib.load(stream)["mcp_servers"]["tongs-agents"]
names = ["HOME", "LANG", "PATH", "TMPDIR", "USER", *table.get("env_vars", [])]
env = {name: os.environ[name] for name in names if name in os.environ}
env["PYTHONPATH"] = source
sys.exit(subprocess.call([table["command"], *table.get("args", [])], env=env))
"""

FAKE_TMUX = """\
#!{python}
import sys
args = sys.argv[1:]
if args[:1] == ["-S"]:
    args = args[2:]
if args[:1] == ["display-message"] and args[-1] == "#{{session_id}}":
    print("$1")
elif args[:1] == ["list-panes"]:
    print("\\t".join(["$1", "@1", "{pane}", "0", "", "1", "1", "", "codex", "codex"]))
else:
    sys.exit(1)
"""

CONFIG = f"""\
[profiles.{PROFILE}]
command = ["codex", "--no-daemon"]
mcp_control = true

[control]
mcp_enabled = true
"""


@pytest.fixture
def codex(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tmux = bin_dir / "tmux"
    tmux.write_text(FAKE_TMUX.format(python=sys.executable, pane=PANE))
    tmux.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    config = paths.config_dir(create=True) / "config.toml"
    config.write_text(CONFIG)
    config.chmod(0o600)
    codex_home = tmp_path / "codex-home"
    started = []

    def start(table=None):
        """A granted launch whose fake codex runs the server; returns its client."""
        assert cli._install_codex_mcp(codex_home)
        if table is not None:
            (codex_home / "config.toml").write_text(table)
        go = tmp_path / f"go-{len(started)}"
        socket = str(tmp_path / "tmux-socket")
        launch_id = os.urandom(16).hex()
        env = dict(os.environ)
        for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "ZELLIJ_SOCKET_DIR",
                    "PYTHONPATH"):
            env.pop(key, None)
        env.update({
            "CODEX_HOME": str(codex_home),
            "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
            "TMUX": f"{socket},4242,0",
            "TMUX_PANE": PANE,
            "TONGS_AGENTS_LAUNCH_ID": launch_id,
            "TONGS_AGENTS_PROFILE": PROFILE,
        })
        process = subprocess.Popen(
            [sys.executable, "-c", FAKE_CODEX, str(go), str(SOURCE)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, bufsize=0, cwd=tmp_path,
        )
        started.append(process)
        info = discovery.get_process_info(process.pid)
        assert info is not None
        launches._write(launches.LaunchRecord(
            launch_id=launch_id, profile=PROFILE, harness="codex", multiplexer="tmux",
            session=SESSION, created=int(time.time()), socket_dir=socket, pane_id=PANE,
            pid=info.pid, process_start=info.start_token, state="running",
        ))
        with ChatStore.maintainer(create=True) as maintainer:
            maintainer.put_grant(
                launch_id=launch_id, handle="lead", level="chat",
                process_start=info.start_token, pane_session=SESSION, pane_id=PANE,
                socket_dir=socket, via="cli", profile=PROFILE, harness="codex",
            )
        go.write_text("")
        client = Client(process)
        client.launch_id = launch_id
        client.initialize()
        return client

    yield start
    for process in started:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout):
            stream.close()


def test_chat_and_control_resolve_under_the_codex_filter(codex):
    lead = codex()
    tools = lead.tools()
    assert CHAT_TOOLS <= tools
    assert CONTROL_TOOLS <= tools

    posted = lead.call("chat_post", channel="general", body="hello from codex")
    assert posted["ok"] and posted["handle"] == "lead"
    thread = lead.call("chat_read", channel="general", after_id=0)
    assert [message["body"] for message in thread["messages"]] == ["hello from codex"]

    # Reached only after the caller resolved from TONGS_AGENTS_LAUNCH_ID, was
    # verified in its tmux pane and its profile was found opted in.
    refused = lead.call("agents_status", launch_id=lead.launch_id)
    assert refused["ok"] is False
    assert refused["error"] == "refusing to act on the caller's own launch"


def test_the_old_env_vars_lose_chat_and_control(codex):
    old = (
        "[mcp_servers.tongs-agents]\n"
        f"command = {json.dumps(sys.executable)}\n"
        'args = ["-m", "tongs_agents.mcp_server"]\n'
        'env_vars = ["XDG_STATE_HOME", "TONGS_AGENTS_STATE_DIR"]\n'
    )
    lead = codex(old)
    tools = lead.tools()
    assert not CHAT_TOOLS & tools
    assert not CONTROL_TOOLS & tools
    assert "fleet_status" in tools
