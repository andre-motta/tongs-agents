"""``tongs-agents workspace --multiplexer tmux``, the launcher's tmux pane and the CI gate."""

import importlib.util
import json
import re
import shlex
import sys
from pathlib import Path

import pytest

from tests.test_workspace_spawn import FakeMux, runtime_dir, write_profiles  # noqa: F401
from tongs_agents import cli, launch, launches
from tongs_agents.commands import sidebar, workspace
from tongs_agents.multiplexers import PaneRef
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tmux-mux.yml"
SOCKET = "/tmp/tmux-1000/default"
ATTACH = ["tmux", "-S", SOCKET, "attach-session", "-t"]
FULL_SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")


class FakeTmuxMux(FakeMux):
    name = "tmux"

    def __init__(self, **kwargs):
        kwargs.setdefault("available", (True, "tmux 3.4"))
        super().__init__(**kwargs)

    def attach_argv(self, session):
        return [*ATTACH, f"={session}"]


class Exec:
    def __init__(self):
        self.calls = []

    def __call__(self, file, argv, env):
        self.calls.append((file, list(argv), dict(env)))
        raise SystemExit("exec")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(key, raising=False)
    write_profiles(tmp_path, ["alpha", "beta"])
    muxes = {"tmux": FakeTmuxMux(), "zellij": FakeMux()}
    requested = []

    def get(name="zellij", **kwargs):
        requested.append(name)
        return muxes[name]

    monkeypatch.setattr(workspace, "get_multiplexer", get)
    monkeypatch.setattr(workspace, "zellij_binary", lambda: "/opt/zellij")
    monkeypatch.setattr(workspace.shutil, "which", lambda name: None)
    execs = Exec()
    monkeypatch.setattr(workspace.os, "execvpe", execs)
    return muxes["tmux"], execs, requested


def _run(*argv):
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["workspace", *argv])
    try:
        return handlers["workspace"](args)
    except SystemExit as raised:
        assert raised.code == "exec"
        return "exec"


def _config(tmp_path, text):
    path = tmp_path / "config" / "config.toml"
    path.write_text(path.read_text() + text)


def test_new_tmux_session_starts_detached_with_sidebar_then_attaches(env, monkeypatch):
    mux, execs, requested = env
    monkeypatch.setenv("TMUX_TMPDIR", "/somewhere")
    assert _run("--multiplexer", "tmux", "--profile", "alpha", "--profile", "beta") == "exec"
    assert requested == ["tmux"]
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME, "alpha", "beta"]
    assert all(call["session"] == "agents" and not call["focus"] for call in mux.spawned)
    assert mux.spawned[0]["argv"][1:] == ["-m", "tongs_agents.commands.sidebar", "--compact"]
    assert mux.spawned[1]["argv"][1:4] == ["-m", "tongs_agents.launch", "alpha"]
    (file, argv, exec_env), = execs.calls
    assert file == "tmux" and argv == [*ATTACH, "=agents"]
    assert "TMUX" not in exec_env and exec_env["TMUX_TMPDIR"] == "/somewhere"
    records = launches.list_records("agents")
    assert sorted(record.profile for record in records) == ["alpha", "beta"]
    assert {record.multiplexer for record in records} == {"tmux"}


def test_running_tmux_session_gets_profiles_and_a_sidebar_then_attaches(env):
    mux, execs, _ = env
    mux.exists = True
    assert _run("--multiplexer", "tmux", "--profile", "alpha", "--session", "work") == "exec"
    assert [call["name"] for call in mux.spawned] == ["alpha", SIDEBAR_PANE_NAME]
    assert execs.calls[0][1] == [*ATTACH, "=work"]


def test_print_command_creates_the_session_and_prints_the_attach(env, capsys):
    mux, execs, _ = env
    assert _run("--multiplexer", "tmux", "--print-command", "--profile", "alpha") == 0
    assert execs.calls == []
    captured = capsys.readouterr()
    assert captured.out.strip() == shlex.join([*ATTACH, "=agents"])
    assert "started the detached tmux session" in captured.err
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME, "alpha"]


def test_print_command_for_a_running_session_spawns_nothing(env, capsys):
    mux, execs, _ = env
    mux.exists = True
    assert _run("--multiplexer", "tmux", "--print-command", "--profile", "alpha") == 0
    assert mux.spawned == [] and execs.calls == []
    assert "does not start alpha" in capsys.readouterr().err


def test_inside_tmux_adds_panes_and_never_nests(env, monkeypatch):
    mux, execs, _ = env
    monkeypatch.setenv("TMUX", f"{SOCKET},1,0")
    monkeypatch.setenv("TMUX_PANE", "%4")
    mux.here = PaneRef("tmux", "agents", "%4", SOCKET)
    assert _run("--multiplexer", "tmux", "--profile", "alpha") == 0
    assert execs.calls == []
    assert [call["name"] for call in mux.spawned] == ["alpha", SIDEBAR_PANE_NAME]
    assert ("session_state", "agents") not in mux.calls


def test_inside_tmux_refuses_another_session_and_print_command(env, monkeypatch, capsys):
    mux, execs, _ = env
    monkeypatch.setenv("TMUX", f"{SOCKET},1,0")
    mux.here = PaneRef("tmux", "agents", "%4", SOCKET)
    assert _run("--multiplexer", "tmux", "--session", "other") == workspace.EXIT_USAGE
    assert "already inside tmux session 'agents'" in capsys.readouterr().err
    assert _run("--multiplexer", "tmux", "--print-command") == workspace.EXIT_USAGE
    assert "nothing to print inside a tmux session" in capsys.readouterr().err
    assert mux.spawned == [] and execs.calls == []


def test_inside_tmux_without_an_identifiable_session(env, monkeypatch, capsys):
    mux, _, _ = env
    monkeypatch.setenv("TMUX", f"{SOCKET},1,0")
    assert _run("--multiplexer", "tmux") == workspace.EXIT_ERROR
    assert "TMUX is set but the current tmux session" in capsys.readouterr().err


def test_tmux_is_refused_inside_zellij(env, monkeypatch, capsys):
    mux, execs, requested = env
    monkeypatch.setenv("ZELLIJ", "0")
    assert _run("--multiplexer", "tmux", "--profile", "alpha") == workspace.EXIT_USAGE
    assert "inside Zellij" in capsys.readouterr().err
    assert requested == [] and mux.spawned == [] and execs.calls == []


def test_unavailable_tmux_stops_before_anything_starts(env, capsys):
    mux, execs, _ = env
    mux._available = (False, "tmux 3.1 is older than the supported floor 3.2")
    assert _run("--multiplexer", "tmux", "--profile", "alpha") == workspace.EXIT_ERROR
    assert "floor 3.2; run 'tongs-agents doctor'" in capsys.readouterr().err
    assert mux.spawned == [] and launches.list_records() == []


def test_resurrect_and_tongs_are_ignored_with_notices(env, capsys):
    mux, _, _ = env
    assert _run("--multiplexer", "tmux", "--resurrect", "--tongs") == "exec"
    err = capsys.readouterr().err
    assert "ignoring --resurrect" in err and "ignoring --tongs" in err
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME]


def test_spawn_failure_is_reported_without_attaching(env, capsys):
    mux, execs, _ = env
    mux.fail_spawn = {"beta"}
    assert _run("--multiplexer", "tmux", "--profile", "alpha", "--profile", "beta") == 1
    assert "cannot spawn beta" in capsys.readouterr().err
    assert execs.calls == []
    assert [record.profile for record in launches.list_records()] == ["alpha"]


def test_config_selects_tmux_and_the_flag_overrides_it(env, tmp_path):
    mux, execs, requested = env
    _config(tmp_path, '\n[workspace]\nmultiplexer = "tmux"\n')
    assert _run("--profile", "alpha") == "exec"
    assert requested == ["tmux"]
    assert _run("--multiplexer", "zellij", "--profile", "alpha") == "exec"
    assert requested == ["tmux", "zellij"]
    assert execs.calls[-1][0] == "/opt/zellij"


def test_zellij_stays_the_default(env):
    _, _, requested = env
    assert _run("--profile", "alpha") == "exec"
    assert requested == ["zellij"]


def test_alias_keeps_the_multiplexer_flag(env, capsys):
    assert _run("--print-alias", "--multiplexer", "tmux", "--profile", "alpha") == 0
    assert "--multiplexer tmux --profile alpha" in capsys.readouterr().out


@pytest.mark.parametrize("env_vars, expected", [
    ({"TMUX": "/tmp/tmux-1/a,b,99,0", "TMUX_PANE": "%7"},
     PaneRef("tmux", "", "%7", "/tmp/tmux-1/a,b")),
    ({"TMUX": "relative,99,0", "TMUX_PANE": "%7"}, None),
    ({"TMUX": "", "TMUX_PANE": "%7"}, None),
    ({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "z", "ZELLIJ_PANE_ID": "2",
      "TMUX": "/s,1,0", "TMUX_PANE": "%7"}, PaneRef("zellij", "z", "2", None)),
])
def test_launcher_records_tmux_pane_without_zellij(env_vars, expected):
    assert launch.pane_from_env(env_vars) == expected


def test_launcher_keeps_tmux_identity_outside_zellij():
    base = {"TMUX": "/s,1,0", "TMUX_PANE": "%1"}
    built = launch.build_env(
        launch.Profile("p", "other", ("sleep",)), "0" * 32, base)
    assert built["TMUX"] == "/s,1,0" and built["TMUX_PANE"] == "%1"


def test_claimed_tmux_launch_keeps_the_workspace_session():
    pending = launches.create_pending(launch.Profile("p", "other", ("sleep",)), "agents", "tmux")
    record = launches.claim(pending.launch_id, 4242, "1",
                            launch.pane_from_env({"TMUX": f"{SOCKET},1,0", "TMUX_PANE": "%3"}))
    assert (record.multiplexer, record.session, record.pane_id, record.socket_dir) == (
        "tmux", "agents", "%3", SOCKET)


@pytest.mark.parametrize("env_vars, expected", [
    ({"TMUX": f"{SOCKET},1,0"}, "tmux"),
    ({"TMUX": f"{SOCKET},1,0", "ZELLIJ": "0"}, "zellij"),
    ({}, "zellij"),
])
def test_sidebar_runs_on_the_enclosing_multiplexer(monkeypatch, capsys, env_vars, expected):
    for key in ("TMUX", "ZELLIJ"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env_vars.items():
        monkeypatch.setenv(key, value)
    requested = []

    class Outside:
        def in_session(self):
            return None

    monkeypatch.setattr(sidebar, "get_multiplexer",
                        lambda name: requested.append(name) or Outside())
    assert sidebar.main([]) == sidebar.EXIT_USAGE
    assert requested == [expected]


def _workflow():
    return WORKFLOW.read_text(encoding="utf-8")


def test_workflow_pins_actions_by_full_sha():
    uses = [m.group(1).strip() for m in re.finditer(r"^\s*-?\s*uses:\s*(.+)$", _workflow(), re.M)]
    assert uses
    for ref in uses:
        assert FULL_SHA_PIN.match(ref), ref


def test_workflow_checkouts_do_not_persist_credentials():
    lines = _workflow().splitlines()
    starts = [i for i, line in enumerate(lines) if re.match(r"^\s+- uses: actions/checkout@", line)]
    assert starts
    for start in starts:
        indent = len(lines[start]) - len(lines[start].lstrip())
        step = []
        for line in lines[start + 1:]:
            if line.strip() and len(line) - len(line.lstrip()) <= indent:
                break
            step.append(line.strip())
        assert "persist-credentials: false" in step


def test_workflow_is_read_only_installs_tmux_from_apt_and_records_it():
    text = _workflow()
    assert re.search(r"^permissions:\n  contents: read$", text, re.M)
    assert "contents: write" not in text and "id-token" not in text
    assert "apt-get install" in text and " tmux" in text
    assert "tmux -V" in text
    assert "python validation/tmux_workspace.py --no-write" in text
    assert re.search(r"^  group: tmux-mux-", text, re.M)


VALIDATION = ROOT / "validation"
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only validator")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validator():
    for name in ("probe_terminals", "workspace", "send"):
        sys.modules.setdefault(name, _load(name, VALIDATION / f"{name}.py"))
    return _load("tmux_workspace_validation", VALIDATION / "tmux_workspace.py")


@linux_only
def test_validator_env_is_isolated_with_a_private_tmux_tmpdir(tmp_path):
    validator = _validator()
    root = tmp_path / "root"
    root.mkdir()
    env = validator.tmux_env(root)
    assert env["TMUX_TMPDIR"] == str(root / "tmux-tmp")
    assert "TMUX" not in env and "TMUX_PANE" not in env
    assert env["HOME"] == str(root / "home")
    assert validator.socket_dir(root).startswith(str(root.resolve()))


@linux_only
def test_validator_server_refuses_a_socket_outside_the_root(tmp_path):
    validator = _validator()
    with pytest.raises(validator.Check):
        validator.Server("/tmp/tmux-1000/default", {}, "tmux", tmp_path)


@linux_only
def test_validator_profiles_are_fake_repls(tmp_path):
    validator = _validator()
    path = validator.write_config(tmp_path, python="/usr/bin/python3")
    text = path.read_text()
    for name in validator.PROFILES:
        assert f"[profiles.{name}]" in text
    assert text.count("fake_repl.py") == len(validator.PROFILES)
    assert 'harness = "other"' in text and "claude" not in text


@linux_only
def test_validator_evidence_keeps_runs_per_tmux_version(tmp_path):
    validator = _validator()
    path = tmp_path / "evidence.json"
    validator.write_evidence(path, {"tmux_version": "tmux 3.4", "result": "pass"})
    validator.write_evidence(path, {"tmux_version": "tmux 3.10a", "result": "pass"})
    validator.write_evidence(path, {"tmux_version": "tmux 3.4", "result": "fail"})
    document = json.loads(path.read_text())
    assert document["schema"] == validator.SCHEMA
    assert list(document["runs"]) == ["3.4", "3.10a"]
    assert document["runs"]["3.4"]["result"] == "fail"


def test_recorded_evidence_passes_and_holds_no_private_paths():
    path = VALIDATION / "evidence" / "tmux-workspace-linux-x86_64.json"
    text = path.read_text(encoding="utf-8")
    document = json.loads(text)
    assert document["schema"] == "tmux-workspace-validation/1"
    assert document["runs"]
    for version, run in document["runs"].items():
        assert run["result"] == "pass", version
        assert run["multi_line"]["submitted_prompts"] == 1
        assert run["multi_line"]["text_intact"] and run["multi_line"]["pastes_in_prompt"] == 1
        assert run["in_session"]["sessions_after"] == 1
        assert run["in_session"]["attached_clients"] == 0
        assert all(step["readback_active_pane_matches"] for step in run["focus"]["steps"])
        assert run["cleanup"]["survivors"] == 0
    for marker in ("/home/", "/tmp/", "/run/user/", "/Users/"):
        assert marker not in text
