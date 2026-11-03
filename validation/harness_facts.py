"""Probe harness registration and hook environment facts in isolated dirs.

Local-only Linux helper; it is never run in CI. It answers the questions in
docs/work/harness-facts.md with the installed Claude Code, Codex and Pi
binaries, each run with a temporary HOME, XDG dirs, CLAUDE_CONFIG_DIR,
CODEX_HOME and PI_CODING_AGENT_DIR. Model traffic goes only to a synthetic
provider on 127.0.0.1; no account, API key or paid model is used and no
credential is read or copied. Hooks are a recorder script that logs the event
name, a fixed list of environment variables and its process ancestry.

Codex runs hooks only after the user trusts them (`/hooks`). The probe stands
in for that review inside the temporary CODEX_HOME: it reads each hook's key
and current hash through `codex app-server` `hooks/list` and records them as
trusted there, exactly as the review would. It never uses a trust bypass flag.

The real user config files are hashed before and after the run and only a
changed/unchanged flag is reported. Processes are signalled only when the
probe started them (PID plus kernel start time) or when their environment
names the probe's private CODEX_HOME. Evidence is JSON with the temporary
root, home directory, user name and host name replaced by placeholders.
"""

import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from probe_terminals import (
    PtyClient,
    Redactor,
    Tracker,
    process_identity,
    run,
    wait_until,
)

from tongs_agents.discovery import (
    find_ancestor_process,
    get_process_info,
    process_matches_harness,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ("claude", "codex", "env", "identity", "pi")
LABELS = ("live", "instrumented", "unvalidated")
RECORDED_ENV = (
    "TONGS_AGENTS_LAUNCH_ID",
    "TONGS_AGENTS_PROFILE",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "PI_CODING_AGENT_DIR",
)
USER_CONFIG = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".pi/agent/settings.json",
    ".pi/agent/mcp.json",
    ".pi/agent/claude-fleet-monitor.json",
)
VOLATILE_USER_CONFIG = (".claude.json",)
CLAUDE_EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PermissionRequest",
    "Stop", "SessionEnd",
)
CODEX_EVENTS = CLAUDE_EVENTS
TOOL_PROMPT = "probe-tool: run the fixture command"
TEXT_PROMPT = "probe-text: reply briefly"
RUN_TIMEOUT = 90
TRUST_MARKER = "# harness-facts: hooks trusted as by /hooks review"

RECORDER = """\
#!{python} -I
import json, os, sys, time

LOG = {log!r}
NAMES = {names!r}


def ancestors():
    found = []
    pid = os.getppid()
    for _ in range(12):
        try:
            with open(f"/proc/{{pid}}/stat") as handle:
                text = handle.read()
            comm = text[text.index("(") + 1:text.rindex(")")]
            ppid = int(text.rsplit(") ", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        found.append({{"pid": pid, "comm": comm}})
        if ppid <= 1:
            break
        pid = ppid
    return found


try:
    payload = json.loads(sys.stdin.read(1 << 20) or "{{}}")
except ValueError:
    payload = {{}}
if not isinstance(payload, dict):
    payload = {{}}
entry = {{
    "argv": sys.argv[1:],
    "event": payload.get("hook_event_name") or payload.get("event_id"),
    "env": {{name: os.environ.get(name) for name in NAMES}},
    "ancestors": ancestors(),
    "ts": time.time(),
}}
with open(LOG, "a") as handle:
    handle.write(json.dumps(entry) + "\\n")
"""

FAKE_MCP = """\
#!{python} -I
import json, sys

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {{
            "protocolVersion": message.get("params", {{}}).get(
                "protocolVersion", "2025-06-18"),
            "capabilities": {{"tools": {{}}}},
            "serverInfo": {{"name": "harness-facts-probe", "version": "1"}},
        }}
    elif method == "tools/list":
        result = {{"tools": []}}
    else:
        result = {{}}
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": message["id"],
                                  "result": result}}) + "\\n")
    sys.stdout.flush()
"""


def sha256(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def real_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def user_config_snapshot():
    home = real_home()
    return {relative: sha256(home / relative)
            for relative in USER_CONFIG + VOLATILE_USER_CONFIG}


def compare_snapshots(before, after):
    report = {}
    for relative, digest in before.items():
        report[f"~/{relative}"] = {
            "exists": digest is not None,
            "unchanged": digest == after.get(relative),
            "volatile": relative in VOLATILE_USER_CONFIG,
        }
    return report


def refuse_private_root(root):
    resolved = Path(root).resolve()
    home = real_home()
    for private in (home / ".claude", home / ".codex", home / ".pi",
                    home / ".claude.json", home / ".config"):
        if resolved == private or private in resolved.parents:
            raise SystemExit(f"refusing to use {root}: inside user config")


def fact(question, label, answer, **details):
    if label not in LABELS:
        raise ValueError(label)
    return {"question": question, "label": label, "answer": answer, **details}


class SyntheticProvider(http.server.BaseHTTPRequestHandler):
    """Anthropic Messages, OpenAI Responses and Chat Completions on loopback.

    A request whose last user text names TOOL_PROMPT and carries no tool
    result yet gets one shell or read tool call; everything else gets a short
    text answer.
    """

    requests = []
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        body = self._body()
        text = json.dumps(body)
        wants_tool = TOOL_PROMPT in text and not any(
            marker in text for marker in (
                '"tool_result"', '"function_call_output"', '"role": "tool"'))
        with self.lock:
            type(self).requests.append(self.path.split("?")[0])
        if self.path.startswith("/v1/messages"):
            self._anthropic(body, wants_tool)
        elif self.path.startswith("/v1/responses"):
            self._responses(wants_tool)
        elif self.path.startswith("/v1/chat/completions"):
            self._chat(wants_tool)
        else:
            self.do_GET()

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def _event(self, name, data):
        prefix = f"event: {name}\n" if name else ""
        self.wfile.write(f"{prefix}data: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()

    def _anthropic(self, body, wants_tool):
        if not body.get("stream"):
            content = [{"type": "text", "text": "Synthetic response."}]
            payload = json.dumps({
                "id": "msg_probe", "type": "message", "role": "assistant",
                "model": body.get("model", "fixture"), "content": content,
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self._stream()
        self._event("message_start", {"type": "message_start", "message": {
            "id": "msg_probe", "type": "message", "role": "assistant",
            "model": body.get("model", "fixture"), "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }})
        if wants_tool:
            block = {"type": "tool_use", "id": "toolu_probe", "name": "Bash",
                     "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(
                {"command": "touch probe-file", "description": "probe"})}
            reason = "tool_use"
        else:
            block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": "Synthetic response."}
            reason = "end_turn"
        self._event("content_block_start", {
            "type": "content_block_start", "index": 0, "content_block": block})
        self._event("content_block_delta", {
            "type": "content_block_delta", "index": 0, "delta": delta})
        self._event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": reason, "stop_sequence": None},
            "usage": {"output_tokens": 1},
        })
        self._event("message_stop", {"type": "message_stop"})

    def _responses(self, wants_tool):
        self._stream()
        self._event("response.created", {
            "type": "response.created", "response": {"id": "resp_probe"}})
        if wants_tool:
            item = {
                "type": "function_call", "id": "fc_probe", "call_id": "call_probe",
                "name": "exec_command",
                "arguments": json.dumps({
                    "cmd": "touch probe-file",
                    "sandbox_permissions": "require_escalated",
                    "justification": "probe",
                }),
            }
        else:
            item = {"type": "message", "role": "assistant", "id": "msg_probe",
                    "content": [{"type": "output_text", "text": "Synthetic response."}]}
        self._event("response.output_item.done", {
            "type": "response.output_item.done", "output_index": 0, "item": item})
        self._event("response.completed", {"type": "response.completed", "response": {
            "id": "resp_probe",
            "usage": {
                "input_tokens": 1, "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 2,
            },
        }})

    def _chat(self, wants_tool):
        self._stream()
        if wants_tool:
            delta = {"role": "assistant", "tool_calls": [{
                "index": 0, "id": "call_probe", "type": "function",
                "function": {"name": "read", "arguments": '{"path":"fixture.txt"}'},
            }]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Synthetic response."}
            finish = "stop"
        for content, reason in ((delta, None), ({}, finish)):
            self._event(None, {
                "id": "probe", "object": "chat.completion.chunk", "created": 1,
                "model": "fixture",
                "choices": [{"index": 0, "delta": content, "finish_reason": reason}],
            })
        self.wfile.write(b"data: [DONE]\n\n")


class Lab:
    """A private root with per-harness config dirs, recorder and provider."""

    def __init__(self, root, tracker):
        self.root = Path(root)
        self.tracker = tracker
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        (self.work / "fixture.txt").write_text("Public synthetic fixture.\n")
        self.log = self.root / "hooks.jsonl"
        self.recorder = self.bin / "fact-recorder"
        self.recorder.write_text(RECORDER.format(
            python=sys.executable, log=str(self.log), names=RECORDED_ENV))
        self.recorder.chmod(0o755)
        self.fake_mcp = self.bin / "fact-mcp"
        self.fake_mcp.write_text(FAKE_MCP.format(python=sys.executable))
        self.fake_mcp.chmod(0o755)
        SyntheticProvider.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SyntheticProvider)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def dir(self, *parts):
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def env(self, home, **extra):
        runtime = self.dir("runtime", home.name)
        runtime.chmod(0o700)
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "USER": os.environ.get("USER", "probe"),
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "TMPDIR": str(self.dir("tmp")),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "XDG_RUNTIME_DIR": str(runtime),
            "FLEET_DIR": str(self.dir("fleet")),
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "PI_OFFLINE": "1",
            "PI_TELEMETRY": "0",
        }
        env.update({key: str(value) for key, value in extra.items()})
        return env

    def records(self):
        try:
            lines = self.log.read_text().splitlines()
        except OSError:
            return []
        return [json.loads(line) for line in lines if line.strip()]

    def records_for(self, tag):
        return [record for record in self.records() if record["argv"][:1] == [tag]]

    def files(self, under):
        base = Path(under)
        return sorted(str(path.relative_to(base)) for path in base.rglob("*")
                      if path.is_file() and "backups" not in path.parts)

    def recorder_command(self, *tags):
        return shlex.join([str(self.recorder), *tags])


def launch_id():
    return secrets.token_hex(16)


def summarize_records(records, harness_pid=None):
    events = []
    for record in records:
        ancestors = record.get("ancestors", [])
        events.append({
            "event": record.get("event"),
            "env_visible": {name: record["env"].get(name) is not None
                            for name in RECORDED_ENV},
            "parent_comm": ancestors[0]["comm"] if ancestors else None,
            "ancestor_comms": [item["comm"] for item in ancestors[:6]],
            "harness_pid_is_ancestor": (
                None if harness_pid is None
                else any(item["pid"] == harness_pid for item in ancestors)),
        })
    return events


def env_matches(records, expected):
    if not records:
        return False
    return all(record["env"].get(name) == value
               for record in records for name, value in expected.items())


def claude_settings_hooks(lab, tag):
    hooks = {}
    for event in CLAUDE_EVENTS:
        hooks[event] = [{"hooks": [{
            "type": "command",
            "command": lab.recorder_command(tag, event),
            "timeout": 5,
        }]}]
    return {"hooks": hooks}


def run_claude_print(lab, env, prompt, timeout=RUN_TIMEOUT):
    argv = ["claude", "-p", "--output-format", "stream-json", "--verbose",
            "--model", "claude-sonnet-4-5", prompt]
    try:
        process = subprocess.Popen(
            argv, cwd=lab.work, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as error:
        return None, [], type(error).__name__, None
    lab.tracker.add(process.pid, "claude-print")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
    messages = []
    for line in stdout.splitlines():
        try:
            messages.append(json.loads(line))
        except ValueError:
            pass
    return process.returncode, messages, stderr[-2000:], process.pid


def probe_claude(lab, results):
    if shutil.which("claude") is None:
        results["claude"] = {"installed": False}
        return
    code, version, _ = run(["claude", "--version"], timeout=20)
    section = {"installed": True, "version": version if code == 0 else None}

    home = lab.dir("claude", "home-default")
    env = lab.env(home)
    before = lab.files(home)
    code, _, _ = run(["claude", "mcp", "add", "--scope", "user", "probe-cli",
                          "--", str(lab.fake_mcp)], env=env, timeout=60)
    section["mcp_add_default"] = fact(
        "Where does `claude mcp add --scope user` write without CLAUDE_CONFIG_DIR?",
        "live",
        sorted(set(lab.files(home)) - set(before)),
        exit_code=code,
    )

    home = lab.dir("claude", "home")
    config = lab.dir("claude", "config")
    env = lab.env(home, CLAUDE_CONFIG_DIR=config)
    before_home, before_config = lab.files(home), lab.files(config)
    code, _, _ = run(["claude", "mcp", "add", "--scope", "user", "probe-cli",
                          "--", str(lab.fake_mcp)], env=env, timeout=60)
    section["mcp_add_config_dir"] = fact(
        "Where does `claude mcp add --scope user` write with CLAUDE_CONFIG_DIR?",
        "live",
        {
            "config_dir_new_files": sorted(set(lab.files(config)) - set(before_config)),
            "home_new_files": sorted(set(lab.files(home)) - set(before_home)),
        },
        exit_code=code,
    )

    settings = claude_settings_hooks(lab, "claude-config")
    settings["mcpServers"] = {"probe-settings": {
        "command": str(lab.fake_mcp), "args": [], "env": {}}}
    (config / "settings.json").write_text(json.dumps(settings, indent=2))
    home_settings = claude_settings_hooks(lab, "claude-home")
    home_settings["mcpServers"] = {"probe-home-settings": {
        "command": str(lab.fake_mcp), "args": []}}
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(home_settings, indent=2))

    _, listing, _ = run(["claude", "mcp", "list"], env=env, timeout=90)
    get_cli = run(["claude", "mcp", "get", "probe-cli"], env=env, timeout=60)[0]
    get_settings = run(["claude", "mcp", "get", "probe-settings"], env=env, timeout=60)[0]
    section["mcp_cli_view"] = fact(
        "Which user MCP servers does the Claude CLI see?",
        "live",
        {
            "probe-cli (.claude.json)": "probe-cli" in listing,
            "probe-settings (settings.json mcpServers)": "probe-settings" in listing,
            "probe-home-settings (HOME/.claude/settings.json)":
                "probe-home-settings" in listing,
        },
        mcp_get_exit={"probe-cli": get_cli, "probe-settings": get_settings},
    )

    section["cli_subcommand_events"] = fact(
        "Do `claude mcp` subcommands fire settings.json hooks?",
        "live",
        sorted({record["event"] for record in lab.records_for("claude-config")}),
    )
    start = len(lab.records_for("claude-config"))
    identity = launch_id()
    session_env = lab.env(
        home, CLAUDE_CONFIG_DIR=config, ANTHROPIC_BASE_URL=lab.base_url,
        ANTHROPIC_API_KEY="synthetic-local-key",
        TONGS_AGENTS_LAUNCH_ID=identity, TONGS_AGENTS_PROFILE="probe")
    code, messages, stderr, pid = run_claude_print(lab, session_env, TOOL_PROMPT)
    init = next((message for message in messages
                 if message.get("type") == "system" and message.get("subtype") == "init"),
                {})
    servers = {server.get("name"): server.get("status")
               for server in init.get("mcp_servers", [])}
    section["mcp_session_view"] = fact(
        "Which user MCP servers does a Claude session load?",
        "live",
        {
            "probe-cli (.claude.json)": servers.get("probe-cli"),
            "probe-settings (settings.json mcpServers)": servers.get("probe-settings"),
            "probe-home-settings (HOME/.claude/settings.json)":
                servers.get("probe-home-settings"),
        },
        exit_code=code,
        init_seen=bool(init),
        stderr_tail=stderr if code else "",
    )
    config_records = lab.records_for("claude-config")[start:]
    section["hooks_relocated"] = fact(
        "Does CLAUDE_CONFIG_DIR relocate settings.json hooks?",
        "live",
        {
            "config_dir_hooks_fired": sorted({r["event"] for r in config_records}),
            "home_hooks_fired": sorted({r["event"] for r in lab.records_for("claude-home")}),
        },
    )
    section["hook_env"] = fact(
        "Do Claude hook subprocesses see the agent's launch env?",
        "live",
        env_matches(config_records, {
            "TONGS_AGENTS_LAUNCH_ID": identity,
            "TONGS_AGENTS_PROFILE": "probe",
            "CLAUDE_CONFIG_DIR": str(config),
        }),
        events=summarize_records(config_records, pid),
    )
    results["claude"] = section


def codex_config(lab, model_url):
    return (
        'model = "fixture"\n'
        'model_provider = "fixture"\n'
        'approval_policy = "on-request"\n'
        'check_for_update_on_startup = false\n'
        '\n[analytics]\nenabled = false\n'
        '\n[model_providers.fixture]\n'
        'name = "fixture"\n'
        f'base_url = "{model_url}/v1"\n'
        'wire_api = "responses"\n'
        'env_key = "PROBE_FIXTURE_KEY"\n'
        '\n[features]\nplugins = false\nplugin_discovery_prefetch = false\n'
        'remote_plugin = false\napps = false\n'
        f'\n[projects."{lab.work}"]\n'
        'trust_level = "trusted"\n'
    )


def codex_hooks(lab, tag):
    hooks = {}
    for event in CODEX_EVENTS:
        group = {"hooks": [{
            "type": "command",
            "command": lab.recorder_command(tag, event),
            "timeout": 3 if event == "SessionEnd" else 5,
        }]}
        if event == "SessionStart":
            group["matcher"] = "startup|resume|clear"
        hooks[event] = [group]
    return {"hooks": hooks}


class AppServer:
    """A `codex app-server` stdio JSON-RPC client."""

    def __init__(self, lab, env, args=()):
        self.process = subprocess.Popen(
            ["codex", *args, "app-server"], cwd=lab.work, env=env, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        lab.tracker.add(self.process.pid, "codex-app-server")
        self.next_id = 0
        self.request("initialize", {"clientInfo": {
            "name": "harness-facts", "title": "harness-facts", "version": "0"}})
        self.notify("initialized")

    def notify(self, method, params=None):
        message = {"method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        self.process.stdin.write(json.dumps(
            {"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if not line:
                return None
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id:
                return message
        return None

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def codex_hook_list(lab, env, args=()):
    server = AppServer(lab, env, args)
    try:
        reply = server.request("hooks/list", {"cwds": [str(lab.work)]})
    finally:
        server.close()
    if not reply or "result" not in reply:
        return []
    return [hook for entry in reply["result"].get("data", [])
            for hook in entry.get("hooks", [])]


def trust_codex_hooks(codex_home, hooks, base):
    """Record hooks as trusted in a private CODEX_HOME, as `/hooks` review does."""
    path = codex_home / "config.toml"
    lines = [base.rstrip("\n"), "", TRUST_MARKER, "[hooks.state]"]
    for hook in hooks:
        key = hook["key"].replace("\\", "\\\\").replace('"', '\\"')
        lines += ["", f'[hooks.state."{key}"]', f'trusted_hash = "{hook["currentHash"]}"']
    path.write_text("\n".join(lines) + "\n")


def run_codex_exec(lab, env, prompt, args=()):
    argv = ["codex", "exec", "--skip-git-repo-check", *args, prompt]
    try:
        process = subprocess.Popen(
            argv, cwd=lab.work, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as error:
        return None, type(error).__name__, None
    lab.tracker.add(process.pid, "codex-exec")
    try:
        _, stderr = process.communicate(timeout=RUN_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        _, stderr = process.communicate()
    return process.returncode, stderr, process.pid


def codex_owned_pids(codex_home):
    """Same-user processes whose environment names the private CODEX_HOME."""
    marker = f"CODEX_HOME={codex_home}".encode()
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            if os.stat(f"/proc/{entry}").st_uid != os.getuid():
                continue
            environ = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker in environ:
            found.append(int(entry))
    return found


def process_summary(pid):
    info = get_process_info(pid)
    if info is None:
        return None
    return {"comm": info.name, "exe": info.executable,
            "argv": list(info.argv[:3]), "ppid_comm": (
                get_process_info(info.ppid).name
                if info.ppid and get_process_info(info.ppid) else None)}


class CapturingPty(PtyClient):
    """A PtyClient that keeps the last screen bytes for failure reports."""

    def __init__(self, *args):
        self.tail = b""
        super().__init__(*args)

    def _drain(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self.tail = (self.tail + data)[-16384:]

    def screen_tail(self, limit=600):
        text = re.sub(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()=>][0-9A-Za-z]?",
                      b" ", self.tail).decode("utf-8", "replace")
        words = ["<path>" if "/" in word else word for word in text.split()]
        return " ".join(words)[-limit:]


def run_codex_tui(lab, env, label, args=(), prompt=TEXT_PROMPT, until="Stop",
                  settle=45):
    """Drive one interactive Codex turn in a private PTY and exit it."""
    start = len(lab.records_for(label))
    client = CapturingPty(["codex", *args, prompt], env, lab.work, lab.tracker, label)

    def seen():
        return any(record["event"] == until
                   for record in lab.records_for(label)[start:])

    finished = wait_until(seen, settle, 0.2)
    screen = "" if finished else client.screen_tail()
    owned = codex_owned_pids(env["CODEX_HOME"])
    for pid in owned:
        lab.tracker.add(pid, "codex-home")
    processes = [process_summary(pid) for pid in owned]
    for keys in ("\x1b", "\x03", "\x03", "\x04"):
        if process_identity(client.pid) is None:
            break
        try:
            os.write(client.fd, keys.encode())
        except OSError:
            break
        time.sleep(1.0)
    wait_until(lambda: process_identity(client.pid) is None, 10, 0.2)
    wait_until(lambda: any(record["event"] == "SessionEnd"
                           for record in lab.records_for(label)[start:]), 5, 0.2)
    return {
        "reached": bool(finished),
        "screen_tail_if_not_reached": screen,
        "tui_pid": client.pid,
        "processes": processes,
        "records": lab.records_for(label)[start:],
    }


def nearest_codex(record):
    return next((item["pid"] for item in record["ancestors"] if item["comm"] == "codex"),
                None)


def daemon_shape(summary):
    if not summary:
        return None
    argv = summary["argv"]
    if summary["comm"] != "codex" or len(argv) < 2 or argv[1] != "app-server":
        return None
    return {"argv": [os.path.basename(argv[0]), *argv[1:]],
            "managed_copy_in_codex_home": "/packages/app-server-daemon/" in argv[0]}


def stop_codex_daemon(lab, env):
    code, _, _ = run(["codex", "app-server", "daemon", "stop"], env=env, timeout=30)
    leftovers = codex_owned_pids(env["CODEX_HOME"])
    for pid in leftovers:
        lab.tracker.add(pid, "codex-home")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    wait_until(lambda: not codex_owned_pids(env["CODEX_HOME"]), 5, 0.1)
    return {"daemon_stop_exit": code, "leftover_processes": len(leftovers),
            "remaining": len(codex_owned_pids(env["CODEX_HOME"]))}


def probe_codex(lab, results, with_tui):
    if shutil.which("codex") is None:
        results["codex"] = {"installed": False}
        return
    code, version, _ = run(["codex", "--version"], timeout=20)
    section = {"installed": True, "version": version if code == 0 else None}

    home = lab.dir("codex", "home")
    codex_home = lab.dir("codex", "codex-home")
    base_config = codex_config(lab, lab.base_url)
    (codex_home / "config.toml").write_text(base_config)
    (codex_home / "hooks.json").write_text(json.dumps(codex_hooks(lab, "codex"), indent=2))
    (codex_home / "probe.config.toml").write_text('model_reasoning_effort = "low"\n')
    env = lab.env(home, CODEX_HOME=codex_home, PROBE_FIXTURE_KEY="synthetic-local-key")

    listed = codex_hook_list(lab, env)
    section["hooks_json_loaded"] = fact(
        "Is $CODEX_HOME/hooks.json honoured, and what trust state do new hooks get?",
        "live",
        {
            "events": sorted({hook["eventName"] for hook in listed}),
            "sources": sorted({hook["source"] for hook in listed}),
            "source_in_codex_home": all(
                Path(hook["sourcePath"]) == codex_home / "hooks.json" for hook in listed),
            "trust": sorted({hook["trustStatus"] for hook in listed}),
        },
    )

    code, stderr, _ = run_codex_exec(lab, env, TEXT_PROMPT)
    section["untrusted_hooks_run"] = fact(
        "Do untrusted hooks run?",
        "live",
        sorted({record["event"] for record in lab.records_for("codex")}),
        exit_code=code,
    )

    trust_codex_hooks(codex_home, listed, base_config)
    relisted = codex_hook_list(lab, env)
    identity = launch_id()
    exec_env = dict(env, TONGS_AGENTS_LAUNCH_ID=identity, TONGS_AGENTS_PROFILE="probe")
    start = len(lab.records_for("codex"))
    code, stderr, pid = run_codex_exec(lab, exec_env, TOOL_PROMPT)
    exec_records = lab.records_for("codex")[start:]
    section["exec_events"] = fact(
        "Which hooks fire in a trusted `codex exec` turn with an escalation request?",
        "live",
        sorted({record["event"] for record in exec_records}),
        exit_code=code,
        trust_after_review=sorted({hook["trustStatus"] for hook in relisted}),
        stderr_mentions_approval="approval" in stderr.lower(),
    )
    section["exec_hook_env"] = fact(
        "Do `codex exec` hook subprocesses see the agent's launch env?",
        "live",
        env_matches(exec_records, {
            "TONGS_AGENTS_LAUNCH_ID": identity, "CODEX_HOME": str(codex_home)}),
        events=summarize_records(exec_records, pid),
    )

    start = len(lab.records_for("codex"))
    code, stderr, _ = run_codex_exec(lab, exec_env, TEXT_PROMPT, ("--profile", "probe"))
    profile_records = lab.records_for("codex")[start:]
    section["profile_shares_hooks"] = fact(
        "Does `codex --profile X` run the base CODEX_HOME hooks?",
        "live",
        sorted({record["event"] for record in profile_records}),
        exit_code=code,
        profile_file="$CODEX_HOME/probe.config.toml",
    )

    before = sha256(codex_home / "config.toml")
    code, _, _ = run(["codex", "mcp", "add", "probe-cli", "--", str(lab.fake_mcp)],
                       env=env, timeout=30)
    get_code, _, _ = run(["codex", "mcp", "get", "probe-cli", "--json"],
                           env=env, timeout=30)
    text = (codex_home / "config.toml").read_text()
    section["mcp_add_codex_home"] = fact(
        "Does `codex mcp add` respect CODEX_HOME?",
        "live",
        {
            "config_toml_changed": sha256(codex_home / "config.toml") != before,
            "entry_in_codex_home_config": "[mcp_servers.probe-cli]" in text,
            "home_dot_codex_created": (home / ".codex").exists(),
            "mcp_get_exit": get_code,
        },
        exit_code=code,
    )

    if with_tui:
        tui_env = dict(env, TONGS_AGENTS_PROFILE="probe")
        runs = (
            ("tui-1", (), TEXT_PROMPT, "Stop",
             "default launch, no daemon running yet"),
            ("tui-2", (), TEXT_PROMPT, "Stop",
             "default launch while the daemon from tui-1 runs"),
            ("tui-3", ("--no-daemon",), TEXT_PROMPT, "Stop",
             "--no-daemon while the daemon runs"),
            ("tui-3b", ("--disable", "daemon_auto_start"), TEXT_PROMPT, "Stop",
             "daemon_auto_start disabled while the daemon runs"),
            ("stop-daemon", None, None, None, None),
            ("tui-4", ("--disable", "daemon_auto_start"), TEXT_PROMPT, "Stop",
             "daemon_auto_start disabled, no daemon running"),
            ("tui-5", ("--no-daemon",), TOOL_PROMPT, "PermissionRequest",
             "--no-daemon, escalated command needing approval"),
        )
        identities = {}
        tui = {}
        for label, args, prompt, until, note in runs:
            if args is None:
                tui[label] = stop_codex_daemon(lab, env)
                continue
            identity = launch_id()
            identities[identity] = label
            (codex_home / "hooks.json").write_text(
                json.dumps(codex_hooks(lab, label), indent=2))
            trust_codex_hooks(codex_home, codex_hook_list(lab, env), base_config)
            outcome = run_codex_tui(
                lab, dict(tui_env, TONGS_AGENTS_LAUNCH_ID=identity), label, args,
                prompt, until)
            records = outcome["records"]
            seen = sorted({record["env"].get("TONGS_AGENTS_LAUNCH_ID")
                           for record in records} - {None})
            tui[label] = {
                "setup": note,
                "args": list(args),
                "reached": {until: outcome["reached"]},
                "screen_tail_if_not_reached": outcome["screen_tail_if_not_reached"],
                "events": sorted({record["event"] for record in records}),
                "launch_ids_seen": [identities.get(value, "other") for value in seen],
                "own_launch_id_seen": identity in seen,
                "nearest_codex_ancestor_is_tui": sorted({
                    nearest_codex(record) == outcome["tui_pid"] for record in records}),
                "hook_ancestor_comms": sorted({
                    " < ".join(item["comm"] for item in record["ancestors"][:3])
                    for record in records}),
                "daemon_processes": sorted(
                    {json.dumps(shape, sort_keys=True) for shape in map(
                        daemon_shape, outcome["processes"]) if shape}),
                "other_codex_home_comms": sorted({
                    item["comm"] for item in outcome["processes"]
                    if item and item["comm"] != "codex"}),
            }
        tui["cleanup"] = stop_codex_daemon(lab, env)
        section["tui_hook_env"] = fact(
            "Does an interactive Codex TUI pass its own env to hook subprocesses?",
            "live", tui)
    results["codex"] = section


def probe_pi(lab, results):
    if shutil.which("pi") is None:
        results["pi"] = {"installed": False}
        return
    code, version, _ = run(["pi", "--version"], timeout=30)
    section = {"installed": True, "version": version if code == 0 else None}
    home = lab.dir("pi", "home")
    config = lab.dir("pi", "agent")
    sessions = lab.dir("pi", "sessions")
    hook = lab.bin / "tongs-agents-hook"
    hook.write_text(f"#!/bin/sh\nexec {shlex.quote(str(lab.recorder))} pi \"$@\"\n")
    hook.chmod(0o755)
    (config / "settings.json").write_text(json.dumps(
        {"retry": {"enabled": False}, "compaction": {"enabled": False}}, indent=2))
    (config / "models.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": f"{lab.base_url}/v1",
        "api": "openai-completions",
        "apiKey": "synthetic-local-key",
        "models": [{"id": "fixture", "contextWindow": 128000, "maxTokens": 2048,
                    "reasoning": False}],
    }}}))
    env = lab.env(home, PI_CODING_AGENT_DIR=config)
    code, _, _ = run([sys.executable, "-m", "tongs_agents.cli", "install",
                          "--agent", "pi"], env=env, timeout=120)
    section["extension_install"] = {"exit_code": code}

    identity = launch_id()
    session_env = dict(env, TONGS_AGENTS_LAUNCH_ID=identity, TONGS_AGENTS_PROFILE="probe")
    argv = ["pi", "-p", "--provider", "fixture", "--model", "fixture", "--offline",
            "--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
            "--no-approve", "--session-dir", str(sessions), TOOL_PROMPT]
    try:
        process = subprocess.Popen(argv, cwd=lab.work, env=session_env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        lab.tracker.add(process.pid, "pi")
        try:
            process.communicate(timeout=RUN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        pi_code, pi_pid = process.returncode, process.pid
    except OSError as error:
        pi_code, pi_pid = type(error).__name__, None
    wait_until(lambda: any(r["event"] == "pi.session-shutdown"
                           for r in lab.records_for("pi")), 5, 0.2)
    records = lab.records_for("pi")
    section["extension_events"] = fact(
        "Which extension events reach the hook on the installed Pi?",
        "live",
        sorted({record["event"] for record in records if record["event"]}),
        exit_code=pi_code,
    )
    section["hook_env"] = fact(
        "Does the Pi extension's hook spawn see Pi's launch env?",
        "live",
        env_matches(records, {
            "TONGS_AGENTS_LAUNCH_ID": identity, "PI_CODING_AGENT_DIR": str(config)}),
        events=summarize_records(records, pi_pid),
    )

    code, _, _ = run(["pi", "mcp", "add", "probe-cli", "--", str(lab.fake_mcp)],
                         env=env, timeout=60)
    section["mcp_add_agent_dir"] = fact(
        "Does `pi mcp add` write under PI_CODING_AGENT_DIR?",
        "live",
        {
            "agent_dir_mcp_json": (config / "mcp.json").exists()
            and "probe-cli" in (config / "mcp.json").read_text(),
            "home_dot_pi_created": (home / ".pi").exists(),
        },
        exit_code=code,
    )

    package = Path(os.path.realpath(shutil.which("pi"))).parents[2]
    changelog = package / "CHANGELOG.md"
    floor = None
    try:
        current = None
        for line in changelog.read_text().splitlines():
            heading = re.match(r"## \[(\d+\.\d+\.\d+)\]", line)
            if heading:
                current = heading.group(1)
            elif "MCP support as built-in" in line or "MCP servers over stdio" in line:
                floor = current
    except OSError:
        pass
    pin = None
    try:
        found = re.search(r"pi-coding-agent@(\d+\.\d+\.\d+)",
                          (REPO_ROOT / ".github" / "workflows" / "pi.yml").read_text())
        pin = found.group(1) if found else None
    except OSError:
        pass
    section["native_mcp_floor"] = fact(
        "Which Pi version first ships native MCP, versus the CI pin?",
        "unvalidated",
        {"first_native_mcp_release": floor, "ci_pin": pin},
        source="installed package CHANGELOG.md",
    )
    extension = (REPO_ROOT / "src" / "tongs_agents" / "pi_extension" / "extensions"
                 / "fleet-monitor.js").read_text()
    used = sorted(set(re.findall(r'pi\.on\("([a-z_]+)"', extension)))
    try:
        docs = (package / "docs" / "extensions.md").read_text()
    except OSError:
        docs = ""
    section["event_names_documented"] = fact(
        "Are the extension's event names in the installed Pi's extension docs?",
        "unvalidated",
        {name: bool(re.search(rf"\b{name}\b", docs)) for name in used},
    )
    results["pi"] = section


def probe_ancestors():
    """The probe's own ancestry, which the hook walk must not escape into."""
    found = set()
    pid = os.getpid()
    while pid and pid > 1 and pid not in found:
        found.add(pid)
        info = get_process_info(pid)
        pid = info.ppid if info else 0
    return found


def identity_case(lab, label, argv, env, harness):
    try:
        process = subprocess.Popen(argv, cwd=lab.work, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        return {"error": type(error).__name__}
    lab.tracker.add(process.pid, label)
    time.sleep(2.5)
    lab.tracker.sweep()
    started = get_process_info(process.pid)
    children = [pid for pid in lab.tracker.alive()
                if (get_process_info(pid) or started).ppid == process.pid]
    harness_process = None
    for pid in [process.pid, *children]:
        info = get_process_info(pid)
        if info and process_matches_harness(info, harness):
            harness_process = info
            break
    walk_from = harness_process.pid if harness_process else process.pid
    found = find_ancestor_process(harness, walk_from)
    if found is not None and found.pid in probe_ancestors():
        found = None
    outcome = {
        "launched": {"comm": started.name, "exe": started.executable,
                     "argv0": os.path.basename(started.argv[0]) if started.argv else None}
        if started else None,
        "launched_matches": bool(started and process_matches_harness(started, harness)),
        "harness_process_found": harness_process is not None,
        "harness_pid_is_launched_pid": bool(
            harness_process and harness_process.pid == process.pid),
        "hook_walk_finds": found is not None,
        "hook_walk_pid_is_launched_pid": bool(found and found.pid == process.pid),
        "matched_on": None,
    }
    if harness_process:
        names = {name.lower() for name in ("claude", "claude.exe", "codex", "codex.exe")}
        outcome["matched_on"] = [
            field for field, value in (
                ("comm", harness_process.name),
                ("exe", harness_process.executable),
                ("argv0", os.path.basename(harness_process.argv[0])
                 if harness_process.argv else ""),
            ) if value.lower() in names]
    try:
        process.stdin.close()
    except OSError:
        pass
    for pid in [*children, process.pid]:
        birth = process_identity(pid)
        if birth is not None and lab.tracker.owned.get(pid, (None,))[0] == birth:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    try:
        process.wait(5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return outcome


def probe_identity(lab, results):
    section = {}
    for harness, args in (("claude", ("mcp", "serve")), ("codex", ("app-server",))):
        binary = shutil.which(harness)
        if binary is None:
            section[harness] = {"installed": False}
            continue
        home = lab.dir("identity", harness, "home")
        config = lab.dir("identity", harness, "config")
        variable = "CLAUDE_CONFIG_DIR" if harness == "claude" else "CODEX_HOME"
        if harness == "codex":
            (config / "config.toml").write_text(codex_config(lab, lab.base_url))
        env = lab.env(home, **{variable: config})
        wrappers = lab.dir("identity", harness, "bin")
        exec_wrapper = wrappers / f"{harness}-work"
        exec_wrapper.write_text(
            f"#!/bin/sh\n{variable}={shlex.quote(str(config))} exec {harness} \"$@\"\n")
        plain_wrapper = wrappers / f"{harness}-plain"
        plain_wrapper.write_text(
            f"#!/bin/sh\n{variable}={shlex.quote(str(config))} {harness} \"$@\"\n")
        for script in (exec_wrapper, plain_wrapper):
            script.chmod(0o755)
        alias_link = wrappers / f"{harness}-personal"
        alias_link.symlink_to(os.path.realpath(binary))
        alias_line = (f"shopt -s expand_aliases; alias {harness}-alias="
                      f"'{variable}={shlex.quote(str(config))} {harness}'\n"
                      f"{harness}-alias {' '.join(args)}\n")
        cases = {
            "path_command": [harness, *args],
            "env_only_profile": [harness, *args],
            "exec_wrapper": [str(exec_wrapper), *args],
            "non_exec_wrapper": [str(plain_wrapper), *args],
            "renamed_symlink": [str(alias_link), *args],
            "resolved_binary": [os.path.realpath(binary), *args],
            "shell_alias": ["bash", "-c", alias_line],
        }
        section[harness] = {
            "binary_target_basename": os.path.basename(os.path.realpath(binary)),
            "cases": {label: identity_case(lab, f"{harness}-{label}", argv, env, harness)
                      for label, argv in cases.items()},
        }
    results["identity"] = fact(
        "What does discovery.process_matches_harness see for wrapper styles?",
        "live", section,
        method="real binaries kept alive on stdin; tongs_agents.discovery functions",
    )


def summarize(results):
    env = {}
    claude = results.get("claude", {})
    if "hook_env" in claude:
        env["claude"] = claude["hook_env"]["answer"]
    codex = results.get("codex", {})
    if "exec_hook_env" in codex:
        env["codex_exec"] = codex["exec_hook_env"]["answer"]
    if "tui_hook_env" in codex:
        env["codex_tui"] = {
            label: run_["own_launch_id_seen"]
            for label, run_ in codex["tui_hook_env"]["answer"].items()
            if isinstance(run_, dict) and "own_launch_id_seen" in run_}
    pi = results.get("pi", {})
    if "hook_env" in pi:
        env["pi"] = pi["hook_env"]["answer"]
    return env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--only", action="append", choices=SECTIONS,
        help="run only these sections (repeatable); 'env' is covered by the others")
    parser.add_argument("--no-tui", action="store_true",
                        help="skip the interactive Codex PTY runs")
    parser.add_argument("--output", type=Path,
                        help="write redacted evidence JSON here instead of stdout")
    parser.add_argument("--workdir", type=Path,
                        help="parent for the private temporary root")
    parser.add_argument("--keep", action="store_true",
                        help="keep the temporary root for inspection")
    args = parser.parse_args(argv)
    if sys.platform != "linux":
        raise SystemExit("harness_facts.py is a Linux-only helper")
    sections = set(args.only or SECTIONS)
    if args.workdir:
        refuse_private_root(args.workdir)
    if args.output:
        refuse_private_root(args.output.parent)

    before = user_config_snapshot()
    root = Path(tempfile.mkdtemp(prefix="harness-facts-", dir=args.workdir))
    refuse_private_root(root)
    tracker = Tracker([str(root)])
    lab = Lab(root, tracker)
    results = {}
    try:
        if "claude" in sections or "env" in sections:
            probe_claude(lab, results)
        if "codex" in sections or "env" in sections:
            probe_codex(lab, results, with_tui=not args.no_tui)
        if "pi" in sections or "env" in sections:
            probe_pi(lab, results)
        if "identity" in sections:
            probe_identity(lab, results)
    finally:
        cleanup = tracker.terminate()
        lab.close()
    results["hook_env_summary"] = summarize(results)
    evidence = {
        "probe": "validation/harness_facts.py",
        "probe_sha256": sha256(__file__),
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": {"system": sys.platform, "python": sys.version.split()[0]},
        "provider_paths": sorted(set(SyntheticProvider.requests)),
        "results": results,
        "cleanup": cleanup,
        "user_config": compare_snapshots(before, user_config_snapshot()),
    }
    redact = Redactor.for_host({str(root): "<tmp>", str(root.parent): "<workdir>",
                                str(real_home()): "<home>"})
    text = redact(json.dumps(evidence, indent=2, sort_keys=True))
    leaks = redact.leaks(text)
    if leaks:
        raise SystemExit(f"redaction failed: {leaks}")
    if args.keep:
        print(f"kept {root}", file=sys.stderr)
    else:
        shutil.rmtree(root, ignore_errors=True)
    if args.output:
        args.output.write_text(text + "\n")
    else:
        print(text)
    changed = [name for name, item in evidence["user_config"].items()
               if not item["unchanged"] and not item["volatile"]]
    return 1 if changed or cleanup["survivors"] else 0


if __name__ == "__main__":
    sys.exit(main())
