import json
import os
import stat
from types import SimpleNamespace

import pytest

from tongs_agents import cli, legacy


LEGACY_HOOK = legacy.LEGACY_HOOK_COMMANDS[0]
LEGACY_MODULE = legacy.LEGACY_MCP_MODULES[0]


def test_install_creates_hooks(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    from tongs_agents.cli import cmd_install
    cmd_install(None)
    settings = json.loads(settings_file.read_text())
    assert "hooks" in settings
    assert "SessionStart" in settings["hooks"]
    assert "Stop" in settings["hooks"]
    assert "PermissionRequest" in settings["hooks"]
    assert "Elicitation" in settings["hooks"]


def test_install_creates_codex_hooks(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    from tongs_agents import cli
    cli.cmd_install(None)
    hooks = json.loads(cli.CODEX_HOOKS_FILE.read_text())["hooks"]
    assert set(hooks) == {
        "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop",
        "SessionEnd", "PermissionRequest",
    }
    assert "--agent codex" in hooks["SessionStart"][0]["hooks"][0]["command"]
    assert hooks["SessionStart"][0]["matcher"] == "startup|resume|clear"


def test_install_creates_mcp_server(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    from tongs_agents.cli import cmd_install
    cmd_install(None)
    settings = json.loads(settings_file.read_text())
    assert "mcpServers" in settings
    assert "fleet" in settings["mcpServers"]
    assert settings["mcpServers"]["fleet"]["args"] == ["-m", "tongs_agents.mcp_server"]


def test_install_codex_mcp_uses_codex_cli(monkeypatch):
    from tongs_agents import cli
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: None)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [[
        "/usr/bin/codex", "mcp", "add", "fleet", "--", cli.sys.executable,
        "-m", "tongs_agents.mcp_server",
    ]]


def test_install_codex_mcp_preserves_name_conflict(monkeypatch):
    from tongs_agents import cli
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: {
        "transport": {"command": "other", "args": []}
    })
    def fail_run(*args, **kwargs):
        raise AssertionError("Codex MCP config should not be replaced")

    monkeypatch.setattr(cli.subprocess, "run", fail_run)
    cli._install_codex_mcp("/usr/bin/codex")


def test_install_idempotent(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    from tongs_agents.cli import cmd_install
    cmd_install(None)
    cmd_install(None)
    settings = json.loads(settings_file.read_text())
    for event_hooks in settings["hooks"].values():
        fleet_hooks = [h for h in event_hooks if "tongs-agents-hook" in json.dumps(h)]
        assert len(fleet_hooks) == 1
    from tongs_agents import cli
    codex_hooks = json.loads(cli.CODEX_HOOKS_FILE.read_text())["hooks"]
    for event_hooks in codex_hooks.values():
        fleet_hooks = [h for h in event_hooks if "tongs-agents-hook" in json.dumps(h)]
        assert len(fleet_hooks) == 1


def test_install_preserves_existing_hooks(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    settings_file.write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}
    }))
    from tongs_agents.cli import cmd_install
    cmd_install(None)
    settings = json.loads(settings_file.read_text())
    commands = [h["hooks"][0]["command"] for h in settings["hooks"]["PreToolUse"]]
    assert "other-hook" in commands
    assert any("tongs-agents-hook" in c for c in commands)


def test_uninstall_removes_hooks(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    from tongs_agents.cli import cmd_install, cmd_uninstall
    cmd_install(None)

    class Args:
        keep_data = True
    cmd_uninstall(Args())
    settings = json.loads(settings_file.read_text())
    assert "hooks" not in settings or not settings["hooks"]
    assert "fleet" not in settings.get("mcpServers", {})
    from tongs_agents import cli
    codex_hooks = json.loads(cli.CODEX_HOOKS_FILE.read_text())
    assert "hooks" not in codex_hooks or not codex_hooks["hooks"]


def test_uninstall_preserves_other_hooks(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    settings_file.write_text(json.dumps({
        "hooks": {"PreToolUse": [
            {"hooks": [{"type": "command", "command": "other-hook"}]},
            {"hooks": [{"type": "command", "command": "tongs-agents-hook tool-use"}]},
        ]}
    }))
    from tongs_agents.cli import cmd_uninstall

    class Args:
        keep_data = True
    cmd_uninstall(Args())
    settings = json.loads(settings_file.read_text())
    assert len(settings["hooks"]["PreToolUse"]) == 1
    assert "other-hook" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


@pytest.mark.parametrize("command", [
    "tongs-agents-hook stop --agent claude",
    "/usr/local/bin/tongs-agents-hook stop --agent claude",
    "'/home/u/my venv/bin/tongs-agents-hook' stop --agent codex",
    f"{LEGACY_HOOK} stop",
    f"/opt/venv/bin/{LEGACY_HOOK} tool-use --agent claude",
])
def test_owned_hook_commands(command):
    assert cli._is_owned_hook({"type": "command", "command": command})


@pytest.mark.parametrize("command", [
    "notify-send tongs-agents-hook",
    "echo 'tongs-agents-hook stop'",
    "/usr/bin/tongs-agents-hook-wrapper stop",
    "FOO=1 tongs-agents-hook stop",
    "unbalanced 'quote",
    "",
])
def test_foreign_hook_commands(command):
    assert not cli._is_owned_hook({"type": "command", "command": command})


def test_non_command_hook_entries_are_foreign():
    assert not cli._is_owned_hook({"type": "command"})
    assert not cli._is_owned_hook({"type": "command", "command": ["tongs-agents-hook"]})
    assert not cli._is_owned_hook("tongs-agents-hook")


def test_legacy_hook_script_is_owned_by_resolved_path(tmp_path, monkeypatch):
    script = tmp_path / ".claude" / "bin" / "fleet-hook.sh"
    monkeypatch.setattr(legacy, "LEGACY_HOOK_SCRIPTS", (script.resolve(),))
    assert cli._is_owned_hook({"command": f"{script} stop"})
    assert cli._is_owned_hook({"command": f"{tmp_path}/.claude/bin/../bin/fleet-hook.sh stop"})
    assert not cli._is_owned_hook({"command": "fleet-hook.sh stop"})
    assert not cli._is_owned_hook({"command": f"{tmp_path}/other/fleet-hook.sh stop"})


def test_windows_hook_command_is_owned(monkeypatch):
    monkeypatch.setattr(cli.os, "name", "nt")
    command = '"C:\\Program Files\\Py\\Scripts\\tongs-agents-hook.EXE" stop --agent claude'
    assert cli._is_owned_hook({"command": command})
    assert not cli._is_owned_hook({"command": "notify.exe tongs-agents-hook"})


def test_install_keeps_foreign_commands_in_mixed_groups(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    settings_file.write_text(json.dumps({"hooks": {"Stop": [{
        "matcher": "*",
        "hooks": [
            {"type": "command", "command": "other-hook"},
            {"type": "command", "command": "/usr/bin/tongs-agents-hook stop --agent claude"},
        ],
    }]}}))
    cli.cmd_install(None)
    groups = json.loads(settings_file.read_text())["hooks"]["Stop"]
    assert groups[0] == {"matcher": "*", "hooks": [{"type": "command", "command": "other-hook"}]}
    assert len(groups) == 2


def test_uninstall_keeps_user_command_mentioning_hook(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    mention = {"hooks": [{"type": "command", "command": "logger tongs-agents-hook ran"}]}
    settings_file.write_text(json.dumps({"hooks": {"Stop": [mention], "Custom": []}}))
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert json.loads(settings_file.read_text())["hooks"] == {"Stop": [mention], "Custom": []}


@pytest.mark.parametrize("config", [
    {"command": "/usr/bin/python3", "args": ["-m", "tongs_agents.mcp_server"]},
    {"command": "/opt/old/bin/python3.11", "args": ["-m", LEGACY_MODULE]},
    {"command": "C:\\Python312\\python.exe", "args": ["-m", "tongs_agents.mcp_server"]},
    {"transport": {"command": "python", "args": ["-m", LEGACY_MODULE]}},
])
def test_owned_mcp_entries(config):
    assert cli._is_owned_mcp(config)


@pytest.mark.parametrize("config", [
    {"command": "node", "args": ["-m", "tongs_agents.mcp_server"]},
    {"command": "/usr/bin/python3", "args": ["-m", "someone.mcp_server"]},
    {"command": "/usr/bin/python3", "args": ["-m", "tongs_agents.mcp_server", "--debug"]},
    {"command": "uv", "args": ["run", "server.py"]},
    {"transport": "stdio"},
    {},
    None,
])
def test_foreign_mcp_entries(config):
    assert not cli._is_owned_mcp(config)


def test_install_keeps_foreign_claude_mcp(settings_file, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    foreign = {"command": "node", "args": ["fleet.js"]}
    settings_file.write_text(json.dumps({"mcpServers": {"fleet": foreign}}))
    cli.cmd_install(None)
    assert json.loads(settings_file.read_text())["mcpServers"]["fleet"] == foreign
    assert "Kept existing Claude MCP server named fleet" in capsys.readouterr().out
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert json.loads(settings_file.read_text())["mcpServers"]["fleet"] == foreign


def _codex_runner(calls, returncode=0):
    def fake_run(command, **kwargs):
        calls.append(command[1:])
        return SimpleNamespace(returncode=returncode, stderr="failed")
    return fake_run


def test_install_codex_mcp_replaces_legacy_entry(monkeypatch):
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: {"transport": {
        "command": "/old/bin/python3", "args": ["-m", LEGACY_MODULE],
    }})
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [
        ["mcp", "remove", "fleet"],
        ["mcp", "add", "fleet", "--", cli.sys.executable, "-m", "tongs_agents.mcp_server"],
    ]


def test_install_codex_mcp_skips_current_entry(monkeypatch):
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: {"transport": {
        "command": cli.sys.executable, "args": ["-m", "tongs_agents.mcp_server"],
    }})
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == []


def test_install_codex_mcp_does_not_add_when_remove_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: {
        "command": "/old/bin/python", "args": ["-m", "tongs_agents.mcp_server"],
    })
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls, returncode=1))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [["mcp", "remove", "fleet"]]
    assert "Could not replace Codex MCP server" in capsys.readouterr().out


@pytest.mark.parametrize("existing, removed", [
    ({"transport": {"command": "/x/python3", "args": ["-m", LEGACY_MODULE]}}, True),
    ({"transport": {"command": "/x/python3", "args": ["-m", "tongs_agents.mcp_server"]}}, True),
    ({"transport": {"command": "node", "args": ["fleet.js"]}}, False),
    (None, False),
])
def test_uninstall_codex_mcp_removes_only_owned(monkeypatch, existing, removed):
    monkeypatch.setattr(cli, "_get_codex_mcp", lambda command: existing)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._uninstall_codex_mcp("/usr/bin/codex")
    assert calls == ([["mcp", "remove", "fleet"]] if removed else [])


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")


@posix_only
def test_save_json_creates_private_file(tmp_path):
    path = tmp_path / "new" / "settings.json"
    cli.save_json(path, {"a": 1})
    assert json.loads(path.read_text()) == {"a": 1}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in path.parent.iterdir()] == ["settings.json"]


@posix_only
def test_save_json_keeps_existing_mode(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    path.chmod(0o644)
    cli.save_json(path, {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert path.read_text() == '{\n  "a": 1\n}\n'


@posix_only
def test_symlinked_config_is_refused(tmp_path):
    target = tmp_path / "real.json"
    target.write_text("{}")
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    with pytest.raises(cli.ConfigError, match="symlink"):
        cli.save_json(link, {"a": 1})
    with pytest.raises(cli.ConfigError, match="symlink"):
        cli.load_json(link)
    assert target.read_text() == "{}"
    assert link.is_symlink()


@pytest.mark.parametrize("text", [
    "{",
    "[]",
    '{"hooks": []}',
    '{"hooks": {"Stop": {}}}',
    '{"hooks": null}',
    '{"mcpServers": []}',
])
def test_malformed_settings_exit_without_write(settings_file, monkeypatch, capsys, text):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    settings_file.write_text(text)
    monkeypatch.setattr(cli.sys, "argv", ["tongs-agents", "install"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert error.startswith(f"tongs-agents: {settings_file}: ")
    assert error.count("\n") == 1
    assert settings_file.read_text() == text
    assert not cli.CODEX_HOOKS_FILE.exists()


@pytest.fixture
def fleet_data(settings_file, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    fleet = cli.FLEET_DIR
    fleet.mkdir()
    for name in (
        "abc.json",
        "fleet-v1-0123.json",
        ".fleet-v1-0123.lock",
        ".fleet-identity-v1-0123.lock",
        ".fleet-tmp-x.tmp",
    ):
        (fleet / name).write_text("{}")
    return fleet


def test_uninstall_removes_fleet_records_and_empty_dir(fleet_data):
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert not fleet_data.exists()


def test_uninstall_keeps_unknown_fleet_files(fleet_data, capsys):
    (fleet_data / "notes.txt").write_text("mine")
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert [entry.name for entry in fleet_data.iterdir()] == ["notes.txt"]
    assert "holds other files" in capsys.readouterr().out


def test_uninstall_keep_data_keeps_fleet_dir(fleet_data):
    before = sorted(entry.name for entry in fleet_data.iterdir())
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert sorted(entry.name for entry in fleet_data.iterdir()) == before


def test_uninstall_keeps_fleet_dir_while_pi_is_installed(fleet_data, tmp_path, capsys):
    config = tmp_path / "pi" / cli.PI_CONFIG_NAME
    config.parent.mkdir()
    config.write_text("{}")
    before = sorted(entry.name for entry in fleet_data.iterdir())
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert sorted(entry.name for entry in fleet_data.iterdir()) == before
    assert "Pi monitoring is still installed" in capsys.readouterr().out


@posix_only
def test_uninstall_keeps_symlinked_fleet_dir(fleet_data, tmp_path, monkeypatch):
    link = tmp_path / "fleet-link"
    link.symlink_to(fleet_data)
    monkeypatch.setattr(cli, "FLEET_DIR", link)
    before = sorted(entry.name for entry in fleet_data.iterdir())
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert link.is_symlink()
    assert sorted(entry.name for entry in fleet_data.iterdir()) == before
