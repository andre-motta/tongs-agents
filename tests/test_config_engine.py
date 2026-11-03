"""config_layer schema loader and engine, driven by fake emitters.

Synthetic: every dotfiles directory and harness home lives under tmp_path;
the emitters below are fakes that plan through owned.py like the real ones.
"""

import json
import os
import sys
import types
from pathlib import Path

import pytest

from tongs_agents import cli, owned
from tongs_agents.config_layer import engine, schema
from tongs_agents.config_layer.protocol import CanonicalFragment, Change, ProfileTarget

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")

AGENTIC = """\
schema = "tongs-agents-dotfiles/1"

[profiles.claude-alt]
command = ["claude"]
config_dir = "{alt}"

[profiles.codex-review]
command = ["codex", "--profile", "review"]

[instructions]
files = ["instructions/common.md"]

[instructions.harness.codex]
files = ["instructions/codex.md"]

[skills]
dirs = ["skills"]

[skills.overrides.pr-review]
harnesses = ["claude", "codex"]

[mcp_servers.issues]
command = ["example-issues-mcp", "--stdio"]
env = ["EXAMPLE_ISSUES_TOKEN"]

[mcp_servers.notes]
command = ["~/bin/example-notes-mcp"]
cwd = "~/notes"
profiles = ["claude-alt"]

[mcp_servers.docs]
url = "https://mcp.example.invalid/mcp"
bearer_token_env = "EXAMPLE_DOCS_TOKEN"

[layouts.review]
profiles = ["claude-alt", "codex-review"]
tongs = false
"""

SKILL = """---
name: {name}
description: >
  Synthetic skill for tests.
---
Body.
"""


def write_dotfiles(root: Path, alt: Path, text: str = AGENTIC) -> Path:
    (root / "instructions").mkdir(parents=True, exist_ok=True)
    (root / "instructions" / "common.md").write_text("Be brief.\n")
    (root / "instructions" / "codex.md").write_text("Codex only.\n\n")
    for name in ("pr-review", "release-notes"):
        skill = root / "skills" / name
        skill.mkdir(parents=True, exist_ok=True)
        (skill / "SKILL.md").write_text(SKILL.format(name=name))
    (root / "skills" / "release-notes" / "template.md").write_text("template\n")
    (root / "agentic.toml").write_text(text.replace("{alt}", str(alt)))
    return root


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setattr(cli, "CLAUDE_DIR", home / ".claude")
    monkeypatch.setattr(cli, "SETTINGS_FILE", home / ".claude" / "settings.json")
    monkeypatch.setattr(cli, "CODEX_DIR", home / ".codex")
    monkeypatch.setattr(cli, "CODEX_HOOKS_FILE", home / ".codex" / "hooks.json")
    monkeypatch.setattr(cli, "CLAUDE_USER_CONFIG", home / ".claude.json")
    return types.SimpleNamespace(home=home, alt=tmp_path / "claude-alt")


@pytest.fixture
def config(tmp_path, homes):
    write_dotfiles(tmp_path / "dotfiles", homes.alt)
    return schema.load_canonical(tmp_path / "dotfiles", local_profiles={})


class FakeClaude:
    """Owned instructions file plus keyed mcpServers entries in settings.json."""

    harness_id = "claude"

    def __init__(self, default_home):
        self.default_home = default_home
        self.disabled_paths = set()
        self.captured = {}

    def home(self, target):
        return self.default_home if target.config_dir is None else target.config_dir

    def plan(self, config, target):
        home = self.home(target)
        changes = []
        text = config.instructions_for("claude")
        if text:
            changes.append(owned.plan_owned_file(
                home / "tongs-agents" / "instructions.md", text,
                owner=owned.OWNER, schema_version=1,
            ))
        servers = {
            server.name: server.to_table() for server in config.mcp_servers_for_target(target)
        }

        def mutate(document):
            document.setdefault("mcpServers", {}).update(servers)

        changes.append(owned.plan_json_edit(home / "settings.json", mutate))
        return changes

    def entry_values(self, target, path, data):
        if path.name != "settings.json":
            return None
        document = {} if data is None else json.loads(data)
        return document.get("mcpServers")

    def disabled(self, target, path, data):
        return path in self.disabled_paths

    def capture(self, target):
        return CanonicalFragment("claude", target, self.captured.get(target.config_dir, {}))


class FakeCodex:
    """One owned TOML file per target."""

    harness_id = "codex"

    def __init__(self, default_home):
        self.default_home = default_home

    def plan(self, config, target):
        home = self.default_home if target.config_dir is None else target.config_dir
        lines = []
        for server in config.mcp_servers_for_target(target):
            if server.command is None:
                continue
            lines += [f"[mcp_servers.{server.name}]",
                      "command = " + json.dumps(server.command[0]), ""]
        return [owned.plan_owned_file(
            home / "tongs-agents.toml", "\n".join(lines), owner=owned.OWNER, schema_version=1,
        )]

    def capture(self, target):
        return CanonicalFragment("codex", target, {})


@pytest.fixture
def fakes(homes):
    claude = FakeClaude(homes.home / ".claude")
    codex = FakeCodex(homes.home / ".codex")
    return types.SimpleNamespace(
        claude=claude, codex=codex,
        registry=engine.Registry({"claude": claude, "codex": codex}),
    )


def snapshot(*roots):
    files = {}
    for root in roots:
        for directory, _, names in os.walk(root):
            for name in names:
                path = Path(directory) / name
                info = path.stat()
                files[str(path)] = (path.read_bytes(), info.st_mtime_ns, info.st_mode)
    return files


def states(plan):
    return {(entry.path.name, str(entry.target.target.config_dir)): entry.state
            for entry in plan.entries}


def by_name(plan, name, home=None):
    found = [entry for entry in plan.entries if entry.path.name == name
             and (home is None or entry.path.is_relative_to(home))]
    assert len(found) == 1, found
    return found[0]


# Schema


def test_load_example(config, homes):
    assert config.source.name == "agentic.toml"
    assert set(config.profiles) == {"claude-alt", "codex-review"}
    assert config.instructions_for("claude") == "Be brief.\n"
    assert config.instructions_for("codex") == "Be brief.\n\nCodex only.\n"
    assert [skill.name for skill in config.skills_for("pi")] == ["release-notes"]
    assert config.skills["release-notes"].files == ("SKILL.md", "template.md")
    assert config.mcp_servers["issues"].env == ("EXAMPLE_ISSUES_TOKEN",)
    assert config.mcp_servers["docs"].bearer_token_env == "EXAMPLE_DOCS_TOKEN"
    assert config.layouts["review"].session == "review"
    alt = ProfileTarget("claude", homes.alt)
    assert config.profiles_for(alt) == ("claude-alt",)
    assert [s.name for s in config.mcp_servers_for_target(alt)] == ["issues", "notes", "docs"]
    default = ProfileTarget("claude", None)
    assert [s.name for s in config.mcp_servers_for_target(default)] == ["issues", "docs"]
    assert config.profiles_for(ProfileTarget("codex", None)) == ("codex-review",)


def _load(tmp_path, homes, text):
    root = write_dotfiles(tmp_path / "bad", homes.alt, text)
    return schema.load_canonical(root, local_profiles={})


@pytest.mark.parametrize(("change", "message"), [
    (('schema = "tongs-agents-dotfiles/1"', 'schema = "tongs-agents-dotfiles/2"'),
     "is not supported"),
    (("[instructions]\n", "colour = 1\n[instructions]\n"), "colour: unknown key"),
    (('env = ["EXAMPLE_ISSUES_TOKEN"]', 'env = { EXAMPLE_ISSUES_TOKEN = "x" }'),
     "values belong in the agent's environment"),
    (('env = ["EXAMPLE_ISSUES_TOKEN"]', 'env = ["lower"]'), r"env\[0\]"),
    (("[mcp_servers.issues]", "[mcp_servers.tongs-agents]"), "reserved"),
    (('cwd = "~/notes"', 'url = "https://x.invalid"'), "exactly one of command"),
    (('"https://mcp.example.invalid/mcp"', '"http://mcp.example.invalid/mcp"'), "https://"),
    (('files = ["instructions/common.md"]', 'files = ["../outside.md"]'), "without '..'"),
    (('files = ["instructions/common.md"]', 'files = ["/etc/hostname"]'), "relative"),
    (('files = ["instructions/common.md"]', 'files = ["instructions/none.md"]'),
     "does not exist"),
    (('profiles = ["claude-alt"]\n', 'profiles = ["nobody"]\n'), "unknown profile 'nobody'"),
    (('profiles = ["claude-alt", "codex-review"]', 'profiles = ["ghost"]'),
     "layouts.review.profiles: unknown profile"),
    (("[skills.overrides.pr-review]", "[skills.overrides.missing]"), "no skill named"),
    (('harnesses = ["claude", "codex"]', 'harnesses = ["vim"]'), "is not one of"),
    (('command = ["~/bin/example-notes-mcp"]', 'command = ["bin/notes"]'), "absolute"),
])
def test_schema_errors_name_the_key(tmp_path, homes, change, message):
    old, new = change
    assert old in AGENTIC
    with pytest.raises(schema.SchemaError, match=message):
        _load(tmp_path, homes, AGENTIC.replace(old, new, 1))


def test_http_allowed_for_localhost(tmp_path, homes):
    text = AGENTIC.replace('"https://mcp.example.invalid/mcp"', '"http://[::1]:8080/mcp"')
    assert _load(tmp_path, homes, text).mcp_servers["docs"].url == "http://[::1]:8080/mcp"


def test_missing_schema_key(tmp_path, homes):
    with pytest.raises(schema.SchemaError, match="schema: missing"):
        _load(tmp_path, homes, AGENTIC.replace('schema = "tongs-agents-dotfiles/1"\n', ""))


def test_skill_name_must_match_frontmatter(tmp_path, homes):
    root = write_dotfiles(tmp_path / "d", homes.alt)
    (root / "skills" / "pr-review" / "SKILL.md").write_text(SKILL.format(name="other"))
    with pytest.raises(schema.SchemaError, match="frontmatter name must equal 'pr-review'"):
        schema.load_canonical(root, local_profiles={})


def test_skill_needs_description(tmp_path, homes):
    root = write_dotfiles(tmp_path / "d", homes.alt)
    (root / "skills" / "pr-review" / "SKILL.md").write_text("---\nname: pr-review\n---\n")
    with pytest.raises(schema.SchemaError, match="non-empty description"):
        schema.load_canonical(root, local_profiles={})


@POSIX
def test_symlinks_are_refused(tmp_path, homes):
    root = write_dotfiles(tmp_path / "d", homes.alt)
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    (root / "skills" / "pr-review" / "link.md").symlink_to(outside)
    with pytest.raises(schema.SchemaError, match="is a symlink"):
        schema.load_canonical(root, local_profiles={})
    (root / "skills" / "pr-review" / "link.md").unlink()
    (root / "instructions" / "escape.md").symlink_to(outside)
    text = AGENTIC.replace("instructions/common.md", "instructions/escape.md")
    (root / "agentic.toml").write_text(text.replace("{alt}", str(homes.alt)))
    with pytest.raises(schema.SchemaError, match="leaves the dotfiles directory"):
        schema.load_canonical(root, local_profiles={})


def test_source_from_config_file_and_local_profile_override(tmp_path, homes):
    from tongs_agents import paths
    root = write_dotfiles(tmp_path / "dotfiles", homes.alt)
    config_file = paths.config_dir(create=True) / "config.toml"
    config_file.write_text(
        f'[dotfiles]\npath = "{root}"\n\n'
        '[profiles.codex-review]\ncommand = ["codex"]\nconfig_dir = "/synthetic/codex"\n'
    )
    loaded = schema.load_canonical()
    assert loaded.root == root
    assert loaded.profiles["codex-review"].config_dir == Path("/synthetic/codex")
    assert loaded.profiles["claude-alt"].config_dir == homes.alt


def test_no_source_is_reported(homes):
    with pytest.raises(schema.NoSourceError, match="--source"):
        schema.load_canonical()


# Registry and targets


def test_missing_registry_means_no_emitters(monkeypatch):
    def missing(name):
        raise ModuleNotFoundError(f"No module named {name!r}", name=name)

    monkeypatch.setattr(engine.importlib, "import_module", missing)
    registry = engine.load_emitters()
    assert registry.emitters == {} and registry.problem is None


def test_broken_registry_is_a_problem(monkeypatch):
    def broken(name):
        raise ImportError("cannot import name 'x'")

    monkeypatch.setattr(engine.importlib, "import_module", broken)
    registry = engine.load_emitters()
    assert registry.emitters == {} and "cannot load the emitters" in registry.problem


@pytest.mark.parametrize("shape", [tuple, lambda items: {e.harness_id: e for e in items}])
def test_registry_reads_emitters(monkeypatch, fakes, shape):
    module = types.ModuleType(engine.EMITTERS_MODULE)
    module.EMITTERS = shape([fakes.claude, fakes.codex])
    monkeypatch.setitem(sys.modules, engine.EMITTERS_MODULE, module)
    assert engine.load_emitters().emitters == {"claude": fakes.claude, "codex": fakes.codex}
    module.EMITTERS = (fakes.claude, fakes.claude)
    with pytest.raises(engine.EngineError, match="two emitters"):
        engine.load_emitters()


def test_resolve_targets_merges_defaults_installs_and_profiles(config, homes):
    cli.save_installs({"claude": [str(homes.alt)], "codex": ["/synthetic/codex-two"], "pi": []})
    targets = {item.describe(): item for item in engine.resolve_targets(config.profiles)}
    assert set(targets) == {
        "claude (default)", "codex (default)", f"claude ({homes.alt})",
        "codex (/synthetic/codex-two)",
    }
    assert targets[f"claude ({homes.alt})"].profiles == ("claude-alt",)
    assert targets[f"claude ({homes.alt})"].sources == ("recorded", "profile claude-alt")
    assert targets["codex (default)"].profiles == ("codex-review",)


# Plan and apply


def test_first_apply_then_second_apply_changes_zero_bytes(config, fakes, homes):
    first = engine.plan(config, registry=fakes.registry)
    assert not first.problems
    assert set(states(first).values()) == {engine.PENDING}
    result = engine.apply(first)
    assert result.written and not result.skipped
    alt_instructions = homes.alt / "tongs-agents" / "instructions.md"
    assert owned.read_owned_file(alt_instructions, owner=owned.OWNER).content == "Be brief.\n"
    settings = json.loads((homes.alt / "settings.json").read_text())
    assert set(settings["mcpServers"]) == {"issues", "notes", "docs"}
    assert "EXAMPLE_ISSUES_TOKEN" in json.dumps(settings) and "x-secret" not in json.dumps(settings)
    ledger = engine.load_ledger()
    assert set(ledger) == {"claude:default", f"claude:{homes.alt}", "codex:default"}
    assert set(owned.managed_paths_for(ProfileTarget("claude", homes.alt))) == {
        alt_instructions, homes.alt / "settings.json",
    }

    before = snapshot(homes.home, homes.alt, engine.ledger_path().parent)
    second = engine.plan(config, registry=fakes.registry)
    assert second.clean and set(states(second).values()) == {engine.IN_SYNC}
    assert engine.apply(second).written == ()
    assert snapshot(homes.home, homes.alt, engine.ledger_path().parent) == before


def test_manual_edit_is_drift_and_apply_skips_only_that_entry(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    path = homes.alt / "tongs-agents" / "instructions.md"
    edited = path.read_bytes() + b"my own line\n"
    path.write_bytes(edited)
    (config.root / "instructions" / "common.md").write_text("Be very brief.\n")
    config = schema.load_canonical(config.root, local_profiles={})

    result = engine.plan(config, registry=fakes.registry)
    assert not result.clean
    assert by_name(result, "instructions.md", homes.alt).state == engine.DRIFTED
    assert by_name(result, "instructions.md", homes.home).state == engine.PENDING
    applied = engine.apply(result)
    assert [entry.path for entry in applied.skipped] == [path]
    assert path.read_bytes() == edited
    default = homes.home / ".claude" / "tongs-agents" / "instructions.md"
    assert "Be very brief." in default.read_text()

    again = engine.plan(config, registry=fakes.registry)
    assert by_name(again, "instructions.md", homes.alt).state == engine.DRIFTED

    forced = engine.plan(config, registry=fakes.registry, force=True)
    engine.apply(forced)
    assert "Be very brief." in path.read_text()
    backup = path.parent / ("instructions.md" + engine.DRIFT_BACKUP_SUFFIX)
    assert backup.read_bytes() == edited
    assert engine.plan(config, registry=fakes.registry).clean

    second = path.read_bytes() + b"a second edit\n"
    path.write_bytes(second)
    engine.apply(engine.plan(config, registry=fakes.registry, force=True))
    assert backup.read_bytes() == edited
    assert backup.with_name(backup.name + ".1").read_bytes() == second


def test_unmarked_target_refuses_the_whole_apply(config, fakes, homes):
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.parent.mkdir(parents=True)
    path.write_text("the user's own file\n")
    result = engine.plan(config, registry=fakes.registry)
    entry = by_name(result, "instructions.md", homes.alt)
    assert entry.state == engine.FOREIGN and entry.change is None
    assert not result.clean
    with pytest.raises(engine.ConflictError, match="nothing was written"):
        engine.apply(result)
    assert path.read_text() == "the user's own file\n"
    assert not (homes.alt / "settings.json").exists()
    assert not (homes.home / ".claude").exists()
    assert not engine.ledger_path().exists() and not owned.manifest_path().exists()


def test_injected_failure_rolls_everything_back(config, fakes, homes, monkeypatch):
    engine.apply(engine.plan(config, registry=fakes.registry))
    (config.root / "instructions" / "common.md").write_text("Changed.\n")
    config = schema.load_canonical(config.root, local_profiles={})
    roots = (homes.home, homes.alt, engine.ledger_path().parent)
    before = {path: data for path, (data, _, _) in snapshot(*roots).items()}

    real = owned._replace
    calls = []

    def replace(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError("injected failure")
        real(source, destination)

    monkeypatch.setattr(owned, "_replace", replace)
    with pytest.raises(owned.OwnedFileError, match="rolled back"):
        engine.apply(engine.plan(config, registry=fakes.registry))
    assert {path: data for path, (data, _, _) in snapshot(*roots).items()} == before


def test_matching_existing_file_is_adopted_without_writing_it(config, fakes, homes):
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(owned.render_owned(
        "md", "Be brief.\n", owner=owned.OWNER, schema_version=1,
    ))
    stamp = path.stat().st_mtime_ns
    result = engine.plan(config, registry=fakes.registry)
    assert by_name(result, "instructions.md", homes.alt).state == engine.ADOPTED
    engine.apply(result)
    assert path.stat().st_mtime_ns == stamp
    assert by_name(engine.plan(config, registry=fakes.registry), "instructions.md",
                   homes.alt).state == engine.IN_SYNC


def test_removed_file_is_missing_and_recreated(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    path = homes.alt / "tongs-agents" / "instructions.md"
    path.unlink()
    result = engine.plan(config, registry=fakes.registry)
    assert by_name(result, "instructions.md", homes.alt).state == engine.MISSING
    engine.apply(result)
    assert path.exists()


def test_orphaned_owned_file_is_removed_unless_edited(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    alt = homes.alt / "tongs-agents" / "instructions.md"
    default = homes.home / ".claude" / "tongs-agents" / "instructions.md"
    alt.write_bytes(alt.read_bytes() + b"edit\n")
    text = AGENTIC.replace('files = ["instructions/common.md"]', "files = []")
    (config.root / "agentic.toml").write_text(text.replace("{alt}", str(homes.alt)))
    (config.root / "instructions" / "codex.md").write_text("")
    config = schema.load_canonical(config.root, local_profiles={})

    result = engine.plan(config, registry=fakes.registry)
    assert by_name(result, "instructions.md", homes.alt).state == engine.ORPHANED
    assert by_name(result, "instructions.md", homes.home).state == engine.ORPHANED
    engine.apply(result)
    assert not default.exists() and alt.exists()
    assert str(default) not in engine.load_ledger()["claude:default"]["entries"]
    assert default not in owned.managed_paths_for(ProfileTarget("claude", None))


def test_shared_entries_drift_by_parsed_value_not_bytes(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    settings = homes.alt / "settings.json"
    document = json.loads(settings.read_text())
    document["theme"] = "user"
    settings.write_text(json.dumps(document))
    assert by_name(engine.plan(config, registry=fakes.registry), "settings.json",
                   homes.alt).state == engine.PENDING
    document["mcpServers"]["issues"]["command"] = ["edited"]
    settings.write_text(json.dumps(document))
    assert by_name(engine.plan(config, registry=fakes.registry), "settings.json",
                   homes.alt).state == engine.DRIFTED


def test_toml_drift_hashes_the_parsed_value(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    path = homes.home / ".codex" / "tongs-agents.toml"
    path.write_text(path.read_text() + "\n# a comment the user added\n\n")
    assert by_name(engine.plan(config, registry=fakes.registry), "tongs-agents.toml").state \
        == engine.PENDING
    path.write_text(path.read_text().replace("example-issues-mcp", "edited"))
    assert by_name(engine.plan(config, registry=fakes.registry), "tongs-agents.toml").state \
        == engine.DRIFTED


def test_toml_entry_values():
    data = b'[mcp_servers.a]\ncommand = "x" # note\n\n[other]\nk = 1\n'
    values = engine.toml_entry_values(data, ["mcp_servers.a", "mcp_servers.b"])
    assert values == {"mcp_servers.a": {"command": "x"}, "mcp_servers.b": None}
    reformatted = b'# top\n[mcp_servers.a]\ncommand="x"\n'
    assert engine.canonical_digest(engine.toml_entry_values(reformatted, ["mcp_servers.a"])) \
        == engine.canonical_digest({"mcp_servers.a": {"command": "x"}})


def test_disabled_entry_is_reported(config, fakes, homes):
    engine.apply(engine.plan(config, registry=fakes.registry))
    fakes.claude.disabled_paths.add(homes.alt / "settings.json")
    result = engine.plan(config, registry=fakes.registry)
    assert by_name(result, "settings.json", homes.alt).state == engine.DISABLED
    assert result.clean


def test_same_path_from_two_targets_is_deduped_or_refused(config, fakes, homes):
    shared = homes.home / "shared.md"

    class Twice(FakeCodex):
        def plan(self, config, target):
            text = "a\n" if target.config_dir is None else str(target.config_dir)
            return [owned.plan_owned_file(shared, text, owner=owned.OWNER, schema_version=1)]

    targets = [
        engine.ResolvedTarget(ProfileTarget("codex", None)),
        engine.ResolvedTarget(ProfileTarget("codex", None)),
    ]
    registry = engine.Registry({"codex": Twice(homes.home)})
    result = engine.plan(config, targets=targets, registry=registry)
    assert [entry.path for entry in result.entries] == [shared] and not result.problems
    targets.append(engine.ResolvedTarget(ProfileTarget("codex", homes.alt)))
    result = engine.plan(config, targets=targets, registry=registry)
    assert any("planned twice" in problem for problem in result.problems)
    with pytest.raises(engine.EngineError, match="planned twice"):
        engine.apply(result)


@POSIX
def test_emitter_error_is_a_problem_and_blocks_apply(config, fakes, homes):
    target = homes.alt / "tongs-agents" / "instructions.md"
    target.parent.mkdir(parents=True)
    target.symlink_to(homes.home)
    result = engine.plan(config, registry=fakes.registry)
    assert any("symlink" in problem for problem in result.problems)
    with pytest.raises(engine.EngineError):
        engine.apply(result)
    assert not engine.ledger_path().exists()


def test_invalid_change_is_a_problem(config, homes):
    class Bad:
        harness_id = "claude"

        def plan(self, config, target):
            return [Change(Path("relative.md"), "unchanged")]

    result = engine.plan(config, registry=engine.Registry({"claude": Bad()}))
    assert any("invalid change" in problem for problem in result.problems)


def test_unified_diff_shows_the_change(config, fakes, homes):
    result = engine.plan(config, registry=fakes.registry)
    entry = by_name(result, "instructions.md", homes.alt)
    diff = engine.unified_diff(entry)
    assert diff.startswith("--- /dev/null\n+++ b")
    assert "+Be brief.\n" in diff


# Capture


def test_capture_writes_a_loadable_dotfiles_dir_and_never_the_harness(tmp_path, config, fakes,
                                                                      homes):
    fakes.claude.captured[homes.alt] = {
        "instructions": "Captured.\n",
        "skills": {"pr-review": {"SKILL.md": SKILL.format(name="pr-review"),
                                 "nested/a.md": b"a\n", "../escape": "x"}},
        "mcp_servers": {
            "issues": {"command": ["example-issues-mcp"],
                       "env": {"EXAMPLE_TOKEN": "${EXAMPLE_TOKEN}", "LEAKED": "x-secret"}},
            "tongs-agents": {"command": ["python"]},
            "weird": {"command": ["x"], "transport": "stdio"},
        },
        "warnings": ["emitter note"],
    }
    harness_files = snapshot(homes.home, homes.alt)
    targets = engine.resolve_targets(config.profiles)
    captured = engine.capture("claude", targets, fakes.registry)
    assert captured.mcp_servers == {
        "issues": {"command": ["example-issues-mcp"], "env": ["EXAMPLE_TOKEN"]},
        "weird": {"command": ["x"]},
    }
    assert any("LEAKED: a literal value" in line for line in captured.warnings)
    assert any("transport" in line for line in captured.warnings)
    assert any("../escape" in line for line in captured.warnings)
    into = tmp_path / "captured"
    engine.write_capture("claude", captured, into)
    assert snapshot(homes.home, homes.alt) == harness_files
    assert "x-secret" not in (into / "agentic.toml").read_text()
    loaded = schema.load_canonical(into, local_profiles={})
    assert loaded.instructions_for("claude") == "Captured.\n"
    assert loaded.mcp_servers["issues"].env == ("EXAMPLE_TOKEN",)
    assert loaded.skills["pr-review"].files == ("SKILL.md", "nested/a.md")

    with pytest.raises(engine.EngineError, match="not empty"):
        engine.write_capture("claude", captured, into)
    engine.write_capture("claude", captured, into, force=True)


def test_capture_without_emitter(config):
    with pytest.raises(engine.EngineError, match="no emitter"):
        engine.capture("pi", [], engine.Registry({}))


def test_deduped_path_recorded_by_another_target_is_not_orphaned(config, homes):
    shared = homes.home / "shared.md"

    class Same(FakeCodex):
        def plan(self, config, target):
            return [owned.plan_owned_file(shared, "a\n", owner=owned.OWNER, schema_version=1)]

    registry = engine.Registry({"codex": Same(homes.home)})
    first = engine.ResolvedTarget(ProfileTarget("codex", None))
    second = engine.ResolvedTarget(ProfileTarget("codex", homes.alt))
    engine.apply(engine.plan(config, targets=[second], registry=registry))
    result = engine.plan(config, targets=[first, second], registry=registry)
    assert [entry.state for entry in result.entries] == [engine.ADOPTED]
    engine.apply(result)
    assert shared.exists()
