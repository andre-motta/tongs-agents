"""Wake on mention (M5-C, #32): rules, rate limits, loop breaker and audit.

The control layer is replaced by fakes; no multiplexer or agent runs.
"""

import argparse
import json

import pytest

from tests.test_chat_identity import START, record
from tongs_agents import chat_wake, control, paths
from tongs_agents.commands import chat as chat_command
from tongs_agents.widgets import chat_panel
from tongs_agents.chat.store import (
    WAKE_INTERVAL_MS,
    WAKE_WINDOW_MS,
    ChatRefused,
    ChatStore,
    Posted,
)
from tongs_agents.control import AgentStatus
from tongs_agents.multiplexers.base import MultiplexerError, PaneInfo, PaneRef
from tongs_agents.multiplexers.zellij import ZellijMultiplexer

TARGET = "a" * 32
PEER = "b" * 32
NOW = 1_000_000_000
NOW_S = NOW // 1000


def grant(launch_id=TARGET, handle="target", level="chat+wake", start=START):
    with ChatStore.maintainer(create=True) as current:
        current.put_grant(
            launch_id=launch_id, handle=handle, level=level, process_start=start,
            pane_session="agents", pane_id="1", socket_dir=None, via="cli",
            profile=handle, harness="claude",
        )


class FakeControl:
    def __init__(self, monkeypatch):
        self.state = "done"
        self.updated = NOW_S - 60
        self.focused = False
        self.refuse = None
        self.send_error = None
        self.sent = []
        monkeypatch.setattr(control, "status", self.status)
        monkeypatch.setattr(control, "client_focused", lambda launch_id: self.focused)

    def verify(self, launch_id):
        if self.refuse is not None:
            raise self.refuse
        return PaneRef("zellij", "agents", "1")

    def status(self, launch_id, *, actor, caller=None):
        assert actor == chat_wake.ACTOR
        self.verify(launch_id)
        return AgentStatus(launch_id, "target", "claude", "agents", "1", self.state,
                           updated=self.updated)

    def send(self, launch_id, text, *, actor, caller=None):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((launch_id, text, actor, caller))


@pytest.fixture
def fake(monkeypatch):
    record(TARGET, profile="target")
    grant()
    return FakeControl(monkeypatch)


def post_and_wake(fake, body="@target please look", *, author=None, now=NOW, thread=None,
                  caller=None):
    opener = ChatStore.maintainer() if author is None else ChatStore.agent(author)
    with opener as current:
        posted = current.post("general", body, thread=thread, now=now)
        return posted, chat_wake.wake_mentioned(current, posted, caller=caller, now=now,
                                                send=fake.send)


def outcomes(results):
    return [(item.handle, item.outcome) for item in results]


def audit():
    path = paths.state_dir() / "audit" / "chat.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_an_idle_unfocused_target_is_woken_once_with_the_fixed_text(fake):
    _, results = post_and_wake(fake, "@target SECRET details")
    assert outcomes(results) == [("target", "ok")]
    assert fake.sent == [(TARGET, chat_wake.WAKE_TEXT, "chat-wake", None)]
    assert "SECRET" not in chat_wake.WAKE_TEXT
    [entry] = audit()
    assert (entry["action"], entry["actor"], entry["launch_id"], entry["outcome"]) == (
        "wake", "chat-wake", TARGET, "ok",
    )
    assert "SECRET" not in json.dumps(entry)


def test_a_chat_level_grant_or_no_mention_never_wakes(fake):
    grant(level="chat")
    assert post_and_wake(fake)[1] == []
    assert post_and_wake(fake, "no mention", now=NOW + 10 * WAKE_INTERVAL_MS)[1] == []
    assert fake.sent == [] and audit() == []


@pytest.mark.parametrize("state,age", [("working", 60), ("needs-you", 60), ("done", 19)])
def test_a_target_that_is_not_idle_for_20_seconds_is_not_woken(fake, state, age):
    fake.state, fake.updated = state, NOW_S - age
    assert outcomes(post_and_wake(fake)[1]) == [("target", "not-idle")]
    assert fake.sent == []


def test_a_focused_pane_is_not_woken(fake):
    fake.focused = True
    assert outcomes(post_and_wake(fake)[1]) == [("target", "focused")]
    assert fake.sent == []


def test_an_unverified_pane_is_not_woken(fake):
    fake.refuse = control.Refused("pane is gone")
    assert outcomes(post_and_wake(fake)[1]) == [("target", "refused")]
    assert fake.sent == []


def test_a_grant_for_another_process_is_stale(fake):
    grant(start="older")
    assert outcomes(post_and_wake(fake)[1]) == [("target", "stale")]


def test_wakes_are_limited_to_one_per_two_minutes_and_ten_per_hour(fake):
    assert outcomes(post_and_wake(fake)[1]) == [("target", "ok")]
    assert outcomes(post_and_wake(fake, now=NOW + WAKE_INTERVAL_MS - 1)[1]) == [
        ("target", "rate")
    ]
    times = [NOW + WAKE_INTERVAL_MS * step for step in range(1, 12)]
    found = [outcomes(post_and_wake(fake, now=stamp)[1])[0][1] for stamp in times[:10]]
    assert found == ["ok"] * 9 + ["rate"]
    assert len(fake.sent) == 10
    assert outcomes(post_and_wake(fake, now=NOW + WAKE_WINDOW_MS)[1]) == [("target", "ok")]


def test_a_failed_send_still_counts_toward_the_rate(fake):
    fake.send_error = control.ControlError("paste failed")
    assert outcomes(post_and_wake(fake)[1]) == [("target", "error")]
    fake.send_error = None
    assert outcomes(post_and_wake(fake, now=NOW + 1000)[1]) == [("target", "rate")]


def test_a_refused_check_does_not_use_up_the_rate(fake):
    fake.focused = True
    post_and_wake(fake)
    fake.focused = False
    assert outcomes(post_and_wake(fake, now=NOW + 1000)[1]) == [("target", "ok")]


def test_six_agent_messages_in_a_thread_stop_waking_until_the_maintainer_posts(fake):
    record(PEER, profile="peer")
    grant(PEER, "peer", level="chat")
    step = WAKE_INTERVAL_MS
    root, results = post_and_wake(fake, "@target start", author=PEER, caller=PEER)
    assert outcomes(results) == [("target", "ok")]
    assert fake.sent[-1][3] == PEER
    for index in range(1, 5):
        _, results = post_and_wake(fake, "@target more", author=PEER, thread=root.id,
                                   now=NOW + step * index)
        assert outcomes(results) == [("target", "ok")]
    _, results = post_and_wake(fake, "@target sixth", author=PEER, thread=root.id,
                               now=NOW + step * 5)
    assert outcomes(results) == [("target", "loop")]
    _, results = post_and_wake(fake, "@target outside the thread", author=PEER,
                               now=NOW + step * 6)
    assert outcomes(results) == [("target", "ok")]
    _, results = post_and_wake(fake, "@target back", thread=root.id, now=NOW + step * 7)
    assert outcomes(results) == [("target", "ok")]
    _, results = post_and_wake(fake, "@target again", author=PEER, thread=root.id,
                               now=NOW + step * 8)
    assert outcomes(results) == [("target", "ok")]


def test_the_streak_counts_only_the_thread_back_to_the_maintainer(fake):
    record(PEER, profile="peer")
    grant(PEER, "peer", level="chat")
    with ChatStore.maintainer() as current:
        root = current.post("general", "root")
    with ChatStore.agent(PEER) as current:
        replies = [current.post("general", f"r{index}", thread=root.id) for index in range(3)]
        other = current.post("general", "elsewhere")
        assert current.agent_streak(replies[-1].id) == 3
        assert current.agent_streak(replies[0].id) == 1
        assert current.agent_streak(root.id) == 0
        assert current.agent_streak(other.id) == 1


def test_wake_never_raises(fake, monkeypatch):
    def broken(*args, **kwargs):
        raise ChatRefused("broken store")

    with ChatStore.maintainer() as current:
        posted = current.post("general", "@target hi")
        monkeypatch.setattr(ChatStore, "grants", broken)
        assert chat_wake.wake_mentioned(current, posted, send=fake.send) == []
    with ChatStore.maintainer() as current:
        assert chat_wake.wake_mentioned(current, Posted(1, "general", None, ()),
                                        send=fake.send) == []


def test_the_hook_role_cannot_claim_a_wake(fake):
    with ChatStore.hook(TARGET) as current:
        with pytest.raises(ChatRefused):
            current.claim_wake(TARGET)


# Posters


def test_the_cli_post_wakes_and_reports_on_stderr(fake, monkeypatch, capsys):
    monkeypatch.setattr(chat_command, "maintainer_refusal", lambda: None)
    monkeypatch.setattr(control, "send", fake.send)
    fake.updated = None
    parser = argparse.ArgumentParser()
    chat_command.add_arguments(parser)
    assert chat_command.run(parser.parse_args(["post", "general", "@target hi"])) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines()[-1] == "1 mentioned @target"
    assert "did not wake (not-idle) @target" in captured.err


def test_the_sidebar_post_wakes(fake, monkeypatch):
    monkeypatch.setattr(control, "send", fake.send)
    assert chat_panel.submit_line("@target hi", "general", lambda: None).startswith("Posted")
    assert [item[0] for item in fake.sent] == [TARGET]


# The focus readback


class FakeMux:
    def __init__(self, focused=None, error=None):
        self._focused = focused
        self._error = error

    def focused_panes(self, session):
        if self._error:
            raise MultiplexerError(self._error)
        return self._focused


def _target(monkeypatch, mux, pane_focused=False):
    ref = PaneRef("zellij", "agents", "1")
    target = control.Target(None, ref, PaneInfo(ref, "t", None, False, pane_focused), mux)
    monkeypatch.setattr(control, "_verify", lambda launch_id: target)


def test_client_focus_comes_from_the_attached_clients(monkeypatch):
    _target(monkeypatch, FakeMux({"1"}))
    assert control.client_focused(TARGET) is True
    _target(monkeypatch, FakeMux({"2"}), pane_focused=True)
    assert control.client_focused(TARGET) is False
    _target(monkeypatch, FakeMux(error="no answer"))
    with pytest.raises(control.ControlError, match="attached clients"):
        control.client_focused(TARGET)


def test_without_a_client_readback_the_pane_focus_flag_decides(monkeypatch):
    _target(monkeypatch, object(), pane_focused=True)
    assert control.client_focused(TARGET) is True


def test_zellij_lists_the_panes_clients_have_focused(monkeypatch):
    mux = ZellijMultiplexer(socket_dir=None, binary="zellij")
    seen = []

    def clients(session, socket_dir):
        seen.append(session)
        return {"1": "terminal_3", "2": "plugin_0", "3": "terminal_7"}

    monkeypatch.setattr(mux, "_clients", clients)
    assert mux.focused_panes("agents") == {"3", "7"}
    assert seen == ["agents"]


def test_an_unexpected_wake_error_never_fails_the_post(monkeypatch):
    def boom(*args, **kwargs):
        raise AttributeError("unexpected")

    monkeypatch.setattr(chat_wake, "_wake_one", boom)

    class Store:
        def grants(self):
            raise AttributeError("unexpected too")

    assert chat_wake.wake_mentioned(Store(), object()) == []
