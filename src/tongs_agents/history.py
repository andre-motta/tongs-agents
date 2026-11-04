"""Past agent sessions, read from the harnesses' own history stores (#133).

Every store is read-only and bounded: at most ``MAX_ENTRIES`` directory
entries are listed per store, transcripts are read newest first (by
modification time) until ``MAX_SESSIONS`` sessions are kept, and at most
``MAX_FILES`` of them are read (``MAX_CODEX_FILES`` for Codex, where most
rollouts are subagent threads that only their first line rules out). A
transcript is never loaded whole: its first ``HEAD_BYTES`` and last
``TAIL_BYTES`` are read (a Codex rollout: its first line, up to
``FIRST_LINE_BYTES``, then ``HEAD_BYTES`` when it is kept), and only lines
that name the wanted record type are parsed. Symlinks
inside a store are skipped (files with ``O_NOFOLLOW``), unreadable or
malformed lines and files are skipped, and nothing runs a subprocess. Only a
short title leaves this module, never message bodies: the harness's own
title, else the first (or last) user prompt, cut to ``TITLE_LENGTH``
characters with control characters removed.

- Claude Code, ``<config dir>/projects/<encoded cwd>/<id>.jsonl``: the
  encoded directory name is lossy, so the folder is the first ``cwd`` of the
  records. Title: the last ``custom-title`` (``customTitle``), else
  ``agent-name`` (``agentName``, ``--name``), else ``ai-title``
  (``aiTitle``, rewritten as the session goes on), else ``summary``, else
  ``last-prompt`` (``lastPrompt``), else the first user prompt.
- Codex, ``<CODEX_HOME>/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl``: the
  first line is ``session_meta`` with ``id``, ``cwd`` and ``source``.
  Subagent and review threads (``source`` is an object) are not listed.
  Title: the last ``thread_name`` for the id in ``session_index.jsonl``,
  else the first user prompt that is not injected context.
- Pi, ``<session dir>/--<encoded cwd>--/<ts>_<id>.jsonl``, the session dir
  being ``<agent dir>/sessions`` unless the caller names another: the
  first line is the ``session`` header with ``id`` and ``cwd``. Title: the
  last ``session_info`` ``name`` (``/name``, ``--name``), else the first
  user prompt.
"""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

HARNESSES = ("claude", "codex", "pi")
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAX_ENTRIES = 20000
MAX_FILES = 400
MAX_CODEX_FILES = 4000
MAX_SESSIONS = 200
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 64 * 1024
FIRST_LINE_BYTES = 256 * 1024
INDEX_BYTES = 256 * 1024
TITLE_LENGTH = 64
ELLIPSIS = "…"
CLAUDE_TITLES = (
    ("custom-title", "customTitle"),
    ("agent-name", "agentName"),
    ("ai-title", "aiTitle"),
    ("summary", "summary"),
    ("last-prompt", "lastPrompt"),
)
CODEX_INJECTED = ("<", "# AGENTS.md instructions")


@dataclass(frozen=True)
class PastSession:
    """One session a harness store holds; ``mtime`` is its transcript's."""

    harness: str
    session_id: str
    folder: str
    title: str
    mtime: int
    home: Path


def clean_title(text) -> str:
    """``text`` on one line, without control characters, at most ``TITLE_LENGTH`` long."""
    if not isinstance(text, str):
        return ""
    text = "".join(
        " " if unicodedata.category(char).startswith(("C", "Z")) else char
        for char in text[:TITLE_LENGTH * 8]
    )
    text = " ".join(text.split())
    if len(text) > TITLE_LENGTH:
        text = text[:TITLE_LENGTH - 1].rstrip() + ELLIPSIS
    return text


# -- Bounded reads --

def _open(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"{path} is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "rb")


def _lines(chunk: bytes, *, cut_first: bool, cut_last: bool) -> list[bytes]:
    lines = chunk.split(b"\n")
    if cut_first:
        lines = lines[1:]
    if cut_last:
        lines = lines[:-1]
    return [line for line in lines if line.strip()]


def _head_tail(path: Path) -> tuple[list[bytes], list[bytes]]:
    """The complete lines of the first ``HEAD_BYTES`` and of the last ``TAIL_BYTES``."""
    with _open(path) as handle:
        size = os.fstat(handle.fileno()).st_size
        head = handle.read(HEAD_BYTES)
        head_lines = _lines(head, cut_first=False, cut_last=len(head) < size)
        if size <= HEAD_BYTES:
            return head_lines, head_lines
        start = max(HEAD_BYTES, size - TAIL_BYTES)
        handle.seek(start)
        tail = handle.read(TAIL_BYTES)
    return head_lines, _lines(tail, cut_first=True, cut_last=False)


def _first_line(handle) -> bytes | None:
    line = handle.readline(FIRST_LINE_BYTES)
    if not line.endswith(b"\n") and len(line) >= FIRST_LINE_BYTES:
        return None
    return line


def _records(lines: Iterable[bytes], needle: bytes | None = None) -> Iterator[dict]:
    for line in lines:
        if needle is not None and needle not in line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if isinstance(record, dict):
            yield record


def _type_needle(kind: str) -> bytes:
    return f'"{kind}"'.encode()


def _text(content, kinds: tuple[str, ...]) -> str:
    """The first text part of a message ``content`` (a string or a list of parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") in kinds:
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return ""


def _valid_id(value) -> bool:
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


# -- Claude Code --

def _claude_prompt(lines: list[bytes]) -> str:
    for record in _records(lines, _type_needle("user")):
        if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
            continue
        if record.get("isCompactSummary"):
            continue
        message = record.get("message")
        text = _text(message.get("content"), ("text",)) if isinstance(message, dict) else ""
        if text.strip() and not text.lstrip().startswith("<"):
            return text
    return ""


def _claude_title(head: list[bytes], tail: list[bytes]) -> str:
    for kind, key in CLAUDE_TITLES:
        needle = _type_needle(kind)
        for lines in (tail, head):
            for record in _records(reversed(lines), needle):
                if record.get("type") == kind:
                    title = clean_title(record.get(key))
                    if title:
                        return title
    return clean_title(_claude_prompt(head))


def _claude_folder(head: list[bytes], tail: list[bytes]) -> str:
    for lines in (head, list(reversed(tail))):
        for record in _records(lines, b'"cwd"'):
            cwd = record.get("cwd")
            if isinstance(cwd, str) and os.path.isabs(cwd):
                return cwd
    return ""


def _read_claude(path: Path, home: Path, mtime: int) -> PastSession | None:
    session_id = path.name[:-len(".jsonl")]
    if not _valid_id(session_id):
        return None
    head, tail = _head_tail(path)
    if not head and not tail:
        return None
    return PastSession("claude", session_id, _claude_folder(head, tail),
                       _claude_title(head, tail), mtime, home)


# -- Codex --

def _codex_prompt(lines: list[bytes]) -> str:
    for record in _records(lines, b'"user'):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        text = ""
        if (record.get("type") == "response_item" and payload.get("type") == "message"
                and payload.get("role") == "user"):
            text = _text(payload.get("content"), ("input_text", "text"))
        elif record.get("type") == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message") if isinstance(payload.get("message"), str) else ""
        if text.strip() and not text.lstrip().startswith(CODEX_INJECTED):
            return text
    return ""


def codex_thread_names(home: Path) -> dict[str, str]:
    """Thread id to its latest name, from the end of ``session_index.jsonl``."""
    names: dict[str, str] = {}
    try:
        with _open(home / "session_index.jsonl") as handle:
            size = os.fstat(handle.fileno()).st_size
            start = max(0, size - INDEX_BYTES)
            handle.seek(start)
            lines = _lines(handle.read(INDEX_BYTES), cut_first=start > 0, cut_last=False)
    except OSError:
        return names
    for record in _records(lines, b'"thread_name"'):
        thread, name = record.get("id"), clean_title(record.get("thread_name"))
        if _valid_id(thread) and name:
            names[thread] = name
    return names


def _read_codex(path: Path, home: Path, mtime: int,
                names: dict[str, str]) -> PastSession | None:
    with _open(path) as handle:
        first = _first_line(handle)
        if first is None:
            return None
        meta = next(_records([first]), None)
        payload = meta.get("payload") if meta is not None else None
        if meta is None or meta.get("type") != "session_meta" or not isinstance(payload, dict):
            return None
        session_id = payload.get("id")
        if isinstance(payload.get("source"), dict) or not _valid_id(session_id):
            return None
        rest = b"" if session_id in names else handle.read(HEAD_BYTES)
    cwd = payload.get("cwd")
    folder = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else ""
    title = names.get(session_id) or clean_title(
        _codex_prompt(_lines(rest, cut_first=False, cut_last=True)))
    return PastSession("codex", session_id, folder, title, mtime, home)


# -- Pi --

def _pi_prompt(lines: list[bytes]) -> str:
    for record in _records(lines, b'"user"'):
        message = record.get("message")
        if record.get("type") != "message" or not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        text = _text(message.get("content"), ("text",))
        if text.strip():
            return text
    return ""


def _read_pi(path: Path, home: Path, mtime: int) -> PastSession | None:
    head, tail = _head_tail(path)
    header = next(_records(head[:1]), None)
    if header is None or header.get("type") != "session":
        return None
    session_id = header.get("id")
    if not _valid_id(session_id) or not path.name.endswith(f"_{session_id}.jsonl"):
        return None
    cwd = header.get("cwd")
    folder = cwd if isinstance(cwd, str) and os.path.isabs(cwd) else ""
    title = ""
    for lines in (tail, head):
        for record in _records(reversed(lines), _type_needle("session_info")):
            if record.get("type") == "session_info":
                title = clean_title(record.get("name"))
                if title:
                    break
        if title:
            break
    title = title or clean_title(_pi_prompt(head))
    return PastSession("pi", session_id, folder, title, mtime, home)


# -- Stores --

def _scan(directory: Path, budget: list[int]) -> list[os.DirEntry]:
    found = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                budget[0] -= 1
                if budget[0] < 0:
                    break
                found.append(entry)
    except OSError:
        return []
    return found


def _subdirs(directory: Path, budget: list[int], *, newest_name_first=False) -> list[Path]:
    dirs = []
    for entry in _scan(directory, budget):
        try:
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry)
        except OSError:
            continue
    if newest_name_first:
        dirs.sort(key=lambda entry: entry.name, reverse=True)
    return [Path(entry.path) for entry in dirs]


def _files(directory: Path, budget: list[int], prefix: str, suffix: str,
           found: list[tuple[float, Path]]) -> None:
    for entry in _scan(directory, budget):
        name = entry.name
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            found.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
        except OSError:
            continue


def _codex_days(sessions: Path, budget: list[int]) -> Iterator[Path]:
    for year in _subdirs(sessions, budget, newest_name_first=True):
        for month in _subdirs(year, budget, newest_name_first=True):
            yield from _subdirs(month, budget, newest_name_first=True)


def transcripts(harness: str, home: Path, *, enough: int | None = None,
                sessions: Path | None = None) -> list[tuple[float, Path]]:
    """The transcript files of ``home``, newest first, within ``MAX_ENTRIES`` entries.

    Codex day folders are walked newest first and the walk stops once
    ``enough`` files were found (none: the whole budget). ``sessions`` is
    Pi's session dir when it is not ``home/sessions``.
    """
    budget = [MAX_ENTRIES]
    found: list[tuple[float, Path]] = []
    if harness == "claude":
        for project in _subdirs(home / "projects", budget):
            _files(project, budget, "", ".jsonl", found)
    elif harness == "codex":
        for day in _codex_days(home / "sessions", budget):
            _files(day, budget, "rollout-", ".jsonl", found)
            if enough is not None and len(found) >= enough:
                break
    elif harness == "pi":
        for folder in _subdirs(sessions or home / "sessions", budget):
            _files(folder, budget, "", ".jsonl", found)
    found.sort(key=lambda item: item[0], reverse=True)
    return found


def read_transcript(harness: str, home: Path, path: Path, mtime: float,
                    names: dict[str, str] | None = None) -> PastSession | None:
    """The session ``path`` holds; None when it is unreadable or not a session."""
    stamp = int(mtime)
    try:
        if harness == "claude":
            return _read_claude(path, home, stamp)
        if harness == "codex":
            return _read_codex(path, home, stamp,
                               codex_thread_names(home) if names is None else names)
        if harness == "pi":
            return _read_pi(path, home, stamp)
    except (OSError, ValueError):
        return None
    return None


def scan(harness: str, home: Path, *, sessions: Path | None = None) -> list[PastSession]:
    """The newest sessions ``home`` holds for ``harness``, newest first.

    Files are read newest first until ``MAX_SESSIONS`` are kept, at most
    ``MAX_FILES`` (Codex: ``MAX_CODEX_FILES``) of them.
    """
    names = codex_thread_names(home) if harness == "codex" else {}
    limit = MAX_CODEX_FILES if harness == "codex" else MAX_FILES
    kept: list[PastSession] = []
    seen: set[str] = set()
    for mtime, path in transcripts(harness, home, enough=limit, sessions=sessions)[:limit]:
        if len(kept) >= MAX_SESSIONS:
            break
        session = read_transcript(harness, home, path, mtime, names)
        if session is None or session.session_id in seen:
            continue
        seen.add(session.session_id)
        kept.append(session)
    return kept


def _named(harness: str, name: str, session_id: str) -> bool:
    if harness == "claude":
        return name == f"{session_id}.jsonl"
    if harness == "codex":
        return name.endswith(f"-{session_id}.jsonl")
    return name.endswith(f"_{session_id}.jsonl")


def locate(harness: str, home: Path, session_id: str, *,
           sessions: Path | None = None) -> PastSession | None:
    """The session ``session_id`` in ``home``, also past the listing caps."""
    if not _valid_id(session_id):
        return None
    for mtime, path in transcripts(harness, home, sessions=sessions):
        if _named(harness, path.name, session_id):
            session = read_transcript(harness, home, path, mtime)
            if session is not None and session.session_id == session_id:
                return session
    return None
