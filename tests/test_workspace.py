"""``tongs-agents workspace``: exec argv, in-session spawning, layout and doctor."""

import argparse
import json
import os
import re
import shlex
import stat
import sys

import pytest

from tests.test_workspace_spawn import FakeMux, runtime_dir, write_profiles  # noqa: F401
from tongs_agents import cli, launches, workspace_spawn
from tongs_agents.commands import workspace
from tongs_agents.launches import LaunchError
from tongs_agents.multiplexers import HOME_PANE_NAME, SESSION_EXITED, PaneInfo, PaneRef
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

ZELLIJ = "/opt/zellij/bin/zellij"
TONGS = "/opt/tongs/bin/tongs"
TOKEN = "0123456789abcdef0123456789abcdef"


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
    mux = FakeMux()
    monkeypatch.setattr(workspace, "get_multiplexer", lambda name="zellij": mux)
    monkeypatch.setattr(workspace, "zellij_binary", lambda: ZELLIJ)
    tools = {"tongs": TONGS}
    monkeypatch.setattr(workspace.shutil, "which", lambda name: tools.get(name))
    execs = Exec()
    monkeypatch.setattr(workspace.os, "execvpe", execs)
    monkeypatch.setattr(workspace.os, "execvp", execs)
    return mux, execs, tools


def _run(*argv):
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["workspace", *argv])
    try:
        return handlers["workspace"](args)
    except SystemExit as raised:
        assert raised.code == "exec"
        return "exec"


def _inside(monkeypatch, mux, session="agents"):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", session)
    monkeypatch.setenv("ZELLIJ_PANE_ID", "7")
    mux.here = PaneRef("zellij", session, "7", None)


def test_new_session_execs_the_create_argv(env, monkeypatch, runtime_dir):  # noqa: F811
    mux, execs, _ = env
    monkeypatch.setenv("TMUX", "/tmp/tmux-1/default,1,0")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", "/run/sockets")
    assert _run("--profile", "alpha", "--profile", "beta") == "exec"
    (file, argv, exec_env), = execs.calls
    layouts = list((runtime_dir / "layouts").iterdir())
    assert len(layouts) == 1
    token = layouts[0].stem
    assert file == ZELLIJ
    assert argv == [ZELLIJ, "--session", "agents", "--new-session-with-layout", str(layouts[0])]
    assert "TMUX" not in exec_env and "TMUX_PANE" not in exec_env
    assert exec_env["ZELLIJ_SOCKET_DIR"] == "/run/sockets"
    assert mux.spawned == []
    request = json.loads((runtime_dir / "requests" / f"{token}.json").read_text())
    records = launches.list_records("agents")
    assert sorted(request["launches"]) == sorted(record.launch_id for record in records)
    assert sorted(record.profile for record in records) == ["alpha", "beta"]
    assert all(record.state == "pending" for record in records)
    text = layouts[0].read_text()
    assert f'"--spawn-request" "{token}"' in text
    assert f'command="{TONGS}"' in text
    assert f'command="{sys.executable}"' in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_layout_and_request_files_are_0600(env, runtime_dir):  # noqa: F811
    _run("--profile", "alpha")
    files = [*(runtime_dir / "layouts").iterdir(), *(runtime_dir / "requests").iterdir()]
    assert len(files) == 2
    assert {stat.S_IMODE(path.stat().st_mode) for path in files} == {0o600}
    assert stat.S_IMODE((runtime_dir / "layouts").stat().st_mode) == 0o700


def test_existing_session_spawns_then_attaches(env, runtime_dir):  # noqa: F811
    mux, execs, _ = env
    mux.exists = True
    assert _run("--profile", "alpha", "--session", "work") == "exec"
    (file, argv, _), = execs.calls
    assert argv == [ZELLIJ, "attach", "work"]
    assert [call["name"] for call in mux.spawned] == ["alpha", SIDEBAR_PANE_NAME]
    assert all(call["session"] == "work" for call in mux.spawned)
    assert ("session_state", "work") in mux.calls
    assert ("delete_session", "work") not in mux.calls
    assert not os.path.lexists(runtime_dir / "layouts")


def _launch_files(tmp_path):
    return sorted(path.name for path in tmp_path.rglob("launches/*"))


def _runtime_files(directory):
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.rglob("*") if path.is_file())


def test_print_command_help_says_it_is_not_a_dry_run():
    parser = argparse.ArgumentParser()
    workspace.add_arguments(parser)
    text = " ".join(parser.format_help().split())
    assert "Not a dry run" in text
    assert "tmux creates a missing session detached and starts its agents" in text


def test_print_command_for_a_running_session_spawns_nothing(
    env, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    mux, execs, _ = env
    mux.exists = True
    capsys.readouterr()
    assert _run("--print-command", "--profile", "alpha") == 0
    assert execs.calls == []
    captured = capsys.readouterr()
    assert captured.out.strip() == shlex.join([ZELLIJ, "attach", "agents"])
    assert "does not start alpha" in captured.err
    assert mux.spawned == []
    assert ("list_panes", "agents") not in mux.calls
    assert launches.list_records() == []
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


def test_print_command_for_a_new_session(env, capsys, tmp_path, runtime_dir):  # noqa: F811
    _, execs, _ = env
    capsys.readouterr()
    assert _run("--print-command", "--profile", "alpha", "--no-tongs") == 0
    assert execs.calls == []
    layout, = (runtime_dir / "layouts").iterdir()
    captured = capsys.readouterr()
    assert shlex.split(captured.out) == [
        ZELLIJ, "--session", "agents", "--new-session-with-layout", str(layout)
    ]
    assert "prepared a one-shot spawn request" in captured.err
    assert 'tab name="tongs"' not in layout.read_text()
    assert [record.state for record in launches.list_records("agents")] == ["pending"]
    assert len(_launch_files(tmp_path)) == 1


def test_exited_session_is_deleted_then_created(env, capsys, runtime_dir):  # noqa: F811
    mux, execs, _ = env
    mux.state = SESSION_EXITED
    capsys.readouterr()
    assert _run("--profile", "alpha") == "exec"
    (_, argv, _), = execs.calls
    assert argv[:4] == [ZELLIJ, "--session", "agents", "--new-session-with-layout"]
    state = mux.calls.index(("session_state", "agents"))
    assert mux.calls[state + 1] == ("delete_session", "agents")
    assert [call for call in mux.calls if call[0] == "delete_session"] == [
        ("delete_session", "agents")
    ]
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "is EXITED; deleting it" in err and "--resurrect" in err
    assert mux.spawned == []
    assert [record.profile for record in launches.list_records("agents")] == ["alpha"]


def test_exited_session_delete_failure_writes_nothing(
    env, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    mux, execs, _ = env
    mux.state = SESSION_EXITED
    mux.fail_delete = True
    assert _run("--profile", "alpha") == 1
    assert execs.calls == []
    assert "cannot delete agents" in capsys.readouterr().err
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


def test_resurrect_attaches_to_an_exited_session(
    env, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    mux, execs, _ = env
    mux.state = SESSION_EXITED
    capsys.readouterr()
    assert _run("--resurrect", "--profile", "alpha") == "exec"
    (_, argv, _), = execs.calls
    assert argv == [ZELLIJ, "attach", "agents"]
    assert ("delete_session", "agents") not in mux.calls
    assert mux.spawned == []
    assert "not starting alpha" in capsys.readouterr().err
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


def test_resurrect_print_command_has_no_side_effects(
    env, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    mux, execs, _ = env
    mux.state = SESSION_EXITED
    capsys.readouterr()
    assert _run("--resurrect", "--print-command") == 0
    assert capsys.readouterr().out.strip() == shlex.join([ZELLIJ, "attach", "agents"])
    assert execs.calls == [] and mux.spawned == []
    assert ("delete_session", "agents") not in mux.calls
    assert _runtime_files(runtime_dir) == []


def test_print_command_refuses_to_delete_an_exited_session(
    env, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    mux, execs, _ = env
    mux.state = SESSION_EXITED
    assert _run("--print-command", "--profile", "alpha") == 1
    err = capsys.readouterr().err
    assert "zellij delete-session agents" in err and "--resurrect" in err
    assert ("delete_session", "agents") not in mux.calls
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


@pytest.mark.parametrize("exists", [True, False])
def test_resurrect_without_an_exited_session_changes_nothing(env, exists):
    mux, execs, _ = env
    mux.exists = exists
    assert _run("--resurrect", "--profile", "alpha") == "exec"
    (_, argv, _), = execs.calls
    if exists:
        assert argv == [ZELLIJ, "attach", "agents"]
        assert [call["name"] for call in mux.spawned] == ["alpha", SIDEBAR_PANE_NAME]
    else:
        assert argv[1:4] == ["--session", "agents", "--new-session-with-layout"]
    assert ("delete_session", "agents") not in mux.calls


def test_inside_zellij_ignores_resurrect_with_a_notice(env, monkeypatch, capsys):
    mux, execs, _ = env
    _inside(monkeypatch, mux)
    assert _run("--resurrect", "--profile", "alpha") == 0
    assert "ignoring --resurrect" in capsys.readouterr().err
    assert execs.calls == []
    assert [call["name"] for call in mux.spawned] == ["alpha", SIDEBAR_PANE_NAME]


def _fail(*args, **kwargs):
    raise OSError(28, "No space left on device")


@pytest.mark.parametrize("step", ["write_spawn_request", "render_layout", "write_layout"])
def test_failures_before_exec_discard_the_launch_records(env, monkeypatch, capsys, tmp_path,
                                                         runtime_dir, step):  # noqa: F811
    _, execs, _ = env
    if step == "render_layout":
        monkeypatch.setattr(workspace, "render_layout", _fail)
    else:
        monkeypatch.setattr(workspace_spawn, step, _fail)
    assert _run("--profile", "alpha", "--profile", "beta") == 1
    assert execs.calls == []
    assert "No space left" in capsys.readouterr().err
    assert launches.list_records() == []
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


def test_a_partial_create_discards_the_records_already_written(env, monkeypatch, tmp_path):
    real = launches.create_pending
    calls = []

    def second_fails(profile, session, multiplexer, **options):
        calls.append(profile.name)
        if len(calls) == 2:
            raise LaunchError("disk full")
        return real(profile, session, multiplexer, **options)

    monkeypatch.setattr(launches, "create_pending", second_fails)
    assert _run("--profile", "alpha", "--profile", "beta") == 1
    assert calls == ["alpha", "beta"]
    assert _launch_files(tmp_path) == []


def test_exec_failure_discards_a_new_session(
    env, monkeypatch, capsys, tmp_path, runtime_dir,
):  # noqa: F811
    def missing(file, argv, env):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(workspace.os, "execvpe", missing)
    assert _run("--profile", "alpha") == 1
    assert "cannot run" in capsys.readouterr().err
    assert _launch_files(tmp_path) == []
    assert _runtime_files(runtime_dir) == []


def test_a_failed_spawn_into_a_running_session_discards_its_record(env):
    mux, execs, _ = env
    mux.exists = True
    mux.fail_spawn = {"beta"}
    assert _run("--profile", "alpha", "--profile", "beta") == 1
    assert execs.calls == []
    assert [record.profile for record in launches.list_records()] == ["alpha"]


def test_print_alias_only_prints(env, capsys, runtime_dir, tmp_path):  # noqa: F811
    mux, execs, tools = env
    tools["tongs-agents"] = "/opt/venv/bin/tongs-agents"
    home = tmp_path / "rc-home"
    capsys.readouterr()
    assert _run("--print-alias", "--profile", "alpha", "--no-tongs") == 0
    out = capsys.readouterr().out
    assert out == (
        "alias agents='/opt/venv/bin/tongs-agents workspace --profile alpha --no-tongs'\n"
    )
    assert execs.calls == [] and mux.spawned == [] and mux.calls == []
    assert not os.path.lexists(runtime_dir)
    assert not home.exists()


def test_inside_zellij_spawns_and_adds_the_sidebar(env, monkeypatch):
    mux, execs, _ = env
    _inside(monkeypatch, mux)
    assert _run("--profile", "alpha", "--profile", "beta") == 0
    assert execs.calls == []
    assert [call["name"] for call in mux.spawned] == ["alpha", "beta", SIDEBAR_PANE_NAME]
    assert all(call["session"] == "agents" for call in mux.spawned)


def test_inside_zellij_keeps_an_existing_sidebar(env, monkeypatch):
    mux, execs, _ = env
    mux.panes.append(PaneInfo(PaneRef("zellij", "work", "0"), SIDEBAR_PANE_NAME, None, False, False))
    _inside(monkeypatch, mux, "work")
    assert _run("--profile", "alpha", "--session", "work") == 0
    assert execs.calls == []
    assert [call["name"] for call in mux.spawned] == ["alpha"]


def test_inside_zellij_refuses_another_session(env, monkeypatch):
    mux, execs, _ = env
    _inside(monkeypatch, mux)
    assert _run("--profile", "alpha", "--session", "other") == 2
    assert execs.calls == [] and mux.spawned == []


def test_inside_zellij_never_execs_print_command(env, monkeypatch):
    mux, execs, _ = env
    _inside(monkeypatch, mux)
    assert _run("--print-command") == 2
    assert execs.calls == [] and mux.spawned == []


def test_unknown_profiles_are_refused(env, capsys):
    mux, execs, _ = env
    assert _run("--profile", "alpha", "--profile", "nope") == 2
    assert "nope" in capsys.readouterr().err
    assert execs.calls == [] and mux.spawned == [] and mux.calls == []


def test_unavailable_zellij_points_at_doctor(env, capsys):
    mux, execs, _ = env
    mux._available = (False, "zellij not found on PATH")
    assert _run("--profile", "alpha") == 1
    assert "tongs-agents doctor" in capsys.readouterr().err
    assert execs.calls == [] and mux.spawned == []


def test_windows_is_unsupported(env, monkeypatch, capsys):
    mux, execs, _ = env
    monkeypatch.setattr(workspace, "_is_windows", lambda: True)
    assert _run("--profile", "alpha") == 2
    assert "unsupported" in capsys.readouterr().err
    assert execs.calls == [] and mux.calls == []


@pytest.mark.parametrize("session", ['a"b', "a{b", "a}b", "a\nb", "a b", "-x", "", "a;b", "é"])
def test_hostile_session_names_are_refused(env, session):
    mux, execs, _ = env
    assert _run(f"--session={session}") == 2
    assert execs.calls == [] and mux.calls == []


@pytest.mark.parametrize("session", ['a"b', "a{b}", "x\ny", 'agents" {', "a\\b"])
def test_render_layout_refuses_hostile_sessions(session):
    with pytest.raises(workspace.LayoutError):
        workspace.render_layout("/usr/bin/python3", session, TOKEN, None)


@pytest.mark.parametrize("python", [
    "/usr/bin/py\nthon", "/usr/bin/py\x00thon", "/usr/bin/py\x1bthon", "/usr/bin/py x",
    "relative/python", "",
])
def test_render_layout_refuses_control_characters_and_relative_paths(python):
    with pytest.raises(workspace.LayoutError):
        workspace.render_layout(python, "agents", TOKEN, None)
    with pytest.raises(workspace.LayoutError):
        workspace.render_layout("/usr/bin/python3", "agents", TOKEN, python or "x")


@pytest.mark.parametrize("token", ["", "x" * 32, TOKEN + "0", '" } tab {'])
def test_render_layout_refuses_bad_tokens(token):
    with pytest.raises(workspace.LayoutError):
        workspace.render_layout("/usr/bin/python3", "agents", token, None)


def test_render_layout_escapes_quotes_and_backslashes():
    hostile = '/opt/a "b" {c} \\d @SESSION@/python'
    text = workspace.render_layout(hostile, "agents", TOKEN, '/opt/"t"/tongs')
    assert 'command="/opt/a \\"b\\" {c} \\\\d @SESSION@/python"' in text
    assert 'command="/opt/\\"t\\"/tongs"' in text
    # The sidebar and the home pane both run the interpreter.
    assert text.count("@SESSION@") == 2
    split = workspace.render_layout(hostile, "agents", TOKEN, None, layout="split")
    assert split.count("@SESSION@") == 1
    assert "@PYTHON@" not in text and "@TOKEN@" not in text and "@TONGS@" not in text


def test_render_layout_without_tongs_drops_the_tab():
    text = workspace.render_layout("/usr/bin/python3", "agents", TOKEN, None)
    assert 'tab name="tongs"' not in text
    assert "@TONGS" not in text
    assert text.count("{") == text.count("}")
    with_tongs = workspace.render_layout("/usr/bin/python3", "agents", TOKEN, TONGS)
    assert 'tab name="tongs"' in with_tongs and "TONGS_TAB" not in with_tongs
    assert with_tongs.count("{") == with_tongs.count("}")


def test_template_names_the_sidebar_and_its_module():
    text = workspace.load_template()
    assert f'pane name="{SIDEBAR_PANE_NAME}"' in text
    assert f'"-m" "{workspace_spawn.SIDEBAR_MODULE}"' in text
    assert set(re.findall(r"@([A-Z_]+)@", text)) == {
        "PYTHON", "SESSION", "TOKEN", "TONGS", "TONGS_TAB_BEGIN", "TONGS_TAB_END",
        "SINGLE_BEGIN", "SINGLE_END", "SPLIT_BEGIN", "SPLIT_END",
    }


def test_single_layout_stacks_a_home_pane_beside_the_sidebar():
    text = workspace.render_layout("/usr/bin/python3", "agents", TOKEN, None)
    assert "BEGIN@" not in text and "END@" not in text
    assert text.count(f'pane name="{SIDEBAR_PANE_NAME}"') == 1
    assert 'pane split_direction="vertical"' in text
    assert "pane stacked=true {" in text
    stack = text[text.index("pane stacked=true"):]
    assert f'pane name="{HOME_PANE_NAME}" command="/usr/bin/python3"' in stack
    assert f'"-m" "{workspace_spawn.HOME_MODULE}" "{workspace_spawn.HOME_COMMAND}"' in stack
    assert text.index(SIDEBAR_PANE_NAME) < text.index(HOME_PANE_NAME)
    assert f"--spawn-request\" \"{TOKEN}\"" in text
    assert text.count("{") == text.count("}")


def test_split_layout_is_the_sidebar_alone():
    text = workspace.render_layout("/usr/bin/python3", "agents", TOKEN, TONGS, layout="split")
    assert "BEGIN@" not in text and "END@" not in text
    assert text.count(f'pane name="{SIDEBAR_PANE_NAME}"') == 1
    assert HOME_PANE_NAME not in text and "stacked" not in text
    assert "split_direction" not in text
    assert 'tab name="tongs"' in text
    assert text.count("{") == text.count("}")


def test_render_layout_refuses_an_unknown_layout():
    with pytest.raises(workspace.LayoutError, match="layout"):
        workspace.render_layout("/usr/bin/python3", "agents", TOKEN, None, layout="tabs")


def test_exec_failure_is_reported(env, monkeypatch, capsys):
    mux, _, _ = env
    mux.exists = True

    def missing(file, argv, env):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(workspace.os, "execvpe", missing)
    assert _run() == 1
    assert "cannot run" in capsys.readouterr().err


@pytest.mark.parametrize(("version", "level"), [
    ((0, 44, 1), "error"), ((0, 44, 2), "ok"), ((0, 45, 1), "ok"), ((0, 46, 0), "warn"),
    (None, "warn"),
])
def test_doctor_zellij_levels(monkeypatch, version, level):
    monkeypatch.setattr(workspace, "zellij_binary", lambda: ZELLIJ)
    monkeypatch.setattr(workspace, "zellij_version", lambda binary: version)
    checks = [check for check in workspace.doctor_checks() if check.name == "workspace-zellij"]
    assert [check.level for check in checks] == [level]


def test_doctor_without_zellij_is_informational(monkeypatch):
    monkeypatch.setattr(workspace, "zellij_binary", lambda: None)
    checks = workspace.doctor_checks()
    assert [(check.name, check.level) for check in checks] == [("workspace-zellij", "info")]


def test_doctor_warns_about_a_temporary_interpreter(tmp_path):
    import tempfile

    temp_python = os.path.join(tempfile.gettempdir(), "venv-x", "bin", "python")
    warned = workspace._interpreter_checks(temp_python)
    assert [check.level for check in warned] == ["warn"]
    assert workspace._interpreter_checks("/opt/venv/bin/python") == []


def test_doctor_reports_the_workspace_checks(monkeypatch):
    from tongs_agents.commands import doctor

    monkeypatch.setattr(workspace, "zellij_binary", lambda: None)
    names = [check.name for check in doctor.module_checks()]
    assert "workspace-zellij" in names


def _app_config(tmp_path, text):
    path = write_profiles(tmp_path, ["alpha", "beta"])
    path.write_text(path.read_text() + text)
    return path


def test_workspace_section_supplies_session_profiles_and_tongs_tab(
    env, tmp_path, runtime_dir,  # noqa: F811
):
    _, execs, _ = env
    _app_config(tmp_path, '[workspace]\nsession = "work"\n'
                          'default_profiles = ["beta"]\ntongs_tab = false\n')
    assert _run() == "exec"
    (_, argv, _), = execs.calls
    assert argv[:3] == [ZELLIJ, "--session", "work"]
    assert [record.profile for record in launches.list_records("work")] == ["beta"]
    layout, = (runtime_dir / "layouts").iterdir()
    text = layout.read_text()
    assert TONGS not in text and 'tab name="tongs"' not in text


def test_flags_override_the_workspace_section(env, tmp_path, runtime_dir):  # noqa: F811
    _, execs, _ = env
    _app_config(tmp_path, '[workspace]\nsession = "work"\n'
                          'default_profiles = ["beta"]\ntongs_tab = false\n')
    assert _run("--session", "other", "--profile", "alpha", "--tongs") == "exec"
    (_, argv, _), = execs.calls
    assert argv[:3] == [ZELLIJ, "--session", "other"]
    assert [record.profile for record in launches.list_records()] == ["alpha"]
    layout, = (runtime_dir / "layouts").iterdir()
    assert f'command="{TONGS}"' in layout.read_text()


def test_default_profiles_only_start_a_new_session(env, tmp_path, monkeypatch):
    mux, execs, _ = env
    _app_config(tmp_path, '[workspace]\ndefault_profiles = ["alpha", "beta"]\n')
    mux.exists = True
    assert _run() == "exec"
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME]
    mux.spawned.clear()
    mux.panes.clear()
    _inside(monkeypatch, mux)
    assert _run() == 0
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME]
    assert launches.list_records() == []


def test_an_unknown_default_profile_is_a_config_error(env, tmp_path, capsys):
    mux, execs, _ = env
    path = _app_config(tmp_path, '[workspace]\ndefault_profiles = ["gamma"]\n')
    assert _run() == 1
    err = capsys.readouterr().err
    assert f"{path}: workspace.default_profiles: unknown profile 'gamma'" in err
    assert execs.calls == [] and mux.spawned == []
    assert launches.list_records() == []


def test_an_unknown_default_profile_does_not_block_attaching(env, tmp_path):
    mux, execs, _ = env
    mux.exists = True
    _app_config(tmp_path, '[workspace]\ndefault_profiles = ["gamma"]\n')
    assert _run() == "exec"
    (_, argv, _), = execs.calls
    assert argv == [ZELLIJ, "attach", "agents"]


def test_an_invalid_workspace_section_is_refused(env, tmp_path, capsys):
    mux, execs, _ = env
    path = _app_config(tmp_path, '[workspace]\nsession = "bad name"\n')
    assert _run("--session", "fine") == 1
    assert f"{path}: workspace.session" in capsys.readouterr().err
    assert execs.calls == [] and mux.calls == []


def test_print_alias_keeps_an_explicit_tongs_flag(env, capsys):
    _, _, tools = env
    tools["tongs-agents"] = "/opt/venv/bin/tongs-agents"
    capsys.readouterr()
    assert _run("--print-alias", "--tongs") == 0
    out = capsys.readouterr().out
    assert out == "alias agents='/opt/venv/bin/tongs-agents workspace --tongs'\n"


def _write_layout_config(tmp_path, layout):
    path = tmp_path / "config" / "config.toml"
    path.write_text(path.read_text() + f'\n[workspace]\nlayout = "{layout}"\n')


def test_new_session_defaults_to_the_single_layout(env, runtime_dir):  # noqa: F811
    assert _run("--profile", "alpha") == "exec"
    (layout,) = (runtime_dir / "layouts").iterdir()
    text = layout.read_text()
    assert f'pane name="{HOME_PANE_NAME}"' in text and "stacked=true" in text


def test_layout_split_in_the_config_renders_the_split_layout(
    env, tmp_path, runtime_dir,  # noqa: F811
):
    _write_layout_config(tmp_path, "split")
    assert _run("--profile", "alpha") == "exec"
    (layout,) = (runtime_dir / "layouts").iterdir()
    text = layout.read_text()
    assert HOME_PANE_NAME not in text and "stacked" not in text
    assert f'pane name="{SIDEBAR_PANE_NAME}"' in text


def test_an_unknown_layout_in_the_config_is_refused(env, tmp_path, capsys):
    mux, execs, _ = env
    _write_layout_config(tmp_path, "tabs")
    assert _run("--profile", "alpha") == 1
    assert "workspace.layout: must be one of single, split" in capsys.readouterr().err
    assert execs.calls == [] and mux.spawned == []


def test_profiles_spawned_into_a_running_session_are_shown(env):
    mux, _, _ = env
    mux.exists = True
    assert _run("--profile", "alpha", "--profile", "beta", "--session", "work") == "exec"
    assert [ref.pane_id for ref in mux.shown] == ["1", "2"]
    assert mux.focused == []


def test_inside_zellij_shows_each_new_profile(env, monkeypatch):
    mux, _, _ = env
    _inside(monkeypatch, mux)
    assert _run("--profile", "alpha") == 0
    assert [ref.pane_id for ref in mux.shown] == ["1"]
