"""TmuxMultiplexer: the argv shapes it runs, parsing and refusals (mocked tmux)."""

import subprocess

import pytest

from tongs_agents.multiplexers import (
    SESSION_RUNNING,
    Multiplexer,
    MultiplexerError,
    PaneRef,
    TmuxMultiplexer,
    Unsupported,
    get_multiplexer,
)
from tongs_agents.multiplexers import tmux as tmux_mux
from tongs_agents.terminal_apis import tmux as tmux_api

SOCKET = "/run/tmux-test/socket,with-comma"
TMUX_PREFIX = ["tmux", "-S", SOCKET]


def completed(argv, stdout="", returncode=0):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


class FakeTmux:
    """Answers tmux argv by subcommand and records every call with its kwargs."""

    def __init__(self):
        self.calls = []
        self.answers = {}

    def answer(self, subcommand, stdout="", returncode=0):
        self.answers.setdefault(subcommand, []).append((stdout, returncode))

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        subcommand = argv[3] if argv[1] == "-S" else argv[1]
        queue = self.answers.get(subcommand)
        if queue:
            stdout, code = queue.pop(0) if len(queue) > 1 else queue[0]
        else:
            stdout, code = "", 0
        return completed(argv, stdout, code)

    def argvs(self):
        return [argv for argv, _ in self.calls]

    def tails(self):
        return [argv[3:] for argv in self.argvs()]


@pytest.fixture
def fake(monkeypatch):
    runner = FakeTmux()
    monkeypatch.setattr(tmux_api.subprocess, "run", runner)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return runner


@pytest.fixture
def mux(fake):
    return TmuxMultiplexer(socket_dir=SOCKET)


def ref(pane="%3", session="agents"):
    return PaneRef("tmux", session, pane, SOCKET)


def test_registered_and_satisfies_the_protocol():
    mux = get_multiplexer("tmux", socket_dir=SOCKET)
    assert isinstance(mux, TmuxMultiplexer)
    assert isinstance(mux, Multiplexer)
    assert mux.name == "tmux"
    assert mux.socket == SOCKET


def test_every_call_targets_the_socket_with_stdin_closed(fake, mux):
    fake.answer("display-message", "$1\n")
    mux.session_state("agents")
    mux.list_panes("agents")
    mux.send_keys(ref(), ["Enter"])
    assert fake.calls
    for argv, kwargs in fake.calls:
        assert argv[:3] == TMUX_PREFIX
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 5


def test_socket_comes_from_tmux_env_else_the_default(monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX", f"{SOCKET},123,4")
    assert TmuxMultiplexer().socket == SOCKET
    monkeypatch.delenv("TMUX")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    expected = tmp_path.resolve() / f"tmux-{tmux_mux.os.getuid()}" / "default"
    assert TmuxMultiplexer().socket == str(expected)
    assert tmux_mux.default_socket({"TMUX_TMPDIR": ""}) == str(
        tmux_mux.os.path.realpath("/tmp") + f"/tmux-{tmux_mux.os.getuid()}/default"
    )
    assert tmux_mux.default_socket({"TMUX_TMPDIR": "relative"}) is None


@pytest.mark.parametrize("output, ok", [
    ("tmux 3.4\n", True),
    ("tmux 3.3a\n", True),
    ("tmux next-3.6\n", True),
    ("tmux 3.2\n", True),
    ("tmux 3.1c\n", False),
    ("tmux 2.9a\n", False),
])
def test_available_reads_the_version_live_and_checks_the_floor(fake, mux, monkeypatch,
                                                             output, ok):
    monkeypatch.setattr(tmux_mux.shutil, "which", lambda name: "/usr/bin/tmux")
    fake.answer("-V", output)
    available, detail = mux.available()
    assert available is ok
    assert fake.argvs() == [["tmux", "-V"]]
    assert mux.version == tmux_mux.parse_version(output)
    if not ok:
        assert "older than the supported floor 3.2" in detail


def test_available_without_tmux_or_a_readable_version(fake, mux, monkeypatch):
    monkeypatch.setattr(tmux_mux.shutil, "which", lambda name: None)
    assert mux.available() == (False, "tmux not found on PATH")
    monkeypatch.setattr(tmux_mux.shutil, "which", lambda name: "/usr/bin/tmux")
    fake.answer("-V", "tmux master\n")
    assert mux.available() == (False, "could not read the tmux version")
    assert mux.version is None


def test_session_state_uses_an_exact_target(fake, mux):
    assert mux.session_state("agents") == SESSION_RUNNING
    fake.answer("has-session", "", 1)
    assert mux.session_state("agents") is None
    assert fake.tails() == [["has-session", "-t", "=agents"]] * 2


def test_delete_session_is_always_refused(fake, mux):
    with pytest.raises(MultiplexerError, match="no EXITED sessions"):
        mux.delete_session("agents")
    assert fake.calls == []


def test_spawn_creates_a_detached_session_when_absent(fake, mux):
    fake.answer("has-session", "", 1)
    fake.answer("new-session", "%0\n")
    pane = mux.spawn("agents", ["python", "-m", "x"], name="sidebar", cwd="/")
    assert pane == PaneRef("tmux", "agents", "%0", SOCKET)
    assert fake.tails() == [
        ["has-session", "-t", "=agents"],
        ["new-session", "-d", "-s", "agents", "-P", "-F", "#{pane_id}", "-c", "/",
         "--", "python", "-m", "x"],
        ["set-option", "-p", "-t", "%0", tmux_mux.NAME_OPTION, "sidebar"],
    ]


def test_spawn_splits_the_current_window_of_a_running_session(fake, mux):
    fake.answer("split-window", "%5\n")
    pane = mux.spawn("agents", ["python", "-m", "x"], name="alpha")
    assert pane.pane_id == "%5"
    assert fake.tails() == [
        ["has-session", "-t", "=agents"],
        ["split-window", "-h", "-d", "-t", "=agents:", "-P", "-F", "#{pane_id}",
         "--", "python", "-m", "x"],
        ["set-option", "-p", "-t", "%5", tmux_mux.NAME_OPTION, "alpha"],
        ["select-layout", "-t", "%5", "-E"],
    ]


def test_spawn_with_focus_does_not_pass_detached(fake, mux):
    fake.answer("split-window", "%5\n")
    mux.spawn("agents", ["python", "-m", "x"], focus=True)
    split = fake.tails()[1]
    assert split[0] == "split-window" and "-d" not in split


def test_spawn_opens_a_new_window_when_the_split_fails(fake, mux):
    fake.answer("split-window", "", 1)
    fake.answer("new-window", "%6\n")
    assert mux.spawn("agents", ["python", "-m", "x"]).pane_id == "%6"
    assert [tail[0] for tail in fake.tails()] == ["has-session", "split-window",
                                                   "new-window"]
    assert fake.tails()[2][:4] == ["new-window", "-d", "-t", "=agents:"]


def test_spawn_raises_when_no_pane_can_be_added(fake, mux):
    fake.answer("split-window", "", 1)
    fake.answer("new-window", "", 1)
    with pytest.raises(MultiplexerError, match="could not add a pane"):
        mux.spawn("agents", ["python", "-m", "x"])


def test_spawn_refuses_an_unparsed_pane_id(fake, mux):
    fake.answer("split-window", "agents:1.2\n")
    with pytest.raises(MultiplexerError, match="new pane id"):
        mux.spawn("agents", ["python", "-m", "x"])


def test_single_argument_argv_never_reaches_the_shell(fake, mux):
    fake.answer("split-window", "%5\n")
    mux.spawn("agents", ["claude"])
    assert fake.tails()[1][-4:] == ["--", "env", "--", "claude"]


@pytest.mark.parametrize("argv", [[], [""], ["-x", "y"], ["a\0b", "c"], ["A=b"], "python"])
def test_spawn_refuses_bad_argv(fake, mux, argv):
    with pytest.raises(MultiplexerError, match="argv"):
        mux.spawn("agents", argv)
    assert fake.calls == []


@pytest.mark.parametrize("session", ["", "a:b", "-x", "=agents", "a\nb", 5])
def test_refused_session_names(fake, mux, session):
    with pytest.raises(MultiplexerError, match="session name"):
        mux.session_state(session)
    assert fake.calls == []


def test_a_new_session_name_with_a_dot_is_refused(fake, mux):
    fake.answer("has-session", "", 1)
    with pytest.raises(MultiplexerError, match="session name"):
        mux.spawn("agents.v2", ["python", "-m", "x"])
    assert [tail[0] for tail in fake.tails()] == ["has-session"]


def test_default_socket_dir_is_created_private_before_a_new_server(fake, monkeypatch,
                                                                   tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    fake.answer("has-session", "", 1)
    fake.answer("new-session", "%0\n")
    mux = TmuxMultiplexer()
    mux.spawn("agents", ["python", "-m", "x"])
    directory = tmp_path / f"tmux-{tmux_mux.os.getuid()}"
    assert directory.is_dir()
    assert directory.stat().st_mode & 0o077 == 0


def _default_dir(tmp_path):
    return tmp_path / f"tmux-{tmux_mux.os.getuid()}"


def _assert_refused(fake, match):
    mux = TmuxMultiplexer()
    with pytest.raises(MultiplexerError, match=match):
        mux.spawn("agents", ["python", "-m", "x"])
    with pytest.raises(MultiplexerError, match=match):
        mux.attach_argv("agents")
    assert fake.calls == []


def test_a_symlinked_default_socket_dir_is_refused(fake, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    other.chmod(0o777)
    _default_dir(tmp_path).symlink_to(other)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    _assert_refused(fake, "not a directory")


def test_a_symlink_to_a_private_dir_is_still_refused(fake, monkeypatch, tmp_path):
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    _default_dir(tmp_path).symlink_to(other)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    _assert_refused(fake, "not a directory")


def test_a_file_in_place_of_the_default_socket_dir_is_refused(fake, monkeypatch,
                                                              tmp_path):
    _default_dir(tmp_path).write_text("")
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    _assert_refused(fake, "not a directory")


@pytest.mark.parametrize("mode", [0o777, 0o755, 0o750, 0o701, 0o710])
def test_an_open_default_socket_dir_is_refused(fake, monkeypatch, tmp_path, mode):
    directory = _default_dir(tmp_path)
    directory.mkdir()
    directory.chmod(mode)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    _assert_refused(fake, "open to other users")


def test_a_default_socket_dir_owned_by_another_user_is_refused(fake, monkeypatch,
                                                               tmp_path):
    other_uid = tmux_mux.os.getuid() + 1
    directory = tmp_path / f"tmux-{other_uid}"
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(tmux_mux.os, "getuid", lambda: other_uid)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    _assert_refused(fake, "owned by another user")


def test_the_default_socket_dir_is_checked_on_every_use(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    mux = TmuxMultiplexer()
    assert mux.session_state("agents") == SESSION_RUNNING
    _default_dir(tmp_path).chmod(0o755)
    with pytest.raises(MultiplexerError, match="open to other users"):
        mux.session_state("agents")
    assert len(fake.calls) == 1


def test_an_explicit_or_env_socket_skips_the_default_dir_check(fake, monkeypatch,
                                                                tmp_path):
    directory = _default_dir(tmp_path)
    directory.mkdir()
    directory.chmod(0o777)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    assert TmuxMultiplexer(socket_dir=SOCKET).socket == SOCKET
    monkeypatch.setenv("TMUX", f"{SOCKET},123,4")
    assert TmuxMultiplexer().socket == SOCKET


LINES = [
    "$1\t@1\t%0\t0\t\t1\t1\ttongs-agents-sidebar\tpython3\thost",
    "$1\t@1\t%3\t1\t2\t0\t1\talpha\t\tagent title\twith tab",
    "$2\t@4\t%9\t0\t\t1\t1\t\tbash\tother session",
]


def test_list_panes_keeps_only_the_named_session(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("list-panes", "\n".join(LINES) + "\n")
    panes = mux.list_panes("agents")
    assert fake.tails() == [
        ["display-message", "-p", "-t", "=agents:", "#{session_id}"],
        ["list-panes", "-a", "-F", tmux_mux.PANE_FORMAT],
    ]
    assert [pane.ref for pane in panes] == [ref("%0"), ref("%3")]
    sidebar, alpha = panes
    assert sidebar.title == "tongs-agents-sidebar"
    assert sidebar.command == "python3" and sidebar.focused and not sidebar.exited
    assert alpha.exited and alpha.exit_status == 2 and not alpha.focused
    assert alpha.title == "alpha" and alpha.command is None


def test_list_panes_falls_back_to_the_pane_title(fake, mux):
    fake.answer("display-message", "$2\n")
    fake.answer("list-panes", "\n".join(LINES) + "\n")
    (pane,) = mux.list_panes("agents")
    assert pane.title == "other session"


def test_list_panes_of_an_unknown_session_raises(fake, mux):
    fake.answer("display-message", "\n")
    with pytest.raises(MultiplexerError, match="not found"):
        mux.list_panes("agents")


def test_list_panes_refuses_malformed_lines(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("list-panes", "$1\t@1\t%0\tmaybe\t\t1\t1\t\tsh\tt\n")
    with pytest.raises(MultiplexerError, match="unrecognized"):
        mux.list_panes("agents")


def test_focus_selects_window_and_pane_and_reads_back(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@2\n")
    fake.answer("display-message", "%0\n")
    fake.answer("display-message", "%3\n")
    result = mux.focus(ref())
    assert result.selection.succeeded and result.state == "partial"
    assert not result.activation.attempted
    assert fake.tails() == [
        ["display-message", "-p", "-t", "=agents:", "#{session_id}"],
        ["display-message", "-p", "-t", "%3", "#{session_id}\t#{window_id}"],
        ["display-message", "-p", "-t", "=agents:", "#{pane_id}"],
        ["select-window", "-t", "=agents:@2"],
        ["select-pane", "-t", "%3"],
        ["display-message", "-p", "-t", "=agents:", "#{pane_id}"],
    ]
    assert not any("switch-client" in tail or "attach-session" in tail
                   for tail in fake.tails())


def test_focus_of_the_active_pane_selects_nothing(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@2\n")
    fake.answer("display-message", "%3\n")
    result = mux.focus(ref())
    assert result.selection.succeeded
    assert result.selection.detail == tmux_mux.ALREADY_SELECTED
    assert [tail[0] for tail in fake.tails()].count("select-pane") == 0


def test_focus_fails_when_the_readback_differs(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@2\n")
    fake.answer("display-message", "%0\n")
    result = mux.focus(ref())
    assert result.state == "failed" and not result.selection.succeeded


def test_focus_refuses_a_pane_of_another_session(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$2\t@2\n")
    result = mux.focus(ref())
    assert result.state == "failed"
    assert "not in session" in result.selection.detail
    assert [tail[0] for tail in fake.tails()].count("select-window") == 0


def test_paste_loads_a_unique_buffer_from_stdin_then_pastes_bracketed(fake, mux):
    mux.paste(ref(), "line one\nline two")
    load, paste = fake.calls
    assert load[0][3:6] == ["load-buffer", "-b", load[0][5]]
    assert load[0][5].startswith(tmux_mux.BUFFER_PREFIX) and load[0][-1] == "-"
    assert load[1]["input"] == b"line one\nline two"
    assert "stdin" not in load[1]
    assert paste[0][3:] == ["paste-buffer", "-p", "-r", "-d", "-b", load[0][5], "-t", "%3"]
    assert paste[1]["stdin"] is subprocess.DEVNULL
    mux.paste(ref(), "again")
    assert fake.calls[2][0][5] != load[0][5]


def test_a_failed_paste_deletes_its_buffer(fake, mux):
    fake.answer("paste-buffer", "", 1)
    with pytest.raises(MultiplexerError):
        mux.paste(ref(), "text")
    buffer = fake.calls[0][0][5]
    assert fake.tails()[-1] == ["delete-buffer", "-b", buffer]


def test_a_failed_load_does_not_paste(fake, mux):
    fake.answer("load-buffer", "", 1)
    with pytest.raises(MultiplexerError, match="load-buffer"):
        mux.paste(ref(), "text")
    assert [tail[0] for tail in fake.tails()] == ["load-buffer"]


@pytest.mark.parametrize("text", ["a\0b", "x" * (64 * 1024 + 1), "\udc80", 3])
def test_paste_refuses_bad_text(fake, mux, text):
    with pytest.raises(MultiplexerError):
        mux.paste(ref(), text)
    assert fake.calls == []


def test_send_keys_maps_named_keys(fake, mux):
    mux.send_keys(ref(), ["Enter"])
    mux.send_keys(ref(), ["Escape", "Backspace"])
    assert fake.tails() == [["send-keys", "-t", "%3", "Enter"],
                            ["send-keys", "-t", "%3", "Escape", "BSpace"]]


@pytest.mark.parametrize("keys", [[], ["hello"], ["C-c"], "Enter", ["Enter"] * 17])
def test_send_keys_refuses_literal_text_and_unknown_keys(fake, mux, keys):
    with pytest.raises(MultiplexerError, match="keys"):
        mux.send_keys(ref(), keys)
    assert fake.calls == []


def test_capture_joins_wraps_and_returns_the_tail(fake, mux):
    fake.answer("capture-pane", "one\ntwo  \nthree\n\n\n")
    assert mux.capture(ref(), 2) == "two\nthree"
    assert fake.tails() == [["capture-pane", "-p", "-J", "-t", "%3", "-S", "-2"]]


@pytest.mark.parametrize("lines", [0, 10_001, True, 2.0])
def test_capture_refuses_bad_line_counts(fake, mux, lines):
    with pytest.raises(MultiplexerError, match="line count"):
        mux.capture(ref(), lines)


def test_subscribe_is_unsupported(fake, mux):
    with pytest.raises(Unsupported):
        mux.subscribe(ref())


@pytest.mark.parametrize("bad", [
    PaneRef("zellij", "agents", "%3", SOCKET),
    PaneRef("tmux", "agents", "3", SOCKET),
    PaneRef("tmux", "agents", "%3", "relative/socket"),
    "agents:%3",
])
def test_pane_io_refuses_foreign_or_malformed_refs(fake, mux, bad):
    for call in (lambda: mux.paste(bad, "x"), lambda: mux.send_keys(bad, ["Enter"]),
                 lambda: mux.capture(bad)):
        with pytest.raises(MultiplexerError):
            call()
    assert mux.focus(bad).state == "failed"
    assert fake.calls == []


def test_ref_socket_overrides_the_adapter_socket(fake):
    mux = TmuxMultiplexer(socket_dir="/other/socket")
    mux.send_keys(ref(), ["Enter"])
    assert fake.argvs()[0][:3] == TMUX_PREFIX


def test_in_session_reads_the_session_name(fake, monkeypatch):
    monkeypatch.setenv("TMUX", f"{SOCKET},123,4")
    monkeypatch.setenv("TMUX_PANE", "%7")
    fake.answer("display-message", "work\n")
    assert TmuxMultiplexer().in_session() == PaneRef("tmux", "work", "%7", SOCKET)
    assert fake.tails() == [["display-message", "-p", "-t", "%7", "#{session_name}"]]


@pytest.mark.parametrize("env", [
    {"TMUX_PANE": "%7"},
    {"TMUX": f"{SOCKET},123,4"},
    {"TMUX": f"{SOCKET},123,4", "TMUX_PANE": "7"},
    {"TMUX": "relative,1,2", "TMUX_PANE": "%7"},
])
def test_in_session_needs_a_valid_tmux_identity(fake, monkeypatch, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert TmuxMultiplexer().in_session() is None
    assert fake.calls == []


def test_attach_argv_names_socket_and_exact_session(mux):
    assert mux.attach_argv("work") == [*TMUX_PREFIX, "attach-session", "-t", "=work"]
