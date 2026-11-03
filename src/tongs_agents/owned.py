"""The one writer for files tongs-agents puts into harness config homes.

Two kinds of file:

- Owned files are written whole and carry an owner marker: top-level
  ``owner``, ``schema_version`` and optional ``managed_paths`` keys in JSON,
  or a ``tongs-agents-owned {...}`` header comment in md, toml, kdl, ``hash``
  (``#`` comments) and ``slash`` (``//`` comments). In Markdown it follows a
  leading front matter block, in ``hash`` and ``slash`` a ``#!`` line. They are
  always 0600, and an existing file without our marker is never replaced.
- Shared files hold entries of ours among the user's: JSON (settings.json,
  hooks.json) edited as a document, text (AGENTS.md, CLAUDE.md, config.toml)
  edited as a string. An edit keeps the file's mode, and the first edit of an
  existing file keeps a ``<name>.tongs-agents.bak`` copy of it next to the file.
  That copy is taken once: an existing backup is never refreshed, and nothing
  here removes it.

Every write is atomic (mkstemp, fsync, replace). Symlinks, non-regular files,
files over a size cap and malformed JSON are refused with a one-line
``OwnedFileError`` and left untouched. A ``ChangeSet`` applies several changes,
re-checking each file's digest first, and restores every file it touched when
any step fails. The emit manifest (``state_dir()/emit/manifest.json``) records
the files tongs-agents manages per target and, per target, keyed entries with
the digest of what was emitted (``entries``), which drift checks compare.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tongs_agents import paths
from tongs_agents.config_layer.protocol import WRITING_KINDS, Change, ProfileTarget

OWNER = "tongs-agents"
BACKUP_SUFFIX = ".tongs-agents.bak"
MAX_OWNED_BYTES = 1024 * 1024
MAX_EDIT_BYTES = 4 * 1024 * 1024
# A file a harness owns (Claude Code's .claude.json) that tongs-agents only
# reads back or copies into a drift backup; it can be far larger than ours.
HARNESS_FILE_MAX_BYTES = 64 * 1024 * 1024
MARKER_KEYS = ("owner", "schema_version", "managed_paths")
FORMATS = ("json", "md", "toml", "kdl", "hash", "slash")
MANIFEST_SCHEMA = 1
MANIFEST_NAME = "manifest.json"

_SUFFIX_FORMATS = {".json": "json", ".md": "md", ".toml": "toml", ".kdl": "kdl"}
_MARKER_TAG = "tongs-agents-owned"
_COMMENTS = {
    "md": ("<!-- ", " -->"), "toml": ("# ", ""), "kdl": ("// ", ""),
    "hash": ("# ", ""), "slash": ("// ", ""),
}
_FRONT_MATTER = re.compile(r"\A---\r?\n.*?^---[ \t]*\r?\n", re.DOTALL | re.MULTILINE)
_SHEBANG = re.compile(r"\A#![^\n]*\n")
# A leading block the marker line must follow so the file keeps working.
_PREAMBLES = {"md": _FRONT_MATTER, "hash": _SHEBANG, "slash": _SHEBANG}
_ENTRY_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,2048}\Z")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class OwnedFileError(OSError):
    """A file tongs-agents refuses to read or change; the message is one line.

    It is an OSError so I/O failures and refusals share one handler, which
    also means a broad ``except OSError`` swallows a refusal: catch this (or
    ``cli.ConfigError``) first wherever a refusal must be reported as one.
    """


class SymlinkError(OwnedFileError):
    """The target is a symlink."""


class SizeError(OwnedFileError):
    """The target, or the content for it, is over the size cap."""


class MalformedError(OwnedFileError):
    """The target is not valid JSON."""


class OwnershipError(OwnedFileError):
    """The target exists without our owner marker."""


class StaleError(OwnedFileError):
    """The target changed between planning and applying."""


@dataclass(frozen=True)
class OwnedFile:
    """An owned file as read back.

    ``content`` is the whole JSON object (marker keys included) for json and
    the text without the marker line for the other formats.
    """

    path: Path
    fmt: str
    owner: str
    schema_version: int
    managed_paths: tuple[str, ...] | None
    content: Any
    raw: bytes


@dataclass(frozen=True)
class EntryOwnership:
    """How an edit may treat a shared JSON file whose entries are mixed.

    The mutation itself decides which entries are ours (per hook command, per
    MCP key). ``validate(path, document)`` runs first and raises
    ``OwnedFileError`` for a document shape the edit must not touch;
    ``max_bytes`` caps both the file read and the content written.
    """

    validate: Callable[[Path, Any], None] | None = None
    max_bytes: int = MAX_EDIT_BYTES


def sha256_of(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def file_format(path: Path, fmt: str | None = None) -> str:
    """``fmt``, else the format named by the suffix of ``path``."""
    chosen = fmt or _SUFFIX_FORMATS.get(Path(path).suffix.lower())
    if chosen not in FORMATS:
        raise ValueError(f"{path}: unknown owned file format; pass fmt= one of {FORMATS}")
    return chosen


def _read_current(path: Path, max_bytes: int) -> tuple[bytes | None, int | None]:
    """The bytes and mode of ``path``, or (None, None) when it is absent."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        raise OwnedFileError(f"{path}: cannot read ({type(error).__name__})") from None
    if stat.S_ISLNK(info.st_mode):
        raise SymlinkError(f"{path}: refusing to modify a symlinked file")
    if not stat.S_ISREG(info.st_mode):
        raise OwnedFileError(f"{path}: cannot read (not a regular file)")
    if info.st_size > max_bytes:
        raise SizeError(f"{path}: refusing a file larger than {max_bytes} bytes")
    try:
        descriptor = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
        with os.fdopen(descriptor, "rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise SymlinkError(f"{path}: refusing to modify a symlinked file") from None
        raise OwnedFileError(f"{path}: cannot read ({type(error).__name__})") from None
    if len(data) > max_bytes:
        raise SizeError(f"{path}: refusing a file larger than {max_bytes} bytes")
    return data, stat.S_IMODE(info.st_mode)


def read_bytes(path: Path, *, max_bytes: int = MAX_EDIT_BYTES) -> bytes | None:
    """The bytes of ``path``, None when absent; refuses symlinks and oversize files."""
    return _read_current(Path(path), max_bytes)[0]


def read_text(path: Path, *, max_bytes: int = MAX_EDIT_BYTES) -> str | None:
    """``path`` as UTF-8 text, None when absent; refuses what ``read_bytes`` refuses."""
    raw = read_bytes(path, max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeError:
        raise OwnedFileError(f"{path}: cannot read (not UTF-8 text)") from None


def _parse_json(path: Path, raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise MalformedError(
            f"{path}: malformed JSON ({error.msg} at line {error.lineno} column {error.colno})"
        ) from None
    except (UnicodeError, RecursionError) as error:
        raise OwnedFileError(f"{path}: cannot read ({type(error).__name__})") from None


def read_json(
    path: Path,
    *,
    missing: Any = None,
    validate: Callable[[Path, Any], None] | None = None,
    max_bytes: int = MAX_EDIT_BYTES,
) -> Any:
    """Parse ``path``, or return ``missing`` when it is absent."""
    raw, _ = _read_current(Path(path), max_bytes)
    if raw is None:
        return missing
    document = _parse_json(path, raw)
    if validate is not None:
        validate(path, document)
    return document


def _replace(source: str, destination: Path) -> None:
    os.replace(source, destination)


def _missing_parents(directory: Path) -> list[Path]:
    missing = []
    while not os.path.lexists(directory):
        missing.append(directory)
        if directory.parent == directory:
            break
        directory = directory.parent
    return missing


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    if os.path.islink(path):
        raise SymlinkError(f"{path}: refusing to modify a symlinked file")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` atomically; ``mode`` None keeps the current mode.

    For tongs-agents' own state files; harness files go through a ChangeSet.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            mode = 0o600
        else:
            if stat.S_ISLNK(info.st_mode):
                raise SymlinkError(f"{path}: refusing to modify a symlinked file")
            mode = stat.S_IMODE(info.st_mode)
    _write_atomic(path, data, mode)


def _marker_line(fmt: str, marker: Mapping[str, Any]) -> str:
    start, end = _COMMENTS[fmt]
    encoded = json.dumps(dict(marker), sort_keys=True, ensure_ascii=True).replace(">", "\\u003e")
    return f"{start}{_MARKER_TAG} {encoded}{end}\n"


def _marker(owner: str, schema_version: int, managed_paths: Iterable[str] | None) -> dict:
    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ValueError("schema_version must be an int")
    marker: dict[str, Any] = {"owner": owner, "schema_version": schema_version}
    if managed_paths is not None:
        values = list(managed_paths)
        if not all(isinstance(value, str) for value in values):
            raise ValueError("managed_paths must be strings")
        marker["managed_paths"] = values
    return marker


def render_owned(
    fmt: str,
    data: Any,
    *,
    owner: str,
    schema_version: int,
    managed_paths: Iterable[str] | None = None,
) -> bytes:
    """The bytes of an owned file: ``data`` plus the owner marker."""
    marker = _marker(owner, schema_version, managed_paths)
    if fmt == "json":
        if not isinstance(data, Mapping):
            raise ValueError("owned JSON content must be an object")
        clashes = sorted(key for key in data if key in MARKER_KEYS)
        if clashes:
            raise ValueError(f"owned JSON content must not set marker keys {clashes}")
        value = {**data, **marker}
        return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if fmt not in _COMMENTS:
        raise ValueError(f"unknown owned file format {fmt!r}")
    if not isinstance(data, str):
        raise ValueError(f"owned {fmt} content must be text")
    if data and not data.endswith("\n"):
        data += "\n"
    line = _marker_line(fmt, marker)
    front = _preamble(fmt, data)
    if front:
        return (data[:front.end()] + line + data[front.end():]).encode("utf-8")
    return (line + data).encode("utf-8")


def _preamble(fmt: str, text: str) -> re.Match | None:
    pattern = _PREAMBLES.get(fmt)
    return pattern.match(text) if pattern is not None else None


def _valid_marker(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    owner = value.get("owner")
    version = value.get("schema_version")
    if not isinstance(owner, str) or not owner:
        return False
    if not isinstance(version, int) or isinstance(version, bool):
        return False
    managed = value.get("managed_paths")
    return managed is None or (
        isinstance(managed, list) and all(isinstance(item, str) for item in managed)
    )


def parse_owned(path: Path, raw: bytes, fmt: str) -> OwnedFile | None:
    """The owned view of ``raw``, or None when it carries no valid marker."""
    if fmt == "json":
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(value, dict) or not _valid_marker(value):
            return None
        marker, content = value, value
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            return None
        start, end = _COMMENTS[fmt]
        front = _preamble(fmt, text)
        offset = front.end() if front else 0
        line_end = text.find("\n", offset)
        line = text[offset:] if line_end < 0 else text[offset:line_end]
        prefix = f"{start}{_MARKER_TAG} "
        if not line.startswith(prefix) or not line.endswith(end):
            return None
        try:
            marker = json.loads(line[len(prefix):len(line) - len(end)])
        except (ValueError, RecursionError):
            return None
        if not _valid_marker(marker):
            return None
        rest = "" if line_end < 0 else text[line_end + 1:]
        content = text[:offset] + rest
    managed = marker.get("managed_paths")
    return OwnedFile(
        path=Path(path),
        fmt=fmt,
        owner=marker["owner"],
        schema_version=marker["schema_version"],
        managed_paths=None if managed is None else tuple(managed),
        content=content,
        raw=raw,
    )


def read_owned_file(
    path: Path, *, owner: str, fmt: str | None = None, max_bytes: int = MAX_OWNED_BYTES
) -> OwnedFile | None:
    """``path`` as an owned file, None when absent; refuses a file not ours."""
    path = Path(path)
    fmt = file_format(path, fmt)
    raw, _ = _read_current(path, max_bytes)
    if raw is None:
        return None
    if fmt == "json":
        _parse_json(path, raw)
    current = parse_owned(path, raw, fmt)
    if current is None or current.owner != owner:
        raise OwnershipError(f"{path}: not owned by {owner}; refusing to change it")
    return current


def _owned_by(path: Path, raw: bytes, fmt: str, owner: str) -> bool:
    current = parse_owned(path, raw, fmt)
    return current is not None and current.owner == owner


def _plan_owned(
    path: Path, content: bytes, owner: str, fmt: str, current: bytes | None
) -> Change:
    before = sha256_of(current)
    if current is not None and not _owned_by(path, current, fmt, owner):
        return Change(path, "conflict", before, before)
    if current is None:
        kind = "create"
    elif current == content:
        kind = "unchanged"
    else:
        kind = "update"
    return Change(path, kind, before, sha256_of(content), content)


def plan_owned_file(
    path: Path,
    data: Any,
    *,
    owner: str,
    schema_version: int,
    managed_paths: Iterable[str] | None = None,
    fmt: str | None = None,
    max_bytes: int = MAX_OWNED_BYTES,
) -> Change:
    """The change that writes an owned file; ``conflict`` when the file is not ours."""
    path = Path(path)
    fmt = file_format(path, fmt)
    content = render_owned(
        fmt, data, owner=owner, schema_version=schema_version, managed_paths=managed_paths
    )
    if len(content) > max_bytes:
        raise SizeError(f"{path}: refusing to write more than {max_bytes} bytes")
    current, _ = _read_current(path, max_bytes)
    return _plan_owned(path, content, owner, fmt, current)


def _plan_delete(path: Path, owner: str, fmt: str, current: bytes | None) -> Change:
    before = sha256_of(current)
    if current is None:
        return Change(path, "unchanged")
    if not _owned_by(path, current, fmt, owner):
        return Change(path, "conflict", before, before)
    return Change(path, "delete", before, None)


def plan_delete_owned(
    path: Path, *, owner: str, fmt: str | None = None, max_bytes: int = MAX_OWNED_BYTES
) -> Change:
    """The change that removes an owned file; ``conflict`` when it is not ours."""
    path = Path(path)
    fmt = file_format(path, fmt)
    current, _ = _read_current(path, max_bytes)
    return _plan_delete(path, owner, fmt, current)


def _serialize_json(document: Any) -> bytes:
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def _plan_edit(
    path: Path,
    mutate: Callable[[Any], Any],
    ownership: EntryOwnership,
    current: bytes | None,
) -> Change:
    document = {} if current is None else _parse_json(path, current)
    if ownership.validate is not None:
        ownership.validate(path, document)
    result = mutate(document)
    if result is not None:
        document = result
    content = _serialize_json(document)
    if len(content) > ownership.max_bytes:
        raise SizeError(f"{path}: refusing to write more than {ownership.max_bytes} bytes")
    before = sha256_of(current)
    if current is None:
        kind = "create"
    elif content == current:
        kind = "unchanged"
    else:
        kind = "edit_entry"
    return Change(path, kind, before, sha256_of(content), content)


def plan_json_edit(
    path: Path, mutate: Callable[[Any], Any], *, ownership: EntryOwnership = EntryOwnership()
) -> Change:
    """The change that applies ``mutate`` to a shared JSON file; never writes.

    ``mutate`` edits the parsed document in place or returns a replacement.
    An absent file starts as ``{}``.
    """
    path = Path(path)
    current, _ = _read_current(path, ownership.max_bytes)
    return _plan_edit(path, mutate, ownership, current)


def _plan_text_edit(
    path: Path,
    mutate: Callable[[str], str | None],
    current: bytes | None,
    max_bytes: int,
) -> Change:
    try:
        text = "" if current is None else current.decode("utf-8")
    except UnicodeError:
        raise OwnedFileError(f"{path}: cannot read (not UTF-8 text)") from None
    result = mutate(text)
    before = sha256_of(current)
    if result is None:
        if current is None:
            return Change(path, "unchanged")
        return Change(path, "delete", before, None)
    if not isinstance(result, str):
        raise TypeError("a text edit must return a string or None")
    content = result.encode("utf-8")
    if len(content) > max_bytes:
        raise SizeError(f"{path}: refusing to write more than {max_bytes} bytes")
    if current is None:
        if not content:
            return Change(path, "unchanged")
        kind = "create"
    elif content == current:
        kind = "unchanged"
    else:
        kind = "edit_entry"
    return Change(path, kind, before, sha256_of(content), content)


def plan_text_edit(
    path: Path, mutate: Callable[[str], str | None], *, max_bytes: int = MAX_EDIT_BYTES
) -> Change:
    """The change that applies ``mutate`` to a shared UTF-8 text file; never writes.

    ``mutate`` gets the current text ("" when absent) and returns the new
    text, or None to remove the file. Callers return None only when nothing
    but their own entries is left. An absent file that stays empty is not
    created.
    """
    path = Path(path)
    current, _ = _read_current(path, max_bytes)
    return _plan_text_edit(path, mutate, current, max_bytes)


@dataclass
class _Undo:
    path: Path
    previous: bytes | None
    mode: int | None
    backup: Path | None = None
    created_dirs: tuple[Path, ...] = ()


def _merge(old: Change, new: Change) -> Change:
    """One change with the effect of ``old`` then ``new`` on the same path."""
    before = old.before_sha256
    if "conflict" in (old.kind, new.kind):
        return Change(old.path, "conflict", before, before)
    if new.kind == "unchanged" and new.content is None:
        return old
    if new.kind == "delete":
        if before is None:
            return Change(old.path, "unchanged")
        return Change(old.path, "delete", before, None)
    after = new.after_sha256
    if before is None:
        kind = "create"
    elif after == before:
        kind = "unchanged"
    elif "edit_entry" in (old.kind, new.kind):
        kind = "edit_entry"
    else:
        kind = "update"
    return Change(old.path, kind, before, after, new.content)


class ChangeSet:
    """Changes to several files, applied together or not at all.

    Stage with the methods below (or ``add`` a planned Change), inspect
    ``changes``, then ``commit``. Staging the same path twice composes the
    edits. ``commit`` refuses when any change is a conflict or any file no
    longer matches its planned digest, before writing anything; when a write
    fails it restores every file it touched, removes backups and directories
    it created, and re-raises.
    """

    def __init__(self) -> None:
        self._changes: dict[Path, Change] = {}
        self._limits: dict[Path, int] = {}
        self._prepare: list[Callable[[], None]] = []
        self._committed = False

    @property
    def changes(self) -> tuple[Change, ...]:
        return tuple(self._changes.values())

    def _key(self, path: Path) -> Path:
        return Path(os.path.abspath(path))

    def _current(self, path: Path, max_bytes: int) -> bytes | None:
        staged = self._changes.get(self._key(path))
        if staged is not None and staged.kind != "conflict":
            if staged.content is not None:
                return staged.content
            if staged.kind == "delete":
                return None
        current, _ = _read_current(path, max_bytes)
        return current

    def add(self, change: Change, *, max_bytes: int = MAX_EDIT_BYTES) -> Change:
        """Stage a planned change; returns the change now staged for its path."""
        if self._committed:
            raise RuntimeError("this ChangeSet was already committed")
        key = self._key(change.path)
        previous = self._changes.get(key)
        staged = change if previous is None else _merge(previous, change)
        self._changes[key] = staged
        self._limits[key] = max(self._limits.get(key, 0), max_bytes)
        return staged

    def write_owned_file(
        self,
        path: Path,
        data: Any,
        *,
        owner: str,
        schema_version: int,
        managed_paths: Iterable[str] | None = None,
        fmt: str | None = None,
        max_bytes: int = MAX_OWNED_BYTES,
    ) -> Change:
        path = Path(path)
        fmt = file_format(path, fmt)
        content = render_owned(
            fmt, data, owner=owner, schema_version=schema_version, managed_paths=managed_paths
        )
        if len(content) > max_bytes:
            raise SizeError(f"{path}: refusing to write more than {max_bytes} bytes")
        current = self._current(path, max_bytes)
        return self.add(_plan_owned(path, content, owner, fmt, current), max_bytes=max_bytes)

    def delete_owned_file(
        self, path: Path, *, owner: str, fmt: str | None = None,
        max_bytes: int = MAX_OWNED_BYTES,
    ) -> Change:
        path = Path(path)
        fmt = file_format(path, fmt)
        current = self._current(path, max_bytes)
        return self.add(_plan_delete(path, owner, fmt, current), max_bytes=max_bytes)

    def edit_json_entries(
        self,
        path: Path,
        mutate: Callable[[Any], Any],
        *,
        ownership: EntryOwnership = EntryOwnership(),
    ) -> Change:
        path = Path(path)
        current = self._current(path, ownership.max_bytes)
        change = _plan_edit(path, mutate, ownership, current)
        return self.add(change, max_bytes=ownership.max_bytes)

    def edit_text_entries(
        self,
        path: Path,
        mutate: Callable[[str], str | None],
        *,
        max_bytes: int = MAX_EDIT_BYTES,
    ) -> Change:
        path = Path(path)
        current = self._current(path, max_bytes)
        change = _plan_text_edit(path, mutate, current, max_bytes)
        return self.add(change, max_bytes=max_bytes)

    def _stage_manifest(
        self, update: Callable[[dict[str, list[str]], dict[str, dict]], None]
    ) -> Change:
        path = manifest_path()
        current = self._current(path, MAX_OWNED_BYTES)
        targets, entries = ({}, {}) if current is None else _manifest_document(path, current)
        update(targets, entries)
        data: dict[str, Any] = {"targets": dict(sorted(targets.items()))}
        if entries:
            data["entries"] = {key: dict(sorted(value.items()))
                               for key, value in sorted(entries.items())}
        if _ensure_manifest_dir not in self._prepare:
            self._prepare.append(_ensure_manifest_dir)
        return self.write_owned_file(
            path, data, owner=OWNER, schema_version=MANIFEST_SCHEMA, fmt="json",
        )

    def record_managed_paths(
        self, target: ProfileTarget, managed: Iterable[Path | str]
    ) -> Change:
        """Stage the manifest entry for ``target``; an empty list drops it."""
        values = sorted({str(item) for item in managed})
        if not all(os.path.isabs(value) for value in values):
            raise ValueError("managed paths must be absolute")
        key = target_key(target)

        def update(targets: dict, entries: dict) -> None:
            if values:
                targets[key] = values
            else:
                targets.pop(key, None)

        return self._stage_manifest(update)

    def record_entries(
        self, target: ProfileTarget, entries: Mapping[str, Mapping[str, str]]
    ) -> Change:
        """Stage the keyed entries for ``target``, replacing its previous ones.

        Each entry id maps to string fields; ``kind`` and ``sha256`` are
        required. An empty mapping drops the target's entries.
        """
        values = {name: dict(fields) for name, fields in entries.items()}
        problem = _entries_problem(values)
        if problem:
            raise ValueError(problem)
        key = target_key(target)

        def update(targets: dict, recorded: dict) -> None:
            if values:
                recorded[key] = values
            else:
                recorded.pop(key, None)

        return self._stage_manifest(update)

    def commit(self) -> list[Change]:
        if self._committed:
            raise RuntimeError("this ChangeSet was already committed")
        changes = list(self._changes.values())
        conflicts = [change for change in changes if change.kind == "conflict"]
        if conflicts:
            raise OwnershipError(
                f"{conflicts[0].path}: not owned by tongs-agents; refusing to change it"
            )
        pending = []
        for change in changes:
            if change.kind == "unchanged":
                continue
            limit = self._limits.get(self._key(change.path), MAX_EDIT_BYTES)
            current, mode = _read_current(change.path, limit)
            if sha256_of(current) != change.before_sha256:
                raise StaleError(
                    f"{change.path}: changed since it was planned; nothing was written"
                )
            pending.append((change, current, mode))
        for prepare in self._prepare:
            prepare()
        applied: list[_Undo] = []
        try:
            for change, current, mode in pending:
                undo = _Undo(change.path, current, mode)
                applied.append(undo)
                _apply(change, current, mode, undo)
        except BaseException as error:
            problems = _rollback(applied)
            if isinstance(error, OSError) and not isinstance(error, OwnedFileError):
                failed = applied[-1].path
                outcome = (
                    "every change was rolled back" if not problems
                    else "could not restore " + ", ".join(str(item) for item in problems)
                )
                raise OwnedFileError(
                    f"{failed}: could not write ({error}); {outcome}"
                ) from error
            for problem in problems:
                error.add_note(f"{problem}: could not be restored")
            raise
        self._committed = True
        return changes


def _apply(change: Change, current: bytes | None, mode: int | None, undo: _Undo) -> None:
    path = change.path
    if change.kind == "delete":
        os.unlink(path)
        return
    if change.kind not in WRITING_KINDS or change.content is None:
        raise ValueError(f"{path}: cannot apply a {change.kind} change")
    missing = _missing_parents(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    undo.created_dirs = tuple(missing)
    if change.kind == "edit_entry" and current is not None:
        backup = backup_path(path)
        if not os.path.lexists(backup):
            _write_atomic(backup, current, 0o600)
            undo.backup = backup
    keep_mode = change.kind == "edit_entry" and mode is not None
    _write_atomic(path, change.content, mode if keep_mode else 0o600)


def _rollback(applied: list[_Undo]) -> list[Path]:
    problems = []
    for undo in reversed(applied):
        try:
            if undo.previous is None:
                try:
                    os.unlink(undo.path)
                except FileNotFoundError:
                    pass
            else:
                _write_atomic(
                    undo.path, undo.previous, 0o600 if undo.mode is None else undo.mode
                )
            if undo.backup is not None:
                os.unlink(undo.backup)
        except OSError:
            problems.append(undo.path)
        for directory in undo.created_dirs:
            try:
                directory.rmdir()
            except OSError:
                pass
    return problems


def write_owned_file(
    path: Path,
    data: Any,
    *,
    owner: str,
    schema_version: int,
    managed_paths: Iterable[str] | None = None,
    fmt: str | None = None,
    max_bytes: int = MAX_OWNED_BYTES,
) -> Change:
    """Write one owned file now (0600); refuses a file that is not ours."""
    changes = ChangeSet()
    change = changes.write_owned_file(
        path, data, owner=owner, schema_version=schema_version,
        managed_paths=managed_paths, fmt=fmt, max_bytes=max_bytes,
    )
    changes.commit()
    return change


def delete_owned_file(
    path: Path, *, owner: str, fmt: str | None = None, max_bytes: int = MAX_OWNED_BYTES
) -> Change:
    """Remove one owned file now; refuses a file that is not ours."""
    changes = ChangeSet()
    change = changes.delete_owned_file(path, owner=owner, fmt=fmt, max_bytes=max_bytes)
    changes.commit()
    return change


def edit_json_entries(
    path: Path, mutate: Callable[[Any], Any], *, ownership: EntryOwnership = EntryOwnership()
) -> Change:
    """Apply ``mutate`` to a shared JSON file now, keeping a backup of the original."""
    changes = ChangeSet()
    change = changes.edit_json_entries(path, mutate, ownership=ownership)
    changes.commit()
    return change


def manifest_path() -> Path:
    return paths.state_dir() / "emit" / MANIFEST_NAME


def _ensure_manifest_dir() -> None:
    paths.ensure_private_dir(paths.state_dir(create=True) / "emit")


def target_key(target: ProfileTarget) -> str:
    where = "default" if target.config_dir is None else str(target.config_dir)
    return f"{target.harness}:{where}"


def _entries_problem(entries: Any) -> str | None:
    if not isinstance(entries, dict):
        return "manifest entries must be a mapping"
    for name, fields in entries.items():
        if not isinstance(name, str) or not _ENTRY_ID.match(name):
            return f"invalid manifest entry id {name!r}"
        if not isinstance(fields, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in fields.items()
        ):
            return f"manifest entry {name!r} must map strings to strings"
        if not fields.get("kind") or not fields.get("sha256"):
            return f"manifest entry {name!r} needs kind and sha256"
    return None


def _manifest_document(
    path: Path, raw: bytes
) -> tuple[dict[str, list[str]], dict[str, dict[str, dict[str, str]]]]:
    current = parse_owned(path, raw, "json")
    if current is None or current.owner != OWNER:
        _parse_json(path, raw)
        raise OwnershipError(f"{path}: not owned by {OWNER}; refusing to change it")
    targets = current.content.get("targets")
    entries = current.content.get("entries", {})
    if (
        current.schema_version != MANIFEST_SCHEMA
        or not isinstance(targets, dict)
        or not all(
            isinstance(key, str)
            and isinstance(values, list)
            and all(isinstance(value, str) and os.path.isabs(value) for value in values)
            for key, values in targets.items()
        )
        or not isinstance(entries, dict)
        or not all(
            isinstance(key, str) and _entries_problem(value) is None
            for key, value in entries.items()
        )
    ):
        raise OwnedFileError(f"{path}: unrecognized emit manifest")
    return (
        {key: list(values) for key, values in targets.items()},
        {key: {name: dict(fields) for name, fields in value.items()}
         for key, value in entries.items()},
    )


def _manifest_targets(path: Path, raw: bytes) -> dict[str, list[str]]:
    return _manifest_document(path, raw)[0]


def load_manifest() -> dict[str, list[str]]:
    """Managed paths per target key; empty when no manifest exists."""
    path = manifest_path()
    raw, _ = _read_current(path, MAX_OWNED_BYTES)
    if raw is None:
        return {}
    return _manifest_targets(path, raw)


def load_manifest_entries() -> dict[str, dict[str, dict[str, str]]]:
    """Keyed entries per target key; empty when no manifest exists."""
    path = manifest_path()
    raw, _ = _read_current(path, MAX_OWNED_BYTES)
    if raw is None:
        return {}
    return _manifest_document(path, raw)[1]


def managed_paths_for(target: ProfileTarget) -> tuple[Path, ...]:
    return tuple(Path(value) for value in load_manifest().get(target_key(target), []))


def manifest_entries_for(target: ProfileTarget) -> dict[str, dict[str, str]]:
    return load_manifest_entries().get(target_key(target), {})
