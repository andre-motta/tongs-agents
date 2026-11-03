from types import SimpleNamespace

import pytest

from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.widgets import agents_panel


def _result(state):
    if state == "complete":
        operation = FocusOperation(True, True, "ok")
        return FocusResult(
            state=state,
            reason="done",
            target_found=True,
            selection=operation,
            activation=operation,
        )
    if state == "partial":
        return FocusResult(
            state=state,
            reason="window only",
            target_found=True,
            selection=FocusOperation.unavailable("unsupported"),
            activation=FocusOperation(True, True, "ok"),
        )
    if state == "failed":
        return FocusResult(
            state=state,
            reason="terminal focus operations failed",
            target_found=True,
            selection=FocusOperation(True, False, "Konsole could not select tab 2"),
            activation=FocusOperation(
                True, False, "window did not become active within 1s"
            ),
        )
    return FocusResult(state=state, reason="no safe target")


@pytest.mark.parametrize(
    ("state", "message", "severity"),
    [
        ("complete", "Focused: repo", "information"),
        (
            "partial",
            "Partially focused: repo (selection: unsupported; activation: ok)",
            "warning",
        ),
        (
            "failed",
            "Focus Failed: repo: terminal focus operations failed"
            " (selection: Konsole could not select tab 2;"
            " activation: window did not become active within 1s)",
            "error",
        ),
        ("unavailable", "Focus Unavailable: repo: no safe target", "error"),
        ("not-found", "Focus Not found: repo: no safe target", "error"),
    ],
)
@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("tongs_agents.tui", "FleetMonitorApp"),
        ("tongs_agents.views.fleet_screen", "FleetScreen"),
    ],
)
def test_focus_consumer_notifies_completed_result(
    module_name, class_name, state, message, severity
):
    module = __import__(module_name, fromlist=[class_name])
    consumer_class = getattr(module, class_name)
    notifications = []
    consumer = SimpleNamespace(
        notify=lambda text, **kwargs: notifications.append((text, kwargs))
    )
    consumer_class._notify_focus_result(consumer, "repo", _result(state))
    assert notifications == [(message, {"severity": severity, "timeout": 3})]


class _ImmediateThread:
    def __init__(self, target, daemon):
        self.target = target

    def start(self):
        self.target()


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("tongs_agents.tui", "FleetMonitorApp"),
        ("tongs_agents.views.fleet_screen", "FleetScreen"),
        ("tongs_agents.widgets.agents_panel", "FocusOnSelect"),
    ],
)
def test_focus_consumer_dispatches_worker_result_to_ui_thread(
    monkeypatch, module_name, class_name
):
    module = __import__(module_name, fromlist=[class_name])
    consumer_class = getattr(module, class_name)
    expected = _result("partial")
    callbacks = []

    def dispatch(callback, *args):
        callbacks.append((callback, args))

    consumer = SimpleNamespace(
        _notify_focus_result=lambda *args: None,
        call_from_thread=dispatch,
        app=SimpleNamespace(call_from_thread=dispatch),
    )
    monkeypatch.setattr(agents_panel, "focus_session", lambda identity: expected)
    monkeypatch.setattr(agents_panel.threading, "Thread", _ImmediateThread)
    session = SimpleNamespace(identity="canonical", repo="repo")
    consumer_class._do_focus(consumer, session)
    assert callbacks == [
        (consumer._notify_focus_result, ("repo", expected))
    ]


def test_focus_failure_is_reported_not_raised(monkeypatch):
    callbacks = []

    def boom(identity):
        raise RuntimeError("no terminal")

    consumer = SimpleNamespace(
        _notify_focus_result=lambda *args: None,
        app=SimpleNamespace(call_from_thread=lambda cb, *args: callbacks.append(args)),
    )
    monkeypatch.setattr(agents_panel, "focus_session", boom)
    monkeypatch.setattr(agents_panel.threading, "Thread", _ImmediateThread)
    agents_panel.FocusOnSelect._do_focus(
        consumer, SimpleNamespace(identity="canonical", repo="repo")
    )
    [(repo, result)] = callbacks
    assert repo == "repo"
    assert result.state == "failed"
    assert result.reason == "no terminal"


def test_selected_message_is_handled_by_the_host_mixin():
    focused = []
    consumer = SimpleNamespace(_do_focus=focused.append)
    stopped = []
    event = SimpleNamespace(session="s", stop=lambda: stopped.append(1))
    agents_panel.FocusOnSelect.on_agents_panel_selected(consumer, event)
    assert focused == ["s"]
    assert stopped == [1]
