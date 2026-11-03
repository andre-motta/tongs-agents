"""The fake REPL fixture, the send validator's safety controls and its evidence."""

import importlib.util
import json
from pathlib import Path
import re
import sys

import pytest

from tongs_agents import control
from tongs_agents.profiles import load_profiles

ROOT = Path(__file__).parents[1]
VALIDATION = ROOT / "validation"
EVIDENCE = VALIDATION / "evidence" / "send-linux-x86_64.json"
PINS = ("0.44.2", "0.45.1")

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only validator")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminal modules")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if sys.platform != "win32":
    fake_repl = _load("send_fake_repl", VALIDATION / "fixtures" / "fake_repl.py")

if sys.platform == "linux":
    sys.modules.setdefault("probe_terminals", _load("probe_terminals",
                                                    VALIDATION / "probe_terminals.py"))
    sys.modules.setdefault("workspace", _load("workspace", VALIDATION / "workspace.py"))
    validator = _load("send_validation", VALIDATION / "send.py")


def _repl():
    submitted = []
    return fake_repl.Repl(submitted.append), submitted


@posix_only
def test_a_bracketed_multi_line_paste_and_enter_is_one_prompt():
    repl, submitted = _repl()

    assert repl.feed(b"\x1b[200~line one\nline two\r\nline three\x1b[201~")
    assert submitted == []
    repl.feed(b"\r")

    assert [entry["text"] for entry in submitted] == ["line one\nline two\r\nline three"]
    assert submitted[0]["pastes"] == 1 and submitted[0]["lines"] == 3


@posix_only
def test_paste_markers_split_across_reads_are_joined():
    repl, submitted = _repl()
    data = b"\x1b[200~split \xc3\xa9 text\n2nd\x1b[201~\r"
    for index in range(len(data)):
        repl.feed(data[index:index + 1])

    assert [entry["text"] for entry in submitted] == ["split é text\n2nd"]


@posix_only
def test_an_unbracketed_newline_submits_each_line():
    repl, submitted = _repl()
    repl.feed(b"typed one\rtyped two\n")
    assert [entry["text"] for entry in submitted] == ["typed one", "typed two"]
    assert all(entry["pastes"] == 0 for entry in submitted)


@posix_only
def test_ctrl_c_stops_the_repl():
    repl, submitted = _repl()
    assert repl.feed(b"abc\x03") is False
    assert submitted == []


@posix_only
def test_fixture_profile_name_is_sanitized():
    assert fake_repl.profile_name({"TONGS_AGENTS_PROFILE": "r1"}) == "r1"
    assert fake_repl.profile_name({"TONGS_AGENTS_PROFILE": "../x"}) == "unknown"
    assert fake_repl.profile_name({}) == "unknown"


@linux_only
def test_validator_requires_zellij(monkeypatch):
    monkeypatch.setattr(validator.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="requires Linux and zellij"):
        validator.validate(write=False)


@linux_only
def test_fixture_config_holds_two_fake_repl_profiles_and_the_send_delay(tmp_path, monkeypatch):
    path = validator.write_config(tmp_path, python="/usr/bin/python3")
    monkeypatch.setenv("TONGS_AGENTS_CONFIG_DIR", str(path.parent))

    profiles = load_profiles()
    assert sorted(profiles) == ["r1", "r2"]
    for profile in profiles.values():
        assert profile.harness == "other"
        assert profile.argv == ("/usr/bin/python3", str(validator.FAKE_REPL),
                                str(tmp_path / "io"))
    assert control.load_settings(path) == control.Settings(validator.SEND_DELAY_MS, False)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


@linux_only
def test_isolated_env_is_checked_before_any_process_starts(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    env = validator.isolated_env(root, real_env={"LANG": "C.UTF-8"})
    assert validator.check_isolation(env, root, home=tmp_path / "user")
    env["ZELLIJ_SESSION_NAME"] = "agents"
    with pytest.raises(Exception, match="inherited"):
        validator.check_isolation(env, root, home=tmp_path / "user")


@linux_only
def test_concurrent_texts_are_distinct_multi_line_and_within_the_limit():
    texts = validator.concurrent_texts("tok")
    assert len(set(texts)) == validator.CONCURRENT
    assert all(text.count("\n") == 2 for text in texts)
    assert all(2048 < len(text.encode()) <= control.MAX_SEND_BYTES for text in texts)


@linux_only
def test_prompts_skip_torn_lines(tmp_path):
    (tmp_path / "repl-r1.jsonl").write_text('{"n": 1, "text": "a"}\n{"n": 2, "te\n')
    assert validator.prompts(tmp_path, "r1") == [{"n": 1, "text": "a"}]
    assert validator.prompts(tmp_path, "missing") == []


@linux_only
def test_write_evidence_keeps_other_versions(tmp_path):
    path = tmp_path / "send.json"
    validator.write_evidence(path, {"zellij_version": "zellij 0.45.1", "result": "pass"})
    validator.write_evidence(path, {"zellij_version": "zellij 0.44.2", "result": "pass"})
    document = json.loads(path.read_text())
    assert list(document["runs"]) == ["0.44.2", "0.45.1"]


def test_committed_evidence_passes_on_every_pin():
    document = json.loads(EVIDENCE.read_text())
    assert document["schema"] == "send-validation/1"
    assert set(PINS) <= set(document["runs"])
    for version in PINS:
        run = document["runs"][version]
        assert run["result"] == "pass" and run["error"] is None
        assert run["zellij_version"] == f"zellij {version}"
        assert run["multi_line"]["submitted_prompts"] == 1
        assert run["multi_line"]["text_intact"]
        concurrent = run["concurrent"]
        assert concurrent["submitted_prompts"] == concurrent["senders"] == 4
        assert concurrent["all_intact"] and concurrent["sha256_match"]
        assert run["read"]["acknowledgement_in_tail"] and run["read"]["last_line_is_prompt"]
        for case in run["not_spawned"]["cases"].values():
            assert case["send_exit_code"] == case["read_exit_code"] == 5
        assert run["not_spawned"]["plain_pane_prompts"] == 0
        stale = run["stale_launch"]
        assert stale["pane_id_reused_by_other_program"]
        assert stale["send_exit_code"] == stale["read_exit_code"] == 5
        assert stale["reused_pane_prompts"] == 0
        assert not run["audit"]["sent_text_found"]
        assert run["cleanup"]["survivors"] == 0 and run["cleanup"]["temp_dir_removed"]
        assert run["user_config_unchanged"] and run["user_agent_config_unchanged"]
        assert not run["modified_since_commit"]


def test_committed_evidence_is_public_safe():
    text = EVIDENCE.read_text()
    assert "/home/" not in text and "/tmp/" not in text and "/run/user/" not in text
    assert "\u2014" not in text
    assert "tongs-send-" not in text
    assert re.search(r"\b[0-9a-f]{32}\b", text) is None


def test_hosted_send_job_runs_the_validator_on_every_pin():
    text = (ROOT / ".github" / "workflows" / "zellij.yml").read_text()
    jobs = text.split("\njobs:\n", 1)[1]
    job = re.search(r"^  send-validation:\n(.*?)(?=^  \S|\Z)", jobs, re.M | re.S)

    assert job is not None
    body = job.group(1)
    assert "needs: pins" in body
    assert "fromJSON(needs.pins.outputs.versions)" in body
    assert "python validation/send.py --no-write" in body
    assert 'test "$actual_sha256" = "$expected_sha256"' in body
    assert body.index("expected_sha256") < body.index("tar -xzf")
    assert "persist-credentials: false" in body
