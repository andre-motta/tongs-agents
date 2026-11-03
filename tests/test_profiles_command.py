import json
import sys

import pytest

from tongs_agents import cli, paths
from tongs_agents.commands import profiles as profiles_command


CONFIG = """
[profiles.claude-alt]
command = ["claude"]
config_dir = "/synthetic/claude-alt"
env = { EXAMPLE_TOKEN = "secret-value" }

[profiles.claude-sandbox]
command = ["/opt/example/bin/claude-sandbox", "--project", "example"]
harness = "claude"

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[profiles.example-agent]
command = ["example-agent"]

[profiles.rel]
command = ["claude"]
env = { CLAUDE_CONFIG_DIR = "relative" }
"""


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path / "home" / ".claude")
    monkeypatch.setattr(cli, "SETTINGS_FILE", tmp_path / "home" / ".claude" / "settings.json")
    monkeypatch.setattr(cli, "CODEX_DIR", tmp_path / "home" / ".codex")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", tmp_path / "home" / ".codex" / "hooks.json")
    path = paths.config_dir(create=True) / "config.toml"
    path.write_text(CONFIG)
    return path


def _run(monkeypatch, capsys, *argv):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "profiles", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as raised:
        code = raised.code
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_list_shows_homes_and_commands(config, tmp_path, monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["NAME", "HARNESS", "HOME", "COMMAND"]
    alt = next(line for line in lines if line.startswith("claude-alt "))
    assert "/synthetic/claude-alt" in alt
    review = next(line for line in lines if line.startswith("codex-review"))
    assert f"{tmp_path / 'home' / '.codex'} (default)" in review
    assert "codex --profile review" in review
    assert "unmonitored" in next(line for line in lines if line.startswith("example-agent"))
    assert "secret-value" not in out
    assert _run(monkeypatch, capsys, "list")[1] == out


def test_list_json(config, monkeypatch, capsys):
    for argv in (("--json",), ("list", "--json"), ("--json", "list")):
        code, out, _ = _run(monkeypatch, capsys, *argv)
        data = json.loads(out)
        assert [item["name"] for item in data] == [
            "claude-alt", "claude-sandbox", "codex-review", "example-agent", "rel",
        ]
        assert data[0]["env"] == ["EXAMPLE_TOKEN"]
        assert "secret-value" not in out


def test_show(config, monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys, "show", "claude-alt")
    assert code == 0
    assert "harness: claude" in out
    assert "config_home: /synthetic/claude-alt" in out
    assert "env: EXAMPLE_TOKEN" in out
    assert "secret-value" not in out
    code, out, _ = _run(monkeypatch, capsys, "show", "claude-alt", "--json")
    assert json.loads(out)["command"] == ["claude"]


def test_show_unknown(config, monkeypatch, capsys):
    code, _, err = _run(monkeypatch, capsys, "show", "missing")
    assert code == 1
    assert "unknown profile 'missing'" in err


def test_no_profiles(monkeypatch, capsys):
    code, out, _ = _run(monkeypatch, capsys)
    assert code == 0
    assert out.startswith("No profiles in ")


def test_invalid_config(config, monkeypatch, capsys):
    config.write_text("[profiles.Bad]\ncommand = ['x']\n")
    code, _, err = _run(monkeypatch, capsys)
    assert code == 1
    assert "profiles.Bad" in err


def test_profiles_command_never_writes(config, monkeypatch, capsys):
    before = config.read_bytes()
    _run(monkeypatch, capsys, "list")
    _run(monkeypatch, capsys, "show", "rel")
    assert config.read_bytes() == before
    assert not paths.state_dir().exists()


def test_doctor_checks(config):
    checks = {(check.level, check.message.split()[1]) for check in profiles_command.doctor_checks()}
    assert ("warn", "claude-sandbox") in checks
    assert ("warn", "example-agent") in checks
    assert ("warn", "rel") in checks
    assert not any(name in ("claude-alt", "codex-review") for _, name in checks)


def test_doctor_checks_report_invalid_config(config):
    config.write_text("not toml [")
    [check] = profiles_command.doctor_checks()
    assert check.level == "error"
    assert "invalid TOML" in check.message


def test_codex_wrapper_without_home_is_info(config):
    config.write_text('[profiles.cw]\ncommand = ["/opt/x/codex-wrap"]\nharness = "codex"\n')
    [check] = profiles_command.doctor_checks()
    assert check.level == "info"
