"""Shared pieces of the Claude, Codex and Pi emitters.

The emitters follow the approved docs/work/emitter-strategy.md. Each one
plans an ``EmitPlan`` for one target: file ``Change`` values for the engine's
ChangeSet, harness CLI steps for MCP servers a harness CLI owns (Claude, Pi),
and the manifest entries that make drift and removal decidable. Nothing here
writes except ``apply_plan``, the reference apply the tests and validators
use; the engine (M2-B) does the same through its own ChangeSet.

Ownership rules carried by every emitter:

- every file written whole carries the owner marker (owned.py); a file at one
  of our paths without it is a ``conflict`` and is never replaced;
- shared files change only inside our entries: a Markdown block between
  ``<!-- tongs-agents:begin -->`` and ``<!-- tongs-agents:end -->``, one
  ``@`` import line, our TOML tables, our hook commands;
- removal happens only for entries the manifest records, and only while they
  still match the recorded digest; an entry someone changed is drifted and
  left alone unless ``force``;
- environment variables are passed by name; no value is ever written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from tongs_agents import owned
from tongs_agents.config_layer import schema
from tongs_agents.config_layer.protocol import CanonicalFragment, Change, ProfileTarget
from tongs_agents.profiles import Profile, ProfileError, parse_profile

OWNER = owned.OWNER
SCHEMA_VERSION = 1
BUNDLE = "tongs-agents"
RESERVED_SERVER = "tongs-agents"
HOOK_COMMAND = "tongs-agents-hook"
BLOCK_BEGIN = "<!-- tongs-agents:begin -->"
BLOCK_END = "<!-- tongs-agents:end -->"
MAX_INSTRUCTIONS_BYTES = 64 * 1024
MAX_SKILL_FILES = 256
MAX_SKILL_BYTES = 4 * 1024 * 1024
# Suffix to owned.py marker format for files inside a skill directory.
SKILL_FORMATS = {
    ".md": "md", ".json": "json", ".toml": "toml", ".kdl": "kdl",
    ".py": "hash", ".sh": "hash", ".bash": "hash", ".yaml": "hash", ".yml": "hash",
    ".js": "slash", ".mjs": "slash", ".cjs": "slash", ".ts": "slash",
}


class EmitError(ValueError):
    """The canonical config cannot be emitted; the message names the item."""


# The schema's types, so a loaded CanonicalConfig needs no translation.
Scope = schema.Scope
McpServer = schema.McpServer


@dataclass(frozen=True)
class Skill:
    name: str
    source: Path
    scope: Scope = Scope()


@dataclass(frozen=True)
class EmitConfig:
    """The canonical config, resolved against its directory, as emitters read it."""

    root: Path
    instructions: tuple[Path, ...] = ()
    harness_instructions: Mapping[str, tuple[Path, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    skills: tuple[Skill, ...] = ()
    mcp_servers: tuple[McpServer, ...] = ()
    profiles: Mapping[str, Profile] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_document(
        cls,
        document: Mapping[str, Any],
        root: Path,
        *,
        profiles: Mapping[str, Any] | None = None,
    ) -> EmitConfig:
        """Validate a parsed ``agentic.toml`` with the schema loader and view it.

        This is ``schema.parse_document`` plus ``from_canonical``, the same
        rules ``config apply`` uses, with every skill's files also checked for
        an owner marker format. ``profiles`` (name to Profile or profile
        table) replaces the document's own; the engine passes the merged set.
        """
        root = Path(os.path.abspath(root))
        local = None
        if profiles is not None:
            document = {key: value for key, value in document.items() if key != "profiles"}
            local = {}
        try:
            for name, value in (profiles or {}).items():
                local[name] = value if isinstance(value, Profile) else parse_profile(
                    name, dict(value)
                )
            canonical = schema.parse_document(
                dict(document), root, root / schema.CANONICAL_FILE_NAME, local
            )
        except (schema.SchemaError, ProfileError, TypeError, ValueError) as error:
            raise EmitError(str(error)) from None
        for skill in canonical.skills.values():
            skill_files(skill.path, skill.name)
        return cls.from_canonical(canonical)

    @classmethod
    def from_canonical(cls, config: schema.CanonicalConfig) -> EmitConfig:
        """The view of a config the schema loader already validated."""
        root = Path(config.root)
        harness = config.instructions.harness
        return cls(
            root=root,
            instructions=tuple(root / item.path for item in config.instructions.files),
            harness_instructions=MappingProxyType({
                name: tuple(root / item.path for item in files) for name, files in harness.items()
            }),
            skills=tuple(
                Skill(skill.name, skill.path, skill.scope) for skill in config.skills.values()
            ),
            mcp_servers=tuple(config.mcp_servers.values()),
            profiles=config.profiles,
        )


@dataclass(frozen=True)
class CommandStep:
    """One harness CLI call (``argv``) or in-process action (``call``).

    ``env`` overrides the caller's environment; a None value unsets the
    variable. ``entry_id`` names the manifest entry the step realizes:
    ``entry`` is recorded when the step succeeds (None removes the record),
    otherwise the previous record is kept.
    """

    description: str
    argv: tuple[str, ...] = ()
    env: Mapping[str, str | None] = field(default_factory=lambda: MappingProxyType({}))
    call: Callable[[], bool] | None = field(default=None, compare=False)
    entry_id: str | None = None
    entry: Mapping[str, str] | None = None
    timeout: float = 60.0


# Engine entry states (config_layer.engine) an Item may carry.
IN_SYNC = "in sync"
PENDING = "pending"
DRIFTED = "drifted"
MISSING = "missing"
FOREIGN = "foreign"
ADOPTED = "adopted"
ORPHANED = "orphaned"
SKIPPED = "skipped"
# A record written before its CLI step runs: ``sha256`` is the wanted value,
# ``previous_sha256`` what was recorded before (absent when nothing was).
PENDING_KEY = "pending"
PREVIOUS_KEY = "previous_sha256"
_ACTION_STATES = {
    "keep": IN_SYNC, "adopt": ADOPTED, "replace": PENDING, "adopt-replace": PENDING,
    "remove": ORPHANED, "forget": ORPHANED, "drifted": DRIFTED, "foreign": FOREIGN,
}


@dataclass(frozen=True)
class Item:
    """The state of one keyed entry the engine cannot see as a whole file.

    A harness-CLI MCP server (``path`` is the file the harness keeps it in),
    an MCP table in a shared TOML file, a Markdown block, or a file the
    emitter left alone. ``key`` names the entry inside ``path``.
    """

    path: Path
    key: str | None
    state: str
    detail: str = ""


@dataclass
class EmitPlan:
    """Everything one emitter plans for one target."""

    target: ProfileTarget
    changes: list[Change] = field(default_factory=list)
    commands: list[CommandStep] = field(default_factory=list)
    entries: dict[str, dict[str, str]] = field(default_factory=dict)
    previous_entries: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    # Shared files to back up before a forced replacement of a drifted entry.
    backups: list[Path] = field(default_factory=list)

    def conflict(self, path: Path, message: str, key: str | None = None) -> None:
        """Report something tongs-agents does not own at ``path``; it is never changed."""
        self.conflicts.append(message)
        self.items.append(Item(Path(path), key, FOREIGN, message))

    def entries_after(self, succeeded: Iterable[CommandStep]) -> dict[str, dict[str, str]]:
        """The entries to record once the ``succeeded`` CLI steps ran."""
        entries = self.file_entries()
        for step in succeeded:
            entries = step_entries(entries, step, True)
        return entries

    @property
    def managed_paths(self) -> list[str]:
        return sorted({
            fields["path"] for fields in self.entries.values()
            if fields.get("kind") == "file" and "path" in fields
        })

    def file_entries(self) -> dict[str, dict[str, str]]:
        """The entries to record with the file changes, before any CLI step runs."""
        pending = {step.entry_id for step in self.commands if step.entry_id}
        result = {key: value for key, value in self.entries.items() if key not in pending}
        for key in pending:
            if key in self.previous_entries:
                result[key] = self.previous_entries[key]
        return result


def step_entries(
    entries: Mapping[str, Mapping[str, str]], step: CommandStep, ok: bool | None,
) -> dict[str, dict[str, str]]:
    """``entries`` before (``ok`` None) or after one CLI step.

    Before a step that sets an entry, the entry is recorded as pending with
    the wanted digest, so a step that took effect but was not reported (a
    non-zero exit after the write, an interruption) is recognised as ours by
    the next plan. A failed step leaves the entries as they are.
    """
    result = {key: dict(value) for key, value in entries.items()}
    if step.entry_id is None or ok is False:
        return result
    if ok:
        if step.entry is None:
            result.pop(step.entry_id, None)
        else:
            result[step.entry_id] = dict(step.entry)
        return result
    if step.entry is None:
        return result
    previous = result.get(step.entry_id)
    pending = {**step.entry, PENDING_KEY: "1"}
    if previous is not None:
        before = previous.get(PREVIOUS_KEY) if PENDING_KEY in previous else previous.get("sha256")
        if before:
            pending[PREVIOUS_KEY] = before
    result[step.entry_id] = pending
    return result


def not_on_path(cli_name: str) -> str:
    return (f"{cli_name} not on PATH (looked up on PATH only; shell aliases and functions "
            "are not seen)")


def skip_absent_home(plan: EmitPlan, cli_name: str, directory: Path) -> bool:
    """True, with a note, when neither the harness CLI nor its config dir exists.

    Nothing is planned for such a target and its recorded entries are kept,
    so a machine without that harness gets no new config home.
    """
    if directory.is_dir():
        return False
    plan.notes.append(f"skipped: {not_on_path(cli_name)}, and {directory} does not exist")
    plan.entries.update((key, dict(value)) for key, value in plan.previous_entries.items())
    return True


def skip_cli_entries(
    plan: EmitPlan, cli_name: str, path: Path, names: Iterable[str],
) -> None:
    """Report the CLI-managed MCP servers of a target whose CLI is not on PATH."""
    detail = not_on_path(cli_name)
    plan.notes.append(f"skipped: {detail}")
    for name in sorted(names):
        plan.items.append(Item(Path(path), f"mcpServers.{name}", SKIPPED, detail))
        recorded = plan.previous_entries.get(f"mcp:{name}")
        if recorded is not None:
            plan.entries[f"mcp:{name}"] = dict(recorded)


# Canonical config


def coerce_config(config: Any) -> EmitConfig:
    """An EmitConfig from what the engine passes: an EmitConfig or a ``schema.CanonicalConfig``."""
    if isinstance(config, EmitConfig):
        return config
    if isinstance(config, schema.CanonicalConfig):
        return EmitConfig.from_canonical(config)
    raise TypeError(f"cannot emit from a {type(config).__name__}")


def skill_files(path: Path, name: str) -> list[tuple[str, str, str]]:
    """``(relative path, marker format, text)`` for every file of one skill."""
    where = f"skills: {name}"
    found = []
    total = 0
    for directory, dirnames, filenames in os.walk(path):
        dirnames.sort()
        for entry in [*dirnames, *filenames]:
            full = Path(directory) / entry
            info = os.lstat(full)
            if stat.S_ISLNK(info.st_mode):
                raise EmitError(f"{where}: refusing the symlink {full.relative_to(path)}")
            if entry in dirnames:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise EmitError(f"{where}: refusing {full.relative_to(path)} (not a regular file)")
            relative = full.relative_to(path).as_posix()
            fmt = SKILL_FORMATS.get(full.suffix.lower())
            if fmt is None:
                raise EmitError(
                    f"{where}: {relative} has no owner marker format; supported suffixes: "
                    + " ".join(sorted(SKILL_FORMATS))
                )
            total += info.st_size
            if len(found) >= MAX_SKILL_FILES or total > MAX_SKILL_BYTES:
                raise EmitError(f"{where}: over {MAX_SKILL_FILES} files or 4 MiB")
            try:
                text = full.read_bytes().decode("utf-8")
            except UnicodeError:
                raise EmitError(f"{where}: {relative} is not UTF-8 text") from None
            found.append((relative, fmt, text))
    return sorted(found)


# Targets


def default_dir(harness: str) -> Path:
    home = Path.home()
    return {
        "claude": home / ".claude",
        "codex": home / ".codex",
        "pi": home / ".pi" / "agent",
    }[harness]


def config_dir(target: ProfileTarget) -> Path:
    """The directory a target writes into; None means the harness default."""
    if target.config_dir is None:
        return default_dir(target.harness)
    return Path(os.path.abspath(Path(target.config_dir).expanduser()))


def profiles_for(config: EmitConfig, target: ProfileTarget) -> tuple[str, ...]:
    return schema.profiles_for(config.profiles, target)


def expand_home(value: str) -> str:
    if value == "~" or value.startswith("~/"):
        return str(Path.home()) + value[1:]
    return value


def selected_servers(config: EmitConfig, target: ProfileTarget) -> list[McpServer]:
    names = profiles_for(config, target)
    return [
        server for server in config.mcp_servers
        if server.enabled and server.scope.applies(target.harness, names)
    ]


def selected_skills(config: EmitConfig, target: ProfileTarget) -> list[Skill]:
    names = profiles_for(config, target)
    return [skill for skill in config.skills if skill.scope.applies(target.harness, names)]


def render_instructions(config: EmitConfig, harness: str) -> str | None:
    """The one instructions document for ``harness``; None when there is none."""
    parts = []
    for path in (*config.instructions, *config.harness_instructions.get(harness, ())):
        try:
            text = owned.read_text(path, max_bytes=MAX_INSTRUCTIONS_BYTES)
        except owned.OwnedFileError as error:
            raise EmitError(f"instructions: {error}") from None
        if text is None:
            raise EmitError(f"instructions: {path.name} does not exist")
        parts.append(text.strip("\n"))
    parts = [part for part in parts if part]
    if not parts:
        return None
    document = "\n\n".join(parts) + "\n"
    if len(document.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES:
        raise EmitError("instructions: the rendered document is over 64 KiB")
    return document


def hook_command() -> str:
    return shutil.which(HOOK_COMMAND) or HOOK_COMMAND


# Digests and decisions


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_value(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def server_action(
    desired: Any, current: Any, recorded: str | None, *, adoptable: bool, force: bool
) -> str:
    """What to do with one keyed server entry.

    ``desired`` None means not wanted here, ``current`` None means absent,
    ``recorded`` is the manifest digest. Returns one of: keep, add, replace,
    adopt, adopt-replace, remove, forget, foreign, drifted, none.
    """
    if desired is None:
        if recorded is None:
            return "none"
        if current is None:
            return "forget"
        if force or digest_value(current) == recorded:
            return "remove"
        return "drifted"
    if current is None:
        return "add"
    if recorded is None:
        if adoptable:
            return "adopt" if current == desired else "adopt-replace"
        return "foreign"
    if current == desired:
        return "keep"
    if force or digest_value(current) == recorded:
        return "replace"
    return "drifted"


def decide_server(
    plan: EmitPlan, path: Path, desired: Any, current: Any,
    record: Mapping[str, str] | None, *, adoptable: bool, force: bool,
) -> str:
    """``server_action`` for a manifest record, adopting a matching pending value.

    A forced replacement or removal of a drifted entry asks the engine to
    back up ``path``, the file that holds it, first.
    """
    recorded = None if record is None else record.get("sha256")
    if record is not None and PENDING_KEY in record:
        pending, recorded = recorded, record.get(PREVIOUS_KEY)
        if current is not None and digest_value(current) == pending:
            recorded = pending
    action = server_action(desired, current, recorded, adoptable=adoptable, force=force)
    if (
        action in ("replace", "remove") and current is not None and recorded is not None
        and digest_value(current) != recorded
    ):
        plan.backups.append(Path(path))
    return action


def is_recorded(record: Mapping[str, str] | None) -> bool:
    """True for a record of a value that was applied (not merely pending)."""
    return record is not None and PENDING_KEY not in record


def report_action(
    plan: EmitPlan, label: str, action: str, *, path: Path, key: str, recorded: bool,
) -> None:
    """Report one keyed server entry: notes, conflicts and its Item for the engine."""
    if action == "foreign":
        plan.conflict(
            path, f"{label}: exists and was not written by tongs-agents; kept it unchanged", key,
        )
        return
    detail = ""
    if action == "drifted":
        detail = "changed since tongs-agents wrote it; left as is (use --force to replace)"
        plan.notes.append(f"{label}: {detail}")
    elif action in ("adopt", "adopt-replace"):
        plan.notes.append(f"{label}: adopted the entry an earlier install wrote")
    elif action == "remove":
        detail = "no longer in the dotfiles; removed"
    elif action == "forget":
        detail = "no longer in the dotfiles; already removed"
    if action == "add":
        state = MISSING if recorded else PENDING
    else:
        state = _ACTION_STATES.get(action)
    if state is not None:
        plan.items.append(Item(Path(path), key, state, detail))


# Owned files and shared text


def plan_owned_tree(
    plan: EmitPlan,
    files: Mapping[Path, tuple[str, Any]],
    *,
    previous: Mapping[str, Mapping[str, str]],
    force: bool,
) -> None:
    """Plan owned files (path to (format, data)) and remove recorded ones no longer wanted."""
    for path, (fmt, data) in sorted(files.items()):
        entry_id = f"file:{path}"
        recorded = previous.get(entry_id)
        try:
            change = owned.plan_owned_file(
                path, data, owner=OWNER, schema_version=SCHEMA_VERSION, fmt=fmt
            )
        except owned.OwnedFileError as error:
            plan.conflict(path, str(error))
            continue
        if change.kind == "conflict":
            plan.conflicts.append(f"{path}: exists without the tongs-agents marker; not replaced")
            plan.changes.append(change)
            continue
        if (
            change.kind == "update" and recorded is not None and not force
            and recorded.get("sha256") != change.before_sha256
        ):
            detail = "changed since tongs-agents wrote it; left as is (use --force to replace)"
            plan.notes.append(f"{path}: {detail}")
            plan.items.append(Item(path, None, DRIFTED, detail))
            plan.entries[entry_id] = dict(recorded)
            continue
        plan.changes.append(change)
        plan.entries[entry_id] = {"kind": "file", "path": str(path),
                                  "sha256": change.after_sha256 or ""}
    for entry_id, recorded in sorted(previous.items()):
        if recorded.get("kind") != "file" or entry_id in plan.entries:
            continue
        path = Path(recorded.get("path", ""))
        if not path.is_absolute():
            continue
        try:
            change = owned.plan_delete_owned(path, owner=OWNER, fmt=_recorded_format(path))
        except (owned.OwnedFileError, ValueError) as error:
            plan.notes.append(f"{path}: kept ({error})")
            continue
        if change.kind == "unchanged":
            continue
        if change.kind == "conflict":
            plan.notes.append(f"{path}: no longer carries the tongs-agents marker; kept it")
            continue
        if not force and change.before_sha256 != recorded.get("sha256"):
            plan.notes.append(f"{path}: changed since tongs-agents wrote it; kept it")
            plan.entries[entry_id] = dict(recorded)
            continue
        plan.changes.append(change)


def _recorded_format(path: Path) -> str:
    return SKILL_FORMATS.get(path.suffix.lower()) or owned.file_format(path)


def _find_block(text: str) -> tuple[int, int, int, int] | None | str:
    """(block start, inner start, inner end, block end), None, or a problem."""
    begins = [match.start() for match in re.finditer(re.escape(BLOCK_BEGIN), text)]
    ends = [match.start() for match in re.finditer(re.escape(BLOCK_END), text)]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        return "has unmatched or repeated tongs-agents block markers"
    begin, end = begins[0], ends[0]
    start = text.rfind("\n", 0, begin) + 1
    inner_start = text.find("\n", begin)
    inner_start = len(text) if inner_start < 0 else inner_start + 1
    if inner_start > end:
        return "has the tongs-agents block markers on one line"
    inner_end = text.rfind("\n", 0, end) + 1
    block_end = text.find("\n", end)
    block_end = len(text) if block_end < 0 else block_end + 1
    if text[start:begin].strip() or text[end + len(BLOCK_END):block_end].strip():
        return "has text on a tongs-agents block marker line"
    return start, inner_start, inner_end, block_end


def _separator(text: str) -> str:
    """What goes between existing text and an appended block: one blank line."""
    if not text or text.endswith("\n\n"):
        return ""
    return "\n" if text.endswith("\n") else "\n\n"


def plan_markdown_block(
    plan: EmitPlan,
    path: Path,
    body: str | None,
    *,
    previous: Mapping[str, Mapping[str, str]],
    force: bool,
) -> None:
    """Our block in a shared Markdown file: set to ``body``, or removed for None."""
    entry_id = f"md-block:{path}"
    recorded = previous.get(entry_id)
    try:
        text = owned.read_text(path) or ""
    except owned.OwnedFileError as error:
        plan.conflict(path, str(error))
        return
    found = _find_block(text)
    if isinstance(found, str):
        plan.conflict(path, f"{path}: {found}; not changed")
        return
    current = None if found is None else text[found[1]:found[2]]
    drifted = (
        current is not None and recorded is not None
        and current != body and digest_text(current) != recorded.get("sha256")
    )
    if drifted and force:
        plan.backups.append(path)
    elif drifted:
        detail = "the tongs-agents block was edited; left as is (use --force to replace)"
        plan.notes.append(f"{path}: {detail}")
        plan.items.append(Item(path, "tongs-agents block", DRIFTED, detail))
        plan.entries[entry_id] = dict(recorded)
        return

    def mutate(text: str) -> str | None:
        if found is None:
            if body is None:
                return text
            return f"{text}{_separator(text)}{BLOCK_BEGIN}\n{body}{BLOCK_END}\n"
        start, _, _, block_end = found
        if body is None:
            before = text[:start]
            after = text[block_end:]
            if before.endswith("\n\n") and not after.strip():
                before = before[:-1]
            remaining = before + after
            return None if not remaining.strip() else remaining
        return f"{text[:start]}{BLOCK_BEGIN}\n{body}{BLOCK_END}\n{text[block_end:]}"

    change = owned.plan_text_edit(path, mutate)
    if change.kind != "unchanged" or body is not None:
        plan.changes.append(change)
    if body is not None:
        plan.entries[entry_id] = {"kind": "md-block", "path": str(path),
                                  "sha256": digest_text(body)}


def plan_import_line(plan: EmitPlan, path: Path, line: str, wanted: bool) -> None:
    """One exact ``@`` import line in a shared Markdown file."""
    entry_id = f"md-import:{path}"

    def mutate(text: str) -> str | None:
        lines = text.splitlines(keepends=True)
        present = any(item.rstrip("\r\n") == line for item in lines)
        if wanted:
            if present:
                return text
            return f"{text}{_separator(text)}{line}\n"
        if not present:
            return text
        remaining = "".join(item for item in lines if item.rstrip("\r\n") != line)
        return None if not remaining.strip() else remaining

    try:
        change = owned.plan_text_edit(path, mutate)
    except owned.OwnedFileError as error:
        plan.conflict(path, str(error))
        return
    if change.kind != "unchanged" or wanted:
        plan.changes.append(change)
    if wanted:
        plan.entries[entry_id] = {"kind": "md-import", "path": str(path),
                                  "sha256": digest_text(line)}


def read_mcp_servers(path: Path) -> dict[str, Any]:
    """The ``mcpServers`` object of a harness JSON file; {} when the file is absent.

    Raises OwnedFileError for a file that cannot be read or parsed, so an
    unreadable file is a conflict, never mistaken for one without servers.
    """
    # The harness CLI does the writing, so this read-only parse follows a
    # symlinked file (dotfiles setups link .claude.json); only writes refuse links.
    path = Path(os.path.realpath(path))
    document = owned.read_json(path, missing={}, max_bytes=owned.HARNESS_FILE_MAX_BYTES)
    servers = document.get("mcpServers", {}) if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        raise owned.OwnedFileError(f"{path}: 'mcpServers' must be a JSON object")
    return servers


def read_block(path: Path) -> str | None:
    try:
        text = owned.read_text(path)
    except owned.OwnedFileError:
        return None
    if text is None:
        return None
    found = _find_block(text)
    if found is None or isinstance(found, str):
        return None
    return text[found[1]:found[2]]


def read_owned_text(path: Path, fmt: str) -> str | None:
    """The content of one owned file without its marker; None when absent or not ours."""
    try:
        current = owned.read_owned_file(path, owner=OWNER, fmt=fmt)
    except (owned.OwnedFileError, ValueError):
        return None
    if current is None:
        return None
    if fmt == "json":
        content = {key: value for key, value in current.content.items()
                   if key not in owned.MARKER_KEYS}
        return json.dumps(content, indent=2, sort_keys=True) + "\n"
    return current.content


def capture_skill_tree(base: Path) -> dict[str, dict[str, str]]:
    """Owned skill files under ``base`` (``<base>/<skill>/...``), by skill and path."""
    skills: dict[str, dict[str, str]] = {}
    if not base.is_dir() or base.is_symlink():
        return skills
    for child in sorted(base.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        files = {}
        for directory, dirnames, filenames in os.walk(child):
            dirnames[:] = sorted(name for name in dirnames
                                 if not (Path(directory) / name).is_symlink())
            for filename in sorted(filenames):
                full = Path(directory) / filename
                fmt = SKILL_FORMATS.get(full.suffix.lower())
                if fmt is None:
                    continue
                text = read_owned_text(full, fmt)
                if text is not None:
                    files[full.relative_to(child).as_posix()] = text
        if "SKILL.md" in files:
            skills[child.name] = files
    return skills


def env_names_from(env: Any, label: str, warnings: list[str]) -> list[str]:
    """Variable names from a harness ``env`` table: ``${NAME}`` only, never values."""
    names = []
    if not isinstance(env, Mapping):
        return names
    for key, value in env.items():
        if isinstance(value, str) and value == f"${{{key}}}":
            names.append(key)
        else:
            warnings.append(f"{label}: skipped the literal value of {key}")
    return names


def fragment(target: ProfileTarget, data: dict[str, Any]) -> CanonicalFragment:
    return CanonicalFragment(harness=target.harness, target=target,
                             data=MappingProxyType(data))


# Reference apply


def _command_env(step: CommandStep) -> dict[str, str]:
    environment = os.environ.copy()
    for key, value in step.env.items():
        if value is None:
            environment.pop(key, None)
        else:
            environment[key] = value
    return environment


def run_step(step: CommandStep, runner: Callable[..., Any] = subprocess.run) -> tuple[bool, str]:
    if step.call is not None:
        try:
            return bool(step.call()), ""
        except (OSError, ValueError) as error:
            return False, str(error)
    try:
        result = runner(list(step.argv), capture_output=True, text=True,
                        timeout=step.timeout, env=_command_env(step))
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    if result.returncode != 0:
        lines = (result.stderr or "").strip().splitlines()
        return False, lines[-1] if lines else f"exit {result.returncode}"
    return True, ""


@dataclass
class ApplyResult:
    changes: list[Change]
    failed_steps: list[tuple[str, str]]


def apply_plan(plan: EmitPlan, runner: Callable[..., Any] = subprocess.run) -> ApplyResult:
    """Apply one plan: files and manifest atomically, then CLI steps, then their entries."""
    changes = owned.ChangeSet()
    for change in plan.changes:
        changes.add(change)
    changes.record_managed_paths(plan.target, plan.managed_paths)
    changes.record_entries(plan.target, plan.file_entries())
    committed = changes.commit()
    failures = []
    succeeded = []
    for step in plan.commands:
        ok, detail = run_step(step, runner)
        if ok:
            succeeded.append(step)
        else:
            failures.append((step.description, detail))
    entries = plan.entries_after(succeeded)
    if entries != plan.file_entries():
        follow_up = owned.ChangeSet()
        follow_up.record_entries(plan.target, entries)
        committed += follow_up.commit()
    return ApplyResult(committed, failures)
