"""``tongs-agents run PROFILE [DIR] [-- ARGS]``, the bare command and ``--version``."""

import json
import sys

import pytest

from tests.test_workspace_spawn import FakeMux, runtime_dir, write_profiles  # noqa: F401
from tests.test_workspace_tmux import FakeTmuxMux
from tongs_agents import __version__, cli, launches
from tongs_agents.commands import run, workspace
from tongs_agents.multiplexers import SESSION_EXITED, PaneRef
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

ZELLIJ = "/opt/zellij/bin/zellij"
LAUNCHER = [sys.executable, "-m", "tongs_agents.launch"]


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
    muxes = {"zellij": FakeMux(), "tmux": FakeTmuxMux()}

    def get(name="zellij", **kwargs):
        return muxes[name]

    monkeypatch.setattr(run, "get_multiplexer", get)
    monkeypatch.setattr(workspace, "get_multiplexer", get)
    monkeypatch.setattr(workspace, "zellij_binary", lambda: ZELLIJ)
    monkeypatch.setattr(workspace.shutil, "which", lambda name: None)
    execs = Exec()
    monkeypatch.setattr(workspace.os, "execvpe", execs)
    project = tmp_path / "project dir"
    project.mkdir()
    return muxes, execs, project


def _main(*argv):
    try:
        cli.main(list(argv))
    except SystemExit as raised:
        if raised.code == "exec":
            return "exec"
        return raised.code or 0
    return 0


def _records():
    return launches.list_records()


def _inside_zellij(monkeypatch, mux, session="agents"):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", session)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "7")
    mux.here = PaneRef("zellij", session, "7", None)


def test_parse_args_keeps_everything_after_the_separator():
    parser, _ = cli.build_parser()
    args = cli.parse_args(parser, ["run", "alpha", "--", "--resume", "-c", "--", "x y"])
    assert (args.profile, args.folder, args.extra) == (
        "alpha", None, ["--resume", "-c", "--", "x y"],
    )
    args = cli.parse_args(parser, ["run", "alpha", "/w", "--", "resume", "--last"])
    assert (args.folder, args.extra) == ("/w", ["resume", "--last"])
    args = cli.parse_args(parser, ["run", "--session", "s", "alpha"])
    assert (args.session, args.folder, args.extra) == ("s", None, [])


def test_an_option_before_the_separator_is_a_usage_error(env, capsys):
    assert _main("run", "alpha", "--resume") == 1
    assert "unrecognized arguments: --resume" in capsys.readouterr().err


def test_run_inside_zellij_spawns_one_pane_with_folder_and_args(env, monkeypatch, capsys):
    muxes, execs, project = env
    mux = muxes["zellij"]
    _inside_zellij(monkeypatch, mux)
    assert _main("run", "alpha", str(project), "--", "--resume", "two words") == 0
    assert execs.calls == []
    [record] = _records()
    assert (record.profile, record.session, record.state) == ("alpha", "agents", "pending")
    assert record.folder == str(project)
    assert record.args == ("--resume", "two words")
    agent, side = mux.spawned
    assert agent["argv"] == [*LAUNCHER, "alpha", "--launch-id", record.launch_id]
    assert (agent["session"], agent["name"], agent["focus"]) == ("agents", "alpha", False)
    assert side["name"] == SIDEBAR_PANE_NAME
    assert "started alpha in" in capsys.readouterr().err


def test_run_inside_zellij_refuses_another_session_or_multiplexer(env, monkeypatch, capsys):
    muxes, _, project = env
    _inside_zellij(monkeypatch, muxes["zellij"])
    assert _main("run", "--session", "other", "alpha", str(project)) == 2
    assert "refusing to use --session 'other'" in capsys.readouterr().err
    assert _main("run", "--multiplexer", "tmux", "alpha", str(project)) == 2
    assert "refusing --multiplexer tmux" in capsys.readouterr().err
    assert _records() == []


def test_run_inside_tmux_uses_the_current_tmux_session(env, monkeypatch):
    muxes, execs, project = env
    tmux = muxes["tmux"]
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%3")
    tmux.here = PaneRef("tmux", "work", "%3", "/tmp/tmux-1000/default")
    assert _main("run", "beta", str(project), "--", "resume", "--last") == 0
    assert execs.calls == []
    [record] = _records()
    assert (record.multiplexer, record.session, record.args) == (
        "tmux", "work", ("resume", "--last"),
    )
    assert [call["name"] for call in tmux.spawned] == ["beta", SIDEBAR_PANE_NAME]
    assert muxes["zellij"].spawned == []


def test_dir_defaults_to_the_current_directory(env, monkeypatch):
    muxes, _, project = env
    _inside_zellij(monkeypatch, muxes["zellij"])
    monkeypatch.chdir(project)
    assert _main("run", "alpha") == 0
    [record] = _records()
    assert record.folder == str(project)
    assert record.args == ()


def test_a_relative_dir_is_made_absolute(env, monkeypatch):
    muxes, _, project = env
    _inside_zellij(monkeypatch, muxes["zellij"])
    monkeypatch.chdir(project.parent)
    assert _main("run", "alpha", project.name) == 0
    assert _records()[0].folder == str(project)


def test_a_missing_folder_is_refused_before_anything_starts(env, monkeypatch, capsys, tmp_path):
    muxes, execs, _ = env
    _inside_zellij(monkeypatch, muxes["zellij"])
    assert _main("run", "alpha", str(tmp_path / "nowhere")) == 2
    assert "does not exist" in capsys.readouterr().err
    afile = tmp_path / "file"
    afile.write_text("")
    assert _main("run", "alpha", str(afile)) == 2
    assert "is not a directory" in capsys.readouterr().err
    assert _records() == []
    assert muxes["zellij"].spawned == [] and execs.calls == []


def test_args_with_a_nul_are_refused(env, monkeypatch, capsys):
    muxes, _, project = env
    _inside_zellij(monkeypatch, muxes["zellij"])
    assert _main("run", "alpha", str(project), "--", "a\0b") == 2
    assert "NUL" in capsys.readouterr().err
    assert _records() == []


def test_an_unknown_profile_is_refused(env, capsys):
    assert _main("run", "nope") == 2
    assert "unknown profile 'nope'" in capsys.readouterr().err


def test_outside_a_running_zellij_session_adds_the_pane_without_attaching(env, capsys):
    muxes, execs, project = env
    mux = muxes["zellij"]
    mux.exists = True
    assert _main("run", "alpha", str(project), "--", "--resume") == 0
    assert execs.calls == []
    assert [(call["session"], call["name"]) for call in mux.spawned] == [
        ("agents", "alpha"), ("agents", SIDEBAR_PANE_NAME),
    ]
    assert _records()[0].args == ("--resume",)
    assert "attach with 'tongs-agents'" in capsys.readouterr().err


def test_outside_without_a_session_starts_an_empty_workspace_with_the_pane(
    env, runtime_dir,  # noqa: F811
):
    muxes, execs, project = env
    assert _main("run", "alpha", str(project), "--", "--resume") == "exec"
    (_, argv, _), = execs.calls
    layout = next((runtime_dir / "layouts").iterdir())
    assert argv[:5] == [ZELLIJ, "--session", "agents", "--new-session-with-layout",
                        str(layout)]
    request = json.loads((runtime_dir / "requests" / f"{layout.stem}.json").read_text())
    [record] = _records()
    assert request["launches"] == [record.launch_id]
    assert (record.profile, record.folder, record.args) == (
        "alpha", str(project), ("--resume",),
    )
    assert muxes["zellij"].spawned == []


def test_outside_an_exited_session_is_deleted_then_started(env):
    muxes, execs, project = env
    mux = muxes["zellij"]
    mux.state = SESSION_EXITED
    assert _main("run", "alpha", str(project)) == "exec"
    assert ("delete_session", "agents") in mux.calls


def test_outside_tmux_creates_the_session_then_attaches(env, tmp_path):
    muxes, execs, project = env
    tmux = muxes["tmux"]
    assert _main("run", "--multiplexer", "tmux", "--session", "work", "beta",
                 str(project), "--", "-c") == "exec"
    assert [call["name"] for call in tmux.spawned] == [SIDEBAR_PANE_NAME, "beta"]
    (_, argv, _), = execs.calls
    assert argv[-1] == "=work"
    assert _records()[0].args == ("-c",)


def test_outside_tmux_refuses_a_bad_folder_before_creating_the_session(env, tmp_path):
    muxes, execs, _ = env
    assert _main("run", "--multiplexer", "tmux", "beta", str(tmp_path / "gone")) == 2
    assert muxes["tmux"].spawned == [] and execs.calls == []


def test_run_is_unsupported_on_windows(env, monkeypatch, capsys):
    monkeypatch.setattr(workspace, "_is_windows", lambda: True)
    assert _main("run", "alpha") == 2
    assert "unsupported on Windows" in capsys.readouterr().err


def test_bare_command_opens_an_empty_workspace(env, tmp_path, runtime_dir):  # noqa: F811
    muxes, execs, _ = env
    path = tmp_path / "config" / "config.toml"
    path.write_text(path.read_text() + '[workspace]\ndefault_profiles = ["alpha", "beta"]\n')
    assert _main() == "exec"
    (_, argv, _), = execs.calls
    assert argv[1:3] == ["--session", "agents"]
    request = next((runtime_dir / "requests").iterdir())
    assert json.loads(request.read_text())["launches"] == []
    assert _records() == []


def test_bare_command_attaches_to_a_running_workspace_without_new_agents(env):
    muxes, execs, _ = env
    mux = muxes["zellij"]
    mux.exists = True
    assert _main() == "exec"
    (_, argv, _), = execs.calls
    assert argv == [ZELLIJ, "attach", "agents"]
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME]


def test_the_workspace_verb_still_starts_the_default_profiles(env, tmp_path):
    muxes, execs, _ = env
    path = tmp_path / "config" / "config.toml"
    path.write_text(path.read_text() + '[workspace]\ndefault_profiles = ["alpha"]\n')
    assert _main("workspace") == "exec"
    assert [record.profile for record in _records()] == ["alpha"]


def test_version(capsys):
    assert _main("--version") == 0
    assert capsys.readouterr().out.strip() == f"tongs-agents {__version__}"


def test_help_mentions_the_bare_command_and_run(capsys):
    assert _main("--help") == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Without a command it opens the workspace with only the sidebar" in out
    assert "run" in out


def test_run_help_shows_the_separator(capsys):
    assert _main("run", "--help") == 0
    out = capsys.readouterr().out
    assert "PROFILE [DIR] [-- ARGS ...]" in out
    assert "-- --resume" in out


def test_a_relative_dir_without_a_current_directory_is_refused(env, monkeypatch, capsys):
    def gone():
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(run.os, "getcwd", gone)
    assert _main("run", "alpha", "sub") == 2
    assert "give an absolute path" in capsys.readouterr().err
