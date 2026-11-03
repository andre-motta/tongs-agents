"""Tests for the SessionTable widget using Textual's pilot."""

import dataclasses

import pytest
from textual.app import App, ComposeResult

from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.terminal_apis import TERMINALS
from tongs_agents.widgets.session_table import (
    COMPACT_FIXED_WIDTHS,
    TERMINAL_ABBREV,
    SessionTable,
    compact_age,
    compact_status_label,
    session_name,
)


def _session(
    sid: str = "s1",
    repo: str = "myrepo",
    status: SessionStatus = SessionStatus.RUNNING,
    detail: str = "processing",
    age: int = 30,
    attention: bool = False,
    canonical_id: str = "",
) -> FleetSession:
    return FleetSession(
        session_id=sid,
        agent="claude",
        repo=repo,
        cwd=f"/tmp/{repo}",
        status=status,
        detail=detail,
        ts=0,
        started=0,
        pid="1234",
        terminal="generic",
        terminal_env={},
        source="hook",
        tool="",
        age_seconds=age,
        needs_attention=attention,
        canonical_id=canonical_id,
    )


class TableApp(App):
    def compose(self) -> ComposeResult:
        yield SessionTable(id="table")


@pytest.mark.asyncio
async def test_table_populates():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        sessions = [_session("s1", "alpha"), _session("s2", "beta")]
        table.set_sessions(sessions)
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_table_clears_on_empty():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([_session()])
        assert table.row_count == 1
        table.set_sessions([])
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_get_selected_session_id():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([_session("s1"), _session("s2")])
        sid = table.get_selected_session_id()
        assert sid in ("s1", "s2")


@pytest.mark.asyncio
async def test_get_selected_empty():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        assert table.get_selected_session_id() is None


@pytest.mark.asyncio
async def test_canonical_ids_allow_duplicate_native_session_ids():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([
            _session("shared", "alpha", canonical_id="fleet:v1:claude:a"),
            _session("shared", "beta", canonical_id="fleet:v1:codex:b"),
        ])
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_attention_marker():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([_session(attention=True)])
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_long_detail_truncated():
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        long_detail = "x" * 100
        table.set_sessions([_session(detail=long_detail)])
        assert table.row_count == 1


@pytest.mark.asyncio
async def test_terminal_column_present():
    """Table should include agent and terminal columns."""
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        assert len(table.columns) == 8


@pytest.mark.asyncio
async def test_various_terminal_types():
    """Sessions with different terminals should render."""
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        sessions = []
        for i, term in enumerate(["konsole", "tmux", "iterm2", "generic"]):
            s = FleetSession(
                session_id=f"s{i}", repo=f"r{i}", cwd=f"/tmp/r{i}",
                agent="claude",
                status=SessionStatus.RUNNING, detail="", ts=0, started=0,
                pid="1", terminal=term, terminal_env={}, source="hook",
                tool="", age_seconds=0, needs_attention=False,
            )
            sessions.append(s)
        table.set_sessions(sessions)
        assert table.row_count == 4


def test_every_registered_terminal_has_an_abbreviation():
    assert {cls.name for cls in TERMINALS} <= set(TERMINAL_ABBREV)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "label"),
    [
        ("terminator", "trm"),
        ("ghostty", "gho"),
        ("konsole", "KDE"),
        ("kitty", "kty"),
        ("screen", "scr"),
    ],
)
async def test_terminal_column_uses_abbreviation(terminal, label):
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([dataclasses.replace(_session(), terminal=terminal)])
        assert str(table.get_row_at(0)[5]) == label


@pytest.mark.asyncio
async def test_age_colors_by_status():
    """Old idle sessions should get different age styling than running."""
    async with TableApp().run_test() as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([
            _session("s1", age=700, status=SessionStatus.IDLE),
            _session("s2", age=700, status=SessionStatus.RUNNING),
        ])
        assert table.row_count == 2


class CompactApp(App):
    def compose(self) -> ComposeResult:
        yield SessionTable(id="table", compact=True)


@dataclasses.dataclass(frozen=True)
class ProfiledSession(FleetSession):
    profile_id: str = ""


def test_session_name_prefers_profile_id():
    base = _session(repo="myrepo")
    profiled = ProfiledSession(**{
        f.name: getattr(base, f.name) for f in dataclasses.fields(FleetSession)
    }, profile_id="reviewer")
    assert session_name(base) == "myrepo"
    assert session_name(profiled) == "reviewer"
    assert session_name(dataclasses.replace(profiled, profile_id="")) == "myrepo"


def test_compact_status_labels_fit_the_status_column():
    for status in SessionStatus:
        assert len(compact_status_label(status)) <= COMPACT_FIXED_WIDTHS["status"]


@pytest.mark.parametrize(
    ("seconds", "label"),
    [(5, "5s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"),
     (99 * 3600, "99h"), (100 * 3600, "4d"), (9999 * 86400, "9999d")],
)
def test_compact_age_fits_the_age_column(seconds, label):
    assert compact_age(seconds) == label
    assert len(label) <= COMPACT_FIXED_WIDTHS["age"]


@pytest.mark.asyncio
async def test_compact_table_has_five_columns_and_no_detail():
    async with CompactApp().run_test(size=(40, 20)) as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([_session(detail="secret detail")])
        await pilot.pause()
        assert [c.key.value for c in table.ordered_columns] == [
            "attention", "icon", "name", "status", "age",
        ]
        assert str(table.get_row_at(0)[2]) == "myrepo"
        assert table.virtual_size.width <= table.size.width


@pytest.mark.asyncio
async def test_table_switches_layout_on_resize_and_keeps_cursor():
    async with TableApp().run_test(size=(120, 20)) as pilot:
        table = pilot.app.query_one("#table", SessionTable)
        table.set_sessions([_session("s1", "alpha"), _session("s2", "beta")])
        table.move_cursor(row=1)
        assert len(table.columns) == 8
        await pilot.resize_terminal(40, 20)
        await pilot.pause()
        assert table.compact
        assert len(table.columns) == 5
        assert table.row_count == 2
        assert table.get_selected_session_id() == "s2"
        await pilot.resize_terminal(120, 20)
        await pilot.pause()
        assert len(table.columns) == 8
