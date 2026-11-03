"""``tongs-agents sandbox`` (#36): labelled sandboxes only, idempotent lifecycle, prover diff.

Every OpenShell call runs against the fake CLI in tests/fixtures/fake_openshell
(instrumented evidence); the argument shapes are unvalidated against a live
gateway until the maintainer's run (row 21).
"""

import ast
import io
import json
import os
from pathlib import Path

import pytest
from textual.geometry import Region

from tests.test_sandbox_support import (  # noqa: F401
    FAKE_DIR,
    LAUNCH_ID,
    create_sandbox,
    fake,
    make_launch,
    pane_process,
    posix_only,
)
from tongs_agents import cli, paths
from tongs_agents.commands import sandbox as sandbox_command
from tongs_agents.sandbox import openshell, state
from tongs_agents.sessions_feed import SessionFeed
from tongs_agents.tui import FleetMonitorApp
from tongs_agents.widgets import agents_panel
from tongs_agents.widgets.agents_panel import SandboxSessionTable
from tongs_agents.widgets.session_table import SessionTable

PROVER_DIR = FAKE_DIR / "prover"
MUTATING = {"start", "stop", "delete", "download", "settings", "upload", "exec"}

pytestmark = posix_only


def write_config(text: str) -> Path:
    path = paths.config_dir(create=True) / "config.toml"
    path.write_text(text)
    return path


def profile_config(name="boxed", *, gateway=None, policy=None, workdir=None) -> str:
    lines = [f"[profiles.{name}]", 'command = ["claude"]', 'cwd = "~/project"',
             f"[profiles.{name}.sandbox]",
             'image = "example/agent:1"']
    if gateway:
        lines.append(f'gateway = "{gateway}"')
    if policy:
        lines.append(f'policy = "{policy}"')
    if workdir:
        lines.append(f'workdir = "{workdir}"')
    return "\n".join(lines) + "\n"


def run(*argv: str) -> int:
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["sandbox", *argv])
    return handlers[args.command](args) or 0


def unlabelled(fake, name="foreign"):
    result = fake.run("sandbox", "create", "--name", name, "--detach")
    assert result.returncode == 0, result.stderr


def verbs(fake) -> list[list[str]]:
    return [call[:2] for call in fake.calls()]


def mutating_calls(fake) -> list[list[str]]:
    """Calls that change a sandbox; the tests' own ``sandbox create`` setup is left out."""
    return [call for call in fake.calls()
            if call[0] == "settings" or (call[0] == "sandbox" and call[1] in MUTATING)]


@pytest.fixture
def no_tty(monkeypatch):
    monkeypatch.setattr(sandbox_command.sys, "stdin", io.StringIO(""))


@pytest.fixture
def prover(monkeypatch):
    monkeypatch.setenv("PATH", f"{PROVER_DIR}{os.pathsep}{os.environ['PATH']}")


# list and status


def test_list_shows_only_labelled_sandboxes(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    unlabelled(fake)
    assert run("list", "--json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["name"], row["profile"], row["phase"], row["in_use"]) for row in rows] == [
        ("tongs-boxed", "boxed", "Ready", []),
    ]
    assert ["sandbox", "list", "--limit", "1000", "-o", "json"] in fake.calls()
    assert run() == 0
    out = capsys.readouterr().out
    assert "tongs-boxed" in out and "foreign" not in out


def test_list_queries_each_profile_gateway(fake, capsys):
    write_config(profile_config(gateway="remote"))
    assert run("list") == 0
    lists = [call for call in fake.calls() if "list" in call]
    assert lists == [["sandbox", "list", "--limit", "1000", "-o", "json"],
                     ["-g", "remote", "sandbox", "list", "--limit", "1000", "-o", "json"]]


def test_list_reports_an_unreachable_gateway(fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "list")
    assert run("list") == 1
    assert "gateway default" in capsys.readouterr().err


def test_status_prints_labels_and_configured_profile(fake, capsys):
    write_config(profile_config())
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("status", "tongs-boxed", "--json") == 0
    row = json.loads(capsys.readouterr().out)
    assert (row["name"], row["phase"], row["profile"], row["configured_profile"]) == (
        "tongs-boxed", "Ready", "boxed", "boxed",
    )
    assert run("status", "absent-box") == 1
    assert "no sandbox named absent-box" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ("status", "foreign"),
    ("start", "foreign"),
    ("stop", "foreign"),
    ("delete", "foreign", "--yes"),
    ("pull", "foreign", "DEST"),
    ("logs", "foreign"),
    ("policy", "show", "foreign"),
    ("policy", "check", "foreign", "--policy", "POLICY"),
    ("advisor", "on", "foreign"),
])
def test_an_unlabelled_sandbox_is_refused_by_every_action(fake, tmp_path, capsys, argv):
    unlabelled(fake)
    (tmp_path / "policy.yaml").write_text("version: 1\n")
    argv = [str(tmp_path / "dest") if arg == "DEST" else
            str(tmp_path / "policy.yaml") if arg == "POLICY" else arg for arg in argv]
    before = len(fake.calls())
    assert run(*argv) == 1
    assert "tongs-agents does not manage it" in capsys.readouterr().err
    assert [call[:2] for call in fake.calls()[before:]] == [["sandbox", "get"]]
    assert not (tmp_path / "dest").exists()


def test_names_that_look_like_options_are_rejected_before_openshell(fake, capsys):
    with pytest.raises(SystemExit):
        run("delete", "--yes", "-g")
    with pytest.raises(SystemExit):
        run("status", "Bad_Name")
    assert fake.calls() == []


# lifecycle


def test_start_is_idempotent(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed", phases="Stopped")
    assert run("start", "tongs-boxed") == 0
    assert run("start", "tongs-boxed") == 0
    assert "already running" in capsys.readouterr().out
    assert [call for call in fake.calls() if call[1] == "start"] == [
        ["sandbox", "start", "tongs-boxed"],
    ]


def test_stop_is_idempotent(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("stop", "tongs-boxed") == 0
    assert run("stop", "tongs-boxed") == 0
    assert "already stopped" in capsys.readouterr().out
    assert [call for call in fake.calls() if call[1] == "stop"] == [
        ["sandbox", "stop", "tongs-boxed"],
    ]


@pytest.mark.parametrize("after", ["Mystery", "Error", "Stopped"])
def test_start_never_reports_success_for_a_phase_it_does_not_know(fake, monkeypatch, capsys,
                                                                  after):
    create_sandbox(fake, "tongs-boxed", profile="boxed", phases="Stopped")
    monkeypatch.setenv("FAKE_OPENSHELL_LIFECYCLE_PHASE", after)
    assert run("start", "tongs-boxed") == 1
    captured = capsys.readouterr()
    assert "Started" not in captured.out
    assert f"phase '{after}'" in captured.err and "not reporting success" in captured.err


def test_start_reports_a_sandbox_still_provisioning(fake, monkeypatch, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed", phases="Stopped")
    monkeypatch.setenv("FAKE_OPENSHELL_LIFECYCLE_PHASE", "Provisioning")
    assert run("start", "tongs-boxed") == 0
    assert "Starting sandbox tongs-boxed (Provisioning)" in capsys.readouterr().out


@pytest.mark.parametrize("after", ["Mystery", "Ready", "Unknown"])
def test_stop_never_reports_success_for_a_phase_it_does_not_know(fake, monkeypatch, capsys,
                                                                 after):
    create_sandbox(fake, "tongs-boxed", profile="boxed", phases="Mystery")
    monkeypatch.setenv("FAKE_OPENSHELL_LIFECYCLE_PHASE", after)
    assert run("stop", "tongs-boxed") == 1
    captured = capsys.readouterr()
    assert "already stopped" not in captured.out and "Stopped sandbox" not in captured.out
    assert f"phase '{after}'" in captured.err
    assert mutating_calls(fake) == [["sandbox", "stop", "tongs-boxed"]]


def test_stop_and_delete_refuse_a_sandbox_an_agent_runs_in(fake, pane_process, capsys,
                                                         no_tty):
    _process, info = pane_process
    make_launch(info, sandbox="tongs-boxed", profile="boxed")
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("stop", "tongs-boxed") == 1
    assert run("delete", "tongs-boxed", "--yes") == 1
    err = capsys.readouterr().err
    assert f"launch {LAUNCH_ID}" in err and "--force" in err
    assert mutating_calls(fake) == []
    assert run("list", "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["in_use"] == [LAUNCH_ID]
    assert run("stop", "tongs-boxed", "--force") == 0
    assert mutating_calls(fake) == [["sandbox", "stop", "tongs-boxed"]]


def test_delete_needs_confirmation_off_a_terminal(fake, no_tty, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("delete", "tongs-boxed") == 1
    assert "pass --yes" in capsys.readouterr().err
    assert mutating_calls(fake) == []
    assert "tongs-boxed" in fake.state()["sandboxes"]


def test_delete_is_idempotent(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("delete", "tongs-boxed", "--yes") == 0
    assert run("delete", "tongs-boxed", "--yes") == 0
    assert "already absent" in capsys.readouterr().out
    assert mutating_calls(fake) == [["sandbox", "delete", "tongs-boxed"]]
    assert fake.state()["sandboxes"] == {}


def test_delete_continues_after_a_refusal(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    unlabelled(fake)
    assert run("delete", "foreign", "tongs-boxed", "--yes") == 1
    assert mutating_calls(fake) == [["sandbox", "delete", "tongs-boxed"]]
    assert list(fake.state()["sandboxes"]) == ["foreign"]


def test_prune_deletes_only_unused_orphans(fake, pane_process, capsys):
    _process, info = pane_process
    write_config(profile_config("boxed"))
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    create_sandbox(fake, "tongs-gone", profile="gone")
    create_sandbox(fake, "tongs-busy", profile="busy")
    make_launch(info, sandbox="tongs-busy", profile="busy")
    unlabelled(fake)
    assert run("prune", "--yes") == 0
    assert mutating_calls(fake) == [["sandbox", "delete", "tongs-gone"]]
    assert run("prune", "--yes") == 0
    assert "Nothing to prune" in capsys.readouterr().out
    assert run("prune", "--all", "--yes") == 0
    assert mutating_calls(fake)[1:] == [["sandbox", "delete", "tongs-boxed"]]
    assert sorted(fake.state()["sandboxes"]) == ["foreign", "tongs-busy"]


def test_prune_refuses_an_invalid_config_and_deletes_nothing(fake, capsys):
    write_config(profile_config("boxed") + "[profiles.typo]\ncommand = 3\n")
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    create_sandbox(fake, "tongs-gone", profile="gone")
    for argv in (("prune", "--yes"), ("prune", "--all", "--yes")):
        assert run(*argv) == 1
        err = capsys.readouterr().err
        assert "cannot read profiles" in err and "nothing was pruned" in err
    assert mutating_calls(fake) == []
    assert sorted(fake.state()["sandboxes"]) == ["tongs-boxed", "tongs-gone"]


def test_list_warns_about_an_invalid_config(fake, capsys):
    write_config("[profiles.typo]\ncommand = 3\n")
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("list") == 0
    captured = capsys.readouterr()
    assert "tongs-boxed" in captured.out
    assert "cannot read profiles" in captured.err and "default gateway only" in captured.err


def test_prune_without_yes_off_a_terminal_only_lists(fake, no_tty, capsys):
    create_sandbox(fake, "tongs-gone", profile="gone")
    assert run("prune") == 0
    out = capsys.readouterr().out
    assert "tongs-gone" in out and "pass --yes" in out
    assert mutating_calls(fake) == []


def test_the_profile_gateway_is_used_for_its_sandbox(fake, capsys):
    write_config(profile_config(gateway="remote"))
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("status", "tongs-boxed") == 0
    assert fake.calls()[-1][:2] == ["-g", "remote"]
    assert run("status", "tongs-boxed", "-g", "other") == 0
    assert fake.calls()[-1][:2] == ["-g", "other"]


# pull and logs


def test_pull_downloads_the_working_directory_into_a_new_directory(fake, tmp_path, capsys):
    write_config(profile_config(workdir="/sandbox/work"))
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    source = fake.fs("tongs-boxed", "/sandbox/work/src")
    source.mkdir(parents=True)
    (source / "main.py").write_text("print('hi')\n")
    dest = tmp_path / "out"
    assert run("pull", "tongs-boxed", str(dest)) == 0
    assert (dest / "src" / "main.py").read_text() == "print('hi')\n"
    assert mutating_calls(fake) == [
        ["sandbox", "download", "tongs-boxed", "/sandbox/work", str(dest)],
    ]
    second = tmp_path / "file"
    assert run("pull", "tongs-boxed", str(second), "--path", "src/main.py") == 0
    assert second.read_text() == "print('hi')\n"


def test_pull_never_overwrites_host_files(fake, tmp_path, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine")
    existing = tmp_path / "existing.txt"
    existing.write_text("mine")
    assert run("pull", "tongs-boxed", str(busy)) == 1
    assert run("pull", "tongs-boxed", str(existing)) == 1
    assert "refusing" in capsys.readouterr().err
    assert (busy / "keep.txt").read_text() == "mine" and existing.read_text() == "mine"
    assert mutating_calls(fake) == []


@pytest.mark.parametrize("path", ["../etc", "/etc/passwd", "a/../../b"])
def test_pull_stays_in_the_working_directory(fake, tmp_path, capsys, path):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("pull", "tongs-boxed", str(tmp_path / "out"), "--path", path) == 1
    assert mutating_calls(fake) == []


def test_logs_are_bounded_never_followed_and_cleaned(fake, monkeypatch, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("logs", "tongs-boxed", "-n", "3") == 0
    assert len(capsys.readouterr().out.splitlines()) == 3
    assert fake.calls()[-1] == ["logs", "tongs-boxed", "-n", "3"]
    monkeypatch.setenv("FAKE_OPENSHELL_LOG_TEXT", "evil \x1b]0;title\x07\x1b[31mred\x1b[0m")
    assert run("logs", "tongs-boxed") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "red" in out
    assert all("--tail" not in call for call in fake.calls())
    with pytest.raises(SystemExit):
        run("logs", "tongs-boxed", "-n", "0")


# policy and advisor


def test_policy_show_prints_policy_get(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("policy", "show", "tongs-boxed", "--full") == 0
    assert "include_workdir: true" in capsys.readouterr().out
    assert fake.calls()[-1] == ["policy", "get", "tongs-boxed", "--full"]


def test_policy_check_diffs_against_the_active_policy_and_runs_the_prover(
        fake, prover, tmp_path, capsys):
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(openshell_default() + "  pypi:\n    host: pypi.org\n")
    write_config(profile_config(policy=str(candidate)))
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("policy", "check", "tongs-boxed") == 0
    out = capsys.readouterr().out
    assert "--- tongs-boxed (active policy)" in out
    assert "+  pypi:" in out
    assert "openshell-prover: no findings" in out
    assert ["sandbox", "get", "tongs-boxed", "--policy-only"] in fake.calls()
    prover_call = prover_calls(fake)[-1]
    assert prover_call["no_color"] == "1"
    assert prover_call["argv"][:3] == ["check", str(candidate), "--boundary"]
    assert mutating_calls(fake) == []


def test_prover_findings_fail_the_check(fake, prover, tmp_path, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    candidate = tmp_path / "risky.yaml"
    candidate.write_text("network_policies:\n  meta:\n    host: 169.254.169.254\n")
    assert run("diff", "tongs-boxed", "--policy", str(candidate)) == 1
    captured = capsys.readouterr()
    assert "link_local_reach" in captured.out
    assert "reported findings" in captured.err


def test_policy_check_without_the_prover_still_shows_the_diff(fake, tmp_path, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text(openshell_default())
    assert run("policy", "check", "tongs-boxed", "--policy", str(candidate)) == 0
    captured = capsys.readouterr()
    assert "No policy changes" in captured.out
    assert "openshell-prover is not on PATH" in captured.err


def test_policy_check_against_a_boundary_file_needs_no_sandbox(fake, prover, tmp_path,
                                                              capsys):
    boundary = tmp_path / "boundary.yaml"
    boundary.write_text("version: 1\n")
    candidate = tmp_path / "candidate.yaml"
    candidate.write_text("version: 2\n")
    assert run("policy", "check", "--policy", str(candidate), "--boundary", str(boundary)) == 0
    assert fake.calls() == []
    assert prover_calls(fake)[-1]["argv"] == [
        "check", str(candidate), "--boundary", str(boundary),
    ]
    assert run("policy", "check", "--policy", str(candidate)) == 1
    assert "pass a sandbox NAME" in capsys.readouterr().err


def test_policy_check_needs_a_candidate(fake, capsys):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("policy", "check", "tongs-boxed") == 1
    assert "no candidate policy" in capsys.readouterr().err


@pytest.mark.parametrize("state_word, value", [("on", "true"), ("off", "false")])
def test_advisor_is_set_per_sandbox_never_global(fake, capsys, state_word, value):
    create_sandbox(fake, "tongs-boxed", profile="boxed")
    assert run("advisor", state_word, "tongs-boxed") == 0
    assert mutating_calls(fake) == [["settings", "set", "tongs-boxed", "--key",
                                     openshell.POLICY_ADVISOR_KEY, "--value", value]]
    assert fake.state()["sandboxes"]["tongs-boxed"]["settings"] == {
        openshell.POLICY_ADVISOR_KEY: value,
    }


def test_no_command_approves_rules_or_writes_global_settings():
    # M7-C: `sandbox rules approve` is a CLI action name; the `openshell rule`
    # argv lives only in sandbox/approvals.py (tests/test_sandbox_approvals.py).
    for module, allowed in ((sandbox_command, {"approve"}), (openshell, set())):
        tree = ast.parse(Path(module.__file__).read_text())
        literals = {node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        forbidden = {"approve", "approve-all", "rule", "--global",
                     "--approval-mode", "proposal_approval_mode"} - allowed
        assert not literals & forbidden


def test_missing_cli_is_a_clear_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert run("status", "tongs-boxed") == 1
    assert "openshell CLI not found" in capsys.readouterr().err


def test_sync_runs_one_bridge_pass(capsys):
    assert run("sync") == 0
    assert "0 sandboxed launch(es)" in capsys.readouterr().out


# doctor


def test_doctor_is_silent_without_cli_or_sandboxed_profiles(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert sandbox_command.doctor_checks() == []


def test_doctor_errors_when_a_sandboxed_profile_has_no_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    write_config(profile_config())
    [check] = sandbox_command.doctor_checks()
    assert (check.name, check.level) == ("openshell", "error")
    assert "boxed" in check.message


def test_doctor_reports_cli_gateway_host_and_telemetry(fake, monkeypatch):
    write_config(profile_config(gateway="local"))
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.1.6-rhaiv.4")
    monkeypatch.setattr(sandbox_command, "landlock_abi", lambda: 6)
    checks = {check.name: check for check in sandbox_command.doctor_checks()}
    assert checks["openshell"].level == "ok" and "midstream" in checks["openshell"].message
    assert checks["openshell-gateway"].level == "ok"
    assert checks["openshell-telemetry"].level == "info"
    assert "OPENSHELL_TELEMETRY_ENABLED=false" in checks["openshell-telemetry"].remediation
    assert checks["openshell-prover"].level == "info"
    assert fake.calls() == [["--version"], ["-g", "local", "status", "-o", "json"]]
    if os.uname().sysname == "Linux":
        assert checks["landlock"].message == "Landlock ABI 6"


def test_doctor_warns_on_unreachable_gateway_old_cli_and_old_landlock(fake, monkeypatch):
    write_config(profile_config())
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "status")
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.1.2")
    monkeypatch.setattr(sandbox_command, "landlock_abi", lambda: 2)
    checks = sandbox_command.doctor_checks()
    levels = {(check.name, check.level) for check in checks}
    assert ("openshell-gateway", "warn") in levels
    assert ("openshell", "warn") in levels
    if os.uname().sysname == "Linux":
        assert ("landlock", "warn") in levels
    gateway = next(check for check in checks if check.name == "openshell-gateway")
    assert "never starts gateways" in gateway.remediation


def test_doctor_refuses_a_cli_older_than_0_1_0(fake, monkeypatch):
    write_config(profile_config())
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", "0.0.9")
    checks = sandbox_command.doctor_checks()
    assert ("openshell", "error") in {(check.name, check.level) for check in checks}
    assert ["status", "-o", "json"] not in fake.calls()


@pytest.mark.parametrize("version", ["0.0.9", "not a version"])
def test_doctor_only_warns_on_an_unsupported_cli_no_profile_uses(fake, monkeypatch, version):
    monkeypatch.setenv("FAKE_OPENSHELL_VERSION", version)
    checks = sandbox_command.doctor_checks()
    openshell = [check for check in checks if check.name == "openshell"]
    assert [check.level for check in openshell] == ["warn"]
    assert all(check.level != "error" for check in checks)
    assert ["status", "-o", "json"] not in fake.calls()


def test_doctor_only_warns_when_an_unused_cli_cannot_report_its_version(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENSHELL_FAIL", "version")
    [check] = [check for check in sandbox_command.doctor_checks() if check.name == "openshell"]
    assert check.level == "warn" and "version" in check.message


def test_landlock_abi_never_raises():
    value = sandbox_command.landlock_abi()
    assert value is None or value > 0


# sidebar column


def _session_record(name, launch_id=None):
    record = {
        "session_id": f"sid-{name}", "agent": "claude", "repo": name, "cwd": f"/tmp/{name}",
        "status": "running", "detail": "", "ts": 0, "age_seconds": 5,
    }
    if launch_id:
        record["launch"] = {"launch_id": launch_id, "profile_id": name}
    return record


def _table_text(table):
    region = Region(0, 0, table.size.width, table.size.height)
    return [strip.text for strip in table.render_lines(region)]


@pytest.mark.asyncio
@pytest.mark.parametrize("size, label, cell", [((40, 20), "Box", "box"),
                                                ((120, 20), "Sandbox", "tongs-boxed")])
async def test_sidebar_shows_a_sandbox_column_for_sandboxed_rows(monkeypatch, size, label,
                                                                 cell):
    monkeypatch.setattr(agents_panel.shutil, "which", lambda name: None)
    state.write(state.new(
        launch_id=LAUNCH_ID, profile="boxed", harness="claude", sandbox="tongs-boxed",
        mode="exec", workdir="/sandbox/workspace",
        spool=f"/sandbox/workspace/.tongs-agents/spool/{LAUNCH_ID}", gateway=None,
        cwd="", terminal="", terminal_env={},
    ))
    records = [_session_record("boxed", LAUNCH_ID), _session_record("plain")]
    feed = SessionFeed(lambda: [dict(record) for record in records])
    app = FleetMonitorApp(refresh_interval=60, notify_level="none", feed=feed)
    async with app.run_test(size=size) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()
        table = app.query_one(SessionTable)
        assert isinstance(table, SandboxSessionTable)
        assert [c.key.value for c in table.ordered_columns][-1] == "sandbox"
        assert str(table.ordered_columns[-1].label) == label
        lines = _table_text(table)
        boxed = next(line for line in lines if "boxed" in line and "plain" not in line)
        plain = next(line for line in lines if "plain" in line)
        assert cell in boxed and cell not in plain
        assert table.virtual_size.width <= table.size.width or size[0] >= 80
        records[:] = [_session_record("plain")]
        app.query_one(agents_panel.AgentsPanel).poll()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert "sandbox" not in [c.key.value for c in table.ordered_columns]


def openshell_default() -> str:
    return (
        "version: 1\nfilesystem_policy:\n  include_workdir: true\nnetwork_policies: {}\n"
    )


def prover_calls(fake) -> list[dict]:
    path = fake.home / "prover-calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]
