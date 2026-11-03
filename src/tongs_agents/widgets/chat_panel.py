"""ChatPanel: the maintainer's view of agent chat, plus the sidebar's chat actions.

``ChatPanel`` shows a channel list, the message log of the selected channel
and an input. It reads and posts as ``maintainer`` through ``chat.store``, in
worker threads, after checking once that it does not run inside an agent
process tree (``chat.identity.maintainer_refusal``). Input lines post to the
selected channel; ``/reply ID text`` replies in the thread of message ID and
``/new NAME`` creates a channel. Message text is untrusted: it is shown as
plain text with control characters replaced, never as markup.

``ChatActions`` is the host mixin behind the sidebar's chat palette entries:
grant chat, grant chat and wake, revoke chat (each confirmed in a dialog that
names the pane, profile and launch id), post to chat and toggle the panel.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from tongs_agents.chat import frame, grants
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.identity import maintainer_refusal
from tongs_agents.chat.store import DEFAULT_CHANNEL, ChatError, ChatStore, Message

LOG_MESSAGES = 50
REPLY_RE = re.compile(r"/reply\s+(\d+)\s+(.+)\Z", re.DOTALL)
NEW_RE = re.compile(r"/new\s+(\S+)\s*\Z")
CHAT_OFF = "Chat is off until you grant a session (palette: Grant chat)."


@dataclass(frozen=True)
class ChatView:
    """One poll of the store, rendered by the panel on the UI thread."""

    available: bool
    channels: tuple[chat_store.Channel, ...] = ()
    channel: str = DEFAULT_CHANNEL
    messages: tuple[Message, ...] = ()
    grants: tuple[chat_store.Grant, ...] = ()
    detail: str = ""


def load_view(channel: str) -> ChatView:
    """Read the channels and the newest messages of ``channel`` as the maintainer."""
    if not chat_store.exists():
        return ChatView(False, channel=channel, detail=CHAT_OFF)
    with ChatStore.maintainer() as current:
        channels = tuple(current.channels())
        names = [item.name for item in channels]
        if channel not in names:
            channel = DEFAULT_CHANNEL if DEFAULT_CHANNEL in names else (names or [channel])[0]
        messages: tuple[Message, ...] = ()
        if channel in names:
            messages = tuple(current.messages(channel, after_id=0, limit=LOG_MESSAGES,
                                              latest=True))
            if messages:
                current.mark_read(channel, messages[-1].id)
        return ChatView(True, channels, channel, messages, tuple(current.grants()))


def status_line(view: ChatView) -> str:
    """``#channel | @handle, @other (tools pending)``: who is granted right now."""
    handles = [
        f"@{grant.handle}" + (
            " (tools pending)" if grant.tools_listed_at is None
            or grant.tools_listed_at < grant.granted_at else ""
        )
        for grant in view.grants
    ]
    return f"#{view.channel} | " + (", ".join(handles) if handles else "no grants")


def message_text(message: Message) -> Text:
    text = Text(no_wrap=False)
    text.append(f"{message.id} ", style="dim")
    if message.thread_id is not None:
        text.append(f"re {message.thread_id} ", style="dim cyan")
    style = "bold magenta" if message.author == chat_store.MAINTAINER else "bold"
    text.append(f"@{message.author_handle}", style=style)
    text.append(": ")
    text.append(frame.clean(message.body))
    return text


class ChatPanel(Widget):
    """Channel list, message log and input over the chat store."""

    DEFAULT_CSS = """
    ChatPanel {
        height: 1fr;
        layout: vertical;
    }
    ChatPanel > .chat-status {
        height: 1;
        background: $accent 15%;
        padding: 0 1;
    }
    ChatPanel > OptionList {
        height: auto;
        max-height: 6;
    }
    ChatPanel > RichLog {
        height: 1fr;
    }
    """

    def __init__(self, *, refresh_interval: float = 2.0,
                 refusal: Callable[[], str | None] = maintainer_refusal,
                 name: str | None = None, id: str | None = None,
                 classes: str | None = None) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.refresh_interval = refresh_interval
        self.refusal_check = refusal
        self.refusal: str | None = None
        self.checked = False
        self.channel = DEFAULT_CHANNEL
        self.view: ChatView | None = None
        self._shown: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static(CHAT_OFF, classes="chat-status", id="chat-status")
        yield OptionList(id="chat-channels")
        yield RichLog(id="chat-log", wrap=True, markup=False, highlight=False)
        yield Input(placeholder="Message (/reply ID text, /new NAME)", id="chat-input")

    @property
    def _live(self) -> bool:
        return self.is_mounted and self.is_attached

    def on_mount(self) -> None:
        self.set_interval(self.refresh_interval, self._tick)
        self._tick()

    def _tick(self) -> None:
        # A hidden panel reads nothing: the sidebar keeps it hidden by default.
        if self.display:
            self.poll()

    @work(thread=True, group="chat-poll", exclusive=True)
    def poll(self) -> None:
        if not self.checked:
            self.refusal = self.refusal_check()
            self.checked = True
        if self.refusal is not None:
            view = ChatView(False, channel=self.channel, detail=self.refusal)
        else:
            try:
                view = load_view(self.channel)
            except ChatError as error:
                view = ChatView(False, channel=self.channel, detail=error.detail)
        self.app.call_from_thread(self.show, view)

    def show(self, view: ChatView) -> None:
        if not self._live:
            return
        self.view = view
        self.channel = view.channel
        status = self.query_one("#chat-status", Static)
        if not view.available:
            status.update(Text(view.detail, overflow="ellipsis", no_wrap=True))
        else:
            status.update(Text(status_line(view), no_wrap=True, overflow="ellipsis"))
        key = (
            view.available, view.channel,
            tuple((item.name, item.unread) for item in view.channels),
            tuple(message.id for message in view.messages),
        )
        if key == self._shown:
            return
        self._shown = key
        channels = self.query_one("#chat-channels", OptionList)
        channels.clear_options()
        for item in view.channels:
            label = f"#{item.name}" + (f" ({item.unread})" if item.unread else "")
            channels.add_option(Option(label, id=item.name))
        names = [item.name for item in view.channels]
        if view.channel in names:
            channels.highlighted = names.index(view.channel)
        log = self.query_one("#chat-log", RichLog)
        log.clear()
        for message in view.messages:
            log.write(message_text(message))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option.id and event.option.id != self.channel:
            self.channel = event.option.id
            self._shown = None
            self.poll()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        event.stop()
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.submit(text)

    @work(thread=True, group="chat-post")
    def submit(self, text: str) -> None:
        try:
            message = submit_line(text, self.channel, self.refusal_check)
        except ChatError as error:
            self.app.call_from_thread(self.app.notify, error.detail, title="Chat",
                                      severity="error", timeout=8)
            return
        self.app.call_from_thread(self.app.notify, message, timeout=3)
        self.app.call_from_thread(self.poll)


def submit_line(text: str, channel: str,
                refusal: Callable[[], str | None] = maintainer_refusal) -> str:
    """Post one input line as the maintainer; returns a short confirmation."""
    reason = refusal()
    if reason is not None:
        raise chat_store.ChatRefused(reason)
    created = NEW_RE.match(text)
    if created:
        with ChatStore.maintainer() as current:
            current.create_channel(created.group(1))
        return f"Created #{created.group(1)}"
    thread = None
    reply = REPLY_RE.match(text)
    if reply:
        thread, text = int(reply.group(1)), reply.group(2)
    with ChatStore.maintainer() as current:
        posted = current.post(channel, text, thread=thread)
    mentioned = f", mentioned {', '.join('@' + name for name in posted.mentioned)}" \
        if posted.mentioned else ""
    return f"Posted {posted.id} to #{posted.channel}{mentioned}"


class ConfirmScreen(ModalScreen[bool]):
    """Yes or no on one chat action; the message names what it acts on."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    ConfirmScreen > Vertical {
        width: 70;
        max-width: 100%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfirmScreen Horizontal {
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.title_text, style="bold"))
            yield Static(Text(self.message))
            with Horizontal():
                yield Button("Yes", id="yes", variant="primary")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class PostScreen(ModalScreen[str | None]):
    """One line to post as the maintainer."""

    DEFAULT_CSS = """
    PostScreen {
        align: center middle;
    }
    PostScreen > Vertical {
        width: 70;
        max-width: 100%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, channel: str) -> None:
        super().__init__()
        self.channel = channel

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Post to #{self.channel} as @maintainer", style="bold"))
            yield Input(placeholder="Message (/reply ID text)", id="post-input")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ChatActions:
    """Host mixin: chat grant, revoke and post actions of the sidebar.

    The host provides ``agents_panel`` (``FocusOnSelect``) and mounts a
    ``ChatPanel`` with id ``chat_panel_id``. Grants are written only after the
    maintainer confirms the dialog, with ``via = "sidebar"``.
    """

    chat_panel_id = "chat-panel"
    chat_enabled = True
    chat_refusal: Callable[[], str | None] = staticmethod(maintainer_refusal)

    @property
    def chat_panel(self) -> ChatPanel:
        return self.query_one(f"#{self.chat_panel_id}", ChatPanel)

    def action_toggle_chat(self) -> None:
        panel = self.chat_panel
        panel.display = not panel.display
        if panel.display:
            panel.poll()
            panel.query_one("#chat-input", Input).focus()

    def _selected_launch(self):
        session = self.agents_panel.selected_session()
        if session is None or not session.launch_id:
            self.notify("Select an agent pane tongs-agents launched", title="Chat",
                        timeout=4)
            return None
        return session

    def _chat_failed(self, detail: str) -> None:
        self.notify(detail, title="Chat", severity="error", timeout=10)

    def chat_grant(self, wake: bool = False) -> None:
        session = self._selected_launch()
        if session is not None:
            self._chat_prepare(session.launch_id, wake)

    @work(thread=True, group="chat-grant")
    def _chat_prepare(self, launch_id: str, wake: bool) -> None:
        reason = self.chat_refusal()
        if reason is not None:
            self.call_from_thread(self._chat_failed, reason)
            return
        try:
            request = grants.prepare(launch_id, wake=wake, actor="sidebar")
        except ChatError as error:
            self.call_from_thread(self._chat_failed, error.detail)
            return
        title = "Grant chat and wake?" if wake else "Grant chat?"
        message = request.describe()
        if request.notes:
            message += "\n" + "\n".join(request.notes)

        def answered(confirmed: bool | None) -> None:
            if confirmed:
                self._chat_apply(request)

        self.call_from_thread(self.push_screen, ConfirmScreen(title, message), answered)

    @work(thread=True, group="chat-grant")
    def _chat_apply(self, request: grants.GrantRequest) -> None:
        try:
            grant = grants.apply(request, via="sidebar")
        except ChatError as error:
            self.call_from_thread(self._chat_failed, error.detail)
            return
        self.call_from_thread(
            self.notify, f"Chat granted to @{grant.handle} ({grant.level})", timeout=4,
        )

    def chat_revoke(self) -> None:
        session = self._selected_launch()
        if session is not None:
            self._chat_revoke_prepare(session.launch_id)

    @work(thread=True, group="chat-grant")
    def _chat_revoke_prepare(self, launch_id: str) -> None:
        reason = self.chat_refusal()
        if reason is not None:
            self.call_from_thread(self._chat_failed, reason)
            return
        grant = None
        try:
            if chat_store.exists():
                with ChatStore.maintainer() as current:
                    grant = current.grant_for(launch_id)
        except ChatError as error:
            self.call_from_thread(self._chat_failed, error.detail)
            return
        if grant is None:
            self.call_from_thread(self.notify, "That pane has no chat grant", timeout=4)
            return
        message = (
            f"pane {grant.pane_session}:{grant.pane_id}, profile {grant.profile}, "
            f"launch {grant.launch_id}, handle @{grant.handle}"
        )

        def answered(confirmed: bool | None) -> None:
            if confirmed:
                self._chat_revoke_apply(launch_id)

        self.call_from_thread(self.push_screen, ConfirmScreen("Revoke chat?", message),
                              answered)

    @work(thread=True, group="chat-grant")
    def _chat_revoke_apply(self, launch_id: str) -> None:
        try:
            removed = grants.revoke(launch_id, actor="sidebar")
        except ChatError as error:
            self.call_from_thread(self._chat_failed, error.detail)
            return
        self.call_from_thread(
            self.notify, "Chat revoked" if removed else "That pane has no chat grant",
            timeout=4,
        )

    def chat_post_prompt(self) -> None:
        channel = self.chat_panel.channel

        def answered(text: str | None) -> None:
            if text:
                self.chat_panel.submit(text)

        self.push_screen(PostScreen(channel), answered)

    @work(thread=True, group="chat-housekeeping")
    def chat_housekeeping(self) -> None:
        """At start: drop stale grants and messages past retention."""
        if not chat_store.exists() or self.chat_refusal() is not None:
            return
        try:
            removed = grants.remove_stale(actor="sidebar")
            with ChatStore.maintainer() as current:
                current.prune()
        except ChatError as error:
            self.call_from_thread(self._chat_failed, error.detail)
            return
        if removed:
            names = ", ".join(f"@{grant.handle}" for grant in removed)
            self.call_from_thread(self.notify, f"Removed stale chat grants: {names}",
                                  title="Chat", timeout=6)
