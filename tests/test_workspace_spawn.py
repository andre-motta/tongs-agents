"""The single agent spawn path, spawn requests and the sidebar pane (mocked mux)."""

import json
import os
import stat
import sys

import pytest

from tongs_agents import launches, workspace_spawn
from tongs_agents.models import FocusOperation, FocusResult
from tongs_agents.multiplexers import MultiplexerError, PaneInfo, PaneRef
from tongs_agents.multiplexers.zellij import ACTIVATION_SKIPPED
from tongs_agents.profiles import Profile
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")


class FakeMux:
    """A Multiplexer double that records spawns and focus calls."""

    name = "zellij"

    def __init__(self, *, here=None, exists=False, available=(True, "zellij 0.45.1"),
                 panes=(), fail_spawn=None, state=None, fail_delete=False):
        self.here = here
        self.exists = exists
        self.state = state
        self.fail_delete = fail_delete
        self._available = available
        self.panes = list(panes)
        self.fail_spawn = fail_spawn or set()
        self.spawned = []
        self.focused = []
        self.calls = []

    def available(self):
        self.calls.append("available")
        return self._available

    def in_session(self):
        return self.here

    def session_state(self, session):
        self.calls.append(("session_state", session))
        if self.state is not None:
            return self.state
        return "running" if self.exists else None

    def session_exists(self, session):
        self.calls.append(("session_exists", session))
        return self.exists

    def delete_session(self, session):
        self.calls.append(("delete_session", session))
        if self.fail_delete:
            raise MultiplexerError(f"cannot delete {session}")
        self.state = None

    def spawn(self, session, argv, *, cwd=None, name=None, focus=False):
        if name in self.fail_spawn:
            raise MultiplexerError(f"cannot spawn {name}")
        pane_id = str(len(self.panes) + 1)
        ref = PaneRef(self.name, session, pane_id, None)
        self.panes.append(PaneInfo(ref, name or "", argv[0], False, False))
        self.spawned.append({
            "session": session, "argv": list(argv), "name": name, "focus": focus, "cwd": cwd,
        })
        return ref

    def list_panes(self, session):
        self.calls.append(("list_panes", session))
        return list(self.panes)

    def focus(self, ref):
        self.focused.append(ref)
        return FocusResult(
            state="partial", reason="pane selected", backend="zellij", target_found=True,
            selection=FocusOperation(True, True, "tab or pane selection"),
            activation=FocusOperation.unavailable(ACTIVATION_SKIPPED),
        )


def profile(name, harness="other", argv=("sleep", "600")):
    return Profile(name=name, harness=harness, argv=tuple(argv))


def write_profiles(tmp_path, names):
    config = tmp_path / "config"
    config.mkdir(mode=0o700, exist_ok=True)
    path = config / "config.toml"
    path.write_text("".join(
        f'[profiles.{name}]\ncommand = ["sleep", "600"]\n' for name in names
    ))
    path.chmod(0o600)
    return path


@pytest.fixture(autouse=True)
def runtime_dir(tmp_path, monkeypatch):
    directory = tmp_path / "xdg-run"
    directory.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(directory))
    return directory / "tongs-agents"


def test_spawn_profiles_runs_the_launcher_for_each_profile():
    mux = FakeMux()
    records = workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha"), profile("beta")])
    assert [record.profile for record in records] == ["alpha", "beta"]
    assert [call["argv"] for call in mux.spawned] == [
        [sys.executable, "-m", "tongs_agents.launch", record.profile,
         "--launch-id", record.launch_id]
        for record in records
    ]
    assert [call["name"] for call in mux.spawned] == ["alpha", "beta"]
    assert all(call["session"] == "agents" and call["focus"] is False for call in mux.spawned)
    for record in records:
        stored = launches.get(record.launch_id)
        assert (stored.state, stored.session, stored.multiplexer) == ("pending", "agents", "zellij")


def test_spawn_profiles_stops_at_the_first_failure():
    mux = FakeMux(fail_spawn={"beta"})
    with pytest.raises(MultiplexerError):
        workspace_spawn.spawn_profiles(
            mux, "agents", [profile("alpha"), profile("beta"), profile("gamma")]
        )
    assert [call["name"] for call in mux.spawned] == ["alpha"]


def test_ensure_sidebar_adds_one_only_when_absent():
    mux = FakeMux()
    ref = workspace_spawn.ensure_sidebar(mux, "agents")
    assert ref is not None
    assert mux.spawned[0]["name"] == SIDEBAR_PANE_NAME
    assert mux.spawned[0]["argv"] == [
        sys.executable, "-m", "tongs_agents.commands.sidebar", "--compact"
    ]
    assert workspace_spawn.ensure_sidebar(mux, "agents") is None
    assert len(mux.spawned) == 1


def test_an_exited_sidebar_does_not_count():
    exited = PaneInfo(PaneRef("zellij", "agents", "0"), SIDEBAR_PANE_NAME, None, True, False)
    mux = FakeMux(panes=[exited])
    assert workspace_spawn.ensure_sidebar(mux, "agents") is not None


@POSIX_MODES
def test_spawn_request_files_are_private(runtime_dir):
    token = workspace_spawn.write_spawn_request("agents", ["a" * 32])
    path = workspace_spawn.request_path(token)
    assert path == runtime_dir / "requests" / f"{token}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text()) == {
        "schema_version": 1, "session": "agents", "launches": ["a" * 32],
    }
    layout = workspace_spawn.write_layout(token, "layout {}\n")
    assert stat.S_IMODE(layout.stat().st_mode) == 0o600


def test_a_request_is_consumed_once():
    token = workspace_spawn.write_spawn_request("agents", ["b" * 32])
    request = workspace_spawn.consume_spawn_request(token)
    assert request == workspace_spawn.SpawnRequest("agents", ("b" * 32,))
    assert not workspace_spawn.request_path(token).exists()
    assert workspace_spawn.consume_spawn_request(token) is None
    assert not any(workspace_spawn.request_path(token).parent.iterdir())


def test_write_private_file_never_replaces(tmp_path):
    target = tmp_path / "file"
    workspace_spawn.write_private_file(target, b"one")
    with pytest.raises(FileExistsError):
        workspace_spawn.write_private_file(target, b"two")
    assert target.read_bytes() == b"one"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_symlinked_request_is_refused(tmp_path):
    token = workspace_spawn.write_spawn_request("agents", [])
    path = workspace_spawn.request_path(token)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"schema_version": 1, "session": "agents", "launches": []}))
    path.unlink()
    path.symlink_to(elsewhere)
    with pytest.raises(workspace_spawn.SpawnRequestError):
        workspace_spawn.consume_spawn_request(token)
    assert not os.path.lexists(path)
    assert elsewhere.exists()


@pytest.mark.parametrize("payload", [
    b"not json",
    json.dumps({"schema_version": 2, "session": "agents", "launches": []}).encode(),
    json.dumps({"schema_version": 1, "session": "a\nb", "launches": []}).encode(),
    json.dumps({"schema_version": 1, "session": "agents", "launches": ["../x"]}).encode(),
    b" " * (workspace_spawn.MAX_REQUEST_BYTES + 1),
])
def test_malformed_requests_are_refused(payload):
    token = workspace_spawn.write_spawn_request("agents", [])
    workspace_spawn.request_path(token).write_bytes(payload)
    with pytest.raises(workspace_spawn.SpawnRequestError):
        workspace_spawn.consume_spawn_request(token)
    assert not workspace_spawn.request_path(token).exists()


@pytest.mark.parametrize("token", ["", "A" * 32, "a" * 31, "../" + "a" * 29, None])
def test_invalid_tokens_are_refused(token):
    with pytest.raises(workspace_spawn.SpawnRequestError):
        workspace_spawn.request_path(token)


def test_spawn_requested_spawns_pending_launches_once():
    records = workspace_spawn.create_launches("zellij", "agents", [profile("alpha"), profile("beta")])
    token = workspace_spawn.write_spawn_request("agents", [r.launch_id for r in records])
    workspace_spawn.write_layout(token, "layout {}\n")
    mux = FakeMux()
    seen = []
    failures = workspace_spawn.spawn_requested(
        mux, "agents", token, lambda launch_id, ref: seen.append((launch_id, ref.pane_id))
    )
    assert failures == []
    assert [call["argv"][-1] for call in mux.spawned] == [r.launch_id for r in records]
    assert seen == [(records[0].launch_id, "1"), (records[1].launch_id, "2")]
    assert not workspace_spawn.layout_path(token).exists()

    second = FakeMux()
    assert workspace_spawn.spawn_requested(second, "agents", token) == []
    assert second.spawned == []


def test_spawn_requested_refuses_another_session():
    records = workspace_spawn.create_launches("zellij", "agents", [profile("alpha")])
    token = workspace_spawn.write_spawn_request("agents", [records[0].launch_id])
    mux = FakeMux()
    failures = workspace_spawn.spawn_requested(mux, "other", token)
    assert len(failures) == 1 and "other" in failures[0]
    assert mux.spawned == []


def test_spawn_requested_reports_failures_and_skips_claimed():
    alpha, beta, gamma = workspace_spawn.create_launches(
        "zellij", "agents", [profile("alpha"), profile("beta"), profile("gamma")]
    )
    launches.claim(gamma.launch_id, os.getpid(), None, None)
    missing = "c" * 32
    token = workspace_spawn.write_spawn_request(
        "agents", [alpha.launch_id, beta.launch_id, gamma.launch_id, missing]
    )
    mux = FakeMux(fail_spawn={"beta"})
    failures = workspace_spawn.spawn_requested(mux, "agents", token)
    assert [call["name"] for call in mux.spawned] == ["alpha"]
    assert failures == [
        "profile beta: cannot spawn beta",
        f"launch {missing}: no launch record",
    ]


def test_spawn_requested_reports_a_bad_request():
    token = workspace_spawn.write_spawn_request("agents", [])
    workspace_spawn.request_path(token).write_text("{}")
    mux = FakeMux()
    assert workspace_spawn.spawn_requested(mux, "agents", token) == ["unrecognized spawn request"]
    assert mux.spawned == []
