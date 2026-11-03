"""Install and uninstall into explicit and profile-named config homes.

Synthetic: HOME is a temporary directory, the harness CLIs are recorded
subprocess calls and Pi is a stub; nothing touches a real config.
"""

import json
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from tongs_agents import cli, paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hook = bin_dir / "tongs-agents-hook"
    hook.write_text("#!/bin/sh\nexit 0\n")
    hook.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".pi" / "agent"))
    monkeypatch.setattr(cli, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(cli, "SETTINGS_FILE", home / ".claude" / "settings.json")
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", home / ".claude.json")
    monkeypatch.setattr(cli, "CODEX_DIR", home / ".codex")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", home / ".codex" / "hooks.json")
    tools = {
        "tongs-agents-hook": str(hook),
        "claude": "/fake/bin/claude",
        "codex": "/fake/bin/codex",
        "pi": "/fake/bin/pi",
    }
    monkeypatch.setattr(cli.shutil, "which", lambda name: tools.get(name))
    calls = []

    def fake_run(argv, **kwargs):
        env = kwargs.get("env") or {}
        calls.append(SimpleNamespace(
            argv=argv,
            claude_dir=env.get("CLAUDE_CONFIG_DIR"),
            codex_home=env.get("CODEX_HOME"),
        ))
        missing = argv[1:3] == ["mcp", "get"]
        return SimpleNamespace(returncode=1 if missing else 0, stdout="", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    pi_calls = []

    def fake_install_pi(pi, hook_path, env=None):
        pi_calls.append(("install", None if env is None else env["PI_CODING_AGENT_DIR"]))
        directory = cli.pi_config_dir(env)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / cli.PI_CONFIG_NAME).write_text("{}")
        return True

    def fake_uninstall_pi(pi, env=None):
        pi_calls.append(("uninstall", None if env is None else env["PI_CODING_AGENT_DIR"]))
        config = cli.pi_config_dir(env) / cli.PI_CONFIG_NAME
        if config.exists():
            config.unlink()
        return True

    monkeypatch.setattr(cli, "install_pi", fake_install_pi)
    monkeypatch.setattr(cli, "uninstall_pi", fake_uninstall_pi)
    return SimpleNamespace(
        root=tmp_path, home=home, hook=str(hook), calls=calls, pi_calls=pi_calls,
    )


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["tongs-agents", *argv])
    cli.main()


def _owned_groups(path, harness):
    config = json.loads(path.read_text())
    counts = {}
    for event, groups in config.get("hooks", {}).items():
        for group in groups:
            for hook in group["hooks"]:
                if cli._is_owned_hook(hook):
                    assert hook["command"].endswith(f"--agent {harness}")
                    counts[event] = counts.get(event, 0) + 1
    return counts


def _all_events(harness):
    return {event: 1 for event, _ in cli._hook_events(harness)}


def _write_profiles(text):
    directory = paths.config_dir(create=True)
    (directory / "config.toml").write_text(text)


def test_install_into_two_claude_dirs_leaves_home_untouched(home, monkeypatch):
    a, b = home.root / "a", home.root / "b"
    _run(monkeypatch, "install", "--claude-config-dir", str(a), "--claude-config-dir", str(b))

    for directory in (a, b):
        assert _owned_groups(directory / "settings.json", "claude") == _all_events("claude")
    assert not (home.home / ".claude").exists()
    assert not (home.home / ".claude.json").exists()
    assert not (home.home / ".codex").exists()
    assert [(call.argv[1:4], call.claude_dir) for call in home.calls] == [
        (["mcp", "add", "--scope"], str(a)),
        (["mcp", "add", "--scope"], str(b)),
    ]
    record = cli.installs_path()
    assert json.loads(record.read_text())["dirs"]["claude"] == sorted([str(a), str(b)])
    if os.name != "nt":
        assert stat.S_IMODE(record.stat().st_mode) == 0o600


def test_rerun_is_byte_identical(home, monkeypatch):
    a, b = home.root / "a", home.root / "b"
    argv = ("install", "--claude-config-dir", str(a), "--claude-config-dir", str(b),
            "--codex-home", str(home.root / "c"))
    _run(monkeypatch, *argv)
    files = [a / "settings.json", b / "settings.json", home.root / "c" / "hooks.json",
             cli.installs_path()]
    first = [path.read_bytes() for path in files]
    _run(monkeypatch, *argv)
    assert [path.read_bytes() for path in files] == first


def test_uninstall_per_dir_is_symmetric(home, monkeypatch, capsys):
    a, b = home.root / "a", home.root / "b"
    b.mkdir()
    (b / "settings.json").write_text(json.dumps({"model": "opus"}))
    _run(monkeypatch, "install", "--claude-config-dir", str(a), "--claude-config-dir", str(b))
    paths.sessions_dir().joinpath("fleet-v1-x.json").write_text("{}")
    (a / ".claude.json").write_text(json.dumps({"mcpServers": {"tongs-agents": {
        "command": cli.sys.executable, "args": ["-m", cli.MCP_MODULE]}}}))
    home.calls.clear()

    _run(monkeypatch, "uninstall", "--claude-config-dir", str(a))
    assert json.loads((a / "settings.json").read_text()) == {}
    assert _owned_groups(b / "settings.json", "claude") == _all_events("claude")
    assert json.loads(cli.installs_path().read_text())["dirs"]["claude"] == [str(b)]
    assert paths.sessions_dir().is_dir()
    assert f"{b / 'settings.json'} still has tongs-agents hooks" in capsys.readouterr().out
    assert [(call.argv[1:], call.claude_dir) for call in home.calls] == [
        (["mcp", "remove", "--scope", "user", "tongs-agents"], str(a)),
    ]

    _run(monkeypatch, "uninstall", "--claude-config-dir", str(b))
    assert json.loads((b / "settings.json").read_text()) == {"model": "opus"}
    assert not cli.installs_path().exists()
    assert not paths.sessions_dir().exists()


def test_uninstall_keeps_sessions_while_a_recorded_dir_has_hooks(home, monkeypatch, capsys):
    a = home.root / "a"
    _run(monkeypatch, "install", "--claude-config-dir", str(a))
    _run(monkeypatch, "uninstall", "--agent", "codex")
    _run(monkeypatch, "uninstall", "--claude-config-dir", str(home.root / "unrelated"))
    assert paths.sessions_dir().is_dir()
    assert not (home.root / "unrelated").exists()
    assert "still has tongs-agents hooks" in capsys.readouterr().out


def test_codex_mcp_table_lands_in_codex_home(home, monkeypatch):
    c = home.root / "c"
    _run(monkeypatch, "install", "--codex-home", str(c))
    assert _owned_groups(c / "hooks.json", "codex") == _all_events("codex")
    assert "tongs-agents" in (c / "config.toml").read_text()
    assert not (home.home / ".codex").exists()
    assert not [call for call in home.calls if call.argv[0].endswith("codex")]

    _run(monkeypatch, "uninstall", "--codex-home", str(c))
    assert "tongs-agents" not in (c / "config.toml").read_text()
    assert not [call for call in home.calls if call.argv[0].endswith("codex")]


def test_default_install_never_follows_env_homes(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home.root / "env-claude"))
    monkeypatch.setenv("CODEX_HOME", str(home.root / "env-codex"))
    _run(monkeypatch, "install")
    assert cli.SETTINGS_FILE.exists() and cli.CODEX_HOOKS_FILE.exists()
    assert not (home.root / "env-claude").exists()
    assert not (home.root / "env-codex").exists()
    for call in home.calls:
        if call.argv[0].endswith("claude"):
            assert call.claude_dir is None
        else:
            assert call.codex_home is None
    assert not cli.installs_path().exists()


def test_pi_agent_dir_passes_env(home, monkeypatch):
    p = home.root / "pi-alt"
    _run(monkeypatch, "install", "--pi-agent-dir", str(p))
    assert home.pi_calls == [("install", str(p))]
    assert json.loads(cli.installs_path().read_text())["dirs"]["pi"] == [str(p)]
    assert not cli.SETTINGS_FILE.exists()
    _run(monkeypatch, "uninstall", "--pi-agent-dir", str(p))
    assert home.pi_calls[-1] == ("uninstall", str(p))
    assert not cli.installs_path().exists()


def test_agent_and_dir_options_conflict(home, monkeypatch, capsys):
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, "install", "--agent", "codex", "--claude-config-dir", "/tmp/x")
    assert raised.value.code == 1
    assert "--agent cannot be combined" in capsys.readouterr().err
    assert not cli.SETTINGS_FILE.exists()


def test_malformed_dir_config_writes_nothing(home, monkeypatch, capsys):
    a, b = home.root / "a", home.root / "b"
    b.mkdir()
    (b / "settings.json").write_text("{")
    with pytest.raises(SystemExit):
        _run(monkeypatch, "install", "--claude-config-dir", str(a), "--claude-config-dir", str(b))
    assert not a.exists()
    assert (b / "settings.json").read_text() == "{"
    assert not paths.state_dir().exists()
    assert str(b / "settings.json") in capsys.readouterr().err


PROFILES = """
[profiles.claude-alt]
command = ["claude"]
config_dir = "{a}"

[profiles.claude-alt-opus]
command = ["claude"]
config_dir = "{a}"
model_args = ["--model", "opus"]

[profiles.claude-env]
command = ["claude"]
env = {{ CLAUDE_CONFIG_DIR = "{b}" }}

[profiles.claude-sandbox]
command = ["/opt/example/bin/claude-sandbox"]
harness = "claude"

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[profiles.codex-alt]
command = ["codex"]
config_dir = "{c}"

[profiles.pi-local]
command = ["pi", "--provider", "ollama"]

[profiles.example-agent]
command = ["example-agent"]
"""


@pytest.fixture
def profiled(home):
    dirs = SimpleNamespace(a=home.root / "a", b=home.root / "b", c=home.root / "c")
    _write_profiles(PROFILES.format(a=dirs.a, b=dirs.b, c=dirs.c))
    return dirs


def test_install_is_profile_driven(home, profiled, monkeypatch, capsys):
    _run(monkeypatch, "install")

    for path in (profiled.a / "settings.json", profiled.b / "settings.json", cli.SETTINGS_FILE):
        assert _owned_groups(path, "claude") == _all_events("claude")
    for path in (profiled.c / "hooks.json", cli.CODEX_HOOKS_FILE):
        assert _owned_groups(path, "codex") == _all_events("codex")
    assert home.pi_calls == [("install", None)]
    output = capsys.readouterr().out
    assert output.count(f"Updated {profiled.a / 'settings.json'}") == 1
    assert "profile claude-alt, profile claude-alt-opus" in output
    claude_dirs = [call.claude_dir for call in home.calls if call.argv[0].endswith("claude")]
    assert claude_dirs == [None, str(profiled.a), str(profiled.b)]
    record = json.loads(cli.installs_path().read_text())["dirs"]
    assert record == {
        "claude": sorted([str(profiled.a), str(profiled.b)]),
        "codex": [str(profiled.c)],
        "pi": [],
    }


def _without_pi(home, monkeypatch):
    real_which = cli.shutil.which
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "pi" else real_which(name))


def test_missing_pi_for_profile_only_is_a_named_warning(home, profiled, monkeypatch, capsys):
    _without_pi(home, monkeypatch)
    _run(monkeypatch, "install")
    output = capsys.readouterr().out
    assert "Warning: skipped Pi monitoring (for profile pi-local)" in output
    assert "Tongs Agents installed" in output
    assert home.pi_calls == []
    assert _owned_groups(cli.SETTINGS_FILE, "claude") == _all_events("claude")


@pytest.mark.parametrize("argv", [["--agent", "pi"], ["--agent", "all"]])
def test_missing_pi_requested_explicitly_still_fails(home, profiled, monkeypatch, capsys, argv):
    _without_pi(home, monkeypatch)
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, "install", *argv)
    assert raised.value.code == 1
    output = capsys.readouterr().out
    assert "Warning: skipped" not in output
    assert "Pi and tongs-agents-hook must be installed before enabling Pi" in output


def test_install_help_documents_profile_pi_warning(monkeypatch, capsys):
    with pytest.raises(SystemExit):
        _run(monkeypatch, "install", "--help")
    help_text = " ".join(capsys.readouterr().out.split())
    assert "skipped with a warning" in help_text


def test_agent_selection_limits_profile_homes(home, profiled, monkeypatch):
    _run(monkeypatch, "install", "--agent", "codex")
    assert (profiled.c / "hooks.json").exists()
    assert not profiled.a.exists()
    assert home.pi_calls == []


def test_dir_options_override_profiles(home, profiled, monkeypatch):
    other = home.root / "other"
    _run(monkeypatch, "install", "--claude-config-dir", str(other))
    assert (other / "settings.json").exists()
    assert not profiled.a.exists()
    assert not cli.CODEX_HOOKS_FILE.exists()


def test_uninstall_removes_profile_and_recorded_homes(home, profiled, monkeypatch):
    _run(monkeypatch, "install")
    _write_profiles("")
    _run(monkeypatch, "uninstall")
    for path in (profiled.a / "settings.json", profiled.b / "settings.json", cli.SETTINGS_FILE):
        assert _owned_groups(path, "claude") == {}
    assert _owned_groups(profiled.c / "hooks.json", "codex") == {}
    assert not cli.installs_path().exists()
    assert home.pi_calls[-1] == ("install", None)
    assert paths.sessions_dir().is_dir()


def test_invalid_profiles_stop_install(home, monkeypatch, capsys):
    _write_profiles("[profiles.Bad]\ncommand = ['claude']\n")
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, "install")
    assert raised.value.code == 1
    assert "profiles.Bad" in capsys.readouterr().err
    assert not cli.SETTINGS_FILE.exists()


def test_invalid_profiles_do_not_block_uninstall(home, monkeypatch, capsys):
    _run(monkeypatch, "install")
    _write_profiles("[profiles.Bad]\ncommand = ['claude']\n")
    _run(monkeypatch, "uninstall", "--keep-data")
    assert _owned_groups(cli.SETTINGS_FILE, "claude") == {}
    assert "Skipped profiles" in capsys.readouterr().out


def test_relative_profile_env_home_is_skipped(home, monkeypatch):
    _write_profiles(
        "[profiles.rel]\ncommand = ['claude']\nenv = { CLAUDE_CONFIG_DIR = 'rel' }\n"
    )
    _run(monkeypatch, "install")
    assert not (home.root / "rel").exists()
    assert not cli.installs_path().exists()


def test_malformed_install_record_is_refused(home, monkeypatch, capsys):
    paths.state_dir(create=True)
    cli.installs_path().write_text('{"schema_version": 1, "dirs": {"claude": ["rel"]}}')
    with pytest.raises(SystemExit):
        _run(monkeypatch, "install")
    assert "unrecognized install record" in capsys.readouterr().err
    assert not cli.SETTINGS_FILE.exists()


def test_profile_home_resolution(tmp_path):
    from tongs_agents.profiles import parse_profile

    explicit = parse_profile("a", {"command": ["claude"], "config_dir": str(tmp_path)})
    assert cli.profile_home(explicit) == ("claude", tmp_path, None)
    env = parse_profile("b", {"command": ["codex"], "env": {"CODEX_HOME": str(tmp_path)}})
    assert cli.profile_home(env) == ("codex", tmp_path, None)
    default = parse_profile("c", {"command": ["pi"]})
    assert cli.profile_home(default) == ("pi", None, None)
    other = parse_profile("d", {"command": ["x"]})
    assert cli.profile_home(other)[0] is None
