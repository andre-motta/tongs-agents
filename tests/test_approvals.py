"""The sidebar's approvals poll survives an unexpected error in its worker (#116)."""

import threading
import time

import pytest
from textual.app import App

from tongs_agents.sandbox.approvals import RuleListing
from tongs_agents.widgets import approvals as approval_widgets
from tongs_agents.widgets.approvals import ApprovalActions

KEY = (None, "tongs-boxed")


class Feed:
    def snapshot(self):
        return []


class Panel:
    def __init__(self):
        self.polls = 0

    def poll(self):
        self.polls += 1


class Host(ApprovalActions, App):
    def __init__(self):
        super().__init__()
        self.init_approvals(Feed(), interval=60)
        self.approval_feed.launch_targets = {"launch": KEY}
        self.agents_panel = Panel()
        self.toasts = []

    def on_mount(self):
        self.mount_approvals()

    def notify(self, message, **kwargs):
        self.toasts.append((message, kwargs))
        super().notify(message, **kwargs)


def scripted(monkeypatch, outcomes):
    calls = []

    def poll(targets, client_for=None):
        calls.append(set(targets))
        outcome = outcomes[min(len(calls), len(outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(approval_widgets.approvals, "poll", poll)
    return calls


async def poll_once(app, pilot):
    app.poll_approvals()
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def errors(app):
    return [(message, kwargs) for message, kwargs in app.toasts
            if kwargs.get("severity") == "error"]


@pytest.mark.asyncio
async def test_a_failing_poll_is_a_notification_and_polling_goes_on(monkeypatch):
    listing = RuleListing(KEY[1], KEY[0])
    calls = scripted(monkeypatch, [ValueError("bad [bold]chunk[/]"), {KEY: listing}])
    app = Host()
    async with app.run_test(notifications=True) as pilot:
        await poll_once(app, pilot)
        assert app.is_running and not app.approval_polling
        (message, kwargs), = errors(app)
        assert "ValueError: bad [bold]chunk[/]" in message
        assert kwargs["markup"] is False and kwargs["title"] == "Access requests"
        assert app.approval_poll_error is not None

        await poll_once(app, pilot)
        assert len(calls) == 2
        assert app.approval_listings == {KEY: listing}
        assert app.approval_poll_error is None
        assert len(errors(app)) == 1


@pytest.mark.asyncio
async def test_a_failure_that_repeats_is_notified_once(monkeypatch):
    calls = scripted(monkeypatch, [RuntimeError("gateway gone"), RuntimeError("gateway gone"),
                                   KeyError("chunk"), {}, RuntimeError("gateway gone")])
    app = Host()
    async with app.run_test(notifications=True) as pilot:
        for _ in range(5):
            await poll_once(app, pilot)
        assert app.is_running and len(calls) == 5
        assert [message.rsplit(": ", 2)[-2:] for message, _ in errors(app)] == [
            ["RuntimeError", "gateway gone"],
            ["KeyError", "'chunk'"],
            ["RuntimeError", "gateway gone"],
        ]


@pytest.mark.asyncio
async def test_a_failure_behind_a_queued_poll_still_runs_it_again(monkeypatch):
    gate = threading.Event()
    calls = []

    def poll(targets, client_for=None):
        calls.append(set(targets))
        if len(calls) == 1:
            gate.wait(10)
            raise OSError("boom")
        return {}

    monkeypatch.setattr(approval_widgets.approvals, "poll", poll)
    app = Host()
    async with app.run_test(notifications=True) as pilot:
        app.poll_approvals()
        await pilot.pause(0.05)
        app.poll_approvals()
        assert len(calls) == 1 and app.approval_poll_again
        gate.set()
        deadline = time.monotonic() + 5
        while app.approval_polling or len(calls) < 2:
            assert time.monotonic() < deadline, "the queued poll never ran"
            await pilot.pause(0.01)
        await pilot.pause()
        assert app.is_running and len(calls) == 2
        assert len(errors(app)) == 1 and app.approval_poll_error is None
