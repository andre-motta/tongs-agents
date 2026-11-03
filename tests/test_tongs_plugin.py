"""Tests for the tongs plugin module."""

import asyncio
import importlib
import logging
import sys
from importlib.metadata import entry_points

import pytest

from tongs_agents import tongs_plugin
from tongs_agents.tongs_plugin import (
    DEFAULT_REFRESH_INTERVAL,
    NOT_READY_MESSAGE,
    PLUGIN_NAME,
    plugin_settings,
)


class FakeContext:
    def __init__(self, config=None, error=None):
        self.config_data = {} if config is None else config
        self.error = error
        self.pushed = []
        self.notifications = []
        self.requested = []

    def plugin_config(self, name):
        self.requested.append(name)
        if self.error:
            raise self.error
        return dict(self.config_data)

    def push_screen(self, screen):
        self.pushed.append(screen)

    def notify(self, message, severity="information"):
        self.notifications.append((message, severity))


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, (DEFAULT_REFRESH_INTERVAL, "all", 0)),
        ({"refresh_interval": 5, "notify_level": "waiting"}, (5, "waiting", 0)),
        ({"refresh_interval": 0.5, "notify_level": "none"}, (0.5, "none", 0)),
        ({"refresh_interval": 0}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"refresh_interval": True}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"refresh_interval": "5"}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"refresh_interval": float("nan")}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"refresh_interval": 10_000}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"notify_level": "loud"}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ({"notify_level": ["all"]}, (DEFAULT_REFRESH_INTERVAL, "all", 1)),
        ("not a table", (DEFAULT_REFRESH_INTERVAL, "all", 0)),
    ],
)
def test_plugin_settings_validation(config, expected):
    refresh, notify, problems = plugin_settings(config)
    assert (refresh, notify, len(problems)) == expected


def test_entry_point_is_registered_as_agents():
    names = {ep.name: ep.value for ep in entry_points(group="tongs.plugins")}
    assert names.get("agents") == "tongs_agents.tongs_plugin:FleetMonitorPlugin"
    assert "fleet-monitor" not in names


def test_not_ready_notifies_running_app(monkeypatch):
    notes = []

    class App:
        def notify(self, message, severity="information"):
            notes.append((message, severity))

    class Var:
        def get(self, default=None):
            return App()

    monkeypatch.setattr(tongs_plugin, "active_app", Var())
    tongs_plugin._notify_not_ready()
    assert notes == [(NOT_READY_MESSAGE, "warning")]


def test_not_ready_without_app_does_not_raise(monkeypatch):
    class Var:
        def get(self, default=None):
            return default

    monkeypatch.setattr(tongs_plugin, "active_app", Var())
    tongs_plugin._notify_not_ready()
    monkeypatch.setattr(tongs_plugin, "active_app", None)
    tongs_plugin._notify_not_ready()


class _BrokenVar:
    def get(self, default=None):
        raise TypeError("changed private API")


class _BrokenApp:
    def notify(self, message, severity="information"):
        raise RuntimeError("app is shutting down")


class _AppVar:
    def get(self, default=None):
        return _BrokenApp()


@pytest.mark.parametrize("var", [_BrokenVar(), _AppVar()])
def test_not_ready_never_raises_and_logs(monkeypatch, caplog, var):
    monkeypatch.setattr(tongs_plugin, "active_app", var)
    with caplog.at_level(logging.WARNING, logger=tongs_plugin.__name__):
        tongs_plugin._notify_not_ready()
    assert "Could not show the not-ready notice" in caplog.text


class TestPluginWithoutTongs:
    def test_no_crash_without_tongs(self):
        saved = sys.modules.get("tongs")
        saved_base = sys.modules.get("tongs.plugins")
        saved_base2 = sys.modules.get("tongs.plugins.base")
        try:
            sys.modules["tongs"] = None
            sys.modules["tongs.plugins"] = None
            sys.modules["tongs.plugins.base"] = None
            if "tongs_agents.tongs_plugin" in sys.modules:
                del sys.modules["tongs_agents.tongs_plugin"]
            mod = importlib.import_module("tongs_agents.tongs_plugin")
            assert not hasattr(mod, "FleetMonitorPlugin") or mod._PluginClass is None
        finally:
            if saved is not None:
                sys.modules["tongs"] = saved
            else:
                sys.modules.pop("tongs", None)
            if saved_base is not None:
                sys.modules["tongs.plugins"] = saved_base
            else:
                sys.modules.pop("tongs.plugins", None)
            if saved_base2 is not None:
                sys.modules["tongs.plugins.base"] = saved_base2
            else:
                sys.modules.pop("tongs.plugins.base", None)
            sys.modules.pop("tongs_agents.tongs_plugin", None)


class TestPluginWithTongs:
    @pytest.fixture(autouse=True)
    def _check_tongs(self):
        pytest.importorskip("tongs")

    def test_plugin_instantiation(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        assert plugin.name == "agents"

    def test_get_commands(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        cmds = plugin.get_commands()
        assert [cmd[0] for cmd in cmds] == ["Agents", "Open agent workspace"]

    def test_get_screens(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        screens = plugin.get_screens()
        assert "fleet" in screens
        from tongs_agents.views.fleet_screen import FleetScreen
        assert screens["fleet"] is FleetScreen

    def test_version(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        v = plugin.version
        assert isinstance(v, str)

    def test_context_starts_unset(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        assert plugin._ctx is None
        assert not hasattr(plugin, "_app")

    def test_command_before_ready_notifies_instead_of_raising(self, monkeypatch):
        module = importlib.import_module("tongs_agents.tongs_plugin")
        calls = []
        monkeypatch.setattr(module, "_notify_not_ready", lambda: calls.append(1))
        plugin = module.FleetMonitorPlugin()
        plugin.get_commands()[0][2]()
        assert calls == [1]

    def test_ready_reads_config_and_opens_screen_with_it(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        from tongs_agents.views.fleet_screen import FleetScreen
        plugin = FleetMonitorPlugin()
        ctx = FakeContext({"refresh_interval": 7, "notify_level": "waiting"})
        asyncio.run(plugin.on_app_ready(ctx))
        assert ctx.requested == [PLUGIN_NAME]
        assert ctx.notifications == []
        plugin.get_commands()[0][2]()
        [screen] = ctx.pushed
        assert isinstance(screen, FleetScreen)
        assert (screen.refresh_interval, screen.notify_level) == (7, "waiting")

    def test_invalid_config_warns_and_uses_defaults(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        ctx = FakeContext({"refresh_interval": -1, "notify_level": "loud"})
        asyncio.run(plugin.on_app_ready(ctx))
        assert (plugin.refresh_interval, plugin.notify_level) == (
            DEFAULT_REFRESH_INTERVAL, "all",
        )
        assert [severity for _, severity in ctx.notifications] == ["warning", "warning"]

    def test_unreadable_config_falls_back(self):
        from tongs_agents.tongs_plugin import FleetMonitorPlugin
        plugin = FleetMonitorPlugin()
        ctx = FakeContext(error=RuntimeError("boom"))
        asyncio.run(plugin.on_app_ready(ctx))
        assert plugin._ctx is ctx
        assert plugin.refresh_interval == DEFAULT_REFRESH_INTERVAL
