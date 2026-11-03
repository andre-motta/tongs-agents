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
from tongs_agents.profiles import Profile, SandboxSpec
from tongs_agents.workspace_spawn import SIDEBAR_PANE_NAME

POSIX_MODES = pytest.mark.skipif(os.name == "nt", reason="POSIX modes")


class FakeMux:
    """A Multiplexer double that records spawns, focus, show and layout calls."""

    name = "zellij"

    def __init__(self, *, here=None, exists=False, available=(True, "zellij 0.45.1"),
                 panes=(), fail_spawn=None, state=None, fail_delete=False,
                 fail_show=False):
        self.fail_show = fail_show
        self.shown = []
        self.layouts = []
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

    def show(self, ref):
        self.shown.append(ref)
        if self.fail_show:
            raise MultiplexerError("no client to show it to")
        return FocusResult(
            state="partial", reason="pane selected", backend=self.name, target_found=True,
            selection=FocusOperation(True, True, "tab or pane selection"),
            activation=FocusOperation.unavailable(ACTIVATION_SKIPPED),
        )

    def set_layout(self, session, layout):
        self.layouts.append((session, layout))


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


class NoticingMux(FakeMux):
    """A FakeMux whose spawns leave a placement notice, as Zellij's does."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.notices = []

    def spawn(self, session, argv, **kwargs):
        self.notices.append(f"notice for {kwargs.get('name')}")
        return super().spawn(session, argv, **kwargs)

    def take_notices(self):
        notices, self.notices = self.notices, []
        return notices


def test_placement_notices_go_to_the_callers_list_or_stderr(capsys):
    mux = NoticingMux()
    notices = []
    workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha")], notices=notices)
    assert notices == ["notice for alpha"]
    assert capsys.readouterr().err == ""
    workspace_spawn.spawn_profiles(mux, "agents", [profile("beta")])
    assert capsys.readouterr().err == "tongs-agents: notice: notice for beta\n"
    assert workspace_spawn.take_notices(FakeMux()) == []


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


def _age(path, seconds):
    then = path.stat().st_mtime - seconds
    os.utime(path, (then, then))


def test_expired_requests_go_with_their_layouts_and_pending_launches():
    old_records = workspace_spawn.create_launches(
        "zellij", "agents", [profile("alpha"), profile("beta")]
    )
    launches.claim(old_records[1].launch_id, os.getpid(), None, None)
    old = workspace_spawn.write_spawn_request("agents", [r.launch_id for r in old_records])
    workspace_spawn.write_layout(old, "layout {}\n")
    stray = workspace_spawn.new_token()
    workspace_spawn.write_layout(stray, "layout {}\n")
    age = workspace_spawn.REQUEST_MAX_AGE + 60
    for path in (workspace_spawn.request_path(old), workspace_spawn.layout_path(old),
                 workspace_spawn.layout_path(stray)):
        _age(path, age)

    fresh_records = workspace_spawn.create_launches("zellij", "agents", [profile("gamma")])
    fresh = workspace_spawn.write_spawn_request("agents", [fresh_records[0].launch_id])
    workspace_spawn.write_layout(fresh, "layout {}\n")

    assert not workspace_spawn.request_path(old).exists()
    assert not workspace_spawn.layout_path(old).exists()
    assert not workspace_spawn.layout_path(stray).exists()
    assert launches.get(old_records[0].launch_id) is None
    assert launches.get(old_records[1].launch_id).state == "running"
    assert workspace_spawn.request_path(fresh).exists()
    assert workspace_spawn.layout_path(fresh).exists()
    assert launches.get(fresh_records[0].launch_id).state == "pending"


def test_prune_keeps_fresh_requests_and_ignores_foreign_names(runtime_dir):
    token = workspace_spawn.write_spawn_request("agents", [])
    other = workspace_spawn.request_path(token).parent / "notes.json"
    other.write_text("{}")
    _age(other, workspace_spawn.REQUEST_MAX_AGE + 60)
    assert workspace_spawn.prune_spawn_requests() == 0
    assert workspace_spawn.request_path(token).exists()
    assert other.exists()
    _age(workspace_spawn.request_path(token), workspace_spawn.REQUEST_MAX_AGE + 60)
    assert workspace_spawn.prune_spawn_requests() == 1
    assert not workspace_spawn.request_path(token).exists()


def test_prune_without_runtime_dirs_does_nothing():
    assert workspace_spawn.prune_spawn_requests() == 0


@POSIX_MODES
def test_prune_skips_a_shared_requests_dir(runtime_dir):
    token = workspace_spawn.write_spawn_request("agents", [])
    path = workspace_spawn.request_path(token)
    _age(path, workspace_spawn.REQUEST_MAX_AGE + 60)
    path.parent.chmod(0o755)
    try:
        assert workspace_spawn.prune_spawn_requests() == 0
        assert path.exists()
    finally:
        path.parent.chmod(0o700)


def _boxed(name="boxed", cwd=None):
    return Profile(name=name, harness="claude", argv=("claude",), cwd=cwd,
                   sandbox=SandboxSpec())


def test_spawn_profiles_records_the_folder_and_args(tmp_path):
    mux = FakeMux()
    [record] = workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha")],
                                              folder=str(tmp_path), args=["--resume"])
    stored = launches.get(record.launch_id)
    assert (stored.folder, stored.args) == (str(tmp_path), ("--resume",))
    assert mux.spawned[0]["argv"][-3:] == ["alpha", "--launch-id", record.launch_id]


def test_check_launch_refuses_missing_folders_before_any_pane(tmp_path):
    mux = FakeMux()
    with pytest.raises(workspace_spawn.LaunchOptionError, match="does not exist"):
        workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha"), profile("beta")],
                                       folder=str(tmp_path / "gone"))
    with pytest.raises(workspace_spawn.LaunchOptionError, match="does not exist"):
        workspace_spawn.create_launches("zellij", "agents", [profile("alpha")],
                                        folder=str(tmp_path / "gone"))
    assert mux.spawned == [] and launches.list_records() == []


def test_check_launch_keeps_the_sandbox_upload_rules(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = home / "src" / "app"
    project.mkdir(parents=True)
    (home / ".ssh").mkdir()
    monkeypatch.setenv("HOME", str(home))
    for unsafe, reason in ((home, "your home folder"), (home / ".ssh", "hidden folder"),
                           (tmp_path, "holds your home folder")):
        with pytest.raises(workspace_spawn.LaunchOptionError, match=reason):
            workspace_spawn.check_launch(_boxed(), str(unsafe))
        assert workspace_spawn.check_launch(profile("plain"), str(unsafe))[0] == str(unsafe)
    assert workspace_spawn.check_launch(_boxed(), str(project), ["-c"]) == (
        str(project), ("-c",),
    )


def test_check_launch_refuses_a_string_for_args():
    with pytest.raises(workspace_spawn.LaunchOptionError, match="list of strings"):
        workspace_spawn.check_launch(profile("alpha"), None, "--resume")


def test_spawn_profiles_shows_each_pane_once_it_is_up():
    mux = FakeMux()
    workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha"), profile("beta")])
    assert [ref.pane_id for ref in mux.shown] == ["1", "2"]
    assert mux.focused == []


def test_spawn_launch_shows_nothing_unless_asked():
    mux = FakeMux()
    record = launches.create_pending(profile("alpha"), "agents", "zellij")
    ref = workspace_spawn.spawn_launch(mux, record)
    assert mux.shown == []
    assert workspace_spawn.spawn_launch(mux, record, show=True) != ref
    assert len(mux.shown) == 1


def test_a_failed_show_does_not_fail_the_spawn():
    mux = FakeMux(fail_show=True)
    records = workspace_spawn.spawn_profiles(mux, "agents", [profile("alpha")])
    assert [record.profile for record in records] == ["alpha"]
    assert len(mux.shown) == 1
    assert launches.get(records[0].launch_id) is not None


def test_spawn_requested_shows_the_spawned_panes():
    mux = FakeMux()
    records = workspace_spawn.create_launches("zellij", "agents", [profile("alpha")])
    token = workspace_spawn.write_spawn_request("agents", [r.launch_id for r in records])
    assert workspace_spawn.spawn_requested(mux, "agents", token) == []
    assert [ref.pane_id for ref in mux.shown] == ["1"]


def test_ensure_sidebar_never_shows_the_sidebar():
    mux = FakeMux()
    workspace_spawn.ensure_sidebar(mux, "agents")
    assert [call["name"] for call in mux.spawned] == [SIDEBAR_PANE_NAME] and mux.shown == []


def test_home_argv_runs_this_module():
    assert workspace_spawn.home_argv() == [
        sys.executable, "-m", "tongs_agents.workspace_spawn", "home",
    ]


def test_home_prints_the_hint_and_stays_up(monkeypatch, capsys):
    class Stop(Exception):
        pass

    def sleep(seconds):
        raise Stop

    ignored = []
    monkeypatch.setattr(workspace_spawn.time, "sleep", sleep)
    monkeypatch.setattr(workspace_spawn.signal, "signal",
                        lambda number, handler: ignored.append((number, handler)))
    with pytest.raises(Stop):
        workspace_spawn.main(["home"])
    assert "Select one in the sidebar" in capsys.readouterr().out
    assert ignored == [(workspace_spawn.signal.SIGINT, workspace_spawn.signal.SIG_IGN)]


def test_main_refuses_anything_but_home(capsys):
    assert workspace_spawn.main([]) == 2
    assert workspace_spawn.main(["home", "x"]) == 2
    assert "usage" in capsys.readouterr().err
