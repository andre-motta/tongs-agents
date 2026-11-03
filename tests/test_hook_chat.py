"""Mention delivery through the hook and the Pi extension (M5-C, #32).

In-process tests use a fake process table; the subprocess tests run the real
hook (and the Pi extension under Node) against real process ancestry with a
launch record claiming this pytest process, inside the private state dir.
No harness, model or multiplexer runs.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from tests.test_chat_identity import AGENT_PID, LAUNCH, START, FakeProcess, record
from tests.test_pi_extension import extension_path
from tongs_agents import discovery, hook, launches
from tongs_agents.chat import delivery, frame, identity
from tongs_agents.chat.identity import IdentityError, verify_agent
from tongs_agents.chat.store import ChatStore
from tongs_agents.models import LAUNCH_ID_ENV, PROFILE_ID_ENV

NODE = shutil.which("node")
LINUX = sys.platform == "linux"
HOOK_PID = os.getpid()
LAUNCH_INFO = {"launch_id": LAUNCH, "profile_id": "lead"}
NOW = 10_000_000


@pytest.fixture
def processes(monkeypatch):
    table = {
        AGENT_PID: FakeProcess(AGENT_PID, START, 1),
        HOOK_PID: FakeProcess(HOOK_PID, "hook", AGENT_PID),
    }
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    return table


@pytest.fixture
def launched(monkeypatch, processes):
    record()
    monkeypatch.setenv(LAUNCH_ID_ENV, LAUNCH)
    monkeypatch.setenv(PROFILE_ID_ENV, "lead")
    monkeypatch.setattr(hook, "CHAT_SUPPORTED", True)
    return processes


def grant(level="chat", start=START, handle="lead", launch_id=LAUNCH):
    with ChatStore.maintainer(create=True) as current:
        current.put_grant(
            launch_id=launch_id, handle=handle, level=level, process_start=start,
            pane_session="agents", pane_id="1", socket_dir=None, via="cli",
            profile=handle, harness="claude",
        )


def post(body, **kwargs):
    with ChatStore.maintainer() as current:
        return current.post("general", body, **kwargs)


def output(event, agent="claude", *, now=NOW, point=None):
    return delivery.hook_output(agent, event, LAUNCH_INFO, point=point, now=now)


def context(text):
    data = json.loads(text)
    assert list(data) == ["hookSpecificOutput"]
    assert list(data["hookSpecificOutput"]) == ["hookEventName", "additionalContext"]
    return data["hookSpecificOutput"]["hookEventName"], data["hookSpecificOutput"][
        "additionalContext"
    ]


# The cheap checks in hook.py


def test_a_session_tongs_agents_did_not_launch_touches_no_chat_file(monkeypatch):
    monkeypatch.delenv(LAUNCH_ID_ENV, raising=False)
    monkeypatch.setattr(hook, "_chat_file_exists",
                        lambda: pytest.fail("a session without a launch id looked for chat"))
    for event in ("session-start", "prompt-submit", "tool-use"):
        assert hook.chat_output(event, "claude") == ""


def test_only_the_injection_events_of_each_harness_reach_chat(launched, monkeypatch):
    grant()
    post("@lead hello")
    calls = []
    monkeypatch.setattr(delivery, "hook_output",
                        lambda *args, **kwargs: calls.append(args[:2]) or "x")
    for event in ("stop", "session-end", "permission-request", "chat-digest"):
        assert hook.chat_output(event, "claude") == ""
    assert hook.chat_output("prompt-submit", "other") == ""
    assert hook.chat_output("prompt-submit", "pi") == ""
    assert hook.chat_output("tool-use", "codex") == "x"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"point": "before_agent_start"}'))
    assert hook.chat_output("chat-digest", "pi") == "x"
    assert calls == [("codex", "tool-use"), ("pi", "chat-digest")]


def test_chat_stays_off_where_the_identity_walk_would_need_ps(launched, monkeypatch):
    grant()
    post("@lead hello")
    assert hook.chat_output("prompt-submit", "claude")
    monkeypatch.setattr(hook, "CHAT_SUPPORTED", False)
    assert hook.chat_output("prompt-submit", "claude") == ""


def test_any_chat_failure_means_no_output(launched, monkeypatch):
    grant()

    def broken(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(delivery, "hook_output", broken)
    assert hook.chat_output("prompt-submit", "claude") == ""


def run_main(monkeypatch, capsys, event, agent="claude"):
    monkeypatch.setattr("sys.argv", ["tongs-agents-hook", event, "--agent", agent])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"session_id": "chat-sess", "cwd": "/tmp/repo"}
    )))
    monkeypatch.setattr(hook, "resolve_process_identity", lambda *args: None)
    monkeypatch.setattr("tongs_agents.terminal_apis.capture_terminal_info",
                        lambda: {"terminal": "test", "terminal_env": {}})
    hook.main()
    return capsys.readouterr().out


def test_main_prints_nothing_for_an_ungranted_session(launched, monkeypatch, capsys):
    assert run_main(monkeypatch, capsys, "prompt-submit") == ""
    grant(launch_id="f" * 32, handle="peer")
    post("@peer hello")
    assert run_main(monkeypatch, capsys, "prompt-submit") == ""
    grant()
    post("@lead hello")
    name, text = context(run_main(monkeypatch, capsys, "prompt-submit"))
    assert name == "UserPromptSubmit" and "@lead hello" in text


# Identity, as the hook checks it


def test_the_hook_identity_follows_the_mcp_identity_steps(processes):
    def both(pid=HOOK_PID, info=LAUNCH_INFO):
        env = {identity.LAUNCH_ENV: info["launch_id"], identity.PROFILE_ENV: info["profile_id"]}
        try:
            verify_agent(env, pid)
            agent = True
        except IdentityError:
            agent = False
        found = delivery.verify_hook(info, pid)
        assert (found is not None) == agent
        return found

    assert both() is None
    record(state="pending")
    assert both() is None
    record(profile="other")
    assert both() is None
    record(start=None)
    assert both() is None
    record()
    found = both()
    assert (found.launch_id, found.profile, found.process_start) == (LAUNCH, "lead", START)
    assert both(info={"launch_id": LAUNCH, "profile_id": "Bad"}) is None
    processes[HOOK_PID] = FakeProcess(HOOK_PID, "hook", 1)
    assert both() is None
    processes[HOOK_PID] = FakeProcess(HOOK_PID, "hook", AGENT_PID)
    processes[AGENT_PID] = FakeProcess(AGENT_PID, "reused", 1)
    assert both() is None
    processes[AGENT_PID] = FakeProcess(AGENT_PID, START, 1)
    record("f" * 32, replaces=LAUNCH, state="pending")
    assert both() is None


# What each point injects


def test_session_start_names_the_handle_unread_count_and_tools(launched):
    grant()
    post("@lead one")
    name, text = context(output("session-start"))
    assert name == "SessionStart"
    assert text.startswith(frame.PREAMBLE)
    assert "@lead" in text and "1 unread mention." in text and "chat_read" in text
    assert len(text.encode("utf-8")) <= delivery.BUDGETS[delivery.START]
    assert "one" not in text


def test_prompt_digest_frames_mentions_within_its_budget_and_marks_them(launched):
    grant()
    assert output("prompt-submit") == ""
    first = post("@lead " + "é" * 400)
    second = post("@lead second\nline")
    post("no mention here")
    name, text = context(output("prompt-submit"))
    assert name == "UserPromptSubmit"
    lines = text.splitlines()
    assert lines[0] == frame.PREAMBLE
    assert lines[1] == "You are @lead. Unread mentions (2, newest first):"
    assert lines[2] == f'- #general id {second.id}, from @maintainer: "@lead second line"'
    assert lines[3].startswith(f"- #general id {first.id}, from @maintainer:")
    assert lines[-1] == frame.FOOTER
    assert "no mention here" not in text
    assert len(text.encode("utf-8")) <= delivery.BUDGETS[delivery.PROMPT]
    with ChatStore.maintainer() as current:
        row = current.connection.execute(
            "SELECT injected_through, last_injected_at FROM deliveries WHERE participant = ?",
            (LAUNCH,),
        ).fetchone()
    assert row == (second.id, NOW)
    assert context(output("prompt-submit"))[1] == text


def test_a_long_digest_drops_whole_lines_to_fit(launched):
    grant()
    for _ in range(10):
        post("@lead " + "x" * 400)
    text = context(output("prompt-submit"))[1]
    assert len(text.encode("utf-8")) <= delivery.BUDGETS[delivery.PROMPT]
    assert "Unread mentions (10, newest first)" in text and text.endswith(frame.FOOTER)
    assert text.count("\n- #general") < 10


def test_a_mid_turn_notice_carries_no_text_and_waits_60_seconds(launched):
    grant()
    assert output("tool-use") == ""
    post("@lead SECRET payload")
    name, text = context(output("tool-use"))
    assert (name, text) == ("PreToolUse", frame.MID_TURN_NOTICE)
    assert "SECRET" not in text and "@" not in text
    assert output("tool-use", now=NOW + 1000) == ""
    post("@lead another")
    assert output("tool-use", now=NOW + 59_999) == ""
    assert context(output("tool-use", now=NOW + 60_000))[1] == frame.MID_TURN_NOTICE
    assert output("tool-use", now=NOW + 200_000) == ""


def test_a_prompt_digest_also_counts_toward_the_notice_limit(launched):
    grant()
    post("@lead one")
    assert output("prompt-submit")
    post("@lead two")
    assert output("tool-use", now=NOW + 1000) == ""
    assert output("tool-use", now=NOW + 60_000)


def test_read_mentions_are_not_injected(launched):
    grant()
    message = post("@lead one")
    with ChatStore.agent(LAUNCH) as current:
        current.mark_read("general", message.id)
    assert output("prompt-submit") == ""
    assert output("tool-use") == ""


def test_codex_gets_the_same_two_field_json(launched):
    grant()
    post("@lead one")
    name, text = context(output("prompt-submit", "codex"))
    assert name == "UserPromptSubmit" and "@lead one" in text


def test_pi_gets_bare_text_for_its_two_events(launched):
    grant()
    assert output("chat-digest", "pi", point="before_agent_start") == ""
    post("@lead one")
    text = output("chat-digest", "pi", point="before_agent_start")
    assert text.startswith(frame.PREAMBLE) and "@lead one" in text
    post("@lead two")
    assert output("chat-digest", "pi", point="tool_execution_end",
                  now=NOW + 60_000) == frame.MID_TURN_NOTICE
    assert output("chat-digest", "pi", point="session_start") == ""
    assert output("chat-digest", "pi", point=None) == ""


def test_a_stale_or_revoked_grant_injects_nothing(launched):
    grant(start="older")
    post("@lead one")
    assert output("prompt-submit") == ""
    grant()
    assert output("prompt-submit")
    with ChatStore.maintainer() as current:
        current.delete_grant(LAUNCH)
    assert output("prompt-submit") == ""


def test_an_unverified_hook_injects_nothing_even_when_granted(launched, processes):
    grant()
    post("@lead one")
    processes[HOOK_PID] = FakeProcess(HOOK_PID, "hook", 1)
    assert output("prompt-submit") == ""
    with ChatStore.maintainer() as current:
        row = current.connection.execute(
            "SELECT injected_through FROM deliveries WHERE participant = ?", (LAUNCH,)
        ).fetchone()
    assert row == (0,)


def test_a_locked_file_means_no_output_after_the_busy_timeout(launched):
    grant()
    post("@lead one")
    with ChatStore.maintainer() as holder:
        holder.connection.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        assert output("prompt-submit") == ""
        elapsed = time.monotonic() - started
        holder.connection.execute("ROLLBACK")
    assert 0.15 <= elapsed < 2.0
    assert output("prompt-submit")


def test_the_hook_role_cannot_wake(launched):
    grant()
    with ChatStore.hook(LAUNCH) as current:
        with pytest.raises(Exception, match="does not"):
            current.claim_wake(LAUNCH)


# Import budget


def _modules_after(code, env=None):
    result = subprocess.run(
        [sys.executable, "-c", code + "\nimport sys\nprint('\\n'.join(sorted(sys.modules)))"],
        capture_output=True, text=True, timeout=30, check=True,
        env={**os.environ, **(env or {})},
    )
    return set(result.stdout.split())


def test_sessions_without_a_chat_file_never_load_chat(tmp_path):
    env = {LAUNCH_ID_ENV: LAUNCH, PROFILE_ID_ENV: "lead"}
    modules = _modules_after(
        "import tongs_agents.hook as h\n"
        "assert h.chat_output('prompt-submit', 'claude') == ''", env)
    assert not {name for name in modules if name.startswith("tongs_agents.chat")}
    assert "sqlite3" not in modules


def test_the_granted_path_loads_neither_the_launcher_nor_control():
    modules = _modules_after("import tongs_agents.hook\nfrom tongs_agents.chat import delivery")
    for heavy in ("tongs_agents.launches", "tongs_agents.control", "tongs_agents.multiplexers",
                  "tongs_agents.terminal_apis", "tongs_agents.profiles", "urllib.request",
                  "textual", "mcp"):
        assert heavy not in modules


# The real hook and Pi extension, real process ancestry


def _claim_this_process():
    me = discovery.get_process_info(os.getpid())
    assert me is not None and me.start_token
    launches._write(launches.LaunchRecord(
        launch_id=LAUNCH, profile="lead", harness="claude", multiplexer="zellij",
        session="agents", created=int(time.time()), pane_id="1", pid=me.pid,
        process_start=me.start_token, state="running",
    ))
    return me.start_token


def _hook_env():
    return {**os.environ, LAUNCH_ID_ENV: LAUNCH, PROFILE_ID_ENV: "lead"}


def _run_hook(event, agent="claude", stdin=None):
    payload = stdin if stdin is not None else json.dumps(
        {"session_id": "live-sess", "cwd": "/tmp/repo"}
    )
    return subprocess.run(
        [sys.executable, "-m", "tongs_agents.hook", event, "--agent", agent],
        input=payload.encode("utf-8"), capture_output=True, timeout=30, env=_hook_env(),
    )


@pytest.mark.skipif(not LINUX, reason="hook chat delivery runs on Linux only")
def test_the_real_hook_delivers_to_a_granted_session_only():
    start = _claim_this_process()
    ungranted = _run_hook("prompt-submit")
    assert (ungranted.returncode, ungranted.stdout) == (0, b"")
    grant(start=start)
    assert _run_hook("prompt-submit").stdout == b""
    post("@lead please rebase")
    granted = _run_hook("prompt-submit")
    assert granted.returncode == 0
    name, text = context(granted.stdout.decode("utf-8"))
    assert name == "UserPromptSubmit" and "@lead please rebase" in text
    assert _run_hook("tool-use").stdout == b""


def _write_pi_config(tmp_path, hook_path):
    config_dir = tmp_path / "pi-config"
    config_dir.mkdir()
    root = str(extension_path().parent.parent)
    (config_dir / "tongs-agents.json").write_text(json.dumps({
        "schema_version": 1, "owner": "tongs-agents", "hook_path": str(hook_path),
        "extension_path": root, "managed_paths": [root],
    }))
    return config_dir


def run_pi(tmp_path, script, *, hook_body=None, hook_path=None, launch=True, timeout=20):
    if hook_path is None:
        hook_path = tmp_path / "tongs-agents-hook"
        hook_path.write_text(hook_body)
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR)
    config_dir = _write_pi_config(tmp_path, hook_path)
    runner = tmp_path / "runner.mjs"
    runner.write_text(
        f"import extension from {json.dumps(extension_path().as_uri())};\n"
        "const handlers = new Map();\n"
        "const sent = [];\n"
        "extension({ on: (name, handler) => handlers.set(name, handler),\n"
        "            sendMessage: (message, options) => sent.push({ message, options }) });\n"
        "const ctx = { cwd: process.cwd(), sessionManager: { getSessionId: () => 'pi-1' } };\n"
        "const fire = (name, event = {}) => handlers.get(name)?.({ type: name, ...event }, ctx);\n"
        "const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));\n"
        + script
    )
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(config_dir),
           "FLEET_TEST_EVENTS": str(tmp_path / "events.jsonl")}
    env.pop(LAUNCH_ID_ENV, None)
    if launch:
        env.update({LAUNCH_ID_ENV: LAUNCH, PROFILE_ID_ENV: "lead"})
    result = subprocess.run([NODE, str(runner)], cwd=tmp_path, env=env, capture_output=True,
                            text=True, timeout=timeout)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


FAKE_DIGEST_HOOK = (
    "#!/usr/bin/env python3\n"
    "import json, os, sys, time\n"
    "data = sys.stdin.read()\n"
    "with open(os.environ['FLEET_TEST_EVENTS'], 'a', encoding='utf-8') as stream:\n"
    "    stream.write(json.dumps({'argv': sys.argv[1:], 'stdin': data}) + '\\n')\n"
    "if sys.argv[1] == 'chat-digest':\n"
    "    point = json.loads(data)['point']\n"
    "    if os.environ.get('FAKE_SLEEP'):\n"
    "        time.sleep(float(os.environ['FAKE_SLEEP']))\n"
    "    sys.stdout.write('DIGEST ' + point)\n"
)

PI_SCRIPT = """
fire('session_start', { reason: 'startup' });
const result = fire('before_agent_start', { prompt: 'p' });
const prompt = await result;
fire('tool_execution_end', { toolName: 'Read' });
fire('tool_execution_end', { toolName: 'Read' });
await sleep(800);
console.log(JSON.stringify({ promise: result instanceof Promise, prompt: prompt ?? null, sent }));
"""

pi_only = pytest.mark.skipif(NODE is None or not LINUX,
                             reason="needs Node.js on Linux")


@pi_only
def test_the_pi_extension_injects_the_digest_and_steers_notices(tmp_path):
    found = run_pi(tmp_path, PI_SCRIPT, hook_body=FAKE_DIGEST_HOOK)
    assert found["promise"] is True
    assert found["prompt"] == {"message": {
        "customType": "tongs-agents-chat", "content": "DIGEST before_agent_start",
        "display": True,
    }}
    assert found["sent"] == [{
        "message": {"customType": "tongs-agents-chat",
                    "content": "DIGEST tool_execution_end", "display": True},
        "options": {"deliverAs": "steer"},
    }]
    calls = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    digests = [call for call in calls if call["argv"][0] == "chat-digest"]
    assert [json.loads(call["stdin"]) for call in digests] == [
        {"point": "before_agent_start"}, {"point": "tool_execution_end"},
    ]
    assert all(call["argv"] == ["chat-digest", "--agent", "pi"] for call in digests)


@pi_only
def test_the_pi_extension_never_asks_outside_a_launched_pane(tmp_path):
    found = run_pi(tmp_path, PI_SCRIPT, hook_body=FAKE_DIGEST_HOOK, launch=False)
    assert found == {"promise": False, "prompt": None, "sent": []}
    calls = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert all(call["argv"][0] == "fleet-event" for call in calls)


@pi_only
def test_a_slow_digest_is_dropped_after_500_ms(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "3")
    script = """
const started = Date.now();
const prompt = await fire('before_agent_start', { prompt: 'p' });
console.log(JSON.stringify({ prompt: prompt ?? null, elapsed: Date.now() - started }));
"""
    found = run_pi(tmp_path, script, hook_body=FAKE_DIGEST_HOOK)
    assert found["prompt"] is None and found["elapsed"] < 2000


@pi_only
def test_an_oversized_digest_is_dropped(tmp_path):
    body = FAKE_DIGEST_HOOK.replace("sys.stdout.write('DIGEST ' + point)",
                                    "sys.stdout.write('x' * 5000)")
    found = run_pi(tmp_path, """
const prompt = await fire('before_agent_start', { prompt: 'p' });
console.log(JSON.stringify({ prompt: prompt ?? null }));
""", hook_body=body)
    assert found == {"prompt": None}


@pi_only
def test_the_real_hook_answers_the_pi_digest_for_a_granted_session(tmp_path):
    start = _claim_this_process()
    grant(start=start)
    post("@lead from the maintainer")
    wrapper = tmp_path / "tongs-agents-hook"
    wrapper.write_text(
        f"#!{sys.executable}\nimport sys\nfrom tongs_agents.hook import main\nmain()\n"
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    found = run_pi(tmp_path, """
const prompt = await fire('before_agent_start', { prompt: 'p' });
console.log(JSON.stringify({ prompt: prompt ?? null }));
""", hook_path=wrapper, timeout=30)
    content = found["prompt"]["message"]["content"]
    assert content.startswith(frame.PREAMBLE) and "@lead from the maintainer" in content
