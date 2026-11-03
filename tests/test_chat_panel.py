"""The chat panel, the standalone chat pane and the sidebar's chat actions."""

import pytest
from textual.widgets import Input, RichLog

from tests.test_sidebar import SESSION, feed, pane, record
from tongs_agents import palette
from tongs_agents.chat import grants
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import ChatError, ChatRefused, ChatStore
from tongs_agents.commands import chat as chat_command
from tongs_agents.commands import sidebar
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.widgets import chat_panel
from tongs_agents.widgets.chat_panel import ChatPanel, ConfirmScreen, PostScreen

LAUNCH = "a" * 32
CHAT_TITLES = [palette.CHAT_GRANT, palette.CHAT_GRANT_WAKE, palette.CHAT_REVOKE,
               palette.CHAT_POST, palette.CHAT_TOGGLE, palette.CHAT_OPEN]


@pytest.fixture(autouse=True)
def maintainer(monkeypatch):
    monkeypatch.setattr(chat_panel.ChatActions, "chat_refusal", staticmethod(lambda: None))
    monkeypatch.setattr(chat_panel, "maintainer_refusal", lambda: None)
    monkeypatch.setattr(grants, "stale_reason", lambda grant: (
        None if grant.process_start == "1" else "the agent process ended"
    ))


def _sidebar():
    launched = record("a", "lead", pane_id="1")
    workspace_feed, mux = feed([launched], [pane("1")])
    return sidebar.SidebarApp(mux, SESSION, feed=workspace_feed, refresh_interval=60), mux


async def _settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


def _log_lines(app):
    return [line.text for line in app.query_one("#chat-log", RichLog).lines]


def _grant(handle="lead", start="1"):
    with ChatStore.maintainer(create=True) as current:
        return current.put_grant(
            launch_id=LAUNCH, handle=handle, level="chat", process_start=start,
            pane_session=SESSION, pane_id="1", socket_dir=None, via="cli",
            profile="lead", harness="other",
        )


def test_the_sidebar_offers_chat_in_its_palette_and_the_monitor_does_not():
    app, _ = _sidebar()
    titles = [command.title for command in app.palette_commands()]
    assert titles[-len(CHAT_TITLES):] == CHAT_TITLES
    monitor = FleetMonitorApp(refresh_interval=60, notify_level="none")
    assert not set(CHAT_TITLES) & {command.title for command in monitor.palette_commands()}


@pytest.mark.asyncio
async def test_m_toggles_the_hidden_chat_panel():
    app, _ = _sidebar()
    async with app.run_test(size=(60, 30)) as pilot:
        await _settle(app, pilot)
        assert app.chat_panel.display is False
        await pilot.press("m")
        await _settle(app, pilot)
        assert app.chat_panel.display is True
        status = app.query_one("#chat-status").render()
        assert "Chat is off" in str(status)
        await pilot.press("escape")
        app.action_toggle_chat()
        assert app.chat_panel.display is False


@pytest.mark.asyncio
async def test_the_panel_shows_messages_as_text_and_posts_as_the_maintainer():
    _grant()
    with ChatStore.agent(LAUNCH) as agent:
        agent.post("general", "[bold]not markup[/bold] @maintainer")
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.action_toggle_chat()
        await _settle(app, pilot)
        assert _log_lines(app) == ["1 @lead: [bold]not markup[/bold] @maintainer"]
        assert str(app.query_one("#chat-status").render()) == (
            "#general | @lead (tools pending)"
        )
        field = app.query_one("#chat-input", Input)
        field.value = "/reply 1 thanks @lead"
        await pilot.press("enter")
        await _settle(app, pilot)
        await _settle(app, pilot)
        assert _log_lines(app)[-1] == "2 re 1 @maintainer: thanks @lead"
    with ChatStore.agent(LAUNCH) as agent:
        assert [m.id for m in agent.unread_mentions()] == [2]
    with ChatStore.maintainer() as current:
        assert current.channels()[0].unread == 0


@pytest.mark.asyncio
async def test_grant_from_the_palette_asks_before_writing(monkeypatch):
    request = grants.GrantRequest(LAUNCH, "lead", "other", SESSION, "1", None, "1",
                                  "lead", "chat+wake")
    prepared, applied = [], []
    monkeypatch.setattr(grants, "prepare", lambda launch_id, **kw: prepared.append(
        (launch_id, kw)) or request)
    monkeypatch.setattr(grants, "apply", lambda req, **kw: applied.append((req, kw)) or _grant())
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.chat_grant(True)
        await _settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert LAUNCH in app.screen.message and "pane agents:1" in app.screen.message
        await pilot.press("n")
        await _settle(app, pilot)
        assert applied == []
        app.chat_grant(True)
        await _settle(app, pilot)
        await pilot.press("y")
        await _settle(app, pilot)
    assert prepared[0] == (LAUNCH, {"wake": True, "actor": "sidebar"})
    assert applied == [(request, {"via": "sidebar"})]


@pytest.mark.asyncio
async def test_grant_refusals_are_notified(monkeypatch):
    def refuse(launch_id, **kw):
        raise ChatRefused("launch has no process start token")

    monkeypatch.setattr(grants, "prepare", refuse)
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.chat_grant()
        await _settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen)
        assert any("no process start token" in str(n.message) for n in app._notifications)


@pytest.mark.asyncio
async def test_revoke_from_the_palette_asks_first():
    _grant()
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.chat_revoke()
        await _settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "@lead" in app.screen.message and LAUNCH in app.screen.message
        await pilot.press("y")
        await _settle(app, pilot)
    with ChatStore.maintainer() as current:
        assert current.grants() == []


@pytest.mark.asyncio
async def test_post_to_chat_from_the_palette():
    _grant()
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.chat_post_prompt()
        await pilot.pause()
        assert isinstance(app.screen, PostScreen)
        app.screen.query_one("#post-input", Input).value = "hello @lead"
        await pilot.press("enter")
        await _settle(app, pilot)
        await _settle(app, pilot)
    with ChatStore.maintainer() as current:
        assert [(m.author, m.body) for m in current.messages("general", after_id=0)] == [
            ("maintainer", "hello @lead"),
        ]


@pytest.mark.asyncio
async def test_mount_removes_stale_grants():
    _grant(start="ended-process")
    app, _ = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
    with ChatStore.maintainer() as current:
        assert current.grants() == []


@pytest.mark.asyncio
async def test_open_chat_pane_spawns_the_chat_command():
    app, mux = _sidebar()
    async with app.run_test(size=(80, 30)) as pilot:
        await _settle(app, pilot)
        app.palette_open_chat()
        await _settle(app, pilot)
    call = mux.spawned[-1]
    assert call["argv"][-2:] == ["-m", "tongs_agents.commands.chat"]
    assert call["name"] == "chat"


def test_submit_line_creates_channels_and_refuses_outside_the_maintainer():
    _grant()
    assert chat_panel.submit_line("/new ops", "general", lambda: None) == "Created #ops"
    assert chat_panel.submit_line("hi @lead", "ops", lambda: None) == (
        "Posted 1 to #ops, mentioned @lead"
    )
    with pytest.raises(ChatRefused, match="inside"):
        chat_panel.submit_line("hi", "ops", lambda: "inside an agent tree")
    with pytest.raises(ChatError):
        chat_panel.submit_line("/reply 99 nothing there", "ops", lambda: None)


@pytest.mark.asyncio
async def test_the_chat_pane_app_and_a_refused_panel():
    app = chat_command.ChatApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await _settle(app, pilot)
        panel = app.query_one(ChatPanel)
        assert panel.view is not None and not panel.view.available
    refused = ChatPanel(refusal=lambda: "refusing inside an agent session")

    class Host(chat_command.ChatApp):
        def compose(self):
            yield refused

    host = Host()
    async with host.run_test(size=(80, 24)) as pilot:
        await _settle(host, pilot)
        assert refused.view.detail == "refusing inside an agent session"
    assert not chat_store.exists()
