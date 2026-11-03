"""``tongs-agents config status|apply|capture`` and its doctor checks.

Synthetic: dotfiles, harness homes and state live under tmp_path, and the
emitters are the fakes from test_config_engine.
"""

import json

import pytest

from tongs_agents import cli, owned, paths
from tongs_agents.commands import config as config_command
from tongs_agents.config_layer import engine

from tests.test_config_engine import (  # noqa: F401 - fixtures
    FakeClaude,
    FakeCodex,
    homes,
    snapshot,
    write_dotfiles,
)


@pytest.fixture
def registry(homes, monkeypatch):
    claude = FakeClaude(homes.home / ".claude")
    codex = FakeCodex(homes.home / ".codex")
    loaded = engine.Registry({"claude": claude, "codex": codex})
    monkeypatch.setattr(engine, "load_emitters", lambda: loaded)
    return loaded


@pytest.fixture
def dotfiles(tmp_path, homes):
    return write_dotfiles(tmp_path / "dotfiles", homes.alt)


def run(*argv):
    parser, handlers = cli.build_parser()
    args = parser.parse_args(["config", *argv])
    return handlers["config"](args)


def harness_state(homes):
    return snapshot(homes.home, homes.alt, paths.state_dir())


def test_status_without_source_is_an_error(homes, registry, capsys):
    assert run("status") == config_command.EXIT_ERROR
    assert "no dotfiles source" in capsys.readouterr().err


def test_status_with_no_emitters_is_clean(dotfiles, monkeypatch, capsys):
    monkeypatch.setattr(engine, "load_emitters", lambda: engine.Registry({}))
    assert run("status", "--source", str(dotfiles)) == 0
    assert "No config emitters" in capsys.readouterr().out


def test_dry_run_shows_a_diff_and_writes_nothing(dotfiles, homes, registry, capsys):
    before = harness_state(homes)
    assert run("apply", "--dry-run", "--source", str(dotfiles)) == 0
    out = capsys.readouterr().out
    assert "+++ b" in out and "+Be brief." in out and "pending" in out
    assert harness_state(homes) == before


def test_apply_is_idempotent_and_status_tracks_drift(dotfiles, homes, registry, capsys):
    assert run("status", "--source", str(dotfiles)) == config_command.EXIT_DRIFT
    assert run("apply", "--source", str(dotfiles)) == 0
    assert "instructions.md" in capsys.readouterr().out
    assert run("status", "--source", str(dotfiles)) == 0
    assert "in sync" in capsys.readouterr().out

    before = harness_state(homes)
    assert run("apply", "--source", str(dotfiles)) == 0
    assert "Nothing to change." in capsys.readouterr().out
    assert harness_state(homes) == before

    path = homes.alt / "tongs-agents" / "instructions.md"
    path.write_text(path.read_text() + "manual edit\n")
    assert run("status", "--source", str(dotfiles)) == config_command.EXIT_DRIFT
    assert "drifted" in capsys.readouterr().out
    assert run("status", "--json", "--source", str(dotfiles)) == config_command.EXIT_DRIFT
    report = json.loads(capsys.readouterr().out)
    assert report["clean"] is False
    assert {"path": str(path), "state": "drifted"}.items() <= next(
        entry for entry in report["entries"] if entry["path"] == str(path)
    ).items()


def test_apply_skips_drift_until_forced(dotfiles, homes, registry, capsys):
    run("apply", "--source", str(dotfiles))
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.write_text(path.read_text() + "manual edit\n")
    (dotfiles / "instructions" / "common.md").write_text("New text.\n")
    capsys.readouterr()
    assert run("apply", "--source", str(dotfiles)) == config_command.EXIT_DRIFT
    assert "skipped" in capsys.readouterr().err
    assert "manual edit" in path.read_text()
    assert run("apply", "--force", "--source", str(dotfiles)) == 0
    assert "New text." in path.read_text()
    assert run("status", "--source", str(dotfiles)) == 0


def test_apply_refuses_an_unmarked_target(dotfiles, homes, registry, capsys):
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.parent.mkdir(parents=True)
    path.write_text("mine\n")
    before = harness_state(homes)
    assert run("apply", "--source", str(dotfiles)) == config_command.EXIT_ERROR
    err = capsys.readouterr().err
    assert "without the tongs-agents marker" in err and "nothing was written" in err
    assert harness_state(homes) == before
    assert run("apply", "--dry-run", "--source", str(dotfiles)) == config_command.EXIT_ERROR


def test_apply_failure_rolls_back_and_exits_1(dotfiles, homes, registry, monkeypatch, capsys):
    before = harness_state(homes)
    real = owned._replace
    calls = []

    def replace(source, destination):
        calls.append(destination)
        if len(calls) == 3:
            raise OSError("injected failure")
        real(source, destination)

    monkeypatch.setattr(owned, "_replace", replace)
    assert run("apply", "--source", str(dotfiles)) == config_command.EXIT_ERROR
    assert "rolled back" in capsys.readouterr().err
    after = harness_state(homes)
    assert {path: value[0] for path, value in after.items()} == {
        path: value[0] for path, value in before.items()
    }


def test_capture_writes_only_into_dir(tmp_path, dotfiles, homes, registry, capsys):
    registry.emitters["claude"].captured[None] = {
        "mcp_servers": {"issues": {"command": ["example-issues-mcp"], "env": ["EXAMPLE_TOKEN"]}},
    }
    before = harness_state(homes)
    into = tmp_path / "out"
    assert run("capture", "claude", "--into", str(into)) == 0
    assert harness_state(homes) == before
    assert '[mcp_servers.issues]' in (into / "agentic.toml").read_text()
    assert run("capture", "claude", "--into", str(into)) == config_command.EXIT_ERROR
    assert "not empty" in capsys.readouterr().err
    assert run("capture", "claude", "--into", str(into), "--force") == 0


def test_capture_reads_profiles_from_the_source(tmp_path, dotfiles, homes, registry):
    registry.emitters["claude"].captured[homes.alt] = {"instructions": "From alt.\n"}
    into = tmp_path / "out"
    assert run("capture", "claude", "--into", str(into), "--source", str(dotfiles)) == 0
    assert (into / "instructions" / "claude.md").read_text() == "From alt.\n"


def test_capture_rejects_unknown_harness(tmp_path, registry):
    with pytest.raises(SystemExit):
        run("capture", "vim", "--into", str(tmp_path / "out"))


def _configure_source(dotfiles):
    config_file = paths.config_dir(create=True) / "config.toml"
    config_file.write_text(f'[dotfiles]\npath = "{dotfiles}"\n')


def test_doctor_checks_report_drift(dotfiles, homes, registry):
    assert config_command.doctor_checks() == []
    _configure_source(dotfiles)
    pending = config_command.doctor_checks()
    assert [(check.level, check.message.split(" ")[0]) for check in pending] == [("info", "5")]
    assert run("apply") == 0
    assert [check.level for check in config_command.doctor_checks()] == ["ok"]
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.write_text(path.read_text() + "manual edit\n")
    checks = config_command.doctor_checks()
    assert [check.level for check in checks] == ["warn"]
    assert str(path) in checks[0].details


def test_doctor_checks_report_a_bad_source(tmp_path, homes, registry):
    _configure_source(tmp_path / "missing")
    checks = config_command.doctor_checks()
    assert [check.level for check in checks] == ["error"]
    assert "does not exist" in checks[0].message
