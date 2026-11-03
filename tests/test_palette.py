"""The command palette of the monitor and the sidebar (mocked multiplexer)."""

import sys
import threading

import pytest

from tests.test_agents_panel import _all_status_feed, _settle
from tests.test_workspace_spawn import FakeMux, runtime_dir, write_profiles  # noqa: F401
from tongs_agents import launches, palette
from tongs_agents.app_config import AppConfig, WorkspaceSettings
from tongs_agents.commands.sidebar import SidebarApp
from tongs_agents.multiplexers import PaneRef
from tongs_agents.palette import (
    ADD_AGENT,
    FOCUS_SESSION,
    NEXT_ATTENTION,
    OPEN_TONGS,
    RELOAD_PROFILES,
    TOGGLE_DETAIL,
    AgentCommands,
)
from tongs_agents.sessions_feed import WorkspaceFeed
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.widgets.detail_panel import DetailPanel

TONGS = "/opt/tongs/bin/tongs"
SESSION = "work"
ACTIONS = [NEXT_ATTENTION, FOCUS_SESSION, TOGGLE_DETAIL, RELOAD_PROFILES]


@pytest.fixture
def tools(monkeypatch):
    found = {"tongs": TONGS}
    monkeypatch.setattr(palette.shutil, "which", lambda name: found.get(name))
    return found


@pytest.fixture
def profiles(tmp_path):
    return write_profiles(tmp_path, ["alpha", "beta", "gamma"])


def _monitor(mux=None, session=SESSION, **workspace):
    config = AppConfig(workspace=WorkspaceSettings(**workspace))
    return FleetMonitorApp(
        refresh_interval=60, notify_level="none", feed=_all_status_feed(),
        config=config, multiplexer=mux, session=session if mux is not None else None,
    )


def _titles(app):
    return [command.title for command in app.palette_commands()]


def _command(app, title):
    return next(command for command in app.palette_commands() if command.title == title)


def test_both_apps_register_the_provider():
    for app_class in (FleetMonitorApp, SidebarApp):
        assert AgentCommands in app_class.COMMANDS
        assert all(command in app_class.COMMANDS for command in palette.App.COMMANDS)


def test_commands_inside_a_session(profiles, tools):
    app = _monitor(FakeMux(), default_profiles=("gamma", "missing", "alpha"))
    assert _titles(app) == [
        ADD_AGENT, "New agent: gamma", "New agent: alpha", "New agent: beta", OPEN_TONGS,
        *ACTIONS,
    ]


def test_outside_a_session_only_panel_commands_are_offered(profiles, tools):
    assert _titles(_monitor(None)) == ACTIONS


def test_open_tongs_needs_tongs_on_path_and_the_tongs_tab(profiles, tools):
    assert OPEN_TONGS in _titles(_monitor(FakeMux()))
    assert OPEN_TONGS not in _titles(_monitor(FakeMux(), tongs_tab=False))
    tools.clear()
    assert OPEN_TONGS not in _titles(_monitor(FakeMux()))


@pytest.mark.asyncio
async def test_new_agent_spawns_the_launcher_into_the_current_session(profiles, tools):
    mux = FakeMux()
    app = _monitor(mux)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        _command(app, "New agent: beta").callback()
        await _settle(app, pilot)
    record, = launches.list_records(SESSION)
    assert record.profile == "beta"
    assert mux.spawned == [{
        "session": SESSION,
        "argv": [sys.executable, "-m", "tongs_agents.launch", "beta",
                 "--launch-id", record.launch_id],
        "name": "beta",
        "focus": False,
        "cwd": None,
    }]


@pytest.mark.asyncio
async def test_open_tongs_spawns_tongs_in_a_focused_pane(profiles, tools):
    mux = FakeMux()
    app = _monitor(mux)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        _command(app, OPEN_TONGS).callback()
        await _settle(app, pilot)
    assert mux.spawned == [{
        "session": SESSION, "argv": [TONGS], "name": "tongs", "focus": True, "cwd": None,
    }]
    assert launches.list_records() == []


@pytest.mark.asyncio
async def test_a_failed_spawn_is_a_notification_and_leaves_no_record(profiles, tools):
    mux = FakeMux(fail_spawn={"alpha"})
    app = _monitor(mux)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        _command(app, "New agent: alpha").callback()
        await _settle(app, pilot)
        messages = [(n.title, str(n.message), n.severity) for n in app._notifications]
    assert ("Spawn failed", "alpha: cannot spawn alpha", "error") in messages
    assert launches.list_records() == []


def test_reload_profiles_rereads_the_file(tmp_path, profiles, tools, monkeypatch):
    app = _monitor(FakeMux())
    notes = []
    monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append((message, kw)))
    assert "New agent: delta" not in _titles(app)
    write_profiles(tmp_path, ["alpha", "delta"])
    assert "New agent: delta" not in _titles(app)
    app.palette_reload_profiles()
    assert notes[-1][0] == "Loaded 2 profiles"
    assert [t for t in _titles(app) if t.startswith("New agent")] == [
        "New agent: alpha", "New agent: delta",
    ]
    profiles.write_text("[profiles.bad]\n")
    app.palette_reload_profiles()
    assert "command: missing" in notes[-1][0]
    assert notes[-1][1]["severity"] == "error"
    assert not any(t.startswith("New agent") for t in _titles(app))
    assert "Last read failed" in _command(app, RELOAD_PROFILES).help


def test_concurrent_profile_reads_load_the_file_once(profiles, monkeypatch):
    app = _monitor(FakeMux())
    entered = threading.Event()
    release = threading.Event()
    loads = []
    real_load = palette.load_profiles

    def slow_load():
        loads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(10)
        return real_load()

    monkeypatch.setattr(palette, "load_profiles", slow_load)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(app.palette_profiles()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(10)
    release.set()
    for thread in threads:
        thread.join(10)
    assert len(loads) == 1
    assert len(results) == 4 and all(result is results[0] for result in results)


@pytest.mark.asyncio
async def test_provider_discovers_and_searches_the_host_commands(profiles, tools):
    app = _monitor(FakeMux())
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        provider = AgentCommands(app.screen)
        discovered = [hit.text for hit in [hit async for hit in provider.discover()]]
        assert discovered == _titles(app)
        found = [hit.text async for hit in provider.search("new agent beta")]
        assert found[0] == "New agent: beta"


@pytest.mark.asyncio
async def test_the_palette_runs_a_command_end_to_end(profiles, tools):
    app = _monitor(None)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        assert not app.query_one(DetailPanel).has_class("visible")
        await pilot.press("ctrl+p")
        await pilot.pause()
        await pilot.press(*"toggle detail")
        await pilot.pause(0.3)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert app.query_one(DetailPanel).has_class("visible")


@pytest.mark.asyncio
async def test_focus_session_focuses_the_selected_row(profiles, tools, monkeypatch):
    app = _monitor(None)
    focused = []
    monkeypatch.setattr(FleetMonitorApp, "_do_focus", lambda self, s: focused.append(s.repo))
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        await pilot.press("3")
        _command(app, FOCUS_SESSION).callback()
    assert focused == ["billing"]


@pytest.mark.asyncio
async def test_the_sidebar_offers_new_agents_into_its_own_session(profiles, tools):
    mux = FakeMux(here=PaneRef("zellij", "agents", "1", None))
    config = AppConfig(workspace=WorkspaceSettings(session="elsewhere",
                                                   default_profiles=("beta",)))
    feed = WorkspaceFeed("agents", mux, lambda: [])
    app = SidebarApp(mux, "agents", feed=feed, refresh_interval=60, config=config)
    assert _titles(app)[:4] == [
        ADD_AGENT, "New agent: beta", "New agent: alpha", "New agent: gamma",
    ]
    async with app.run_test(size=(40, 20)) as pilot:
        await _settle(app, pilot)
        _command(app, "New agent: gamma").callback()
        await _settle(app, pilot)
    assert [(call["session"], call["name"]) for call in mux.spawned] == [("agents", "gamma")]
