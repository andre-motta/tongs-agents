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


LAYOUT_QUERY = ["show-options", "-q", "-v", "-t", "=agents:", tmux_mux.LAYOUT_OPTION]


def test_registered_and_satisfies_the_protocol():
    mux = get_multiplexer("tmux", socket_dir=SOCKET)
    assert isinstance(mux, TmuxMultiplexer)
    assert isinstance(mux, Multiplexer)
    assert mux.name == "tmux"
    assert mux.socket == SOCKET


def test_align_agents_is_a_no_op(fake, mux):
    assert mux.align_agents("agents", ["%3", "%4"]) == []
    assert fake.calls == []


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
        LAYOUT_QUERY,
        ["split-window", "-h", "-d", "-t", "=agents:", "-P", "-F", "#{pane_id}",
         "--", "python", "-m", "x"],
        ["set-option", "-p", "-t", "%5", tmux_mux.NAME_OPTION, "alpha"],
        ["select-layout", "-t", "%5", "-E"],
    ]


def test_spawn_with_focus_does_not_pass_detached(fake, mux):
    fake.answer("split-window", "%5\n")
    mux.spawn("agents", ["python", "-m", "x"], focus=True)
    split = fake.tails()[2]
    assert split[0] == "split-window" and "-d" not in split


def test_spawn_opens_a_new_window_when_the_split_fails(fake, mux):
    fake.answer("split-window", "", 1)
    fake.answer("new-window", "%6\n")
    assert mux.spawn("agents", ["python", "-m", "x"]).pane_id == "%6"
    assert [tail[0] for tail in fake.tails()] == ["has-session", "show-options",
                                                   "split-window", "new-window"]
    assert fake.tails()[3][:4] == ["new-window", "-d", "-t", "=agents:"]


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
    assert fake.tails()[2][-4:] == ["--", "env", "--", "claude"]


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
        LAYOUT_QUERY,
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


# One agent at a time (#124): a single session parks agents and swaps them in.

def _slots(*rows):
    return "".join("\t".join(row) + "\n" for row in rows)


SIDEBAR_ROW = ("%0", "@0", tmux_mux.SIDEBAR_PANE_NAME, "0", "160")


def test_detach_and_kill_session_target_the_exact_session(fake, mux):
    mux.detach("agents")
    mux.kill_session("agents")
    assert fake.tails() == [
        ["detach-client", "-s", "=agents"],
        ["kill-session", "-t", "=agents"],
    ]


def test_set_layout_records_the_session_option(fake, mux):
    mux.set_layout("agents", "single")
    assert fake.tails() == [
        ["set-option", "-t", "=agents:", tmux_mux.LAYOUT_OPTION, "single"],
    ]
    with pytest.raises(MultiplexerError, match="layout"):
        mux.set_layout("agents", "tabs")


@pytest.mark.parametrize("stdout, expected", [
    ("single\n", "single"), ("split\n", "split"), ("", None), ("tabs\n", None),
])
def test_layout_reads_the_session_option(fake, mux, stdout, expected):
    fake.answer("show-options", stdout)
    assert mux.layout("agents") == expected


def test_spawn_in_a_single_session_parks_the_pane_in_a_detached_window(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("new-window", "%7\n")
    pane = mux.spawn("agents", ["python", "-m", "x"], name="alpha", cwd="/")
    assert pane.pane_id == "%7"
    assert fake.tails() == [
        ["has-session", "-t", "=agents"],
        LAYOUT_QUERY,
        ["new-window", "-d", "-t", "=agents:", "-n", "alpha", "-P", "-F", "#{pane_id}",
         "-c", "/", "--", "python", "-m", "x"],
        ["set-option", "-p", "-t", "%7", tmux_mux.NAME_OPTION, "alpha"],
    ]


def test_a_failed_park_is_an_error_not_a_split(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("new-window", "", 1)
    with pytest.raises(MultiplexerError, match="could not park"):
        mux.spawn("agents", ["python", "-m", "x"], name="alpha")
    assert "split-window" not in [tail[0] for tail in fake.tails()]


def test_the_sidebar_is_never_parked(fake, mux):
    fake.answer("split-window", "%2\n")
    mux.spawn("agents", ["python", "-m", "x"], name=tmux_mux.SIDEBAR_PANE_NAME)
    assert [tail[0] for tail in fake.tails()] == [
        "has-session", "split-window", "set-option", "select-layout"]


def test_show_swaps_a_parked_pane_into_the_main_slot(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "beta\n")
    fake.answer("display-message", "@0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%5", "@0", "beta", "1", "160"),
                                     ("%3", "@4", "alpha", "1", "80")))
    result = mux.show(ref())
    assert result.selection.succeeded and result.selection.detail == "swapped into the main slot"
    assert not result.activation.attempted
    assert fake.tails()[3:] == [
        ["list-panes", "-s", "-t", "=agents", "-F", tmux_mux.SLOT_FORMAT],
        ["swap-pane", "-d", "-s", "%3", "-t", "%5"],
        ["display-message", "-p", "-t", "%5", "#{" + tmux_mux.NAME_OPTION + "}"],
        ["rename-window", "-t", "@4", "beta"],
        ["select-window", "-t", "=agents:@0"],
        ["select-pane", "-t", "%3"],
        ["display-message", "-p", "-t", "%3", "#{window_id}"],
        ["display-message", "-p", "-t", "=agents:", "#{pane_id}"],
    ]


def test_show_joins_beside_the_sidebar_when_the_main_slot_is_empty(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "@0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%3", "@4", "alpha", "1", "80")))
    result = mux.show(ref())
    assert result.selection.detail == "joined into the empty main slot"
    assert ["join-pane", "-h", "-d", "-l", "119", "-s", "%3", "-t", "%0"] in fake.tails()
    assert "swap-pane" not in [tail[0] for tail in fake.tails()]


def test_show_of_the_pane_in_the_main_slot_only_selects_it(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%3", "@0", "alpha", "1", "160")))
    result = mux.show(ref())
    assert result.selection.detail == tmux_mux.ALREADY_SELECTED
    assert not {"swap-pane", "join-pane"} & {tail[0] for tail in fake.tails()}


def test_show_fails_when_the_pane_did_not_move(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "@4\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%3", "@4", "alpha", "1", "80")))
    result = mux.show(ref())
    assert result.state == "failed"
    assert "did not move pane %3 into the main slot" in result.selection.detail


def test_show_in_a_split_session_leaves_the_active_pane_alone(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@2\n")
    result = mux.show(ref())
    assert result.selection.succeeded
    assert [tail[0] for tail in fake.tails()] == [
        "display-message", "display-message", "show-options"]


def test_focus_of_a_parked_pane_in_a_single_session_swaps_it_in(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "\n")
    fake.answer("display-message", "@0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%5", "@0", "", "1", "160"),
                                     ("%3", "@4", "alpha", "1", "80")))
    result = mux.focus(ref())
    assert result.selection.detail == "swapped into the main slot"
    assert "rename-window" not in [tail[0] for tail in fake.tails()]
    assert ["select-window", "-t", "=agents:@4"] not in fake.tails()


def test_a_single_session_without_a_sidebar_selects_as_before(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "%0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(("%3", "@4", "alpha", "1", "80")))
    result = mux.focus(ref())
    assert result.selection.detail == "window and pane selection"
    assert ["select-window", "-t", "=agents:@4"] in fake.tails()


def test_an_unparsed_slot_listing_fails_the_show(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", "%0\t@0\tsidebar\n")
    result = mux.show(ref())
    assert result.state == "failed" and "unrecognized" in result.selection.detail


@pytest.mark.parametrize("width, columns", [(160, 119), (81, 40), (80, 40), (30, 15), (1, 1)])
def test_main_columns_keep_the_sidebar_about_forty_wide(width, columns):
    assert tmux_mux._main_columns(width) == columns


def test_session_of_names_the_session_holding_a_pane(fake, mux):
    fake.answer("display-message", "agents\n")
    assert mux.session_of("%3") == "agents"
    assert fake.tails() == [["display-message", "-p", "-t", "%3", "#{session_name}"]]
    with pytest.raises(MultiplexerError, match="pane id"):
        mux.session_of("3")


def test_a_focused_spawn_in_a_single_session_shows_the_parked_pane(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("new-window", "%7\n")
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "@0\n")
    fake.answer("display-message", "%7\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%7", "@4", "chat", "1", "80")))
    mux.spawn("agents", ["python", "-m", "x"], name="chat", focus=True)
    assert ["join-pane", "-h", "-d", "-l", "119", "-s", "%7", "-t", "%0"] in fake.tails()


def test_rename_pane_sets_the_name_option_and_a_literal_title(fake, mux):
    mux.rename_pane(ref(), "review #{pane_id} #(id)")
    assert fake.tails() == [
        ["set-option", "-p", "-t", "%3", tmux_mux.NAME_OPTION, "review #{pane_id} #(id)"],
        ["select-pane", "-t", "%3", "-T", "review ##{pane_id} ##(id)"],
        LAYOUT_QUERY,
    ]


def test_rename_pane_renames_the_window_a_parked_pane_sits_in_alone(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("display-message", "@4\t1\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%5", "@0", "beta", "1", "160"),
                                     ("%3", "@4", "alpha", "1", "80")))
    mux.rename_pane(ref(), "fix #130")
    assert fake.tails()[-1] == ["rename-window", "-t", "@4", "fix ##130"]


@pytest.mark.parametrize("located", ["@0\t2\n", "@4\t2\n", "garbage\n"])
def test_rename_pane_leaves_shared_and_sidebar_windows_alone(fake, mux, located):
    fake.answer("show-options", "single\n")
    fake.answer("display-message", located)
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%3", "@0", "alpha", "1", "160")))
    mux.rename_pane(ref(), "alpha2")
    assert "rename-window" not in [tail[0] for tail in fake.tails()]


def test_rename_pane_in_the_main_slot_leaves_the_sidebar_window_alone(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("display-message", "@0\t1\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW))
    mux.rename_pane(ref(), "alpha2")
    assert "rename-window" not in [tail[0] for tail in fake.tails()]


@pytest.mark.parametrize("name", ["", "-x", "a\nb", "x" * 300, None])
def test_rename_pane_refuses_bad_names(fake, mux, name):
    with pytest.raises(MultiplexerError, match="pane name"):
        mux.rename_pane(ref(), name)
    assert fake.calls == []


def test_rename_pane_fails_when_the_pane_is_gone(fake, mux):
    fake.answer("set-option", "", 1)
    with pytest.raises(MultiplexerError, match="set-option"):
        mux.rename_pane(ref(), "alpha2")


def test_a_swap_names_the_parked_window_literally(fake, mux):
    fake.answer("display-message", "$1\n")
    fake.answer("display-message", "$1\t@4\n")
    fake.answer("display-message", "fix #130\n")
    fake.answer("display-message", "@0\n")
    fake.answer("display-message", "%3\n")
    fake.answer("show-options", "single\n")
    fake.answer("list-panes", _slots(SIDEBAR_ROW, ("%5", "@0", "beta", "1", "160"),
                                     ("%3", "@4", "alpha", "1", "80")))
    mux.show(ref())
    assert ["rename-window", "-t", "@4", "fix ##130"] in fake.tails()


def test_a_parked_window_named_at_spawn_is_named_literally(fake, mux):
    fake.answer("show-options", "single\n")
    fake.answer("new-window", "%7\n")
    mux.spawn("agents", ["python", "-m", "x"], name="fix #130")
    assert fake.tails()[2][:6] == ["new-window", "-d", "-t", "=agents:", "-n", "fix ##130"]
    assert fake.tails()[3] == ["set-option", "-p", "-t", "%7", tmux_mux.NAME_OPTION,
                               "fix #130"]
