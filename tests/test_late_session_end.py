"""A late Codex SessionEnd from the shared daemon carries a stale launch env (#71).

The payloads are synthetic, modelled on the live probe in
validation/evidence/harness-facts-fedora44.json (codex tui_hook_env): with the
shared Codex app-server daemon, the SessionEnd for a later TUI ran about 58 s
after that TUI exited, inside the daemon, with the FIRST daemon-attached
launch's TONGS_AGENTS_LAUNCH_ID and TONGS_AGENTS_PROFILE.
"""

import io
import json

import pytest

from tongs_agents import hook
from tongs_agents.discovery import (
    ProcessInfo,
    _record_path,
    read_session_record,
    read_stored_sessions,
)
from tongs_agents.hook import handle
from tongs_agents.models import (
    LAUNCH_ID_ENV,
    PROFILE_ID_ENV,
    UNRESOLVED_INSTANCE_ID,
    canonical_session_id,
)

FIRST_LAUNCH = {
    "launch_id": "0123456789abcdef0123456789abcdef",
    "profile_id": "first",
}
LATER_LAUNCH = {
    "launch_id": "fedcba9876543210fedcba9876543210",
    "profile_id": "later",
}
FIRST_SESSION = "11111111-1111-4111-8111-111111111111"
LATER_SESSION = "22222222-2222-4222-8222-222222222222"
CWD = "/tmp/repo"
DAEMON = ProcessInfo(
    4242, 1, "codex", "codex", ("codex", "app-server", "--listen"), CWD, "900"
)
DAEMON_INSTANCE = "4242:900"
TUI = ProcessInfo(5151, 77, "codex", "codex", ("codex", "--no-daemon"), CWD, "950")
TUI_INSTANCE = "5151:950"


@pytest.fixture(autouse=True)
def _no_inherited_launch(monkeypatch):
    monkeypatch.delenv(LAUNCH_ID_ENV, raising=False)
    monkeypatch.delenv(PROFILE_ID_ENV, raising=False)
    monkeypatch.setattr(
        "tongs_agents.terminal_apis.capture_terminal_info",
        lambda: {"terminal": "test", "terminal_env": {}},
    )


def set_env(monkeypatch, launch):
    if launch is None:
        monkeypatch.delenv(LAUNCH_ID_ENV, raising=False)
        monkeypatch.delenv(PROFILE_ID_ENV, raising=False)
        return
    monkeypatch.setenv(LAUNCH_ID_ENV, launch["launch_id"])
    monkeypatch.setenv(PROFILE_ID_ENV, launch["profile_id"])


def codex_event(monkeypatch, event, session_id, process, launch, hook_name):
    set_env(monkeypatch, launch)
    monkeypatch.setattr(
        "tongs_agents.hook.resolve_process_identity",
        lambda harness, pid=None: process,
    )
    payload = {"session_id": session_id, "cwd": CWD, "hook_event_name": hook_name}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    return handle(event, "codex")


def start(monkeypatch, session_id, process, launch):
    return codex_event(
        monkeypatch, "session-start", session_id, process, launch, "SessionStart"
    )


def late_session_end(monkeypatch, session_id, process=DAEMON):
    """The daemon's late SessionEnd: the first launch's env, any session id."""
    return codex_event(
        monkeypatch, "session-end", session_id, process, FIRST_LAUNCH, "SessionEnd"
    )


def stored(session_id, instance):
    return read_session_record("codex", session_id, instance)


def record_file(session_id, instance):
    return _record_path(canonical_session_id("codex", session_id, instance))


@pytest.mark.parametrize(
    ("process", "instance"),
    [(DAEMON, DAEMON_INSTANCE), (None, UNRESOLVED_INSTANCE_ID)],
)
def test_late_end_keeps_the_ended_sessions_launch(
    fleet_dir, monkeypatch, process, instance
):
    assert start(monkeypatch, FIRST_SESSION, process, FIRST_LAUNCH)
    assert start(monkeypatch, LATER_SESSION, process, LATER_LAUNCH)
    first_path = record_file(FIRST_SESSION, instance)
    first_before = first_path.read_bytes()

    assert late_session_end(monkeypatch, LATER_SESSION, process)

    ended = stored(LATER_SESSION, instance)
    assert ended["status"] == "ended"
    assert ended["launch"] == LATER_LAUNCH
    assert first_path.read_bytes() == first_before
    assert stored(FIRST_SESSION, instance)["status"] == "started"


def test_late_end_does_not_label_an_unlabelled_session(fleet_dir, monkeypatch):
    assert start(monkeypatch, FIRST_SESSION, DAEMON, FIRST_LAUNCH)
    assert start(monkeypatch, LATER_SESSION, DAEMON, None)

    assert late_session_end(monkeypatch, LATER_SESSION)

    ended = stored(LATER_SESSION, DAEMON_INSTANCE)
    assert ended["status"] == "ended"
    assert "launch" not in ended


def test_late_end_without_a_record_creates_nothing(fleet_dir, monkeypatch):
    assert start(monkeypatch, FIRST_SESSION, DAEMON, FIRST_LAUNCH)
    first_path = record_file(FIRST_SESSION, DAEMON_INSTANCE)
    first_before = first_path.read_bytes()

    assert not late_session_end(monkeypatch, LATER_SESSION)

    assert [record["session_id"] for record in read_stored_sessions()] == [
        FIRST_SESSION
    ]
    assert first_path.read_bytes() == first_before


def test_late_daemon_end_cannot_end_another_instance(fleet_dir, monkeypatch):
    assert start(monkeypatch, LATER_SESSION, TUI, LATER_LAUNCH)
    path = record_file(LATER_SESSION, TUI_INSTANCE)
    before = path.read_bytes()

    assert not late_session_end(monkeypatch, LATER_SESSION, DAEMON)

    assert path.read_bytes() == before
    assert stored(LATER_SESSION, DAEMON_INSTANCE) is None
    assert [record["session_id"] for record in read_stored_sessions()] == [
        LATER_SESSION
    ]


def test_late_end_of_the_first_session_ends_only_that_session(fleet_dir, monkeypatch):
    assert start(monkeypatch, FIRST_SESSION, DAEMON, FIRST_LAUNCH)
    assert start(monkeypatch, LATER_SESSION, DAEMON, LATER_LAUNCH)

    assert late_session_end(monkeypatch, FIRST_SESSION)

    assert stored(FIRST_SESSION, DAEMON_INSTANCE)["status"] == "ended"
    assert stored(FIRST_SESSION, DAEMON_INSTANCE)["launch"] == FIRST_LAUNCH
    later = stored(LATER_SESSION, DAEMON_INSTANCE)
    assert later["status"] == "started"
    assert later["launch"] == LATER_LAUNCH


def test_session_end_never_reads_the_launch_env(monkeypatch):
    set_env(monkeypatch, FIRST_LAUNCH)
    assert hook._record_launch("session-end", None) is None
    assert hook._record_launch("session-end", {"launch": LATER_LAUNCH}) == LATER_LAUNCH
    assert hook._record_launch("session-end", {}) is None
    assert hook._record_launch("stop", {"launch": LATER_LAUNCH}) == LATER_LAUNCH
    assert hook._record_launch("prompt-submit", None) == FIRST_LAUNCH
    assert hook._record_launch("session-start", {"launch": LATER_LAUNCH}) == (
        FIRST_LAUNCH
    )
