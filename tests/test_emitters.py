"""Claude, Codex and Pi emitters driven directly with owned.py (no engine).

Synthetic: the canonical config is tests/fixtures/dotfiles_synthetic, every
config dir lives under tmp_path, and the claude and pi CLIs are a recording
fake that edits ``.claude.json`` and ``mcp.json`` the way the real CLIs do
(evidence: validation/evidence/harness-config-*-fedora44.json). No harness
binary runs.
"""

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from tongs_agents import owned, pi_install, toml_tables
from tongs_agents.config_layer import emitters
from tongs_agents.config_layer.emitters import (
    EMITTERS,
    EmitConfig,
    EmitError,
    _common,
    apply_plan,
    claude,
    codex,
    pi,
)
from tongs_agents.config_layer.protocol import CanonicalFragment, Emitter, ProfileTarget

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX stubs on PATH")

FIXTURE = Path(__file__).parent / "fixtures" / "dotfiles_synthetic"
SECRET = "synthetic-secret-value-0451"
MCP_ARGS = ["-m", "tongs_agents.mcp_server"]


def _stub(path):
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


class FakeCli:
    """Records claude and pi calls and edits their files like the real CLIs."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **options):
        env = options["env"]
        name = Path(argv[0]).name
        self.calls.append((name, list(argv[1:]), {
            key: env.get(key) for key in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR")
        }))
        if name == "claude":
            directory = env.get("CLAUDE_CONFIG_DIR")
            path = Path(directory) / ".claude.json" if directory else (
                Path(env["HOME"]) / ".claude.json")
            verb, rest = argv[2], argv[3:]
            assert rest[:2] == ["--scope", "user"]
            server = rest[2]
            data = json.loads(path.read_text()) if path.exists() else {}
            servers = data.setdefault("mcpServers", {})
            if verb == "add":
                if server in servers:
                    return SimpleNamespace(returncode=1, stderr="already exists\n")
                command = rest[4:]
                servers[server] = {"type": "stdio", "command": command[0],
                                   "args": command[1:], "env": {}}
            else:
                servers.pop(server, None)
        else:
            path = Path(env["PI_CODING_AGENT_DIR"]) / "mcp.json"
            data = json.loads(path.read_text()) if path.exists() else {}
            servers = data.setdefault("mcpServers", {})
            verb, server, rest = argv[2], argv[3], argv[4:]
            if verb == "add":
                entry = {}
                if rest[0] == "--cwd":
                    entry["cwd"] = rest[1]
                    rest = rest[2:]
                assert rest[0] == "--"
                entry = {"command": rest[1], "args": rest[2:], **entry}
                servers[server] = entry
            else:
                servers.pop(server, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
        return SimpleNamespace(returncode=0, stderr="")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "pi", "tongs-agents-hook"):
        _stub(bin_dir / name)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("EXAMPLE_ISSUES_TOKEN", SECRET)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (1, 5, 0))
    installs = []

    def install_pi(command, hook, env=None):
        installs.append(env["PI_CODING_AGENT_DIR"])
        extension = str(pi_install.pi_extension_path())
        pi_install._save_owned_config(
            Path(env["PI_CODING_AGENT_DIR"]) / pi_install.PI_CONFIG_NAME,
            {"hook_path": str(Path(hook).resolve()), "extension_path": extension,
             "managed_paths": [extension]},
        )
        return True

    monkeypatch.setattr(pi_install, "install_pi", install_pi)
    dirs = {harness: tmp_path / f"{harness}-home" for harness in ("claude", "codex", "pi")}
    profiles = {
        "claude-alt": {"command": ["claude"], "config_dir": str(dirs["claude"])},
        "codex-review": {"command": ["codex"], "config_dir": str(dirs["codex"])},
        "pi-local": {"command": ["pi"], "config_dir": str(dirs["pi"])},
    }
    return SimpleNamespace(
        tmp=tmp_path, home=home, bin=bin_dir, dirs=dirs, profiles=profiles,
        cli=FakeCli(), pi_installs=installs,
        hook=str(bin_dir / "tongs-agents-hook"),
    )


def load(world, root=FIXTURE, **changes):
    document = tomllib.loads((root / "agentic.toml").read_text())
    for key, value in changes.items():
        document[key] = value
    return EmitConfig.from_document(document, root, profiles=world.profiles)


def target(world, harness):
    return ProfileTarget(harness, world.dirs[harness])


def emit(world, harness, config, **options):
    plan = EMITTERS[harness].plan_full(config, target(world, harness), **options)
    result = apply_plan(plan, runner=world.cli)
    assert result.failed_steps == []
    return plan


def tree(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(Path(directory).rglob("*")) if path.is_file()
    }


def copy_fixture(tmp_path):
    destination = tmp_path / "dotfiles"
    for source in FIXTURE.rglob("*"):
        relative = source.relative_to(FIXTURE)
        if source.is_dir():
            (destination / relative).mkdir(parents=True, exist_ok=True)
        else:
            (destination / relative).parent.mkdir(parents=True, exist_ok=True)
            (destination / relative).write_bytes(source.read_bytes())
    return destination


SHARED = {
    "claude": {"CLAUDE.md", "settings.json", ".claude.json"},
    "codex": {"AGENTS.md", "hooks.json", "config.toml"},
    "pi": {"AGENTS.md", "mcp.json"},
}


# Registry and protocol


def test_emitters_registry_implements_the_protocol():
    assert set(EMITTERS) == {"claude", "codex", "pi"}
    assert emitters.EMITTERS is EMITTERS
    for harness, emitter in EMITTERS.items():
        assert isinstance(emitter, Emitter)
        assert emitter.harness_id == harness


def test_an_emitter_refuses_another_harness_target(world):
    with pytest.raises(ValueError, match="codex target"):
        EMITTERS["claude"].plan(load(world), target(world, "codex"))


def test_plan_accepts_an_engine_object_with_root_and_document(world):
    document = tomllib.loads((FIXTURE / "agentic.toml").read_text())
    engine_config = SimpleNamespace(root=FIXTURE, document=document, profiles=world.profiles)
    changes = EMITTERS["codex"].plan(engine_config, target(world, "codex"))
    assert {change.kind for change in changes} == {"create"}


# Per-harness snapshots


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_emission_is_idempotent_and_marks_every_generated_file(world, harness):
    config = load(world)
    first = emit(world, harness, config)
    assert first.conflicts == []
    snapshot = tree(world.dirs[harness])
    manifest = owned.manifest_path().read_bytes()

    second = EMITTERS[harness].plan_full(config, target(world, harness))
    assert [change for change in second.changes if change.kind != "unchanged"] == []
    assert second.commands == []
    world.cli.calls.clear()
    apply_plan(second, runner=world.cli)
    assert world.cli.calls == []
    assert tree(world.dirs[harness]) == snapshot
    assert owned.manifest_path().read_bytes() == manifest

    for relative, raw in snapshot.items():
        path = world.dirs[harness] / relative
        if relative in SHARED[harness] or relative == pi_install.PI_CONFIG_NAME:
            continue
        fmt = _common.SKILL_FORMATS.get(path.suffix) or owned.file_format(path)
        parsed = owned.parse_owned(path, raw, fmt)
        assert parsed is not None and parsed.owner == "tongs-agents", relative
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_secret_values_are_never_written(world, harness):
    emit(world, harness, load(world))
    for raw in tree(world.dirs[harness]).values():
        assert SECRET.encode() not in raw
    assert SECRET.encode() not in owned.manifest_path().read_bytes()
    for _, argv, _ in world.cli.calls:
        assert SECRET not in " ".join(argv)


def test_claude_layout(world):
    plan = emit(world, "claude", load(world))
    directory = world.dirs["claude"]
    bundle = directory / "skills" / "tongs-agents"
    files = set(tree(directory))
    assert {
        "skills/tongs-agents/.claude-plugin/plugin.json",
        "skills/tongs-agents/skills/pr-review/SKILL.md",
        "skills/tongs-agents/skills/release-notes/SKILL.md",
        "skills/tongs-agents/skills/release-notes/template.md",
        "skills/tongs-agents/skills/release-notes/scripts/render.py",
        "tongs-agents/instructions.md",
        "CLAUDE.md",
        "settings.json",
        ".claude.json",
    } == files
    manifest = json.loads((bundle / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "tongs-agents" and manifest["owner"] == "tongs-agents"
    skill = (bundle / "skills" / "release-notes" / "SKILL.md").read_text().splitlines()
    assert skill[:4] == ["---", "name: release-notes",
                         "description: Draft release notes from merged changes.", "---"]
    assert skill[4].startswith("<!-- tongs-agents-owned ")
    script = (bundle / "skills" / "release-notes" / "scripts" / "render.py").read_text()
    assert script.splitlines()[:2] == [
        "#!/usr/bin/env python3",
        '# tongs-agents-owned {"owner": "tongs-agents", "schema_version": 1}',
    ]
    assert (directory / "CLAUDE.md").read_text() == "@tongs-agents/instructions.md\n"
    instructions = (directory / "tongs-agents" / "instructions.md").read_text()
    assert "Prefer small, reviewed changes." in instructions
    assert "Codex only" not in instructions
    settings = json.loads((directory / "settings.json").read_text())
    assert f"{world.hook} session-start --agent claude" in json.dumps(settings)

    calls = [(name, argv[:6], env) for name, argv, env in world.cli.calls]
    assert calls == [
        ("claude", ["mcp", "add", "--scope", "user", "issues", "--"],
         {"CLAUDE_CONFIG_DIR": str(directory), "PI_CODING_AGENT_DIR": None}),
        ("claude", ["mcp", "add", "--scope", "user", "tongs-agents", "--"],
         {"CLAUDE_CONFIG_DIR": str(directory), "PI_CODING_AGENT_DIR": None}),
    ]
    servers = json.loads((directory / ".claude.json").read_text())["mcpServers"]
    assert servers["tongs-agents"]["args"] == MCP_ARGS
    assert servers["issues"] == {"type": "stdio", "command": "example-issues-mcp",
                                 "args": ["--stdio"], "env": {}}
    assert "notes" not in servers and "paused" not in servers and "docs" not in servers
    assert any("docs: HTTP servers" in note for note in plan.notes)


def test_codex_layout(world):
    emit(world, "codex", load(world))
    directory = world.dirs["codex"]
    assert set(tree(directory)) == {
        "skills/tongs-agents/pr-review/SKILL.md",
        "skills/tongs-agents/release-notes/SKILL.md",
        "skills/tongs-agents/release-notes/template.md",
        "skills/tongs-agents/release-notes/scripts/render.py",
        "AGENTS.md", "hooks.json", "config.toml",
    }
    agents = (directory / "AGENTS.md").read_text()
    assert agents.startswith("<!-- tongs-agents:begin -->\n# Shared instructions")
    assert agents.endswith("Codex only: keep sandbox defaults.\n<!-- tongs-agents:end -->\n")
    tables = tomllib.loads((directory / "config.toml").read_text())["mcp_servers"]
    assert tables["issues"] == {"command": "example-issues-mcp", "args": ["--stdio"],
                                "env_vars": ["EXAMPLE_ISSUES_TOKEN"]}
    assert tables["notes"] == {"command": str(world.home / "bin" / "example-notes-mcp"),
                               "cwd": str(world.home / "notes")}
    assert tables["tongs-agents"]["env_vars"] == ["XDG_STATE_HOME", "TONGS_AGENTS_STATE_DIR"]
    assert set(tables) == {"issues", "notes", "tongs-agents"}
    assert "# tongs-agents" not in (directory / "config.toml").read_text()
    assert world.cli.calls == []


def test_pi_layout(world):
    emit(world, "pi", load(world))
    directory = world.dirs["pi"]
    assert set(tree(directory)) == {
        "skills/tongs-agents/release-notes/SKILL.md",
        "skills/tongs-agents/release-notes/template.md",
        "skills/tongs-agents/release-notes/scripts/render.py",
        "AGENTS.md", "mcp.json", pi_install.PI_CONFIG_NAME,
    }
    assert world.pi_installs == [str(directory)]
    servers = json.loads((directory / "mcp.json").read_text())["mcpServers"]
    assert servers["notes"]["cwd"] == str(world.home / "notes")
    assert servers["tongs-agents"]["args"] == MCP_ARGS
    assert "env" not in servers["issues"]
    assert {env["PI_CODING_AGENT_DIR"] for _, _, env in world.cli.calls} == {str(directory)}


def test_pi_mcp_is_skipped_below_the_native_mcp_floor(world, monkeypatch):
    monkeypatch.setattr(pi_install, "detect_pi_version", lambda command: (0, 85, 0))
    plan = emit(world, "pi", load(world))
    assert world.cli.calls == []
    assert any("0.99.0 or newer is required (found 0.85.0)" in note for note in plan.notes)
    assert not (world.dirs["pi"] / "mcp.json").exists()
    assert world.pi_installs == [str(world.dirs["pi"])]


# User content


def test_unmarked_user_files_stay_byte_identical(world):
    directory = world.dirs["codex"]
    user_files = {
        "skills/my-skill/SKILL.md": b"---\nname: my-skill\ndescription: mine\n---\n",
        "skills/tongs-agents/user-note.md": b"a file the user dropped in our dir\n",
        "AGENTS.override.md.disabled": b"user\n",
        "rules/default.rules": b"user rules\n",
    }
    for relative, raw in user_files.items():
        (directory / relative).parent.mkdir(parents=True, exist_ok=True)
        (directory / relative).write_bytes(raw)
    (directory / "AGENTS.md").write_text("# My Codex notes\n\nKeep this.\n")
    for harness in ("codex",):
        emit(world, harness, load(world))
        emit(world, harness, load(world, skills={"dirs": []}))
    for relative, raw in user_files.items():
        assert (directory / relative).read_bytes() == raw
    assert (directory / "AGENTS.md").read_text().startswith("# My Codex notes\n\nKeep this.\n\n")


def test_unmarked_file_at_an_owned_path_is_a_conflict(world):
    path = world.dirs["claude"] / "tongs-agents" / "instructions.md"
    path.parent.mkdir(parents=True)
    path.write_text("user instructions\n")
    plan = EMITTERS["claude"].plan_full(load(world), target(world, "claude"))
    assert [change.kind for change in plan.changes if change.path == path] == ["conflict"]
    assert any("without the tongs-agents marker" in item for item in plan.conflicts)
    with pytest.raises(owned.OwnershipError):
        apply_plan(plan, runner=world.cli)
    assert path.read_text() == "user instructions\n"
    assert world.cli.calls == []


def test_user_claude_md_keeps_its_text_and_gains_one_import(world):
    path = world.dirs["claude"] / "CLAUDE.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Mine\nNo trailing newline")
    emit(world, "claude", load(world))
    assert path.read_text() == (
        "# Mine\nNo trailing newline\n\n@tongs-agents/instructions.md\n")
    assert owned.backup_path(path).read_text() == "# Mine\nNo trailing newline"
    emit(world, "claude", load(world, instructions={}))
    assert path.read_text() == "# Mine\nNo trailing newline\n\n"
    assert not (world.dirs["claude"] / "tongs-agents" / "instructions.md").exists()


def test_claude_md_we_created_is_removed_with_the_instructions(world):
    emit(world, "claude", load(world))
    emit(world, "claude", load(world, instructions={}))
    assert not (world.dirs["claude"] / "CLAUDE.md").exists()


def test_malformed_block_markers_are_a_conflict(world):
    path = world.dirs["pi"] / "AGENTS.md"
    path.parent.mkdir(parents=True)
    path.write_text("<!-- tongs-agents:begin -->\nhalf a block\n")
    plan = EMITTERS["pi"].plan_full(load(world), target(world, "pi"))
    assert any("unmatched or repeated" in item for item in plan.conflicts)
    assert path not in [change.path for change in plan.changes]


def test_foreign_same_name_mcp_servers_are_kept(world):
    claude_json = world.dirs["claude"] / ".claude.json"
    claude_json.parent.mkdir(parents=True)
    foreign = {"type": "stdio", "command": "someone-else", "args": [], "env": {}}
    claude_json.write_text(json.dumps({"mcpServers": {"issues": foreign}}))
    plan = emit(world, "claude", load(world))
    assert json.loads(claude_json.read_text())["mcpServers"]["issues"] == foreign
    assert any("issues" in item and "not written by tongs-agents" in item
               for item in plan.conflicts)
    assert "mcp:issues" not in owned.manifest_entries_for(target(world, "claude"))

    config_toml = world.dirs["codex"] / "config.toml"
    config_toml.parent.mkdir(parents=True)
    user_text = '# mine\n[mcp_servers.issues]\ncommand = "theirs"  # keep\n'
    config_toml.write_text(user_text)
    plan = emit(world, "codex", load(world))
    assert config_toml.read_text().startswith(user_text)
    assert tomllib.loads(config_toml.read_text())["mcp_servers"]["issues"] == {
        "command": "theirs"}
    assert any("issues" in item for item in plan.conflicts)

    mcp_json = world.dirs["pi"] / "mcp.json"
    mcp_json.parent.mkdir(parents=True)
    mcp_json.write_text(json.dumps({"mcpServers": {"notes": {"command": "theirs"}}}))
    world.cli.calls.clear()
    plan = emit(world, "pi", load(world))
    assert json.loads(mcp_json.read_text())["mcpServers"]["notes"] == {"command": "theirs"}
    assert not any(argv[2:3] == ["notes"] for _, argv, _ in world.cli.calls)


def test_codex_user_comments_in_mcp_tables_survive(world):
    path = world.dirs["codex"] / "config.toml"
    path.parent.mkdir(parents=True)
    user_text = (
        "# USER-COMMENT-ABOVE-MCP-TABLE\n[mcp_servers.user-server]\n"
        "# USER-COMMENT-INSIDE-MCP-TABLE\ncommand = \"u\"  # USER-INLINE\n"
    )
    path.write_text(user_text)
    emit(world, "codex", load(world))
    assert path.read_text().startswith(user_text)
    emit(world, "codex", load(world, mcp_servers={}))
    remaining = tomllib.loads(path.read_text())["mcp_servers"]
    assert set(remaining) == {"user-server", "tongs-agents"}
    assert path.read_text().startswith(user_text)


def test_codex_refuses_an_inline_definition_of_our_server(world):
    path = world.dirs["codex"] / "config.toml"
    path.parent.mkdir(parents=True)
    text = (f'[mcp_servers]\n"tongs-agents" = {{ command = "{sys.executable}", '
            'args = ["-m", "tongs_agents.mcp_server"] }\n')
    path.write_text(text)
    plan = EMITTERS["codex"].plan_full(load(world), target(world, "codex"))
    assert any("inline or with dotted keys" in item for item in plan.conflicts)
    assert path not in [change.path for change in plan.changes]
    assert not any(key.startswith("toml-table:") for key in plan.entries)


# Removal through the manifest and drift


def test_removed_items_are_deleted_only_through_the_manifest(world):
    emit(world, "codex", load(world))
    directory = world.dirs["codex"]
    stray = directory / "skills" / "tongs-agents" / "pr-review" / "notes.md"
    stray.write_text("user file inside our skill dir\n")
    emit(world, "codex", load(world, skills={}, instructions={}, mcp_servers={}))
    remaining = set(tree(directory))
    assert "skills/tongs-agents/pr-review/notes.md" in remaining
    assert not any(name.endswith("SKILL.md") for name in remaining)
    assert not (directory / "AGENTS.md").exists()
    tables = tomllib.loads((directory / "config.toml").read_text())["mcp_servers"]
    assert set(tables) == {"tongs-agents"}
    entries = owned.manifest_entries_for(target(world, "codex"))
    assert set(entries) == {"toml-table:tongs-agents"}


def test_claude_and_pi_servers_are_removed_through_the_cli(world):
    emit(world, "claude", load(world))
    emit(world, "pi", load(world))
    world.cli.calls.clear()
    emit(world, "claude", load(world, mcp_servers={}))
    emit(world, "pi", load(world, mcp_servers={}))
    assert sorted((name, argv[:3]) for name, argv, _ in world.cli.calls) == [
        ("claude", ["mcp", "remove", "--scope"]),
        ("pi", ["mcp", "remove", "issues"]),
        ("pi", ["mcp", "remove", "notes"]),
    ]
    claude_servers = json.loads((world.dirs["claude"] / ".claude.json").read_text())
    assert set(claude_servers["mcpServers"]) == {"tongs-agents"}
    entries = owned.manifest_entries_for(target(world, "pi"))
    assert {key for key in entries if key.startswith("mcp:")} == {"mcp:tongs-agents"}


def test_unmanaged_server_removal_needs_a_manifest_record(world):
    mcp_json = world.dirs["pi"] / "mcp.json"
    mcp_json.parent.mkdir(parents=True)
    mcp_json.write_text(json.dumps({"mcpServers": {"mine": {"command": "x"}}}))
    emit(world, "pi", load(world, mcp_servers={}))
    assert "mine" in json.loads(mcp_json.read_text())["mcpServers"]


def test_drifted_owned_file_is_left_unless_forced(world):
    emit(world, "claude", load(world))
    path = world.dirs["claude"] / "tongs-agents" / "instructions.md"
    edited = path.read_text() + "user edit\n"
    path.write_text(edited)
    root = copy_fixture(world.tmp)
    (root / "instructions" / "common.md").write_text("# New text\n")
    plan = emit(world, "claude", load(world, root=root))
    assert path.read_text() == edited
    assert any("changed since tongs-agents wrote it" in note for note in plan.notes)
    emit(world, "claude", load(world, root=root), force=True)
    assert path.read_text().endswith("# New text\n")


def test_drifted_server_is_left_unless_forced(world):
    emit(world, "codex", load(world))
    path = world.dirs["codex"] / "config.toml"
    edited = path.read_text().replace('args = ["--stdio"]', 'args = ["--stdio", "--debug"]')
    path.write_text(edited)
    root = copy_fixture(world.tmp)
    toml_path = root / "agentic.toml"
    toml_path.write_text(toml_path.read_text().replace('"--stdio"]', '"--stdio", "-v"]'))
    plan = emit(world, "codex", load(world, root=root))
    assert path.read_text() == edited
    assert any("issues" in note and "changed since" in note for note in plan.notes)
    emit(world, "codex", load(world, root=root), force=True)
    issues = tomllib.loads(path.read_text())["mcp_servers"]["issues"]
    assert issues["args"] == ["--stdio", "-v"]


def test_disabled_codex_server_stays_disabled_when_updated(world):
    emit(world, "codex", load(world))
    path = world.dirs["codex"] / "config.toml"
    text = path.read_text().replace(
        'env_vars = ["EXAMPLE_ISSUES_TOKEN"]', 'env_vars = ["EXAMPLE_ISSUES_TOKEN"]\nenabled = false')
    path.write_text(text)
    root = copy_fixture(world.tmp)
    toml_path = root / "agentic.toml"
    toml_path.write_text(toml_path.read_text().replace('"--stdio"]', '"--stdio", "-v"]'))
    emit(world, "codex", load(world, root=root))
    issues = tomllib.loads(path.read_text())["mcp_servers"]["issues"]
    assert issues["enabled"] is False and issues["args"] == ["--stdio", "-v"]


def test_m1_entries_are_adopted(world):
    claude_json = world.dirs["claude"] / ".claude.json"
    claude_json.parent.mkdir(parents=True)
    claude_json.write_text(json.dumps({"mcpServers": {"tongs-agents": {
        "type": "stdio", "command": "/old/venv/bin/python3", "args": MCP_ARGS, "env": {}}}}))
    plan = emit(world, "claude", load(world, mcp_servers={}))
    assert any("adopted" in note for note in plan.notes)
    entry = json.loads(claude_json.read_text())["mcpServers"]["tongs-agents"]
    assert entry["command"] == sys.executable

    config_toml = world.dirs["codex"] / "config.toml"
    config_toml.parent.mkdir(parents=True)
    config_toml.write_text(
        f'[mcp_servers.tongs-agents]\ncommand = "{sys.executable}"\n'
        'args = ["-m", "tongs_agents.mcp_server"]\n')
    plan = emit(world, "codex", load(world, mcp_servers={}))
    assert any("adopted" in note for note in plan.notes)
    assert tomllib.loads(config_toml.read_text())["mcp_servers"]["tongs-agents"][
        "env_vars"] == ["XDG_STATE_HOME", "TONGS_AGENTS_STATE_DIR"]


def test_a_failed_cli_step_keeps_the_previous_record(world):
    plan = EMITTERS["pi"].plan_full(load(world), target(world, "pi"))

    def failing(argv, **options):
        return SimpleNamespace(returncode=1, stderr="boom\n")

    result = apply_plan(plan, runner=failing)
    assert [detail for _, detail in result.failed_steps] == ["boom"] * 3
    entries = owned.manifest_entries_for(target(world, "pi"))
    assert not any(key.startswith("mcp:") for key in entries)
    retry = EMITTERS["pi"].plan_full(load(world), target(world, "pi"))
    assert len(retry.commands) == 3


# Narrowing and validation


def test_profile_narrowing_selects_targets(world):
    root = copy_fixture(world.tmp)
    toml_path = root / "agentic.toml"
    toml_path.write_text(toml_path.read_text().replace(
        'harnesses = ["codex", "pi"]', 'profiles = ["pi-local"]'))
    emit(world, "codex", load(world, root=root))
    tables = tomllib.loads((world.dirs["codex"] / "config.toml").read_text())["mcp_servers"]
    assert "notes" not in tables
    default_codex = EMITTERS["codex"].plan_full(load(world, root=root), ProfileTarget("codex"))
    paths = [change.path for change in default_codex.changes]
    assert all(str(path).startswith(str(world.home / ".codex")) for path in paths)


@pytest.mark.parametrize(("key", "value", "message"), [
    ("mcp_servers", {"x": {"command": ["a"], "env": {"A": "b"}}}, "values belong"),
    ("mcp_servers", {"tongs-agents": {"command": ["a"]}}, "reserved"),
    ("mcp_servers", {"x": {"command": ["a"], "url": "https://h"}}, "exactly one"),
    ("mcp_servers", {"x": {"url": "http://example.invalid"}}, "https"),
    ("mcp_servers", {"x": {"command": ["a"], "env": ["lower"]}}, "variable name"),
    ("instructions", {"files": ["../outside.md"]}, "inside the dotfiles"),
    ("instructions", {"files": ["/etc/hostname"]}, "inside the dotfiles"),
    ("skills", {"dirs": ["skills"], "overrides": {"nope": {}}}, "no such skill"),
    ("mcp_servers", {"x": {"command": ["a"], "profiles": ["ghost"]}}, "unknown profile"),
])
def test_invalid_canonical_config_is_refused(world, key, value, message):
    with pytest.raises(EmitError, match=message):
        load(world, **{key: value})


def test_skill_contents_are_checked(world, tmp_path):
    root = copy_fixture(tmp_path)
    skill = root / "skills" / "release-notes"
    (skill / "data.bin").write_bytes(b"\x00\x01")
    with pytest.raises(EmitError, match="no owner marker format"):
        load(world, root=root)
    (skill / "data.bin").unlink()
    (skill / "link.md").symlink_to(skill / "template.md")
    with pytest.raises(EmitError, match="symlink"):
        load(world, root=root)
    (skill / "link.md").unlink()
    (skill / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\n")
    with pytest.raises(EmitError, match="name must be 'release-notes'"):
        load(world, root=root)


# Capture


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_capture_reads_back_canonical_terms(world, harness):
    emit(world, harness, load(world))
    fragment = EMITTERS[harness].capture(target(world, harness))
    assert isinstance(fragment, CanonicalFragment)
    data = fragment.data
    assert "release-notes" in data["skills"]
    skill = data["skills"]["release-notes"]
    assert skill["SKILL.md"] == (FIXTURE / "skills" / "release-notes" / "SKILL.md").read_text()
    assert skill["scripts/render.py"] == (
        FIXTURE / "skills" / "release-notes" / "scripts" / "render.py").read_text()
    assert "Prefer small, reviewed changes." in data["instructions"]
    assert data["mcp_servers"]["issues"]["command"] == ["example-issues-mcp", "--stdio"]
    assert "tongs-agents" not in data["mcp_servers"]
    if harness == "codex":
        assert data["mcp_servers"]["issues"]["env"] == ["EXAMPLE_ISSUES_TOKEN"]


def test_capture_skips_literal_env_values_and_unmanaged_servers(world):
    emit(world, "pi", load(world))
    path = world.dirs["pi"] / "mcp.json"
    data = json.loads(path.read_text())
    data["mcpServers"]["issues"]["env"] = {"EXAMPLE_ISSUES_TOKEN": SECRET,
                                           "OTHER": "${OTHER}"}
    data["mcpServers"]["mine"] = {"command": "mine"}
    path.write_text(json.dumps(data))
    fragment = EMITTERS["pi"].capture(target(world, "pi"))
    assert fragment.data["mcp_servers"]["issues"]["env"] == ["OTHER"]
    assert "mine" not in fragment.data["mcp_servers"]
    assert any("EXAMPLE_ISSUES_TOKEN" in item for item in fragment.data["warnings"])
    assert SECRET not in json.dumps(dict(fragment.data))
    everything = EMITTERS["pi"].capture(target(world, "pi"), include_unmanaged=True)
    assert "mine" in everything.data["mcp_servers"]


# owned.py additions used by the emitters


def test_text_edit_keeps_mode_and_backup(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("user\n")
    path.chmod(0o644)
    changes = owned.ChangeSet()
    change = changes.edit_text_entries(path, lambda text: text + "ours\n")
    assert change.kind == "edit_entry"
    changes.commit()
    assert path.read_text() == "user\nours\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert owned.backup_path(path).read_text() == "user\n"
    assert owned.plan_text_edit(path, lambda text: text).kind == "unchanged"
    assert owned.plan_text_edit(tmp_path / "absent.md", lambda text: text).kind == "unchanged"
    assert owned.plan_text_edit(path, lambda text: None).kind == "delete"


def test_text_edit_refuses_non_utf8(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(owned.OwnedFileError, match="UTF-8"):
        owned.plan_text_edit(path, lambda text: text)


def test_manifest_entries_round_trip_and_keep_paths(tmp_path):
    first = ProfileTarget("codex", tmp_path / "a")
    changes = owned.ChangeSet()
    changes.record_managed_paths(first, [tmp_path / "a" / "x"])
    changes.record_entries(first, {"mcp:x": {"kind": "mcp-cli", "sha256": "0" * 64}})
    changes.commit()
    assert owned.manifest_entries_for(first) == {
        "mcp:x": {"kind": "mcp-cli", "sha256": "0" * 64}}
    assert owned.managed_paths_for(first) == (tmp_path / "a" / "x",)
    changes = owned.ChangeSet()
    changes.record_entries(first, {})
    changes.commit()
    assert owned.manifest_entries_for(first) == {}
    assert owned.managed_paths_for(first) == (tmp_path / "a" / "x",)
    with pytest.raises(ValueError, match="kind and sha256"):
        owned.ChangeSet().record_entries(first, {"x": {"kind": "a"}})


@pytest.mark.parametrize(("fmt", "body", "lines"), [
    ("hash", "#!/bin/sh\necho hi\n", ["#!/bin/sh", "# tongs-agents-owned", "echo hi"]),
    ("hash", "key: value\n", ["# tongs-agents-owned", "key: value"]),
    ("slash", "#!/usr/bin/env node\nx()\n", ["#!/usr/bin/env node", "// tongs-agents-owned",
                                              "x()"]),
])
def test_script_formats_keep_the_shebang_first(tmp_path, fmt, body, lines):
    path = tmp_path / "file"
    owned.write_owned_file(path, body, owner="tongs-agents", schema_version=1, fmt=fmt)
    text = path.read_text().splitlines()
    assert [line.split(" {")[0] for line in text] == lines
    assert owned.read_owned_file(path, owner="tongs-agents", fmt=fmt).content == body


def test_toml_digest_matches_the_emitter_digest():
    value = {"command": "x", "args": ["1"]}
    assert toml_tables.table_digest(value) == _common.digest_value(value)


def test_modules_are_the_ones_registered():
    assert isinstance(EMITTERS["claude"], claude.ClaudeEmitter)
    assert isinstance(EMITTERS["codex"], codex.CodexEmitter)
    assert isinstance(EMITTERS["pi"], pi.PiEmitter)
    assert os.path.basename(_common.__file__) == "_common.py"


# Pi pins: the lifecycle pin stays, a second pin sits at the native MCP floor


PI_PINS = Path(__file__).resolve().parents[1] / "src" / "tongs_agents" / "data" / "pi_pins.json"
PI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "pi.yml"


def _version(text):
    return tuple(int(part) for part in text.split("."))


def test_pi_pins_keep_the_lifecycle_pin_and_add_one_at_the_mcp_floor():
    table = json.loads(PI_PINS.read_text())
    assert table["package"] == "@earendil-works/pi-coding-agent"
    assert _version(table["mcp_floor"]) == pi.MCP_MINIMUM
    pins = table["pins"]
    assert pins["0.85.0"]["gate"] == "pi-validation"
    mcp_pins = [version for version, entry in pins.items() if entry["gate"] == "pi-mcp-emitter"]
    assert len(mcp_pins) == 1 and _version(mcp_pins[0]) >= pi.MCP_MINIMUM
    for version, entry in pins.items():
        assert entry["url"] == (
            "https://registry.npmjs.org/@earendil-works/pi-coding-agent/-/"
            f"pi-coding-agent-{version}.tgz")
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
        assert entry["integrity"].startswith("sha512-") and entry["size"] > 0


def test_pi_workflow_installs_the_mcp_pin_through_the_verifying_helper():
    text = PI_WORKFLOW.read_text()
    pins = json.loads(PI_PINS.read_text())["pins"]
    for version, entry in pins.items():
        if entry["gate"] == "pi-mcp-emitter":
            assert f"validation/pi/install_pin.py {version} " in text
            assert f"pi-coding-agent@{version};" in text
    assert "python validation/pi_mcp.py" in text


def test_pi_workflow_keeps_the_lifecycle_install_unchanged():
    # The lifecycle job runs a Pi session, which cannot be reproduced
    # locally, so its install step stays exactly as it was validated.
    text = PI_WORKFLOW.read_text()
    assert "validation/pi/install_pin.py 0.85.0" not in text
    assert ("npm install --global --prefix \"$pi_prefix\" \\\n"
            "            '@earendil-works/pi-coding-agent@0.85.0'") in text


def _install_pin_module():
    path = PI_WORKFLOW.parents[2] / "validation" / "pi" / "install_pin.py"
    spec = importlib.util.spec_from_file_location("install_pin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_pin_refuses_a_tarball_that_does_not_match(tmp_path):
    module = _install_pin_module()
    archive = tmp_path / "pi.tgz"
    archive.write_bytes(b"synthetic")
    good = {"size": 9, "sha256": hashlib.sha256(b"synthetic").hexdigest()}
    assert module.verify(archive, good) == good["sha256"]
    with pytest.raises(SystemExit, match="size"):
        module.verify(archive, {**good, "size": 10})
    with pytest.raises(SystemExit, match="sha256"):
        module.verify(archive, {**good, "sha256": "0" * 64})
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"package": "p", "pins": {"1.0.0": {
        "url": "https://example.invalid/p.tgz"}}}))
    with pytest.raises(SystemExit, match="outside"):
        module.load_pin("1.0.0", pins)
    with pytest.raises(SystemExit, match="not in"):
        module.load_pin("2.0.0", pins)
