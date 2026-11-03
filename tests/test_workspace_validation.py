"""Safety controls and committed evidence for the isolated workspace validator."""

import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tongs_agents import cli, hook
from tongs_agents.discovery import read_stored_sessions
from tongs_agents.profiles import load_profiles
from tests.launch_helpers import write_claim

ROOT = Path(__file__).parents[1]
VALIDATION = ROOT / "validation"
EVIDENCE = VALIDATION / "evidence" / "workspace-linux-x86_64.json"
PINS = ("0.44.2", "0.45.1")

linux_only = pytest.mark.skipif(sys.platform != "linux", reason="Linux-only validator")
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX argv quoting")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake_agent = _load("workspace_fake_agent", VALIDATION / "fixtures" / "fake_agent.py")
fake_cli = _load("workspace_fake_cli", VALIDATION / "fixtures" / "fake_cli.py")

if sys.platform == "linux":
    sys.modules.setdefault("probe_terminals", _load("probe_terminals",
                                                    VALIDATION / "probe_terminals.py"))
    validator = _load("workspace_validation", VALIDATION / "workspace.py")


@pytest.fixture
def user_home(tmp_path):
    home = tmp_path / "user"
    home.mkdir()
    return home


@pytest.fixture
def private(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


def _isolated(private):
    return validator.isolated_env(private, real_env={"LANG": "C.UTF-8"})


@linux_only
def test_validator_requires_zellij(monkeypatch):
    monkeypatch.setattr(validator.shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="requires Linux and zellij"):
        validator.validate(write=False)


@linux_only
def test_isolated_env_drops_inherited_sessions_config_and_secrets(private, user_home):
    inherited = {
        "ZELLIJ": "0", "ZELLIJ_SESSION_NAME": "agents", "ZELLIJ_SOCKET_DIR": "/run/user/1/zellij",
        "TMUX": "/tmp/tmux-1/default,1,0", "CLAUDE_CONFIG_DIR": str(user_home / ".claude"),
        "CODEX_HOME": str(user_home / ".codex"), "TONGS_AGENTS_STATE_DIR": str(user_home),
        "FLEET_DIR": str(user_home), "ANTHROPIC_API_KEY": "secret", "LANG": "C.UTF-8",
        "HOME": str(user_home), "PATH": f"{user_home}/.local/bin:/usr/bin",
    }

    env = validator.isolated_env(private, real_env=inherited)

    assert env["LANG"] == "C.UTF-8"
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "TMUX", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
                "TONGS_AGENTS_STATE_DIR", "FLEET_DIR", "ANTHROPIC_API_KEY"):
        assert key not in env
    for key in validator.ISOLATION_KEYS:
        assert env[key].startswith(str(private))
    assert env["PATH"].split(os.pathsep) == [str(private / "bin"), "/usr/bin", "/bin"]
    assert validator.check_isolation(env, private, home=user_home, real_env=inherited)


@linux_only
@pytest.mark.parametrize("key", ["ZELLIJ_SOCKET_DIR", "XDG_RUNTIME_DIR", "TMPDIR"])
def test_default_socket_dirs_are_refused(private, user_home, key):
    env = _isolated(private)
    env[key] = f"/run/user/{os.getuid()}/zellij"

    with pytest.raises(validator.Check):
        validator.check_isolation(env, private, home=user_home, real_env={})


@linux_only
def test_root_inside_the_default_socket_dir_is_refused(tmp_path, user_home):
    runtime = tmp_path / "run"
    root = runtime / "zellij" / "root"
    root.mkdir(parents=True)

    with pytest.raises(validator.Check, match="private root"):
        validator.check_isolation(validator.isolated_env(root, real_env={}), root,
                                  home=user_home, real_env={"XDG_RUNTIME_DIR": str(runtime)})


@linux_only
@pytest.mark.parametrize("key, relative", [
    ("HOME", ""), ("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"),
    ("XDG_CACHE_HOME", ".cache"), ("ZELLIJ_CONFIG_DIR", ".config/zellij"),
])
def test_user_config_dirs_are_refused(private, user_home, key, relative):
    env = _isolated(private)
    env[key] = str(user_home / relative) if relative else str(user_home)

    with pytest.raises(validator.Check):
        validator.check_isolation(env, private, home=user_home, real_env={})


@linux_only
def test_root_inside_user_config_is_refused(user_home):
    root = user_home / ".config" / "root"
    root.mkdir(parents=True)

    with pytest.raises(validator.Check, match="private root"):
        validator.check_isolation(validator.isolated_env(root, real_env={}), root,
                                  home=user_home, real_env={})


@linux_only
@pytest.mark.parametrize("key", ["CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR",
                                 "TONGS_AGENTS_CONFIG_DIR", "FLEET_DIR",
                                 "ZELLIJ_SESSION_NAME", "TMUX"])
def test_inherited_agent_and_session_variables_are_refused(private, user_home, key):
    env = _isolated(private)
    env[key] = str(user_home)

    with pytest.raises(validator.Check, match="inherited"):
        validator.check_isolation(env, private, home=user_home, real_env={})


@linux_only
@pytest.mark.parametrize("path", ["/usr/bin:/bin", "{bin}:{home}/.local/bin"])
def test_path_must_start_with_the_private_bin_and_avoid_home(private, user_home, path):
    env = _isolated(private)
    env["PATH"] = path.format(bin=private / "bin", home=user_home)

    with pytest.raises(validator.Check, match="PATH"):
        validator.check_isolation(env, private, home=user_home, real_env={})


@linux_only
def test_private_bin_shadows_every_agent_cli(private, monkeypatch, tmp_path):
    (private / "bin").mkdir()
    zellij = tmp_path / "zellij"
    zellij.write_text("")
    monkeypatch.setattr(validator, "entry_point", lambda name: f"/opt/venv/bin/{name}")

    directory = validator.write_bin(private, str(zellij))

    for name in validator.FAKE_CLIS:
        assert os.readlink(directory / name) == str(validator.FAKE_CLI)
    assert os.readlink(directory / "zellij") == str(zellij.resolve())
    assert os.readlink(directory / "tongs-agents-hook") == "/opt/venv/bin/tongs-agents-hook"
    assert os.access(validator.FAKE_CLI, os.X_OK)


@linux_only
def test_fixture_profiles_parse_with_private_config_dirs(private):
    path = validator.write_profiles(private, python="/opt/venv/bin/python")

    profiles = load_profiles(path)

    assert sorted(profiles) == ["f1", "f2", "f3", "f4"]
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert profiles["f1"].harness == "pi" and profiles["f1"].config_dir is None
    assert profiles["f3"].harness == "claude"
    assert profiles["f3"].config_dir == private / "claude-hooked"
    assert profiles["f4"].config_dir == private / "claude-bare"
    for profile in profiles.values():
        assert profile.argv[:2] == ("/opt/venv/bin/python", str(validator.FAKE_AGENT))
        assert profile.argv[3] == str(private / "io")
        assert profile.cwd == private / "work" / profile.name


@linux_only
def test_cleanup_signals_only_started_processes(monkeypatch, private):
    calls = []
    monkeypatch.setattr(validator, "run", lambda argv, env=None: calls.append((argv, env)))
    (private / "bin").mkdir()
    os.symlink("/bin/true", private / "bin" / "zellij")
    owned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    unowned = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        tracker = validator.Tracker([str(private)])
        tracker.add(owned.pid, "fixture")

        result = validator.cleanup(private, ("tongs-ws-x",), tracker)

        assert owned.wait(timeout=3) != 0
        assert unowned.poll() is None
        assert result["survivors"] == 0 and result["temp_dir_removed"]
        assert [argv[1] for argv, _ in calls] == ["kill-session", "delete-session"]
        assert all(env["ZELLIJ_SOCKET_DIR"] == str(private / "sock") for _, env in calls)
    finally:
        for process in (owned, unowned):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


@linux_only
@pytest.mark.parametrize("value", [str(Path.home()), "/tmp/tongs-ws-x", "/run/user/1/zellij"])
def test_public_text_flags_private_paths(value):
    _, leaks = validator.public_text({"path": value}, lambda text: text)

    assert leaks


@linux_only
def test_diagnostic_lines_drop_private_paths_and_blank_lines():
    root = "/tmp/tongs-ws-abcd-xyz"
    screen = (f"error: {root}/xdg-config/config.toml\n\n   \n"
              "wrapped tail /tmp/tongs-ws-ab\ncd-xyz/sock /home/someone/x /run/user/1/z\n")

    lines = validator.diagnostic_lines(screen, root)
    text, leaks = validator.public_text({"lines": lines}, lambda value: value)

    assert lines == ["error: <tmp>/xdg-config/config.toml",
                     "wrapped tail <path>", "cd-xyz/sock <path> <path>"]
    assert not leaks
    assert validator.diagnostic_lines("\n".join(map(str, range(50))), root, limit=3) == [
        "47", "48", "49"]


@linux_only
def test_rerun_stderr_wrapper_execs_the_command(tmp_path):
    stderr = tmp_path / "stderr"
    result = subprocess.run(
        ["/bin/sh", "-c", validator.STDERR_TO_FILE, str(stderr), sys.executable, "-c",
         "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"],
        capture_output=True, text=True, timeout=10)

    assert result.returncode == 3
    assert result.stdout == "out\n"
    assert stderr.read_text() == "err\n"


class _Clock:
    """A fake monotonic clock that each sleep, and each wait, moves forward."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class _Target:
    def __init__(self, sessions, panes):
        self._sessions = sessions
        self._panes = panes

    def sessions(self):
        return list(self._sessions)

    def panes(self):
        return list(self._panes)

    def action(self, *arguments):
        return 0, "sidebar screen line\n", ""


class _Client:
    def __init__(self, *arguments):
        pass

    def screen(self):
        return "zellij: \x07could not start\nlast line\n"


class _Tracker:
    def sweep(self):
        pass


TOKEN = "ab" * 16


def _workspace_panes():
    return [
        {"id": 0, "title": validator.workspace_spawn.SIDEBAR_PANE_NAME,
         "terminal_command": f"tongs-agents sidebar --spawn-request {TOKEN}"},
        {"id": 1, "title": validator.HOME_PANE_NAME},
        {"id": 2, "title": "f1"},
        {"id": 3, "title": "f2"},
    ]


def _create(monkeypatch, tmp_path, target, session_after, panes_after=1.0):
    """Run create_workspace with the session listed ``session_after`` s after start."""
    clock = _Clock()
    waits = []

    def wait_until(predicate, timeout, step=0.1):
        waits.append(timeout)
        clock.now += session_after if len(waits) == 1 else panes_after
        return predicate()

    monkeypatch.setattr(validator, "time", clock)
    monkeypatch.setattr(validator, "wait_until", wait_until)
    monkeypatch.setattr(validator, "CapturingClient", _Client)
    observations = {}
    try:
        token = validator.create_workspace(tmp_path, "s1", {}, "tongs-agents", target,
                                           _Tracker(), observations)
    except validator.Check as failure:
        return failure, observations, waits
    return token, observations, waits


@linux_only
def test_ceilings_are_generous_and_above_the_recorded_budgets():
    assert validator.SESSION_BUDGET == 5.0 and validator.STATUS_BUDGET == 4.0
    assert validator.SESSION_CEILING >= 4 * validator.SESSION_BUDGET
    assert validator.PANES_CEILING >= validator.SESSION_CEILING
    assert validator.STATUS_CEILING >= 3 * validator.STATUS_BUDGET


@linux_only
def test_slow_session_past_the_budget_is_recorded_not_failed(monkeypatch, tmp_path):
    target = _Target(["s1"], _workspace_panes())

    token, observations, waits = _create(monkeypatch, tmp_path, target, session_after=7.5)

    assert token == TOKEN
    assert waits[:2] == [validator.SESSION_CEILING, validator.PANES_CEILING]
    workspace = observations["workspace"]
    assert workspace["session_listed_seconds"] == 7.5
    assert workspace["session_budget_seconds"] == validator.SESSION_BUDGET
    assert workspace["session_within_budget"] is False
    assert workspace["session_ceiling_seconds"] == validator.SESSION_CEILING
    assert "client_screen" not in workspace


@linux_only
def test_session_within_the_budget_is_marked_within(monkeypatch, tmp_path):
    target = _Target(["s1"], _workspace_panes())

    _, observations, _ = _create(monkeypatch, tmp_path, target, session_after=1.2)

    assert observations["workspace"]["session_within_budget"] is True


@linux_only
def test_session_past_the_ceiling_fails(monkeypatch, tmp_path):
    target = _Target(["s1"], _workspace_panes())

    failure, _, _ = _create(monkeypatch, tmp_path, target,
                            session_after=validator.SESSION_CEILING + 1)

    assert isinstance(failure, validator.Check)
    assert "ceiling" in str(failure)


@linux_only
def test_missing_session_records_the_client_screen(monkeypatch, tmp_path):
    target = _Target([], [])

    failure, observations, _ = _create(monkeypatch, tmp_path, target,
                                       session_after=validator.SESSION_CEILING)

    assert isinstance(failure, validator.Check)
    assert "workspace.client_screen" in str(failure)
    workspace = observations["workspace"]
    assert workspace["session_listed"] is False
    assert workspace["session_within_budget"] is False
    assert workspace["client_screen"] == ["zellij: \x07could not start", "last line"]


@linux_only
def test_missing_panes_record_the_client_and_sidebar_screens(monkeypatch, tmp_path):
    target = _Target(["s1"], _workspace_panes()[:2])

    failure, observations, _ = _create(monkeypatch, tmp_path, target, session_after=1.0)

    assert isinstance(failure, validator.Check)
    assert "expected 4 panes, got 2" in str(failure)
    workspace = observations["workspace"]
    assert workspace["client_screen"] == ["zellij: \x07could not start", "last line"]
    assert workspace["sidebar_screen"] == ["sidebar screen line"]


def _hook_rows(monkeypatch, found_after):
    clock = _Clock()
    record = SimpleNamespace(launch_id="L1", profile="f1", pid=42, pane_id="1")
    row = SimpleNamespace(source="hook", status=SimpleNamespace(value="idle"), agent="pi",
                          launch_id="L1", profile_id="f1", pid="42",
                          terminal_env={"ZELLIJ_PANE_ID": "1", "ZELLIJ_SESSION_NAME": "s1"})
    monkeypatch.setattr(validator, "time", clock)
    monkeypatch.setattr(validator, "records_by_profile", lambda session: {"f1": record})
    monkeypatch.setattr(validator, "rows_by_launch",
                        lambda feed: {"L1": row} if clock.now >= found_after else {})
    observations = {}
    try:
        validator.hook_rows("s1", None, None, ("f1",), 0.0, observations, "first")
    except validator.Check as failure:
        return failure, observations
    return None, observations


@linux_only
def test_slow_hook_row_past_the_status_budget_is_recorded_not_failed(monkeypatch):
    failure, observations = _hook_rows(monkeypatch, found_after=6.0)

    assert failure is None
    facts = observations["first"]
    assert facts["status_budget_seconds"] == validator.STATUS_BUDGET
    assert facts["status_ceiling_seconds"] == validator.STATUS_CEILING
    assert facts["profiles"]["f1"]["seconds_after_panes_listed"] >= 6.0
    assert facts["profiles"]["f1"]["within_status_budget"] is False


@linux_only
def test_hook_row_missing_past_the_status_ceiling_fails(monkeypatch):
    failure, observations = _hook_rows(monkeypatch,
                                       found_after=validator.STATUS_CEILING + 1)

    assert isinstance(failure, validator.Check)
    assert observations["first"]["profiles"]["f1"] == {"hook_row": False}


@linux_only
def test_screen_text_strips_terminal_sequences():
    raw = b"\x1b[2J\x1b[1;1Hzellij \x1b[31merror\x1b[0m\r\n\x1b]0;title\x07next\rline"

    assert validator.screen_text(raw) == "\nzellij error\nnext\nline"
    st = b"\x1b]8;;https://x.invalid\x1b\\link\x1b]8;;\x1b\\ done"
    assert validator.screen_text(st) == "link done"
    rows = b"\x1b[1;1Hfirst row\x1b[2;1Hsecond row\x1b[3Hthird"
    assert validator.screen_text(rows).split("\n")[1:] == ["first row", "second row", "third"]


def test_diagnostic_lines_are_cut_to_a_readable_width():
    lines = validator.diagnostic_lines("x" * 5000 + "\nshort", "/tmp/root")
    assert lines == ["x" * 197 + "...", "short"]


@linux_only
def test_write_evidence_keeps_other_pins(tmp_path):
    path = tmp_path / "evidence.json"
    validator.write_evidence(path, {"zellij_version": "zellij 0.45.1", "result": "pass"})
    validator.write_evidence(path, {"zellij_version": "zellij 0.44.2", "result": "pass"})
    validator.write_evidence(path, {"zellij_version": "zellij 0.45.1", "result": "fail"})

    document = json.loads(path.read_text())

    assert document["schema"] == validator.SCHEMA
    assert list(document["runs"]) == ["0.44.2", "0.45.1"]
    assert document["runs"]["0.45.1"]["result"] == "fail"


@linux_only
def test_pi_fixture_payloads_are_accepted_by_the_hook(tmp_path, monkeypatch):
    monkeypatch.setenv("TONGS_AGENTS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TONGS_AGENTS_LAUNCH_ID", "a" * 32)
    monkeypatch.setenv("TONGS_AGENTS_PROFILE", "f1")
    write_claim("a" * 32, os.getpid(), None, harness="pi", profile="f1")

    for payload in fake_agent.pi_payloads(os.getpid(), "fixture-x", str(tmp_path)):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        assert hook.handle("fleet-event", "pi")

    [record] = read_stored_sessions()
    assert record["status"] == "idle" and record["pid"] == str(os.getpid())
    assert record["launch"] == {"launch_id": "a" * 32, "profile_id": "f1"}


@posix_only
def test_claude_fixture_runs_exactly_the_installed_hooks():
    settings = {}
    cli._install_hooks(settings, cli.CLAUDE_HOOK_EVENTS, "/venv/bin/tongs-agents-hook", "claude")

    calls = fake_agent.claude_calls(settings, "sid", "/work")

    assert [(event, argv) for event, argv, _ in calls] == [
        ("SessionStart", ["/venv/bin/tongs-agents-hook", "session-start", "--agent", "claude"]),
        ("UserPromptSubmit", ["/venv/bin/tongs-agents-hook", "prompt-submit", "--agent",
                              "claude"]),
        ("Stop", ["/venv/bin/tongs-agents-hook", "stop", "--agent", "claude"]),
    ]
    assert all(payload["session_id"] == "sid" and payload["cwd"] == "/work"
               for _, _, payload in calls)
    assert fake_agent.claude_calls({}, "sid", "/work") == []


def test_fake_agent_strips_terminal_control_sequences():
    assert fake_agent.CONTROL.sub("", "\x1b[Browfocus0\r") == "rowfocus0"


def test_fake_cli_records_calls_and_reports_no_server(tmp_path, monkeypatch):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("TONGS_AGENTS_FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))

    assert fake_cli.main(["/bin/claude", "mcp", "add", "--scope", "user", "x"]) == 0
    assert fake_cli.main(["/bin/codex", "mcp", "get", "x", "--json"]) == 1

    first, second = [json.loads(line) for line in log.read_text().splitlines()]
    assert first == {"cli": "claude", "args": ["mcp", "add", "--scope", "user", "x"],
                     "env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}}
    assert second["cli"] == "codex"


def test_committed_evidence_passes_on_both_pins():
    document = json.loads(EVIDENCE.read_text())

    assert document["schema"] == "workspace-validation/1"
    assert document["provenance"] == "instrumented"
    assert sorted(document["runs"]) == list(PINS)
    for version, run in document["runs"].items():
        assert run["result"] == "pass" and run["error"] is None
        assert run["zellij_version"] == f"zellij {version}"
        assert re.fullmatch(r"[0-9a-f]{40}", run["tested_commit"])
        assert not run["modified_since_commit"]
        assert "validation/workspace.py" in run["sha256"]
        workspace = run["workspace"]
        assert workspace["session_listed_seconds"] <= 5
        assert workspace["terminal_panes"] == workspace["expected_terminal_panes"] == 3
        assert run["spawn_request"]["second_consume"] is None
        assert not run["spawn_request"]["request_file_after_mount"]
        for facts in run["first_profiles"]["profiles"].values():
            assert facts["seconds_after_panes_listed"] <= 4
            assert facts["launch_id_matches"] and facts["profile_id_matches"]
        rerun = run["rerun"]
        assert rerun["exit_status"] == 0 and rerun["sessions_after"] == ["<session>"]
        assert rerun["zellij_servers_after"] == 1 and rerun["sidebar_panes"] == 1
        hooked = run["claude_config_dir"]
        assert hooked["profiles"]["f3"]["source"] == "hook"
        assert hooked["profiles"]["f3"]["status"] != "discovered"
        assert hooked["control"]["status"] == "discovered"
        assert run["install"]["exit_code"] == 0
        assert not run["install"]["private_home_settings_written"]
        focus = run["row_focus"]
        assert focus["attached_clients"] == 2 and focus["parent_activation_calls"] == []
        assert len(focus["steps"]) == 3
        for step in focus["steps"]:
            assert step["selection_succeeded"] and not step["activation_attempted"]
            assert step["second_client_moved_to_target"]
            assert step["keystroke_routed_to"] == [step["profile"]]
        assert run["cleanup"]["survivors"] == 0 and run["cleanup"]["temp_dir_removed"]
        assert run["cleanup"]["marker_strays_after_run"] == 0
        assert run["user_config_unchanged"] and run["user_agent_config_unchanged"]


def test_committed_evidence_is_public_safe():
    text = EVIDENCE.read_text()

    assert "/home/" not in text and "/tmp/" not in text and "/run/user/" not in text
    assert "\u2014" not in text
    assert "tongs-ws-" not in text
    assert "<session>" in text


def test_hosted_workspace_job_runs_the_validator_on_every_pin():
    text = (ROOT / ".github" / "workflows" / "zellij.yml").read_text()
    jobs = text.split("\njobs:\n", 1)[1]
    job = re.search(r"^  workspace-validation:\n(.*?)(?=^  \S|\Z)", jobs, re.M | re.S)

    assert job is not None
    body = job.group(1)
    assert "needs: pins" in body
    assert "fromJSON(needs.pins.outputs.versions)" in body
    assert "python validation/workspace.py --no-write" in body
    assert 'test "$actual_sha256" = "$expected_sha256"' in body
    assert body.index("expected_sha256") < body.index("tar -xzf")


def _geometry_pane(pane_id, title, x, columns=80, **extra):
    return {"id": pane_id, "title": title, "tab_id": 0, "pane_x": x, "pane_columns": columns,
            "pane_rows": 12, "is_floating": False, "is_suppressed": False, **extra}


def _stacked_panes():
    return [
        _geometry_pane(0, validator.workspace_spawn.SIDEBAR_PANE_NAME, 0, 40),
        _geometry_pane(1, validator.HOME_PANE_NAME, 40, is_suppressed=True),
        _geometry_pane(2, "f1", 40, is_suppressed=True),
        _geometry_pane(3, "f2", 40, pane_rows=1),
    ]


@linux_only
def test_stack_membership_accepts_profiles_in_the_home_column():
    observations = {}
    validator.stack_membership(_Target(["s1"], _stacked_panes()), ("f1", "f2"),
                               observations, "stack")
    facts = observations["stack"]
    assert facts["member_titles"] == ["f1", "f2", validator.HOME_PANE_NAME]
    assert facts["sidebar_outside_stack"] and facts["suppressed_members"] == 2
    assert facts["collapsed_members"] == 1


@linux_only
@pytest.mark.parametrize("stray", [
    {"pane_x": 0, "pane_columns": 40}, {"tab_id": 1}, {"is_floating": True},
])
def test_stack_membership_refuses_a_profile_outside_the_stack(stray):
    panes = _stacked_panes()
    panes[3].update(stray)
    with pytest.raises(validator.Check, match="not stacked"):
        validator.stack_membership(_Target(["s1"], panes), ("f1", "f2"), {}, "stack")


@linux_only
def test_stack_membership_needs_a_home_pane():
    panes = [pane for pane in _stacked_panes() if pane["title"] != validator.HOME_PANE_NAME]
    with pytest.raises(validator.Check, match="no home pane"):
        validator.stack_membership(_Target(["s1"], panes), ("f1",), {}, "stack")


class _RecoveringTarget(_Target):
    """Closing the home pane removes it; the fake adapter's spawn adds it back."""

    def __init__(self, panes):
        super().__init__(["s1"], panes)
        self.closed = []

    def action(self, *arguments):
        if arguments[0] == "close-pane":
            pane_id = int(arguments[-1].removeprefix("terminal_"))
            self.closed.append(pane_id)
            self._panes = [pane for pane in self._panes if pane["id"] != pane_id]
        return 0, "", ""


class _FakeAdapter:
    def __init__(self, target, panes, notices=()):
        self.target = target
        self.panes = panes
        self.notices = list(notices)

    def __call__(self):
        return self

    def spawn(self, session, argv, *, name=None):
        self.target._panes.extend(self.panes)
        return SimpleNamespace(pane_id=str(self.panes[-1]["id"]))

    def take_notices(self):
        return self.notices


@linux_only
def test_home_recovery_expects_a_new_home_in_the_agent_column(monkeypatch):
    target = _RecoveringTarget(_stacked_panes())
    reopened = [_geometry_pane(7, validator.HOME_PANE_NAME, 40),
                _geometry_pane(8, "recovered", 40)]
    monkeypatch.setattr(validator, "ZellijMultiplexer", _FakeAdapter(target, reopened))
    observations = {}
    validator.home_recovery("/root", "s1", target, _Tracker(), observations)
    facts = observations["home_recovery"]
    assert target.closed == [1]
    assert facts["home_reopened"] and facts["same_column"] and facts["spawn_stacked"]
    assert (facts["members_before"], facts["members_after"]) == (3, 4)


@linux_only
def test_home_recovery_refuses_a_side_by_side_fallback(monkeypatch):
    target = _RecoveringTarget(_stacked_panes())
    monkeypatch.setattr(validator, "ZellijMultiplexer", _FakeAdapter(
        target, [_geometry_pane(8, "recovered", 0, 40)], ["the agent stack is gone"]))
    with pytest.raises(validator.Check, match="not reopened"):
        validator.home_recovery("/root", "s1", target, _Tracker(), {})


@linux_only
@pytest.mark.parametrize("pane, shown", [
    ({"is_suppressed": False, "pane_rows": 12}, True),
    ({"is_suppressed": True, "pane_rows": 12}, False),
    ({"is_suppressed": False, "pane_rows": 1}, False),
    ({}, False),
])
def test_expanded_reads_both_zellij_stack_shapes(pane, shown):
    assert validator.expanded(pane) is shown
