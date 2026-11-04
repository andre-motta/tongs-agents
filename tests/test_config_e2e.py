"""``tongs-agents config`` end to end: the real engine and the real emitters.

Each verb runs as a ``tongs-agents`` process over a copy of the synthetic
dotfiles fixture, with HOME, the tongs-agents state and config dirs and every
harness config home under tmp_path. ``claude``, ``codex``, ``pi`` and
``tongs-agents-hook`` are fake CLIs on an otherwise empty PATH: they log every
call, and ``claude mcp`` and ``pi mcp`` edit ``.claude.json`` and
``mcp.json`` the way the real CLIs do. A ``fail-<cli>-<verb>`` file in
tmp_path makes that call fail; ``fail-claude-add-after-write`` makes
``claude mcp add`` write the server and then exit 1. No real harness CLI or config home is touched.
Synthetic evidence.
"""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import tongs_agents
from tongs_agents.config_layer import engine, schema

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake harness CLIs")

FIXTURE = Path(__file__).parent / "fixtures" / "dotfiles_synthetic"
SOURCE_ROOT = Path(tongs_agents.__file__).resolve().parents[1]
MAIN = "import sys; from tongs_agents.cli import main; sys.argv[0] = 'tongs-agents'; main()"
COMMON = "Prefer small, reviewed changes."
CODEX_ONLY = "Codex only: keep sandbox defaults."
HARNESS_CLIS = ("claude", "codex", "pi", "tongs-agents-hook")
FAKE_CLI = """#!{python} -I
import json, os, sys
from pathlib import Path

control = Path({control!r})
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with open(control / "cli-calls.log", "a") as log:
    log.write(" ".join([name, *args]) + "\\n")


def fail_if(tag):
    if (control / f"fail-{{tag}}").exists():
        sys.stderr.write(f"simulated {{tag}} failure\\n")
        sys.exit(1)


def servers_in(path):
    data = json.loads(path.read_text()) if path.exists() else {{}}
    return data, data.setdefault("mcpServers", {{}})


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\\n")


if name == "claude" and args[:1] == ["mcp"]:
    directory = os.environ.get("CLAUDE_CONFIG_DIR")
    path = Path(directory or os.environ["HOME"]) / ".claude.json"
    verb, rest = args[1], args[2:]
    assert rest[:2] == ["--scope", "user"], rest
    data, servers = servers_in(path)
    if verb == "add":
        fail_if("claude-add")
        if rest[2] in servers:
            sys.stderr.write(f"MCP server {{rest[2]}} already exists in user config\\n")
            sys.exit(1)
        servers[rest[2]] = {{"type": "stdio", "command": rest[4], "args": rest[5:], "env": {{}}}}
    elif servers.pop(rest[2], None) is None:
        sys.stderr.write(f"No MCP server named {{rest[2]}}\\n")
        sys.exit(1)
    save(path, data)
    if verb == "add":
        fail_if("claude-add-after-write")
elif name == "pi" and args == ["--version"]:
    print("1.5.0")
elif name == "pi" and args[:1] == ["mcp"]:
    path = Path(os.environ["PI_CODING_AGENT_DIR"]) / "mcp.json"
    data, servers = servers_in(path)
    verb, server, rest = args[1], args[2], args[3:]
    if verb == "add":
        fail_if("pi-add")
        entry = {{}}
        if rest[:1] == ["--cwd"]:
            entry["cwd"], rest = rest[1], rest[2:]
        assert rest[0] == "--", rest
        servers[server] = {{"command": rest[1], "args": rest[2:], **entry}}
    else:
        servers.pop(server, None)
    save(path, data)
"""


@pytest.fixture
def world(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in HARNESS_CLIS:
        fake = bin_dir / name
        fake.write_text(FAKE_CLI.format(python=sys.executable, control=str(tmp_path)))
        fake.chmod(0o755)
    dotfiles = tmp_path / "dotfiles"
    shutil.copytree(FIXTURE, dotfiles)
    for path in [dotfiles, *dotfiles.rglob("*")]:
        path.chmod(0o700 if path.is_dir() else 0o600)
    dirs = {harness: tmp_path / f"{harness}-home" for harness in ("claude", "codex", "pi")}
    config_dir = tmp_path / "config"
    config_dir.mkdir(mode=0o700)
    profiles = config_dir / "config.toml"
    profiles.write_text(
        f'[profiles.claude-alt]\ncommand = ["claude"]\nconfig_dir = "{dirs["claude"]}"\n\n'
        f'[profiles.codex-review]\ncommand = ["codex"]\nconfig_dir = "{dirs["codex"]}"\n\n'
        f'[profiles.pi-local]\ncommand = ["pi"]\nconfig_dir = "{dirs["pi"]}"\n'
    )
    profiles.chmod(0o600)
    state = tmp_path / "state"
    pythonpath = os.pathsep.join(
        item for item in (str(SOURCE_ROOT), os.environ.get("PYTHONPATH")) if item
    )
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": str(bin_dir),
        "PYTHONPATH": pythonpath,
        "TONGS_AGENTS_STATE_DIR": str(state),
        "TONGS_AGENTS_CONFIG_DIR": str(config_dir),
        "EXAMPLE_ISSUES_TOKEN": "never-written",
    }

    def run(*argv):
        return subprocess.run(
            [sys.executable, "-c", MAIN, "config", *argv],
            capture_output=True, text=True, env=env, timeout=120, cwd=tmp_path,
        )

    return SimpleNamespace(
        tmp=tmp_path, home=home, dirs=dirs, dotfiles=dotfiles, state=state,
        log=tmp_path / "cli-calls.log", run=run,
    )


def source(world):
    return ("--source", str(world.dotfiles))


def snapshot(world):
    files = {}
    for root in (world.home, *world.dirs.values(), world.state):
        for path in sorted(Path(root).rglob("*")):
            if path.is_file():
                info = path.stat()
                files[str(path)] = (path.read_bytes(), info.st_mtime_ns, info.st_mode)
    return files


def entries(stdout):
    """``{label: state}`` from the entry lines ``config status`` prints."""
    found = {}
    for line in stdout.splitlines():
        if line.startswith("  "):
            state, _, rest = line.strip().partition("  ")
            found[rest.strip().split("  (")[0]] = state
    return found


def calls(world):
    return world.log.read_text().splitlines() if world.log.exists() else []


def mcp_calls(world):
    return [call for call in calls(world) if " mcp " in f" {call} " or call.startswith("pi install")]


def servers(path):
    return json.loads(path.read_text())["mcpServers"]


def apply_ok(world, *options):
    result = world.run("apply", *options, *source(world))
    assert "Traceback" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def no_traceback(result):
    assert "Traceback" not in result.stderr, result.stderr


def claude_server(world, name, home=None):
    home = world.dirs["claude"] if home is None else home
    return f"{home / '.claude.json'} [mcpServers.{name}]"


def test_status_reports_every_target_pending(world):
    result = world.run("status", *source(world))
    no_traceback(result)
    assert result.returncode == 3, result.stderr
    states = entries(result.stdout)
    assert states and set(states.values()) == {"pending"}
    for home in (world.home / ".claude", world.home / ".codex", *world.dirs.values()):
        assert any(label.startswith(f"{home}{os.sep}") for label in states), home
    assert claude_server(world, "issues") in states
    assert claude_server(world, "tongs-agents", world.home) in states
    assert f"{world.dirs['pi'] / 'mcp.json'} [mcpServers.notes]" in states
    assert f"{world.dirs['pi'] / 'tongs-agents.json'} [Pi extension]" in states
    assert "note: " in result.stdout and "HTTP servers are not emitted" in result.stdout
    assert mcp_calls(world) == []


def test_dry_run_shows_a_diff_and_the_cli_steps_and_writes_nothing(world):
    before = snapshot(world)
    result = world.run("apply", "--dry-run", *source(world))
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    assert f"+++ b{world.dirs['codex'] / 'AGENTS.md'}" in result.stdout
    assert f"+{CODEX_ONLY}" in result.stdout
    assert f"+{COMMON}" in result.stdout
    assert "pending" in result.stdout
    would_run = [line for line in result.stdout.splitlines() if line.startswith("would run")]
    assert any("mcp add --scope user issues -- example-issues-mcp --stdio" in line
               for line in would_run), would_run
    assert any("mcp add notes --cwd" in line for line in would_run), would_run
    assert any("install the tongs-agents Pi extension" in line for line in would_run)
    assert snapshot(world) == before
    assert mcp_calls(world) == []


def test_apply_writes_every_target_and_a_second_apply_writes_nothing(world):
    result = apply_ok(world)

    claude, codex, pi = world.dirs["claude"], world.dirs["codex"], world.dirs["pi"]
    assert COMMON in (claude / "tongs-agents" / "instructions.md").read_text()
    assert "@tongs-agents/instructions.md" in (claude / "CLAUDE.md").read_text().splitlines()
    assert (claude / "skills" / "tongs-agents" / "skills" / "pr-review" / "SKILL.md").is_file()
    agents = (codex / "AGENTS.md").read_text()
    assert COMMON in agents and CODEX_ONLY in agents
    tables = tomllib.loads((codex / "config.toml").read_text())["mcp_servers"]
    assert tables["issues"]["env_vars"] == ["EXAMPLE_ISSUES_TOKEN"]
    assert "docs" not in tables and "paused" not in tables
    assert (codex / "skills" / "tongs-agents" / "release-notes" / "template.md").is_file()
    pi_agents = (pi / "AGENTS.md").read_text()
    assert COMMON in pi_agents and CODEX_ONLY not in pi_agents
    assert (pi / "skills" / "tongs-agents" / "release-notes" / "SKILL.md").is_file()
    assert not (pi / "skills" / "tongs-agents" / "pr-review").exists()

    for home in (claude, world.home):
        registered = servers(home / ".claude.json")
        assert set(registered) == {"issues", "tongs-agents"}
        assert registered["issues"]["args"] == ["--stdio"]
    pi_servers = servers(pi / "mcp.json")
    assert set(pi_servers) == {"issues", "notes", "tongs-agents"}
    assert pi_servers["notes"]["cwd"] == str(world.home / "notes")
    assert (pi / "tongs-agents.json").is_file()
    assert "pi install" in "\n".join(calls(world))
    assert "ran        claude" in result.stdout
    for path in snapshot(world):
        assert b"never-written" not in Path(path).read_bytes(), path

    before = snapshot(world)
    called = len(calls(world))
    again = apply_ok(world)
    assert again.stdout.strip().splitlines()[0] == "Nothing to change."
    assert snapshot(world) == before
    assert [call for call in calls(world)[called:] if "--version" not in call] == []

    status = world.run("status", *source(world))
    assert status.returncode == 0, status.stdout + status.stderr
    states = entries(status.stdout)
    assert set(states.values()) == {"in sync"}
    assert states[f"{pi / 'tongs-agents.json'} [Pi extension]"] == "in sync"
    assert "Every managed entry is in sync." in status.stdout


def test_a_manual_edit_shows_drift(world):
    apply_ok(world)
    path = world.dirs["claude"] / "tongs-agents" / "instructions.md"
    path.write_text(path.read_text() + "manual edit\n")

    result = world.run("status", *source(world))
    no_traceback(result)
    assert result.returncode == 3
    states = entries(result.stdout)
    assert states[str(path)] == "drifted"
    assert {state for name, state in states.items() if name != str(path)} == {"in sync"}

    kept = world.run("apply", *source(world))
    assert kept.returncode == 3
    assert f"skipped {path}" in kept.stderr
    assert path.read_text().endswith("manual edit\n")


def test_status_reads_cli_managed_servers_back(world):
    apply_ok(world)
    claude_json = world.dirs["claude"] / ".claude.json"
    data = json.loads(claude_json.read_text())
    del data["mcpServers"]["issues"]
    claude_json.write_text(json.dumps(data))
    mcp_json = world.dirs["pi"] / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["issues"]["args"] = ["--edited"]
    mcp_json.write_text(json.dumps(data))
    pi_issues = f"{mcp_json} [mcpServers.issues]"

    result = world.run("status", *source(world))
    no_traceback(result)
    assert result.returncode == 3
    states = entries(result.stdout)
    assert states[claude_server(world, "issues")] == "missing"
    assert states[pi_issues] == "drifted"
    assert {state for name, state in states.items()
            if name not in (claude_server(world, "issues"), pi_issues)} == {"in sync"}

    kept = world.run("apply", *source(world))
    assert kept.returncode == 3, kept.stderr
    assert f"skipped {pi_issues}" in kept.stderr
    assert "issues" in servers(claude_json)
    assert servers(mcp_json)["issues"]["args"] == ["--edited"]

    apply_ok(world, "--force")
    assert servers(mcp_json)["issues"]["args"] == ["--stdio"]
    assert world.run("status", *source(world)).returncode == 0


def test_a_failed_cli_step_exits_1_keeps_the_files_and_is_retried(world):
    (world.tmp / "fail-claude-add").touch()
    result = world.run("apply", *source(world))
    no_traceback(result)
    assert result.returncode == 1
    assert "failed claude" in result.stderr
    assert "add Claude Code MCP server issues: simulated claude-add failure" in result.stderr
    assert (world.dirs["claude"] / "tongs-agents" / "instructions.md").is_file()
    assert "issues" in servers(world.dirs["pi"] / "mcp.json")

    status = world.run("status", *source(world))
    assert status.returncode == 3
    assert entries(status.stdout)[claude_server(world, "issues")] == "pending"

    (world.tmp / "fail-claude-add").unlink()
    retried = apply_ok(world)
    assert "add Claude Code MCP server issues" in retried.stdout
    assert set(servers(world.dirs["claude"] / ".claude.json")) == {"issues", "tongs-agents"}
    assert world.run("status", *source(world)).returncode == 0


def test_a_server_the_user_owns_refuses_the_apply(world):
    claude_json = world.home / ".claude.json"
    claude_json.write_text(json.dumps({"mcpServers": {"issues": {"command": "mine"}}}))
    before = snapshot(world)

    status = world.run("status", *source(world))
    assert entries(status.stdout)[claude_server(world, "issues", world.home)] == "foreign"

    result = world.run("apply", *source(world))
    no_traceback(result)
    assert result.returncode == 1
    assert "not written by tongs-agents" in result.stderr
    assert snapshot(world) == before
    assert mcp_calls(world) == []


@pytest.mark.parametrize("harness, instructions, skills, mcp", [
    ("claude", (COMMON,), {"pr-review", "release-notes"}, {"issues"}),
    ("codex", (COMMON, CODEX_ONLY), {"pr-review", "release-notes"}, {"issues", "notes"}),
    ("pi", (COMMON,), {"release-notes"}, {"issues", "notes"}),
])
def test_capture_writes_the_harness_side_into_an_empty_dir(
    world, harness, instructions, skills, mcp,
):
    apply_ok(world)
    harness_side = snapshot(world)
    into = world.tmp / f"captured-{harness}"
    into.mkdir()

    result = world.run("capture", harness, "--into", str(into), *source(world))
    no_traceback(result)
    assert result.returncode == 0, result.stderr
    assert snapshot(world) == harness_side

    captured = schema.load_canonical(into, local_profiles={})
    text = captured.instructions_for(harness)
    assert all(part in text for part in instructions)
    assert set(captured.skills) == skills
    template = into / "skills" / "release-notes" / "template.md"
    assert template.read_bytes() == (FIXTURE / "skills" / "release-notes" / "template.md").read_bytes()
    assert set(captured.mcp_servers) == mcp
    assert captured.mcp_servers["issues"].command == ("example-issues-mcp", "--stdio")
    if harness == "codex":
        assert captured.mcp_servers["issues"].env == ("EXAMPLE_ISSUES_TOKEN",)


def test_a_dotfiles_change_to_a_codex_server_reaches_config_toml(world):
    apply_ok(world)
    agentic = world.dotfiles / "agentic.toml"
    agentic.write_text(agentic.read_text().replace('"--stdio"]', '"--stdio", "--verbose"]'))
    apply_ok(world)
    tables = tomllib.loads((world.dirs["codex"] / "config.toml").read_text())["mcp_servers"]
    assert tables["issues"]["args"] == ["--stdio", "--verbose"]
    assert servers(world.dirs["claude"] / ".claude.json")["issues"]["args"] == [
        "--stdio", "--verbose"]
    assert servers(world.dirs["pi"] / "mcp.json")["issues"]["args"] == ["--stdio", "--verbose"]
    assert world.run("status", *source(world)).returncode == 0


def test_a_harness_cli_not_on_path_is_skipped_and_status_can_be_clean(world):
    for name in ("claude", "pi"):
        (world.tmp / "bin" / name).unlink()
    world.dirs["claude"].mkdir()

    status = world.run("status", *source(world))
    no_traceback(status)
    assert status.returncode == 3
    states = entries(status.stdout)
    assert states[claude_server(world, "issues")] == "skipped"
    assert states[claude_server(world, "tongs-agents")] == "skipped"
    path_only = "not on PATH (looked up on PATH only; shell aliases and functions are not seen)"
    assert f"note: claude ({world.dirs['claude']}): skipped: claude {path_only}" in status.stdout
    assert f"skipped: claude {path_only}, and {world.home / '.claude'} does not exist" in (
        status.stdout)
    assert f"skipped: pi {path_only}, and {world.dirs['pi']} does not exist" in status.stdout
    assert not any(label.startswith(f"{world.dirs['pi']}{os.sep}") for label in states)

    result = apply_ok(world)
    assert "would run" not in result.stdout and "ran " not in result.stdout
    assert (world.dirs["claude"] / "tongs-agents" / "instructions.md").is_file()
    assert not (world.dirs["claude"] / ".claude.json").exists()
    assert not (world.home / ".claude").exists()
    assert not world.dirs["pi"].exists()
    assert (world.dirs["codex"] / "AGENTS.md").is_file()
    assert mcp_calls(world) == []

    clean = world.run("status", *source(world))
    assert clean.returncode == 0, clean.stdout + clean.stderr
    assert set(entries(clean.stdout).values()) == {"in sync", "skipped"}
    assert "Every managed entry is in sync." in clean.stdout


def test_a_cli_step_that_wrote_but_failed_is_adopted_by_the_next_apply(world):
    (world.tmp / "fail-claude-add-after-write").touch()
    result = world.run("apply", *source(world))
    no_traceback(result)
    assert result.returncode == 1
    assert "add Claude Code MCP server issues: simulated claude-add-after-write failure" in (
        result.stderr)
    assert set(servers(world.dirs["claude"] / ".claude.json")) == {"issues", "tongs-agents"}

    (world.tmp / "fail-claude-add-after-write").unlink()
    status = world.run("status", *source(world))
    assert status.returncode == 0, status.stdout + status.stderr
    called = len(calls(world))
    apply_ok(world)
    assert [call for call in calls(world)[called:] if call.startswith("claude")] == []
    assert world.run("status", *source(world)).returncode == 0


def test_force_backs_up_the_files_of_drifted_keyed_entries(world):
    apply_ok(world)
    config_toml = world.dirs["codex"] / "config.toml"
    agents_md = world.dirs["codex"] / "AGENTS.md"
    claude_json = world.dirs["claude"] / ".claude.json"
    mcp_json = world.dirs["pi"] / "mcp.json"
    config_toml.write_text(config_toml.read_text().replace('"--stdio"', '"--edited"', 1))
    agents_md.write_text(agents_md.read_text().replace(CODEX_ONLY, "Edited by hand."))
    for path in (claude_json, mcp_json):
        data = json.loads(path.read_text())
        data["mcpServers"]["issues"]["args"] = ["--edited"]
        path.write_text(json.dumps(data))
    edited = {path: path.read_bytes() for path in (config_toml, agents_md, claude_json, mcp_json)}

    states = entries(world.run("status", *source(world)).stdout)
    assert states[f"{config_toml} [mcp_servers.issues]"] == "drifted"
    assert states[f"{agents_md} [tongs-agents block]"] == "drifted"
    assert states[claude_server(world, "issues")] == "drifted"
    assert states[f"{mcp_json} [mcpServers.issues]"] == "drifted"
    assert world.run("apply", *source(world)).returncode == 3
    assert all(path.read_bytes() == data for path, data in edited.items())

    apply_ok(world, "--force")
    for path, data in edited.items():
        backup = path.with_name(path.name + ".tongs-agents.drift.bak")
        assert backup.read_bytes() == data, path
        assert backup.stat().st_mode & 0o077 == 0
    assert "--edited" not in config_toml.read_text()
    assert CODEX_ONLY in agents_md.read_text()
    assert servers(claude_json)["issues"]["args"] == ["--stdio"]
    assert servers(mcp_json)["issues"]["args"] == ["--stdio"]
    assert world.run("status", *source(world)).returncode == 0

    config_toml.write_text(config_toml.read_text().replace('"--stdio"', '"--again"', 1))
    second = config_toml.read_bytes()
    apply_ok(world, "--force")
    assert config_toml.with_name("config.toml.tongs-agents.drift.bak").read_bytes() == (
        edited[config_toml])
    assert config_toml.with_name("config.toml.tongs-agents.drift.bak.1").read_bytes() == second


@pytest.mark.parametrize("version, extension", [
    ("0.90.0", "in sync"),
    ("not-a-version", "pending"),
])
def test_a_pi_without_mcp_support_never_looks_in_sync(world, version, extension):
    fake = world.tmp / "bin" / "pi"
    fake.write_text(fake.read_text().replace('print("1.5.0")', f'print("{version}")'))
    found = "an unrecognized version" if version == "not-a-version" else version
    reason = f"Pi 0.99.0 or newer is required (found {found})"
    pi_server = f"{world.dirs['pi'] / 'mcp.json'} [mcpServers.issues]"

    apply_ok(world)
    status = world.run("status", *source(world))
    no_traceback(status)
    assert status.returncode == 3
    states = entries(status.stdout)
    assert states[pi_server] == "pending"
    assert f"{pi_server}  ({reason})" in status.stdout
    assert states[f"{world.dirs['pi'] / 'tongs-agents.json'} [Pi extension]"] == extension
    assert not any(call.startswith("pi mcp") for call in calls(world))


def test_an_old_pi_lists_servers_the_dotfiles_dropped_as_orphaned(world):
    apply_ok(world)
    agentic = world.dotfiles / "agentic.toml"
    agentic.write_text(agentic.read_text().replace('harnesses = ["codex", "pi"]',
                                                   'harnesses = ["codex"]'))
    fake = world.tmp / "bin" / "pi"
    fake.write_text(fake.read_text().replace('print("1.5.0")', 'print("0.90.0")'))
    mcp_json = world.dirs["pi"] / "mcp.json"
    reason = "Pi 0.99.0 or newer is required (found 0.90.0)"

    status = world.run("status", *source(world))
    no_traceback(status)
    assert status.returncode == 3
    states = entries(status.stdout)
    assert states[f"{mcp_json} [mcpServers.notes]"] == "orphaned"
    assert (f"{mcp_json} [mcpServers.notes]  (no longer in the dotfiles; not removed: {reason})"
            in status.stdout)
    assert states[f"{mcp_json} [mcpServers.issues]"] == "pending"
    called = len(calls(world))
    apply_ok(world)
    assert not any(call.startswith("pi mcp") for call in calls(world)[called:])
    assert "notes" in servers(mcp_json)


def test_a_forced_apply_makes_no_backup_for_a_disabled_drifted_pi_server(world):
    apply_ok(world)
    mcp_json = world.dirs["pi"] / "mcp.json"
    data = json.loads(mcp_json.read_text())
    data["mcpServers"]["issues"].update(args=["--edited"], enabled=False)
    mcp_json.write_text(json.dumps(data))
    edited = mcp_json.read_bytes()

    called = len(calls(world))
    result = apply_ok(world, "--force")
    assert f"Pi MCP server issues: disabled in {mcp_json}; left as is" in result.stdout
    assert mcp_json.read_bytes() == edited
    assert not mcp_json.with_name("mcp.json" + ".tongs-agents.drift.bak").exists()
    assert not any(call.startswith("pi mcp") for call in calls(world)[called:])


def test_apply_is_refused_while_another_apply_holds_the_lock(world, monkeypatch):
    monkeypatch.setenv("TONGS_AGENTS_STATE_DIR", str(world.state))
    before = snapshot(world)
    with engine.apply_lock():
        result = world.run("apply", *source(world))
    no_traceback(result)
    assert result.returncode == 1
    assert "another tongs-agents config apply is running" in result.stderr
    assert "nothing was written" in result.stderr
    lock = str(world.state / "emit" / engine.APPLY_LOCK_NAME)
    assert {path: value for path, value in snapshot(world).items() if path != lock} == before
    assert not mcp_calls(world)
    apply_ok(world)


def test_an_unreadable_claude_json_is_a_conflict(world):
    claude_json = world.home / ".claude.json"
    claude_json.write_text('{"mcpServers": {"issues": ')
    before = snapshot(world)

    status = world.run("status", *source(world))
    no_traceback(status)
    assert status.returncode == 3
    assert entries(status.stdout)[str(claude_json)] == "foreign"
    assert "malformed JSON" in status.stdout

    result = world.run("apply", *source(world))
    assert result.returncode == 1
    assert f"{claude_json}: malformed JSON" in result.stderr
    assert snapshot(world) == before
    assert mcp_calls(world) == []


def test_codex_not_on_path_creates_no_codex_home(world):
    (world.tmp / "bin" / "codex").unlink()
    world.dirs["codex"].mkdir()

    status = world.run("status", *source(world))
    no_traceback(status)
    assert (f"skipped: codex not on PATH (looked up on PATH only; shell aliases and functions "
            f"are not seen), and {world.home / '.codex'} does not exist") in status.stdout
    assert not any(label.startswith(f"{world.home / '.codex'}{os.sep}")
                   for label in entries(status.stdout))

    apply_ok(world)
    assert not (world.home / ".codex").exists()
    agents = (world.dirs["codex"] / "AGENTS.md").read_text()
    assert CODEX_ONLY in agents
    assert "issues" in tomllib.loads((world.dirs["codex"] / "config.toml").read_text())[
        "mcp_servers"]
    assert world.run("status", *source(world)).returncode == 0
