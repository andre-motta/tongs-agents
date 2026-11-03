"""Profiles: the [profiles] section of config.toml and its validation matrix."""

import os
from pathlib import Path

import pytest

from tongs_agents import paths, profiles
from tongs_agents.profiles import Profile, ProfileError, load_profiles


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    directory = tmp_path / "config"
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(directory))
    return directory


def write_config(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_profiles_path_is_in_the_config_dir(config_dir):
    assert profiles.profiles_path() == config_dir / "config.toml"


def test_missing_file_or_section_yields_no_profiles(config_dir):
    assert load_profiles() == {}
    write_config(config_dir, '[workspace]\nsession = "agents"\n')
    assert load_profiles() == {}


def test_full_profile_and_other_sections_ignored(config_dir, tmp_path):
    write_config(config_dir, f"""
[workspace]
session = "agents"
anything = {{ nested = [1, 2] }}

[ui]
theme = "dark"

[profiles.claude-alt]
command = ["claude", "--verbose"]
config_dir = "{tmp_path / 'claude-alt'}"
model_args = ["--model", "opus"]
effort_args = ["--effort", "high"]
cwd = "{tmp_path / 'src'}"
mcp_control = true
env = {{ EXAMPLE_FLAG = "1" }}

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[profiles.pi]
command = ["/opt/example/bin/pi"]

[profiles.wrapped]
command = ["claude-sandbox", "--project", "example"]
harness = "claude"

[profiles.shell-tool]
command = ["example-agent"]
""")
    loaded = load_profiles()
    assert list(loaded) == ["claude-alt", "codex-review", "pi", "wrapped", "shell-tool"]
    alt = loaded["claude-alt"]
    assert alt == Profile(
        name="claude-alt",
        harness="claude",
        argv=("claude", "--verbose"),
        env={"EXAMPLE_FLAG": "1"},
        model_args=("--model", "opus"),
        effort_args=("--effort", "high"),
        config_dir=tmp_path / "claude-alt",
        cwd=tmp_path / "src",
        mcp_control=True,
    )
    with pytest.raises(TypeError):
        alt.env["OTHER"] = "x"
    assert loaded["codex-review"].harness == "codex"
    assert loaded["codex-review"].mcp_control is False
    assert loaded["pi"].harness == "pi"
    assert loaded["wrapped"].harness == "claude"
    assert loaded["shell-tool"].harness == "other"


def test_command_is_never_shell_parsed(config_dir):
    write_config(config_dir, """
[profiles.literal]
command = ["example-agent --flag", "$(touch pwned)", "a;b", ""]
""")
    assert load_profiles()["literal"].argv == (
        "example-agent --flag", "$(touch pwned)", "a;b", ""
    )


def test_home_relative_paths_expand(config_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    write_config(config_dir, """
[profiles.claude-alt]
command = ["claude"]
config_dir = "~/.claude-alt"
cwd = "~/src/example"
""")
    profile = load_profiles()["claude-alt"]
    assert profile.config_dir == tmp_path / "home" / ".claude-alt"
    assert profile.cwd == tmp_path / "home" / "src" / "example"


def test_explicit_path_argument(tmp_path):
    path = write_config(tmp_path / "elsewhere", '[profiles.one]\ncommand = ["x"]\n')
    assert list(load_profiles(path)) == ["one"]


BIG = "x" * 4096


@pytest.mark.parametrize("body, key", [
    ('[profiles.Bad]\ncommand = ["x"]', "profiles.Bad"),
    ('[profiles."-dash"]\ncommand = ["x"]', "profiles.-dash"),
    (f'[profiles.{"a" * 33}]\ncommand = ["x"]', "profiles.aaa"),
    ('[profiles.p]\nharness = "claude"', "profiles.p.command"),
    ('[profiles.p]\ncommand = []', "profiles.p.command"),
    ('[profiles.p]\ncommand = [""]', "profiles.p.command"),
    ('[profiles.p]\ncommand = "claude --x"', "profiles.p.command"),
    ('[profiles.p]\ncommand = ["claude", 3]', "profiles.p.command"),
    ('[profiles.p]\ncommand = ["claude", "a\\u0000b"]', "profiles.p.command[1]"),
    ('[profiles.p]\ncommand = ["x"]\nharness = "gemini"', "profiles.p.harness"),
    ('[profiles.p]\ncommand = ["x"]\nenv = { lower = "1" }', "profiles.p.env.lower"),
    ('[profiles.p]\ncommand = ["x"]\nenv = { "1X" = "1" }', "profiles.p.env.1X"),
    ('[profiles.p]\ncommand = ["x"]\nenv = { TONGS_AGENTS_LAUNCH_ID = "a" }',
     "profiles.p.env.TONGS_AGENTS_LAUNCH_ID"),
    ('[profiles.p]\ncommand = ["x"]\nenv = { A = 1 }', "profiles.p.env.A"),
    ('[profiles.p]\ncommand = ["x"]\nenv = { A = "a\\u0000" }', "profiles.p.env.A"),
    (f'[profiles.p]\ncommand = ["x"]\nenv = {{ A = "{BIG}" }}', "profiles.p.env.A"),
    ('[profiles.p]\ncommand = ["x"]\nenv = ["A=1"]', "profiles.p.env"),
    ('[profiles.p]\ncommand = ["x"]\nmodel_args = "--model x"', "profiles.p.model_args"),
    ('[profiles.p]\ncommand = ["x"]\neffort_args = [1]', "profiles.p.effort_args"),
    ('[profiles.p]\ncommand = ["claude"]\nconfig_dir = "relative/dir"',
     "profiles.p.config_dir"),
    ('[profiles.p]\ncommand = ["claude"]\nconfig_dir = ""', "profiles.p.config_dir"),
    ('[profiles.p]\ncommand = ["example"]\nconfig_dir = "/abs/dir"',
     "profiles.p.config_dir"),
    ('[profiles.p]\ncommand = ["claude"]\nconfig_dir = "/abs"\n'
     'env = { CLAUDE_CONFIG_DIR = "/other" }', "profiles.p.config_dir"),
    ('[profiles.p]\ncommand = ["x"]\ncwd = "relative"', "profiles.p.cwd"),
    ('[profiles.p]\ncommand = ["x"]\nmcp_control = "yes"', "profiles.p.mcp_control"),
    ('[profiles.p]\ncommand = ["x"]\nargs = ["y"]', "profiles.p.args"),
    ('profiles = ["p"]', "profiles"),
    ("[profiles]\np = 1", "profiles.p"),
])
def test_invalid_profiles_name_the_key(config_dir, body, key):
    path = write_config(config_dir, body + "\n")
    with pytest.raises(ProfileError) as raised:
        load_profiles()
    message = str(raised.value)
    assert message.startswith(f"{path}: ")
    assert key in message


def test_env_value_just_under_the_limit_is_accepted(config_dir):
    write_config(config_dir, f'[profiles.p]\ncommand = ["x"]\nenv = {{ A = "{BIG[1:]}" }}\n')
    assert len(load_profiles()["p"].env["A"]) == 4095


def test_toml_syntax_error_is_clear(config_dir):
    path = write_config(config_dir, '[profiles.p]\ncommand = ["x"]\ncwd = /bad\n')
    with pytest.raises(ProfileError, match=r"invalid TOML.*line"):
        load_profiles()
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ProfileError, match="UTF-8"):
        load_profiles()


def test_oversized_file_is_refused(config_dir):
    write_config(config_dir, "# " + "x" * (64 * 1024) + "\n")
    with pytest.raises(ProfileError, match="64 KiB"):
        load_profiles()


def test_exactly_64_kib_is_accepted(config_dir):
    body = '[profiles.p]\ncommand = ["x"]\n'
    padding = 64 * 1024 - len(body) - 1
    write_config(config_dir, body + "#" + "x" * (padding - 1) + "\n")
    assert (config_dir / "config.toml").stat().st_size == 64 * 1024
    assert list(load_profiles()) == ["p"]


@posix_only
def test_symlinked_config_file_is_refused(config_dir, tmp_path):
    real = write_config(tmp_path / "real", '[profiles.p]\ncommand = ["x"]\n')
    config_dir.mkdir()
    (config_dir / "config.toml").symlink_to(real)
    with pytest.raises(ProfileError, match="symlink"):
        load_profiles()


@posix_only
def test_directory_in_place_of_config_is_refused(config_dir):
    (config_dir / "config.toml").mkdir(parents=True)
    with pytest.raises(ProfileError, match="regular file"):
        load_profiles()


@posix_only
@pytest.mark.parametrize("mode", [0o620, 0o602, 0o666])
def test_group_or_world_writable_config_is_refused(config_dir, mode):
    path = write_config(config_dir, '[profiles.p]\ncommand = ["x"]\n')
    path.chmod(mode)
    with pytest.raises(ProfileError, match="writable by group or others"):
        load_profiles()


@posix_only
def test_readable_config_modes_are_accepted(config_dir):
    path = write_config(config_dir, '[profiles.p]\ncommand = ["x"]\n')
    path.chmod(0o644)
    assert list(load_profiles()) == ["p"]


@posix_only
def test_foreign_owned_config_is_refused(config_dir, monkeypatch):
    write_config(config_dir, '[profiles.p]\ncommand = ["x"]\n')
    real_getuid = os.getuid
    monkeypatch.setattr(profiles.os, "getuid", lambda: real_getuid() + 1)
    with pytest.raises(ProfileError, match="another user"):
        load_profiles()
