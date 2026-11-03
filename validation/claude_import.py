"""Confirm that Claude Code loads the emitted ``@tongs-agents/instructions.md`` import.

Decision row 2 of docs/work/emitter-strategy.md: the Claude emitter writes an
owned ``<claude>/tongs-agents/instructions.md`` and one ``@`` import line in
``<claude>/CLAUDE.md``. No offline command renders Claude's memory, so this
check starts a real ``claude -p`` session against a fake provider: a local
stub of the Messages API on 127.0.0.1 that answers every request with a fixed
reply and records only whether a request body contains a synthetic token. No
model runs and no credential exists; the API key is a fixed fake string.

Isolation: a private temporary root holds HOME, CLAUDE_CONFIG_DIR, the XDG
dirs, TMPDIR and the tongs-agents state dir. On Linux with ``unshare``
available the whole check re-runs inside a new user and network namespace
with only loopback up, so nothing can leave the host. Two sessions run:

- with the import line: the token from the dotfiles instructions must reach
  the provider;
- control, the import line removed and the owned file kept: it must not.

The emitter also wrote the monitoring hooks, so the session records written by
tongs-agents-hook in the private state dir are reported too. Evidence is
redacted before it is printed.
"""

import argparse
import hashlib
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from unittest.mock import patch

from tongs_agents.config_layer.emitters import EMITTERS, EmitConfig, apply_plan
from tongs_agents.config_layer.emitters.claude import IMPORT_LINE
from tongs_agents.config_layer.protocol import ProfileTarget

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "claude-import-validation/1"
INNER_FLAG = "--inner"
FAKE_KEY = "sk-ant-fake-provider-not-a-credential"
REPLY = "fake provider reply"
USER_FILES = (".claude/settings.json", ".claude.json", ".claude/CLAUDE.md")


class Check(Exception):
    """A validation expectation that did not hold."""


def expect(condition, message):
    if not condition:
        raise Check(message)


def sse(events):
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)


def message(model):
    return {"id": "msg_fake", "type": "message", "role": "assistant", "model": model,
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}}


class FakeProvider(http.server.ThreadingHTTPServer):
    """A Messages API stub that remembers which requests carried the token."""

    daemon_threads = True

    def __init__(self, token):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.token = token.encode()
        self.requests = []
        self.lock = threading.Lock()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def _record(self, body):
        with self.server.lock:
            self.server.requests.append({
                "method": self.command,
                "path": self.path.split("?")[0],
                "has_token": self.server.token in body,
            })

    def do_GET(self):
        self._record(b"")
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_HEAD = do_GET

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self._record(body)
        try:
            request = json.loads(body or b"{}")
        except ValueError:
            request = {}
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            payload, kind = json.dumps({"input_tokens": 1}), "application/json"
        elif path.endswith("/v1/messages"):
            model = request.get("model", "fake")
            if request.get("stream"):
                kind = "text/event-stream"
                payload = sse([
                    ("message_start", {"type": "message_start", "message": message(model)}),
                    ("content_block_start", {"type": "content_block_start", "index": 0,
                                             "content_block": {"type": "text", "text": ""}}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                             "delta": {"type": "text_delta", "text": REPLY}}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta",
                                       "delta": {"stop_reason": "end_turn",
                                                 "stop_sequence": None},
                                       "usage": {"output_tokens": 1}}),
                    ("message_stop", {"type": "message_stop"}),
                ])
            else:
                kind = "application/json"
                payload = json.dumps({**message(model), "stop_reason": "end_turn",
                                      "content": [{"type": "text", "text": REPLY}]})
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def digests(home):
    result = {}
    for relative in USER_FILES:
        try:
            result[relative] = hashlib.sha256((Path(home) / relative).read_bytes()).hexdigest()
        except OSError:
            result[relative] = None
    return result


def isolated_env(root, provider_url):
    env = {key: os.environ[key] for key in ("LANG", "USER", "LOGNAME") if key in os.environ}
    for name in ("home", "claude", "xdg-config", "xdg-state", "xdg-cache", "xdg-data",
                 "tmp", "work", "state"):
        (root / name).mkdir(mode=0o700)
    env.update({
        "PATH": os.pathsep.join((os.path.dirname(sys.executable), "/usr/bin", "/bin")),
        "HOME": str(root / "home"),
        "CLAUDE_CONFIG_DIR": str(root / "claude"),
        "XDG_CONFIG_HOME": str(root / "xdg-config"),
        "XDG_STATE_HOME": str(root / "xdg-state"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "TMPDIR": str(root / "tmp"),
        "TONGS_AGENTS_STATE_DIR": str(root / "state"),
        "ANTHROPIC_BASE_URL": provider_url,
        "ANTHROPIC_API_KEY": FAKE_KEY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
    })
    return env


def write_dotfiles(root, token):
    dotfiles = root / "dotfiles"
    (dotfiles / "instructions").mkdir(parents=True)
    (dotfiles / "agentic.toml").write_text(
        'schema = "tongs-agents-dotfiles/1"\n\n[instructions]\n'
        'files = ["instructions/common.md"]\n')
    (dotfiles / "instructions" / "common.md").write_text(
        f"# Synthetic instructions\n\nThe check token is {token}.\n")
    return dotfiles


def session(claude, env, root, provider, label, observations):
    start = len(provider.requests)
    result = subprocess.run(
        [claude, "-p", "Reply with ok.", "--output-format", "json"],
        cwd=root / "work", env=env, capture_output=True, text=True, timeout=180,
    )
    requests = provider.requests[start:]
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = {}
    observations[label] = {
        "exit_code": result.returncode,
        "reply_is_fake_provider": output.get("result") == REPLY,
        "requests": len(requests),
        "message_requests": sum(item["path"].endswith("/v1/messages") for item in requests),
        "token_in_a_request": any(item["has_token"] for item in requests),
        "stderr_tail": result.stderr.strip().splitlines()[-1:] if result.returncode else [],
    }
    return observations[label]


def exercise(root, token, observations):
    claude = shutil.which("claude")
    expect(claude is not None, "claude is not on PATH")
    claude = os.path.realpath(claude)
    provider = FakeProvider(token)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    try:
        env = isolated_env(root, provider.url)
        version = subprocess.run([claude, "--version"], env=env, capture_output=True,
                                 text=True, timeout=60)
        observations["claude_version"] = version.stdout.strip()
        dotfiles = write_dotfiles(root, token)
        directory = root / "claude"
        target = ProfileTarget("claude", directory)
        with patch.dict(os.environ, env, clear=True):
            document = tomllib.loads((dotfiles / "agentic.toml").read_text())
            config = EmitConfig.from_document(document, dotfiles, profiles={
                "claude-alt": {"command": ["claude"], "config_dir": str(directory)}})
            plan = EMITTERS["claude"].plan_full(config, target)
            result = apply_plan(plan)
        claude_md = directory / "CLAUDE.md"
        owned_file = directory / "tongs-agents" / "instructions.md"
        observations["emit"] = {
            "conflicts": plan.conflicts,
            "failed_steps": result.failed_steps,
            "claude_md": claude_md.read_text(),
            "instructions_file_has_token": token in owned_file.read_text(),
            "settings_has_hooks": "tongs-agents-hook" in (directory / "settings.json").read_text(),
        }
        expect(claude_md.read_text() == IMPORT_LINE + "\n", "CLAUDE.md is not the import line")
        with_import = session(claude, env, root, provider, "with_import", observations)
        claude_md.write_text("# control: no import\n")
        control = session(claude, env, root, provider, "control_without_import", observations)
        records = []
        sessions = root / "state" / "sessions"
        for path in sorted(sessions.glob("*.json")) if sessions.is_dir() else []:
            try:
                records.append(json.loads(path.read_text()).get("status"))
            except (OSError, ValueError):
                records.append("unreadable")
        observations["hook_session_records"] = {"count": len(records),
                                                "statuses": sorted(set(map(str, records)))}
    finally:
        provider.shutdown()
    expect(with_import["exit_code"] == 0 and with_import["reply_is_fake_provider"],
           "the session did not complete against the fake provider")
    expect(with_import["token_in_a_request"], "the imported instructions did not reach the model")
    expect(control["exit_code"] == 0 and not control["token_in_a_request"],
           "the control session saw the token without the import")


def in_namespace():
    return os.environ.get("TONGS_AGENTS_IMPORT_CHECK_NS") == "1"


def redact(text, replacements):
    for value, label in sorted(replacements.items(), key=lambda item: -len(item[0])):
        if value:
            text = text.replace(value, label)
    return text


def validate(output=None):
    token = f"TONGS-IMPORT-CHECK-{secrets.token_hex(8)}"
    real_home = str(Path.home())
    before = digests(real_home)
    root = Path(tempfile.mkdtemp(prefix="tongs-claude-import-"))
    observations = {}
    started = time.monotonic()
    try:
        if sys.platform == "linux" and in_namespace():
            subprocess.run(["ip", "link", "set", "lo", "up"], check=True, timeout=10)
        exercise(root, token, observations)
        error = None
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
    finally:
        shutil.rmtree(root, ignore_errors=True)
    commit = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    evidence = {
        "schema": SCHEMA,
        "result": "pass" if error is None else "fail",
        "error": error,
        "provenance": "live",
        "provenance_detail": (
            "real claude -p sessions against a local fake Messages API (no model, fake "
            "key); config emitted by the Claude emitter into a private CLAUDE_CONFIG_DIR"),
        "network": ("user and network namespace, loopback only" if in_namespace()
                    else "host network; ANTHROPIC_BASE_URL on 127.0.0.1"),
        "decision": "emitter-strategy row 2",
        **observations,
        "user_config_unchanged": digests(real_home) == before,
        "temp_dir_removed": not root.exists(),
        "duration_seconds": round(time.monotonic() - started, 1),
        "tested_commit": commit.stdout.strip() if commit.returncode == 0 else None,
    }
    if not evidence["user_config_unchanged"]:
        evidence["result"] = "fail"
    text = redact(json.dumps(evidence, indent=2) + "\n", {
        str(root): "<tmp>", sys.executable: "<python>", str(REPO_ROOT): "<repo>",
        real_home: "<home>", token: "<token>",
    })
    if "/home/" in text or "/tmp/" in text:
        raise RuntimeError("refusing to print evidence with an absolute path")
    if output:
        Path(output).write_text(text, encoding="utf-8")
    print(text, end="")
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", help="also write the evidence to this file")
    parser.add_argument("--host-network", action="store_true",
                        help="do not re-run inside a private network namespace")
    parser.add_argument(INNER_FLAG, action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    unshare = shutil.which("unshare")
    if (sys.platform == "linux" and unshare and not arguments.inner
            and not arguments.host_network):
        command = [unshare, "--user", "--map-root-user", "--net", sys.executable,
                   str(Path(__file__).resolve()), INNER_FLAG]
        if arguments.out:
            command += ["--out", arguments.out]
        env = {**os.environ, "TONGS_AGENTS_IMPORT_CHECK_NS": "1"}
        return subprocess.run(command, env=env).returncode
    evidence = validate(arguments.out)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
