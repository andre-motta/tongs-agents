"""Chat framing, previews and the hook inbox seam (M5-C uses it)."""

import pytest

from tests.test_chat_identity import AGENT_PID, LAUNCH, START, FakeProcess, record
from tongs_agents import discovery
from tongs_agents.chat import frame, inbox
from tongs_agents.chat.identity import AgentIdentity
from tongs_agents.chat.store import ChatStore, Message


def message(body, *, id=7, thread=None, author="f" * 32, handle="peer", kind="agent"):
    return Message(id, "general", thread, author, handle, kind, body, 1)


def test_previews_flatten_control_characters_and_cut_on_utf8_boundaries():
    assert frame.preview("a\nb\tc\x1b[31m") == "a b c [31m"
    cut = frame.preview("é" * 100)
    assert len(cut.encode("utf-8")) == 160 and cut == "é" * 80
    assert frame.preview("ab" + "€" * 60, limit=5) == "ab€"


def test_clean_keeps_newlines_and_tabs_only():
    assert frame.clean("a\n\tb\x1b\x07\x9b") == "a\n\tb���"


def test_the_digest_is_framed_and_fits_its_budget():
    mentions = [message("first " + "x" * 300, id=412, thread=398),
                message("second", id=377, author="maintainer", handle="maintainer",
                        kind="maintainer")]
    text = frame.mention_digest("codex-review", mentions)
    lines = text.splitlines()
    assert lines[0] == frame.PREAMBLE
    assert lines[1] == "You are @codex-review. Unread mentions (2, newest first):"
    assert lines[2].startswith("- #general id 412, thread 398, from @peer (agent): \"first ")
    assert lines[3] == "- #general id 377, from @maintainer: \"second\""
    assert lines[-1] == frame.FOOTER
    small = frame.mention_digest("codex-review", mentions, max_bytes=512)
    assert len(small.encode("utf-8")) <= 512
    assert small.startswith(frame.PREAMBLE) and small.endswith(frame.FOOTER)
    assert "id 412" not in small and "id 377" not in small


def test_mid_turn_notices_carry_no_text():
    assert "chat_inbox" in frame.MID_TURN_NOTICE
    assert "@" not in frame.MID_TURN_NOTICE
    note = frame.session_start_note("lead", 1)
    assert "1 unread mention." in note and "chat_post" in note


def test_message_json_replaces_control_characters():
    entry = frame.message_json(message("line\x1b[2J"))
    assert entry["body"] == "line�[2J" and entry["from"] == "peer"


@pytest.fixture
def lead(monkeypatch):
    table = {AGENT_PID: FakeProcess(AGENT_PID, START, 1)}
    monkeypatch.setattr(discovery, "get_process_info", table.get)
    launch = record()
    return AgentIdentity(LAUNCH, "lead", "claude", START, launch)


def test_inbox_is_empty_without_a_file_or_grant(lead):
    assert inbox.read_inbox(lead) is None
    inbox.mark_injected(lead, 3)
    with ChatStore.maintainer(create=True):
        pass
    assert inbox.read_inbox(lead) is None


def test_inbox_reads_mentions_and_records_injection(lead):
    with ChatStore.maintainer(create=True) as current:
        current.put_grant(
            launch_id=LAUNCH, handle="lead", level="chat+wake", process_start=START,
            pane_session="agents", pane_id="1", socket_dir=None, via="cli",
            profile="lead", harness="claude",
        )
        first = current.post("general", "@lead one")
        current.post("general", "not a mention")
        second = current.post("general", "@lead two")
    found = inbox.read_inbox(lead)
    assert (found.handle, found.level, found.unread_mentions) == ("lead", "chat+wake", 2)
    assert [item.id for item in found.mentions] == [second.id, first.id]
    assert found.has_new and found.newest_mention_id == second.id
    inbox.mark_injected(lead, second.id, now=5)
    again = inbox.read_inbox(lead)
    assert (again.injected_through, again.last_injected_at, again.has_new) == (
        second.id, 5, False,
    )
    inbox.mark_injected(lead, first.id, now=6)
    assert inbox.read_inbox(lead).injected_through == second.id


def test_inbox_ignores_a_grant_for_another_process(lead):
    with ChatStore.maintainer(create=True) as current:
        current.put_grant(
            launch_id=LAUNCH, handle="lead", level="chat", process_start="older",
            pane_session="agents", pane_id="1", socket_dir=None, via="cli",
            profile="lead", harness="claude",
        )
    assert inbox.read_inbox(lead) is None
