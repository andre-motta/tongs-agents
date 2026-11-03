"""The supervisor verbs: registration, argument handling and exit codes."""

import io
import json
import sys

import pytest

from tongs_agents import cli, commands, control
from tongs_agents.commands import supervisor

LAUNCH = "c" * 32


def _status(state="done", **extra):
    values = dict(launch_id=LAUNCH, profile="r1", harness="other", session="agents",
                  pane_id="3", state=state, detail="", exit_status=None, updated=None)
    values.update(extra)
    return control.AgentStatus(**values)


def invoke(argv):
    parser, handlers = cli.build_parser()
    args = parser.parse_args(argv)
    return handlers[args.command](args)


class Calls(list):
    record = None


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()
    monkeypatch.delenv(control.CALLER_ENV, raising=False)

    def record(name, result=None):
        def call(*args, **kwargs):
            seen.append((name, args, kwargs))
            if isinstance(result, BaseException):
                raise result
            return result
        return call

    seen.record = record
    return seen


def test_the_verbs_are_registered_once_and_sorted():
    names = [commands.command_name(module) for module in commands.discover()]
    assert {"send", "read", "wait", "agent-status"} <= set(names)
    assert names == sorted(names)
    parser, handlers = cli.build_parser()
    assert handlers["send"] is supervisor.run_send
    assert handlers["agent-status"] is supervisor.run_status


def test_send_passes_text_and_actor(calls, monkeypatch):
    monkeypatch.setattr(control, "send", calls.record("send"))
    monkeypatch.setenv(control.CALLER_ENV, "d" * 32)

    assert invoke(["send", LAUNCH, "hello there"]) == 0

    assert calls == [("send", (LAUNCH, "hello there"),
                      {"actor": "cli", "submit": True, "caller": "d" * 32})]


def test_send_no_submit(calls, monkeypatch):
    monkeypatch.setattr(control, "send", calls.record("send"))
    invoke(["send", "--no-submit", LAUNCH, "draft"])
    assert calls[0][2]["submit"] is False


def test_send_reads_standard_input_for_dash(calls, monkeypatch):
    monkeypatch.setattr(control, "send", calls.record("send"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"line one\r\nline two\n")))

    assert invoke(["send", LAUNCH, "-"]) == 0

    assert calls[0][1] == (LAUNCH, "line one\nline two")


@pytest.mark.parametrize(
    "data, message",
    [(b"x" * (control.MAX_SEND_BYTES + 2), "limit"), (b"\xff\xfe", "UTF-8")],
)
def test_send_refuses_bad_standard_input(calls, monkeypatch, capsys, data, message):
    monkeypatch.setattr(control, "send", calls.record("send"))
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))

    assert invoke(["send", LAUNCH, "-"]) == control.EXIT_ERROR

    assert calls == []
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "error, code",
    [
        (control.ControlError("bad"), 1),
        (control.AgentExited("exited", 0), 4),
        (control.Refused("not spawned by tongs-agents"), 5),
        (control.ControlUnsupported("no adapter"), 6),
    ],
)
def test_send_maps_errors_to_exit_codes(calls, monkeypatch, capsys, error, code):
    monkeypatch.setattr(control, "send", calls.record("send", error))
    assert invoke(["send", LAUNCH, "hi"]) == code
    assert capsys.readouterr().err.startswith("tongs-agents: ")


def test_read_prints_the_tail(calls, monkeypatch, capsys):
    monkeypatch.setattr(control, "read", calls.record("read", "a\nb"))
    assert invoke(["read", LAUNCH, "--lines", "2"]) == 0
    assert capsys.readouterr().out == "a\nb\n"
    assert calls[0][1] == (LAUNCH, 2)


def test_read_refused_exits_5(calls, monkeypatch):
    monkeypatch.setattr(control, "read", calls.record("read", control.Refused("no")))
    assert invoke(["read", LAUNCH]) == 5


@pytest.mark.parametrize(
    "code, shown",
    [(0, "done"), (2, "timeout"), (3, "needs-you"), (4, "ended")],
)
def test_wait_exit_codes(calls, monkeypatch, capsys, code, shown):
    state = {2: "working"}.get(code, shown)
    monkeypatch.setattr(control, "wait", calls.record("wait", control.WaitResult(_status(state), code)))

    assert invoke(["wait", LAUNCH, "--until", "done", "--timeout", "30"]) == code

    assert capsys.readouterr().out == f"{LAUNCH} {shown}\n"
    assert calls[0][1] == (LAUNCH, "done", 30.0)


def test_wait_defaults(calls, monkeypatch):
    monkeypatch.setattr(control, "wait", calls.record("wait", control.WaitResult(_status(), 0)))
    invoke(["wait", LAUNCH])
    assert calls[0][1] == (LAUNCH, "done", 600.0)


def test_wait_rejects_unknown_until():
    with pytest.raises(SystemExit):
        invoke(["wait", LAUNCH, "--until", "finished"])


def test_agent_status_json_for_one_launch(calls, monkeypatch, capsys):
    monkeypatch.setattr(control, "status", calls.record("status", _status("needs-you")))
    assert invoke(["agent-status", LAUNCH, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "needs-you"


def test_agent_status_lists_every_launch(calls, monkeypatch, capsys):
    rows = [_status("working"), _status("refused", launch_id="e" * 32, detail="stale")]
    monkeypatch.setattr(control, "list_statuses", calls.record("list", rows))

    assert invoke(["agent-status"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == [f"{LAUNCH}  r1  agents:3  working", f"{'e' * 32}  r1  agents:3  refused  stale"]


def test_agent_status_refused_exits_5(calls, monkeypatch):
    monkeypatch.setattr(control, "status", calls.record("status", control.Refused("no")))
    assert invoke(["agent-status", LAUNCH]) == 5
