"""Pure helpers of the local zellij workspace spike (validation/workspace_spike.py)."""

import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only spike")

if sys.platform == "linux":
    _probe_spec = importlib.util.spec_from_file_location(
        "probe_terminals", ROOT / "validation" / "probe_terminals.py"
    )
    _probe = importlib.util.module_from_spec(_probe_spec)
    _probe_spec.loader.exec_module(_probe)
    sys.modules.setdefault("probe_terminals", _probe)
    _spec = importlib.util.spec_from_file_location(
        "workspace_spike", ROOT / "validation" / "workspace_spike.py"
    )
    spike = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(spike)


def test_key_types_unions_fields_and_types():
    assert spike.key_types([{"id": 1, "cwd": None}, {"id": 2, "cwd": "/x"}]) == {
        "cwd": ["null", "str"], "id": ["int"]}


@pytest.mark.parametrize("stdout, shape, pane_id", [
    ("terminal_7\n", "terminal_<digits>", 7),
    ("plugin_2", "plugin_<digits>", 2),
    ("", "", None),
    ("Session 'x' not found", "Session 'x' not found", None),
    ("terminal_x", "terminal_x", None),
])
def test_returned_id_shapes(stdout, shape, pane_id):
    assert spike.returned_id(stdout) == {"raw_shape": shape, "pane_id": pane_id}


def test_starts_reads_json_lines(tmp_path):
    (tmp_path / "starts-a").write_text('{"pid": 1}\n\n{"pid": 2}\n')

    assert [item["pid"] for item in spike.starts(tmp_path, "a")] == [1, 2]
    assert spike.starts(tmp_path, "missing") == []


def test_selected_steps_default_runs_everything():
    assert spike.selected_steps(None) == [name for name, _ in spike.STEPS]


@pytest.mark.parametrize("only, expected", [
    (["focus"], ["create", "spawn", "focus"]),
    (["tmux"], ["create", "tmux"]),
    (["create"], ["create"]),
    (["exit", "spawn"], ["create", "spawn", "exit"]),
])
def test_selected_steps_add_create_and_dependencies(only, expected):
    assert spike.selected_steps(only) == expected


def test_focus_step_skips_without_a_spawned_pane():
    result = spike.step_focus(SimpleNamespace(out_pane=None))

    assert "skipped" in result


EVIDENCE = sorted((ROOT / "validation" / "evidence").glob("workspace-spike-*.json"))


@pytest.mark.parametrize("path", EVIDENCE, ids=lambda path: path.name)
def test_committed_spike_records_are_redacted_and_clean(path):
    text = path.read_text()
    record = json.loads(text)

    assert record["schema"] == spike.SCHEMA
    assert "/home/" not in text
    run = record["run"]
    assert run["cleanup"]["survivors"] == 0
    assert run["cleanup"]["marker_strays_after_run"] == 0
    assert run["user_config_unchanged"] is True
    assert run["user_zellij_cache_entries_for_run"] == 0
    assert not any("error" in step for step in record["steps"].values())


def test_spike_records_are_committed():
    assert {path.name for path in EVIDENCE} >= {
        "workspace-spike-fedora44-zellij-0.44.2.json",
        "workspace-spike-fedora44-zellij-0.45.1.json",
    }
