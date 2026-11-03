"""The [workspace], [ui] and [control] sections of the config file."""

import os
import stat

import pytest

from tongs_agents import app_config
from tongs_agents.app_config import (
    AppConfig,
    AppConfigError,
    ControlSettings,
    UISettings,
    WorkspaceSettings,
    load_app_config,
)

POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")


def write_config(tmp_path, text, mode=0o600):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "config.toml"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_the_config_file_is_the_profiles_file(tmp_path):
    assert app_config.config_path() == tmp_path / "config" / "config.toml"


def test_an_absent_file_gives_the_defaults():
    config = load_app_config()
    assert config == AppConfig()
    assert config.workspace == WorkspaceSettings(
        session="agents", default_profiles=(), tongs_tab=True, multiplexer="zellij",
    )
    assert config.ui == UISettings(
        refresh_interval=2.0, notify_level="all", theme="textual-dark", compact=False,
    )
    assert config.control == ControlSettings(mcp_enabled=False, send_delay_ms=150)


def test_every_section_is_parsed(tmp_path):
    write_config(tmp_path, """
[profiles.alpha]
command = ["sleep", "600"]

[workspace]
session = "work.1"
default_profiles = ["alpha", "beta"]
tongs_tab = false
multiplexer = "tmux"

[ui]
refresh_interval = 5
notify_level = "waiting"
theme = "nord"
compact = true

[control]
mcp_enabled = true
send_delay_ms = 0
""")
    config = load_app_config()
    assert config.workspace == WorkspaceSettings("work.1", ("alpha", "beta"), False, "tmux")
    assert config.ui == UISettings(5.0, "waiting", "nord", True)
    assert config.control == ControlSettings(True, 0)


def test_a_partial_section_keeps_the_other_defaults(tmp_path):
    write_config(tmp_path, '[ui]\ntheme = "gruvbox"\n')
    config = load_app_config()
    assert config.ui == UISettings(theme="gruvbox")
    assert config.workspace == WorkspaceSettings()


def test_unknown_sections_are_ignored(tmp_path):
    write_config(tmp_path, """
[dotfiles]
path = "~/example"

[future_section]
anything = [1, 2, 3]

[future_section.nested]
value = true

[ui]
compact = true
""")
    assert load_app_config().ui.compact is True


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[ui]\ncolour = 1\n", "ui.colour: unknown key"),
        ("[workspace]\nsession = \"a b\"\n", "workspace.session: must be 1 to 64"),
        ("[workspace]\nsession = \"-x\"\n", "workspace.session"),
        ("[workspace]\nsession = 3\n", "workspace.session"),
        ("[workspace]\ndefault_profiles = \"alpha\"\n",
         "workspace.default_profiles: must be a list"),
        ("[workspace]\ndefault_profiles = [\"Bad Name\"]\n",
         "workspace.default_profiles[0]: 'Bad Name' is not a profile name"),
        ("[workspace]\ntongs_tab = \"no\"\n", "workspace.tongs_tab: must be true or false"),
        ("[workspace]\nmultiplexer = \"screen\"\n",
         "workspace.multiplexer: must be one of zellij, tmux"),
        ("[ui]\nrefresh_interval = 0\n", "ui.refresh_interval: must be a number"),
        ("[ui]\nrefresh_interval = true\n", "ui.refresh_interval"),
        ("[ui]\nrefresh_interval = \"2\"\n", "ui.refresh_interval"),
        ("[ui]\nnotify_level = \"loud\"\n", "ui.notify_level: must be one of all, waiting, none"),
        ("[ui]\ntheme = \"\"\n", "ui.theme: must be a Textual theme name"),
        ("[ui]\ntheme = \"a/b\"\n", "ui.theme"),
        ("[ui]\ncompact = 1\n", "ui.compact: must be true or false"),
        ("[control]\nmcp_enabled = \"yes\"\n", "control.mcp_enabled: must be true or false"),
        ("[control]\nsend_delay_ms = 1.5\n", "control.send_delay_ms: must be a whole number"),
        ("[control]\nsend_delay_ms = -1\n", "control.send_delay_ms"),
        ("[control]\nsend_delay_ms = 60000\n", "control.send_delay_ms"),
        ("ui = 3\n", "ui: must be a table"),
        ("[ui\n", "invalid TOML"),
    ],
)
def test_validation_errors_name_the_file_and_key(tmp_path, text, message):
    path = write_config(tmp_path, text)
    with pytest.raises(AppConfigError) as raised:
        load_app_config()
    assert str(raised.value).startswith(f"{path}: ")
    assert message in str(raised.value)


@POSIX_MODES
def test_a_group_writable_file_is_refused(tmp_path):
    write_config(tmp_path, "[ui]\ncompact = true\n", mode=0o620)
    with pytest.raises(AppConfigError, match="writable by group or others"):
        load_app_config()


@POSIX_MODES
def test_a_symlinked_file_is_refused(tmp_path):
    real = tmp_path / "real.toml"
    real.write_text("[ui]\n")
    real.chmod(0o600)
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    (directory / "config.toml").symlink_to(real)
    with pytest.raises(AppConfigError, match="symlinked"):
        load_app_config()


@POSIX_MODES
def test_the_file_is_never_rewritten(tmp_path):
    text = "# keep me\n[ui]\ntheme = \"nord\"  # inline\n[unknown]\nx = 1\n"
    path = write_config(tmp_path, text)
    before = path.stat()
    load_app_config()
    after = path.stat()
    assert path.read_text() == text
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert stat.S_IMODE(after.st_mode) == 0o600


def test_an_explicit_path_is_read(tmp_path):
    path = tmp_path / "other.toml"
    path.write_text("[control]\nmcp_enabled = true\n")
    path.chmod(0o600)
    assert load_app_config(path).control.mcp_enabled is True
