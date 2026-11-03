"""The sidebar's Add agent dialog: profile, folder completion, arguments (headless)."""

import json
import os

import pytest
from textual.widgets import Input, Select, Static

from tests.test_workspace_spawn import FakeMux, runtime_dir  # noqa: F401
from tongs_agents import launches
from tongs_agents.commands.sidebar import SidebarApp
from tongs_agents.palette import ADD_AGENT
from tongs_agents.sessions_feed import WorkspaceFeed
from tongs_agents.widgets import add_agent, agents_panel
from tongs_agents.widgets.add_agent import AddAgentScreen, complete_folder

SESSION = "agents"
SIZE = (100, 40)


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


@pytest.fixture
def projects(tmp_path, monkeypatch):
    start = tmp_path / "start"
    alpha_home = tmp_path / "alpha-home"
    start.mkdir()
    alpha_home.mkdir()
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "config.toml"
    path.write_text(
        '[profiles.alpha]\ncommand = ["claude"]\n'
        f"cwd = {json.dumps(str(alpha_home))}\n"
        '[profiles.beta]\ncommand = ["codex"]\n'
    )
    path.chmod(0o600)
    monkeypatch.chdir(start)
    return start, alpha_home


def _app(mux):
    feed = WorkspaceFeed(SESSION, mux, lambda: [])
    return SidebarApp(mux, SESSION, feed=feed, refresh_interval=60)


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _value(screen, selector):
    return screen.query_one(selector, Input).value


async def _open(app, pilot):
    await pilot.press("plus")
    await pilot.pause()
    screen = app.screen
    assert isinstance(screen, AddAgentScreen)
    return screen


@pytest.mark.asyncio
async def test_plus_opens_the_dialog_and_starts_the_agent_with_folder_and_args(
    projects, tmp_path,
):
    _start, alpha_home = projects
    target = tmp_path / "target"
    target.mkdir()
    mux = FakeMux()
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        screen = await _open(app, pilot)
        assert screen.query_one("#add-profile", Select).value == "alpha"
        assert _value(screen, "#add-folder") == str(alpha_home)
        screen.query_one("#add-folder", Input).value = str(target)
        screen.query_one("#add-args", Input).value = "--resume 'two words'"
        await pilot.pause()
        preview = str(screen.query_one("#add-preview", Static).render())
        assert "Runs: claude --resume 'two words'" in preview
        assert str(target) in preview
        await pilot.click("#add-start")
        await _settle(app, pilot)
        assert not isinstance(app.screen, AddAgentScreen)
    [record] = launches.list_records()
    assert (record.profile, record.folder, record.args) == (
        "alpha", str(target), ("--resume", "two words"),
    )
    assert [call["name"] for call in mux.spawned] == ["alpha"]
    assert mux.spawned[0]["argv"][-3:] == ["alpha", "--launch-id", record.launch_id]


@pytest.mark.asyncio
async def test_a_missing_folder_keeps_the_dialog_open_with_the_reason(projects, tmp_path):
    mux = FakeMux()
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        screen = await _open(app, pilot)
        screen.query_one("#add-folder", Input).value = str(tmp_path / "gone")
        await pilot.pause()
        screen.action_start()
        await pilot.pause()
        assert app.screen is screen
        assert "does not exist" in str(screen.query_one("#add-error", Static).render())
        screen.query_one("#add-folder", Input).value = str(tmp_path)
        screen.query_one("#add-args", Input).value = "'unbalanced"
        await pilot.pause()
        assert "Arguments:" in str(screen.query_one("#add-preview", Static).render())
        screen.action_start()
        await pilot.pause()
        assert app.screen is screen
        assert "arguments:" in str(screen.query_one("#add-error", Static).render())
        await pilot.press("escape")
        await _settle(app, pilot)
        assert not isinstance(app.screen, AddAgentScreen)
    assert mux.spawned == [] and launches.list_records() == []


@pytest.mark.asyncio
async def test_switching_profile_moves_an_untouched_folder_to_its_default(projects):
    start, alpha_home = projects
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        screen = await _open(app, pilot)
        screen.query_one("#add-profile", Select).value = "beta"
        await pilot.pause()
        assert _value(screen, "#add-folder") == str(start)
        assert "codex --no-daemon" in str(screen.query_one("#add-preview", Static).render())
        screen.query_one("#add-folder", Input).value = "/somewhere"
        screen.query_one("#add-profile", Select).value = "alpha"
        await pilot.pause()
        assert _value(screen, "#add-folder") == "/somewhere"


@pytest.mark.asyncio
async def test_the_palette_entry_opens_the_dialog(projects):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        command = next(c for c in app.palette_commands() if c.title == ADD_AGENT)
        command.callback()
        await pilot.pause()
        assert isinstance(app.screen, AddAgentScreen)


@pytest.mark.asyncio
async def test_without_profiles_the_dialog_does_not_open(tmp_path):
    app = _app(FakeMux())
    async with app.run_test(size=SIZE) as pilot:
        await _settle(app, pilot)
        await pilot.press("plus")
        await pilot.pause()
        assert not isinstance(app.screen, AddAgentScreen)
        assert any("No profiles yet" in str(note.message) for note in app._notifications)


def test_complete_folder_extends_to_unique_and_common_directory_names(tmp_path, monkeypatch):
    for name in ("project-one", "project-two", "single", ".hidden"):
        (tmp_path / name).mkdir()
    (tmp_path / "somefile").write_text("")
    base = str(tmp_path) + os.sep
    assert complete_folder(base + "si") == base + "single" + os.sep
    assert complete_folder(base + "pro") == base + "project-"
    assert complete_folder(base + "project-") is None
    assert complete_folder(base + "some") is None
    assert complete_folder(base + ".hi") == base + ".hidden" + os.sep
    assert complete_folder(base + "h") is None
    assert complete_folder(base + "missing/x") is None
    assert complete_folder("") is None
    assert complete_folder("relative") is None
    monkeypatch.setenv("HOME", str(tmp_path))
    assert complete_folder("~") == "~" + os.sep
    assert complete_folder("~/si") == "~/single" + os.sep


def test_complete_folder_reads_a_bounded_number_of_entries(tmp_path, monkeypatch):
    for index in range(5):
        (tmp_path / f"d{index}").mkdir()
    monkeypatch.setattr(add_agent, "MAX_COMPLETION_ENTRIES", 0)
    assert complete_folder(str(tmp_path) + os.sep + "d") is None
