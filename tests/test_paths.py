"""Runtime names: the state directory, its private layout and the tongs entry point."""

import io
import json
import os
import stat
import time
from importlib import metadata

import pytest

from tongs_agents import discovery, hook, paths
from tongs_agents.models import SCHEMA_VERSION
from tests.test_harness_contract import make_record


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")


@pytest.fixture
def no_override(monkeypatch, tmp_path):
    monkeypatch.delenv(paths.STATE_DIR_ENV)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def test_default_state_dir_follows_xdg(no_override, monkeypatch, tmp_path):
    expected = tmp_path / "home" / ".local" / "state" / "tongs-agents"
    assert paths.state_dir() == expected
    assert paths.sessions_dir() == expected / "sessions"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert paths.sessions_dir() == tmp_path / "xdg" / "tongs-agents" / "sessions"
    monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
    assert paths.state_dir() == expected


def test_override_replaces_the_state_dir(no_override, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "custom"))
    assert paths.sessions_dir() == tmp_path / "custom" / "sessions"


def test_paths_are_not_created_until_asked(tmp_path):
    assert not paths.sessions_dir().exists()
    assert paths.sessions_dir(create=True).is_dir()


@posix_only
def test_created_dirs_are_private(tmp_path):
    sessions = paths.sessions_dir(create=True)
    for directory in (paths.state_dir(), sessions):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert paths.is_private_dir(directory)


@posix_only
def test_existing_open_dirs_are_tightened(tmp_path):
    state = paths.state_dir()
    (state / "sessions").mkdir(parents=True)
    state.chmod(0o755)
    (state / "sessions").chmod(0o777)
    paths.sessions_dir(create=True)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "sessions").stat().st_mode) == 0o700


@posix_only
@pytest.mark.parametrize("linked", ["state", "sessions"])
def test_symlinked_dirs_are_refused(tmp_path, linked):
    target = tmp_path / "elsewhere"
    target.mkdir()
    state = paths.state_dir()
    if linked == "state":
        state.symlink_to(target, target_is_directory=True)
    else:
        state.mkdir()
        (state / "sessions").symlink_to(target, target_is_directory=True)
    with pytest.raises(paths.UnsafePathError, match="symlink"):
        paths.sessions_dir(create=True)
    assert not discovery.write_session_record(make_record())
    assert discovery.read_stored_sessions() == []
    assert list(target.iterdir()) == []


def test_fleet_dir_variable_is_ignored(tmp_path, monkeypatch):
    old = tmp_path / "old-fleet"
    old.mkdir()
    monkeypatch.setenv("FLEET_DIR", str(old))
    assert discovery.write_session_record(make_record())
    assert list(old.iterdir()) == []
    assert len(list(paths.sessions_dir().glob("fleet-v1-*.json"))) == 1


def test_hook_writes_to_the_sessions_dir(tmp_path, monkeypatch):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "pi.session-start",
        "session_id": "native",
        "instance_id": "instance-1",
        "cwd": str(tmp_path),
        "pid": os.getpid(),
        "sequence": 1,
        "status": "started",
        "ts": int(time.time()),
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "_capture_terminal_info",
                        lambda: {"terminal": "generic", "terminal_env": {}})
    assert hook.handle("fleet-event", "pi")
    records = list(paths.sessions_dir().glob("fleet-v1-*.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["harness_id"] == "pi"
    assert [record["session_id"] for record in discovery.read_session_records()] == ["native"]


def test_tongs_entry_point_is_agents():
    plugins = {
        entry.name: entry.value
        for entry in metadata.entry_points(group="tongs.plugins")
        if entry.value.startswith("tongs_agents.")
    }
    assert plugins == {"agents": "tongs_agents.tongs_plugin:FleetMonitorPlugin"}


@pytest.fixture
def xdg_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("XDG_CONFIG_HOME", "XDG_RUNTIME_DIR", paths.CONFIG_DIR_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "home"


def test_config_dir_follows_xdg_and_override(xdg_home, monkeypatch, tmp_path):
    assert paths.config_dir() == xdg_home / ".config" / "tongs-agents"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    assert paths.config_dir() == xdg_home / ".config" / "tongs-agents"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert paths.config_dir() == tmp_path / "xdg" / "tongs-agents"
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "custom"))
    assert paths.config_dir() == tmp_path / "custom"


def test_runtime_dir_falls_back_to_state(xdg_home, monkeypatch, tmp_path):
    assert paths.runtime_dir() == paths.state_dir() / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", "relative/run")
    assert paths.runtime_dir() == paths.state_dir() / "run"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    assert paths.runtime_dir() == tmp_path / "run-user" / "tongs-agents"


@posix_only
@pytest.mark.parametrize("xdg_runtime", [False, True])
def test_owned_dirs_are_created_private(xdg_home, monkeypatch, tmp_path, xdg_runtime):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    if xdg_runtime:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    assert not paths.config_dir().exists()
    assert not paths.runtime_dir().exists()
    for directory in (
        paths.config_dir(create=True),
        paths.runtime_dir(create=True),
        paths.launches_dir(create=True),
    ):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_symlinked_config_and_runtime_dirs_are_refused(xdg_home, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "config").symlink_to(target, target_is_directory=True)
    (tmp_path / "run-user").mkdir()
    (tmp_path / "run-user" / "tongs-agents").symlink_to(target, target_is_directory=True)
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run-user"))
    with pytest.raises(paths.UnsafePathError, match="symlink"):
        paths.config_dir(create=True)
    with pytest.raises(paths.UnsafePathError, match="symlink"):
        paths.runtime_dir(create=True)


@posix_only
def test_foreign_owned_dir_is_refused(tmp_path, monkeypatch):
    directory = tmp_path / "foreign"
    directory.mkdir()
    real_getuid = os.getuid
    monkeypatch.setattr(paths.os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(paths.UnsafePathError, match="another user"):
        paths.ensure_private_dir(directory)


@pytest.mark.parametrize("relation", ["equal", "inside"])
def test_owned_dirs_never_overlap_the_legacy_dir(xdg_home, monkeypatch, relation):
    legacy_dir = xdg_home / ".claude" / "fleet"
    candidate = {
        "equal": legacy_dir,
        "inside": legacy_dir / "state",
    }[relation]
    for variable, resolve in (
        (paths.STATE_DIR_ENV, paths.state_dir),
        (paths.CONFIG_DIR_ENV, paths.config_dir),
    ):
        monkeypatch.setenv(variable, str(candidate))
        assert resolve() == candidate
        with pytest.raises(paths.UnsafePathError, match="legacy"):
            resolve(create=True)
        monkeypatch.delenv(variable)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(legacy_dir))
    with pytest.raises(paths.UnsafePathError, match="legacy"):
        paths.runtime_dir(create=True)
    assert not legacy_dir.exists()


@pytest.mark.parametrize("ancestor", ["home", "claude"])
def test_state_dir_may_be_an_ancestor_of_the_legacy_dir(xdg_home, monkeypatch, ancestor):
    candidate = xdg_home if ancestor == "home" else xdg_home / ".claude"
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(candidate))
    assert paths.state_dir(create=True) == candidate
    assert paths.sessions_dir(create=True) == candidate / "sessions"
    assert paths.launches_dir(create=True) == candidate / "launches"
    assert not (xdg_home / ".claude" / "fleet").exists()
