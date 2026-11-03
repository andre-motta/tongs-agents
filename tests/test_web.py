"""``tongs-agents web``: token handling, redaction, state and the web build switch.

No Zellij runs here; ``zellij web`` calls are replaced by fakes. The live
behaviour is checked by validation/zellij_web.py.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tongs_agents import cli, paths, zellij_download
from tongs_agents.commands import web, workspace
from tongs_agents.multiplexers import zellij_binary

TOKEN = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
OTHER = "11111111-2222-3333-4444-555555555555"
MINTED = f"Created token successfully\n\ntoken_1: {TOKEN} (read-only)\n"
LISTED = ("token_1: created at 2026-11-03 15:31:08 [READ-ONLY]\n"
          "token_2: created at 2026-11-03 15:31:09 [READ-ONLY]\n")


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(zellij_download, "_system", lambda: ("linux", "x86_64"))


@pytest.fixture
def web_build(monkeypatch):
    """A managed web build link that resolves to an executable file."""
    directory = paths.state_dir(create=True) / "bin" / "zellij-web-0.45.1"
    directory.mkdir(parents=True, mode=0o700)
    binary = directory / "zellij"
    binary.write_text("#!/bin/sh\necho 'zellij 0.45.1'\n")
    binary.chmod(0o700)
    os.symlink(os.path.join("zellij-web-0.45.1", "zellij"), zellij_binary.managed_web_link())
    monkeypatch.setattr(web, "_web_build_problem", lambda binary: None)
    return str(zellij_binary.managed_web_link())


class FakeZellij:
    """Records ``zellij web`` calls and answers like Zellij 0.45.1 did live."""

    def __init__(self, monkeypatch, mint=(0, MINTED), running=False):
        self.calls = []
        self.mint = mint
        self.running = running
        monkeypatch.setattr(web, "_call", self)
        monkeypatch.setattr(web, "owned_server_pids", lambda: [4242] if self.running else [])
        monkeypatch.setattr(web, "_wait_gone", lambda timeout: not self.running)

    def __call__(self, binary, *arguments, daemon=False):
        self.calls.append(arguments)
        if arguments[0] == "--start":
            self.running = True
            return 0, "Web Server started on 127.0.0.1 port 8083\n"
        if arguments[0] == "--stop":
            self.running = False
            return 0, "Stopped web server.\n"
        if arguments[0] == "--create-read-only-token":
            token_db = web.tokens_db()
            token_db.parent.mkdir(parents=True, exist_ok=True)
            token_db.write_bytes(b"hashes only")
            token_db.chmod(0o644)
            return self.mint
        if arguments[0] == "--list-tokens":
            return 0, LISTED
        if arguments[0] in ("--revoke-token", "--revoke-all-tokens"):
            return 0, "Successfully revoked token.\n"
        raise AssertionError(arguments)

    def verbs(self):
        return [arguments[0] for arguments in self.calls]


def _cli(monkeypatch, capsys, *argv):
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", ["tongs-agents", *argv])
    try:
        cli.main()
        code = 0
    except SystemExit as raised:
        code = raised.code
    out = capsys.readouterr()
    return code, out.out, out.err


def _all_state_bytes():
    root = paths.state_dir()
    return b"".join(path.read_bytes() for path in root.rglob("*")
                    if path.is_file() and not path.is_symlink())


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# Redaction


@pytest.mark.parametrize("text", [
    f"token_1: {TOKEN} (read-only)",
    f"error near {TOKEN.upper()}",
    "token_12:\tsecret-value-without-uuid-shape",
])
def test_redact_removes_token_shapes(text):
    redacted = web.redact(text)

    assert TOKEN not in redacted and TOKEN.upper() not in redacted
    assert "secret-value" not in redacted
    assert web.REDACTED in redacted


def test_redact_keeps_token_listings_readable():
    assert web.redact(LISTED) == LISTED
    assert web.redact("") == ""


def test_mint_parses_the_name_and_token(web_build, monkeypatch):
    FakeZellij(monkeypatch)

    assert web.mint_read_only_token(web_build) == ("token_1", TOKEN)


@pytest.mark.parametrize("answer", [
    (1, f"token_1: {TOKEN} (read-only)\nbut something failed\n"),
    (0, f"unexpected shape {TOKEN}\n"),
])
def test_mint_failure_never_carries_the_token(web_build, monkeypatch, answer):
    FakeZellij(monkeypatch, mint=answer)

    with pytest.raises(web.WebError) as raised:
        web.mint_read_only_token(web_build)

    assert TOKEN not in str(raised.value)
    assert web.REDACTED in str(raised.value)


def test_mint_name_clash_explains_the_zellij_quirk(web_build, monkeypatch):
    FakeZellij(monkeypatch, mint=(1, "Error: Token name 'token_2' already exists\n"))

    with pytest.raises(web.WebError, match="revoke --all"):
        web.mint_read_only_token(web_build)


# enable


def test_enable_prints_the_token_once_and_never_stores_it(
        linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)

    code, out, err = _cli(monkeypatch, capsys, "web", "enable", "--port", "8099")

    assert code == 0, err
    assert out.count(TOKEN) == 1
    assert TOKEN not in err
    assert "http://127.0.0.1:8099/agents" in out
    assert "ssh -L 8099:127.0.0.1:8099" in out
    assert fake.verbs() == ["--start", "--create-read-only-token"]
    assert TOKEN.encode() not in _all_state_bytes()
    state = json.loads(zellij_binary.web_state_path().read_text())
    assert state == {"schema": web.STATE_SCHEMA, "enabled": True, "bind": "127.0.0.1",
                     "port": 8099}
    assert web.configured_bind(web.config_path().read_text()) == "127.0.0.1"
    assert "web_server_port 8099" in web.config_path().read_text()
    for path in (zellij_binary.web_state_path(), web.config_path(), web.tokens_db()):
        assert _mode(path) == 0o600, path
    for path in (web.web_dir(), web.data_dir()):
        assert _mode(path) == 0o700, path


def test_enable_uses_the_session_from_the_config(linux, web_build, monkeypatch, capsys):
    FakeZellij(monkeypatch)
    directory = paths.config_dir(create=True)
    (directory / "config.toml").write_text('[workspace]\nsession = "team.1"\n')
    (directory / "config.toml").chmod(0o600)

    code, out, _ = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 0
    assert f"http://127.0.0.1:{web.DEFAULT_PORT}/team.1" in out


def test_enable_again_mints_without_restarting(linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable")[0] == 0
    fake.calls.clear()

    code, _, _ = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 0
    assert fake.verbs() == ["--create-read-only-token"]


def test_enable_on_a_new_port_restarts_the_server(linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable", "--port", "8100")[0] == 0
    fake.calls.clear()

    code, _, _ = _cli(monkeypatch, capsys, "web", "enable", "--port", "8101")

    assert code == 0
    assert fake.verbs() == ["--stop", "--start", "--create-read-only-token"]
    assert "web_server_port 8101" in web.config_path().read_text()


def test_enable_failure_stops_the_new_server_and_stays_off(
        linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch, mint=(1, f"token_1: {TOKEN} broken\n"))

    code, out, err = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 1
    assert TOKEN not in out + err
    assert fake.verbs() == ["--start", "--create-read-only-token", "--stop"]
    assert not zellij_binary.web_enabled()


def test_enable_without_the_web_build_points_at_the_download(linux, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)

    code, _, err = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 1
    assert "tongs-agents zellij install --web" in err
    assert fake.calls == []
    assert not zellij_binary.web_state_path().exists()


def test_enable_refuses_unsupported_platforms(web_build, monkeypatch, capsys):
    monkeypatch.setattr(zellij_download, "_system", lambda: ("darwin", "arm64"))
    fake = FakeZellij(monkeypatch)

    code, _, err = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 2
    assert "unsupported" in err
    assert fake.calls == []


def test_enable_refuses_a_web_build_that_is_not_the_pinned_one(linux, monkeypatch, capsys):
    directory = paths.state_dir(create=True) / "bin" / "zellij-web-0.45.1"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "zellij").write_text("#!/bin/sh\n")
    (directory / "zellij").chmod(0o700)
    os.symlink(os.path.join("zellij-web-0.45.1", "zellij"), zellij_binary.managed_web_link())
    fake = FakeZellij(monkeypatch)

    code, _, err = _cli(monkeypatch, capsys, "web", "enable")

    assert code == 1
    assert "does not match the pinned web build" in err
    assert fake.calls == []


@pytest.mark.parametrize("port", ["80", "70000", "web"])
def test_enable_refuses_bad_ports(linux, web_build, monkeypatch, capsys, port):
    FakeZellij(monkeypatch)

    code, _, _ = _cli(monkeypatch, capsys, "web", "enable", "--port", port)

    assert code == 1


# tokens, disable and status


def test_tokens_list_shows_names_only(linux, web_build, monkeypatch, capsys):
    FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable")[0] == 0

    code, out, _ = _cli(monkeypatch, capsys, "web", "tokens", "list")

    assert code == 0
    assert out == LISTED


def test_revoke_restarts_a_running_server_to_close_connections(
        linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable")[0] == 0
    fake.calls.clear()

    code, out, _ = _cli(monkeypatch, capsys, "web", "tokens", "revoke", "token_1")

    assert code == 0
    assert fake.calls == [("--revoke-token", "token_1"), ("--stop",),
                          ("--start", "--daemonize")]
    assert "close every open connection" in out


def test_revoke_all_without_a_running_server(linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)

    code, _, _ = _cli(monkeypatch, capsys, "web", "tokens", "revoke", "--all")

    assert code == 0
    assert fake.calls == [("--revoke-all-tokens",)]


@pytest.mark.parametrize("argv", [
    ("web", "tokens", "revoke"),
    ("web", "tokens", "revoke", "token_1", "--all"),
    ("web", "tokens", "revoke", "--bad"),
    ("web", "tokens", "revoke", "../x"),
])
def test_revoke_needs_exactly_one_safe_target(linux, web_build, monkeypatch, capsys, argv):
    fake = FakeZellij(monkeypatch)

    code, _, _ = _cli(monkeypatch, capsys, *argv)

    assert code == 1
    assert fake.calls == []


def test_disable_stops_revokes_and_switches_back(linux, web_build, monkeypatch, capsys):
    fake = FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable")[0] == 0
    assert zellij_binary.zellij_binary() == web_build
    fake.calls.clear()

    code, out, _ = _cli(monkeypatch, capsys, "web", "disable")

    assert code == 0
    assert fake.verbs() == ["--stop", "--revoke-all-tokens"]
    assert not fake.running
    assert not zellij_binary.web_enabled()
    assert zellij_binary.zellij_binary() != web_build
    assert "Web watching is off" in out


def test_status_never_prints_a_token(linux, web_build, monkeypatch, capsys):
    FakeZellij(monkeypatch)
    _, enabled_out, _ = _cli(monkeypatch, capsys, "web", "enable")

    code, out, _ = _cli(monkeypatch, capsys, "web")

    assert code == 0
    assert TOKEN in enabled_out and TOKEN not in out
    assert "Web watching: on" in out
    assert "Server: running on 127.0.0.1:8083" in out


# the web build switch


def test_web_build_is_used_only_while_enabled(web_build, monkeypatch):
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: "/usr/bin/zellij")
    monkeypatch.setattr(zellij_binary, "_cached_version", lambda binary: (0, 45, 1))

    assert zellij_binary.zellij_binary() == "/usr/bin/zellij"
    assert zellij_binary.new_session_options("/usr/bin/zellij") == []

    web.write_state(True, 8083)

    assert zellij_binary.zellij_binary() == web_build
    assert zellij_binary.new_session_options(web_build) == ["options", "--web-sharing", "on"]
    assert zellij_binary.new_session_options("/usr/bin/zellij") == []

    web.write_state(False, 8083)

    assert zellij_binary.zellij_binary() == "/usr/bin/zellij"
    assert zellij_binary.new_session_options(web_build) == []


def test_enabled_without_the_web_build_falls_back(monkeypatch):
    monkeypatch.setattr(zellij_binary.shutil, "which", lambda name: "/usr/bin/zellij")
    web.write_state(True, 8083)

    assert zellij_binary.zellij_binary() == "/usr/bin/zellij"
    assert zellij_binary.new_session_options("/usr/bin/zellij") == []


@pytest.mark.parametrize("content", [b"not json", b"[true]", b'{"enabled": "yes"}',
                                     b'{"enabled": true, "pad": "' + b"x" * 5000 + b'"}'])
def test_unreadable_state_counts_as_off(web_build, content):
    path = zellij_binary.web_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    assert not zellij_binary.web_enabled()
    assert zellij_binary.web_binary() is None


def test_new_workspace_session_gets_web_sharing_only_when_enabled(web_build, monkeypatch):
    monkeypatch.setattr(workspace.workspace_spawn, "create_launches", lambda *args: [])
    monkeypatch.setattr(workspace.workspace_spawn, "write_spawn_request",
                        lambda *args: "a" * 32)
    monkeypatch.setattr(workspace.workspace_spawn, "write_layout",
                        lambda token, layout: Path("/run/layout.kdl"))

    class Mux:
        name = "zellij"

    def argv(binary):
        return workspace._prepare_new_session(Mux(), binary, "agents", [], False)[0]

    assert argv(web_build)[-1] == "/run/layout.kdl"
    web.write_state(True, 8083)
    assert argv(web_build)[-4:] == ["/run/layout.kdl", "options", "--web-sharing", "on"]
    assert argv("/usr/bin/zellij")[-1] == "/run/layout.kdl"


# owned server lookup


def test_owned_server_pids_match_only_the_owned_config(monkeypatch):
    config = str(web.config_path())
    commands = {
        "10": ["zellij", "--config", config, "web", "--start", "--daemonize"],
        "11": ["zellij", "web", "--start", "--daemonize"],
        "12": ["zellij", "--config", "/elsewhere/config.kdl", "web", "--start"],
        "13": ["zellij", "--config", config, "attach", "agents"],
    }
    monkeypatch.setattr(web.os, "listdir", lambda path: ["self", *commands])
    monkeypatch.setattr(web.os, "stat", lambda path: os.stat_result((0,) * 4 + (os.getuid(),)
                                                                    + (0,) * 5))
    monkeypatch.setattr(web, "_read_cmdline", lambda pid: commands.get(pid, []))

    assert web.owned_server_pids() == [10]


def test_stop_falls_back_to_sigterm_of_the_owned_server(monkeypatch):
    alive = {77}
    monkeypatch.setattr(web, "owned_server_pids", lambda: sorted(alive))
    monkeypatch.setattr(web, "_wait_gone", lambda timeout: not alive)
    signals = []
    monkeypatch.setattr(web.os, "kill", lambda pid, sig: (signals.append((pid, sig)),
                                                          alive.discard(pid)))

    assert web.stop_server(None)
    assert signals == [(77, web.signal.SIGTERM)]


def test_stop_does_nothing_without_an_owned_server(monkeypatch):
    monkeypatch.setattr(web, "owned_server_pids", lambda: [])
    monkeypatch.setattr(web, "_call", lambda *args, **kwargs: pytest.fail("called zellij"))

    assert web.stop_server("/bin/zellij-web")


def test_zellij_env_points_the_token_store_into_state(monkeypatch):
    env = web.zellij_env({"ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents",
                          "ZELLIJ_PANE_ID": "3", "ZELLIJ_SOCKET_DIR": "/run/z",
                          "XDG_DATA_HOME": "/srv/share"})

    assert env == {"ZELLIJ_SOCKET_DIR": "/run/z", "XDG_DATA_HOME": str(web.data_dir())}


# doctor


def _checks():
    return [(check.name, check.level) for check in web.doctor_checks()]


def test_doctor_is_quiet_when_off():
    assert _checks() == [("web", "ok")]


def test_doctor_warns_on_a_non_localhost_bind(linux, web_build, monkeypatch, capsys):
    FakeZellij(monkeypatch)
    assert _cli(monkeypatch, capsys, "web", "enable")[0] == 0
    assert _checks() == [("web", "ok")]
    text = web.config_path().read_text().replace('"127.0.0.1"', '"0.0.0.0"')
    web.config_path().write_text(text)

    checks = web.doctor_checks()

    assert [(check.name, check.level) for check in checks] == [("web-bind", "warn")]
    assert "0.0.0.0" in checks[0].message


def test_doctor_warns_on_a_recorded_non_localhost_bind():
    path = zellij_binary.web_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"enabled": False, "bind": "0.0.0.0"}))

    assert _checks() == [("web-bind", "warn")]


def test_doctor_warns_when_enabled_without_the_build_or_with_open_tokens():
    web.write_state(True, 8083)
    web.tokens_db().parent.mkdir(parents=True)
    web.tokens_db().write_bytes(b"")
    web.tokens_db().chmod(0o644)

    assert _checks() == [("web", "warn"), ("web-tokens", "warn")]


def test_doctor_warns_about_a_leftover_server(monkeypatch):
    monkeypatch.setattr(web, "owned_server_pids", lambda: [5])

    assert _checks() == [("web", "warn")]


def test_doctor_checks_only_read(monkeypatch):
    monkeypatch.setattr(web, "_call", lambda *args, **kwargs: pytest.fail("ran zellij"))
    before = sorted(paths.state_dir().rglob("*")) if paths.state_dir().exists() else []

    web.doctor_checks()

    after = sorted(paths.state_dir().rglob("*")) if paths.state_dir().exists() else []
    assert before == after


def test_disable_without_the_web_build_drops_the_token_store(linux, monkeypatch, capsys):
    store = web.tokens_db()
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_bytes(b"token hashes")
    monkeypatch.setattr(web, "managed_web_binary", lambda: None)
    monkeypatch.setattr(web, "stop_server", lambda binary: True)

    code, out, _ = _cli(monkeypatch, capsys, "web", "disable")

    assert code == 0
    assert not store.exists()
    assert "Could not revoke" not in out
