"""The config layer engine: plan, classify and apply emitter changes per target.

Targets are the harness default homes (Claude and Codex, plus Pi when
tongs-agents is installed there), the homes recorded in ``installs.json`` and
the config dirs of every profile. Each target goes to the emitter registered
for its harness in ``config_layer.emitters.EMITTERS``; a missing registry means
no emitters.

Every planned path becomes an entry in one of the states of the emitter
strategy (docs/work/emitter-strategy.md): in sync, pending, drifted, missing,
disabled, foreign, adopted or orphaned. Apply stages the writes of every entry
that may change into one ``owned.ChangeSet``, together with the entry ledger
and the emit manifest, so a failure restores every file. A path without our
marker (a ``conflict`` change) refuses the whole apply; a drifted entry is left
alone unless forced, and the other entries proceed.

Drift compares a digest of the file with the one recorded at the last apply,
kept in the ledger ``state_dir()/emit/entries.json``:

- an emitter may define ``entry_values(target, path, data)`` returning the
  parsed values of its entries in that file (for example its TOML tables, see
  ``toml_entry_values``); the digest is the sha256 of their canonical JSON, so
  whitespace, comments and the user's own entries never count as drift;
- otherwise an owned file (one carrying our marker) hashes its bytes, or its
  ``tomllib``-parsed value for ``.toml``;
- otherwise (a shared file the emitter edits by key) drift is not tracked.

An emitter may also define ``disabled(target, path, data) -> bool`` to report
an entry the user disabled in the harness. Any exception from either hook
becomes a plan problem naming the target, never a traceback.

An orphan (a recorded entry no longer planned) that tongs-agents owns and
nobody edited is removed. A drifted orphan, or one in a shared file, stays
recorded until a forced apply: that backs up and removes an owned file, or
forgets the record of a shared one and leaves the file as it is.

``CanonicalFragment.data`` from ``Emitter.capture`` may hold ``instructions``
(text), ``skills`` ({name: {relative path: bytes or text}}), ``mcp_servers``
({name: schema v1 server table}) and ``warnings`` (lines).
"""

from __future__ import annotations

import difflib
import hashlib
import importlib
import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from tongs_agents import cli, owned, paths
from tongs_agents.config_layer import schema
from tongs_agents.config_layer.protocol import WRITING_KINDS, Change, ProfileTarget
from tongs_agents.pi_install import PI_CONFIG_NAME

EMITTERS_MODULE = "tongs_agents.config_layer.emitters"
LEDGER_NAME = "entries.json"
LEDGER_SCHEMA = 1
DRIFT_BACKUP_SUFFIX = ".tongs-agents.drift.bak"
DRIFT_BACKUP_LIMIT = 100

IN_SYNC = "in sync"
PENDING = "pending"
DRIFTED = "drifted"
MISSING = "missing"
DISABLED = "disabled"
FOREIGN = "foreign"
ADOPTED = "adopted"
ORPHANED = "orphaned"
STATES = (IN_SYNC, PENDING, DRIFTED, MISSING, DISABLED, FOREIGN, ADOPTED, ORPHANED)
CLEAN_STATES = frozenset({IN_SYNC, DISABLED, ADOPTED})

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+\Z")


class EngineError(RuntimeError):
    """The engine cannot plan or apply; the message is one line or a short list."""


class ConflictError(EngineError):
    """A planned path exists without our marker; nothing was written."""


class EmitterHookError(EngineError):
    """An emitter's ``entry_values`` or ``disabled`` hook raised."""


@dataclass(frozen=True)
class Registry:
    """The loaded emitters by harness, and why loading fell short, if it did."""

    emitters: Mapping[str, Any]
    problem: str | None = None


@dataclass(frozen=True)
class ResolvedTarget:
    """A target with the profiles that use it and where it came from."""

    target: ProfileTarget
    profiles: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    @property
    def harness(self) -> str:
        return self.target.harness

    def describe(self) -> str:
        where = "default" if self.target.config_dir is None else str(self.target.config_dir)
        return f"{self.harness} ({where})"


@dataclass(frozen=True)
class Entry:
    """One managed path in one target, its state and the change apply stages."""

    target: ResolvedTarget
    path: Path
    state: str
    emitter: str | None
    change: Change | None = None
    detail: str = ""
    current: bytes | None = field(default=None, repr=False, compare=False)

    @property
    def writes(self) -> bool:
        return self.change is not None and self.change.kind != "unchanged"


@dataclass
class Plan:
    targets: list[ResolvedTarget]
    entries: list[Entry]
    problems: list[str]
    registry: Registry
    ledger_before: dict
    ledger_after: dict
    backups: list[Change] = field(default_factory=list)

    @property
    def foreign(self) -> list[Entry]:
        return [entry for entry in self.entries if entry.state == FOREIGN]

    @property
    def skipped(self) -> list[Entry]:
        """Drifted entries apply leaves alone because they were not forced."""
        return [entry for entry in self.entries if entry.state == DRIFTED and not entry.writes]

    @property
    def clean(self) -> bool:
        return not self.problems and all(entry.state in CLEAN_STATES for entry in self.entries)

    @property
    def staged(self) -> list[Change]:
        return [entry.change for entry in self.entries if entry.writes]


@dataclass(frozen=True)
class ApplyResult:
    written: tuple[Change, ...]
    skipped: tuple[Entry, ...]


def load_emitters() -> Registry:
    """``EMITTERS`` from the emitters package.

    Only a missing emitters package means no emitters; any other ImportError
    (a broken emitter module) is reported as the registry problem.
    """
    try:
        module = importlib.import_module(EMITTERS_MODULE)
    except ImportError as error:
        if isinstance(error, ModuleNotFoundError) and error.name == EMITTERS_MODULE:
            return Registry({})
        return Registry({}, f"cannot load the emitters: {type(error).__name__}: {error}")
    registry = getattr(module, "EMITTERS", ())
    items = registry.values() if isinstance(registry, Mapping) else registry
    emitters: dict[str, Any] = {}
    for emitter in items:
        harness = getattr(emitter, "harness_id", None)
        if not isinstance(harness, str) or not harness:
            raise EngineError(f"{EMITTERS_MODULE}: an emitter has no harness_id")
        if harness in emitters:
            raise EngineError(f"{EMITTERS_MODULE}: two emitters for {harness}")
        emitters[harness] = emitter
    return Registry(emitters)


def resolve_targets(profiles: Mapping[str, Any], installs: Mapping | None = None) -> list[ResolvedTarget]:
    """Default homes, installs.json homes and every profile's home, merged."""
    if installs is None:
        installs = cli.load_installs()
    found = [cli.Target("claude", None, ("default",)), cli.Target("codex", None, ("default",))]
    default_pi = cli.Target("pi", None, ("default",))
    if os.path.lexists(default_pi.pi_dir / PI_CONFIG_NAME):
        found.append(default_pi)
    found += cli.recorded_targets(installs)
    found += cli.profile_targets(profiles)
    resolved = []
    for target in cli._merge_targets(found):
        names = tuple(
            source[len("profile "):] for source in target.sources if source.startswith("profile ")
        )
        resolved.append(ResolvedTarget(
            ProfileTarget(target.harness, target.directory), names, target.sources,
        ))
    return resolved


def ledger_path() -> Path:
    return paths.state_dir() / "emit" / LEDGER_NAME


def _valid_ledger(targets) -> bool:
    if not isinstance(targets, dict):
        return False
    for value in targets.values():
        if not isinstance(value, dict) or not isinstance(value.get("harness"), str):
            return False
        where = value.get("config_dir")
        if where is not None and not (isinstance(where, str) and os.path.isabs(where)):
            return False
        entries = value.get("entries")
        if not isinstance(entries, dict):
            return False
        for name, record in entries.items():
            if not os.path.isabs(name) or not isinstance(record, dict):
                return False
            digest = record.get("digest")
            if digest is not None and not isinstance(digest, str):
                return False
            if not isinstance(record.get("emitter"), str):
                return False
    return True


def load_ledger() -> dict:
    """Recorded entries per target key; empty when nothing was applied yet."""
    path = ledger_path()
    current = owned.read_owned_file(path, owner=owned.OWNER, fmt="json")
    if current is None:
        return {}
    targets = current.content.get("targets")
    if current.schema_version != LEDGER_SCHEMA or not _valid_ledger(targets):
        raise owned.OwnedFileError(f"{path}: unrecognized entry ledger")
    return json.loads(json.dumps(targets))


def canonical_digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def toml_entry_values(data: bytes | None, names: Iterable[str]) -> dict[str, Any]:
    """The parsed value at each dotted table name (``mcp_servers.x``), None when absent.

    For an emitter's ``entry_values``: drift then follows the parsed tables,
    not their bytes. Unparsable TOML maps every name to its raw digest.
    """
    names = list(names)
    try:
        document = {} if data is None else tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {name: owned.sha256_of(data) for name in names}
    values = {}
    for name in names:
        node: Any = document
        for part in name.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        values[name] = node
    return values


def _is_ours(path: Path, data: bytes | None) -> bool:
    if data is None:
        return False
    try:
        fmt = owned.file_format(path)
    except ValueError:
        return False
    parsed = owned.parse_owned(path, data, fmt)
    return parsed is not None and parsed.owner == owned.OWNER


def _call_hook(emitter: Any, name: str, target: ProfileTarget, path: Path, data: bytes | None):
    """``emitter.<name>(target, path, data)``, None when the emitter has no such hook."""
    hook = getattr(emitter, name, None)
    if not callable(hook):
        return None
    try:
        return hook(target, path, data)
    except Exception as error:
        harness = getattr(emitter, "harness_id", "unknown")
        raise EmitterHookError(
            f"the {harness} emitter's {name} failed for {path}: {type(error).__name__}: {error}"
        ) from error


def entry_digest(emitter: Any, target: ProfileTarget, path: Path, data: bytes | None) -> str | None:
    """The drift digest of ``path`` holding ``data``; None when drift is not tracked.

    Raises ``EmitterHookError`` when the emitter's ``entry_values`` raises.
    """
    values = _call_hook(emitter, "entry_values", target, path, data)
    if values is not None:
        return canonical_digest(values)
    if data is None or not _is_ours(path, data):
        return None
    if path.suffix.lower() == ".toml":
        try:
            return canonical_digest(tomllib.loads(data.decode("utf-8")))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            pass
    return owned.sha256_of(data)


def _read(path: Path) -> bytes | None:
    data, _ = owned._read_current(path, owned.MAX_EDIT_BYTES)
    return data


def _key(path: Path) -> str:
    return os.path.abspath(path)


def _ledger_target(target: ProfileTarget, entries: dict) -> dict:
    return {
        "harness": target.harness,
        "config_dir": None if target.config_dir is None else str(target.config_dir),
        "entries": entries,
    }


def _disabled(emitter: Any, target: ProfileTarget, path: Path, data: bytes | None) -> bool:
    return bool(_call_hook(emitter, "disabled", target, path, data))


def _drift_backup(path: Path, current: bytes) -> Change:
    """Back up a drifted file without ever replacing an earlier backup.

    A backup holding other content gets a numbered sibling, so a second
    forced apply cannot destroy the edit the first one saved.
    """
    after = owned.sha256_of(current)
    base = path.name + DRIFT_BACKUP_SUFFIX
    for number in range(DRIFT_BACKUP_LIMIT):
        backup = path.with_name(base if number == 0 else f"{base}.{number}")
        previous = _read(backup)
        if previous is None:
            return Change(backup, "create", owned.sha256_of(None), after, current)
        if previous == current:
            return Change(backup, "unchanged", after, after, current)
    raise EngineError(
        f"{path}: {DRIFT_BACKUP_LIMIT} drift backups already exist; "
        f"remove old {base}* files and retry"
    )


_DRIFT_DETAIL = (
    "edited outside tongs-agents since the last apply; keep the edit with "
    "'tongs-agents config capture', or overwrite it with 'config apply --force'"
)


def _classify(
    resolved: ResolvedTarget, emitter: Any, emitter_id: str, change: Change,
    record: dict | None, force: bool, plan: Plan,
) -> tuple[Entry | None, dict | None]:
    """The entry for one planned change and its new ledger record (None drops it)."""
    target = resolved.target
    path = Path(_key(change.path))
    current = _read(path)
    if change.kind == "conflict":
        return Entry(
            resolved, path, FOREIGN, emitter_id, None,
            "exists without the tongs-agents marker; it is never overwritten", current,
        ), record
    if change.kind == "unchanged":
        if current is None:
            return None, None
        digest = entry_digest(emitter, target, path, current)
        state = IN_SYNC if record is not None else ADOPTED
        if _disabled(emitter, target, path, current):
            state = DISABLED
        detail = "" if record is not None else "matches what tongs-agents writes; recorded on apply"
        return Entry(resolved, path, state, emitter_id, change, detail, current), {
            "digest": digest, "emitter": emitter_id,
        }
    recorded = None if record is None else record.get("digest")
    drifted = (
        current is not None and recorded is not None
        and entry_digest(emitter, target, path, current) != recorded
    )
    if drifted and not force:
        return Entry(resolved, path, DRIFTED, emitter_id, None, _DRIFT_DETAIL, current), record
    if drifted:
        plan.backups.append(_drift_backup(path, current))
    if change.kind == "delete":
        return Entry(resolved, path, DRIFTED if drifted else PENDING, emitter_id, change,
                     "removed", current), None
    state = MISSING if current is None and record is not None else PENDING
    if drifted:
        state = DRIFTED
    detail = "overwritten after a backup" if drifted else ""
    return Entry(resolved, path, state, emitter_id, change, detail, current), {
        "digest": entry_digest(emitter, target, path, change.content), "emitter": emitter_id,
    }


_ORPHAN_FORGOTTEN = "no longer in the dotfiles; forgotten, the file is left as it is"


def _orphan(
    resolved: ResolvedTarget, emitter: Any, name: str, record: dict, force: bool, plan: Plan,
) -> tuple[Entry, dict | None]:
    """The entry for a recorded path no longer planned and its kept record (None drops it).

    Without ``force`` a drifted or shared orphan keeps its record; with it an
    owned file is backed up and removed, and a shared file is forgotten.
    """
    path = Path(name)
    current = _read(path)
    emitter_id = record["emitter"]
    if current is None:
        return Entry(resolved, path, ORPHANED, emitter_id, None, "already removed"), None
    recorded = record.get("digest")
    ours = _is_ours(path, current)
    drifted = (
        recorded is not None and entry_digest(emitter, resolved.target, path, current) != recorded
    )
    if drifted and not force:
        remedy = "backs it up and removes it" if ours else "forgets it and leaves the file"
        return Entry(
            resolved, path, ORPHANED, emitter_id, None,
            "no longer in the dotfiles but edited since the last apply; left in place; "
            f"'config apply --force' {remedy}",
            current,
        ), record
    if ours:
        change = owned.plan_delete_owned(path, owner=owned.OWNER)
        detail = "no longer in the dotfiles; removed"
        if drifted:
            plan.backups.append(_drift_backup(path, current))
            detail = "no longer in the dotfiles; removed after a backup"
        return Entry(resolved, path, ORPHANED, emitter_id, change, detail, current), None
    if force:
        return Entry(resolved, path, ORPHANED, emitter_id, None, _ORPHAN_FORGOTTEN, current), None
    return Entry(
        resolved, path, ORPHANED, emitter_id, None,
        "no longer in the dotfiles; its emitter removes its entries from this shared file; "
        "'config apply --force' forgets it and leaves the file",
        current,
    ), record


def _ledger_targets(ledger: dict) -> dict[str, ResolvedTarget]:
    found = {}
    for key, value in ledger.items():
        where = value.get("config_dir")
        target = ProfileTarget(value["harness"], None if where is None else Path(where))
        found[key] = ResolvedTarget(target, (), ("ledger",))
    return found


def _planned_changes(config, resolved, emitter, plan) -> list[Change] | None:
    try:
        changes = list(emitter.plan(config, resolved.target))
    except (OSError, ValueError) as error:
        plan.problems.append(f"{resolved.describe()}: {error}")
        return None
    for change in changes:
        if not isinstance(change, Change) or not os.path.isabs(change.path):
            plan.problems.append(
                f"{resolved.describe()}: the {resolved.harness} emitter planned an invalid change"
            )
            return None
    return changes


def plan(
    config: schema.CanonicalConfig,
    *,
    targets: list[ResolvedTarget] | None = None,
    registry: Registry | None = None,
    force: bool = False,
) -> Plan:
    """Classify every entry of every target; never writes."""
    registry = load_emitters() if registry is None else registry
    targets = resolve_targets(config.profiles) if targets is None else targets
    ledger = load_ledger()
    result = Plan(targets, [], [], registry, ledger, {})
    if registry.problem:
        result.problems.append(registry.problem)
    seen: dict[str, tuple[str, Change]] = {}
    planned_keys: set[tuple[str, str]] = set()
    after: dict[str, dict] = {}
    for resolved in targets:
        emitter = registry.emitters.get(resolved.harness)
        if emitter is None:
            continue
        key = owned.target_key(resolved.target)
        records = ledger.get(key, {}).get("entries", {})
        changes = _planned_changes(config, resolved, emitter, result)
        if changes is None:
            if key in ledger:
                after[key] = ledger[key]
                planned_keys.update((key, name) for name in records)
            continue
        entries: dict[str, dict] = {}
        for change in changes:
            name = _key(change.path)
            planned_keys.add((key, name))
            previous = seen.get(name)
            if previous is not None:
                if (previous[1].kind, previous[1].after_sha256) != (change.kind, change.after_sha256):
                    result.problems.append(
                        f"{name}: planned twice with different content "
                        f"({previous[0]} and {resolved.describe()})"
                    )
                continue
            seen[name] = (resolved.describe(), change)
            try:
                entry, record = _classify(
                    resolved, emitter, emitter.harness_id, change, records.get(name), force, result,
                )
            except (OSError, EmitterHookError) as error:
                result.problems.append(f"{resolved.describe()}: {error}")
                if name in records:
                    entries[name] = records[name]
                continue
            if entry is not None:
                result.entries.append(entry)
            if record is not None:
                entries[name] = record
        if entries:
            after[key] = _ledger_target(resolved.target, entries)
    by_key = {owned.target_key(item.target): item for item in targets}
    for key, item in _ledger_targets(ledger).items():
        resolved = by_key.get(key, item)
        for name, record in ledger[key]["entries"].items():
            if (key, name) in planned_keys:
                continue
            emitter = registry.emitters.get(record["emitter"])
            if emitter is None:
                after.setdefault(key, _ledger_target(resolved.target, {}))["entries"][name] = record
                continue
            try:
                entry, kept = _orphan(resolved, emitter, name, record, force, result)
            except (OSError, EmitterHookError) as error:
                result.problems.append(f"{resolved.describe()}: {error}")
                kept = record
                entry = None
            if entry is not None:
                result.entries.append(entry)
            if kept is not None:
                after.setdefault(key, _ledger_target(resolved.target, {}))["entries"][name] = kept
    result.ledger_after = {key: after[key] for key in sorted(after) if after[key]["entries"]}
    return result


def unified_diff(entry: Entry) -> str:
    """The planned change of ``entry`` as a unified diff."""
    change = entry.change
    if change is None:
        return ""
    before = entry.current
    after = None if change.kind == "delete" else change.content
    try:
        old = [] if before is None else before.decode("utf-8").splitlines(keepends=True)
        new = [] if after is None else after.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return f"Binary file {entry.path} changes\n"
    lines = difflib.unified_diff(
        old, new,
        fromfile="/dev/null" if before is None else f"a{entry.path}",
        tofile="/dev/null" if after is None else f"b{entry.path}",
    )
    text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                   for line in lines)
    return text


def _manifest_updates(plan: Plan) -> list[tuple[ProfileTarget, list[str]]]:
    current = owned.load_manifest()
    wanted: dict[str, tuple[ProfileTarget, list[str]]] = {}
    for key, value in plan.ledger_after.items():
        where = value["config_dir"]
        target = ProfileTarget(value["harness"], None if where is None else Path(where))
        wanted[key] = (target, sorted(value["entries"]))
    updates = []
    for key, (target, managed) in wanted.items():
        if current.get(key) != managed:
            updates.append((target, managed))
    for key in current:
        if key not in wanted:
            harness, _, where = key.partition(":")
            target = ProfileTarget(harness, None if where == "default" else Path(where))
            updates.append((target, []))
    return updates


def apply(plan: Plan) -> ApplyResult:
    """Write every staged change, the ledger and the manifest as one ChangeSet.

    Refuses everything when the plan has problems or foreign entries. Drifted
    entries the plan did not force are skipped and returned.
    """
    if plan.problems:
        raise EngineError("; ".join(plan.problems))
    if plan.foreign:
        raise ConflictError(
            "; ".join(f"{entry.path}: {entry.detail}" for entry in plan.foreign)
            + "; nothing was written"
        )
    changes = owned.ChangeSet()
    for backup in plan.backups:
        changes.add(backup)
    for change in plan.staged:
        changes.add(change)
    if plan.ledger_after != plan.ledger_before:
        if plan.ledger_after or os.path.lexists(ledger_path()):
            changes.write_owned_file(
                ledger_path(), {"targets": plan.ledger_after},
                owner=owned.OWNER, schema_version=LEDGER_SCHEMA, fmt="json",
            )
    for target, managed in _manifest_updates(plan):
        changes.record_managed_paths(target, managed)
    skipped = tuple(plan.skipped)
    written = tuple(change for change in changes.changes if change.kind != "unchanged")
    if not written:
        return ApplyResult((), skipped)
    paths.ensure_private_dir(paths.state_dir(create=True) / "emit")
    changes.commit()
    return ApplyResult(written, skipped)


# Capture


@dataclass
class Capture:
    instructions: str | None = None
    skills: dict[str, dict[str, bytes]] = field(default_factory=dict)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.instructions is None and not self.skills and not self.mcp_servers


def _safe_relative(name: str) -> bool:
    if not isinstance(name, str) or not name or "\0" in name or "\\" in name:
        return False
    relative = PurePosixPath(name)
    return not relative.is_absolute() and ".." not in relative.parts and "." not in relative.parts


def _captured_env(server: str, value, warnings: list[str]) -> list[str]:
    names = []
    if isinstance(value, Mapping):
        for name, item in value.items():
            if item in (f"${{{name}}}", f"${name}"):
                names.append(name)
            else:
                warnings.append(f"mcp_servers.{server}.env.{name}: a literal value; skipped")
        return names
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return names


def _captured_server(name: str, table, warnings: list[str]) -> dict | None:
    if not isinstance(table, Mapping):
        warnings.append(f"mcp_servers.{name}: not a table; skipped")
        return None
    server: dict = {}
    for key, value in table.items():
        if key == "env":
            names = [item for item in _captured_env(name, value, warnings)
                     if schema.ENV_NAME_RE.match(item)]
            if names:
                server["env"] = sorted(set(names))
        elif key in schema.SERVER_KEYS:
            server[key] = list(value) if isinstance(value, tuple) else value
        else:
            warnings.append(f"mcp_servers.{name}.{key}: not in schema v1; skipped")
    try:
        schema._server(name, server, ())
    except schema.SchemaError as error:
        warnings.append(f"{error}; skipped")
        return None
    return server


def _merge_fragment(result: Capture, where: str, data: Mapping[str, Any]) -> None:
    for line in data.get("warnings", ()) or ():
        result.warnings.append(f"{where}: {line}")
    unknown = sorted(set(data) - {"instructions", "skills", "mcp_servers", "warnings"})
    for key in unknown:
        result.warnings.append(f"{where}: {key} is not captured by schema v1; skipped")
    text = data.get("instructions")
    if isinstance(text, str) and text:
        if result.instructions is None:
            result.instructions = text
        elif result.instructions != text:
            result.warnings.append(f"{where}: instructions differ from an earlier target; kept the first")
    for name, files in (data.get("skills") or {}).items():
        if not isinstance(name, str) or not schema.SKILL_NAME_RE.match(name):
            result.warnings.append(f"{where}: skill {name!r} has an invalid name; skipped")
            continue
        if not isinstance(files, Mapping) or schema.SKILL_FILE not in files:
            result.warnings.append(f"{where}: skill {name} has no {schema.SKILL_FILE}; skipped")
            continue
        content = {}
        for relative, value in files.items():
            if not _safe_relative(relative) or not isinstance(value, (bytes, str)):
                result.warnings.append(f"{where}: skill {name} file {relative!r} skipped")
                continue
            content[relative] = value.encode("utf-8") if isinstance(value, str) else value
        if name in result.skills and result.skills[name] != content:
            result.warnings.append(f"{where}: skill {name} differs from an earlier target; kept the first")
            continue
        result.skills.setdefault(name, content)
    for name, table in (data.get("mcp_servers") or {}).items():
        if not isinstance(name, str) or name == schema.RESERVED_SERVER_NAME:
            continue
        server = _captured_server(name, table, result.warnings)
        if server is None:
            continue
        if name in result.mcp_servers and result.mcp_servers[name] != server:
            result.warnings.append(
                f"{where}: MCP server {name} differs from an earlier target; kept the first"
            )
            continue
        result.mcp_servers.setdefault(name, server)


def capture(
    harness: str, targets: list[ResolvedTarget], registry: Registry | None = None
) -> Capture:
    """Read ``harness``'s side of every target; never writes."""
    registry = load_emitters() if registry is None else registry
    emitter = registry.emitters.get(harness)
    if emitter is None:
        raise EngineError(registry.problem or f"no emitter is available for {harness}")
    result = Capture()
    for resolved in targets:
        if resolved.harness != harness:
            continue
        try:
            fragment = emitter.capture(resolved.target)
        except (OSError, ValueError) as error:
            raise EngineError(f"{resolved.describe()}: {error}") from None
        _merge_fragment(result, resolved.describe(), fragment.data or {})
    return result


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _toml_string(key)


def _toml_string(value: str) -> str:
    escaped = []
    for char in value:
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\t":
            escaped.append("\\t")
        elif ord(char) < 32 or ord(char) == 127:
            escaped.append(f"\\u{ord(char):04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"cannot write {type(value).__name__} to TOML")


def render_capture(harness: str, captured: Capture) -> dict[str, bytes]:
    """The files of a dotfiles directory holding ``captured``, by relative path."""
    files: dict[str, bytes] = {}
    lines = [f"schema = {_toml_string(schema.SCHEMA_ID)}", ""]
    if captured.instructions is not None:
        name = f"instructions/{harness}.md"
        files[name] = captured.instructions.encode("utf-8")
        lines += ["[instructions]", f"files = {_toml_value([name])}", ""]
    if captured.skills:
        lines += ["[skills]", 'dirs = ["skills"]', ""]
        for skill, content in sorted(captured.skills.items()):
            for relative, data in sorted(content.items()):
                files[f"skills/{skill}/{relative}"] = data
    for name, server in sorted(captured.mcp_servers.items()):
        lines.append(f"[mcp_servers.{_toml_key(name)}]")
        lines += [f"{_toml_key(key)} = {_toml_value(value)}" for key, value in server.items()]
        lines.append("")
    files[schema.CANONICAL_FILE_NAME] = ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")
    return files


def write_capture(harness: str, captured: Capture, into: Path, *, force: bool = False) -> list[Path]:
    """Write ``captured`` under ``into`` only; refuses a non-empty ``into`` unless forced."""
    into = Path(os.path.abspath(Path(into).expanduser()))
    if into.is_symlink():
        raise EngineError(f"{into}: refusing to write into a symlinked directory")
    if os.path.lexists(into):
        if not into.is_dir():
            raise EngineError(f"{into}: not a directory")
        if any(into.iterdir()) and not force:
            raise EngineError(f"{into}: not empty; pass --force to write into it")
    files = render_capture(harness, captured)
    written = []
    for relative, data in sorted(files.items()):
        path = into.joinpath(*PurePosixPath(relative).parts)
        parent = path.parent
        while parent != into:
            if parent.is_symlink():
                raise EngineError(f"{parent}: refusing to write through a symlinked directory")
            parent = parent.parent
        owned.atomic_write(path, data, mode=0o644)
        written.append(path)
    return written
