"""OpenShell access requests in the sidebar and CLI (#37): parser, decisions, guards.

The parser runs on the text fixtures in tests/fixtures/openshell_rules (built
from upstream ``sandbox_draft_get`` at ba302a6, not captured live) and every
OpenShell call on the fake CLI (instrumented). The live round trip is pending
the maintainer's run (docs/work/openshell-sandbox.md, row 21).
"""

import ast
import builtins
import dataclasses
import io
import json
import time
import uuid
from pathlib import Path

import pytest

from tests.test_sandbox_support import (  # noqa: F401
    FAKE_OPENSHELL,
    LAUNCH_ID,
    create_sandbox,
    fake,
    posix_only,
)
from tests.test_sandbox_validation import run_validator
from tests.test_workspace_spawn import FakeMux
from tongs_agents import cli
from tongs_agents.attention import (
    ACCESS_TOOL,
    Attention,
    attention_state,
    with_access_requests,
)
from tongs_agents.commands import sandbox as sandbox_command
from tongs_agents.commands import sidebar
from tongs_agents.models import FleetSession, SessionStatus
from tongs_agents.sandbox import approvals, state
from tongs_agents.sandbox.approvals import ApprovalError, ParseError, RuleListing
from tongs_agents.sandbox.openshell import OpenShell, OpenShellError
from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets import approvals as approval_widgets
from tongs_agents.widgets.approvals import ApprovalFeed, ApprovalScreen, ReasonScreen
from tongs_agents.widgets.chat_panel import ConfirmScreen

RULES = Path(__file__).parent / "fixtures" / "openshell_rules"
SRC = Path(__file__).resolve().parents[1] / "src" / "tongs_agents"
SANDBOX = "tongs-boxed"
CHUNK = "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6"


def fixture(name: str) -> str:
    return (RULES / name).read_text()


# Parser


def test_no_rules():
    assert approvals.parse_rules(fixture("none.txt"), sandbox=SANDBOX) == (None, ())


def test_no_rules_of_another_sandbox_is_refused():
    with pytest.raises(ParseError, match="another sandbox"):
        approvals.parse_rules(fixture("none.txt"), sandbox="tongs-other")


def test_one_pending_proposal():
    version, [proposal] = approvals.parse_rules(fixture("pending-one.txt"), sandbox=SANDBOX)
    assert version == 2
    assert proposal.chunk_id == CHUNK
    assert proposal.status == "pending"
    assert proposal.rule == "allow_api_example_test_443"
    assert proposal.binary == "/usr/bin/curl"
    assert proposal.confidence == "85%"
    assert proposal.endpoints == "api.example.test:443 [L4]"
    assert proposal.binaries == "/usr/bin/curl"
    assert not proposal.flagged
    assert proposal.text.splitlines()[0] == f"Chunk: {CHUNK}"
    assert len(proposal.text.splitlines()) == 8


def test_many_proposals_keep_every_line():
    version, proposals = approvals.parse_rules(fixture("pending-many.txt"))
    assert version == 7
    flagged, rest, broken = proposals
    assert flagged.flagged
    assert flagged.security.startswith("Destination '10.0.0.5'")
    assert flagged.prover.startswith("credential reach expansion")
    assert flagged.hits.startswith("4 (first seen")
    assert "Security: " in flagged.text and "Prover: " in flagged.text
    assert rest.candidate == "9e8d7c6b5a49"
    assert rest.endpoints.startswith("api.github.com:443 [L7 rest")
    assert rest.binaries == "/usr/bin/node, /usr/local/bin/gh"
    assert broken.binary == ""
    assert broken.application.startswith("policy merge failed")


def test_colour_escapes_are_removed_before_parsing():
    plain = approvals.parse_rules(fixture("pending-one.txt"))
    assert approvals.parse_rules(fixture("pending-colored.txt")) == plain


def test_the_older_validation_label_reads_as_prover():
    _, [proposal] = approvals.parse_rules(fixture("legacy-validation.txt"))
    assert proposal.prover == "no findings"


@pytest.mark.parametrize("name, message", [
    ("unknown-label.txt", "unknown label 'Scope'"),
    ("injected-rationale.txt", "announces 1 rule"),
])
def test_unrecognised_output_is_refused(name, message):
    with pytest.raises(ParseError, match=message):
        approvals.parse_rules(fixture(name))


def _block(*lines, header="Network Rules:  (version 1, 1 chunk)"):
    return "\n".join([header, "", *lines, ""]) + "\n"


BASE = [f"  Chunk: {CHUNK}", "  Status: pending", "  Rule: r", "  Confidence: 50%",
        "  Rationale: why"]


def test_an_empty_rationale_is_accepted():
    _, [proposal] = approvals.parse_rules(_block(*BASE[:4], "  Rationale: "))
    assert proposal.rationale == ""


@pytest.mark.parametrize("lines, message", [
    ([BASE[1], BASE[0], *BASE[2:]], "out of order"),
    ([*BASE, "  Security: a", "  Security: b"], "repeated"),
    (BASE[:4], "lacks Rationale"),
    ([BASE[0], "  Status: approved", *BASE[2:]], "approved rule in a --status pending"),
    (["  Chunk: --chunk-id", *BASE[1:]], "invalid chunk id"),
    ([*BASE[:3], "  Confidence: high", BASE[4]], "confidence"),
    ([*BASE, "    continued"], "unrecognised line"),
    ([*BASE, "  Prover: x", "  Validation: y"], "out of order|Prover and Validation"),
])
def test_strict_block_rules(lines, message):
    with pytest.raises(ParseError, match=message):
        approvals.parse_rules(_block(*lines))


def test_duplicate_ids_and_blank_lines_are_refused():
    two = "Network Rules:  (version 1, 2 chunks)"
    with pytest.raises(ParseError, match="twice"):
        approvals.parse_rules(_block(*BASE, "", *BASE, header=two))
    with pytest.raises(ParseError, match="blank"):
        approvals.parse_rules(_block(*BASE, "", "", *BASE, header=two))
    with pytest.raises(ParseError, match="first line"):
        approvals.parse_rules("Rules:\n")
    with pytest.raises(ParseError, match="no output"):
        approvals.parse_rules("\n")


def test_fingerprint_ignores_a_growing_hit_count():
    _, [first, *_] = approvals.parse_rules(fixture("pending-many.txt"))
    later = dataclasses.replace(first, hits="9 (first seen a, last seen b)")
    assert later.fingerprint() == first.fingerprint()
    assert dataclasses.replace(first, rationale="x").fingerprint() != first.fingerprint()


@pytest.mark.parametrize("reason", ["", "   ", "two\nlines", "x" * 501])
def test_a_reason_is_one_short_printable_line(reason):
    with pytest.raises(ApprovalError):
        approvals.check_reason(reason)


# Against the fake CLI


def propose(fake, sandbox=SANDBOX, *, host="api.example.test", port=443, status="pending",
            **extra):
    data = fake.state()
    chunk = {
        "id": str(uuid.uuid4()), "status": status,
        "rule_name": f"allow_{host.replace('.', '_')}_{port}", "binary": "/usr/bin/curl",
        "confidence": 0.85, "rationale": f"Blocked connection to {host}:{port}",
        "endpoint": f"{host}:{port} [L4]", "binaries": ["/usr/bin/curl"], "hit_count": 1,
        **extra,
    }
    data["sandboxes"][sandbox].setdefault("rules", []).append(chunk)
    (fake.home / "state.json").write_text(json.dumps(data))
    return chunk["id"]


def rule_calls(fake):
    return [call for call in fake.calls() if call[:1] == ["rule"]]


def client(fake, gateway=None):
    return OpenShell(fake.executable, gateway=gateway)


@posix_only
def test_list_rules_reads_pending_rules_without_colour(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake, security_notes="Port 5432 is a well-known database/service port.",
                    validation_result="no new reach", hit_count=3, first_seen=0, last_seen=60)
    listing = approvals.list_rules(client(fake, "local"), SANDBOX)
    assert listing.needs_you and listing.failure is None and listing.parse_error is None
    [proposal] = listing.proposals
    assert proposal.chunk_id == chunk and proposal.flagged
    assert proposal.hits == "3 (first seen 1970-01-01 00:00:00, last seen 1970-01-01 00:01:00)"
    [call] = [c for c in fake.raw_calls() if c["argv"][2:3] == ["rule"]]
    assert call["argv"] == ["-g", "local", "rule", "get", SANDBOX, "--status", "pending"]
    assert call["no_color"] == "1"


@posix_only
def test_a_failed_rule_get_is_a_failure_not_a_request(fake, monkeypatch):
    create_sandbox(fake, SANDBOX, profile="boxed")
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "rule")
    listing = approvals.list_rules(client(fake), SANDBOX)
    assert "gateway unreachable" in listing.failure
    assert not listing.needs_you


def test_poll_reports_a_missing_cli_per_sandbox():
    def missing(gateway):
        raise OpenShellError("openshell CLI not found on PATH")

    listings = approvals.poll([(None, "a-box"), ("gw", "b-box")], missing)
    assert {key: item.failure for key, item in listings.items()} == {
        (None, "a-box"): "openshell CLI not found on PATH",
        ("gw", "b-box"): "openshell CLI not found on PATH",
    }


@posix_only
def test_unreadable_output_needs_you_and_keeps_the_raw_text(fake, monkeypatch):
    create_sandbox(fake, SANDBOX, profile="boxed")
    monkeypatch.setenv("FAKE_OPENSHELL_RULES_TEXT", str(RULES / "unknown-label.txt"))
    listing = approvals.list_rules(client(fake), SANDBOX)
    assert listing.needs_you and listing.proposals == ()
    assert "Scope: all sandboxes" in listing.raw
    assert "Scope" in listing.parse_error


@posix_only
def test_approve_checks_the_reviewed_proposal_then_approves_one_chunk(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake)
    [proposal] = approvals.list_rules(client(fake), SANDBOX).proposals
    message = approvals.approve(client(fake), SANDBOX, proposal)
    assert message.startswith("OK Chunk approved. Policy version: 2")
    assert rule_calls(fake)[-2:] == [
        ["rule", "get", SANDBOX, "--status", "pending"],
        ["rule", "approve", SANDBOX, "--chunk-id", chunk],
    ]
    assert fake.state()["sandboxes"][SANDBOX]["rules"][0]["status"] == "approved"
    with pytest.raises(ApprovalError, match="no longer pending"):
        approvals.approve(client(fake), SANDBOX, proposal)


@posix_only
def test_approve_refuses_a_proposal_that_changed_since_it_was_shown(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    propose(fake)
    [proposal] = approvals.list_rules(client(fake), SANDBOX).proposals
    data = fake.state()
    data["sandboxes"][SANDBOX]["rules"][0]["endpoint"] = "evil.example.test:443 [L4]"
    (fake.home / "state.json").write_text(json.dumps(data))
    with pytest.raises(ApprovalError, match="changed since it was shown"):
        approvals.approve(client(fake), SANDBOX, proposal)
    assert not [call for call in rule_calls(fake) if call[1] == "approve"]


@posix_only
def test_approve_refuses_when_the_output_cannot_be_read(fake, monkeypatch):
    create_sandbox(fake, SANDBOX, profile="boxed")
    _, [proposal] = approvals.parse_rules(fixture("pending-one.txt"))
    monkeypatch.setenv("FAKE_OPENSHELL_RULES_TEXT", str(RULES / "injected-rationale.txt"))
    with pytest.raises(ApprovalError, match="cannot read"):
        approvals.approve(client(fake), SANDBOX, proposal)
    assert [call[1] for call in rule_calls(fake)] == ["get"]


@posix_only
def test_reject_needs_a_reason_and_sends_it(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake)
    [proposal] = approvals.list_rules(client(fake), SANDBOX).proposals
    before = len(fake.calls())
    with pytest.raises(ApprovalError, match="needs a reason"):
        approvals.reject(client(fake), SANDBOX, proposal, "  ")
    assert len(fake.calls()) == before
    assert approvals.reject(client(fake), SANDBOX, proposal, " not needed ") == \
        "OK Chunk rejected."
    assert rule_calls(fake)[-1] == ["rule", "reject", SANDBOX, "--chunk-id", chunk,
                                    "--reason", "not needed"]
    stored = fake.state()["sandboxes"][SANDBOX]["rules"][0]
    assert (stored["status"], stored["rejection_reason"]) == ("rejected", "not needed")


@posix_only
def test_the_fake_refuses_approve_all(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    result = fake.run("rule", "approve-all", SANDBOX)
    assert result.returncode == 2 and "refuses rule approve-all" in result.stderr


@posix_only
def test_the_fake_network_drafts_one_rule_per_denied_endpoint(fake, monkeypatch):
    create_sandbox(fake, SANDBOX, profile="boxed")
    monkeypatch.setenv("FAKE_OPENSHELL_NETWORK", "1")
    fetch = ["sandbox", "exec", "-n", SANDBOX, "--no-tty", "--", "sh", "-c", "x", "sh",
             "https://api.example.test/v1"]
    assert fake.run(*fetch).returncode == 56
    assert fake.run(*fetch).returncode == 56
    [proposal] = approvals.list_rules(client(fake), SANDBOX).proposals
    assert proposal.hits.startswith("2 (")
    approvals.approve(client(fake), SANDBOX, proposal)
    assert fake.run(*fetch).returncode == 0


# Attention


def _row(status=SessionStatus.RUNNING, launch_id=LAUNCH_ID, repo="boxed"):
    return FleetSession(
        session_id=f"sid-{repo}", repo=repo, cwd="/w", status=status, detail="working",
        ts=0, started=0, pid="", terminal="", terminal_env={}, source="sandbox", tool="Bash",
        launch_id=launch_id,
    )


def test_access_requests_make_a_row_need_you():
    marked = with_access_requests(_row(), 2)
    assert attention_state(marked) is Attention.NEEDS_YOU
    assert marked.needs_attention and marked.tool == ACCESS_TOOL
    assert marked.detail == "2 OpenShell access requests (a to review)"
    unreadable = with_access_requests(_row(SessionStatus.IDLE), 0, unreadable=True)
    assert attention_state(unreadable) is Attention.NEEDS_YOU
    assert "need reading" in unreadable.detail


def test_no_requests_or_an_ended_row_stay_as_they_are():
    assert with_access_requests(_row(), 0) == _row()
    ended = _row(SessionStatus.ENDED)
    assert with_access_requests(ended, 3) == ended


def test_the_feed_marks_only_rows_whose_sandbox_has_requests():
    _, proposals = approvals.parse_rules(fixture("pending-many.txt"))
    other = "f" * 32
    listings = {(None, SANDBOX): RuleListing(SANDBOX, proposals=proposals),
                ("gw", "tongs-quiet"): RuleListing("tongs-quiet", "gw")}

    class Inner:
        def snapshot(self):
            return [_row(), _row(launch_id=other, repo="quiet"), _row(launch_id="", repo="x")]

    feed = ApprovalFeed(Inner(), lambda: listings,
                        lambda: {LAUNCH_ID: (None, SANDBOX), other: ("gw", "tongs-quiet")})
    boxed, quiet, plain = feed.snapshot()
    assert boxed.status is SessionStatus.WAITING and "3 OpenShell" in boxed.detail
    assert quiet == _row(launch_id=other, repo="quiet")
    assert plain.status is SessionStatus.RUNNING
    assert feed.targets() == {(None, SANDBOX), ("gw", "tongs-quiet")}


# Sidebar


def _sidebar_state(fake):
    create_sandbox(fake, SANDBOX, profile="boxed")
    state.write(state.new(
        launch_id=LAUNCH_ID, profile="boxed", harness="claude", sandbox=SANDBOX,
        mode="exec", workdir="/sandbox/workspace",
        spool=f"/sandbox/workspace/.tongs-agents/spool/{LAUNCH_ID}", gateway=None,
        cwd="", terminal="", terminal_env={},
    ))
    record = {"session_id": "sid-boxed", "agent": "claude", "repo": "boxed", "cwd": "/w",
              "status": "running", "detail": "", "ts": 0, "age_seconds": 5,
              "launch": {"launch_id": LAUNCH_ID, "profile_id": "boxed"}}
    plain = {**record, "session_id": "sid-plain", "repo": "plain"}
    plain.pop("launch")
    return SessionFeed(lambda: [dict(record), dict(plain)])


def _app(feed):
    app = sidebar.SidebarApp(FakeMux(), "agents", feed=feed, refresh_interval=60,
                             approval_interval=60)
    # quiet_desktop hides every executable from shutil.which, openshell included.
    app.approval_client_for = lambda gateway: OpenShell(str(FAKE_OPENSHELL), gateway=gateway)
    return app


async def _settle(app, pilot):
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


async def _poll(app, pilot):
    """One panel poll (to learn the sandboxed rows), then one approvals poll."""
    app.agents_panel.poll()
    await _settle(app, pilot)
    app.poll_approvals()
    await _settle(app, pilot)


def _boxed_row(app):
    return next(row for row in app.agents_panel.sessions if row.repo == "boxed")


@pytest.fixture
def quiet_desktop(monkeypatch):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)


@posix_only
@pytest.mark.asyncio
async def test_a_request_shows_as_needs_you_and_polling_never_decides(fake, quiet_desktop):
    feed = _sidebar_state(fake)
    propose(fake, security_notes="Destination appears internal.",
            validation_result="credential reach expansion")
    app = _app(feed)
    async with app.run_test(size=(100, 30)) as pilot:
        await _poll(app, pilot)
        await _poll(app, pilot)
        row = _boxed_row(app)
        assert attention_state(row) is Attention.NEEDS_YOU
        assert row.detail == "1 OpenShell access request (a to review)"
        plain = next(r for r in app.agents_panel.sessions if r.repo == "plain")
        assert plain.status is SessionStatus.RUNNING
        notes = [str(note.message) for note in app._notifications
                 if note.title == "Access request"]
        assert len(notes) == 1 and "api.example.test:443" in notes[0]
        assert "(flagged)" in notes[0]
    assert {call[1] for call in rule_calls(fake)} == {"get"}


@posix_only
@pytest.mark.asyncio
async def test_approve_needs_the_screen_and_a_second_yes(fake, quiet_desktop):
    feed = _sidebar_state(fake)
    chunk = propose(fake, security_notes="Port 5432 is a well-known database/service port.",
                    validation_result="no new reach")
    app = _app(feed)
    async with app.run_test(size=(100, 40)) as pilot:
        await _poll(app, pilot)
        app.agents_panel.table.focus()
        await pilot.press("a")
        await _settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, ApprovalScreen)
        detail = str(screen.query_one("#access-detail").render())
        assert "Security: Port 5432" in detail and "Prover: no new reach" in detail
        await pilot.press("a")
        await _settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        assert "api.example.test:443" in app.screen.message
        await pilot.press("n")
        await _settle(app, pilot)
        assert not [call for call in rule_calls(fake) if call[1] != "get"]
        await pilot.press("a")
        await _settle(app, pilot)
        await pilot.press("a")
        await _settle(app, pilot)
        await pilot.press("y")
        await _settle(app, pilot)
        assert [call for call in rule_calls(fake) if call[1] != "get"] == [
            ["rule", "approve", SANDBOX, "--chunk-id", chunk]]
        await _poll(app, pilot)
        assert attention_state(_boxed_row(app)) is not Attention.NEEDS_YOU


@posix_only
@pytest.mark.asyncio
async def test_reject_needs_a_reason(fake, quiet_desktop):
    feed = _sidebar_state(fake)
    chunk = propose(fake)
    app = _app(feed)
    async with app.run_test(size=(100, 40)) as pilot:
        await _poll(app, pilot)
        await pilot.press("a")
        await _settle(app, pilot)
        await pilot.press("d")
        await _settle(app, pilot)
        assert isinstance(app.screen, ReasonScreen)
        await pilot.press("enter")
        await _settle(app, pilot)
        assert isinstance(app.screen, ReasonScreen)
        assert not [call for call in rule_calls(fake) if call[1] != "get"]
        await pilot.press(*"not needed")
        await pilot.press("enter")
        await _settle(app, pilot)
        assert [call for call in rule_calls(fake) if call[1] != "get"] == [
            ["rule", "reject", SANDBOX, "--chunk-id", chunk, "--reason", "not needed"]]


@posix_only
@pytest.mark.asyncio
async def test_unreadable_output_is_shown_raw_and_cannot_be_approved(fake, quiet_desktop,
                                                                     monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_RULES_TEXT", str(RULES / "injected-rationale.txt"))
    app = _app(_sidebar_state(fake))
    async with app.run_test(size=(100, 40)) as pilot:
        await _poll(app, pilot)
        assert "need reading" in _boxed_row(app).detail
        assert any(note.severity == "error" for note in app._notifications)
        await pilot.press("a")
        await _settle(app, pilot)
        assert isinstance(app.screen, ApprovalScreen)
        assert "allow_docs_example_test_443" in str(app.screen.query_one("#access-raw")
                                                    .render())
        await pilot.press("a")
        await _settle(app, pilot)
        assert isinstance(app.screen, ApprovalScreen)
    assert {call[1] for call in rule_calls(fake)} == {"get"}


@posix_only
@pytest.mark.asyncio
async def test_gateway_text_in_toasts_is_never_read_as_markup(fake, quiet_desktop, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_RULES_TEXT", str(RULES / "pending-many.txt"))
    app = _app(_sidebar_state(fake))
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        await _poll(app, pilot)
        app._access_failed("stderr [/] and [/x] and [bold]")
        await _settle(app, pilot)
        assert app.is_running
        toasts = [str(toast.render()) for toast in app.screen.query("Toast")]
        assert any("[L7" in text for text in toasts)
        assert any("[L4]" in text for text in toasts)
        assert any("[/] and [/x] and [bold]" in text for text in toasts)


@pytest.mark.asyncio
async def test_without_sandboxed_rows_the_sidebar_never_runs_openshell(quiet_desktop,
                                                                     monkeypatch):
    def refuse(gateway):
        raise AssertionError("no openshell call expected")

    app = sidebar.SidebarApp(FakeMux(), "agents", feed=SessionFeed(lambda: []),
                             refresh_interval=60, approval_interval=60)
    app.approval_client_for = refuse
    async with app.run_test(size=(60, 20)) as pilot:
        await _poll(app, pilot)
        await pilot.press("a")
        await _settle(app, pilot)
        assert not isinstance(app.screen, ApprovalScreen)
        assert any("No pending access requests" in str(note.message)
                   for note in app._notifications)


def test_the_palette_offers_review_before_the_chat_entries():
    app = sidebar.SidebarApp(FakeMux(), "agents", feed=SessionFeed(lambda: []))
    titles = [command.title for command in app.palette_commands()]
    assert approval_widgets.REVIEW_ACCESS in titles
    assert titles.index(approval_widgets.REVIEW_ACCESS) < titles.index("Grant chat")


# CLI


def run(*argv: str) -> int:
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["sandbox", *argv])
    return handlers[args.command](args) or 0


class _Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    answers = []
    monkeypatch.setattr(sandbox_command.sys, "stdin", _Terminal())
    monkeypatch.setattr(builtins, "input", lambda prompt="": answers.pop(0))
    return answers


@posix_only
def test_rules_list_prints_full_proposals(fake, capsys):
    create_sandbox(fake, SANDBOX, profile="boxed")
    assert run("rules", "list", SANDBOX) == 0
    assert f"No pending access requests for sandbox {SANDBOX}." in capsys.readouterr().out
    propose(fake, security_notes="internal")
    assert run("rules", "list", SANDBOX) == 0
    out = capsys.readouterr().out
    assert "  Chunk: " in out and "  Security: internal" in out


@posix_only
def test_rules_list_shows_unreadable_output_raw(fake, monkeypatch, capsys):
    create_sandbox(fake, SANDBOX, profile="boxed")
    monkeypatch.setenv("FAKE_OPENSHELL_RULES_TEXT", str(RULES / "unknown-label.txt"))
    assert run("rules", "list", SANDBOX) == 1
    err = capsys.readouterr().err
    assert "Scope: all sandboxes" in err and "cannot read" in err


@posix_only
def test_approve_off_a_terminal_is_refused(fake, monkeypatch, capsys):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake)
    monkeypatch.setattr(sandbox_command.sys, "stdin", io.StringIO(""))
    assert run("rules", "approve", SANDBOX, chunk) == 1
    assert "needs a person at a terminal" in capsys.readouterr().err
    assert rule_calls(fake) == []


@posix_only
def test_approve_at_a_terminal_shows_the_rule_and_asks(fake, terminal, capsys):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake)
    terminal += ["n", "y"]
    assert run("rules", "approve", SANDBOX, chunk) == 1
    assert "Endpoints: api.example.test:443 [L4]" in capsys.readouterr().out
    assert [call[1] for call in rule_calls(fake)] == ["get"]
    assert run("rules", "approve", SANDBOX, chunk) == 0
    assert "Chunk approved" in capsys.readouterr().out
    assert rule_calls(fake)[-1] == ["rule", "approve", SANDBOX, "--chunk-id", chunk]


@posix_only
def test_reject_needs_a_reason_and_confirmation(fake, monkeypatch, capsys):
    create_sandbox(fake, SANDBOX, profile="boxed")
    chunk = propose(fake)
    monkeypatch.setattr(sandbox_command.sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        run("rules", "reject", SANDBOX, chunk)
    assert run("rules", "reject", SANDBOX, chunk, "--reason", " ") == 1
    assert run("rules", "reject", SANDBOX, chunk, "--reason", "no") == 1
    assert "pass --yes" in capsys.readouterr().err
    assert [call[1] for call in rule_calls(fake)] == ["get"]
    assert run("rules", "reject", SANDBOX, chunk, "--reason", "no", "--yes") == 0
    assert rule_calls(fake)[-1] == ["rule", "reject", SANDBOX, "--chunk-id", chunk,
                                    "--reason", "no"]


@posix_only
def test_rules_refuse_an_unlabelled_sandbox_and_bad_chunk_ids(fake, terminal, capsys):
    assert fake.run("sandbox", "create", "--name", "foreign", "--detach").returncode == 0
    assert run("rules", "list", "foreign") == 1
    assert "does not manage it" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("rules", "approve", SANDBOX, "bad id!")
    assert rule_calls(fake) == []


# Guards (row 14)


def _modules():
    return sorted(SRC.rglob("*.py"))


def _constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)}


def test_no_source_enables_auto_approval_or_approves_all():
    forbidden = {"approve-all", "--approval-mode", "proposal_approval_mode",
                 "--include-security-flagged", "auto"}
    for path in _modules():
        assert not _constants(path) & forbidden, path


def test_only_the_approvals_module_runs_openshell_rule():
    users = {path.relative_to(SRC).as_posix() for path in _modules()
             if "rule" in _constants(path)}
    assert users == {"sandbox/approvals.py"}


def test_only_the_sidebar_screen_and_the_cli_call_approve_or_reject():
    callers = set()
    for path in _modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in ("approve", "reject")
                    and isinstance(node.value, ast.Name) and node.value.id == "approvals"):
                callers.add(path.relative_to(SRC).as_posix())
            if isinstance(node, ast.ImportFrom) and node.module == "tongs_agents.sandbox.approvals":
                assert not {alias.name for alias in node.names} & {"approve", "reject"}, path
    assert callers == {"widgets/approvals.py", "commands/sandbox.py"}


def test_no_mcp_module_reaches_approvals():
    for path in SRC.glob("mcp*.py"):
        text = path.read_text()
        assert "approvals" not in text and "openshell" not in text.lower(), path


def test_the_screen_approves_only_through_a_confirmation():
    tree = ast.parse(Path(approval_widgets.__file__).read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "access_approve"]
    assert len(calls) == 1
    confirmed = next(node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef) and node.name == "confirmed")
    assert calls[0] in list(ast.walk(confirmed))


# Validator


@posix_only
def test_validator_round_trips_approval_and_rejection_through_the_sidebar(tmp_path):
    started = time.monotonic()
    result, evidence = run_validator(
        tmp_path, "--approval-host", "https://api.example.test",
        "--reject-host", "https://blocked.example.test",
        env={"FAKE_OPENSHELL_NETWORK": "1"},
    )
    assert result.returncode == 0, (result.stderr, evidence)
    assert evidence["label"] == "instrumented"
    passed = {item["name"]: item["passed"] for item in evidence["checks"]}
    for name in ("access request: first fetch denied",
                 "access request shown as needs-you in the sidebar",
                 "approval from the sidebar hot-reloads: retry passes",
                 "rejection with a reason from the sidebar",
                 "rejected host still denied"):
        assert passed[name] is True, name
    calls = [json.loads(line)["argv"] for line in
             (tmp_path / "fake" / "calls.jsonl").read_text().splitlines()]
    decisions = [call[1] for call in calls if call[:1] == ["rule"] and call[1] != "get"]
    assert decisions == ["approve", "reject"]
    assert time.monotonic() - started < 110


@posix_only
def test_validator_without_an_approval_host_marks_the_checks_not_applicable(tmp_path):
    result, evidence = run_validator(tmp_path)
    assert result.returncode == 0, (result.stderr, evidence)
    item = next(item for item in evidence["checks"]
                if item["name"] == "access request: first fetch denied")
    assert item["passed"] is None and "--approval-host" in item["detail"]
