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

``:`` (or the palette's Command line) opens the command line
(``widgets.command_line``): ``run PROFILE [DIR] [-- ARGS]`` and
``resume [ID] [--profile NAME]`` with the CLI's syntax and checks, in this
session, with history and Tab completion; the result or the error shows
under the prompt and Escape closes it.

When its own pane changes size (the sidebar or the terminal was resized), it
asks the multiplexer, debounced and in a worker thread, to move the panes it
launched into this session onto the agent area again
(``Multiplexer.align_agents``: Zellij's pinned floating agent panes follow
the home pane). Other floating panes are never moved, and a failure is
ignored.

``u`` (or the palette's Resume in workspace) resumes the selected row's
harness session in this session; the palette's Resume session… lists the
recent sessions of the store to pick one (``commands.resume``). A session
whose process already runs in this session is not resumed again: the
sidebar offers to show its pane instead. One whose process runs elsewhere
is resumed only after a confirmation; that process is never stopped. While
a session is being resumed, another request for it is ignored.

``R`` renames the selected agent (``widgets.rename_prompt``): a display
name kept in its launch record and given to its pane, best effort.

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

from textual import events, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding

from tongs_agents import workspace_spawn
from tongs_agents.app_config import AppConfig, AppConfigError, load_app_config
from tongs_agents.commands import CommandParser
from tongs_agents.models import FleetSession, FocusResult, SessionStatus
from tongs_agents.multiplexers import (
    LAYOUT_OPTION,
    LAYOUTS,
    Multiplexer,
    MultiplexerError,
    PaneRef,
    get_multiplexer,
)
from tongs_agents.commands import resume as resume_command
from tongs_agents.palette import (
    COMMAND_LINE,
    RESUME_SELECTED,
    RESUME_SESSION,
    AppShell,
    PaletteCommand,
)
from tongs_agents.profiles import load_profiles
from tongs_agents.sessions_feed import LAUNCH_SOURCE, WorkspaceFeed
from tongs_agents.widgets.agents_panel import AgentsPanel, FocusOnSelect
from tongs_agents.widgets.approvals import APPROVAL_POLL_SECONDS, ApprovalActions
from tongs_agents.widgets.chat_panel import ChatActions, ChatPanel, ConfirmScreen
from tongs_agents.widgets.command_line import (
    CommandContext,
    CommandError,
    CommandLine,
    ResumeCheck,
    ResumeGuard,
    already_resuming,
    check_resume,
    start_resume,
)
from tongs_agents.widgets.quit_screen import DETACH, KILL, KillScreen, QuitScreen
from tongs_agents.widgets.rename_prompt import RenameActions, RenamePrompt
from tongs_agents.widgets.resume_picker import ResumePickerScreen
from tongs_agents.widgets.shell import AppFooter, AppHeader

NAME = "sidebar"
HELP = "Run the workspace sidebar inside a Zellij session"
EXIT_USAGE = 2
ALIGN_DELAY = 0.4


def add_arguments(parser):
    parser.add_argument(
        "--spawn-request", metavar="TOKEN", default=None,
        help="Consume this spawn request and start its agent panes",
    )
    parser.add_argument("--compact", action="store_true", help="Always use compact rows")
    parser.add_argument(
        LAYOUT_OPTION, choices=LAYOUTS, default=None,
        help="The session's layout; the workspace layout sets it so that panes can find "
             "the agent area again after its home pane was closed",
    )


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



class SidebarApp(AppShell, ChatActions, ApprovalActions, RenameActions, FocusOnSelect, App):
    """The workspace sidebar: a compact AgentsPanel, chat, access requests, spawns."""

    TITLE = "tongs-agents"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh", "Refresh", show=True, priority=True),
        Binding("plus", "add_agent", "Add agent", show=True),
        Binding("colon", "command_line", "Command", show=True, key_display=":"),
        Binding("u", "resume_selected", "Resume", show=False),
        Binding("R", "rename_agent", "Rename", show=True),
        Binding("n", "next_attention", "Next", show=True),
        Binding("m", "toggle_chat", "Chat", show=True),
        Binding("a", "review_access", "Access", show=True),
    ]

    agents_panel_id = "agents-panel"
    command_line_id = "command-line"

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
        align_delay: float = ALIGN_DELAY,
    ) -> None:
        super().__init__()
        self.init_shell(config, multiplexer, session)
        self.sub_title = session
        self.config_error = config_error
        self.multiplexer = multiplexer
        self.session = session
        self.leaving = False
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
        self.command_context = CommandContext(multiplexer, session, load_profiles)
        self.init_approvals(self.feed, approval_interval)
        self.align_delay = align_delay
        self._align_timer = None
        self._align_lock = threading.Lock()

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
        yield RenamePrompt(id=self.rename_prompt_id)
        yield CommandLine(self.command_context, id=self.command_line_id)
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

    def on_resize(self, event: events.Resize) -> None:
        if self._align_timer is not None:
            self._align_timer.stop()
        self._align_timer = self.set_timer(self.align_delay, self.align_agents)

    @work(thread=True, exclusive=True, group="align-agents")
    def align_agents(self) -> None:
        """Move this session's launched panes onto the agent area; best effort.

        An exclusive worker does not stop a thread that already runs, so runs
        are serialized: each reads the geometry after the previous one moved.
        """
        with self._align_lock:
            try:
                panes = self.feed.launched_panes()
                if panes:
                    self.multiplexer.align_agents(self.session, panes)
            except Exception:  # noqa: BLE001, S110 - never disturb the sidebar for a layout nicety
                pass

    def palette_commands(self) -> list[PaletteCommand]:
        commands = super().palette_commands()
        chat = len(self.chat_commands()) if self.chat_enabled else 0
        at = len(commands) - chat
        entry = PaletteCommand(
            COMMAND_LINE,
            f"Type a workspace verb (run PROFILE [DIR] [-- ARGS]) for session {self.session}",
            self.action_command_line,
        )
        resume = [
            PaletteCommand(
                RESUME_SELECTED,
                f"Resume the selected agent's session in a new pane of session {self.session}",
                self.action_resume_selected,
            ),
            PaletteCommand(
                RESUME_SESSION,
                f"Pick a recent session to resume in session {self.session}",
                self.action_resume_session,
            ),
        ]
        return [*commands[:at], entry, *resume, *self.approval_commands(), *commands[at:]]

    @property
    def command_line(self) -> CommandLine:
        return self.query_one(f"#{self.command_line_id}", CommandLine)

    def action_command_line(self) -> None:
        self.command_line.open()

    async def action_quit(self) -> None:
        """Every quit path (q, ctrl+q, the palette) leaves the whole workspace.

        The sidebar never quits on its own: it asks whether to detach (every
        agent keeps running) or to kill the session, after a second check.
        """
        if self.leaving or any(
            isinstance(screen, (QuitScreen, KillScreen)) for screen in self.screen_stack
        ):
            return
        self.push_screen(QuitScreen(self.session, self.multiplexer.name), self._quit_chosen)

    def get_system_commands(self, screen):
        for command in super().get_system_commands(screen):
            if command.callback == self.action_quit:
                command = SystemCommand(
                    command.title, "Leave the workspace: detach or kill it", self.action_quit
                )
            yield command

    def _quit_chosen(self, choice: str | None) -> None:
        if choice == DETACH:
            self._leave(DETACH)
        elif choice == KILL:
            self.push_screen(
                KillScreen(self.session), lambda yes: self._leave(KILL) if yes else None
            )

    def _leave(self, choice: str) -> None:
        self.leaving = True
        self._leave_worker(choice)

    @work(thread=True, exclusive=True, group="quit")
    def _leave_worker(self, choice: str) -> None:
        try:
            if choice == KILL:
                self.multiplexer.kill_session(self.session)
            else:
                self.multiplexer.detach(self.session)
        except MultiplexerError as error:
            self.call_from_thread(self._leave_failed, choice, error.detail)
            return
        if choice == KILL:
            self.call_from_thread(self.exit)
        else:
            self.call_from_thread(setattr, self, "leaving", False)

    def _leave_failed(self, choice: str, detail: str) -> None:
        self.leaving = False
        self.notify(detail, title=f"Could not {choice}", severity="error", timeout=15,
                    markup=False)

    def on_command_line_ran(self, event: CommandLine.Ran) -> None:
        if event.ok:
            self.agents_panel.poll()

    def on_command_line_closed(self, event: CommandLine.Closed) -> None:
        self.agents_panel.table.focus()

    # -- Resume --

    def action_resume_selected(self) -> None:
        session = self.agents_panel.selected_session()
        if session is None:
            self.notify("No agent selected", timeout=2)
            return
        if (session.source == LAUNCH_SOURCE
                or session.agent not in resume_command.RESUME_ARGS
                or not resume_command.SESSION_ID_RE.match(session.session_id)):
            self.notify(
                f"{session.repo or session.agent}: no harness session to resume yet",
                title="Resume", severity="warning", timeout=6,
            )
            return
        self.resume_in_workspace(session.session_id)

    @work(thread=True, exclusive=True, group="resume-list")
    def action_resume_session(self) -> None:
        sessions = resume_command.recent_sessions()
        self.call_from_thread(self._pick_resume, sessions)

    def _pick_resume(self, sessions: list[resume_command.Resumable]) -> None:
        if not sessions:
            self.notify("No sessions to resume in the session store", title="Resume",
                        timeout=5)
            return
        self.push_screen(
            ResumePickerScreen(sessions[:resume_command.LIST_LIMIT], self.session),
            self._resume_chosen,
        )

    def _resume_chosen(self, session_id: str | None) -> None:
        if session_id is not None:
            self.resume_in_workspace(session_id)

    @property
    def resuming(self) -> ResumeGuard:
        """Session ids being resumed, shared with the command line's ``:resume``."""
        return self.command_context.resuming

    def resume_in_workspace(self, session_id: str) -> None:
        """Check, confirm when needed, then resume ``session_id`` in this session.

        The id is held in ``resuming`` from before the check until the pane
        is spawned or the attempt ends; a request for an id already held
        (from ``u``, the picker or ``:resume``) is ignored with a notice.
        Each session id has its own exclusive worker groups, one for the
        check and one for the spawn.
        """
        if not self.resuming.acquire(session_id):
            self.notify(already_resuming(session_id)[0].upper()
                        + already_resuming(session_id)[1:], title="Resume", timeout=3)
            return
        self.run_worker(
            lambda: self._check_resume(session_id), thread=True,
            group=f"resume-check-{session_id}", exclusive=True,
        )

    def _check_resume(self, session_id: str) -> None:
        try:
            checked = check_resume(self.command_context, session_id)
        except CommandError as error:
            self.call_from_thread(self._resume_failed, session_id, str(error))
            return
        except Exception as error:  # noqa: BLE001 - report, never crash the sidebar
            self.call_from_thread(self._resume_failed, session_id,
                                  f"{type(error).__name__}: {error}")
            return
        self.call_from_thread(self._resume_checked, session_id, checked)

    def _resume_checked(self, session_id: str, checked: ResumeCheck) -> None:
        if checked.refusal is not None:
            self.resuming.release(session_id)
            self._offer_focus(session_id, checked)
            return
        if checked.warning is not None:
            self.push_screen(
                ConfirmScreen(
                    "Resume a session that still runs?",
                    f"{checked.plan.summary}\n\n{checked.warning[0].upper()}"
                    f"{checked.warning[1:]}.\n\nResume it here anyway?",
                ),
                lambda yes: self._resume_confirmed(session_id, checked, yes),
            )
            return
        self._start_resume(session_id, checked)

    def _resume_confirmed(self, session_id: str, checked: ResumeCheck, yes: bool) -> None:
        if yes:
            self._start_resume(session_id, checked)
        else:
            self.resuming.release(session_id)

    def _start_resume(self, session_id: str, checked: ResumeCheck) -> None:
        self.run_worker(
            lambda: self._run_resume(session_id, checked), thread=True,
            group=f"resume-{session_id}", exclusive=True,
        )

    def _run_resume(self, session_id: str, checked: ResumeCheck) -> None:
        try:
            # The confirmation dialog may have been open for a while: another
            # terminal could have resumed the same session in this workspace.
            again = check_resume(self.command_context, session_id)
            if again.refusal is not None:
                self.call_from_thread(self._resume_failed, session_id, again.refusal)
                return
            notices = start_resume(self.command_context, checked.plan)
        except CommandError as error:
            self.call_from_thread(self._resume_failed, session_id, str(error))
            return
        except Exception as error:  # noqa: BLE001 - report, never crash the sidebar
            self.call_from_thread(self._resume_failed, session_id,
                                  f"{type(error).__name__}: {error}")
            return
        self.call_from_thread(self._resumed, session_id, checked.plan.summary,
                              checked.warning, notices)

    def _live_row(self, session_id: str, live) -> FleetSession | None:
        """The row of the live process: its pid, else a row that has not ended."""
        rows = [session for session in self.agents_panel.sessions
                if session.session_id == session_id]
        if live is not None and live.pid is not None:
            for row in rows:
                if str(row.pid) == str(live.pid):
                    return row
        return next((row for row in rows if row.status != SessionStatus.ENDED), None)

    def _offer_focus(self, session_id: str, checked: ResumeCheck) -> None:
        """Refuse a duplicate and offer to show the pane the session already has."""
        row = self._live_row(session_id, checked.live)
        message = checked.refusal[0].upper() + checked.refusal[1:]
        if row is None:
            self.notify(message, title="Already running here", severity="warning",
                        timeout=10)
            return
        self.push_screen(
            ConfirmScreen("Already running here", f"{message}.\n\nShow its pane now?"),
            lambda yes: self._do_focus(row) if yes else None,
        )

    def _resume_failed(self, session_id: str, message: str) -> None:
        self.resuming.release(session_id)
        self.notify(message, title="Resume failed", severity="error", timeout=15)

    def _resumed(self, session_id: str, summary: str, warning: str | None,
                 notices: list[str] = ()) -> None:
        self.resuming.release(session_id)
        self.notify(f"Resumed {summary}", title="Resume", timeout=5)
        if warning is not None:
            self.notify(warning, title="Still running", severity="warning", timeout=20)
        self.notify_placement(notices)
        self.agents_panel.poll()

    @work(thread=True, exclusive=True, group="spawn-request")
    def consume_spawn_request(self) -> None:
        notices: list[str] = []
        failures = workspace_spawn.spawn_requested(
            self.multiplexer, self.session, self.token, self.feed.note_spawned,
            notices=notices,
        )
        self.call_from_thread(self._spawned, failures, notices)

    def _spawned(self, failures: list[str], notices: list[str] = ()) -> None:
        self.spawn_failures.extend(failures)
        for message in failures:
            self.notify(message, title="Spawn failed", severity="error", timeout=15)
        self.notify_placement(notices)
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
