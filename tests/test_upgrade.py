"""Upgrade from a synthetic claude-fleet-monitor install.

Hand-written fixtures follow the documented Claude settings.json, Codex
hooks.json and `codex mcp get --json` shapes. The Codex CLI is a fake script.
"""

import json
import os
import shlex
import sys

import pytest

from tongs_agents import cli, legacy


pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake CLIs are POSIX scripts")

LEGACY_HOOK = "/opt/old-venv/bin/claude-fleet-hook"
LEGACY_PYTHON = "/opt/old-venv/bin/python3.11"
LEGACY_MCP = {"command": LEGACY_PYTHON, "args": ["-m", "claude_fleet_monitor.mcp_server"]}
CURRENT_MCP_ARGS = ["-m", "tongs_agents.mcp_server"]
USER_MENTION = "notify-send 'tongs-agents-hook finished'"

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
            "docs": {"command": "npx", "args": ["docs-server"]},
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
    bin_dir = tmp_path / "bin"
    for directory in (claude_dir / "bin", codex_dir, bin_dir):
        directory.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home / ".pi" / "agent"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(cli, "CLAUDE_DIR", claude_dir)
    monkeypatch.setattr(cli, "CODEX_DIR", codex_dir)
    monkeypatch.setattr(cli, "SETTINGS_FILE", claude_dir / "settings.json")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", codex_dir / "hooks.json")
    monkeypatch.setattr(cli, "FLEET_DIR", claude_dir / "fleet")
    monkeypatch.setattr(
        legacy, "LEGACY_HOOK_SCRIPTS", ((claude_dir / "bin" / "fleet-hook.sh").resolve(),)
    )
    pi_calls = []
    monkeypatch.setattr(cli, "install_pi", lambda pi, hook: pi_calls.append("install") or True)
    monkeypatch.setattr(cli, "uninstall_pi", lambda pi: pi_calls.append("uninstall") or True)

    codex = bin_dir / "codex"
    codex.write_text(FAKE_CODEX.format(python=sys.executable))
    for name in ("tongs-agents-hook", "pi"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    codex_state = tmp_path / "codex-mcp.json"
    codex_log = tmp_path / "codex-calls.jsonl"
    codex_log.write_text("")
    monkeypatch.setenv("FAKE_CODEX_STATE", str(codex_state))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(codex_log))

    cli.SETTINGS_FILE.write_text(json.dumps(_legacy_claude_settings(claude_dir), indent=2))
    cli.CODEX_HOOKS_FILE.write_text(json.dumps(_legacy_codex_hooks(), indent=2))
    codex_state.write_text(json.dumps({
        "fleet": LEGACY_MCP,
        "docs": {"command": "npx", "args": ["docs-server"]},
    }))
    legacy_script = claude_dir / "bin" / "fleet-hook.sh"
    legacy_script.write_text("#!/bin/bash\n")

    class Env:
        settings = cli.SETTINGS_FILE
        codex_hooks = cli.CODEX_HOOKS_FILE
        state = codex_state
        log = codex_log
        hook = str(bin_dir / "tongs-agents-hook")
        calls = pi_calls

        @staticmethod
        def codex_calls():
            return [json.loads(line) for line in codex_log.read_text().splitlines()]

    return Env


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["tongs-agents", *argv])
    cli.main()


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


def test_install_adopts_legacy_install(upgrade, monkeypatch, capsys):
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
    assert settings["mcpServers"] == {
        "fleet": {"command": sys.executable, "args": CURRENT_MCP_ARGS, "env": {}},
        "docs": {"command": "npx", "args": ["docs-server"]},
    }
    assert settings["model"] == "opus"
    assert settings["permissions"] == {"allow": ["Bash(ls:*)"]}

    codex_hooks = json.loads(upgrade.codex_hooks.read_text())
    _assert_no_legacy(codex_hooks)
    for event, arg in cli.CODEX_HOOK_EVENTS:
        owned = _owned_groups(codex_hooks["hooks"][event])
        assert len(owned) == 1
        assert _commands(owned) == [shlex.join([upgrade.hook, arg, "--agent", "codex"])]
    assert owned_matcher(codex_hooks) == "startup|resume|clear"
    assert "say done" in _commands(codex_hooks["hooks"]["Stop"])

    assert json.loads(upgrade.state.read_text()) == {
        "fleet": {"command": sys.executable, "args": CURRENT_MCP_ARGS},
        "docs": {"command": "npx", "args": ["docs-server"]},
    }
    assert upgrade.calls == ["install"]
    assert "Kept existing" not in capsys.readouterr().out


def test_second_install_is_byte_identical(upgrade, monkeypatch):
    _run(monkeypatch, "install", "--agent", "all")
    first = (upgrade.settings.read_bytes(), upgrade.codex_hooks.read_bytes(),
             upgrade.state.read_bytes())
    calls_before = len(upgrade.codex_calls())

    _run(monkeypatch, "install", "--agent", "all")

    assert (upgrade.settings.read_bytes(), upgrade.codex_hooks.read_bytes(),
            upgrade.state.read_bytes()) == first
    assert [call[:2] for call in upgrade.codex_calls()[calls_before:]] == [["mcp", "get"]]


def test_uninstall_removes_legacy_entries(upgrade, monkeypatch):
    _run(monkeypatch, "uninstall", "--keep-data")

    settings = json.loads(upgrade.settings.read_text())
    assert sorted(_all_commands(settings)) == ["notify-send 'tongs-agents-hook finished'", "other-hook --check"]
    assert settings["mcpServers"] == {"docs": {"command": "npx", "args": ["docs-server"]}}
    codex_hooks = json.loads(upgrade.codex_hooks.read_text())
    assert codex_hooks == {"hooks": {"Stop": [_group("say done")]}}
    assert json.loads(upgrade.state.read_text()) == {
        "docs": {"command": "npx", "args": ["docs-server"]},
    }


def test_uninstall_after_upgrade_removes_new_entries(upgrade, monkeypatch):
    _run(monkeypatch, "install", "--agent", "all")
    _run(monkeypatch, "uninstall", "--agent", "all", "--keep-data")

    settings = json.loads(upgrade.settings.read_text())
    assert sorted(_all_commands(settings)) == ["notify-send 'tongs-agents-hook finished'", "other-hook --check"]
    assert "fleet" not in settings["mcpServers"]
    assert _all_commands(json.loads(upgrade.codex_hooks.read_text())) == ["say done"]
    assert "fleet" not in json.loads(upgrade.state.read_text())
    assert upgrade.calls == ["install", "uninstall"]


def test_foreign_fleet_entries_are_kept(upgrade, monkeypatch, capsys):
    foreign_claude = {"command": "node", "args": ["fleet.js"], "env": {"A": "1"}}
    foreign_codex = {"command": "/usr/bin/python3", "args": ["-m", "someone.fleet"]}
    settings = json.loads(upgrade.settings.read_text())
    settings["mcpServers"]["fleet"] = foreign_claude
    upgrade.settings.write_text(json.dumps(settings))
    upgrade.state.write_text(json.dumps({"fleet": foreign_codex}))

    _run(monkeypatch, "install")
    output = capsys.readouterr().out
    assert "Kept existing Claude MCP server named fleet" in output
    assert "Kept existing Codex MCP server named fleet" in output
    assert json.loads(upgrade.settings.read_text())["mcpServers"]["fleet"] == foreign_claude
    assert json.loads(upgrade.state.read_text()) == {"fleet": foreign_codex}

    _run(monkeypatch, "uninstall", "--keep-data")
    assert json.loads(upgrade.settings.read_text())["mcpServers"]["fleet"] == foreign_claude
    assert json.loads(upgrade.state.read_text()) == {"fleet": foreign_codex}
    assert not any(call[1] in ("add", "remove") for call in upgrade.codex_calls())


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
    assert not cli.FLEET_DIR.exists()
