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
from tongs_agents.config_layer.protocol import CanonicalFragment, Change, ProfileTarget
from tongs_agents.profiles import CONFIG_DIR_ENV_BY_HARNESS, Profile, ProfileError, parse_profile

OWNER = owned.OWNER
SCHEMA_VERSION = 1
BUNDLE = "tongs-agents"
RESERVED_SERVER = "tongs-agents"
HOOK_COMMAND = "tongs-agents-hook"
BLOCK_BEGIN = "<!-- tongs-agents:begin -->"
BLOCK_END = "<!-- tongs-agents:end -->"
DOTFILES_SCHEMA = "tongs-agents-dotfiles/1"
EMIT_HARNESSES = ("claude", "codex", "pi")
MAX_INSTRUCTIONS_BYTES = 64 * 1024
MAX_SKILL_FILES = 256
MAX_SKILL_BYTES = 4 * 1024 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*\Z")
# Suffix to owned.py marker format for files inside a skill directory.
SKILL_FORMATS = {
    ".md": "md", ".json": "json", ".toml": "toml", ".kdl": "kdl",
    ".py": "hash", ".sh": "hash", ".bash": "hash", ".yaml": "hash", ".yml": "hash",
    ".js": "slash", ".mjs": "slash", ".cjs": "slash", ".ts": "slash",
}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")


class EmitError(ValueError):
    """The canonical config cannot be emitted; the message names the item."""


@dataclass(frozen=True)
class Scope:
    """``harnesses`` and ``profiles`` narrowing; None means every one."""

    harnesses: tuple[str, ...] | None = None
    profiles: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Skill:
    name: str
    source: Path
    scope: Scope = Scope()


@dataclass(frozen=True)
class McpServer:
    name: str
    command: tuple[str, ...] | None = None
    cwd: str | None = None
    env: tuple[str, ...] = ()
    url: str | None = None
    bearer_token_env: str | None = None
    enabled: bool = True
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
        """Build from a parsed ``agentic.toml`` and the directory that holds it.

        ``profiles`` (name to Profile or profile table) replaces the
        document's own; the engine passes the merged set. Validation here is
        what emission needs; the schema loader owns the full checks.
        """
        return _from_document(document, Path(root), profiles)


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


# Canonical config


def coerce_config(config: Any) -> EmitConfig:
    """An EmitConfig from what the engine passes.

    Accepted: an EmitConfig; an object with ``to_emit_config()``; or an
    object with ``root`` (or ``source``) and ``document`` (or ``data``),
    optionally with ``profiles``.
    """
    if isinstance(config, EmitConfig):
        return config
    convert = getattr(config, "to_emit_config", None)
    if callable(convert):
        result = convert()
        if isinstance(result, EmitConfig):
            return result
    root = getattr(config, "root", None) or getattr(config, "source", None)
    document = getattr(config, "document", None)
    if document is None:
        document = getattr(config, "data", None)
    if root is not None and isinstance(document, Mapping):
        return EmitConfig.from_document(
            document, Path(root), profiles=getattr(config, "profiles", None)
        )
    raise TypeError(f"cannot emit from a {type(config).__name__}")


def _inside_root(root: Path, value: Any, where: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise EmitError(f"{where}: must be a non-empty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or value.startswith("~"):
        raise EmitError(f"{where}: must stay inside the dotfiles directory")
    path = root / relative
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if os.path.commonpath([real_root, real]) != real_root:
        raise EmitError(f"{where}: leaves the dotfiles directory through a symlink")
    return path


def _string_tuple(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise EmitError(f"{where}: must be a list of strings")
    return tuple(value)


def _scope(table: Mapping[str, Any], where: str, profiles: Mapping[str, Profile]) -> Scope:
    harnesses = profiles_list = None
    if "harnesses" in table:
        harnesses = _string_tuple(table["harnesses"], f"{where}.harnesses")
        unknown = [name for name in harnesses if name not in EMIT_HARNESSES]
        if unknown:
            raise EmitError(f"{where}.harnesses: unknown harness {unknown[0]!r}")
    if "profiles" in table:
        profiles_list = _string_tuple(table["profiles"], f"{where}.profiles")
        unknown = [name for name in profiles_list if name not in profiles]
        if unknown:
            raise EmitError(f"{where}.profiles: unknown profile {unknown[0]!r}")
    return Scope(harnesses, profiles_list)


def _table(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EmitError(f"{where}: must be a table")
    return value


def _profiles(document: Mapping[str, Any], override: Mapping[str, Any] | None) -> dict:
    source = override if override is not None else document.get("profiles", {})
    result = {}
    for name, value in _table(source, "profiles").items():
        if isinstance(value, Profile):
            result[name] = value
            continue
        try:
            result[name] = parse_profile(name, dict(_table(value, f"profiles.{name}")))
        except ProfileError as error:
            raise EmitError(str(error)) from None
    return result


def _front_matter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        key, separator, value = line.partition(":")
        if separator and key and not key[0].isspace():
            values[key.strip()] = value.strip().strip("'\"")
    return {}


def _check_skill(path: Path, name: str) -> None:
    where = f"skills: {name}"
    if not SKILL_NAME_RE.match(name):
        raise EmitError(f"{where}: name must match {SKILL_NAME_RE.pattern}")
    files = skill_files(path, name)
    skill_md = next((text for relative, _, text in files if relative == "SKILL.md"), None)
    if skill_md is None:
        raise EmitError(f"{where}: SKILL.md is missing")
    front = _front_matter(skill_md)
    if front.get("name") != name:
        raise EmitError(f"{where}: SKILL.md front matter name must be {name!r}")
    if not front.get("description"):
        raise EmitError(f"{where}: SKILL.md front matter needs a description")


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


def _from_document(
    document: Mapping[str, Any], root: Path, profiles_override: Mapping[str, Any] | None
) -> EmitConfig:
    schema = document.get("schema")
    if schema is not None and schema != DOTFILES_SCHEMA:
        raise EmitError(f"schema: expected {DOTFILES_SCHEMA!r}, got {schema!r}")
    root = Path(os.path.abspath(root))
    profiles = _profiles(document, profiles_override)

    section = _table(document.get("instructions", {}), "instructions")
    instructions = tuple(
        _inside_root(root, item, "instructions.files")
        for item in _string_tuple(section.get("files", []), "instructions.files")
    )
    per_harness = {}
    for harness, table in _table(section.get("harness", {}), "instructions.harness").items():
        if harness not in EMIT_HARNESSES:
            raise EmitError(f"instructions.harness.{harness}: unknown harness")
        where = f"instructions.harness.{harness}.files"
        per_harness[harness] = tuple(
            _inside_root(root, item, where)
            for item in _string_tuple(_table(table, where).get("files", []), where)
        )

    section = _table(document.get("skills", {}), "skills")
    overrides = _table(section.get("overrides", {}), "skills.overrides")
    skills = {}
    for item in _string_tuple(section.get("dirs", []), "skills.dirs"):
        directory = _inside_root(root, item, "skills.dirs")
        if not directory.is_dir():
            raise EmitError(f"skills.dirs: {item} is not a directory")
        for child in sorted(directory.iterdir()):
            if child.is_symlink() or not child.is_dir() or not (child / "SKILL.md").is_file():
                continue
            if child.name in skills:
                raise EmitError(f"skills: two skills are named {child.name!r}")
            _check_skill(child, child.name)
            scope = _scope(
                _table(overrides.get(child.name, {}), f"skills.overrides.{child.name}"),
                f"skills.overrides.{child.name}", profiles,
            )
            skills[child.name] = Skill(child.name, child, scope)
    for name in overrides:
        if name not in skills:
            raise EmitError(f"skills.overrides.{name}: no such skill")

    servers = []
    for name, table in _table(document.get("mcp_servers", {}), "mcp_servers").items():
        servers.append(_server(name, _table(table, f"mcp_servers.{name}"), profiles))
    return EmitConfig(
        root=root,
        instructions=instructions,
        harness_instructions=MappingProxyType(per_harness),
        skills=tuple(skills.values()),
        mcp_servers=tuple(servers),
        profiles=MappingProxyType(profiles),
    )


def _server(name: str, table: Mapping[str, Any], profiles: Mapping[str, Profile]) -> McpServer:
    where = f"mcp_servers.{name}"
    if not NAME_RE.match(name):
        raise EmitError(f"{where}: name must match {NAME_RE.pattern}")
    if name == RESERVED_SERVER:
        raise EmitError(f"{where}: the name {RESERVED_SERVER} is reserved")
    if ("command" in table) == ("url" in table):
        raise EmitError(f"{where}: needs exactly one of command or url")
    command = None
    if "command" in table:
        command = _string_tuple(table["command"], f"{where}.command")
        if not command or not command[0]:
            raise EmitError(f"{where}.command: must start with a non-empty command")
    env = table.get("env", [])
    if isinstance(env, Mapping):
        raise EmitError(
            f"{where}.env: list variable names only; values belong in the agent's environment"
        )
    env = _string_tuple(env, f"{where}.env")
    for item in env:
        if not ENV_NAME_RE.match(item):
            raise EmitError(f"{where}.env: {item!r} is not a variable name")
    url = table.get("url")
    if url is not None:
        if not isinstance(url, str) or not (
            url.startswith("https://")
            or any(url.startswith(f"http://{host}") for host in _LOCAL_HOSTS)
        ):
            raise EmitError(f"{where}.url: must be https://, or http:// on localhost")
    bearer = table.get("bearer_token_env")
    if bearer is not None and (not isinstance(bearer, str) or not ENV_NAME_RE.match(bearer)):
        raise EmitError(f"{where}.bearer_token_env: must be a variable name")
    cwd = table.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not (
        cwd.startswith("~/") or os.path.isabs(cwd))):
        raise EmitError(f"{where}.cwd: must be absolute or start with ~/")
    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise EmitError(f"{where}.enabled: must be true or false")
    return McpServer(name, command, cwd, env, url, bearer, enabled, _scope(table, where, profiles))


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


def _normal_key(harness: str, directory: Path | None) -> tuple[str, Path | None]:
    if directory is None:
        return harness, None
    directory = Path(os.path.abspath(Path(directory).expanduser()))
    # Claude keeps .claude.json beside its default dir, so an explicit
    # ~/.claude is a different home; Codex and Pi have one file layout.
    if harness != "claude" and directory == default_dir(harness):
        return harness, None
    return harness, directory


def profile_target(profile: Profile) -> ProfileTarget | None:
    """The target a profile's harness uses, or None (harness other, bad env)."""
    variable = CONFIG_DIR_ENV_BY_HARNESS.get(profile.harness)
    if variable is None:
        return None
    directory = profile.config_dir
    if directory is None:
        value = profile.env.get(variable)
        if value is not None:
            if not os.path.isabs(value):
                return None
            directory = Path(value)
    return ProfileTarget(*_normal_key(profile.harness, directory))


def profiles_for(config: EmitConfig, target: ProfileTarget) -> tuple[str, ...]:
    wanted = _normal_key(target.harness, target.config_dir)
    return tuple(sorted(
        name for name, profile in config.profiles.items()
        if (found := profile_target(profile)) is not None
        and (found.harness, found.config_dir) == wanted
    ))


def applies(scope: Scope, harness: str, profile_names: Iterable[str]) -> bool:
    if scope.harnesses is not None and harness not in scope.harnesses:
        return False
    if scope.profiles is not None and not set(scope.profiles) & set(profile_names):
        return False
    return True


def expand_home(value: str) -> str:
    if value == "~" or value.startswith("~/"):
        return str(Path.home()) + value[1:]
    return value


def selected_servers(config: EmitConfig, target: ProfileTarget) -> list[McpServer]:
    names = profiles_for(config, target)
    return [
        server for server in config.mcp_servers
        if server.enabled and applies(server.scope, target.harness, names)
    ]


def selected_skills(config: EmitConfig, target: ProfileTarget) -> list[Skill]:
    names = profiles_for(config, target)
    return [skill for skill in config.skills if applies(skill.scope, target.harness, names)]


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


def report_action(plan: EmitPlan, label: str, action: str) -> None:
    if action == "foreign":
        plan.conflicts.append(
            f"{label}: exists and was not written by tongs-agents; kept it unchanged"
        )
    elif action == "drifted":
        plan.notes.append(
            f"{label}: changed since tongs-agents wrote it; left as is (use --force to replace)"
        )
    elif action in ("adopt", "adopt-replace"):
        plan.notes.append(f"{label}: adopted the entry an earlier install wrote")


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
            plan.conflicts.append(str(error))
            continue
        if change.kind == "conflict":
            plan.conflicts.append(f"{path}: exists without the tongs-agents marker; not replaced")
            plan.changes.append(change)
            continue
        if (
            change.kind == "update" and recorded is not None and not force
            and recorded.get("sha256") != change.before_sha256
        ):
            plan.notes.append(
                f"{path}: changed since tongs-agents wrote it; left as is (use --force to replace)"
            )
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
        plan.conflicts.append(str(error))
        return
    found = _find_block(text)
    if isinstance(found, str):
        plan.conflicts.append(f"{path}: {found}; not changed")
        return
    current = None if found is None else text[found[1]:found[2]]
    if (
        current is not None and recorded is not None and not force
        and current != body and digest_text(current) != recorded.get("sha256")
    ):
        plan.notes.append(
            f"{path}: the tongs-agents block was edited; left as is (use --force to replace)"
        )
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
    if change.kind != "unchanged":
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
        plan.conflicts.append(str(error))
        return
    if change.kind != "unchanged":
        plan.changes.append(change)
    if wanted:
        plan.entries[entry_id] = {"kind": "md-import", "path": str(path),
                                  "sha256": digest_text(line)}


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
    entries = plan.file_entries()
    for step in plan.commands:
        ok, detail = run_step(step, runner)
        if not ok:
            failures.append((step.description, detail))
            continue
        if step.entry_id is None:
            continue
        if step.entry is None:
            entries.pop(step.entry_id, None)
        else:
            entries[step.entry_id] = dict(step.entry)
    if entries != plan.file_entries():
        follow_up = owned.ChangeSet()
        follow_up.record_entries(plan.target, entries)
        committed += follow_up.commit()
    return ApplyResult(committed, failures)
