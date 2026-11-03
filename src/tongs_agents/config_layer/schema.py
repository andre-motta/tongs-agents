"""The canonical config loader: ``agentic.toml`` in schema ``tongs-agents-dotfiles/1``.

A dotfiles directory holds ``agentic.toml`` plus the instruction files and
skill directories it names (docs/work/dotfiles-schema-draft.md). It is chosen
by ``--source`` or by ``[dotfiles] path`` in the tongs-agents config file.
Validation is stdlib only, refuses unknown keys at every level and names the
offending key in each error. Every path stays inside the dotfiles directory.

Profiles come from ``agentic.toml`` with the M1 rules; a profile of the same
name in ``config.toml`` replaces it as a local override.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from tongs_agents import cli
from tongs_agents import profiles as profiles_module
from tongs_agents.config_layer.protocol import ProfileTarget
from tongs_agents.profiles import Profile, ProfileError

SCHEMA_ID = "tongs-agents-dotfiles/1"
CANONICAL_FILE_NAME = "agentic.toml"
EMITTED_HARNESSES: tuple[str, ...] = ("claude", "codex", "pi")
RESERVED_SERVER_NAME = "tongs-agents"
NAME_RE = profiles_module.PROFILE_NAME_RE
ENV_NAME_RE = profiles_module.ENV_KEY_RE
SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
MAX_INSTRUCTIONS_BYTES = 64 * 1024
MAX_SKILL_FILES = 256
MAX_SKILL_BYTES = 4 * 1024 * 1024
SKILL_FILE = "SKILL.md"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]")

TOP_KEYS = frozenset({"schema", "profiles", "instructions", "skills", "mcp_servers", "layouts"})
INSTRUCTION_KEYS = frozenset({"files", "harness"})
SKILLS_KEYS = frozenset({"dirs", "overrides"})
SCOPE_KEYS = frozenset({"harnesses", "profiles"})
SERVER_KEYS = frozenset({
    "command", "cwd", "env", "url", "bearer_token_env", "enabled", "harnesses", "profiles",
})
LAYOUT_KEYS = frozenset({"session", "profiles", "tongs"})
DOTFILES_KEYS = frozenset({"path"})

_FRONT_MATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*$", re.DOTALL | re.MULTILINE)


class SchemaError(ValueError):
    """The dotfiles source is missing or invalid; the message names the key."""


class NoSourceError(SchemaError):
    """No dotfiles directory was given or configured."""


@dataclass(frozen=True)
class Scope:
    """The ``harnesses`` and ``profiles`` narrowing of one item; None is no limit."""

    harnesses: tuple[str, ...] | None = None
    profiles: tuple[str, ...] | None = None

    def applies(self, harness: str, profile_names: Iterable[str] = ()) -> bool:
        if self.harnesses is not None and harness not in self.harnesses:
            return False
        if self.profiles is not None:
            return any(name in self.profiles for name in profile_names)
        return True


@dataclass(frozen=True)
class Skill:
    """One skill directory; ``files`` are POSIX paths relative to ``path``."""

    name: str
    path: Path
    files: tuple[str, ...]
    scope: Scope = Scope()

    def read_files(self) -> dict[str, bytes]:
        return {name: (self.path / name).read_bytes() for name in self.files}


@dataclass(frozen=True)
class McpServer:
    """One MCP server. ``env`` holds variable names only, never values."""

    name: str
    command: tuple[str, ...] | None = None
    cwd: str | None = None
    env: tuple[str, ...] = ()
    url: str | None = None
    bearer_token_env: str | None = None
    enabled: bool = True
    scope: Scope = Scope()

    def to_table(self) -> dict:
        """The server as schema v1 keys, without scope, for hashing and capture."""
        table: dict = {}
        if self.command is not None:
            table["command"] = list(self.command)
        if self.cwd is not None:
            table["cwd"] = self.cwd
        if self.env:
            table["env"] = list(self.env)
        if self.url is not None:
            table["url"] = self.url
        if self.bearer_token_env is not None:
            table["bearer_token_env"] = self.bearer_token_env
        if not self.enabled:
            table["enabled"] = False
        return table


@dataclass(frozen=True)
class Layout:
    name: str
    session: str
    profiles: tuple[str, ...]
    tongs: bool = True


@dataclass(frozen=True)
class InstructionFile:
    path: str
    text: str


@dataclass(frozen=True)
class Instructions:
    files: tuple[InstructionFile, ...] = ()
    harness: Mapping[str, tuple[InstructionFile, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True)
class CanonicalConfig:
    """A validated dotfiles directory. Emitters read it; nothing here writes."""

    root: Path
    source: Path
    profiles: Mapping[str, Profile]
    instructions: Instructions = Instructions()
    skills: Mapping[str, Skill] = field(default_factory=lambda: MappingProxyType({}))
    mcp_servers: Mapping[str, McpServer] = field(default_factory=lambda: MappingProxyType({}))
    layouts: Mapping[str, Layout] = field(default_factory=lambda: MappingProxyType({}))

    def profiles_for(self, target: ProfileTarget) -> tuple[str, ...]:
        """The profiles whose harness config home is ``target``."""
        names = []
        for profile in self.profiles.values():
            harness, directory, problem = cli.profile_home(profile)
            if harness != target.harness or problem is not None:
                continue
            home = cli._make_target(harness, directory, "profile").directory
            wanted = target.config_dir
            if wanted is not None:
                wanted = cli._make_target(harness, wanted, "target").directory
            if home == wanted:
                names.append(profile.name)
        return tuple(names)

    def skills_for_target(self, target: ProfileTarget) -> tuple[Skill, ...]:
        return self.skills_for(target.harness, self.profiles_for(target))

    def mcp_servers_for_target(
        self, target: ProfileTarget, *, include_disabled: bool = False
    ) -> tuple[McpServer, ...]:
        return self.mcp_servers_for(
            target.harness, self.profiles_for(target), include_disabled=include_disabled,
        )

    def instructions_for(self, harness: str) -> str:
        """The one rendered instruction document for ``harness``; '' when none."""
        return render_instructions(
            self.instructions.files + self.instructions.harness.get(harness, ())
        )

    def skills_for(self, harness: str, profile_names: Iterable[str] = ()) -> tuple[Skill, ...]:
        names = tuple(profile_names)
        return tuple(
            skill for skill in self.skills.values() if skill.scope.applies(harness, names)
        )

    def mcp_servers_for(
        self, harness: str, profile_names: Iterable[str] = (), *, include_disabled: bool = False
    ) -> tuple[McpServer, ...]:
        names = tuple(profile_names)
        return tuple(
            server for server in self.mcp_servers.values()
            if server.scope.applies(harness, names) and (include_disabled or server.enabled)
        )


def render_instructions(files: Iterable[InstructionFile]) -> str:
    """Files in order, separated by one blank line, ending with one newline."""
    parts = [item.text.strip("\n") for item in files]
    parts = [part for part in parts if part]
    return "\n\n".join(parts) + "\n" if parts else ""


def _read_limited(path: Path) -> bytes | None:
    try:
        return profiles_module._read_config_bytes(path)
    except ProfileError as error:
        raise SchemaError(str(error)) from None


def _parse_toml(path: Path, raw: bytes) -> dict:
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        raise SchemaError(f"{path}: not valid UTF-8") from None
    except tomllib.TOMLDecodeError as error:
        raise SchemaError(f"{path}: invalid TOML: {error}") from None


def _user_path(value, where: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise SchemaError(f"{where}: must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise SchemaError(f"{where}: must be an absolute path (or start with ~/)")
    return path


def dotfiles_path(config_file: Path | None = None) -> Path | None:
    """``[dotfiles] path`` from the tongs-agents config file, or None."""
    path = profiles_module.profiles_path() if config_file is None else config_file
    raw = _read_limited(path)
    if raw is None:
        return None
    section = _parse_toml(path, raw).get("dotfiles")
    if section is None:
        return None
    where = f"{path}: dotfiles"
    if not isinstance(section, dict):
        raise SchemaError(f"{where}: must be a table")
    unknown = sorted(set(section) - DOTFILES_KEYS)
    if unknown:
        raise SchemaError(f"{where}.{unknown[0]}: unknown key")
    if "path" not in section:
        return None
    return _user_path(section["path"], f"{where}.path")


def resolve_source(source: Path | str | None = None) -> Path:
    """The ``agentic.toml`` to load: ``source`` (a directory or the file) or the configured one."""
    if source is None:
        configured = dotfiles_path()
        if configured is None:
            raise NoSourceError(
                "no dotfiles source: pass --source DIR or set [dotfiles] path in "
                f"{profiles_module.profiles_path()}"
            )
        source = configured
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(os.path.abspath(path))
    if path.is_dir() and not path.is_symlink():
        path = path / CANONICAL_FILE_NAME
    return path


def _table(value, where: str, keys: frozenset[str]) -> dict:
    if not isinstance(value, dict):
        raise SchemaError(f"{where}: must be a table")
    unknown = sorted(set(value) - keys)
    if unknown:
        raise SchemaError(f"{where}.{unknown[0]}: unknown key")
    return value


def _strings(value, where: str, *, non_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SchemaError(f"{where}: must be a list of strings")
    if non_empty and (not value or not value[0]):
        raise SchemaError(f"{where}: must start with a non-empty string")
    for index, item in enumerate(value):
        if "\0" in item:
            raise SchemaError(f"{where}[{index}]: contains a NUL character")
    return tuple(value)


def _bool(value, where: str) -> bool:
    if not isinstance(value, bool):
        raise SchemaError(f"{where}: must be true or false")
    return value


def _name(name: str, where: str) -> str:
    if not NAME_RE.match(name):
        raise SchemaError(f"{where}: name must match {NAME_RE.pattern}")
    return name


def _env_name(value, where: str) -> str:
    if not isinstance(value, str) or not ENV_NAME_RE.match(value):
        raise SchemaError(f"{where}: must be an environment variable name matching "
                          f"{ENV_NAME_RE.pattern}")
    return value


def _inside(root: Path, value, where: str) -> Path:
    """``value`` as a path under ``root``; refuses absolute, ``..`` and escaping links."""
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise SchemaError(f"{where}: must be a relative path string")
    relative = PurePosixPath(value)
    if relative.is_absolute() or value.startswith("~") or ".." in relative.parts:
        raise SchemaError(f"{where}: must be relative to the dotfiles directory, without '..'")
    path = root.joinpath(*relative.parts)
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    if real != real_root and not real.startswith(real_root.rstrip(os.sep) + os.sep):
        raise SchemaError(f"{where}: {value} leaves the dotfiles directory")
    return path


def _scope(table: dict, where: str, profile_names: Iterable[str]) -> Scope:
    harnesses = None
    profiles = None
    if "harnesses" in table:
        harnesses = _strings(table["harnesses"], f"{where}.harnesses")
        for harness in harnesses:
            if harness not in EMITTED_HARNESSES:
                raise SchemaError(
                    f"{where}.harnesses: {harness!r} is not one of {', '.join(EMITTED_HARNESSES)}"
                )
    if "profiles" in table:
        profiles = _strings(table["profiles"], f"{where}.profiles")
        known = set(profile_names)
        for name in profiles:
            if name not in known:
                raise SchemaError(f"{where}.profiles: unknown profile {name!r}")
    return Scope(harnesses, profiles)


def _read_text(path: Path, where: str, limit: int) -> str:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise SchemaError(f"{where}: {path.name} does not exist") from None
    except OSError as error:
        raise SchemaError(f"{where}: cannot read: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise SchemaError(f"{where}: not a regular file")
    if info.st_size > limit:
        raise SchemaError(f"{where}: larger than {limit // 1024} KiB")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SchemaError(f"{where}: not valid UTF-8") from None
    except OSError as error:
        raise SchemaError(f"{where}: cannot read: {error.strerror}") from None


def _instruction_files(root: Path, value, where: str) -> tuple[InstructionFile, ...]:
    files = []
    for index, item in enumerate(_strings(value, where)):
        item_where = f"{where}[{index}]"
        path = _inside(root, item, item_where)
        files.append(InstructionFile(item, _read_text(path, item_where, MAX_INSTRUCTIONS_BYTES)))
    return tuple(files)


def _instructions(root: Path, value) -> Instructions:
    table = _table(value, "instructions", INSTRUCTION_KEYS)
    files = _instruction_files(root, table.get("files", []), "instructions.files")
    per_harness = {}
    harness_table = table.get("harness", {})
    if not isinstance(harness_table, dict):
        raise SchemaError("instructions.harness: must be a table")
    for harness, entry in harness_table.items():
        where = f"instructions.harness.{harness}"
        if harness not in EMITTED_HARNESSES:
            raise SchemaError(f"{where}: harness must be one of {', '.join(EMITTED_HARNESSES)}")
        entry = _table(entry, where, frozenset({"files"}))
        per_harness[harness] = _instruction_files(root, entry.get("files", []), f"{where}.files")
    result = Instructions(files, MappingProxyType(per_harness))
    for harness in EMITTED_HARNESSES:
        rendered = render_instructions(files + per_harness.get(harness, ()))
        if len(rendered.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES:
            raise SchemaError(
                f"instructions: the {harness} document is larger than "
                f"{MAX_INSTRUCTIONS_BYTES // 1024} KiB"
            )
    return result


def _front_matter(text: str) -> dict[str, str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}
    values: dict[str, str] = {}
    current = None
    for line in match.group(1).splitlines():
        if line[:1] in (" ", "\t"):
            if current is not None and line.strip():
                values[current] = (values[current] + " " + line.strip()).strip()
            continue
        key, separator, rest = line.partition(":")
        if not separator:
            current = None
            continue
        current = key.strip()
        rest = rest.strip()
        if rest in (">", "|", ">-", "|-", ">+", "|+"):
            rest = ""
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
            rest = rest[1:-1]
        values[current] = rest
    return values


def _skill_files(path: Path, where: str) -> tuple[str, ...]:
    files: list[str] = []
    total = 0
    pending = [(path, PurePosixPath())]
    while pending:
        directory, prefix = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise SchemaError(f"{where}: cannot read: {error.strerror}") from None
        for entry in entries:
            relative = prefix / entry.name
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise SchemaError(f"{where}: {relative} is a symlink; skills must hold real files")
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), relative))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise SchemaError(f"{where}: {relative} is not a regular file")
            files.append(str(relative))
            total += info.st_size
            if len(files) > MAX_SKILL_FILES:
                raise SchemaError(f"{where}: more than {MAX_SKILL_FILES} files")
            if total > MAX_SKILL_BYTES:
                raise SchemaError(f"{where}: larger than {MAX_SKILL_BYTES // (1024 * 1024)} MiB")
    return tuple(sorted(files))


def _skill(path: Path, where: str) -> Skill:
    name = path.name
    if not SKILL_NAME_RE.match(name):
        raise SchemaError(f"{where}: skill directory name must match {SKILL_NAME_RE.pattern}")
    files = _skill_files(path, where)
    front = _front_matter(_read_text(path / SKILL_FILE, f"{where}/{SKILL_FILE}", MAX_SKILL_BYTES))
    if front.get("name") != name:
        raise SchemaError(f"{where}/{SKILL_FILE}: frontmatter name must equal {name!r}")
    if not front.get("description"):
        raise SchemaError(f"{where}/{SKILL_FILE}: frontmatter needs a non-empty description")
    return Skill(name, path, files)


def _skills(root: Path, value, profile_names: Iterable[str]) -> dict[str, Skill]:
    table = _table(value, "skills", SKILLS_KEYS)
    skills: dict[str, Skill] = {}
    for index, item in enumerate(_strings(table.get("dirs", []), "skills.dirs")):
        where = f"skills.dirs[{index}]"
        directory = _inside(root, item, where)
        if directory.is_symlink() or not directory.is_dir():
            raise SchemaError(f"{where}: {item} is not a directory")
        for child in sorted(directory.iterdir(), key=lambda entry: entry.name):
            child_where = f"{where}: {item}/{child.name}"
            if child.is_symlink():
                if (child / SKILL_FILE).exists():
                    raise SchemaError(f"{child_where} is a symlink; skills must be real directories")
                continue
            if not child.is_dir() or not os.path.lexists(child / SKILL_FILE):
                continue
            skill = _skill(child, child_where)
            if skill.name in skills:
                raise SchemaError(f"{child_where}: a second skill named {skill.name!r}")
            skills[skill.name] = skill
    overrides = table.get("overrides", {})
    if not isinstance(overrides, dict):
        raise SchemaError("skills.overrides: must be a table")
    for name, entry in overrides.items():
        where = f"skills.overrides.{name}"
        if name not in skills:
            raise SchemaError(f"{where}: no skill named {name!r} in skills.dirs")
        scope = _scope(_table(entry, where, SCOPE_KEYS), where, profile_names)
        old = skills[name]
        skills[name] = Skill(old.name, old.path, old.files, scope)
    return skills


def _url(value, where: str) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 33 for char in value):
        raise SchemaError(f"{where}: must be a URL without spaces or control characters")
    scheme, separator, rest = value.partition("://")
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    if host.startswith("["):
        host = host[:host.find("]") + 1]
    else:
        host = host.split(":", 1)[0]
    if not separator or not host:
        raise SchemaError(f"{where}: must be an https:// URL")
    if scheme == "https":
        return value
    if scheme == "http" and host in LOCAL_HOSTS:
        return value
    raise SchemaError(f"{where}: must be https://, or http:// only for {', '.join(LOCAL_HOSTS)}")


def _command_path(value: str, where: str) -> str:
    if "/" in value and not (value.startswith("/") or value.startswith("~/")):
        raise SchemaError(f"{where}: a path must be absolute or start with ~/")
    return value


def _server(name: str, value, profile_names: Iterable[str]) -> McpServer:
    where = f"mcp_servers.{name}"
    _name(name, where)
    if name == RESERVED_SERVER_NAME:
        raise SchemaError(f"{where}: the name {name!r} is reserved for the tongs-agents server")
    table = _table(value, where, SERVER_KEYS)
    if ("command" in table) == ("url" in table):
        raise SchemaError(f"{where}: needs exactly one of command (stdio) or url (HTTP)")
    command = cwd = url = bearer = None
    if "command" in table:
        command = _strings(table["command"], f"{where}.command", non_empty=True)
        _command_path(command[0], f"{where}.command[0]")
        if "bearer_token_env" in table:
            raise SchemaError(f"{where}.bearer_token_env: only for url servers")
        if "cwd" in table:
            _user_path(table["cwd"], f"{where}.cwd")
            cwd = table["cwd"]
    else:
        url = _url(table["url"], f"{where}.url")
        if "cwd" in table:
            raise SchemaError(f"{where}.cwd: only for command servers")
        if "bearer_token_env" in table:
            bearer = _env_name(table["bearer_token_env"], f"{where}.bearer_token_env")
    env: tuple[str, ...] = ()
    if "env" in table:
        raw = table["env"]
        if isinstance(raw, dict):
            raise SchemaError(
                f"{where}.env: list variable names only (env = [\"NAME\"]); values belong "
                "in the agent's environment and are never written"
            )
        env = _strings(raw, f"{where}.env")
        for index, item in enumerate(env):
            _env_name(item, f"{where}.env[{index}]")
        if len(set(env)) != len(env):
            raise SchemaError(f"{where}.env: names must be unique")
    enabled = _bool(table.get("enabled", True), f"{where}.enabled")
    scope = _scope(table, where, profile_names)
    return McpServer(name, command, cwd, env, url, bearer, enabled, scope)


def _layout(name: str, value, profile_names: Iterable[str]) -> Layout:
    where = f"layouts.{name}"
    _name(name, where)
    table = _table(value, where, LAYOUT_KEYS)
    session = table.get("session", name)
    if not isinstance(session, str) or not SESSION_RE.match(session):
        raise SchemaError(f"{where}.session: must match {SESSION_RE.pattern}")
    if "profiles" not in table:
        raise SchemaError(f"{where}.profiles: missing (a list of profile names)")
    names = _strings(table["profiles"], f"{where}.profiles")
    if not names:
        raise SchemaError(f"{where}.profiles: must name at least one profile")
    known = set(profile_names)
    for item in names:
        if item not in known:
            raise SchemaError(f"{where}.profiles: unknown profile {item!r}")
    return Layout(name, session, names, _bool(table.get("tongs", True), f"{where}.tongs"))


def _profiles(document: dict, local: Mapping[str, Profile]) -> dict[str, Profile]:
    section = document.get("profiles", {})
    if not isinstance(section, dict):
        raise SchemaError("profiles: must be a table")
    result = {}
    for name, table in section.items():
        try:
            result[name] = profiles_module.parse_profile(name, table)
        except ProfileError as error:
            raise SchemaError(str(error)) from None
    result.update(local)
    return result


def parse_document(
    document: dict, root: Path, source: Path, local_profiles: Mapping[str, Profile] | None = None
) -> CanonicalConfig:
    """Validate a parsed ``agentic.toml`` whose files live under ``root``."""
    if not isinstance(document, dict):
        raise SchemaError("expected a TOML table")
    if "schema" not in document:
        raise SchemaError(f"schema: missing; set schema = \"{SCHEMA_ID}\"")
    if document["schema"] != SCHEMA_ID:
        raise SchemaError(
            f"schema: {document['schema']!r} is not supported; this tongs-agents reads {SCHEMA_ID}"
        )
    unknown = sorted(set(document) - TOP_KEYS)
    if unknown:
        raise SchemaError(f"{unknown[0]}: unknown key")
    profiles = _profiles(document, local_profiles or {})
    names = tuple(profiles)
    instructions = _instructions(root, document.get("instructions", {}))
    skills = _skills(root, document.get("skills", {}), names)
    servers_table = document.get("mcp_servers", {})
    if not isinstance(servers_table, dict):
        raise SchemaError("mcp_servers: must be a table")
    servers = {name: _server(name, value, names) for name, value in servers_table.items()}
    layouts_table = document.get("layouts", {})
    if not isinstance(layouts_table, dict):
        raise SchemaError("layouts: must be a table")
    layouts = {name: _layout(name, value, names) for name, value in layouts_table.items()}
    return CanonicalConfig(
        root=root,
        source=source,
        profiles=MappingProxyType(profiles),
        instructions=instructions,
        skills=MappingProxyType(skills),
        mcp_servers=MappingProxyType(servers),
        layouts=MappingProxyType(layouts),
    )


def load_canonical(
    source: Path | str | None = None, *, local_profiles: Mapping[str, Profile] | None = None
) -> CanonicalConfig:
    """Load and validate the dotfiles source; raises SchemaError naming the key.

    ``local_profiles`` None reads the override profiles from ``config.toml``.
    """
    path = resolve_source(source)
    raw = _read_limited(path)
    if raw is None:
        raise SchemaError(f"{path}: does not exist")
    if local_profiles is None:
        try:
            local_profiles = profiles_module.load_profiles()
        except ProfileError as error:
            raise SchemaError(str(error)) from None
    try:
        return parse_document(_parse_toml(path, raw), path.parent, path, local_profiles)
    except SchemaError as error:
        if str(error).startswith(f"{path}:"):
            raise
        raise SchemaError(f"{path}: {error}") from None
