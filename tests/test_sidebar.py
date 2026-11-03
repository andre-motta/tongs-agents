"""The workspace sidebar: launch rows, spawn on mount and focus (mocked mux)."""

import threading

import pytest
from textual.geometry import Region

from tests.test_workspace_spawn import FakeMux, profile, runtime_dir  # noqa: F401
from tongs_agents import workspace_spawn
from tongs_agents.commands import sidebar
from tongs_agents.launches import LaunchRecord
from tongs_agents.models import FocusResult, SessionStatus
from tongs_agents.multiplexers import MultiplexerError, PaneInfo, PaneRef
from tongs_agents.sessions_feed import LAUNCH_SOURCE, WorkspaceFeed
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets.session_table import status_label

SESSION = "agents"
NOW = 1_000_000


def record(launch_id, profile_name, *, state="running", pane_id=None, pid=4242,
           session=SESSION, replaces=None, socket_dir=None):
    return LaunchRecord(
        launch_id=launch_id * 32 if len(launch_id) == 1 else launch_id,
        profile=profile_name,
        harness="other",
        multiplexer="zellij",
        session=session,
        created=NOW - 30,
        socket_dir=socket_dir,
        pane_id=pane_id,
        pid=pid if state == "running" else None,
        process_start="1" if state == "running" else None,
        state=state,
        replaces=replaces,
    )


def pane(pane_id, *, exited=False, exit_status=None, title="agent", socket_dir=None):
    return PaneInfo(
        PaneRef("zellij", SESSION, pane_id, socket_dir), title, "cmd", exited, False,
        exit_status,
    )


def hook_record(launch_id=None, repo="hooked-repo", *, session=SESSION, socket_dir=None):
    terminal_env = {"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": session, "ZELLIJ_PANE_ID": "1"}
    if session is None:
        terminal_env = {}
    if socket_dir is not None:
        terminal_env["ZELLIJ_SOCKET_DIR"] = socket_dir
    record = {
        "session_id": f"sid-{repo}", "agent": "claude", "repo": repo, "cwd": f"/w/{repo}",
        "status": "running", "detail": "processing prompt", "ts": NOW, "age_seconds": 3,
        "terminal": "zellij", "terminal_env": terminal_env,
    }
    if launch_id is not None:
        record["launch"] = {"launch_id": launch_id, "profile_id": "hooked"}
    return record


def feed(records, panes, hooks=(), alive=lambda record: True, socket_dir=None):
    mux = FakeMux(panes=panes)
    return WorkspaceFeed(
        SESSION, mux, lambda: [dict(hook) for hook in hooks],
        launch_reader=lambda session: list(records), alive=alive, clock=lambda: NOW,
        socket_dir=socket_dir,
    ), mux


def test_hook_rows_are_limited_to_this_session():
    hooks = [
        hook_record(repo="here"),
        hook_record(repo="here-same-socket", socket_dir="/run/zj/"),
        hook_record(repo="other-session", session="scratch"),
        hook_record(repo="other-socket", socket_dir="/run/elsewhere"),
        hook_record(repo="plain-terminal", session=None),
    ]
    workspace_feed, _ = feed([], [], hooks=hooks, socket_dir="/run/zj")
    assert [row.repo for row in workspace_feed.snapshot()] == ["here", "here-same-socket"]


def test_a_captured_socket_dir_needs_a_known_sidebar_socket_dir():
    workspace_feed, _ = feed([], [], hooks=[
        hook_record(repo="default"), hook_record(repo="custom", socket_dir="/run/zj"),
    ])
    assert [row.repo for row in workspace_feed.snapshot()] == ["default"]


def test_a_hook_row_from_another_session_does_not_hide_its_launch_row():
    launched = record("a", "launched", pane_id="1")
    workspace_feed, _ = feed(
        [launched], [pane("1")],
        hooks=[hook_record(launched.launch_id, session="scratch")],
    )
    rows = workspace_feed.snapshot()
    assert [(row.source, row.profile_id) for row in rows] == [(LAUNCH_SOURCE, "launched")]


def test_every_launched_pane_is_listed():
    hooked = record("a", "hooked", pane_id="1")
    plain = record("b", "plain", pane_id="2")
    failing = record("c", "failing", pane_id="3")
    clean = record("d", "clean", pane_id="4")
    held = record("e", "held", pane_id="5")
    workspace_feed, _ = feed(
        [hooked, plain, failing, clean, held],
        [pane("1"), pane("2"), pane("3", exited=True, exit_status=3),
         pane("4", exited=True, exit_status=0), pane("5", exited=True)],
        hooks=[hook_record(hooked.launch_id)],
    )
    rows = workspace_feed.snapshot()
    assert [(row.source, row.profile_id, row.status, row.detail) for row in rows] == [
        ("hook", "hooked", SessionStatus.RUNNING, "processing prompt"),
        (LAUNCH_SOURCE, "plain", SessionStatus.DISCOVERED, "running"),
        (LAUNCH_SOURCE, "failing", SessionStatus.ERROR, "exited 3"),
        (LAUNCH_SOURCE, "clean", SessionStatus.ENDED, "exited 0"),
        (LAUNCH_SOURCE, "held", SessionStatus.ERROR, "exited"),
    ]
    plain_row = rows[1]
    assert plain_row.identity == f"launch:{plain.launch_id}"
    assert plain_row.terminal == "zellij"
    assert plain_row.terminal_env == {"ZELLIJ_SESSION_NAME": SESSION, "ZELLIJ_PANE_ID": "2"}
    assert plain_row.age_seconds == 30
    assert [status_label(row) for row in rows[1:]] == [
        "RUNNING", "EXITED 3", "EXITED 0", "EXITED",
    ]


def test_hook_rows_alone_skip_list_panes():
    hooked = record("a", "hooked", pane_id="1")
    workspace_feed, mux = feed([hooked], [pane("1")], hooks=[hook_record(hooked.launch_id)])
    assert len(workspace_feed.snapshot()) == 1
    assert ("list_panes", SESSION) not in mux.calls


def test_stale_replaced_missing_and_foreign_records_are_skipped():
    stale = record("a", "stale", pane_id="1")
    old = record("b", "old", pane_id="2")
    new = record("c", "new", pane_id="2", replaces=old.launch_id)
    gone = record("d", "gone", pane_id="9")
    pending = record("e", "pending", state="pending")
    other_socket = record("f", "moved", pane_id="3", socket_dir="/run/a")
    workspace_feed, _ = feed(
        [stale, old, new, gone, pending, other_socket],
        [pane("1"), pane("2"), pane("3", socket_dir="/run/b")],
        alive=lambda candidate: candidate.launch_id != stale.launch_id,
    )
    assert [row.profile_id for row in workspace_feed.snapshot()] == ["new"]


def test_a_dead_process_in_an_exited_pane_is_still_listed():
    exited = record("a", "done", pane_id="1")
    workspace_feed, _ = feed([exited], [pane("1", exited=True, exit_status=64)],
                             alive=lambda candidate: False)
    assert [row.detail for row in workspace_feed.snapshot()] == ["exited 64"]


def test_a_pending_launch_this_sidebar_spawned_shows_its_pane():
    pending = record("a", "starting", state="pending")
    workspace_feed, _ = feed([pending], [pane("4", exited=True, exit_status=64), pane("5")])
    assert workspace_feed.snapshot() == []
    workspace_feed.note_spawned(pending.launch_id, PaneRef("zellij", SESSION, "5"))
    assert [row.detail for row in workspace_feed.snapshot()] == ["starting"]
    workspace_feed.note_spawned(pending.launch_id, PaneRef("zellij", SESSION, "4"))
    assert [row.detail for row in workspace_feed.snapshot()] == ["exited 64"]


def test_list_panes_failure_hides_only_launch_rows():
    workspace_feed, mux = feed([record("a", "plain", pane_id="1")], [])

    def broken(session):
        raise MultiplexerError("zellij gone")

    mux.list_panes = broken
    assert workspace_feed.snapshot() == []


def test_focus_launch_pane_selects_a_listed_pane():
    workspace_feed, mux = feed([record("a", "plain", pane_id="2")], [pane("2")])
    row, = workspace_feed.snapshot()
    result = sidebar.focus_launch_pane(mux, SESSION, row)
    assert mux.focused == [PaneRef("zellij", SESSION, "2", None)]
    assert result.selection.succeeded
    mux.panes.clear()
    assert sidebar.focus_launch_pane(mux, SESSION, row).state == "not-found"
    assert sidebar.focus_launch_pane(mux, "other", row).state == "unavailable"
    assert len(mux.focused) == 1


def test_sidebar_refuses_to_run_outside_zellij(monkeypatch, capsys):
    monkeypatch.setattr(sidebar, "get_multiplexer", lambda name="zellij": FakeMux())
    assert sidebar.main([]) == 2
    assert "inside a Zellij session" in capsys.readouterr().err
    assert sidebar.main(["--spawn-request", "../etc"]) == 2


@pytest.fixture(autouse=True)
def _no_desktop_notifications(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _launch_records(names):
    return workspace_spawn.create_launches("zellij", SESSION, [profile(name) for name in names])


def _app(mux, token=None, hooks=()):
    workspace_feed = WorkspaceFeed(SESSION, mux, lambda: [dict(hook) for hook in hooks])
    return sidebar.SidebarApp(mux, SESSION, token=token, feed=workspace_feed,
                              refresh_interval=60)


@pytest.mark.asyncio
async def test_mount_consumes_the_spawn_request_once():
    records = _launch_records(["alpha", "beta"])
    token = workspace_spawn.write_spawn_request(SESSION, [r.launch_id for r in records])
    mux = FakeMux(fail_spawn={"beta"})
    app = _app(mux, token)
    async with app.run_test(size=(40, 20)) as pilot:
        await _settle(app, pilot)
        assert [call["name"] for call in mux.spawned] == ["alpha"]
        assert app.spawn_failures == ["profile beta: cannot spawn beta"]
        assert any("cannot spawn beta" in str(note.message) for note in app._notifications)
        rows = app.agents_panel.sessions
        assert [(row.profile_id, row.detail) for row in rows] == [("alpha", "starting")]

    second = FakeMux()
    again = _app(second, token)
    async with again.run_test(size=(40, 20)) as pilot:
        await _settle(again, pilot)
        assert second.spawned == []
        assert again.spawn_failures == []


@pytest.mark.asyncio
async def test_selecting_a_hookless_row_focuses_its_pane():
    records = _launch_records(["alpha"])
    token = workspace_spawn.write_spawn_request(SESSION, [records[0].launch_id])
    mux = FakeMux()
    done = threading.Event()
    original = mux.focus

    def focus(ref):
        try:
            return original(ref)
        finally:
            done.set()

    mux.focus = focus
    app = _app(mux, token)
    async with app.run_test(size=(40, 20)) as pilot:
        await _settle(app, pilot)
        app.agents_panel.table.focus()
        await pilot.press("enter")
        assert done.wait(5)
        await pilot.pause()
        assert mux.focused == [PaneRef("zellij", SESSION, "1", None)]


@pytest.mark.asyncio
async def test_selecting_a_hook_row_uses_focus_session(monkeypatch):
    called = []
    done = threading.Event()

    def fake_focus(identity):
        called.append(identity)
        done.set()
        return FocusResult(state="not-found", reason="test")

    monkeypatch.setattr(agents_panel, "focus_session", fake_focus)
    mux = FakeMux()
    app = _app(mux, hooks=[hook_record("f" * 32)])
    async with app.run_test(size=(40, 20)) as pilot:
        await _settle(app, pilot)
        app.agents_panel.table.focus()
        await pilot.press("enter")
        assert done.wait(5)
        assert called == ["sid-hooked-repo"]
        assert mux.focused == []


@pytest.mark.asyncio
async def test_compact_rows_show_the_pane_state():
    records = _launch_records(["alpha"])
    token = workspace_spawn.write_spawn_request(SESSION, [records[0].launch_id])
    mux = FakeMux()
    app = _app(mux, token)
    async with app.run_test(size=(40, 20)) as pilot:
        await _settle(app, pilot)
        table = app.agents_panel.table
        region = Region(0, 0, table.size.width, table.size.height)
        text = "\n".join(strip.text for strip in table.render_lines(region))
        assert "alpha" in text and "STARTING" in text


def test_the_sidebar_feed_uses_the_pane_socket_dir():
    app = sidebar.SidebarApp(FakeMux(), SESSION, socket_dir="/run/zj")
    assert app.feed.session == SESSION
    assert app.feed.socket_dir == "/run/zj"
