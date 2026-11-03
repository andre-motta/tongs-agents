import json
import os
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


def test_show_includes_the_sandbox_table(config, monkeypatch, capsys):
    config.write_text(
        '[profiles.boxed]\ncommand = ["claude"]\ncwd = "/srv/project"\n'
        '[profiles.boxed.sandbox]\nproviders = ["anthropic"]\nmemory = "4Gi"\n'
    )
    code, out, _ = _run(monkeypatch, capsys, "show", "boxed")
    assert code == 0
    assert "config_home: inside the OpenShell sandbox" in out
    assert ("sandbox: mode=exec, upload=True, workdir=/sandbox/workspace, providers=anthropic, "
            "memory=4Gi") in out
    code, out, _ = _run(monkeypatch, capsys, "show", "boxed", "--json")
    info = json.loads(out)
    assert info["sandbox"]["providers"] == ["anthropic"]
    assert info["sandbox"]["upload"] is True
    code, out, _ = _run(monkeypatch, capsys)
    assert "inside the OpenShell sandbox" in out


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
    assert ("info", "example-agent") in checks
    assert ("warn", "rel") in checks
    assert not any(name in ("claude-alt", "codex-review") for _, name in checks)


def test_doctor_checks_report_invalid_config(config):
    config.write_text("not toml [")
    [check] = profiles_command.doctor_checks()
    assert check.level == "error"
    assert "invalid TOML" in check.message


def test_other_harness_points_at_notify(config):
    config.write_text('[profiles.tests]\ncommand = ["sh", "-c", "make test"]\n')
    [check] = profiles_command.doctor_checks()
    assert check.level == "info"
    assert "tongs-agents notify" in check.message
    assert check.remediation.startswith("Report status with tongs-agents notify")


def test_missing_cwd_is_a_warning(config, tmp_path):
    missing = tmp_path / "not-there"
    config.write_text(
        f'[profiles.a]\ncommand = ["claude"]\ncwd = "{missing}"\n'
        f'[profiles.b]\ncommand = ["claude"]\ncwd = "{tmp_path}"\n'
    )
    [check] = profiles_command.doctor_checks()
    assert check.level == "warn"
    assert check.message.startswith(f"Profile a has cwd {missing}")
    assert check.remediation == "Create that directory or change cwd in [profiles.a]"


def test_untraversable_cwd_is_a_warning_not_a_crash(config, tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    project = blocked / "project"
    config.write_text(f'[profiles.a]\ncommand = ["claude"]\ncwd = "{project}"\n')
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if os.fspath(path).startswith(str(blocked)):
            raise PermissionError(13, "Permission denied", os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    [check] = profiles_command.doctor_checks()
    assert check.level == "warn"
    assert check.message.startswith(f"Profile a has cwd {project}")


def test_codex_wrapper_without_home_is_info(config):
    config.write_text('[profiles.cw]\ncommand = ["/opt/x/codex-wrap"]\nharness = "codex"\n')
    [check] = profiles_command.doctor_checks()
    assert check.level == "info"
