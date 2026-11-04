"""Validate pane control (send, read) against an isolated live Zellij session.

Linux helper, run locally and by the hosted send-validation job on each pinned
Zellij release. It reuses the workspace validator's isolation: a fresh
temporary root holds HOME, the XDG config, state, cache, data and runtime
dirs, TMPDIR, ZELLIJ_SOCKET_DIR and ZELLIJ_CONFIG_DIR; claude, codex and pi
on the private PATH are a stub; only processes this run started (PID plus
kernel start time) are signalled and only the session it created is killed;
the evidence is redacted before it is printed or written.

The session runs headless (``zellij attach --create-background``). Two
profiles start the fake REPL fixture (validation/fixtures/fake_repl.py)
through the real spawn path and launcher, so their launch records are
claimed. The fixture turns on bracketed paste and logs every submitted
prompt. ``tongs-agents send|read|wait|agent-status`` run as separate
processes. Checked:

- a multi-line send gives exactly one submitted prompt with the exact text;
- four concurrent sends to one pane are serialized and arrive intact;
- read returns the tail of the pane;
- a pane tongs-agents did not spawn (unknown id, unclaimed record) exits 5
  and receives nothing;
- after the session is recreated with the same name and the pane ids are
  reused by other programs, the old launch exits 5 and nothing is delivered;
- the control audit log holds no sent text.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from unittest.mock import patch

from probe_terminals import (
    Redactor,
    Tracker,
    config_snapshot,
    platform_facts,
    run,
    wait_until,
    zellij_cache_mentions,
)
from workspace import (
    agent_snapshot,
    check_isolation,
    isolated_env,
    private_base,
    public_text,
    write_bin,
)

from tongs_agents import control, launches, paths, workspace_spawn
from tongs_agents.multiplexers.zellij import ZellijMultiplexer
from tongs_agents.profiles import Profile, load_profiles

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "validation" / "fixtures"
FAKE_REPL = FIXTURES / "fake_repl.py"
EVIDENCE = REPO_ROOT / "validation" / "evidence" / "send-linux-x86_64.json"
SCHEMA = "send-validation/1"
RUN_SCHEMA = "send-validation-run/1"
HASHED = (
    "src/tongs_agents/commands/supervisor.py",
    "src/tongs_agents/control.py",
    "src/tongs_agents/launch.py",
    "src/tongs_agents/launches.py",
    "src/tongs_agents/multiplexers/zellij.py",
    "src/tongs_agents/workspace_spawn.py",
    "validation/fixtures/fake_repl.py",
    "validation/send.py",
)
PRIVATE_DIRS = ("home", "bin", "sock", "zellij-cfg", "xdg-config", "xdg-state", "xdg-cache",
                "xdg-data", "xdg-run", "tmp", "io", "stale", "work")
PROFILES = ("r1", "r2")
SEND_DELAY_MS = 150
CONCURRENT = 4
SETTLE = 1.0
CLI_TIMEOUT = 60
UNKNOWN = "unknown"
REUSE_ATTEMPTS = 3
REUSE_SLACK = 4


class Check(Exception):
    """A validation expectation that did not hold."""


def expect(condition, message):
    if not condition:
        raise Check(message)


def file_digest(relative):
    return hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()


def repo_facts():
    code, head, _ = run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    _, dirty, _ = run(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", *HASHED])
    return {
        "tested_commit": head if code == 0 else None,
        "modified_since_commit": bool(dirty),
        "sha256": {relative: file_digest(relative) for relative in HASHED},
    }


def write_config(root, python=None):
    """Two fake REPL profiles (harness other) and the control send delay."""
    root = Path(root)
    python = sys.executable if python is None else python
    lines = []
    for name in PROFILES:
        cwd = root / "work" / name
        cwd.mkdir(parents=True, exist_ok=True)
        command = [python, str(FAKE_REPL), str(root / "io")]
        lines += [f"[profiles.{name}]", f"command = {json.dumps(command)}",
                  'harness = "other"', f"cwd = {json.dumps(str(cwd))}", ""]
    lines += ["[control]", f"send_delay_ms = {SEND_DELAY_MS}", ""]
    directory = root / "xdg-config" / "tongs-agents"
    directory.mkdir(parents=True, mode=0o700)
    path = directory / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return path


def write_fixture(root, zellij_binary):
    root = Path(root)
    for name in PRIVATE_DIRS:
        (root / name).mkdir(mode=0o700)
    (root / "zellij-cfg" / "config.kdl").write_text(
        "show_startup_tips false\nshow_release_notes false\nsession_serialization false\n")
    write_bin(root, zellij_binary)
    write_config(root)


def prompts(directory, name):
    """The prompts the fake REPL logged for ``name``, oldest first."""
    try:
        lines = (Path(directory) / f"repl-{name}.jsonl").read_text().splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def settled_prompts(directory, name, count, timeout=15):
    """Wait for ``count`` prompts, then a quiet period, and return them all."""
    wait_until(lambda: len(prompts(directory, name)) >= count, timeout)
    time.sleep(SETTLE)
    return prompts(directory, name)


def concurrent_texts(token, count=CONCURRENT):
    """Distinct multi-line prompts large enough to interleave if unserialized."""
    filler = "x" * 2048
    return [f"concurrent {index} {token}\nmiddle {index} {filler}\nend {index} {token}"
            for index in range(count)]


class Cli:
    """``tongs-agents`` run as a separate process in the isolated env."""

    def __init__(self, binary, env, cwd):
        self.binary = binary
        self.env = env
        self.cwd = cwd

    def __call__(self, *arguments, stdin=None):
        result = subprocess.run(
            [self.binary, *arguments], env=self.env, cwd=self.cwd, input=stdin,
            capture_output=True, text=True, timeout=CLI_TIMEOUT,
        )
        return result.returncode, result.stdout, result.stderr

    def start(self, *arguments):
        return subprocess.Popen(
            [self.binary, *arguments], env=self.env, cwd=self.cwd, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )


class Session:
    """Raw zellij commands against the run's own session."""

    def __init__(self, name, env, binary):
        self.name = name
        self.env = env
        self.binary = binary

    def start(self):
        code, _, err = run([self.binary, "attach", "--create-background", self.name],
                           env=self.env, timeout=15)
        expect(code == 0, f"zellij attach --create-background failed: {err[-200:]}")
        expect(wait_until(lambda: self.name in self.sessions(), 15),
               "the session was not listed")

    def stop(self):
        run([self.binary, "kill-session", self.name], env=self.env)
        run([self.binary, "delete-session", "--force", self.name], env=self.env)
        return wait_until(lambda: self.name not in self.sessions(), 10, 0.2)

    def sessions(self):
        code, out, _ = run([self.binary, "list-sessions", "--short", "--no-formatting"],
                           env=self.env)
        return out.split() if code == 0 else []

    def new_pane(self, *argv):
        code, out, err = run([self.binary, "--session", self.name, "action", "new-pane",
                              "--", *argv], env=self.env)
        expect(code == 0 and out.startswith("terminal_"), f"new-pane failed: {err[:120]}")
        return out.removeprefix("terminal_")


def exercise(root, session, token, tracker, observations):
    root = Path(root)
    env = isolated_env(root)
    check_isolation(env, root)
    zellij_bin = str(root / "bin" / "zellij")
    io_dir = root / "io"
    code, version, _ = run([zellij_bin, "--version"], env=env)
    expect(code == 0 and version, "zellij --version failed")
    observations["zellij_version"] = version
    target = Session(session, env, zellij_bin)
    cli = Cli(str(root / "bin" / "tongs-agents"), env, str(root / "work"))
    sent = []

    with patch.dict(os.environ, env, clear=True):
        target.start()
        tracker.sweep()
        records = spawn(session, io_dir, observations)
        tracker.sweep()
        r1, r2 = records["r1"].launch_id, records["r2"].launch_id
        multi_line(cli, r1, io_dir, token, sent, observations)
        concurrent(cli, r1, io_dir, token, sent, observations)
        read_tail(cli, r2, io_dir, token, sent, observations)
        hookless_status(cli, r1, observations)
        not_spawned(cli, target, session, io_dir, token, sent, observations)
        tracker.sweep()
        stale(cli, target, records["r1"], root, token, sent, tracker, observations)
        audit(sent, observations)


def spawn(session, io_dir, observations):
    profiles = load_profiles()
    started = time.monotonic()
    created = workspace_spawn.spawn_profiles(
        ZellijMultiplexer(), session, [profiles[name] for name in PROFILES])

    def claimed():
        current = {record.profile: launches.get(record.launch_id) for record in created}
        ready = all((io_dir / f"repl-{name}.ready").exists() for name in PROFILES)
        if ready and all(item and item.state == "running" for item in current.values()):
            return current
        return None

    records = wait_until(claimed, 20)
    observations["spawn"] = {
        "profiles": list(PROFILES),
        "claimed": bool(records),
        "seconds": round(time.monotonic() - started, 2),
        "pane_ids": {name: records[name].pane_id for name in PROFILES} if records else None,
        "socket_dir_recorded": bool(records) and all(
            records[name].socket_dir for name in PROFILES),
    }
    expect(records, "the fake REPL panes were not claimed and ready")
    return records


def multi_line(cli, launch_id, io_dir, token, sent, observations):
    text = f"multi {token} line one\nline two\n\tline three"
    sent.append(text)
    code, _, err = cli("send", launch_id, "-", stdin=text + "\n")
    entries = settled_prompts(io_dir, "r1", 1)
    observations["multi_line"] = {
        "argv": ["tongs-agents", "send", "<r1>", "-"],
        "exit_code": code,
        "sent_lines": 3,
        "submitted_prompts": len(entries),
        "text_intact": bool(entries) and entries[0]["text"] == text,
        "pastes_in_prompt": entries[0]["pastes"] if entries else None,
        "received_lines": entries[0]["lines"] if entries else None,
    }
    facts = observations["multi_line"]
    expect(code == 0, f"send exited {code}: {err[-200:]}")
    expect(facts["submitted_prompts"] == 1, f"{len(entries)} prompts submitted, expected 1")
    expect(facts["text_intact"] and facts["pastes_in_prompt"] == 1,
           "the multi-line prompt did not arrive as one bracketed paste")


def concurrent(cli, launch_id, io_dir, token, sent, observations):
    before = len(prompts(io_dir, "r1"))
    texts = concurrent_texts(token)
    sent.extend(texts)
    started = time.monotonic()
    processes = [cli.start("send", launch_id, "-") for _ in texts]
    for process, text in zip(processes, texts):
        process.stdin.write(text)
        process.stdin.close()
    codes = []
    for process in processes:
        try:
            codes.append(process.wait(CLI_TIMEOUT))
        except subprocess.TimeoutExpired:
            process.kill()
            codes.append(None)
        process.stdout.close()
        process.stderr.close()
    entries = settled_prompts(io_dir, "r1", before + len(texts))[before:]
    digests = {hashlib.sha256(text.encode()).hexdigest() for text in texts}
    observations["concurrent"] = {
        "senders": len(texts),
        "separate_processes": True,
        "bytes_each": len(texts[0].encode()),
        "exit_codes": codes,
        "seconds": round(time.monotonic() - started, 2),
        "submitted_prompts": len(entries),
        "all_intact": sorted(entry["text"] for entry in entries) == sorted(texts),
        "sha256_match": {entry["sha256"] for entry in entries} == digests,
        "one_paste_each": all(entry["pastes"] == 1 for entry in entries),
    }
    facts = observations["concurrent"]
    expect(codes == [0] * len(texts), f"concurrent sends exited {codes}")
    expect(facts["submitted_prompts"] == len(texts),
           f"{len(entries)} prompts submitted, expected {len(texts)}")
    expect(facts["all_intact"] and facts["sha256_match"] and facts["one_paste_each"],
           "concurrent prompts were interleaved or altered")


def read_tail(cli, launch_id, io_dir, token, sent, observations):
    marker = f"tail-marker-{token}"
    sent.append(marker)
    send_code, _, _ = cli("send", launch_id, marker)
    settled_prompts(io_dir, "r2", 1)
    code, out, err = cli("read", launch_id, "--lines", "5")
    rows = out.rstrip("\n").splitlines()
    one_code, one, _ = cli("read", launch_id, "--lines", "1")
    observations["read"] = {
        "argv": ["tongs-agents", "read", "<r2>", "--lines", "5"],
        "send_exit_code": send_code,
        "exit_code": code,
        "lines_returned": len(rows),
        "acknowledgement_in_tail": f"received #1: {marker}" in rows,
        "last_line_is_prompt": bool(rows) and rows[-1].startswith("fake-repl>"),
        "one_line_exit_code": one_code,
        "one_line_is_prompt": one.strip() == "fake-repl>",
    }
    facts = observations["read"]
    expect(send_code == 0 and code == 0 and one_code == 0, f"read failed: {err[-200:]}")
    expect(0 < facts["lines_returned"] <= 5, f"read returned {len(rows)} lines")
    expect(facts["acknowledgement_in_tail"] and facts["last_line_is_prompt"],
           "read did not return the pane's tail")
    expect(facts["one_line_is_prompt"], "read --lines 1 was not the prompt line")


def hookless_status(cli, launch_id, observations):
    code, out, _ = cli("agent-status", launch_id, "--json")
    try:
        state = json.loads(out).get("state")
    except ValueError:
        state = None
    wait_code, wait_out, _ = cli("wait", launch_id, "--until", "done", "--timeout", "1")
    observations["status"] = {
        "agent_status_exit_code": code,
        "state": state,
        "wait_exit_code": wait_code,
        "wait_output": wait_out.split()[-1] if wait_out.split() else None,
    }
    expect(code == 0 and state == "quiet", f"agent-status was {code} {state}")
    expect(wait_code == control.EXIT_TIMEOUT, f"wait on a hookless pane exited {wait_code}")


def not_spawned(cli, target, session, io_dir, token, sent, observations):
    plain_pane = target.new_pane(sys.executable, str(FAKE_REPL), str(io_dir))
    ready = wait_until(lambda: (io_dir / f"repl-{UNKNOWN}.ready").exists(), 10)
    pending = launches.create_pending(Profile("r1", "other", ("unused",)), session, "zellij")
    text = f"must not arrive {token}"
    sent.append(text)
    cases = {
        "unknown launch id": "0" * 32,
        "malformed launch id": f"terminal_{plain_pane}",
        "unclaimed launch record": pending.launch_id,
    }
    results = {}
    for label, launch_id in cases.items():
        send_code, _, send_err = cli("send", launch_id, text)
        read_code, _, _ = cli("read", launch_id)
        results[label] = {"send_exit_code": send_code, "read_exit_code": read_code,
                          "refusal": send_err.strip().splitlines()[-1][:120]
                          .replace(launch_id, "<id>") if send_err.strip() else None}
    time.sleep(SETTLE)
    launches.discard(pending.launch_id)
    observations["not_spawned"] = {
        "plain_pane": "a fake REPL started with zellij new-pane, no launch record",
        "plain_pane_ready": bool(ready),
        "cases": results,
        "plain_pane_prompts": len(prompts(io_dir, UNKNOWN)),
    }
    expect(ready, "the plain pane did not start")
    for label, item in results.items():
        expect(item["send_exit_code"] == control.EXIT_REFUSED
               and item["read_exit_code"] == control.EXIT_REFUSED,
               f"{label} was not refused with exit 5: {item}")
    expect(observations["not_spawned"]["plain_pane_prompts"] == 0,
           "the non-spawned pane received a prompt")


def reuse_pane_id(target, pane_id, stale_io):
    """Open panes in the recreated session until zellij hands out ``pane_id`` again.

    Ids grow from the new session's first pane, so stop once one passes it;
    the caller then recreates the session and tries again (zellij sometimes
    starts a new session's ids higher).
    """
    created = []
    for _ in range(int(pane_id) + REUSE_SLACK):
        created.append(target.new_pane(sys.executable, str(FAKE_REPL), str(stale_io)))
        if created[-1] == pane_id or int(created[-1]) > int(pane_id):
            break
    return created


def stale(cli, target, record, root, token, sent, tracker, observations):
    stale_io = Path(root) / "stale"
    reused = []
    for _ in range(REUSE_ATTEMPTS):
        gone = target.stop()
        expect(gone, "the session was not removed before recreation")
        target.start()
        tracker.sweep()
        reused = reuse_pane_id(target, record.pane_id, stale_io)
        tracker.sweep()
        if record.pane_id in reused:
            break
    wait_until(lambda: (stale_io / f"repl-{UNKNOWN}.ready").exists(), 10)
    text = f"stale launch {token}"
    sent.append(text)
    send_code, _, err = cli("send", record.launch_id, text)
    read_code, _, _ = cli("read", record.launch_id)
    status_code, _, _ = cli("agent-status", record.launch_id)
    time.sleep(SETTLE)
    observations["stale_launch"] = {
        "session": "killed, deleted and recreated with the same name",
        "old_pane_id": record.pane_id,
        "new_pane_ids": reused,
        "pane_id_reused_by_other_program": record.pane_id in reused,
        "send_exit_code": send_code,
        "read_exit_code": read_code,
        "agent_status_exit_code": status_code,
        "refusal": err.strip().splitlines()[-1][:160].replace(record.launch_id, "<r1>")
        if err.strip() else None,
        "reused_pane_prompts": len(prompts(stale_io, UNKNOWN)),
    }
    facts = observations["stale_launch"]
    expect(facts["pane_id_reused_by_other_program"], "the old pane id was not reused")
    expect(send_code == read_code == status_code == control.EXIT_REFUSED,
           f"the stale launch was not refused: {send_code} {read_code} {status_code}")
    expect(facts["reused_pane_prompts"] == 0, "the stale launch reached the new pane")


def audit(sent, observations):
    path = paths.state_dir() / control.AUDIT_DIR / control.AUDIT_FILE
    raw = path.read_text(encoding="utf-8")
    lines = [json.loads(line) for line in raw.splitlines()]
    sends = [line for line in lines if line["action"] == "send"]
    leaked = [text for text in sent for part in text.split("\n") if part and part in raw]
    observations["audit"] = {
        "mode": oct(path.stat().st_mode & 0o777),
        "lines": len(lines),
        "fields": sorted(lines[0]) if lines else [],
        "sends_ok": sum(1 for line in sends if line["outcome"] == "ok"),
        "sends_refused": sum(1 for line in sends if line["outcome"] == "refused"),
        "sent_text_found": bool(leaked),
    }
    facts = observations["audit"]
    expect(facts["mode"] == "0o600", "the audit log is not 0600")
    expect(facts["sends_ok"] == 2 + CONCURRENT, f"{facts['sends_ok']} audited ok sends")
    expect(facts["sends_refused"] == 4, f"{facts['sends_refused']} audited refused sends")
    expect(not facts["sent_text_found"], "sent text appears in the audit log")


def cleanup(root, sessions, tracker):
    root = Path(root)
    env = isolated_env(root)
    binary = str(root / "bin" / "zellij")
    if os.path.lexists(binary):
        for name in sessions:
            run([binary, "kill-session", name], env=env)
            run([binary, "delete-session", "--force", name], env=env)
    result = tracker.terminate()
    shutil.rmtree(root, ignore_errors=True)
    result["temp_dir_removed"] = not root.exists()
    return result


def validate(write=True, output=EVIDENCE):
    zellij_binary = shutil.which("zellij")
    if sys.platform != "linux" or not zellij_binary:
        raise RuntimeError("This check requires Linux and zellij on PATH")
    token = secrets.token_hex(4)
    session = f"tongs-send-{token}"
    real_home = Path.home()
    config_before = config_snapshot()
    agents_before = agent_snapshot(real_home)
    root = Path(tempfile.mkdtemp(prefix=f"tongs-send-{token}-", dir=private_base()))
    tracker = Tracker([str(root), session])
    observations = {}
    started = time.monotonic()
    try:
        write_fixture(root, zellij_binary)
        exercise(root, session, token, tracker, observations)
        error = None
    except Exception as failure:
        where = traceback.extract_tb(failure.__traceback__)[-1]
        error = (f"{type(failure).__name__}: {failure} "
                 f"({os.path.basename(where.filename)}:{where.lineno})")
    finally:
        cleanup_result = cleanup(root, (session,), tracker)
    cleanup_result["marker_strays_after_run"] = len(Tracker([str(root), token]).stray())
    healthy_cleanup = (cleanup_result["survivors"] == 0 and cleanup_result["marker_strays"] == 0
                       and cleanup_result["marker_strays_after_run"] == 0
                       and cleanup_result["temp_dir_removed"])
    evidence = {
        "schema": RUN_SCHEMA,
        "result": "pass" if error is None and healthy_cleanup else "fail",
        "error": error,
        "zellij_version": observations.pop("zellij_version", None),
        "provenance": "instrumented",
        "provenance_detail": (
            "real zellij server (headless session), real tongs-agents spawn path, launcher "
            "and send/read/wait/agent-status CLI processes in a private root; a fake REPL "
            "fixture with bracketed paste stands in for agents and logs submitted prompts"),
        "fixtures": {
            "r1": "fake REPL profile (harness other) spawned by tongs-agents",
            "r2": "fake REPL profile (harness other) spawned by tongs-agents",
            "plain pane": "fake REPL started by zellij new-pane without a launch record",
            "claude, codex and pi on PATH": "stub CLI that records its arguments",
        },
        "send_delay_ms": SEND_DELAY_MS,
        **observations,
        "cleanup": cleanup_result,
        "user_config_unchanged": config_snapshot() == config_before,
        "user_agent_config_unchanged": agent_snapshot(real_home) == agents_before,
        "user_zellij_cache_entries_for_run": zellij_cache_mentions(token),
        "duration_seconds": round(time.monotonic() - started, 1),
        "platform": platform_facts(),
        **repo_facts(),
    }
    if (not evidence["user_config_unchanged"] or not evidence["user_agent_config_unchanged"]
            or evidence["user_zellij_cache_entries_for_run"]):
        evidence["result"] = "fail"
    redact = Redactor.for_host({str(root): "<tmp>", session: "<session>", token: "<token>",
                                str(REPO_ROOT): "<repo>", sys.executable: "<python>"})
    text, leaks = public_text(evidence, redact)
    if leaks:
        raise RuntimeError(f"refusing to print evidence with private data: {leaks}")
    run_evidence = json.loads(text)
    if write:
        write_evidence(Path(output), run_evidence)
    print(text, end="")
    return run_evidence


def version_key(evidence):
    reported = evidence.get("zellij_version") or ""
    return reported.removeprefix("zellij ").strip() or "unknown"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_evidence(path, run_evidence):
    """Replace this zellij version's run in the evidence file, keeping the others."""
    current = read_json(path)
    runs = current.get("runs", {}) if isinstance(current, dict) and current.get(
        "schema") == SCHEMA else {}
    runs[version_key(run_evidence)] = run_evidence
    document = {
        "schema": SCHEMA,
        "provenance": "instrumented",
        "runs": dict(sorted(runs.items(),
                            key=lambda item: tuple(int(part) for part in item[0].split(".")
                                                   if part.isdigit()))),
    }
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-write", action="store_true", help="print evidence only")
    parser.add_argument("--out", default=str(EVIDENCE))
    arguments = parser.parse_args(argv)
    evidence = validate(write=not arguments.no_write, output=arguments.out)
    return 0 if evidence["result"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
