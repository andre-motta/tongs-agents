"""The profile ``sandbox`` table (docs/work/openshell-sandbox.md, row 3)."""

import json
from pathlib import Path

import pytest

from tongs_agents import profiles
from tongs_agents.profiles import (
    DEFAULT_SANDBOX_WORKDIR,
    ProfileError,
    SandboxSpec,
    load_profiles,
    parse_profile,
)


def profile(sandbox, **extra):
    table = {"command": ["claude"], "cwd": "/home/user/project", "sandbox": sandbox}
    table.update(extra)
    return parse_profile("boxed", table)


def test_defaults_are_exec_mode_with_upload():
    spec = profile({}).sandbox
    assert spec == SandboxSpec()
    assert (spec.mode, spec.upload, spec.workdir) == ("exec", True, DEFAULT_SANDBOX_WORKDIR)
    assert spec.policy_advisor is False


def test_every_key_is_validated_and_kept():
    spec = profile({
        "name": "my-box",
        "image": "registry.example.com/org/claude-agent:1.2",
        "providers": ["anthropic", "github"],
        "policy": "/home/user/policy.yaml",
        "upload": True,
        "workdir": "/sandbox/project",
        "gateway": "local",
        "cpu": "2",
        "memory": "4Gi",
        "policy_advisor": True,
        "driver_config": {"podman": {"volumes": ["data"]}},
    }).sandbox
    assert spec.name == "my-box"
    assert spec.providers == ("anthropic", "github")
    assert spec.policy == Path("/home/user/policy.yaml")
    assert spec.workdir == "/sandbox/project"
    assert json.loads(spec.driver_config) == {"podman": {"volumes": ["data"]}}


@pytest.mark.parametrize("sandbox, message", [
    ({"bogus": 1}, "sandbox.bogus: unknown key"),
    ([], "sandbox: must be a table"),
    ({"name": "Bad_Name"}, "sandbox.name"),
    ({"name": "-x"}, "sandbox.name"),
    ({"image": "a b"}, "sandbox.image"),
    ({"providers": ["ok", "no spaces"]}, "sandbox.providers[1]"),
    ({"policy": "relative.yaml"}, "sandbox.policy"),
    ({"workdir": "relative"}, "sandbox.workdir"),
    ({"workdir": "/sandbox/../etc"}, "sandbox.workdir"),
    ({"workdir": "/"}, "sandbox.workdir"),
    ({"mode": "ssh"}, "sandbox.mode"),
    ({"upload": "yes"}, "sandbox.upload"),
    ({"gateway": "--global"}, "sandbox.gateway"),
    ({"cpu": "lots"}, "sandbox.cpu"),
    ({"memory": "4 GB"}, "sandbox.memory"),
    ({"policy_advisor": 1}, "sandbox.policy_advisor"),
    ({"driver_config": "{}"}, "sandbox.driver_config"),
    ({"mode": "attach", "upload": True}, "sandbox.upload: attach mode"),
])
def test_invalid_sandbox_tables_name_the_key(sandbox, message):
    with pytest.raises(ProfileError, match=message.replace("[", r"\[").replace("]", r"\]")):
        profile(sandbox)


def test_attach_mode_defaults_to_no_upload_and_needs_no_cwd():
    spec = parse_profile("boxed", {"command": ["claude"], "sandbox": {"mode": "attach"}}).sandbox
    assert (spec.mode, spec.upload) == ("attach", False)


@pytest.mark.parametrize("extra, message", [
    ({"env": {"API_TOKEN": "x"}}, "env: not passed into a sandbox"),
    ({"config_dir": "/home/user/.claude-alt"}, "config_dir: a sandboxed profile"),
    ({"mcp_control": True}, "mcp_control: not available inside a sandbox"),
])
def test_keys_that_cannot_apply_inside_a_sandbox_are_errors(extra, message):
    with pytest.raises(ProfileError, match=message):
        profile({}, **extra)


def test_upload_needs_a_cwd():
    with pytest.raises(ProfileError, match="cwd: needed to upload"):
        parse_profile("boxed", {"command": ["claude"], "sandbox": {}})


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home" / "user"
    (home / "project").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.mark.parametrize("cwd, message", [
    (lambda home: "~", "is your home folder"),
    (lambda home: str(home), "is your home folder"),
    (lambda home: str(home.parent), "holds your home folder"),
    (lambda home: "/", "is the filesystem root"),
])
def test_upload_refuses_home_root_and_folders_holding_home(fake_home, cwd, message):
    with pytest.raises(ProfileError, match=rf"boxed\.cwd: .*{message}"):
        profile({}, cwd=cwd(fake_home))
    with pytest.raises(ProfileError, match=rf"boxed\.cwd: .*{message}"):
        profile({"upload": True}, cwd=cwd(fake_home))


def test_upload_accepts_a_project_inside_home(fake_home):
    spec = profile({}, cwd="~/project").sandbox
    assert spec.upload is True


def test_no_upload_may_use_any_cwd(fake_home):
    assert profile({"upload": False}, cwd="~").sandbox.upload is False


def test_unsandboxed_profiles_may_use_home_as_cwd(fake_home):
    assert parse_profile("plain", {"command": ["claude"], "cwd": "~"}).cwd == fake_home


def test_unsandboxed_profiles_are_unchanged():
    assert parse_profile("plain", {"command": ["claude"]}).sandbox is None


def test_config_file_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        "[profiles.boxed]\n"
        'command = ["codex"]\n'
        'cwd = "/home/user/project"\n'
        "[profiles.boxed.sandbox]\n"
        'providers = ["openai"]\n'
        "[profiles.boxed.sandbox.driver_config.kubernetes.pod]\n"
        'node_selector = { pool = "gpu" }\n'
    )
    path.chmod(0o600)
    spec = load_profiles(path)["boxed"].sandbox
    assert spec.providers == ("openai",)
    assert json.loads(spec.driver_config) == {
        "kubernetes": {"pod": {"node_selector": {"pool": "gpu"}}}
    }


def test_hidden_home_folders_are_refused_for_upload(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    assert "hidden folder" in profiles.unsafe_folder(home / ".ssh", home)
    assert profiles.unsafe_folder(home / "project", home) is None


def test_the_account_home_is_protected_when_home_is_overridden(monkeypatch, tmp_path):
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.setattr(profiles, "_account_home", lambda: account)
    monkeypatch.setenv("HOME", str(tmp_path / "elsewhere"))
    assert "home folder" in profiles.unsafe_folder(account)
