import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def completed(code=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_version_probe_isolated_from_user_config(monkeypatch):
    from tongs_agents import pi_install

    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs["env"])
        assert command == ["/usr/bin/pi", "--version"]
        return completed(stdout="0.85.0\n")

    monkeypatch.setattr(pi_install.subprocess, "run", fake_run)
    monkeypatch.setenv("HOME", "/real/home")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/real/pi")
    assert pi_install.detect_pi_version("/usr/bin/pi") == (0, 85, 0)
    assert seen["HOME"] != "/real/home"
    assert seen["PI_CODING_AGENT_DIR"] != "/real/pi"
    assert seen["PI_OFFLINE"] == "1"
    assert seen["PI_TELEMETRY"] == "0"


def test_version_probe_rejects_unknown_output(monkeypatch):
    from tongs_agents import pi_install

    monkeypatch.setattr(
        pi_install.subprocess,
        "run",
        lambda *args, **kwargs: completed(stdout="development build\n"),
    )
    assert pi_install.detect_pi_version("/usr/bin/pi") is None


def test_version_probe_does_not_claim_prerelease_support(monkeypatch):
    from tongs_agents import pi_install

    monkeypatch.setattr(
        pi_install.subprocess,
        "run",
        lambda *args, **kwargs: completed(stdout="0.84.4-alpha.1\n"),
    )
    assert pi_install.detect_pi_version("/usr/bin/pi") is None


def test_old_pi_is_rejected_before_configuration(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 84, 3))
    monkeypatch.setattr(
        pi_install, "_run_pi_package",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not install")),
    )
    assert not pi_install.install_pi("/usr/bin/pi", "/usr/bin/hook")
    assert not config_dir.exists()


def test_pi_install_handles_paths_and_preserves_owned_history(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "Pi config"
    config_dir.mkdir()
    extension = tmp_path / "new extension Ω"
    extension.mkdir()
    hook = tmp_path / "venv with spaces" / "tongs-agents-hook"
    hook.parent.mkdir()
    hook.write_text("")
    old = tmp_path / "old extension"
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/old/hook",
        "extension_path": str(old),
        "managed_paths": [str(old)],
    }))
    calls = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 84, 4))
    monkeypatch.setattr(pi_install, "pi_extension_path", lambda: extension)
    monkeypatch.setattr(
        pi_install,
        "_run_pi_package",
        lambda command, operation, path, env=None: calls.append((operation, path)) or completed(),
    )

    assert pi_install.install_pi("/path/pi", str(hook))
    assert calls == [("install", extension), ("remove", old)]
    config = json.loads(config_path.read_text())
    assert config["hook_path"] == str(hook)
    assert config["extension_path"] == str(extension)
    assert config["managed_paths"] == [str(extension)]


def test_failed_upgrade_removal_is_retained_for_retry(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    extension = tmp_path / "new"
    extension.mkdir()
    hook = tmp_path / "hook"
    hook.write_text("")
    old = tmp_path / "old"
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/old/hook",
        "extension_path": str(old),
        "managed_paths": [str(old)],
    }))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    monkeypatch.setattr(pi_install, "pi_extension_path", lambda: extension)

    def run(command, operation, path, env=None):
        if operation == "remove":
            return completed(1, stderr="busy")
        return completed()

    monkeypatch.setattr(pi_install, "_run_pi_package", run)
    assert pi_install.install_pi("/path/pi", str(hook))
    config = json.loads(config_path.read_text())
    assert config["managed_paths"] == [str(extension), str(old)]

    calls = []
    monkeypatch.setattr(
        pi_install,
        "_run_pi_package",
        lambda command, operation, path, env=None: calls.append((operation, path)) or completed(),
    )
    assert pi_install.install_pi("/path/pi", str(hook))
    assert calls == [("install", extension), ("remove", old)]
    config = json.loads(config_path.read_text())
    assert config["managed_paths"] == [str(extension)]


def test_failed_install_restores_previous_config(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    extension = tmp_path / "new"
    extension.mkdir()
    hook = tmp_path / "hook"
    hook.write_text("")
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    previous = {
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/old/hook",
        "extension_path": "/old/extension",
        "managed_paths": ["/old/extension"],
    }
    config_path.write_text(json.dumps(previous))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    monkeypatch.setattr(pi_install, "pi_extension_path", lambda: extension)
    monkeypatch.setattr(
        pi_install, "_run_pi_package",
        lambda *args: completed(1, stderr="install failed"),
    )
    assert not pi_install.install_pi("/path/pi", str(hook))
    assert json.loads(config_path.read_text()) == previous


def test_unmanaged_pi_config_is_never_replaced(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    before = '{"owner":"someone-else"}\n'
    config_path.write_text(before)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    assert not pi_install.install_pi("/path/pi", "/path/hook")
    assert config_path.read_text() == before


def test_broken_symlink_pi_config_is_never_replaced(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    try:
        config_path.symlink_to(tmp_path / "missing")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    assert not pi_install.install_pi("/path/pi", "/path/hook")
    assert config_path.is_symlink()


@pytest.mark.parametrize(
    "managed_path",
    ["relative/path", "", "npm:other-package", "/" + "x" * 5000],
)
def test_malformed_owned_paths_are_never_removed(
    tmp_path, monkeypatch, managed_path
):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/owned/hook",
        "extension_path": "/owned/extension",
        "managed_paths": ["/owned/extension", managed_path],
    }))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(
        pi_install, "_run_pi_package",
        lambda *args: (_ for _ in ()).throw(AssertionError("must not remove")),
    )
    assert not pi_install.uninstall_pi("/path/pi")
    assert config_path.exists()


def test_uninstall_removes_only_owned_paths(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/hook",
        "extension_path": "/current",
        "managed_paths": ["/current", "/old"],
    }))
    calls = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(
        pi_install,
        "_run_pi_package",
        lambda command, operation, path, env=None: calls.append((operation, str(path)))
        or completed(1, stderr="No matching package found"),
    )
    assert pi_install.uninstall_pi("/path/pi")
    assert calls == [("remove", "/current"), ("remove", "/old")]
    assert not config_path.exists()


def test_partial_uninstall_preserves_valid_retry_state(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / pi_install.PI_CONFIG_NAME
    config_path.write_text(json.dumps({
        "schema_version": 1,
        "owner": pi_install.PI_CONFIG_OWNER,
        "hook_path": "/hook",
        "extension_path": "/current",
        "managed_paths": ["/current", "/old"],
    }))
    attempts = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))

    def first_attempt(command, operation, path, env=None):
        attempts.append(str(path))
        return completed(1, stderr="busy") if str(path) == "/old" else completed()

    monkeypatch.setattr(pi_install, "_run_pi_package", first_attempt)
    assert not pi_install.uninstall_pi("/path/pi")
    assert attempts == ["/current", "/old"]
    saved = pi_install._read_owned_config(config_path)
    assert saved["managed_paths"] == ["/current", "/old"]

    attempts.clear()
    monkeypatch.setattr(
        pi_install,
        "_run_pi_package",
        lambda command, operation, path, env=None: attempts.append(str(path)) or completed(),
    )
    assert pi_install.uninstall_pi("/path/pi")
    assert attempts == ["/current", "/old"]
    assert not config_path.exists()


def test_repeated_uninstall_succeeds_when_pi_is_no_longer_installed(
    tmp_path, monkeypatch
):
    from tongs_agents import pi_install

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "config"))
    assert pi_install.uninstall_pi(None)


def test_pi_asset_is_packaged_and_dependency_free():
    from tongs_agents.pi_install import pi_extension_path

    package = pi_extension_path()
    manifest = json.loads((package / "package.json").read_text())
    assert manifest["pi"]["extensions"] == ["./extensions/fleet-monitor.js"]
    assert "dependencies" not in manifest
    assert (package / "extensions" / "fleet-monitor.js").is_file()


def test_cli_pi_selection_does_not_touch_other_agents(settings_file, monkeypatch):
    from tongs_agents import cli

    hook = Path(settings_file).parent / "tongs-agents-hook"
    hook.write_text("")
    monkeypatch.setattr(cli.shutil, "which", lambda name: str(hook) if name == "tongs-agents-hook" else "/usr/bin/pi")
    monkeypatch.setattr(cli, "install_pi", lambda pi, hook_path, env=None: True)
    cli.cmd_install(SimpleNamespace(agent="pi"))
    assert json.loads(settings_file.read_text()) == {}
    assert not cli.CODEX_HOOKS_FILE.exists()


def test_cli_pi_uninstall_instructs_running_sessions_to_restart(
    settings_file, monkeypatch, capsys
):
    from tongs_agents import cli

    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/pi")
    monkeypatch.setattr(cli, "uninstall_pi", lambda command, env=None: True)

    cli.cmd_uninstall(SimpleNamespace(agent="pi", keep_data=True))

    assert (
        "Restart running Pi sessions to finish removing monitoring."
        in capsys.readouterr().out
    )


def test_cli_default_selection_remains_claude_and_codex(settings_file, monkeypatch):
    from tongs_agents import cli

    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        cli, "install_pi",
        lambda *args: (_ for _ in ()).throw(AssertionError("Pi remains opt-in")),
    )
    cli.cmd_install(None)
    assert "hooks" in json.loads(settings_file.read_text())
    assert cli.CODEX_HOOKS_FILE.exists()


def _legacy_pi(config_dir, *paths, owner=None):
    from tongs_agents import legacy

    path = config_dir / legacy.LEGACY_PI_CONFIG_NAME
    path.write_text(json.dumps({
        "schema_version": 1,
        "owner": owner or legacy.LEGACY_PI_CONFIG_OWNER,
        "hook_path": "/old/venv/bin/hook",
        "extension_path": str(paths[0]),
        "managed_paths": [str(item) for item in paths],
    }))
    return path


@pytest.fixture
def pi_env(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    extension = tmp_path / "current"
    extension.mkdir()
    hook = tmp_path / "hook"
    hook.write_text("")
    calls = []
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(config_dir))
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    monkeypatch.setattr(pi_install, "pi_extension_path", lambda: extension)
    monkeypatch.setattr(
        pi_install,
        "_run_pi_package",
        lambda command, operation, path, env=None: calls.append((operation, str(path))) or completed(),
    )
    return SimpleNamespace(
        config_dir=config_dir, extension=extension, hook=hook, calls=calls,
        config=config_dir / pi_install.PI_CONFIG_NAME,
    )


def test_install_removes_legacy_pi_extension(pi_env):
    from tongs_agents import pi_install

    legacy_config = _legacy_pi(pi_env.config_dir, "/old/site/pi_extension", pi_env.extension)
    assert pi_install.install_pi("/path/pi", str(pi_env.hook))
    assert pi_env.calls == [
        ("install", str(pi_env.extension)),
        ("remove", "/old/site/pi_extension"),
    ]
    assert not legacy_config.exists()
    assert pi_install._read_owned_config(pi_env.config)["owner"] == "tongs-agents"


def test_failed_legacy_pi_removal_is_kept_for_retry(pi_env, monkeypatch, capsys):
    from tongs_agents import pi_install

    legacy_config = _legacy_pi(pi_env.config_dir, "/old/site/pi_extension")
    monkeypatch.setattr(
        pi_install, "_run_pi_package",
        lambda command, operation, path, env=None: completed(1 if operation == "remove" else 0, stderr="busy"),
    )
    assert not pi_install.install_pi("/path/pi", str(pi_env.hook))
    assert legacy_config.exists()
    assert pi_env.config.exists()
    assert "kept" in capsys.readouterr().out


def test_unrecognized_legacy_pi_config_is_left_alone(pi_env, capsys):
    from tongs_agents import pi_install

    legacy_config = _legacy_pi(pi_env.config_dir, "/old/ext", owner="someone-else")
    before = legacy_config.read_bytes()
    assert pi_install.install_pi("/path/pi", str(pi_env.hook))
    assert legacy_config.read_bytes() == before
    assert pi_env.calls == [("install", str(pi_env.extension))]
    assert "Kept unrecognized legacy Pi config" in capsys.readouterr().out


def test_uninstall_removes_legacy_and_current_pi_extensions(pi_env):
    from tongs_agents import pi_install

    assert pi_install.install_pi("/path/pi", str(pi_env.hook))
    legacy_config = _legacy_pi(pi_env.config_dir, "/old/ext")
    pi_env.calls.clear()
    assert pi_install.uninstall_pi("/path/pi")
    assert pi_env.calls == [("remove", "/old/ext"), ("remove", str(pi_env.extension))]
    assert not legacy_config.exists()
    assert not pi_env.config.exists()


def test_uninstall_without_pi_keeps_legacy_pi_config(pi_env):
    from tongs_agents import pi_install

    legacy_config = _legacy_pi(pi_env.config_dir, "/old/ext")
    assert not pi_install.uninstall_pi(None)
    assert legacy_config.exists()


def test_python_and_javascript_markers_change_in_lockstep():
    from tongs_agents import pi_install

    package = pi_install.pi_extension_path()
    source = (package / "extensions" / "fleet-monitor.js").read_text()
    assert f'const CONFIG_OWNER = "{pi_install.PI_CONFIG_OWNER}";' in source
    assert f'const CONFIG_NAME = "{pi_install.PI_CONFIG_NAME}";' in source
    assert 'Symbol.for("tongs-agents.pi.runtime.v1")' in source
    assert pi_install.PI_CONFIG_OWNER == "tongs-agents"
    assert pi_install.PI_CONFIG_NAME == "tongs-agents.json"
    assert json.loads((package / "package.json").read_text())["name"] == "tongs-agents-pi"


def test_install_and_uninstall_follow_the_env_agent_dir(pi_env, tmp_path, monkeypatch):
    from tongs_agents import legacy, pi_install

    other = tmp_path / "pi-alt"
    seen = []
    monkeypatch.setattr(
        pi_install, "_run_pi_package",
        lambda command, operation, path, env=None: seen.append(
            (operation, env["PI_CODING_AGENT_DIR"])) or completed(),
    )
    _legacy_pi(pi_env.config_dir, "/old/ext")
    env = pi_install.pi_environment(other)
    assert pi_install.install_pi("/path/pi", str(pi_env.hook), env)
    assert (other / pi_install.PI_CONFIG_NAME).exists()
    assert not pi_env.config.exists()
    assert (pi_env.config_dir / legacy.LEGACY_PI_CONFIG_NAME).exists()
    assert seen == [("install", str(other))]

    assert pi_install.uninstall_pi("/path/pi", env)
    assert not (other / pi_install.PI_CONFIG_NAME).exists()
    assert seen[-1] == ("remove", str(other))


def test_pi_config_dir_reads_the_given_env(tmp_path, monkeypatch):
    from tongs_agents import pi_install

    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "process"))
    assert pi_install.pi_config_dir() == tmp_path / "process"
    assert pi_install.pi_config_dir({"PI_CODING_AGENT_DIR": str(tmp_path / "x")}) == tmp_path / "x"
    env = pi_install.pi_environment(tmp_path / "y")
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "y")
    assert pi_install.pi_config_dir() == tmp_path / "process"
