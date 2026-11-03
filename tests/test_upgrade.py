"""Upgrade from a synthetic claude-fleet-monitor 0.9.x install.

Hand-written fixtures follow the documented Claude settings.json and
`.claude.json`, Codex hooks.json and `codex mcp get --json`, and Pi package
shapes. The Claude, Codex and Pi CLIs are fake scripts.
"""

import json
import os
import shlex
import sys

import pytest

from tongs_agents import cli, legacy, paths, pi_install


pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake CLIs are POSIX scripts")

LEGACY_HOOK = "/opt/old-venv/bin/claude-fleet-hook"
LEGACY_PYTHON = "/opt/old-venv/bin/python3.11"
LEGACY_MCP = {"command": LEGACY_PYTHON, "args": ["-m", "claude_fleet_monitor.mcp_server"]}
LEGACY_PI_EXTENSION = "/opt/old-venv/lib/python3.11/site-packages/claude_fleet_monitor/pi_extension"
CURRENT_MCP_ARGS = ["-m", "tongs_agents.mcp_server"]
USER_MENTION = "notify-send 'tongs-agents-hook finished'"
DOCS_MCP = {"command": "npx", "args": ["docs-server"]}

FAKE_CODEX = """\
#!{python}
import json, os, sys
state_path = os.environ["FAKE_CODEX_STATE"]
with open(state_path) as stream:
    state = json.load(stream)
with open(os.environ["FAKE_CODEX_LOG"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args[:2] == ["mcp", "get"] and args[3:] == ["--json"]:
    if args[2] not in state:
        print("No MCP server named " + args[2], file=sys.stderr)
        sys.exit(1)
    server = state[args[2]]
    print(json.dumps({{
        "name": args[2],
        "enabled": True,
        "transport": {{"type": "stdio", "command": server["command"],
                       "args": server["args"], "env": None}},
    }}))
elif args[:2] == ["mcp", "add"] and args[3] == "--":
    state[args[2]] = {{"command": args[4], "args": args[5:]}}
elif args[:2] == ["mcp", "remove"]:
    if args[2] not in state:
        sys.exit(1)
    del state[args[2]]
else:
    sys.exit(2)
with open(state_path, "w") as stream:
    json.dump(state, stream, indent=2, sort_keys=True)
"""

FAKE_CLAUDE = """\
#!{python}
import json, os, sys
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as stream:
    stream.write(json.dumps([sys.argv[1:], "CLAUDE_CONFIG_DIR" in os.environ]) + "\\n")
path = os.path.join(os.environ["HOME"], ".claude.json")
with open(path) as stream:
    data = json.load(stream)
servers = data.setdefault("mcpServers", {{}})
args = sys.argv[1:]
if args[:4] == ["mcp", "add", "--scope", "user"] and args[5] == "--":
    if args[4] in servers:
        sys.exit(1)
    servers[args[4]] = {{"type": "stdio", "command": args[6], "args": args[7:], "env": {{}}}}
elif args[:4] == ["mcp", "remove", "--scope", "user"]:
    if args[4] not in servers:
        sys.exit(1)
    del servers[args[4]]
else:
    sys.exit(2)
with open(path, "w") as stream:
    json.dump(data, stream, indent=2)
"""

FAKE_PI = """\
#!{python}
import json, os, sys
args = sys.argv[1:]
if args == ["--version"]:
    print("0.85.0")
    sys.exit(0)
state_path = os.environ["FAKE_PI_STATE"]
with open(state_path) as stream:
    packages = json.load(stream)
if args[0] == "install":
    if args[1] not in packages:
        packages.append(args[1])
elif args[0] == "remove":
    if args[1] not in packages:
        print("No matching package found", file=sys.stderr)
        sys.exit(1)
    packages.remove(args[1])
else:
    sys.exit(2)
with open(state_path, "w") as stream:
    json.dump(packages, stream)
"""


def _group(command, timeout=5, **extra):
    return {**extra, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def _legacy_claude_settings(claude_dir):
    hooks = {
        event: [_group(f"{LEGACY_HOOK} {arg} --agent claude")]
        for event, arg in cli.CLAUDE_HOOK_EVENTS
    }
    hooks["PreToolUse"] = [{
        "matcher": "Bash",
        "hooks": [
            {"type": "command", "command": "other-hook --check"},
            {"type": "command", "command": f"{LEGACY_HOOK} tool-use --agent claude"},
        ],
    }]
    hooks["Stop"].append(_group(f"{claude_dir / 'bin' / 'fleet-hook.sh'} stop"))
    hooks["UserPromptSubmit"].append(_group(USER_MENTION))
    hooks["Notification"] = [_group(f"{claude_dir / 'bin' / 'fleet-hook.sh'} notification")]
    return {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"]},
        "hooks": hooks,
        "mcpServers": {
            "fleet": {**LEGACY_MCP, "env": {}},
            "docs": DOCS_MCP,
        },
    }


def _legacy_codex_hooks():
    hooks = {
        event: [_group(f"{LEGACY_HOOK} {arg} --agent codex")]
        for event, arg in cli.CODEX_HOOK_EVENTS
    }
    hooks["SessionStart"][0]["matcher"] = "startup|resume|clear"
    hooks["Stop"].insert(0, _group("say done"))
    return {"hooks": hooks}


@pytest.fixture
def upgrade(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    codex_dir = home / ".codex"
    pi_dir = home / ".pi" / "agent"
    bin_dir = tmp_path / "bin"
    for directory in (claude_dir / "bin", codex_dir, pi_dir, bin_dir):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv(paths.STATE_DIR_ENV)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "not-the-default"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", home / ".claude.json")
    monkeypatch.setattr(cli, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(cli, "SETTINGS_FILE", claude_dir / "settings.json")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", codex_dir / "hooks.json")
    monkeypatch.setattr(
        legacy, "LEGACY_HOOK_SCRIPTS", ((claude_dir / "bin" / "fleet-hook.sh").resolve(),)
    )
    extension = tmp_path / "site-packages" / "tongs_agents" / "pi_extension"
    extension.mkdir(parents=True)
    monkeypatch.setattr(pi_install, "pi_extension_path", lambda: extension)

    fakes = {"codex": FAKE_CODEX, "claude": FAKE_CLAUDE, "pi": FAKE_PI}
    for name, script in fakes.items():
        (bin_dir / name).write_text(script.format(python=sys.executable))
    (bin_dir / "tongs-agents-hook").write_text("#!/bin/sh\nexit 0\n")
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    codex_state = tmp_path / "codex-mcp.json"
    codex_log = tmp_path / "codex-calls.jsonl"
    claude_log = tmp_path / "claude-calls.jsonl"
    pi_state = tmp_path / "pi-packages.json"
    codex_log.write_text("")
    claude_log.write_text("")
    monkeypatch.setenv("FAKE_CODEX_STATE", str(codex_state))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(codex_log))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(claude_log))
    monkeypatch.setenv("FAKE_PI_STATE", str(pi_state))

    cli.SETTINGS_FILE.write_text(json.dumps(_legacy_claude_settings(claude_dir), indent=2))
    cli.CODEX_HOOKS_FILE.write_text(json.dumps(_legacy_codex_hooks(), indent=2))
    cli.CLAUDE_USER_CONFIG.write_text(json.dumps({
        "numStartups": 7,
        "mcpServers": {"docs": {"type": "stdio", **DOCS_MCP, "env": {}}},
    }))
    codex_state.write_text(json.dumps({"fleet": LEGACY_MCP, "docs": DOCS_MCP}))
    pi_state.write_text(json.dumps(["npm:other-pi-package", LEGACY_PI_EXTENSION]))
    legacy_pi_config = pi_dir / legacy.LEGACY_PI_CONFIG_NAME
    legacy_pi_config.write_text(json.dumps({
        "schema_version": 1,
        "owner": legacy.LEGACY_PI_CONFIG_OWNER,
        "hook_path": LEGACY_HOOK,
        "extension_path": LEGACY_PI_EXTENSION,
        "managed_paths": [LEGACY_PI_EXTENSION],
    }))
    legacy_script = claude_dir / "bin" / "fleet-hook.sh"
    legacy_script.write_text("#!/bin/bash\n")
    legacy_sessions = claude_dir / "fleet"
    legacy_sessions.mkdir()
    (legacy_sessions / "fleet-v1-old.json").write_text("{}")

    class Env:
        settings = cli.SETTINGS_FILE
        codex_hooks = cli.CODEX_HOOKS_FILE
        claude_config = cli.CLAUDE_USER_CONFIG
        state = codex_state
        pi_packages = pi_state
        pi_config = pi_dir / pi_install.PI_CONFIG_NAME
        old_pi_config = legacy_pi_config
        old_sessions = legacy_sessions
        hook = str(bin_dir / "tongs-agents-hook")
        current_extension = str(extension)

        @staticmethod
        def codex_calls():
            return [json.loads(line) for line in codex_log.read_text().splitlines()]

        @staticmethod
        def claude_calls():
            return [json.loads(line) for line in claude_log.read_text().splitlines()]

        @classmethod
        def files(cls):
            return [
                path.read_bytes()
                for path in (cls.settings, cls.codex_hooks, cls.claude_config,
                             cls.state, cls.pi_packages, cls.pi_config)
            ]

    return Env


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["tongs-agents", *argv])
    cli.main()


def _doctor(monkeypatch, *argv):
    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, "doctor", *argv)
    return raised.value.code


def _commands(groups):
    return [hook["command"] for group in groups for hook in group.get("hooks", [])]


def _all_commands(config):
    return [
        command
        for groups in config.get("hooks", {}).values()
        for command in _commands(groups)
    ]


def _owned_groups(groups):
    return [
        group for group in groups
        if any(shlex.split(command)[0].endswith("/tongs-agents-hook")
               for command in _commands([group]))
    ]


def owned_matcher(codex_hooks):
    return _owned_groups(codex_hooks["hooks"]["SessionStart"])[0]["matcher"]


def _assert_no_legacy(config):
    for command in _all_commands(config):
        assert "claude-fleet-hook" not in command
        assert "fleet-hook.sh" not in command


def _claude_servers(upgrade):
    return json.loads(upgrade.claude_config.read_text())["mcpServers"]


def test_install_cleans_up_legacy_install(upgrade, monkeypatch, capsys):
    _run(monkeypatch, "install", "--agent", "all")

    settings = json.loads(upgrade.settings.read_text())
    _assert_no_legacy(settings)
    assert set(settings["hooks"]) == {event for event, _ in cli.CLAUDE_HOOK_EVENTS}
    for event, arg in cli.CLAUDE_HOOK_EVENTS:
        owned = _owned_groups(settings["hooks"][event])
        assert len(owned) == 1
        assert _commands(owned) == [shlex.join([upgrade.hook, arg, "--agent", "claude"])]
    assert settings["hooks"]["PreToolUse"][0] == {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "other-hook --check"}],
    }
    assert USER_MENTION in _commands(settings["hooks"]["UserPromptSubmit"])
    assert settings["mcpServers"] == {"docs": DOCS_MCP}
    assert settings["model"] == "opus"
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}

    assert _claude_servers(upgrade) == {
        "docs": {"type": "stdio", **DOCS_MCP, "env": {}},
        "tongs-agents": {"type": "stdio", "command": sys.executable,
                         "args": CURRENT_MCP_ARGS, "env": {}},
    }
    assert json.loads(upgrade.claude_config.read_text())["numStartups"] == 7
    assert not any(with_config_dir for _, with_config_dir in upgrade.claude_calls())

    codex_hooks = json.loads(upgrade.codex_hooks.read_text())
    _assert_no_legacy(codex_hooks)
    for event, arg in cli.CODEX_HOOK_EVENTS:
        owned = _owned_groups(codex_hooks["hooks"][event])
        assert len(owned) == 1
        assert _commands(owned) == [shlex.join([upgrade.hook, arg, "--agent", "codex"])]
    assert owned_matcher(codex_hooks) == "startup|resume|clear"
    assert "say done" in _commands(codex_hooks["hooks"]["Stop"])

    assert json.loads(upgrade.state.read_text()) == {
        "tongs-agents": {"command": sys.executable, "args": CURRENT_MCP_ARGS},
        "docs": DOCS_MCP,
    }

    assert json.loads(upgrade.pi_packages.read_text()) == [
        "npm:other-pi-package", upgrade.current_extension,
    ]
    assert not upgrade.old_pi_config.exists()
    assert json.loads(upgrade.pi_config.read_text())["owner"] == "tongs-agents"

    sessions = paths.sessions_dir()
    assert sessions == upgrade.settings.parent.parent / ".local" / "state" / "tongs-agents" / "sessions"
    assert sessions.is_dir()
    assert (upgrade.old_sessions / "fleet-v1-old.json").exists()
    output = capsys.readouterr().out
    assert "Kept existing" not in output
    assert "tongs-agents doctor" in output


def test_second_install_is_byte_identical(upgrade, monkeypatch):
    _run(monkeypatch, "install", "--agent", "all")
    first = upgrade.files()
    codex_before = len(upgrade.codex_calls())
    claude_before = len(upgrade.claude_calls())

    _run(monkeypatch, "install", "--agent", "all")

    assert upgrade.files() == first
    assert [call[:2] for call in upgrade.codex_calls()[codex_before:]] == [
        ["mcp", "get"], ["mcp", "get"],
    ]
    assert upgrade.claude_calls()[claude_before:] == []


def test_uninstall_removes_legacy_entries(upgrade, monkeypatch):
    _run(monkeypatch, "uninstall", "--agent", "all", "--keep-data")

    settings = json.loads(upgrade.settings.read_text())
    assert sorted(_all_commands(settings)) == [USER_MENTION, "other-hook --check"]
    assert settings["mcpServers"] == {"docs": DOCS_MCP}
    codex_hooks = json.loads(upgrade.codex_hooks.read_text())
    assert codex_hooks == {"hooks": {"Stop": [_group("say done")]}}
    assert json.loads(upgrade.state.read_text()) == {"docs": DOCS_MCP}
    assert json.loads(upgrade.pi_packages.read_text()) == ["npm:other-pi-package"]
    assert not upgrade.old_pi_config.exists()
    assert upgrade.old_sessions.is_dir()


def test_uninstall_after_upgrade_removes_new_entries(upgrade, monkeypatch):
    _run(monkeypatch, "install", "--agent", "all")
    _run(monkeypatch, "uninstall", "--agent", "all")

    settings = json.loads(upgrade.settings.read_text())
    assert sorted(_all_commands(settings)) == [USER_MENTION, "other-hook --check"]
    assert settings["mcpServers"] == {"docs": DOCS_MCP}
    assert _all_commands(json.loads(upgrade.codex_hooks.read_text())) == ["say done"]
    assert json.loads(upgrade.state.read_text()) == {"docs": DOCS_MCP}
    assert list(_claude_servers(upgrade)) == ["docs"]
    assert json.loads(upgrade.pi_packages.read_text()) == ["npm:other-pi-package"]
    assert not upgrade.pi_config.exists()
    assert not paths.state_dir().exists()
    assert (upgrade.old_sessions / "fleet-v1-old.json").exists()


def test_foreign_entries_are_kept(upgrade, monkeypatch, capsys):
    foreign_claude = {"command": "node", "args": ["fleet.js"], "env": {"A": "1"}}
    foreign_codex = {"command": "/usr/bin/python3", "args": ["-m", "someone.fleet"]}
    settings = json.loads(upgrade.settings.read_text())
    settings["mcpServers"]["fleet"] = foreign_claude
    upgrade.settings.write_text(json.dumps(settings))
    upgrade.claude_config.write_text(json.dumps({"mcpServers": {
        "fleet": foreign_claude, "tongs-agents": foreign_claude,
    }}))
    upgrade.state.write_text(json.dumps({"fleet": foreign_codex, "tongs-agents": foreign_codex}))

    _run(monkeypatch, "install")
    output = capsys.readouterr().out
    assert "Kept existing Claude Code MCP server named tongs-agents" in output
    assert "Kept existing Codex MCP server named tongs-agents" in output
    assert json.loads(upgrade.settings.read_text())["mcpServers"]["fleet"] == foreign_claude
    expected_claude = {"fleet": foreign_claude, "tongs-agents": foreign_claude}
    expected_codex = {"fleet": foreign_codex, "tongs-agents": foreign_codex}
    assert _claude_servers(upgrade) == expected_claude
    assert json.loads(upgrade.state.read_text()) == expected_codex

    _run(monkeypatch, "uninstall", "--keep-data")
    assert json.loads(upgrade.settings.read_text())["mcpServers"]["fleet"] == foreign_claude
    assert _claude_servers(upgrade) == expected_claude
    assert json.loads(upgrade.state.read_text()) == expected_codex
    assert not any(call[1] in ("add", "remove") for call in upgrade.codex_calls())
    assert upgrade.claude_calls() == []


def test_doctor_reports_leftovers(upgrade, monkeypatch, capsys):
    old_cli = upgrade.settings.parent.parent.parent / "bin" / "claude-fleet"
    old_cli.write_text("#!/bin/sh\n")
    old_cli.chmod(0o755)
    assert _doctor(monkeypatch) == 2
    output = capsys.readouterr().out
    assert str(paths.sessions_dir()) in output
    assert shlex.join(["rm", "-r", "--", str(upgrade.old_sessions)]) in output
    assert f"claude-fleet at {old_cli}" in output
    assert "hook command(s)" in output
    assert "unused MCP entries: fleet" in output
    assert str(upgrade.old_pi_config) in output
    assert "pip uninstall claude-fleet-monitor" in output
    assert upgrade.old_sessions.is_dir()


def test_doctor_after_install_reports_only_manual_steps(upgrade, monkeypatch, capsys):
    _run(monkeypatch, "install", "--agent", "all")
    capsys.readouterr()
    before = upgrade.files()
    assert _doctor(monkeypatch) == 1
    output = capsys.readouterr().out
    assert "hook command(s)" not in output
    assert "unused MCP entries" not in output
    assert "registers the old MCP server" not in output
    assert str(upgrade.old_sessions) in output
    assert upgrade.files() == before


def test_doctor_reports_clean_state(upgrade, monkeypatch, capsys):
    _run(monkeypatch, "install", "--agent", "all")
    for path in upgrade.old_sessions.iterdir():
        path.unlink()
    upgrade.old_sessions.rmdir()
    capsys.readouterr()
    _doctor(monkeypatch)
    output = capsys.readouterr().out
    assert "No claude-fleet-monitor leftovers found." in output
    assert "[ERROR]" not in output


@pytest.mark.parametrize("command", ["install", "uninstall"])
@pytest.mark.parametrize("target", ["settings", "codex_hooks"])
def test_malformed_json_exits_without_writing(upgrade, monkeypatch, capsys, command, target):
    broken = getattr(upgrade, target)
    broken.write_text('{"hooks": {')
    before = {path: path.read_bytes() for path in (upgrade.settings, upgrade.codex_hooks)}
    script = cli.CLAUDE_DIR / "bin" / "fleet-hook.sh"

    with pytest.raises(SystemExit) as raised:
        _run(monkeypatch, command)

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert error.count("\n") == 1
    assert str(broken) in error
    assert "Traceback" not in error
    assert {path: path.read_bytes() for path in before} == before
    assert script.exists()
    assert upgrade.codex_calls() == []
    assert upgrade.claude_calls() == []
    assert not paths.state_dir().exists()
