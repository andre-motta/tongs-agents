"""OpenShell access requests in the sidebar: needs-you rows, review, approve, reject (#37).

``ApprovalFeed`` wraps the sidebar's session feed: a row whose launch runs in
a sandbox with pending access requests (``rule get --status pending``, read
by ``sandbox.approvals``) reads as waiting, so it counts as NEEDS-YOU, jumps
with ``n`` and raises the panel's desktop notification.

``ApprovalActions`` is the host mixin. It polls the sandboxes of the listed
rows in a worker thread, notifies once per new request, and on ``a`` (or the
palette's "Review access requests") opens ``ApprovalScreen`` with the full
proposal text, Security and Prover lines included. Approving needs a second
yes in a confirmation dialog; rejecting needs a reason. Output the parser
does not recognise is shown raw and cannot be approved here. Polling only
reads; nothing approves without those key presses (row 14), and there is no
approve-all.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from tongs_agents.attention import with_access_requests
from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.palette import PaletteCommand
from tongs_agents.sandbox import approvals
from tongs_agents.sandbox import state as sandbox_state
from tongs_agents.sandbox.approvals import ApprovalError, Proposal, RuleListing
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError
from tongs_agents.widgets.chat_panel import ConfirmScreen

Key = tuple[str | None, str]

APPROVAL_POLL_SECONDS = 5.0
REVIEW_ACCESS = "Review access requests"
NOTE_LIMIT = 160
FLAG_LABELS = ("Security:", "Prover:", "Validation:", "Application:")


def launch_sandboxes() -> dict[str, Key]:
    """Launch id to ``(gateway, sandbox)`` for every recorded sandboxed launch; never raises."""
    try:
        return {record.launch_id: (record.gateway, record.sandbox)
                for record in sandbox_state.list_records()}
    except (OSError, ValueError):
        return {}


def overlay(sessions: list[FleetSession], targets: Mapping[str, Key],
            listings: Mapping[Key, RuleListing]) -> list[FleetSession]:
    """Mark each row whose sandbox has pending access requests as needing you."""
    marked = []
    for session in sessions:
        listing = listings.get(targets.get(session.launch_id)) if session.launch_id else None
        if listing is not None and listing.needs_you:
            session = with_access_requests(session, len(listing.proposals),
                                           unreadable=listing.parse_error is not None)
        marked.append(session)
    return marked


class ApprovalFeed:
    """A session feed whose sandboxed rows carry their pending access requests."""

    def __init__(self, inner, listings: Callable[[], Mapping[Key, RuleListing]],
                 launches: Callable[[], Mapping[str, Key]] = launch_sandboxes) -> None:
        self.inner = inner
        self._listings = listings
        self._launches = launches
        self.launch_targets: dict[str, Key] = {}

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def snapshot(self) -> list[FleetSession]:
        sessions = self.inner.snapshot()
        launches = self._launches()
        self.launch_targets = {
            session.launch_id: launches[session.launch_id] for session in sessions
            if session.launch_id in launches and session.status is not SessionStatus.ENDED
        }
        return overlay(sessions, self.launch_targets, self._listings())

    def targets(self) -> set[Key]:
        return set(self.launch_targets.values())


def proposal_text(proposal: Proposal) -> Text:
    text = Text()
    for index, line in enumerate(proposal.text.split("\n")):
        if index:
            text.append("\n")
        style = "bold yellow" if line.startswith(FLAG_LABELS) else None
        text.append(line, style=style)
    return text


def _short(value: str, limit: int = NOTE_LIMIT) -> str:
    return value if len(value) <= limit else value[:limit - 3] + "..."


class ApprovalScreen(ModalScreen[tuple[str, Proposal] | None]):
    """The pending access requests of one sandbox; dismisses with the chosen action."""

    DEFAULT_CSS = """
    ApprovalScreen {
        align: center middle;
    }
    ApprovalScreen > Vertical {
        width: 100;
        max-width: 100%;
        height: auto;
        max-height: 100%;
        border: thick $warning;
        background: $surface;
        padding: 0 1;
    }
    ApprovalScreen OptionList {
        height: auto;
        max-height: 6;
    }
    ApprovalScreen VerticalScroll {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [
        Binding("a", "decide('approve')", "Approve"),
        Binding("d", "decide('reject')", "Reject"),
        Binding("escape", "close", "Close"),
    ]

    def __init__(self, listing: RuleListing) -> None:
        super().__init__()
        self.listing = listing

    def compose(self) -> ComposeResult:
        listing = self.listing
        where = f" (gateway {listing.gateway})" if listing.gateway else ""
        with Vertical():
            yield Static(Text(f"Access requests: sandbox {listing.sandbox}{where}",
                              style="bold"))
            if listing.failure is not None:
                yield Static(Text(listing.failure, style="red"), id="access-failure")
            elif listing.parse_error is not None:
                yield Static(Text(
                    f"tongs-agents cannot read this output ({listing.parse_error}), so it "
                    "cannot approve from here. Review it and decide with openshell rule "
                    "approve or reject yourself.", style="bold red"), id="access-unreadable")
                with VerticalScroll():
                    yield Static(Text(listing.raw), id="access-raw")
            elif not listing.proposals:
                yield Static("No pending access requests.", id="access-empty")
            else:
                yield OptionList(*[
                    Option(Text(("! " if proposal.flagged else "  ")
                                + _short(proposal.summary(), 90)), id=proposal.chunk_id)
                    for proposal in listing.proposals
                ], id="access-list")
                with VerticalScroll():
                    yield Static(proposal_text(listing.proposals[0]), id="access-detail")
            yield Static(Text("a approve   d reject (reason required)   esc close",
                              style="dim"))

    def on_mount(self) -> None:
        if self.listing.proposals:
            options = self.query_one("#access-list", OptionList)
            options.highlighted = 0
            options.focus()

    def selected(self) -> Proposal | None:
        if not self.listing.proposals:
            return None
        index = self.query_one("#access-list", OptionList).highlighted
        if index is None or not 0 <= index < len(self.listing.proposals):
            return None
        return self.listing.proposals[index]

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        event.stop()
        proposal = self.listing.proposal(event.option.id or "")
        if proposal is not None:
            self.query_one("#access-detail", Static).update(proposal_text(proposal))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()

    def action_decide(self, action: str) -> None:
        proposal = self.selected()
        if proposal is None:
            self.notify("Nothing to decide here", timeout=3)
            return
        self.dismiss((action, proposal))

    def action_close(self) -> None:
        self.dismiss(None)


class ReasonScreen(ModalScreen[str | None]):
    """The reason for one rejection; an empty reason is refused."""

    DEFAULT_CSS = """
    ReasonScreen {
        align: center middle;
    }
    ReasonScreen > Vertical {
        width: 80;
        max-width: 100%;
        height: auto;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, sandbox: str, proposal: Proposal) -> None:
        super().__init__()
        self.sandbox = sandbox
        self.proposal = proposal

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Reject {_short(self.proposal.summary(), 60)} for "
                              f"{self.sandbox}?", style="bold"))
            yield Input(placeholder="Reason (required)", id="reject-reason",
                        max_length=approvals.MAX_REASON)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        try:
            reason = approvals.check_reason(event.value)
        except ApprovalError as error:
            self.notify(str(error), severity="error", timeout=4)
            return
        self.dismiss(reason)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ApprovalActions:
    """Host mixin: poll, show and decide OpenShell access requests.

    The host calls ``init_approvals(feed)`` from ``__init__`` (and gives the
    returned ``ApprovalFeed`` to its AgentsPanel) and ``mount_approvals()``
    from ``on_mount``, and provides ``agents_panel``.
    """

    approval_client_for: Callable[[str | None], OpenShell] | None = None

    def init_approvals(self, feed, interval: float = APPROVAL_POLL_SECONDS) -> ApprovalFeed:
        self.approval_listings: dict[Key, RuleListing] = {}
        self.approval_interval = interval
        self.approval_feed = ApprovalFeed(feed, lambda: self.approval_listings)
        return self.approval_feed

    def mount_approvals(self) -> None:
        self.set_interval(self.approval_interval, self.poll_approvals)

    def approval_commands(self) -> list[PaletteCommand]:
        return [PaletteCommand(REVIEW_ACCESS,
                               "Approve or reject a sandbox's pending network access",
                               self.action_review_access)]

    @work(thread=True, exclusive=True, group="approvals")
    def poll_approvals(self) -> None:
        targets = self.approval_feed.targets()
        if not targets and not self.approval_listings:
            return
        listings = approvals.poll(targets, self.approval_client_for)
        self.call_from_thread(self._approvals_polled, listings)

    def _approvals_polled(self, listings: dict[Key, RuleListing]) -> None:
        before = self.approval_listings
        self.approval_listings = listings
        fresh = approvals.new_proposals(before, listings)
        for listing, proposal in fresh:
            flag = " (flagged)" if proposal.flagged else ""
            self.notify(f"{listing.sandbox}: {_short(proposal.summary())}{flag}; a to review",
                        title="Access request", severity="warning", timeout=10,
                        markup=False)
        if fresh:
            self.bell()
        for key, listing in listings.items():
            previous = before.get(key)
            if listing.failure and (previous is None or previous.failure != listing.failure):
                self.notify(_short(listing.failure, 300), title="Access requests",
                            severity="warning", timeout=8, markup=False)
            if listing.parse_error and (previous is None
                                        or previous.parse_error != listing.parse_error):
                self.notify(f"{listing.sandbox}: unreadable rule output; a to review",
                            title="Access requests", severity="error", timeout=10,
                            markup=False)
        if _shape(before) != _shape(listings):
            self.agents_panel.poll()

    def _review_listing(self) -> RuleListing | None:
        session = self.agents_panel.selected_session()
        key = None
        if session is not None and session.launch_id:
            key = self.approval_feed.launch_targets.get(session.launch_id)
        if key is not None:
            return self.approval_listings.get(key) or RuleListing(key[1], key[0])
        return next((listing for listing in self.approval_listings.values()
                     if listing.needs_you), None)

    def action_review_access(self) -> None:
        listing = self._review_listing()
        if listing is None:
            self.notify("No pending access requests", timeout=3)
            return

        def decided(choice: tuple[str, Proposal] | None) -> None:
            if choice is not None:
                self._access_decided(listing, *choice)

        self.push_screen(ApprovalScreen(listing), decided)

    def _access_decided(self, listing: RuleListing, action: str, proposal: Proposal) -> None:
        if action == "approve":
            def confirmed(answer: bool | None) -> None:
                if answer:
                    self.access_approve(listing.key, proposal)

            message = f"Sandbox {listing.sandbox}\n{proposal.text}"
            self.push_screen(ConfirmScreen("Approve this network access?", message),
                             confirmed)
            return

        def reasoned(reason: str | None) -> None:
            if reason:
                self.access_reject(listing.key, proposal, reason)

        self.push_screen(ReasonScreen(listing.sandbox, proposal), reasoned)

    def _access_client(self, gateway: str | None) -> OpenShell:
        factory = self.approval_client_for or (lambda name: OpenShell(gateway=name))
        return factory(gateway)

    @work(thread=True, group="approval-decision")
    def access_approve(self, key: Key, proposal: Proposal) -> None:
        gateway, sandbox = key
        try:
            message = approvals.approve(self._access_client(gateway), sandbox, proposal)
        except (ApprovalError, OpenShellError) as error:
            self.call_from_thread(self._access_failed, str(error))
            return
        self.call_from_thread(self._access_done, f"{sandbox}: {message}")

    @work(thread=True, group="approval-decision")
    def access_reject(self, key: Key, proposal: Proposal, reason: str) -> None:
        gateway, sandbox = key
        try:
            message = approvals.reject(self._access_client(gateway), sandbox, proposal, reason)
        except (ApprovalError, OpenShellError) as error:
            self.call_from_thread(self._access_failed, str(error))
            return
        self.call_from_thread(self._access_done, f"{sandbox}: {message}")

    def _access_failed(self, detail: str) -> None:
        self.notify(_short(detail, 300), title="Access request", severity="error", timeout=10,
                    markup=False)

    def _access_done(self, message: str) -> None:
        self.notify(_short(message, 300), title="Access request", timeout=5, markup=False)
        self.poll_approvals()


def _shape(listings: Mapping[Key, RuleListing]) -> dict:
    return {key: (tuple(p.chunk_id for p in listing.proposals), listing.parse_error is None)
            for key, listing in listings.items() if listing.needs_you}
