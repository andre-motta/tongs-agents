"""AgentsPanel behaviour inside both hosts, at sidebar and full widths."""

import ast
import re
from pathlib import Path

import pytest
from textual.geometry import Region

from tongs_agents.models import SessionStatus
from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.views.fleet_screen import FLEET_CHILD_IDS, FleetScreen
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets.agents_panel import AgentsPanel
from tongs_agents.widgets.detail_panel import DetailPanel
from tongs_agents.widgets.session_table import (
    SessionTable,
    compact_status_label,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "tongs_agents"

NAMES = {
    SessionStatus.STARTED: "api",
    SessionStatus.RUNNING: "web-ui",
    SessionStatus.IDLE: "docs-site",
    SessionStatus.WAITING: "billing",
    SessionStatus.ERROR: "infra",
    SessionStatus.ENDED: "mobile-app",
    SessionStatus.DISCOVERED: "cli-tools",
}


def _record(status, name, *, age=30, attention=None):
    if attention is None:
        attention = status is SessionStatus.WAITING
    return {
        "session_id": f"sid-{name}",
        "agent": "claude",
        "repo": name,
        "cwd": f"/tmp/{name}",
        "status": status.value,
        "detail": f"detail for {name}",
        "ts": 0,
        "age_seconds": age,
        "needs_attention": attention,
    }


class MutableReader:
    def __init__(self, records):
        self.records = list(records)

    def __call__(self):
        return [dict(r) for r in self.records]


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


def _all_status_feed():
    return SessionFeed(MutableReader(
        _record(status, name) for status, name in NAMES.items()
    ))


def _table_text(table):
    region = Region(0, 0, table.size.width, table.size.height)
    return [strip.text for strip in table.render_lines(region)]


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(40, 30), (120, 30)])
async def test_app_renders_every_status_without_truncation(size):
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=_all_status_feed())
    async with app.run_test(size=size) as pilot:
        await _settle(app, pilot)
        table = app.query_one(SessionTable)
        assert table.row_count == len(NAMES)
        assert table.compact is (size[0] < 80)
        lines = _table_text(table)
        for status, name in NAMES.items():
            label = compact_status_label(status) if table.compact else status.value.upper()
            row = next((line for line in lines if re.search(rf"\b{re.escape(name)}\b", line)), None)
            assert row is not None, (name, lines)
            assert label in row, (label, row)
        if table.compact:
            assert [c.key.value for c in table.ordered_columns] == [
                "attention", "icon", "name", "status", "age",
            ]
            assert table.virtual_size.width <= table.size.width
            assert "detail for" not in "\n".join(lines)


@pytest.mark.asyncio
async def test_compact_flag_forces_compact_columns_at_full_width():
    app = FleetMonitorApp(refresh_interval=60, notify_level="none",
                          feed=_all_status_feed(), compact=True)
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        table = app.query_one(SessionTable)
        assert table.compact
        assert len(table.columns) == 5


SANDBOX_LAUNCH = "b0" * 16


def _sandbox_feed(sandboxed):
    boxed = _record(SessionStatus.RUNNING, "boxed")
    if sandboxed:
        boxed["launch"] = {"launch_id": SANDBOX_LAUNCH, "profile_id": "boxed"}
    return SessionFeed(MutableReader([boxed, _record(SessionStatus.IDLE, "plain")]))


@pytest.mark.asyncio
@pytest.mark.parametrize("sandboxed, width, compact", [
    (False, 79, True),
    (False, 80, False),
    (False, 97, False),
    (True, 79, True),
    (True, 80, True),
    (True, 97, True),
    (True, 98, False),
])
async def test_sandbox_column_moves_the_full_layout_threshold(monkeypatch, sandboxed, width,
                                                              compact):
    monkeypatch.setattr(agents_panel, "sandbox_launches",
                        lambda: {SANDBOX_LAUNCH: "tongs-boxed"})
    app = FleetMonitorApp(refresh_interval=60, notify_level="none",
                          feed=_sandbox_feed(sandboxed))
    async with app.run_test(size=(width, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.query_one(AgentsPanel)
        table = panel.table
        keys = [c.key.value for c in table.ordered_columns]
        assert ("sandbox" in keys) is sandboxed
        assert table.full_min_width == (80 + 16 + 2 * table.cell_padding if sandboxed else 80)
        assert table.compact is compact
        assert ("name" in keys) is compact
        assert panel._is_narrow() is compact
        summary = str(panel.summary_bar.render())
        assert ("2 agents" in summary) is compact
        assert ("2 sessions" in summary) is not compact
        if compact:
            assert table.virtual_size.width <= table.size.width


@pytest.mark.asyncio
async def test_summary_follows_the_sandbox_column_as_it_comes_and_goes(monkeypatch):
    monkeypatch.setattr(agents_panel, "sandbox_launches",
                        lambda: {SANDBOX_LAUNCH: "tongs-boxed"})
    feed = _sandbox_feed(True)
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=feed)
    async with app.run_test(size=(90, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.query_one(AgentsPanel)
        assert panel.table.compact and panel._is_narrow()
        assert "2 agents" in str(panel.summary_bar.render())
        panel.feed = _sandbox_feed(False)
        panel.poll()
        await _settle(app, pilot)
        assert "sandbox" not in [c.key.value for c in panel.table.ordered_columns]
        assert not panel.table.compact and not panel._is_narrow()
        assert "2 sessions" in str(panel.summary_bar.render())


@pytest.mark.asyncio
async def test_screen_hosts_panel_with_its_ids_and_screen_bindings():
    from textual.app import App

    class Host(App):
        def on_mount(self):
            self.push_screen(FleetScreen(refresh_interval=60, notify_level="none",
                                         feed=_all_status_feed()))

    app = Host()
    async with app.run_test(size=(40, 30)) as pilot:
        await _settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, FleetScreen)
        panel = screen.query_one(AgentsPanel)
        for child_id in FLEET_CHILD_IDS.values():
            assert screen.query_one(f"#{child_id}")
        assert screen.query_one("#fleet-table", SessionTable).row_count == len(NAMES)
        await pilot.press("3")
        assert screen.query_one("#fleet-table", SessionTable).row_count == 1
        assert panel.selected_session().status is SessionStatus.WAITING
        await pilot.press("escape")
        assert not isinstance(app.screen, FleetScreen)


@pytest.mark.asyncio
async def test_panel_actions_filter_search_sort_detail_and_jump():
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=_all_status_feed())
    async with app.run_test(size=(120, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.query_one(AgentsPanel)
        table = panel.table
        await pilot.press("n")
        assert panel.selected_session().repo == "billing"
        await pilot.press("d")
        detail = app.query_one(DetailPanel)
        assert detail.has_class("visible")
        assert "billing" in str(detail.render())
        await pilot.press("slash")
        panel.search_input.value = "infra"
        await pilot.pause()
        assert table.row_count == 1
        await pilot.press("escape")
        assert table.row_count == len(NAMES)
        await pilot.press("s")
        assert panel._sort_key == "attention"
        assert panel.shown_sessions()[0].repo == "billing"


@pytest.mark.asyncio
async def test_bell_rings_once_for_a_new_waiting_session(monkeypatch):
    reader = MutableReader([_record(SessionStatus.RUNNING, "api")])
    app = FleetMonitorApp(refresh_interval=60, notify_level="all", feed=SessionFeed(reader))
    rings = []
    async with app.run_test(size=(40, 30)) as pilot:
        monkeypatch.setattr(app, "bell", lambda: rings.append(1))
        await _settle(app, pilot)
        assert rings == []
        reader.records.append(_record(SessionStatus.WAITING, "billing"))
        panel = app.query_one(AgentsPanel)
        panel.poll()
        await _settle(app, pilot)
        assert rings == [1]
        panel.poll()
        await _settle(app, pilot)
        assert rings == [1]


@pytest.mark.asyncio
async def test_poll_after_removal_is_ignored():
    feed = _all_status_feed()
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=feed)
    async with app.run_test(size=(40, 30)) as pilot:
        await _settle(app, pilot)
        panel = app.query_one(AgentsPanel)
        shown = panel.sessions
        await panel.remove()
        await pilot.pause()
        assert not panel.is_attached
        panel.update_sessions(feed.snapshot())
        panel._render_view()
        assert panel.sessions == shown


@pytest.mark.asyncio
async def test_search_placeholder_names_agents():
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=_all_status_feed())
    async with app.run_test(size=(40, 30)) as pilot:
        await _settle(app, pilot)
        assert app.query_one(AgentsPanel).search_input.placeholder == "Filter agents..."


@pytest.mark.asyncio
async def test_no_bell_when_notifications_are_off(monkeypatch):
    feed = SessionFeed(MutableReader([_record(SessionStatus.WAITING, "billing")]))
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=feed)
    rings = []
    async with app.run_test(size=(40, 30)) as pilot:
        monkeypatch.setattr(app, "bell", lambda: rings.append(1))
        await _settle(app, pilot)
    assert rings == []


@pytest.mark.asyncio
async def test_enter_posts_selected_and_host_focuses(monkeypatch):
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=_all_status_feed())
    focused = []
    monkeypatch.setattr(FleetMonitorApp, "_do_focus", lambda self, s: focused.append(s.repo))
    async with app.run_test(size=(40, 30)) as pilot:
        await _settle(app, pilot)
        app.query_one(SessionTable).focus()
        await pilot.press("n", "enter")
        await pilot.pause()
    assert focused == ["billing"]


def _ui_modules():
    return [PACKAGE / "tui.py", *sorted((PACKAGE / "views").glob("*.py")),
            *sorted((PACKAGE / "widgets").glob("*.py"))]


def test_read_sessions_is_only_called_through_sessions_feed():
    assert "read_sessions" in (PACKAGE / "sessions_feed.py").read_text()
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _ui_modules()
        if "read_sessions" in path.read_text()
    ]
    assert offenders == []


@pytest.mark.parametrize("path", ["tui.py", "views/fleet_screen.py"])
def test_both_hosts_compose_agents_panel(path):
    tree = ast.parse((PACKAGE / path).read_text())
    composed = {
        node.value.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Yield)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert "AgentsPanel" in composed
    assert "SessionTable" not in composed
