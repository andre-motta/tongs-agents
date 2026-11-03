"""``tongs-agents sidebar``: the workspace sidebar, run in a Zellij pane.

It hosts a compact AgentsPanel over a WorkspaceFeed, so every pane the
launcher started in this session is listed, with hook status when a hook
record carries its launch id and pane state otherwise. With
``--spawn-request TOKEN`` it consumes that request on mount and spawns its
pending launches; failures become notifications. Selecting a row focuses its
pane: hook rows through ``focus.focus_session`` (its in-session branch), and
hookless rows by selecting their pane directly after checking it is listed.

The layout starts it as ``python -m tongs_agents.commands.sidebar``.
"""

from __future__ import annotations

import argparse
import sys
import threading

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from tongs_agents import workspace_spawn
from tongs_agents.models import FleetSession, FocusResult
from tongs_agents.multiplexers import (
    Multiplexer,
    MultiplexerError,
    PaneRef,
    get_multiplexer,
)
from tongs_agents.sessions_feed import LAUNCH_SOURCE, WorkspaceFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect

NAME = "sidebar"
HELP = "Run the workspace sidebar inside a Zellij session"
EXIT_USAGE = 2


def add_arguments(parser):
    parser.add_argument(
        "--spawn-request", metavar="TOKEN", default=None,
        help="Consume this spawn request and start its agent panes",
    )
    parser.add_argument("--compact", action="store_true", help="Always use compact rows")


def focus_launch_pane(mux: Multiplexer, current_session: str,
                      session: FleetSession) -> FocusResult:
    """Select a hookless row's pane when it is listed in the caller's session."""
    env = session.terminal_env
    name = env.get("ZELLIJ_SESSION_NAME")
    pane_id = env.get("ZELLIJ_PANE_ID")
    socket_dir = env.get("ZELLIJ_SOCKET_DIR") or None
    if name != current_session or not pane_id:
        return FocusResult(
            state="unavailable", reason="the pane is not in this zellij session",
        )
    try:
        panes = mux.list_panes(name)
    except MultiplexerError as error:
        return FocusResult(state="failed", reason=error.detail, backend=mux.name)
    if not any(pane.ref.pane_id == pane_id for pane in panes):
        return FocusResult(state="not-found", reason=f"pane {pane_id} is gone")
    try:
        return mux.focus(PaneRef(mux.name, name, pane_id, socket_dir))
    except MultiplexerError as error:
        return FocusResult(state="failed", reason=error.detail, backend=mux.name)


class SidebarApp(FocusOnSelect, App):
    """The workspace sidebar: a compact AgentsPanel plus spawn-on-mount."""

    TITLE = "tongs-agents"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("n", "next_attention", "Next", show=True),
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
        refresh_interval: float = 2,
    ) -> None:
        super().__init__()
        self.multiplexer = multiplexer
        self.session = session
        self.token = token
        self.compact = compact
        self.feed = feed if feed is not None else WorkspaceFeed(session, multiplexer)
        self.refresh_interval = refresh_interval
        self.spawn_failures: list[str] = []

    def compose(self) -> ComposeResult:
        yield AgentsPanel(
            self.feed,
            refresh_interval=self.refresh_interval,
            compact=self.compact,
            id=self.agents_panel_id,
        )
        yield Footer()

    def on_mount(self) -> None:
        if self.token is not None:
            self.consume_spawn_request()

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
        if session.source != LAUNCH_SOURCE:
            super()._do_focus(session)
            return

        def _run():
            try:
                result = focus_launch_pane(self.multiplexer, self.session, session)
            except Exception as error:  # noqa: BLE001 - report, never crash the sidebar
                result = FocusResult(state="failed", reason=str(error))
            self.call_from_thread(self._notify_focus_result, session.repo, result)

        threading.Thread(target=_run, daemon=True).start()


def run(args) -> int:
    token = args.spawn_request
    if token is not None and not workspace_spawn.is_token(token):
        print(f"tongs-agents sidebar: invalid spawn request token {token!r:.80}",
              file=sys.stderr)
        return EXIT_USAGE
    mux = get_multiplexer("zellij")
    here = mux.in_session()
    if here is None:
        print(
            "tongs-agents sidebar: run it inside a Zellij session "
            "('tongs-agents workspace' starts one)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    SidebarApp(mux, here.session, token=token, compact=args.compact).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tongs-agents sidebar", description=HELP)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
