"""owned.py: owned files, shared JSON edits, ChangeSet rollback and the manifest.

Synthetic: every file lives under tmp_path; failures are injected through the
module's ``_replace`` seam.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from tongs_agents import owned, paths
from tongs_agents.config_layer import protocol
from tongs_agents.config_layer.protocol import CanonicalFragment, Change, ProfileTarget

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and symlinks")


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _add_hook(document):
    document.setdefault("hooks", {}).setdefault("Stop", []).append({"command": "ours"})


def _fail_on_call(monkeypatch, number):
    real = owned._replace
    calls = []

    def replace(source, destination):
        calls.append(destination)
        if len(calls) == number:
            raise OSError("injected failure")
        real(source, destination)

    monkeypatch.setattr(owned, "_replace", replace)
    return calls


# Owned files


@POSIX
def test_write_owned_json_is_private_and_marked(tmp_path):
    path = tmp_path / "pi" / "tongs-agents.json"
    change = owned.write_owned_file(
        path, {"hook_path": "/x"}, owner="tongs-agents", schema_version=1,
        managed_paths=["/a"],
    )
    assert change.kind == "create"
    assert change.before_sha256 is None
    assert change.after_sha256 == owned.sha256_of(path.read_bytes())
    assert _mode(path) == 0o600
    assert path.read_text() == json.dumps({
        "hook_path": "/x", "managed_paths": ["/a"], "owner": "tongs-agents",
        "schema_version": 1,
    }, indent=2, sort_keys=True) + "\n"


def test_rewrite_is_unchanged_then_update(tmp_path):
    path = tmp_path / "x.json"
    owned.write_owned_file(path, {"a": 1}, owner="tongs-agents", schema_version=1)
    mtime = path.stat().st_mtime_ns
    again = owned.write_owned_file(path, {"a": 1}, owner="tongs-agents", schema_version=1)
    assert again.kind == "unchanged"
    assert path.stat().st_mtime_ns == mtime
    updated = owned.write_owned_file(path, {"a": 2}, owner="tongs-agents", schema_version=1)
    assert updated.kind == "update"
    assert json.loads(path.read_text())["a"] == 2


@pytest.mark.parametrize("text", ['{"a": 1}', '{"owner": "someone-else", "schema_version": 1}',
                                  "not json", "[]"])
def test_unmarked_file_is_never_replaced(tmp_path, text):
    path = tmp_path / "x.json"
    path.write_text(text)
    with pytest.raises(owned.OwnershipError, match="not owned by tongs-agents"):
        owned.write_owned_file(path, {"a": 1}, owner="tongs-agents", schema_version=1)
    assert path.read_text() == text
    plan = owned.plan_owned_file(path, {"a": 1}, owner="tongs-agents", schema_version=1)
    assert plan.kind == "conflict"


def test_marker_keys_in_content_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="marker keys"):
        owned.write_owned_file(tmp_path / "x.json", {"owner": "me"}, owner="tongs-agents",
                               schema_version=1)


@pytest.mark.parametrize(("name", "first_line"), [
    ("AGENTS.md", '<!-- tongs-agents-owned {"owner": "tongs-agents", "schema_version": 2} -->'),
    ("config.toml", '# tongs-agents-owned {"owner": "tongs-agents", "schema_version": 2}'),
    ("layout.kdl", '// tongs-agents-owned {"owner": "tongs-agents", "schema_version": 2}'),
])
def test_text_formats_carry_a_header_marker(tmp_path, name, first_line):
    path = tmp_path / name
    owned.write_owned_file(path, "body\n", owner="tongs-agents", schema_version=2)
    assert path.read_text().splitlines() == [first_line, "body"]
    current = owned.read_owned_file(path, owner="tongs-agents")
    assert current.content == "body\n"
    assert current.schema_version == 2
    assert current.managed_paths is None


def test_markdown_marker_follows_front_matter(tmp_path):
    path = tmp_path / "SKILL.md"
    body = "---\nname: demo\ndescription: x\n---\n# Demo\n"
    owned.write_owned_file(path, body, owner="tongs-agents", schema_version=1,
                           managed_paths=["/a-->b"])
    lines = path.read_text().splitlines()
    assert lines[:4] == ["---", "name: demo", "description: x", "---"]
    assert lines[4].startswith("<!-- tongs-agents-owned ")
    assert "-->b" not in lines[4]
    current = owned.read_owned_file(path, owner="tongs-agents")
    assert current.content == body
    assert current.managed_paths == ("/a-->b",)


def test_unknown_format_needs_fmt(tmp_path):
    with pytest.raises(ValueError, match="unknown owned file format"):
        owned.write_owned_file(tmp_path / "x.yaml", "a", owner="o", schema_version=1)
    owned.write_owned_file(tmp_path / "x.yaml", "a", owner="o", schema_version=1, fmt="toml")


def test_delete_owned_file(tmp_path):
    path = tmp_path / "x.json"
    owned.write_owned_file(path, {}, owner="tongs-agents", schema_version=1)
    assert owned.delete_owned_file(path, owner="tongs-agents").kind == "delete"
    assert not path.exists()
    assert owned.delete_owned_file(path, owner="tongs-agents").kind == "unchanged"
    path.write_text("{}")
    with pytest.raises(owned.OwnershipError):
        owned.delete_owned_file(path, owner="tongs-agents")
    assert path.read_text() == "{}"


# Refusals


@POSIX
@pytest.mark.parametrize("operation", ["owned", "edit", "delete", "read"])
def test_symlinked_target_is_refused(tmp_path, operation):
    real = tmp_path / "real.json"
    real.write_text('{"owner": "tongs-agents", "schema_version": 1}')
    link = tmp_path / "link.json"
    link.symlink_to(real)
    before = real.read_bytes()
    with pytest.raises(owned.SymlinkError, match="refusing to modify a symlinked file"):
        if operation == "owned":
            owned.write_owned_file(link, {}, owner="tongs-agents", schema_version=1)
        elif operation == "edit":
            owned.edit_json_entries(link, _add_hook)
        elif operation == "delete":
            owned.delete_owned_file(link, owner="tongs-agents")
        else:
            owned.read_json(link)
    assert link.is_symlink()
    assert real.read_bytes() == before
    assert not owned.backup_path(link).exists()


def test_oversized_file_is_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"pad": "x" * 200}))
    before = path.read_bytes()
    ownership = owned.EntryOwnership(max_bytes=100)
    with pytest.raises(owned.SizeError, match="larger than 100 bytes"):
        owned.edit_json_entries(path, _add_hook, ownership=ownership)
    assert path.read_bytes() == before
    with pytest.raises(owned.SizeError):
        owned.write_owned_file(tmp_path / "big.json", {"pad": "x" * 200},
                               owner="tongs-agents", schema_version=1, max_bytes=100)
    assert not (tmp_path / "big.json").exists()


def test_non_regular_file_is_refused(tmp_path):
    (tmp_path / "settings.json").mkdir()
    with pytest.raises(owned.OwnedFileError, match="not a regular file"):
        owned.edit_json_entries(tmp_path / "settings.json", _add_hook)


@pytest.mark.parametrize("text", ['{"hooks": {', "﻿{}", "{]"])
def test_malformed_json_is_left_untouched(tmp_path, text):
    path = tmp_path / "settings.json"
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(owned.MalformedError) as raised:
        owned.edit_json_entries(path, _add_hook)
    message = str(raised.value)
    assert message.startswith(f"{path}: malformed JSON (")
    assert "\n" not in message
    assert path.read_bytes() == before
    assert not owned.backup_path(path).exists()


def test_validate_refuses_before_mutate(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[]")
    called = []

    def validate(where, document):
        if not isinstance(document, dict):
            raise owned.OwnedFileError(f"{where}: expected a JSON object")

    with pytest.raises(owned.OwnedFileError, match="expected a JSON object"):
        owned.edit_json_entries(path, called.append,
                                ownership=owned.EntryOwnership(validate=validate))
    assert called == []
    assert path.read_text() == "[]"


def test_errors_are_os_errors():
    assert issubclass(owned.OwnedFileError, OSError)


# Shared JSON edits


@POSIX
def test_edit_keeps_mode_and_backs_up_once(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"env": {"A": "1"}}')
    path.chmod(0o644)
    original = path.read_bytes()

    change = owned.edit_json_entries(path, _add_hook)

    assert change.kind == "edit_entry"
    assert change.before_sha256 == owned.sha256_of(original)
    assert _mode(path) == 0o644
    backup = owned.backup_path(path)
    assert backup.name == "settings.json.tongs-agents.bak"
    assert backup.read_bytes() == original
    assert _mode(backup) == 0o600
    assert json.loads(path.read_text()) == {
        "env": {"A": "1"}, "hooks": {"Stop": [{"command": "ours"}]},
    }

    owned.edit_json_entries(path, _add_hook)
    assert backup.read_bytes() == original


def test_unchanged_edit_writes_nothing(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{\n  "a": 1\n}\n')
    mtime = path.stat().st_mtime_ns
    change = owned.edit_json_entries(path, lambda document: None)
    assert change.kind == "unchanged"
    assert path.stat().st_mtime_ns == mtime
    assert not owned.backup_path(path).exists()


@POSIX
def test_edit_creates_missing_file_private_without_backup(tmp_path):
    path = tmp_path / "new" / "hooks.json"
    change = owned.edit_json_entries(path, _add_hook)
    assert change.kind == "create"
    assert _mode(path) == 0o600
    assert not owned.backup_path(path).exists()


def test_mutate_may_return_a_replacement(tmp_path):
    path = tmp_path / "x.json"
    owned.edit_json_entries(path, lambda document: {"replaced": True})
    assert path.read_text() == '{\n  "replaced": true\n}\n'


def test_plan_does_not_write(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    change = owned.plan_json_edit(path, _add_hook)
    assert change.kind == "edit_entry"
    assert path.read_text() == "{}"
    assert json.loads(change.content) == {"hooks": {"Stop": [{"command": "ours"}]}}


# ChangeSet


def _three_files(tmp_path):
    shared = tmp_path / "claude" / "settings.json"
    shared.parent.mkdir()
    shared.write_text('{"user": true}')
    own = tmp_path / "pi" / "tongs-agents.json"
    own.parent.mkdir()
    own.write_text('{"owner": "tongs-agents", "schema_version": 1, "v": 0}')
    fresh = tmp_path / "codex" / "deep" / "hooks.json"
    return shared, own, fresh


def _stage_three(changes, shared, own, fresh):
    changes.edit_json_entries(shared, _add_hook)
    changes.write_owned_file(own, {"v": 1}, owner="tongs-agents", schema_version=1)
    changes.edit_json_entries(fresh, _add_hook)


@pytest.mark.parametrize("failing_call", [1, 2, 3, 4])
def test_injected_failure_between_files_rolls_everything_back(
    tmp_path, monkeypatch, failing_call
):
    shared, own, fresh = _three_files(tmp_path)
    before = {shared: shared.read_bytes(), own: own.read_bytes()}
    changes = owned.ChangeSet()
    _stage_three(changes, shared, own, fresh)
    _fail_on_call(monkeypatch, failing_call)

    with pytest.raises(OSError, match="injected failure"):
        changes.commit()

    assert {path: path.read_bytes() for path in before} == before
    assert not fresh.exists()
    assert not (tmp_path / "codex").exists()
    assert not owned.backup_path(shared).exists()
    assert sorted(entry.name for entry in shared.parent.iterdir()) == ["settings.json"]
    assert sorted(entry.name for entry in own.parent.iterdir()) == ["tongs-agents.json"]


def test_failed_restore_is_named_in_the_error(tmp_path, monkeypatch):
    shared, own, fresh = _three_files(tmp_path)
    changes = owned.ChangeSet()
    _stage_three(changes, shared, own, fresh)
    real = owned._replace

    def replace(source, destination):
        if destination == own:
            raise OSError("disk gone")
        real(source, destination)

    monkeypatch.setattr(owned, "_replace", replace)
    with pytest.raises(owned.OwnedFileError) as raised:
        changes.commit()
    assert str(raised.value) == f"{own}: could not write (disk gone); could not restore {own}"
    assert shared.read_text() == '{"user": true}'


def test_commit_applies_every_change(tmp_path):
    shared, own, fresh = _three_files(tmp_path)
    changes = owned.ChangeSet()
    _stage_three(changes, shared, own, fresh)
    applied = changes.commit()
    assert [change.kind for change in applied] == ["edit_entry", "update", "create"]
    assert json.loads(own.read_text())["v"] == 1
    assert fresh.exists()
    with pytest.raises(RuntimeError):
        changes.commit()


def test_conflict_refuses_the_whole_set(tmp_path):
    shared, own, fresh = _three_files(tmp_path)
    foreign = tmp_path / "foreign.json"
    foreign.write_text('{"mine": 1}')
    changes = owned.ChangeSet()
    _stage_three(changes, shared, own, fresh)
    staged = changes.write_owned_file(foreign, {}, owner="tongs-agents", schema_version=1)
    assert staged.kind == "conflict"
    before = shared.read_bytes()
    with pytest.raises(owned.OwnershipError, match="foreign.json"):
        changes.commit()
    assert shared.read_bytes() == before
    assert not fresh.exists()
    assert foreign.read_text() == '{"mine": 1}'


def test_file_changed_after_planning_is_refused(tmp_path):
    shared, own, fresh = _three_files(tmp_path)
    changes = owned.ChangeSet()
    _stage_three(changes, shared, own, fresh)
    own.write_text('{"owner": "tongs-agents", "schema_version": 1, "v": 9}')
    with pytest.raises(owned.StaleError, match="changed since it was planned"):
        changes.commit()
    assert shared.read_text() == '{"user": true}'
    assert not fresh.exists()


def test_staging_one_path_twice_composes(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    changes = owned.ChangeSet()
    changes.edit_json_entries(path, _add_hook)
    staged = changes.edit_json_entries(path, lambda document: document.update(extra=1))
    assert len(changes.changes) == 1
    assert staged.kind == "edit_entry"
    assert staged.before_sha256 == owned.sha256_of(b"{}")
    changes.commit()
    assert json.loads(path.read_text()) == {
        "hooks": {"Stop": [{"command": "ours"}]}, "extra": 1,
    }


def test_staging_a_revert_is_unchanged(tmp_path):
    path = tmp_path / "x.json"
    owned.write_owned_file(path, {"v": 1}, owner="tongs-agents", schema_version=1)
    changes = owned.ChangeSet()
    changes.write_owned_file(path, {"v": 2}, owner="tongs-agents", schema_version=1)
    staged = changes.write_owned_file(path, {"v": 1}, owner="tongs-agents", schema_version=1)
    assert staged.kind == "unchanged"


def test_create_then_delete_is_unchanged(tmp_path):
    path = tmp_path / "x.json"
    changes = owned.ChangeSet()
    changes.write_owned_file(path, {}, owner="tongs-agents", schema_version=1)
    staged = changes.delete_owned_file(path, owner="tongs-agents")
    assert staged.kind == "unchanged"
    changes.commit()
    assert not path.exists()


def test_add_applies_an_emitter_change(tmp_path):
    path = tmp_path / "AGENTS.md"
    planned = owned.plan_owned_file(path, "hi\n", owner="tongs-agents", schema_version=1)
    changes = owned.ChangeSet()
    changes.add(planned)
    changes.commit()
    assert owned.read_owned_file(path, owner="tongs-agents").content == "hi\n"


# Manifest


@POSIX
def test_manifest_records_managed_paths_per_target(tmp_path):
    claude = ProfileTarget("claude", tmp_path / "work")
    codex = ProfileTarget("codex")
    changes = owned.ChangeSet()
    changes.record_managed_paths(claude, [tmp_path / "work" / "b", tmp_path / "work" / "a"])
    changes.record_managed_paths(codex, ["/home/user/.codex/AGENTS.md"])
    assert not owned.manifest_path().exists()
    changes.commit()

    path = owned.manifest_path()
    assert path == paths.state_dir() / "emit" / "manifest.json"
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert owned.load_manifest() == {
        f"claude:{tmp_path / 'work'}": [str(tmp_path / "work" / "a"),
                                        str(tmp_path / "work" / "b")],
        "codex:default": ["/home/user/.codex/AGENTS.md"],
    }
    assert owned.managed_paths_for(codex) == (Path("/home/user/.codex/AGENTS.md"),)

    changes = owned.ChangeSet()
    changes.record_managed_paths(codex, [])
    changes.commit()
    assert list(owned.load_manifest()) == [f"claude:{tmp_path / 'work'}"]


def test_manifest_refuses_relative_paths():
    with pytest.raises(ValueError, match="absolute"):
        owned.ChangeSet().record_managed_paths(ProfileTarget("pi"), ["relative"])


def test_missing_manifest_is_empty():
    assert owned.load_manifest() == {}
    assert owned.managed_paths_for(ProfileTarget("pi")) == ()


@pytest.mark.parametrize("text", [
    '{"targets": {}}',
    '{"owner": "tongs-agents", "schema_version": 1, "targets": {"pi:default": ["x"]}}',
    '{"owner": "tongs-agents", "schema_version": 9, "targets": {}}',
])
def test_unrecognized_manifest_is_refused(text):
    path = owned.manifest_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(owned.OwnedFileError):
        owned.load_manifest()
    with pytest.raises(owned.OwnedFileError):
        owned.ChangeSet().record_managed_paths(ProfileTarget("pi"), [])
    assert path.read_text() == text


# Protocol


def test_change_kinds_are_checked(tmp_path):
    with pytest.raises(ValueError, match="unknown change kind"):
        Change(tmp_path, "rename")
    with pytest.raises(ValueError, match="needs content"):
        Change(tmp_path, "create")
    assert Change(tmp_path, "delete", "a", None).content is None
    assert set(protocol.CHANGE_KINDS) == {
        "create", "update", "delete", "edit_entry", "unchanged", "conflict",
    }


class _FakeEmitter:
    harness_id = "claude"

    def plan(self, config, target):
        path = target.config_dir / "CLAUDE.md"
        return [owned.plan_owned_file(path, config, owner="tongs-agents", schema_version=1)]

    def capture(self, target):
        current = owned.read_owned_file(target.config_dir / "CLAUDE.md", owner="tongs-agents")
        return CanonicalFragment("claude", target, {"instructions": current.content})


def test_fake_emitter_round_trips_through_a_changeset(tmp_path):
    emitter = _FakeEmitter()
    assert isinstance(emitter, protocol.Emitter)
    target = ProfileTarget("claude", tmp_path)
    changes = owned.ChangeSet()
    for change in emitter.plan("Be brief.\n", target):
        changes.add(change)
    changes.commit()
    assert emitter.capture(target).data == {"instructions": "Be brief.\n"}
    assert [change.kind for change in emitter.plan("Be brief.\n", target)] == ["unchanged"]
    assert os.listdir(tmp_path) == ["CLAUDE.md"]
