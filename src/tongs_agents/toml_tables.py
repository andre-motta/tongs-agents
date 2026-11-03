"""Minimal owned-table writer for TOML files tongs-agents shares with a user.

Codex keeps MCP servers in ``config.toml`` as ``[mcp_servers.<name>]`` tables
next to the user's own. This module adds, replaces or removes only the tables
it is told to, standard library only (decision row 7, option C, in
docs/work/emitter-strategy.md):

- a table is located by its header line through the next header; its own
  sub-tables (``[mcp_servers.<name>.env]``) belong to it wherever they sit;
- comment lines directly above the next header stay with that header;
- values are strings, arrays of strings, booleans and integers only, keys bare
  or quoted basic strings, and control characters are refused, not escaped;
- a table that ``tomllib`` sees but that has no header of its own (an inline
  table or dotted keys) is refused;
- the result must parse, and the parsed document must equal the parsed
  original with only the named tables added, changed or removed; otherwise
  nothing is returned.

No comment markers are written: ownership is the table name plus the digest
of its parsed value, which the emit manifest records.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

PREFIX = "mcp_servers"
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+\Z")
_BARE_RUN = re.compile(r"[A-Za-z0-9_-]+")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


class TomlTableError(ValueError):
    """The file or the requested change is outside what this writer handles."""


@dataclass(frozen=True)
class _Header:
    path: tuple[str, ...]
    start: int
    array: bool


def table_digest(value: Any) -> str:
    """The sha256 of a parsed table: canonical JSON, so formatting is ignored."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise TomlTableError(f"invalid TOML: {error}") from None


def read_tables(text: str, prefix: str = PREFIX) -> dict[str, Any]:
    """The parsed ``[<prefix>.<name>]`` tables of ``text``, by name."""
    section = parse(text).get(prefix, {})
    if not isinstance(section, dict):
        raise TomlTableError(f"{prefix} is not a table")
    return dict(section)


def _key_segments(text: str) -> tuple[str, ...]:
    """Split a header key (``a."b.c".d``) into its segments."""
    segments = []
    index = 0
    length = len(text)
    while True:
        while index < length and text[index] in " \t":
            index += 1
        if index >= length:
            raise TomlTableError(f"malformed table header [{text}]")
        char = text[index]
        if char == '"':
            index += 1
            value = []
            while True:
                if index >= length:
                    raise TomlTableError(f"malformed table header [{text}]")
                char = text[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\":
                    escape = text[index + 1:index + 2]
                    if escape in _ESCAPES:
                        value.append(_ESCAPES[escape])
                        index += 2
                        continue
                    if escape in ("u", "U"):
                        width = 4 if escape == "u" else 8
                        digits = text[index + 2:index + 2 + width]
                        try:
                            value.append(chr(int(digits, 16)))
                        except ValueError:
                            raise TomlTableError(f"malformed table header [{text}]") from None
                        index += 2 + width
                        continue
                    raise TomlTableError(f"malformed table header [{text}]")
                value.append(char)
                index += 1
            segments.append("".join(value))
        elif char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                raise TomlTableError(f"malformed table header [{text}]")
            segments.append(text[index + 1:end])
            index = end + 1
        else:
            match = _BARE_RUN.match(text, index)
            if match is None:
                raise TomlTableError(f"malformed table header [{text}]")
            segments.append(match.group(0))
            index = match.end()
        while index < length and text[index] in " \t":
            index += 1
        if index >= length:
            return tuple(segments)
        if text[index] != ".":
            raise TomlTableError(f"malformed table header [{text}]")
        index += 1


def _skip_basic(text: str, index: int, quote: str, multiline: bool) -> int:
    """The index after the string that starts at ``index`` (after its opening quotes)."""
    closing = quote * 3 if multiline else quote
    escapes = quote == '"'
    while index < len(text):
        char = text[index]
        if escapes and char == "\\":
            index += 2
            continue
        if not multiline and char == "\n":
            raise TomlTableError("unterminated string")
        if text.startswith(closing, index):
            index += len(closing)
            # A multi-line string may end with up to two extra quotes.
            while multiline and index < len(text) and text[index] == quote:
                index += 1
            return index
        index += 1
    raise TomlTableError("unterminated string")


def _headers(text: str) -> list[_Header]:
    """Every table header, found by a scan that skips strings, comments and values."""
    headers = []
    index = 0
    length = len(text)
    depth = 0
    line_start = True
    while index < length:
        char = text[index]
        if char == "\n":
            line_start = True
            index += 1
            continue
        if char in " \t\r":
            index += 1
            continue
        if char == "#":
            end = text.find("\n", index)
            index = length if end < 0 else end
            continue
        if char in "\"'":
            multiline = text.startswith(char * 3, index)
            index = _skip_basic(text, index + (3 if multiline else 1), char, multiline)
            line_start = False
            continue
        if line_start and depth == 0 and char == "[":
            array = text.startswith("[[", index)
            open_width = 2 if array else 1
            end = index + open_width
            # Find the closing bracket outside quoted key segments.
            cursor = end
            while cursor < length and text[cursor] != "\n":
                if text[cursor] in "\"'":
                    cursor = _skip_basic(text, cursor + 1, text[cursor], False)
                    continue
                if text[cursor] == "]":
                    break
                cursor += 1
            closing = "]]" if array else "]"
            if not text.startswith(closing, cursor):
                raise TomlTableError("malformed table header")
            start = text.rfind("\n", 0, index) + 1
            headers.append(_Header(_key_segments(text[end:cursor]), start, array))
            index = cursor + len(closing)
            line_start = False
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth = max(0, depth - 1)
        line_start = False
        index += 1
    return headers


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _section_end(text: str, next_start: int, own_start: int) -> int:
    """Where a section ends: before comment lines that lead into the next header."""
    end = next_start
    cursor = next_start
    while cursor > own_start:
        previous = _line_start(text, cursor - 1)
        line = text[previous:cursor].strip()
        if line.startswith("#"):
            end = previous
        elif line:
            break
        cursor = previous
    return max(end, text.find("\n", own_start) + 1 or len(text))


def _quote_key(key: str) -> str:
    if _BARE_KEY.match(key):
        return key
    return _string(key)


def _string(value: str) -> str:
    if _CONTROL.search(value):
        raise TomlTableError("refusing a value with control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _value(value: Any, where: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if _CONTROL.search(value):
            raise TomlTableError(f"{where}: refusing control characters")
        return _string(value)
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        for item in value:
            if _CONTROL.search(item):
                raise TomlTableError(f"{where}: refusing control characters")
        return "[" + ", ".join(_string(item) for item in value) + "]"
    raise TomlTableError(f"{where}: unsupported value type {type(value).__name__}")


def render_table(prefix: str, name: str, value: Mapping[str, Any]) -> str:
    """``[<prefix>.<name>]`` with its keys, in the order given."""
    for key in (prefix, name, *value):
        if not isinstance(key, str) or not key or _CONTROL.search(key):
            raise TomlTableError(f"refusing key {key!r}")
    lines = [f"[{_quote_key(prefix)}.{_quote_key(name)}]"]
    for key, item in value.items():
        lines.append(f"{_quote_key(key)} = {_value(item, f'{prefix}.{name}.{key}')}")
    return "\n".join(lines) + "\n"


def render_tables(
    text: str, changes: Mapping[str, Mapping[str, Any] | None], *, prefix: str = PREFIX
) -> str:
    """``text`` with each named ``[<prefix>.<name>]`` table set (or removed for None).

    Everything outside those tables keeps its bytes. Raises TomlTableError
    and returns nothing when the change cannot be made exactly.
    """
    original = parse(text)
    section = original.get(prefix, {})
    if not isinstance(section, dict):
        raise TomlTableError(f"{prefix} is not a table")
    headers = _headers(text)
    for header in headers:
        if header.path[:1] == (prefix,) and len(header.path) >= 2 and header.array:
            raise TomlTableError(f"{prefix}.{header.path[1]} is an array of tables")
    for name in changes:
        owned = [header for header in headers if header.path[:2] == (prefix, name)]
        if name in section and not any(len(header.path) == 2 for header in owned):
            raise TomlTableError(
                f"{prefix}.{name} is defined inline or with dotted keys; "
                "edit it by hand or move it into its own table"
            )
    ranges: dict[str, list[tuple[int, int]]] = {}
    for position, header in enumerate(headers):
        if header.path[:1] != (prefix,) or len(header.path) < 2:
            continue
        name = header.path[1]
        if name not in changes:
            continue
        next_start = headers[position + 1].start if position + 1 < len(headers) else len(text)
        end = _section_end(text, next_start, header.start)
        ranges.setdefault(name, []).append((header.start, end))
    pieces: list[tuple[int, int, str]] = []
    appended = []
    for name, value in changes.items():
        rendered = None if value is None else render_table(prefix, name, value)
        spans = sorted(ranges.get(name, []))
        if spans:
            first_start, first_end = spans[0]
            if rendered is not None:
                tail = text[first_start:first_end]
                spacing = tail[len(tail.rstrip("\n")):]
                rendered = rendered + spacing[1:] if spacing.startswith("\n") else rendered
            pieces.append((first_start, first_end, rendered or ""))
            pieces.extend((start, end, "") for start, end in spans[1:])
        elif rendered is not None:
            appended.append(rendered)
    result = []
    cursor = 0
    for start, end, replacement in sorted(pieces):
        result.append(text[cursor:start])
        result.append(replacement)
        cursor = end
    result.append(text[cursor:])
    rendered_text = "".join(result)
    if appended:
        if rendered_text and not rendered_text.endswith("\n"):
            rendered_text += "\n"
        if rendered_text.strip():
            rendered_text += "\n"
        rendered_text += "\n".join(appended)
    if rendered_text.endswith("\n\n") and not text.endswith("\n\n"):
        rendered_text = rendered_text.rstrip("\n") + "\n"
    _verify(original, rendered_text, changes, prefix)
    return rendered_text


def _verify(
    original: dict[str, Any],
    rendered: str,
    changes: Mapping[str, Mapping[str, Any] | None],
    prefix: str,
) -> None:
    try:
        result = tomllib.loads(rendered)
    except tomllib.TOMLDecodeError as error:
        raise TomlTableError(f"the edited file would not parse ({error}); nothing written") \
            from None
    expected = copy.deepcopy(original)
    section = expected.setdefault(prefix, {})
    for name, value in changes.items():
        if value is None:
            section.pop(name, None)
        else:
            section[name] = {key: list(item) if isinstance(item, tuple) else item
                             for key, item in value.items()}
    if not section and prefix not in result:
        expected.pop(prefix)
    if result != expected:
        raise TomlTableError("the edit would change more than the named tables; nothing written")
