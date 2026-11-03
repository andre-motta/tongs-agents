import json
import os
import stat
from types import SimpleNamespace

import pytest

from tongs_agents import cli, legacy, owned, paths


LEGACY_HOOK = legacy.LEGACY_HOOK_COMMANDS[0]
LEGACY_MODULE = legacy.LEGACY_MCP_MODULES[0]
OLD_KEY = legacy.LEGACY_MCP_NAME


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


def test_install_registers_claude_mcp_through_claude_cli(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    registered = []
    monkeypatch.setattr(cli, "_install_claude_mcp", lambda *args: registered.append(args))
    cli.cmd_install(None)
    settings = json.loads(settings_file.read_text())
    assert "mcpServers" not in settings
    assert registered == [("/usr/bin/claude", None)]


def test_install_without_claude_cli_skips_mcp(settings_file, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda x: None if x == "claude" else f"/usr/bin/{x}")
    registered = []
    monkeypatch.setattr(cli, "_install_claude_mcp", lambda *args: registered.append(args))
    cli.cmd_install(None)
    assert registered == []
    assert "Claude Code CLI not found" in capsys.readouterr().out


def _claude_servers(directory, servers):
    config = directory / ".claude.json"
    config.write_text(json.dumps({"numStartups": 3, "mcpServers": servers}))
    return config


def _recorder(calls, returncode=0):
    def fake_run(command, **kwargs):
        calls.append((command[1:], kwargs.get("env")))
        return SimpleNamespace(returncode=returncode, stderr="failed")
    return fake_run


CURRENT = {"type": "stdio", "command": cli.sys.executable,
           "args": ["-m", "tongs_agents.mcp_server"], "env": {}}
ADD_CLAUDE = ["mcp", "add", "--scope", "user", "tongs-agents", "--",
              cli.sys.executable, "-m", "tongs_agents.mcp_server"]


@pytest.mark.parametrize("servers, expected", [
    ({}, [ADD_CLAUDE]),
    ({"tongs-agents": CURRENT}, []),
    ({"tongs-agents": {**CURRENT, "command": "/old/python3"}},
     [["mcp", "remove", "--scope", "user", "tongs-agents"], ADD_CLAUDE]),
    ({OLD_KEY: {"command": "/old/python3", "args": ["-m", LEGACY_MODULE]},
      "tongs-agents": CURRENT},
     [["mcp", "remove", "--scope", "user", OLD_KEY]]),
    ({OLD_KEY: {"command": "node", "args": ["fleet.js"]}}, [ADD_CLAUDE]),
    ({"tongs-agents": {"command": "node", "args": ["mine.js"]}}, []),
])
def test_install_claude_mcp(tmp_path, monkeypatch, servers, expected):
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(tmp_path, servers))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _recorder(calls))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == expected
    assert all("CLAUDE_CONFIG_DIR" not in env for _, env in calls)


def test_install_claude_mcp_does_not_add_when_replace_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(
        tmp_path, {"tongs-agents": {**CURRENT, "command": "/old/python3"}}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _recorder(calls, returncode=1))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [["mcp", "remove", "--scope", "user", "tongs-agents"]]
    assert "Could not replace Claude Code MCP server" in capsys.readouterr().out


def _claude_script(calls, results):
    def fake_run(command, **kwargs):
        calls.append((command[1:], kwargs.get("env")))
        return SimpleNamespace(returncode=results.pop(0), stderr="failed")
    return fake_run


OUTDATED_CLAUDE = {**CURRENT, "command": "/old/python3", "env": {"EXAMPLE": "1"}}


@pytest.mark.parametrize("restore_code, message", [
    (0, "Restored the previous Claude Code MCP server tongs-agents; "
        "run 'tongs-agents install --agent claude' to retry the update"),
    (1, "was removed and could not be restored; "
        "run 'tongs-agents install --agent claude' to register it again"),
])
def test_install_claude_mcp_restores_entry_when_add_fails(tmp_path, monkeypatch, capsys,
                                                          restore_code, message):
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(
        tmp_path, {"tongs-agents": OUTDATED_CLAUDE}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _claude_script(calls, [0, 1, restore_code]))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [
        ["mcp", "remove", "--scope", "user", "tongs-agents"],
        ADD_CLAUDE,
        ["mcp", "add-json", "--scope", "user", "tongs-agents", json.dumps(OUTDATED_CLAUDE)],
    ]
    assert message in capsys.readouterr().out


def test_install_claude_mcp_restore_uses_the_config_dir(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "claude-alt"
    config_dir.mkdir()
    _claude_servers(config_dir, {"tongs-agents": OUTDATED_CLAUDE})
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _claude_script(calls, [0, 1, 1]))
    cli._install_claude_mcp("/usr/bin/claude", config_dir)
    assert [env["CLAUDE_CONFIG_DIR"] for _, env in calls] == [str(config_dir)] * 3
    rerun = cli._command_string(["tongs-agents", "install", "--claude-config-dir", str(config_dir)])
    assert f"run '{rerun}' to register it again" in capsys.readouterr().out


def test_install_claude_mcp_fresh_add_failure_does_not_restore(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(tmp_path, {}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _claude_script(calls, [1]))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [ADD_CLAUDE]
    assert "restore" not in capsys.readouterr().out


def test_unreadable_claude_config_still_registers(tmp_path, monkeypatch):
    config = tmp_path / ".claude.json"
    config.write_text("{not json")
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", config)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _recorder(calls))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [ADD_CLAUDE]
    assert config.read_text() == "{not json"


def test_uninstall_claude_mcp_removes_only_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(tmp_path, {
        "tongs-agents": CURRENT,
        OLD_KEY: {"command": "/old/python3", "args": ["-m", LEGACY_MODULE]},
        "docs": {"command": "/usr/bin/python3", "args": ["-m", "tongs_agents.mcp_server"]},
    }))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _recorder(calls))
    cli._uninstall_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [
        ["mcp", "remove", "--scope", "user", "tongs-agents"],
        ["mcp", "remove", "--scope", "user", OLD_KEY],
    ]


def _codex_lookup(servers):
    return lambda command, name, directory=None: servers.get(name)


def test_install_codex_mcp_uses_codex_cli(monkeypatch):
    from tongs_agents import cli
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({}))
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [[
        "/usr/bin/codex", "mcp", "add", "tongs-agents", "--", cli.sys.executable,
        "-m", "tongs_agents.mcp_server",
    ]]


def test_install_codex_mcp_preserves_name_conflict(monkeypatch):
    from tongs_agents import cli
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({
        "tongs-agents": {"transport": {"command": "other", "args": []}},
        OLD_KEY: {"transport": {"command": "node", "args": ["fleet.js"]}},
    }))
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
    assert "mcpServers" not in settings
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


@pytest.mark.parametrize("suffix", [" stop", " session-start --flag", ""])
def test_unquoted_legacy_script_under_a_home_with_spaces_is_owned(tmp_path, monkeypatch, suffix):
    script = tmp_path / "my home" / ".claude" / "bin" / "fleet-hook.sh"
    monkeypatch.setattr(legacy, "LEGACY_HOOK_SCRIPTS", (script.resolve(),))
    hook = {"command": f"{script}{suffix}"}
    assert cli._is_owned_hook(hook)
    assert cli.hook_report({"hooks": {"Stop": [{"hooks": [hook]}]}}, "claude")["legacy"] == 1


def test_legacy_script_prefix_needs_a_word_boundary(tmp_path, monkeypatch):
    script = tmp_path / "my home" / ".claude" / "bin" / "fleet-hook.sh"
    monkeypatch.setattr(legacy, "LEGACY_HOOK_SCRIPTS", (script.resolve(),))
    assert not cli._is_owned_hook({"command": f"{script}x stop"})
    assert not cli._is_owned_hook({"command": f"/usr/bin/notify {script} stop"})


def test_legacy_script_with_unbalanced_quote_in_home_is_owned(tmp_path, monkeypatch):
    script = tmp_path / "o'brien" / ".claude" / "bin" / "fleet-hook.sh"
    monkeypatch.setattr(legacy, "LEGACY_HOOK_SCRIPTS", (script.resolve(),))
    hook = {"command": f"{script} stop"}
    assert cli._command_argv(hook["command"]) == []
    assert cli._is_owned_hook(hook)
    assert cli.hook_report({"hooks": {"Stop": [{"hooks": [hook]}]}}, "claude")["legacy"] == 1


def test_windows_hook_command_is_owned(monkeypatch):
    monkeypatch.setattr(cli, "_is_windows", lambda: True)
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


def _uv_legacy(directory):
    return {"command": "/usr/bin/uv",
            "args": ["run", "--directory", str(directory), legacy.LEGACY_MCP_SCRIPT]}


def test_bash_era_uv_mcp_is_owned(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path / ".claude")
    mcp_dir = tmp_path / ".claude" / legacy.LEGACY_MCP_DIR_NAME
    assert cli._is_owned_mcp(_uv_legacy(mcp_dir))
    assert cli._is_owned_mcp({"transport": _uv_legacy(mcp_dir)})
    assert cli._is_owned_mcp({"command": "uv", "args": [
        "run", "--directory", f"~/.claude/{legacy.LEGACY_MCP_DIR_NAME}", "server.py"]})
    assert not cli._is_owned_mcp(_uv_legacy(tmp_path / "elsewhere"))
    assert not cli._is_owned_mcp({**_uv_legacy(mcp_dir), "command": "node"})
    assert not cli._is_owned_mcp({"command": "uv", "args": [
        "run", "--directory", str(mcp_dir), "other.py"]})


def test_install_claude_mcp_replaces_bash_era_fleet_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path / ".claude")
    mcp_dir = tmp_path / ".claude" / legacy.LEGACY_MCP_DIR_NAME
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", _claude_servers(
        tmp_path, {OLD_KEY: _uv_legacy(mcp_dir)}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _recorder(calls))
    cli._install_claude_mcp("/usr/bin/claude")
    assert [argv for argv, _ in calls] == [
        ["mcp", "remove", "--scope", "user", OLD_KEY], ADD_CLAUDE,
    ]


@pytest.fixture
def bash_era_mcp(settings_file, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: None)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi"))
    mcp_dir = tmp_path / legacy.LEGACY_MCP_DIR_NAME
    mcp_dir.mkdir()
    (mcp_dir / legacy.LEGACY_MCP_SCRIPT).write_text("")
    settings_file.write_text(json.dumps({"mcpServers": {OLD_KEY: _uv_legacy(mcp_dir)}}))
    return mcp_dir


def test_uninstall_removes_bash_era_mcp_entry_and_dir(bash_era_mcp, settings_file, capsys):
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert "mcpServers" not in json.loads(settings_file.read_text())
    assert not bash_era_mcp.exists()
    assert f"Removed legacy {bash_era_mcp}" in capsys.readouterr().out


def test_uninstall_keeps_mcp_dir_while_claude_still_registers_it(bash_era_mcp, tmp_path, capsys):
    _claude_servers(tmp_path, {OLD_KEY: _uv_legacy(bash_era_mcp)})
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert (bash_era_mcp / legacy.LEGACY_MCP_SCRIPT).exists()
    output = capsys.readouterr().out
    assert f"Kept legacy {bash_era_mcp}" in output
    assert f"claude mcp remove --scope user {OLD_KEY}" in output


def test_uninstall_removes_mcp_dir_after_claude_unregisters_it(bash_era_mcp, tmp_path,
                                                               monkeypatch):
    config = _claude_servers(tmp_path, {OLD_KEY: _uv_legacy(bash_era_mcp)})
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}" if x == "claude" else None)

    def unregister(command, directory=None):
        assert bash_era_mcp.exists()
        _claude_servers(tmp_path, {})

    monkeypatch.setattr(cli, "_uninstall_claude_mcp", unregister)
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert json.loads(config.read_text())["mcpServers"] == {}
    assert not bash_era_mcp.exists()


def test_install_drops_owned_settings_mcp_and_keeps_foreign(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    foreign = {"command": "node", "args": ["fleet.js"]}
    settings_file.write_text(json.dumps({"mcpServers": {
        OLD_KEY: foreign,
        "tongs-agents": {"command": "/old/python3", "args": ["-m", "tongs_agents.mcp_server"]},
    }}))
    cli.cmd_install(None)
    assert json.loads(settings_file.read_text())["mcpServers"] == {OLD_KEY: foreign}
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert json.loads(settings_file.read_text())["mcpServers"] == {OLD_KEY: foreign}


def _codex_runner(calls, returncode=0):
    def fake_run(command, **kwargs):
        calls.append(command[1:])
        return SimpleNamespace(returncode=returncode, stderr="failed")
    return fake_run


def test_install_codex_mcp_removes_legacy_entry(monkeypatch):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({OLD_KEY: {"transport": {
        "command": "/old/bin/python3", "args": ["-m", LEGACY_MODULE],
    }}}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [
        ["mcp", "remove", OLD_KEY],
        ["mcp", "add", "tongs-agents", "--", cli.sys.executable, "-m", "tongs_agents.mcp_server"],
    ]


def test_install_codex_mcp_skips_current_entry(monkeypatch):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({"tongs-agents": {"transport": {
        "command": cli.sys.executable, "args": ["-m", "tongs_agents.mcp_server"],
    }}}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == []


def _codex_script(calls, results):
    def fake_run(command, **kwargs):
        calls.append(command[1:])
        return SimpleNamespace(returncode=results.pop(0), stderr="failed")
    return fake_run


OUTDATED_CODEX = {"transport": {
    "type": "stdio", "command": "/old/bin/python3", "args": ["-m", "tongs_agents.mcp_server"],
    "env": {"EXAMPLE": "1"},
}}


@pytest.mark.parametrize("restore_code, message", [
    (0, "Restored the previous Codex MCP server tongs-agents; "
        "run 'tongs-agents install --agent codex' to retry the update"),
    (1, "was removed and could not be restored; "
        "run 'tongs-agents install --agent codex' to register it again"),
])
def test_install_codex_mcp_restores_entry_when_add_fails(monkeypatch, capsys, restore_code,
                                                         message):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({"tongs-agents": OUTDATED_CODEX}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_script(calls, [0, 1, restore_code]))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [
        ["mcp", "remove", "tongs-agents"],
        ["mcp", "add", "tongs-agents", "--", cli.sys.executable, "-m", "tongs_agents.mcp_server"],
        ["mcp", "add", "tongs-agents", "--env", "EXAMPLE=1", "--",
         "/old/bin/python3", "-m", "tongs_agents.mcp_server"],
    ]
    assert message in capsys.readouterr().out


def test_install_codex_mcp_restore_names_the_config_home(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({"tongs-agents": OUTDATED_CODEX}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_script(calls, [0, 1, 1]))
    cli._install_codex_mcp("/usr/bin/codex", tmp_path / "codex-alt")
    rerun = cli._command_string(
        ["tongs-agents", "install", "--codex-home", str(tmp_path / "codex-alt")]
    )
    assert f"run '{rerun}' to register it again" in capsys.readouterr().out


def test_install_codex_mcp_fresh_add_failure_does_not_restore(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_script(calls, [1]))
    cli._install_codex_mcp("/usr/bin/codex")
    assert len(calls) == 1
    assert "restore" not in capsys.readouterr().out


def test_install_codex_mcp_does_not_add_when_remove_fails(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({"tongs-agents": {
        "command": "/old/bin/python", "args": ["-m", "tongs_agents.mcp_server"],
    }}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls, returncode=1))
    cli._install_codex_mcp("/usr/bin/codex")
    assert calls == [["mcp", "remove", "tongs-agents"]]
    assert "Could not replace Codex MCP server" in capsys.readouterr().out


@pytest.mark.parametrize("existing, removed", [
    ({"transport": {"command": "/x/python3", "args": ["-m", LEGACY_MODULE]}}, True),
    ({"transport": {"command": "/x/python3", "args": ["-m", "tongs_agents.mcp_server"]}}, True),
    ({"transport": {"command": "node", "args": ["fleet.js"]}}, False),
    (None, False),
])
@pytest.mark.parametrize("name", ["tongs-agents", OLD_KEY])
def test_uninstall_codex_mcp_removes_only_owned(monkeypatch, existing, removed, name):
    monkeypatch.setattr(cli, "_get_codex_mcp", _codex_lookup({name: existing}))
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", _codex_runner(calls))
    cli._uninstall_codex_mcp("/usr/bin/codex")
    assert calls == ([["mcp", "remove", name]] if removed else [])


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


@posix_only
def test_install_backs_up_existing_settings_once(settings_file, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    settings_file.write_text('{"env": {"A": "1"}}')
    settings_file.chmod(0o644)
    original = settings_file.read_bytes()
    cli.cmd_install(None)
    backup = owned.backup_path(settings_file)
    assert backup.read_bytes() == original
    assert stat.S_IMODE(settings_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(cli.CODEX_HOOKS_FILE.stat().st_mode) == 0o600
    assert not owned.backup_path(cli.CODEX_HOOKS_FILE).exists()
    cli.cmd_uninstall(SimpleNamespace(keep_data=True))
    assert backup.read_bytes() == original
    assert json.loads(settings_file.read_text()) == {"env": {"A": "1"}}


def test_install_failure_rolls_back_every_harness_file(settings_file, monkeypatch, capsys):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    cli.CODEX_HOOKS_FILE.parent.mkdir()
    cli.CODEX_HOOKS_FILE.write_text('{"hooks": {}}')
    before = {path: path.read_bytes() for path in (settings_file, cli.CODEX_HOOKS_FILE)}
    real = owned._replace
    failed = []

    def replace(source, destination):
        if destination == cli.CODEX_HOOKS_FILE and not failed:
            failed.append(destination)
            raise OSError("injected failure")
        real(source, destination)

    monkeypatch.setattr(owned, "_replace", replace)
    monkeypatch.setattr(cli.sys, "argv", ["tongs-agents", "install"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert output.err == (
        f"tongs-agents: {cli.CODEX_HOOKS_FILE}: could not write (injected failure); "
        "every change was rolled back\n"
    )
    assert "Updated" not in output.out
    assert {path: path.read_bytes() for path in before} == before
    assert not owned.backup_path(settings_file).exists()
    assert not owned.backup_path(cli.CODEX_HOOKS_FILE).exists()


@posix_only
def test_symlinked_codex_hooks_stop_install_before_any_write(settings_file, monkeypatch,
                                                              tmp_path):
    monkeypatch.setattr("shutil.which", lambda x: f"/usr/bin/{x}")
    real = tmp_path / "real-hooks.json"
    real.write_text("{}")
    cli.CODEX_HOOKS_FILE.parent.mkdir()
    cli.CODEX_HOOKS_FILE.symlink_to(real)
    with pytest.raises(cli.ConfigError, match="symlink"):
        cli.cmd_install(None)
    assert settings_file.read_text() == "{}"
    assert real.read_text() == "{}"
    assert cli.CODEX_HOOKS_FILE.is_symlink()


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
    fleet = paths.sessions_dir(create=True)
    for name in (
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
    assert not paths.state_dir().exists()


def test_uninstall_removes_only_record_shaped_json(fleet_data, capsys):
    for name in ("abc.json", "fleet.json", "fleet-v1.json", "settings.json"):
        (fleet_data / name).write_text("{}")
    (fleet_data / "fleet-v2-beef.json").write_text("{}")
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert sorted(entry.name for entry in fleet_data.iterdir()) == [
        "abc.json", "fleet-v1.json", "fleet.json", "settings.json",
    ]
    assert "holds other files" in capsys.readouterr().out


def test_uninstall_never_deletes_legacy_session_dir(fleet_data):
    old = legacy.legacy_sessions_dir()
    old.mkdir(parents=True)
    (old / "abc.json").write_text("{}")
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert (old / "abc.json").exists()


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
    link = tmp_path / "linked-state" / "sessions"
    link.parent.mkdir()
    link.symlink_to(fleet_data)
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(link.parent))
    before = sorted(entry.name for entry in fleet_data.iterdir())
    cli.cmd_uninstall(SimpleNamespace(keep_data=False))
    assert link.is_symlink()
    assert sorted(entry.name for entry in fleet_data.iterdir()) == before
