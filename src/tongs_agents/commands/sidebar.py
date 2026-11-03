"""``tongs-agents sidebar``: the workspace sidebar, run in a Zellij pane.

It hosts a compact AgentsPanel over a WorkspaceFeed, so every pane the
launcher started in this session is listed, with hook status when a hook
record carries its launch id and pane state otherwise. With
``--spawn-request TOKEN`` it consumes that request on mount and spawns its
pending launches; failures become notifications. Selecting a row focuses its
pane: hook rows through ``focus.focus_session`` (its in-session branch), and
hookless rows by selecting their pane directly after checking it is listed.

``+`` (or the palette's Add agent…) opens the Add agent dialog: a profile, a
folder with path completion and optional extra arguments, started through
the same spawn path as ``tongs-agents run``.

Chat (docs/chat.md): ``m`` shows or hides a ChatPanel under the agents, and
the palette offers Grant chat, Grant chat and wake, Revoke chat (for the
selected launched pane, each confirmed in a dialog), Post to chat and Open
chat pane. At start it removes stale grants and messages past retention.

OpenShell access requests (docs/sandbox.md, #37): every few seconds it reads
the pending network rules of the sandboxes its rows run in; a row with
requests reads as waiting (NEEDS-YOU, desktop notification). ``a`` or the
palette's Review access requests shows the full proposals; approving takes a
second confirmation and rejecting a reason. Nothing is approved otherwise.

It reads ``[ui]`` (refresh interval, notify level, theme) and ``[workspace]``
(default profiles first in the palette, ``tongs_tab``) from the config file.
An invalid config file does not stop it: the request is still consumed, with
defaults and an error notification.

The layout starts it as ``python -m tongs_agents.commands.sidebar``.
"""

from __future__ import annotations

import os
import sys
import threading

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding

from tongs_agents import workspace_spawn
from tongs_agents.app_config import AppConfig, AppConfigError, load_app_config
from tongs_agents.commands import CommandParser
from tongs_agents.models import FleetSession, FocusResult
from tongs_agents.multiplexers import (
    Multiplexer,
    MultiplexerError,
    PaneRef,
    get_multiplexer,
)
from tongs_agents.palette import AppShell, PaletteCommand
from tongs_agents.sessions_feed import LAUNCH_SOURCE, WorkspaceFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect
from tongs_agents.widgets.approvals import APPROVAL_POLL_SECONDS, ApprovalActions
from tongs_agents.widgets.chat_panel import ChatActions, ChatPanel
from tongs_agents.widgets.shell import AppFooter, AppHeader

NAME = "sidebar"
HELP = "Run the workspace sidebar inside a Zellij session"
EXIT_USAGE = 2


def add_arguments(parser):
    parser.add_argument(
        "--spawn-request", metavar="TOKEN", default=None,
        help="Consume this spawn request and start its agent panes",
    )
    parser.add_argument("--compact", action="store_true", help="Always use compact rows")


def focus_pane(mux: Multiplexer, ref: PaneRef) -> FocusResult:
    """Select ``ref`` through the multiplexer when it is still listed in its session."""
    try:
        panes = mux.list_panes(ref.session)
    except MultiplexerError as error:
        return FocusResult(state="failed", reason=error.detail, backend=mux.name)
    if not any(pane.ref.pane_id == ref.pane_id for pane in panes):
        return FocusResult(state="not-found", reason=f"pane {ref.pane_id} is gone")
    try:
        return mux.focus(ref)
    except MultiplexerError as error:
        return FocusResult(state="failed", reason=error.detail, backend=mux.name)


def focus_launch_pane(mux: Multiplexer, current_session: str,
                      session: FleetSession) -> FocusResult:
    """Select a hookless row's pane when it is listed in the caller's session."""
    env = session.terminal_env
    name = env.get("ZELLIJ_SESSION_NAME")
    pane_id = env.get("ZELLIJ_PANE_ID")
    socket_dir = env.get("ZELLIJ_SOCKET_DIR") or None
    if name != current_session or not pane_id:
        return FocusResult(
            state="unavailable", reason=f"the pane is not in this {mux.name} session",
        )
    return focus_pane(mux, PaneRef(mux.name, name, pane_id, socket_dir))


def focus_launched_row(mux: Multiplexer, feed: WorkspaceFeed,
                       session: FleetSession) -> FocusResult:
    """Select the pane of the launch a hook or notify row names (tmux rows)."""
    ref = feed.launch_pane(session.launch_id)
    if ref is None:
        return FocusResult(
            state="not-found", reason="the launch's pane is no longer recorded",
        )
    return focus_pane(mux, ref)


class SidebarApp(AppShell, ChatActions, ApprovalActions, FocusOnSelect, App):
    """The workspace sidebar: a compact AgentsPanel, chat, access requests, spawns."""

    TITLE = "tongs-agents"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("plus", "add_agent", "Add agent", show=True),
        Binding("n", "next_attention", "Next", show=True),
        Binding("m", "toggle_chat", "Chat", show=True),
        Binding("a", "review_access", "Access", show=True),
    ]

    agents_panel_id = "agents-panel"

    def __init__(
        self,
        multiplexer: Multiplexer,
        session: str,
        *,
        token: str | None = None,
        compact: bool = True,
        feed: WorkspaceFeed | None = None,
        refresh_interval: float | None = None,
        socket_dir: str | None = None,
        config: AppConfig | None = None,
        config_error: str | None = None,
        approval_interval: float = APPROVAL_POLL_SECONDS,
    ) -> None:
        super().__init__()
        self.init_shell(config, multiplexer, session)
        self.sub_title = session
        self.config_error = config_error
        self.multiplexer = multiplexer
        self.session = session
        self.token = token
        self.compact = compact
        self.feed = feed if feed is not None else WorkspaceFeed(
            session, multiplexer, socket_dir=socket_dir
        )
        ui = self.app_config.ui
        self.refresh_interval = (
            ui.refresh_interval if refresh_interval is None else refresh_interval
        )
        self.notify_level = ui.notify_level
        self.spawn_failures: list[str] = []
        self.init_approvals(self.feed, approval_interval)

    def compose(self) -> ComposeResult:
        yield AppHeader()
        yield AgentsPanel(
            self.approval_feed,
            refresh_interval=self.refresh_interval,
            notify_level=self.notify_level,
            compact=self.compact,
            id=self.agents_panel_id,
        )
        chat = ChatPanel(refresh_interval=max(self.refresh_interval, 1.0),
                         refusal=self.chat_refusal, id=self.chat_panel_id)
        chat.display = False
        yield chat
        yield AppFooter()

    def on_mount(self) -> None:
        self.mount_shell()
        if self.config_error is not None:
            self.notify(f"{self.config_error}; using defaults", title="Config",
                        severity="error", timeout=15)
        if self.token is not None:
            self.consume_spawn_request()
        self.chat_housekeeping()
        self.mount_approvals()

    def palette_commands(self) -> list[PaletteCommand]:
        commands = super().palette_commands()
        chat = len(self.chat_commands()) if self.chat_enabled else 0
        at = len(commands) - chat
        return [*commands[:at], *self.approval_commands(), *commands[at:]]

    @work(thread=True, exclusive=True, group="spawn-request")
    def consume_spawn_request(self) -> None:
        failures = workspace_spawn.spawn_requested(
            self.multiplexer, self.session, self.token, self.feed.note_spawned,
        )
        self.call_from_thread(self._spawned, failures)

    def _spawned(self, failures: list[str]) -> None:
        self.spawn_failures.extend(failures)
        for message in failures:
            self.notify(message, title="Spawn failed", severity="error", timeout=15)
        self.agents_panel.poll()

    def _do_focus(self, session: FleetSession) -> None:
        """Launch rows, and on tmux every launched row, focus through the multiplexer.

        On tmux a hook or notify row replaces its launch row; it is selected
        through the launch's pane, never through the terminal API, so no
        emulator window is raised for a pane in the sidebar's own session.
        """
        through_launch = self.multiplexer.name == "tmux" and bool(session.launch_id)
        if session.source != LAUNCH_SOURCE and not through_launch:
            super()._do_focus(session)
            return

        def _run():
            try:
                if session.source == LAUNCH_SOURCE:
                    result = focus_launch_pane(self.multiplexer, self.session, session)
                else:
                    result = focus_launched_row(self.multiplexer, self.feed, session)
            except Exception as error:  # noqa: BLE001 - report, never crash the sidebar
                result = FocusResult(state="failed", reason=str(error))
            self.call_from_thread(self._notify_focus_result, session.repo, result)

        threading.Thread(target=_run, daemon=True).start()


def load_config_or_defaults() -> tuple[AppConfig, str | None]:
    """The app config, or the defaults plus the error when the file is invalid."""
    try:
        return load_app_config(), None
    except AppConfigError as error:
        return AppConfig(), str(error)


def run(args) -> int:
    token = args.spawn_request
    if token is not None and not workspace_spawn.is_token(token):
        print(f"tongs-agents sidebar: invalid spawn request token {token!r:.80}",
              file=sys.stderr)
        return EXIT_USAGE
    in_tmux = os.environ.get("ZELLIJ") is None and bool(os.environ.get("TMUX"))
    mux = get_multiplexer("tmux" if in_tmux else "zellij")
    here = mux.in_session()
    if here is None:
        print(
            "tongs-agents sidebar: run it inside a Zellij session "
            "('tongs-agents workspace' starts one)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    config, config_error = load_config_or_defaults()
    SidebarApp(
        mux, here.session, token=token, compact=args.compact or config.ui.compact,
        socket_dir=here.socket_dir, config=config, config_error=config_error,
    ).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = CommandParser(prog="tongs-agents sidebar", description=HELP)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
