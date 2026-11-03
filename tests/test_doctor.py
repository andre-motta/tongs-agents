"""Doctor levels, exit codes and remediation commands (synthetic homes)."""

import json
import os
import sys
from importlib import metadata

import pytest

from tests.test_cli_config_dirs import _run, _write_profiles, home  # noqa: F401
from tongs_agents import cli, legacy, paths
from tongs_agents.commands import doctor


def _doctor(monkeypatch, capsys, *argv):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "doctor", "--json", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as raised:
        code = raised.code
    report = json.loads(capsys.readouterr().out)
    assert report["exit_code"] == code
    return code, report["checks"]


def _find(checks, level=None, name=None, text=""):
    return [
        check for check in checks
        if (level is None or check["level"] == level)
        and (name is None or check["name"] == name)
        and text in check["message"]
    ]


def _trust(codex_home):
    hooks = codex_home / "hooks.json"
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        f'[hooks.state."{hooks}:session_start:0:0"]\ntrusted_hash = "sha256:00"\n'
    )


@pytest.fixture
def installed(home, monkeypatch):  # noqa: F811
    _run(monkeypatch, "install")
    _trust(home.home / ".codex")
    return home


def test_clean_install_exits_zero(installed, monkeypatch, capsys):
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 0, [check for check in checks if check["level"] in ("warn", "error")]
    assert _find(checks, "ok", "hooks")
    assert _find(checks, "ok", "hook-command")
    assert _find(checks, "info", "codex-trust")


def test_doctor_only_reads(installed, monkeypatch, capsys):
    before = {
        path: path.read_bytes() for path in installed.root.rglob("*") if path.is_file()
    }
    _doctor(monkeypatch, capsys, "--config-dir", f"claude={installed.root / 'new'}")
    after = {
        path: path.read_bytes() for path in installed.root.rglob("*") if path.is_file()
    }
    assert after == before
    assert installed.calls[-1].argv[1:3] != ["mcp", "list"]


def test_not_installed_default_is_a_warning(home, monkeypatch, capsys):  # noqa: F811
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    claude = _find(checks, "warn", "hooks", str(cli.SETTINGS_FILE))
    assert claude[0]["remediation"] == "tongs-agents install --agent claude"


def test_partial_default_hooks_are_an_error(installed, monkeypatch, capsys):
    settings = json.loads(cli.SETTINGS_FILE.read_text())
    del settings["hooks"]["Stop"]
    cli.SETTINGS_FILE.write_text(json.dumps(settings))
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    missing = _find(checks, "error", "hooks", "missing tongs-agents hooks for Stop")
    assert missing[0]["remediation"] == "tongs-agents install --agent claude"


def test_missing_hooks_in_recorded_dir(installed, monkeypatch, capsys):
    a = installed.root / "a"
    _run(monkeypatch, "install", "--claude-config-dir", str(a))
    (a / "settings.json").write_text("{}")
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    error = _find(checks, "error", "hooks", str(a / "settings.json"))[0]
    assert error["remediation"] == cli._command_string(
        ["tongs-agents", "install", "--claude-config-dir", str(a)])


def test_config_dir_option_checks_extra_home(installed, monkeypatch, capsys):
    extra = installed.root / "extra"
    code, checks = _doctor(monkeypatch, capsys, "--config-dir", f"codex={extra}")
    assert code == 2
    error = _find(checks, "error", "hooks", str(extra / "hooks.json"))[0]
    assert error["remediation"].endswith(f"--codex-home {extra}")


def test_config_dir_option_validates(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "doctor", "--config-dir", "zsh=/x"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    assert raised.value.code == 2
    assert "KIND=DIR" in capsys.readouterr().err


def test_legacy_hook_markers_are_warnings(installed, monkeypatch, capsys):
    settings = json.loads(cli.SETTINGS_FILE.read_text())
    command = f"/opt/old/bin/{legacy.LEGACY_HOOK_COMMANDS[0]} stop --agent claude"
    settings["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": command}]})
    settings["mcpServers"] = {legacy.LEGACY_MCP_NAME: {
        "command": "/opt/old/bin/python3", "args": ["-m", legacy.LEGACY_MCP_MODULES[0]]}}
    cli.SETTINGS_FILE.write_text(json.dumps(settings))
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    hooks = _find(checks, "warn", "legacy-hooks")[0]
    assert "1 claude-fleet-monitor hook command(s)" in hooks["message"]
    assert hooks["remediation"] == "tongs-agents install --agent claude"
    assert _find(checks, "warn", "legacy-mcp", "unused MCP entries: fleet")


def test_foreign_and_legacy_mcp_entries(installed, monkeypatch, capsys):
    cli.CLAUDE_USER_CONFIG.write_text(json.dumps({"mcpServers": {
        "tongs-agents": {"command": "node", "args": ["mine.js"]},
        legacy.LEGACY_MCP_NAME: {"command": "python3", "args": ["-m", legacy.LEGACY_MCP_MODULES[0]]},
    }}))
    (installed.home / ".codex" / "config.toml").write_text(
        (installed.home / ".codex" / "config.toml").read_text()
        + '\n[mcp_servers.tongs-agents]\ncommand = "npx"\nargs = ["other"]\n'
    )
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    assert len(_find(checks, "warn", "foreign-mcp")) == 2
    assert _find(checks, "warn", "legacy-mcp", "registers the old MCP server")


def test_co_installation_is_an_error(installed, monkeypatch, capsys):
    monkeypatch.setattr(cli, "legacy_installed", lambda: [
        f"{legacy.LEGACY_DISTRIBUTION} 0.9.3 in this Python environment"])
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    error = _find(checks, "error", "legacy-installed")[0]
    assert error["remediation"].endswith(f"pip uninstall {legacy.LEGACY_DISTRIBUTION}")


@pytest.mark.parametrize("entries", [
    [("agents", "tongs_agents.tongs_plugin:FleetMonitorPlugin"),
     (legacy.LEGACY_TONGS_PLUGIN, f"{legacy.LEGACY_PACKAGE}.tongs_plugin:FleetMonitorPlugin")],
    [("agents", "tongs_agents.tongs_plugin:FleetMonitorPlugin"),
     ("agents-copy", "tongs_agents.tongs_plugin:FleetMonitorPlugin")],
])
def test_duplicate_plugin_entry_point(installed, monkeypatch, capsys, entries):
    points = [metadata.EntryPoint(name, value, doctor.PLUGIN_GROUP) for name, value in entries]
    monkeypatch.setattr(doctor, "_plugin_entry_points", lambda: points)
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    assert _find(checks, "error", "duplicate-plugin")


@pytest.mark.parametrize("legacy_entry, leftovers_line", [(True, False), (False, True)])
def test_leftover_line_follows_plugin_duplicates(monkeypatch, capsys, legacy_entry,
                                                 leftovers_line):
    ours = ("agents", "tongs_agents.tongs_plugin:FleetMonitorPlugin")
    other = (
        (legacy.LEGACY_TONGS_PLUGIN, f"{legacy.LEGACY_PACKAGE}.tongs_plugin:FleetMonitorPlugin")
        if legacy_entry else ("agents-copy", ours[1])
    )
    points = [metadata.EntryPoint(name, value, doctor.PLUGIN_GROUP) for name, value in (ours, other)]
    monkeypatch.setattr(doctor, "_plugin_entry_points", lambda: points)
    monkeypatch.setattr(cli, "legacy_installed", lambda: [])
    checks = doctor.check_coinstall()
    assert [check.name for check in checks] == ["duplicate-plugin"]
    assert "leftover" not in checks[0].to_json()
    doctor._print_text(checks)
    output = capsys.readouterr().out
    assert "[ERROR] tongs would load more than one agents plugin" in output
    assert ("No claude-fleet-monitor leftovers found." in output) is leftovers_line


def test_single_plugin_entry_point_is_fine():
    one = [metadata.EntryPoint("agents", "tongs_agents.tongs_plugin:X", doctor.PLUGIN_GROUP)]
    original = doctor._plugin_entry_points
    try:
        doctor._plugin_entry_points = lambda: one
        assert not [check for check in doctor.check_coinstall() if check.name == "duplicate-plugin"]
    finally:
        doctor._plugin_entry_points = original


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks and modes")
def test_symlinked_sessions_dir_is_an_error(home, tmp_path, monkeypatch, capsys):  # noqa: F811
    real = tmp_path / "real-sessions"
    real.mkdir()
    paths.state_dir(create=True)
    paths.sessions_dir().symlink_to(real)
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    assert _find(checks, "error", "sessions-dir", "symlink")


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX modes")
def test_unwritable_sessions_dir_is_an_error(installed, monkeypatch, capsys):
    sessions = paths.sessions_dir()
    sessions.chmod(0o500)
    try:
        code, checks = _doctor(monkeypatch, capsys)
    finally:
        sessions.chmod(0o700)
    assert code == 2
    assert _find(checks, "error", "sessions-dir", "not writable")


def test_missing_hook_command_is_an_error(installed, monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    assert _find(checks, "error", "hook-command")
    assert _find(checks, "error", "hooks", "no longer exists") == []


def test_stale_hook_path_is_an_error(installed, monkeypatch, capsys):
    os.unlink(installed.hook)
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    stale = _find(checks, "error", "hooks", "no longer exists")
    assert stale and installed.hook in stale[0]["details"]


def test_env_home_not_installed_is_a_warning(installed, monkeypatch, capsys):
    target = installed.root / "env-claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(target))
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    warning = _find(checks, "warn", "environment", "CLAUDE_CONFIG_DIR")[0]
    assert warning["remediation"].endswith(f"--claude-config-dir {target}")

    _run(monkeypatch, "install", "--claude-config-dir", str(target))
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 0
    assert _find(checks, "ok", "environment")


def test_codex_home_env_at_default_is_fine(installed, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_HOME", str(installed.home / ".codex"))
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 0
    assert _find(checks, "ok", "environment", "CODEX_HOME")


def test_untrusted_codex_hooks_are_a_warning(installed, monkeypatch, capsys):
    (installed.home / ".codex" / "config.toml").unlink()
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    assert _find(checks, "warn", "codex-trust")[0]["remediation"].endswith("/hooks")


def test_profile_homes_are_checked(installed, monkeypatch, capsys):
    a = installed.root / "a"
    _write_profiles(
        f'[profiles.claude-alt]\ncommand = ["claude"]\nconfig_dir = "{a}"\n'
        '[profiles.example]\ncommand = ["example-agent"]\n'
    )
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    error = _find(checks, "error", "hooks", "(profile claude-alt)")[0]
    assert error["remediation"] == "tongs-agents install"
    assert _find(checks, "warn", "profiles", "example")

    _run(monkeypatch, "install")
    _trust(installed.home / ".codex")
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 1
    assert not _find(checks, "error")


def test_pi_profile_without_extension_is_an_error(installed, monkeypatch, capsys):
    _write_profiles('[profiles.pi-local]\ncommand = ["pi"]\n')
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    error = _find(checks, "error", "hooks", "Pi extension")[0]
    assert error["remediation"] == "tongs-agents install"


def test_invalid_profiles_are_an_error(installed, monkeypatch, capsys):
    _write_profiles("[profiles.Bad]\ncommand = ['claude']\n")
    code, checks = _doctor(monkeypatch, capsys)
    assert code == 2
    assert _find(checks, "error", "profiles", "profiles.Bad")


def test_failing_module_check_is_reported(installed, monkeypatch, capsys):
    from types import ModuleType

    broken = ModuleType("tongs_agents.commands.broken")
    broken.NAME = "broken"
    broken.add_arguments = lambda parser: None
    broken.run = lambda args: 0

    def explode():
        raise RuntimeError("boom")

    broken.doctor_checks = explode
    real = doctor.commands.discover()
    monkeypatch.setattr(doctor.commands, "discover", lambda: [*real, broken])
    checks = doctor.collect(type("Args", (), {"config_dirs": []})())
    assert [check.name for check in checks if "boom" in check.message] == ["broken"]
    assert any(check.name == "hook-command" for check in checks)


def test_text_output_lists_fixes(home, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "doctor"])
    with pytest.raises(SystemExit) as raised:
        cli.main()
    output = capsys.readouterr().out
    assert raised.value.code == 1
    assert output.startswith(f"Session directory: {paths.sessions_dir()}")
    assert "[WARN] tongs-agents is not installed in" in output
    assert "  Fix: tongs-agents install --agent claude" in output
    assert "No claude-fleet-monitor leftovers found." in output


def test_unknown_level_is_rejected():
    with pytest.raises(ValueError):
        doctor.Check("x", "fatal", "nope")


def test_exit_code_is_the_worst_level():
    assert doctor.exit_code([]) == 0
    assert doctor.exit_code([doctor.Check("a", "info", "")]) == 0
    assert doctor.exit_code([doctor.Check("a", "warn", ""), doctor.Check("b", "ok", "")]) == 1
    assert doctor.exit_code([doctor.Check("a", "warn", ""), doctor.Check("b", "error", "")]) == 2


def _doctor_text(monkeypatch, capsys, *argv):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["tongs-agents", "doctor", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as raised:
        code = raised.code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_fix_names_must_be_known(home, monkeypatch, capsys):  # noqa: F811
    code, _, err = _doctor_text(monkeypatch, capsys, "--fix", "nope")
    assert code == 2
    assert "no fix named 'nope'; available: zellij" in err


def test_fix_cannot_print_json(home, monkeypatch, capsys):  # noqa: F811
    code, _, err = _doctor_text(monkeypatch, capsys, "--fix", "zellij", "--json")
    assert code == 2
    assert "--fix cannot be combined with --json" in err


def test_fix_runs_only_the_named_fix_once(home, monkeypatch, capsys):  # noqa: F811
    calls = []
    monkeypatch.setattr(doctor, "available_fixes", lambda: {"zellij": calls.append})
    monkeypatch.setattr(doctor, "collect", lambda args: pytest.fail("checks ran"))
    code, _, _ = _doctor_text(monkeypatch, capsys, "--fix", "zellij", "--fix", "zellij")
    assert code == 0
    assert len(calls) == 1


def test_fix_exit_status_is_the_fix_result(home, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(doctor, "available_fixes", lambda: {"zellij": lambda args: 2})
    code, _, _ = _doctor_text(monkeypatch, capsys, "--fix", "zellij")
    assert code == 2


def test_missing_zellij_check_offers_the_fix_as_info(home, monkeypatch, capsys):  # noqa: F811
    _, checks = _doctor(monkeypatch, capsys)
    managed = _find(checks, name="zellij-managed")
    assert managed and managed[0]["level"] == "info"
    assert managed[0]["remediation"] in (
        "tongs-agents doctor --fix zellij", "",
    )
