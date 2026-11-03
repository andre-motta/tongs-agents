"""Pure helpers of the local harness facts probe (validation/harness_facts.py)."""

import importlib.util
import json
from pathlib import Path
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only probe")

if sys.platform == "linux":
    _probe_spec = importlib.util.spec_from_file_location(
        "probe_terminals", ROOT / "validation" / "probe_terminals.py"
    )
    _probe = importlib.util.module_from_spec(_probe_spec)
    _probe_spec.loader.exec_module(_probe)
    sys.modules.setdefault("probe_terminals", _probe)
    _spec = importlib.util.spec_from_file_location(
        "harness_facts", ROOT / "validation" / "harness_facts.py"
    )
    probe = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(probe)


def _record(tag, event, launch=None, ts=0.0, ancestors=()):
    return {
        "argv": [tag, event],
        "event": event,
        "env": {name: None for name in probe.RECORDED_ENV} | {
            "TONGS_AGENTS_LAUNCH_ID": launch},
        "ancestors": list(ancestors),
        "ts": ts,
    }


def test_fact_rejects_unknown_labels():
    assert probe.fact("q", "live", True, note="n") == {
        "question": "q", "label": "live", "answer": True, "note": "n"}
    with pytest.raises(ValueError):
        probe.fact("q", "guessed", True)


def test_compare_snapshots_reports_only_flags():
    report = probe.compare_snapshots(
        {".codex/config.toml": "a", ".claude.json": "b", ".pi/agent/mcp.json": None},
        {".codex/config.toml": "a", ".claude.json": "c", ".pi/agent/mcp.json": None},
    )

    assert report["~/.codex/config.toml"] == {
        "exists": True, "unchanged": True, "volatile": False}
    assert report["~/.claude.json"] == {"exists": True, "unchanged": False, "volatile": True}
    assert report["~/.pi/agent/mcp.json"]["exists"] is False


def test_refuse_private_root_blocks_user_config(monkeypatch, tmp_path):
    monkeypatch.setattr(probe, "real_home", lambda: tmp_path)

    for inside in (tmp_path / ".codex", tmp_path / ".claude" / "x", tmp_path / ".config"):
        with pytest.raises(SystemExit):
            probe.refuse_private_root(inside)
    probe.refuse_private_root(tmp_path / "scratch")


def test_env_matches_needs_every_record_to_match():
    good = _record("t", "Stop", launch="id-1")
    bad = _record("t", "Stop", launch="id-2")

    assert probe.env_matches([good], {"TONGS_AGENTS_LAUNCH_ID": "id-1"})
    assert not probe.env_matches([good, bad], {"TONGS_AGENTS_LAUNCH_ID": "id-1"})
    assert not probe.env_matches([], {"TONGS_AGENTS_LAUNCH_ID": "id-1"})


def test_summarize_records_reports_visibility_and_ancestry():
    record = _record("t", "Stop", launch="id", ancestors=[
        {"pid": 10, "comm": "sh"}, {"pid": 9, "comm": "codex"}])

    [event] = probe.summarize_records([record], harness_pid=9)

    assert event["event"] == "Stop"
    assert event["env_visible"]["TONGS_AGENTS_LAUNCH_ID"] is True
    assert event["env_visible"]["CODEX_HOME"] is False
    assert event["parent_comm"] == "sh"
    assert event["harness_pid_is_ancestor"] is True


def test_codex_hooks_cover_every_event_with_session_end_timeout_three():
    class Lab:
        def recorder_command(self, *tags):
            return " ".join(("recorder", *tags))

    hooks = probe.codex_hooks(Lab(), "tag")["hooks"]

    assert set(hooks) == set(probe.CODEX_EVENTS)
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 3
    assert hooks["Stop"][0]["hooks"][0]["timeout"] == 5
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear"


def test_trust_codex_hooks_writes_parseable_trust_entries(tmp_path):
    key = '/x/hooks.json:SessionEnd:0:0 "quoted"'
    probe.trust_codex_hooks(tmp_path, [{"key": key, "currentHash": "sha256:ab"}],
                            'model = "fixture"\n')

    config = tomllib.loads((tmp_path / "config.toml").read_text())

    assert config["model"] == "fixture"
    assert config["hooks"]["state"][key] == {"trusted_hash": "sha256:ab"}


def test_trust_after_change_compares_hashes_by_key():
    trusted = [{"key": "k1", "currentHash": "h1", "trustStatus": "trusted"},
               {"key": "k2", "currentHash": "h2", "trustStatus": "trusted"}]
    changed = [{"key": "k1", "currentHash": "h1x", "trustStatus": "modified"},
               {"key": "k2", "currentHash": "h2x", "trustStatus": "modified"}]

    report = probe.trust_after_change(trusted, changed, trusted)

    assert report == {
        "trust_before": ["trusted"], "trust_after_change": ["modified"],
        "trust_after_restore": ["trusted"], "keys_unchanged": True, "hash_changed": True}
    assert probe.trust_after_change(trusted, trusted, trusted)["hash_changed"] is False
    assert probe.trust_after_change([], [], [])["keys_unchanged"] is False


def test_session_end_report_spans_tags_and_labels_launches():
    identities = {"id-1": "tui-1", "id-2": "tui-2"}
    records = [
        _record("tui-1", "Stop", launch="id-1", ts=1.0),
        _record("tui-2", "SessionEnd", launch="id-1", ts=2.0),
        _record("tui-3b", "SessionEnd", launch="id-1", ts=12.0),
        _record("tui-3b", "SessionEnd", launch="unknown", ts=13.0),
        _record("tui-3", "SessionEnd", ts=3.0),
    ]

    report = probe.session_end_report(records, identities, stopped_at=10.0,
                                      exits={"tui-2": 1.5})

    assert report == [
        {"tag": "tui-2", "launch": "tui-1", "after_daemon_stop": False,
         "seconds_from_daemon_stop": -8.0, "seconds_after_tui_exit": 0.5},
        {"tag": "tui-3b", "launch": "tui-1", "after_daemon_stop": True,
         "seconds_from_daemon_stop": 2.0, "seconds_after_tui_exit": None},
        {"tag": "tui-3b", "launch": "other", "after_daemon_stop": True,
         "seconds_from_daemon_stop": 3.0, "seconds_after_tui_exit": None},
        {"tag": "tui-3", "launch": None, "after_daemon_stop": False,
         "seconds_from_daemon_stop": -7.0, "seconds_after_tui_exit": None},
    ]


def test_daemon_shape_recognises_only_the_app_server():
    assert probe.daemon_shape({
        "comm": "codex", "argv": ["/h/packages/app-server-daemon/codex", "app-server"],
    }) == {"argv": ["codex", "app-server"], "managed_copy_in_codex_home": True}
    assert probe.daemon_shape({"comm": "codex", "argv": ["codex", "exec"]}) is None
    assert probe.daemon_shape({"comm": "bash", "argv": ["bash", "app-server"]}) is None
    assert probe.daemon_shape(None) is None


def test_summarize_collects_hook_env_answers():
    results = {
        "claude": {"hook_env": {"answer": True}},
        "codex": {"exec_hook_env": {"answer": True}, "tui_hook_env": {"answer": {
            "tui-1": {"own_launch_id_seen": True},
            "tui-2": {"own_launch_id_seen": False},
            "stop-daemon": {"daemon_stop_exit": 0},
        }}},
        "pi": {"hook_env": {"answer": True}},
    }

    assert probe.summarize(results) == {
        "claude": True, "codex_exec": True,
        "codex_tui": {"tui-1": True, "tui-2": False}, "pi": True}


EVIDENCE = ROOT / "validation" / "evidence" / "harness-facts-fedora44.json"


def test_committed_evidence_is_redacted_and_clean():
    text = EVIDENCE.read_text()
    evidence = json.loads(text)

    assert evidence["probe"] == "validation/harness_facts.py"
    assert "/home/" not in text and "/tmp/" not in text
    assert evidence["cleanup"]["survivors"] == 0
    assert all(item["unchanged"] for item in evidence["user_config"].values()
               if not item["volatile"])
    assert set(evidence["provider_paths"]) <= {
        "/v1/messages", "/v1/responses", "/v1/chat/completions"}
    for section in ("claude", "codex", "pi"):
        for value in evidence["results"][section].values():
            if isinstance(value, dict) and "label" in value:
                assert value["label"] in probe.LABELS
