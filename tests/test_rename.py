"""Rename an agent from the sidebar (#130): label, rows, status and pane (headless)."""

import json
import os

import pytest

from tests.test_sidebar import feed, hook_record, pane, record
from tests.test_workspace_spawn import FakeMux, profile, runtime_dir  # noqa: F401
from tongs_agents import control, launches, mcp_server
from tongs_agents.commands import supervisor
from tongs_agents.commands.sidebar import SidebarApp
from tongs_agents.launches import LaunchRecord
from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.multiplexers import PaneInfo, PaneRef
from tongs_agents.sessions_feed import WorkspaceFeed, visible_sessions
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets.rename_prompt import NOT_HERE
from tongs_agents.widgets.session_table import session_name

SESSION = "agents"
SIZE = (60, 30)


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


def _labelled(base: LaunchRecord, label: str) -> LaunchRecord:
    return LaunchRecord(**{**base.__dict__, "label": label})


# -- Rows --

def test_launch_and_hook_rows_carry_the_label_and_show_it_as_their_name():
    launched = _labelled(record("a", "claude-personal", pane_id="1"), "reviewer")
    hooked = _labelled(record("b", "claude-personal", pane_id="2"), "writer")
    plain = record("c", "claude-personal", pane_id="3")
    workspace, _ = feed([launched, hooked, plain], [pane("1"), pane("2"), pane("3")],
                        hooks=[hook_record("b" * 32)])
    rows = {row.launch_id: row for row in workspace.snapshot()}
    assert rows["a" * 32].label == "reviewer"
    assert rows["b" * 32].label == "writer"
    assert rows["c" * 32].label == ""
    assert [session_name(rows[key * 32]) for key in "abc"] == [
        "reviewer", "writer", "claude-personal"]
    assert [row.launch_id for row in visible_sessions(rows.values(), query="WRIT")] == [
        "b" * 32]
    assert json.loads(rows["a" * 32].to_json())["label"] == "reviewer"


def test_a_hook_row_without_a_launch_record_here_gets_no_label():
    elsewhere = _labelled(record("b", "alpha", pane_id="2", session="scratch"), "x")
    workspace, _ = feed([elsewhere], [], hooks=[hook_record("b" * 32)])
    (row,) = workspace.snapshot()
    assert row.label == ""
    assert workspace.launch_record("b" * 32) is None


# -- agent-status and MCP --

def test_agent_status_and_its_json_show_the_label():
    status = control._status_of(_labelled(record("a", "alpha", pane_id="1"), "fix #130"),
                                "working")
    assert status.to_json()["label"] == "fix #130"
    assert status.to_json()["profile"] == "alpha"
    assert supervisor._line(status).startswith(f"{'a' * 32}  alpha (fix #130)  agents:1")
    plain = control._status_of(record("a", "alpha", pane_id="1"), "working")
    assert plain.to_json()["label"] == ""
    assert supervisor._line(plain).startswith(f"{'a' * 32}  alpha  agents:1")


def test_the_fleet_tools_return_each_session_with_its_label(monkeypatch):
    pending = launches.create_pending(profile("alpha"), SESSION, "zellij")
    launches.set_label(pending.launch_id, "reviewer")
    sessions = [
        {"session_id": "one", "status": "running", "needs_attention": False,
         "launch_id": pending.launch_id},
        {"session_id": "two", "status": "waiting", "needs_attention": True,
         "launch_id": ""},
    ]
    monkeypatch.setattr(mcp_server, "read_sessions", lambda: [dict(s) for s in sessions])
    listed = json.loads(mcp_server.fleet_status())["sessions"]
    assert [(s["session_id"], s["label"]) for s in listed] == [
        ("one", "reviewer"), ("two", "")]
    assert json.loads(mcp_server.fleet_session("one"))["label"] == "reviewer"
    attention = json.loads(mcp_server.fleet_sessions_needing_attention())["sessions"]
    assert [s["label"] for s in attention] == [""]


# -- The sidebar prompt --

def _running(name="claude-personal", pane_id="1"):
    pending = launches.create_pending(profile(name), SESSION, "zellij")
    return launches.claim(pending.launch_id, os.getpid(), None,
                          PaneRef("zellij", SESSION, pane_id, None))


def _mux(*pane_ids, **kwargs):
    return FakeMux(panes=[
        PaneInfo(PaneRef("zellij", SESSION, pane_id, None), "agent", "cmd", False, False)
        for pane_id in pane_ids
    ], **kwargs)


def _app(mux, hooks=()):
    workspace = WorkspaceFeed(SESSION, mux, lambda: [dict(hook) for hook in hooks])
    return SidebarApp(mux, SESSION, feed=workspace, refresh_interval=60)


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _names(app):
    return [str(app.agents_panel.table.get_row_at(index)[2])
            for index in range(app.agents_panel.table.row_count)]


async def _open(app, pilot):
    await _settle(app, pilot)
    app.agents_panel.table.focus()
    await pilot.press("R")
    await pilot.pause()
    return app.rename_prompt


@pytest.mark.asyncio
async def test_r_renames_the_selected_agent_and_its_pane():
    running = _running()
    mux = _mux("1")
    app = _app(mux)
    fired = []
    async with app.run_test(size=SIZE) as pilot:
        for name in ("refresh", "quit", "add_agent", "command_line", "resume_selected",
                     "next_attention", "toggle_chat", "review_access", "rename_agent"):
            setattr(app, f"action_{name}", lambda name=name: fired.append(name))
        await _settle(app, pilot)
        assert _names(app) == ["claude-personal"]
        app.agents_panel.table.focus()
        await pilot.press("R")
        assert fired == ["rename_agent"]
        del app.action_rename_agent
        fired.clear()
        prompt = await _open(app, pilot)
        field = prompt.input
        assert app.focused is field
        assert field.value == "claude-personal"
        assert "empty restores claude-personal" in prompt.message_text
        # Input:focus sets a tall border; on a one-row prompt it hid the text.
        assert field.styles.border_top[0] in ("", "none")
        assert field.content_region.height == 1
        await pilot.press(*["backspace"] * len("claude-personal"))
        await pilot.press(*"Review qa+", "colon", "slash", "u", "n", "m", "s", "d", "R",
                          "space", "number_sign", "1", "3", "0")
        await pilot.pause()
        assert field.value == "Review qa+:/unmsdR #130"
        assert fired == []
        await pilot.press("enter")
        await _settle(app, pilot)
        assert not prompt.has_class("visible")
        assert app.focused is app.agents_panel.table
        assert _names(app) == ["Review qa+:/unmsdR #130"]
    stored = launches.get(running.launch_id)
    assert stored.label == "Review qa+:/unmsdR #130"
    assert (stored.profile, stored.harness, stored.pane_id, stored.pid) == (
        running.profile, running.harness, running.pane_id, running.pid)
    assert mux.renamed == [(PaneRef("zellij", SESSION, "1", None), "Review qa+:/unmsdR #130")]


@pytest.mark.asyncio
async def test_an_empty_name_restores_the_profile_name():
    running = _running()
    launches.set_label(running.launch_id, "reviewer")
    mux = _mux("1")
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        prompt = await _open(app, pilot)
        assert _names(app) == ["reviewer"]
        assert prompt.input.value == "reviewer"
        prompt.input.value = "   "
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _names(app) == ["claude-personal"]
    assert launches.get(running.launch_id).label is None
    assert mux.renamed == [(PaneRef("zellij", SESSION, "1", None), "claude-personal")]


@pytest.mark.asyncio
async def test_escape_cancels_without_saving():
    running = _running()
    mux = _mux("1")
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        prompt = await _open(app, pilot)
        await pilot.press("x", "escape")
        await _settle(app, pilot)
        assert not prompt.has_class("visible")
        assert app.focused is app.agents_panel.table
    assert launches.get(running.launch_id) == running
    assert mux.renamed == []


@pytest.mark.asyncio
async def test_an_invalid_name_keeps_the_prompt_open_with_the_reason():
    running = _running()
    mux = _mux("1")
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        prompt = await _open(app, pilot)
        prompt.input.value = "-oops"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert prompt.has_class("visible")
        assert "cannot start with '-'" in prompt.message_text
        assert app.focused is prompt.input
    assert launches.get(running.launch_id) == running
    assert mux.renamed == []


@pytest.mark.asyncio
async def test_a_row_not_started_here_is_refused_inline():
    mux = _mux()
    app = _app(mux, hooks=[hook_record()])
    async with app.run_test(size=SIZE) as pilot:
        prompt = await _open(app, pilot)
        assert prompt.has_class("visible") and prompt.has_class("refused")
        assert prompt.message_text == f"hooked-repo: {NOT_HERE}"
        assert not prompt.input.display or not prompt.input.region.height
        assert app.focused is app.agents_panel.table
        await pilot.press("escape")
    assert mux.renamed == []
    assert launches.list_records() == []


@pytest.mark.asyncio
async def test_a_failed_pane_rename_keeps_the_label_and_warns():
    running = _running()
    mux = _mux("1", fail_rename=True)
    app = _app(mux)
    async with app.run_test(size=SIZE) as pilot:
        prompt = await _open(app, pilot)
        prompt.input.value = "reviewer"
        await pilot.press("enter")
        await _settle(app, pilot)
        assert _names(app) == ["reviewer"]
        messages = [str(note.message) for note in app._notifications]
        assert any("the pane keeps its old name: no such pane" in m for m in messages)
    assert launches.get(running.launch_id).label == "reviewer"


def test_the_footer_shows_the_rename_key():
    binding = next(b for b in SidebarApp.BINDINGS if b.action == "rename_agent")
    assert (binding.key, binding.description, binding.show) == ("R", "Rename", True)


def test_session_name_falls_back_to_profile_then_repo():
    base = FleetSession(
        session_id="s", repo="repo", cwd="", status=SessionStatus.RUNNING, detail="",
        ts=0, started=None, pid="", terminal="", terminal_env={}, source="hook", tool="",
    )
    assert session_name(base) == "repo"
    assert session_name(FleetSession(**{**base.__dict__, "profile_id": "alpha"})) == "alpha"
    assert session_name(FleetSession(**{**base.__dict__, "profile_id": "alpha",
                                         "label": "mine"})) == "mine"
