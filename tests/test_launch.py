"""The in-pane launcher: claim, environment, argv and exit codes."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tongs_agents import launch, launches, paths
from tongs_agents.multiplexers.base import PaneRef
from tongs_agents.profiles import Profile


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX exec semantics")

FIXTURE = textwrap.dedent("""
    import json, os, sys
    from tongs_agents import discovery, launches
    record = launches.get(os.environ["TONGS_AGENTS_LAUNCH_ID"])
    assert record is not None, "no launch record"
    assert record.pid == os.getpid(), (record.pid, os.getpid())
    info = discovery.get_process_info(os.getpid())
    if info is not None:
        assert record.process_start == info.start_token
    names = ("TONGS_AGENTS_LAUNCH_ID", "TONGS_AGENTS_PROFILE", "CLAUDE_CONFIG_DIR",
             "EXAMPLE_FLAG", "TMUX", "TMUX_PANE", "ZELLIJ_PANE_ID")
    json.dump({
        "argv": sys.argv[1:],
        "cwd": os.getcwd(),
        "env": {name: os.environ.get(name) for name in names},
        "record": record.to_json(),
    }, open(os.environ["FIXTURE_OUT"], "w"))
""")


@pytest.fixture
def config(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    directory.mkdir()
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(directory))

    def write(text):
        path = directory / "config.toml"
        path.write_text(text)
        path.chmod(0o600)
        return path

    return write


def pending(name="claude-alt", harness="claude"):
    profile = Profile(name=name, harness=harness, argv=("x",))
    return launches.create_pending(profile, "agents", "zellij")


@posix_only
def test_exec_into_fixture_reads_its_own_record(config, tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text(FIXTURE)
    out = tmp_path / "out.json"
    workdir = tmp_path / "work dir"
    workdir.mkdir()
    agent_config = tmp_path / "claude-alt"
    tricky = ["a b", "$(touch pwned)", "x;y", "*", ""]
    config(f"""
[profiles.claude-alt]
command = {json.dumps([sys.executable, str(script), *tricky])}
harness = "claude"
config_dir = {json.dumps(str(agent_config))}
model_args = ["--model", "m 1"]
effort_args = ["--effort", "high"]
cwd = {json.dumps(str(workdir))}
env = {{ EXAMPLE_FLAG = "on" }}
""")
    record = pending()
    env = dict(os.environ)
    env.update({
        "FIXTURE_OUT": str(out),
        "ZELLIJ": "0",
        "ZELLIJ_SESSION_NAME": "agents",
        "ZELLIJ_PANE_ID": "12",
        "ZELLIJ_SOCKET_DIR": str(tmp_path / "zellij-sock"),
        "TMUX": "/tmp/tmux-1/default,1,0",
        "TMUX_PANE": "%4",
        "TONGS_AGENTS_LAUNCH_ID": "f" * 32,
    })
    result = subprocess.run(
        [sys.executable, "-m", "tongs_agents.launch", "claude-alt",
         "--launch-id", record.launch_id],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        timeout=60, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    seen = json.loads(out.read_text())
    assert seen["argv"] == [*tricky, "--model", "m 1", "--effort", "high"]
    assert seen["cwd"] == str(workdir)
    assert not (tmp_path / "pwned").exists()
    assert seen["env"] == {
        "TONGS_AGENTS_LAUNCH_ID": record.launch_id,
        "TONGS_AGENTS_PROFILE": "claude-alt",
        "CLAUDE_CONFIG_DIR": str(agent_config),
        "EXAMPLE_FLAG": "on",
        "TMUX": None,
        "TMUX_PANE": None,
        "ZELLIJ_PANE_ID": "12",
    }
    stored = seen["record"]
    assert stored["state"] == "running"
    assert (stored["multiplexer"], stored["session"], stored["pane_id"]) == (
        "zellij", "agents", "12"
    )
    assert stored["socket_dir"] == str(tmp_path / "zellij-sock")
    assert stored["replaces"] is None


@posix_only
def test_relaunch_of_claimed_id_gets_fresh_record(config, tmp_path):
    script = tmp_path / "fixture.py"
    script.write_text(FIXTURE)
    out = tmp_path / "out.json"
    config(f'[profiles.p]\ncommand = {json.dumps([sys.executable, str(script)])}\n')
    record = pending("p", "other")
    env = {**os.environ, "FIXTURE_OUT": str(out)}
    for key in ("ZELLIJ", "TMUX_PANE"):
        env.pop(key, None)
    command = [sys.executable, "-m", "tongs_agents.launch", "p", "--launch-id", record.launch_id]
    for _ in range(2):
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
    second = json.loads(out.read_text())
    assert second["record"]["replaces"] == record.launch_id
    assert second["env"]["TONGS_AGENTS_LAUNCH_ID"] == second["record"]["launch_id"]
    assert launches.get(record.launch_id).state == "running"


@pytest.fixture
def no_exec(monkeypatch):
    calls = []

    def fake_execvpe(file, args, env):
        calls.append((file, list(args), dict(env)))
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(launch.os, "execvpe", fake_execvpe)
    return calls


def test_argv_is_passed_as_a_list(config, no_exec, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config('[profiles.codex-review]\ncommand = ["codex", "--profile", "review"]\n'
           'model_args = ["-m", "a b"]\n')
    record = pending("codex-review", "codex")
    assert launch.main(["codex-review", "--launch-id", record.launch_id]) == 127
    file, args, env = no_exec[0]
    assert file == "codex"
    assert args == ["codex", "--profile", "review", "--no-daemon", "-m", "a b"]
    assert env["TONGS_AGENTS_PROFILE"] == "codex-review"
    assert launches.get(record.launch_id).pid == os.getpid()


@pytest.mark.parametrize("argv, expected", [
    (("codex",), ["codex", "--no-daemon"]),
    (("codex", "--no-daemon"), ["codex", "--no-daemon"]),
    (("codex-wrapper", "--flag"), ["codex-wrapper", "--flag", "--no-daemon"]),
])
def test_codex_profiles_run_without_daemon(argv, expected):
    assert launch.build_argv(Profile("c", "codex", argv)) == expected
    assert launch.build_argv(Profile("c", "codex", argv, effort_args=("--no-daemon",))) == [
        *argv, "--no-daemon"
    ]


def test_non_codex_argv_is_unchanged():
    profile = Profile("p", "claude", ("claude", "--x"), model_args=("--model", "m"))
    assert launch.build_argv(profile) == ["claude", "--x", "--model", "m"]


def test_build_env_strips_tmux_only_inside_zellij(tmp_path):
    profile = Profile("p", "codex", ("codex",), env={"A": "1"}, config_dir=tmp_path)
    base = {"TMUX": "/s,1,0", "TMUX_PANE": "%1", "TONGS_AGENTS_PROFILE": "old", "KEEP": "k"}
    env = launch.build_env(profile, "a" * 32, base)
    assert env == {**base, "A": "1", "CODEX_HOME": str(tmp_path),
                   "TONGS_AGENTS_LAUNCH_ID": "a" * 32, "TONGS_AGENTS_PROFILE": "p"}
    env = launch.build_env(profile, "a" * 32, {**base, "ZELLIJ": "0"})
    assert "TMUX" not in env and "TMUX_PANE" not in env
    pi = Profile("p", "pi", ("pi",), config_dir=tmp_path)
    assert launch.build_env(pi, "a" * 32, {})["PI_CODING_AGENT_DIR"] == str(tmp_path)


@pytest.mark.parametrize("env, expected", [
    ({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents", "ZELLIJ_PANE_ID": "3",
      "TMUX_PANE": "%1"}, PaneRef("zellij", "agents", "3", None)),
    ({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents", "ZELLIJ_PANE_ID": "3",
      "ZELLIJ_SOCKET_DIR": "relative"}, PaneRef("zellij", "agents", "3", None)),
    ({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents", "ZELLIJ_PANE_ID": "x3"}, None),
    ({"ZELLIJ": "0", "ZELLIJ_PANE_ID": "3"}, None),
    ({"TMUX": "/tmp/tmux-1/default,99,0", "TMUX_PANE": "%7"},
     PaneRef("tmux", "", "%7", "/tmp/tmux-1/default")),
    ({"TMUX_PANE": "7"}, None),
    ({}, None),
])
def test_pane_from_env(env, expected):
    assert launch.pane_from_env(env) == expected


def test_exit_64_cases(config, no_exec, capsys):
    config('[profiles.p]\ncommand = ["x"]\n')
    record = pending("p", "other")
    assert launch.main(["p"]) == 64
    assert launch.main(["p", "--launch-id", "nothex"]) == 64
    assert launch.main(["missing", "--launch-id", record.launch_id]) == 64
    assert launch.main(["p", "--launch-id", "0" * 32]) == 64
    other = pending("claude-alt")
    assert launch.main(["p", "--launch-id", other.launch_id]) == 64
    assert launches.get(other.launch_id).state == "pending"
    config('[profiles.p]\ncommand = []\n')
    assert launch.main(["p", "--launch-id", record.launch_id]) == 64
    assert no_exec == []
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 6
    assert all(line.startswith("tongs-agents launch: ") for line in lines)
    assert "profiles.p.command" in lines[-1]


def test_harness_changed_since_pending_is_64(config, no_exec, capsys):
    config('[profiles.p]\ncommand = ["x"]\nharness = "codex"\n')
    record = pending("p", "claude")
    assert launch.main(["p", "--launch-id", record.launch_id]) == 64
    assert no_exec == []
    assert launches.get(record.launch_id).state == "pending"
    error = capsys.readouterr().err
    assert "'claude'" in error and "'codex'" in error


def test_missing_cwd_is_64(config, no_exec, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    config(f'[profiles.p]\ncommand = ["x"]\ncwd = {json.dumps(str(tmp_path / "gone"))}\n')
    record = pending("p", "other")
    assert launch.main(["p", "--launch-id", record.launch_id]) == 64
    assert no_exec == []


def test_exec_failure_is_127_and_waits_on_a_tty(config, monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    config(f'[profiles.p]\ncommand = [{json.dumps(str(tmp_path / "no-such-agent"))}]\n')
    record = pending("p", "other")

    class Tty:
        def isatty(self):
            return True

    prompts = []
    monkeypatch.setattr(launch.sys, "stdin", Tty())
    monkeypatch.setattr("builtins.input", lambda *args: prompts.append(args) or "")
    assert launch.main(["p", "--launch-id", record.launch_id]) == 127
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert "no-such-agent" in err and "press Enter" in err
    assert prompts == [()]
