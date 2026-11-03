"""``tongs-agents monitor``: [ui] defaults, flag precedence, theme and monitor mode."""

import pytest

from tests.test_agents_panel import _all_status_feed, _settle
from tests.test_app_config import write_config
from tests.test_workspace_spawn import FakeMux
from tongs_agents import cli, tui
from tongs_agents.app_config import AppConfig, UISettings
from tongs_agents.multiplexers import PaneRef
from tongs_agents.palette import AgentCommands
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets.detail_panel import DetailPanel
from tongs_agents.widgets.session_table import SessionTable
from tongs_agents.widgets.shell import AppFooter, AppHeader


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


class Captured:
    """The apps whose run() was called instead of running them."""

    def __init__(self):
        self.apps = []


@pytest.fixture
def captured(monkeypatch):
    runs = Captured()
    monkeypatch.setattr(FleetMonitorApp, "run", lambda app, *a, **kw: runs.apps.append(app))
    monkeypatch.setattr(tui, "get_multiplexer", lambda name="zellij": FakeMux())
    return runs


def _monitor(*argv):
    parser, handlers = cli.build_parser()
    return handlers["monitor"](parser.parse_args(["monitor", *argv]))


def test_monitor_defaults_without_a_config_file(captured):
    assert _monitor() == 0
    app, = captured.apps
    assert (app.refresh_interval, app.notify_level, app.compact) == (2.0, "all", False)
    assert app.app_config.ui.theme == "textual-dark"
    assert (app.palette_multiplexer, app.palette_session) == (None, None)


def test_monitor_reads_the_ui_section(tmp_path, captured):
    write_config(tmp_path, """
[ui]
refresh_interval = 5
notify_level = "waiting"
theme = "nord"
compact = true
""")
    assert _monitor() == 0
    app, = captured.apps
    assert (app.refresh_interval, app.notify_level, app.compact) == (5.0, "waiting", True)
    assert app.app_config.ui.theme == "nord"


def test_flags_override_the_ui_section(tmp_path, captured):
    write_config(tmp_path, """
[ui]
refresh_interval = 5
notify_level = "waiting"
theme = "nord"
compact = true
""")
    assert _monitor("--refresh", "0.5", "--notify", "none", "--theme", "gruvbox",
                    "--no-compact") == 0
    app, = captured.apps
    assert (app.refresh_interval, app.notify_level, app.compact) == (0.5, "none", False)
    assert app.app_config.ui == UISettings(0.5, "none", "gruvbox", False)


def test_an_invalid_config_file_falls_back_to_defaults(tmp_path, captured, capsys):
    path = write_config(tmp_path, '[ui]\nnotify_level = "loud"\n')
    assert _monitor() == 0
    app, = captured.apps
    assert (app.refresh_interval, app.notify_level, app.compact) == (2.0, "all", False)
    err = capsys.readouterr().err
    assert err.startswith(f"tongs-agents monitor: {path}: ui.notify_level")
    assert "using the default settings" in err


def test_an_out_of_range_refresh_flag_is_refused(captured, capsys):
    assert _monitor("--refresh", "0") == 1
    assert captured.apps == []
    assert "--refresh must be from 0.5" in capsys.readouterr().err


def test_inside_zellij_the_monitor_gets_the_current_session(monkeypatch, captured):
    mux = FakeMux(here=PaneRef("zellij", "work", "3", None))
    monkeypatch.setattr(tui, "get_multiplexer", lambda name="zellij": mux)
    assert _monitor() == 0
    app, = captured.apps
    assert (app.palette_multiplexer, app.palette_session) == (mux, "work")


@pytest.mark.asyncio
async def test_the_ui_theme_is_applied_headless():
    app = FleetMonitorApp(
        refresh_interval=60, notify_level="none", feed=_all_status_feed(),
        config=AppConfig(ui=UISettings(theme="nord")),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert app.theme == "nord"
        assert app.theme_warning is None
        assert len(app.query(AppHeader)) == 1
        assert len(app.query(AppFooter)) == 1


@pytest.mark.asyncio
async def test_an_unknown_theme_keeps_the_default_and_warns():
    app = FleetMonitorApp(
        refresh_interval=60, notify_level="none", feed=_all_status_feed(),
        config=AppConfig(ui=UISettings(theme="no-such-theme")),
    )
    async with app.run_test(size=(100, 30)) as pilot:
        await _settle(app, pilot)
        assert app.theme == "textual-dark"
        assert "unknown theme 'no-such-theme'" in app.theme_warning
        assert any("no-such-theme" in str(n.message) for n in app._notifications)


def test_the_palette_provider_is_registered_beside_the_system_commands():
    assert AgentCommands in FleetMonitorApp.COMMANDS
    assert len(FleetMonitorApp.COMMANDS) >= 2


@pytest.mark.asyncio
async def test_monitor_mode_regression():
    """The plain monitor (no config, no Zellij) keeps its keys and rows."""
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=_all_status_feed())
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        assert app.theme == "textual-dark"
        table = app.query_one(SessionTable)
        assert table.row_count == 7
        await pilot.press("3")
        assert table.row_count == 1
        assert app._get_selected_session().repo == "billing"
        await pilot.press("d")
        assert app.query_one(DetailPanel).has_class("visible")
        await pilot.press("0", "n")
        assert app._get_selected_session().repo == "billing"
        titles = [command.title for command in app.palette_commands()]
        assert titles == [
            "Next needing attention", "Focus session", "Toggle detail", "Reload profiles",
        ]
        await pilot.press("q")
        assert not app.is_running
