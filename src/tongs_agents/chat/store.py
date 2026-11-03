"""The chat store: ``state_dir()/chat/chat.db``, schema v1, roles and limits.

The file is created only by the maintainer (the first grant creates it), in a
private directory (0700) and at 0600; SQLite gives its WAL and shm files the
same mode. Every connection sets ``journal_mode=WAL``, ``synchronous=NORMAL``,
``foreign_keys=ON`` and a busy timeout (5000 ms, 200 ms for the hook), then
installs the SQLite authorizer of its role:

- maintainer (sidebar and CLI): nothing denied;
- agent (an MCP server of a granted session): no write to ``grants``,
  ``participants`` or ``channels``, no delete and no schema change; it may add
  messages and mentions, its read cursors and its ``deliveries`` row, and the
  statements here only ever name the caller's own participant id;
- hook: reads plus UPDATE of ``deliveries`` only.

The authorizer is defence in depth against our own bugs, not a security
boundary: any process of the same user can open the file directly.

A file whose ``user_version`` is newer than ``SCHEMA_VERSION`` is refused and
never rewritten. Limits: body 1 to 8192 bytes, at most 20 mentions, 30 posts
per minute per author, posts refused once the file passes 64 MiB, messages
older than 30 days removed by ``prune``.
"""

from __future__ import annotations

import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from tongs_agents import paths
from tongs_agents.models import valid_launch_value

CHAT_DIR = "chat"
DB_NAME = "chat.db"
SCHEMA_VERSION = 1
MAINTAINER = "maintainer"
DEFAULT_CHANNEL = "general"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
MENTION_RE = re.compile(r"(?<![A-Za-z0-9_@-])@([a-z0-9][a-z0-9-]{0,31})(?![A-Za-z0-9_-])")
LEVEL_CHAT = "chat"
LEVEL_WAKE = "chat+wake"
LEVELS = (LEVEL_CHAT, LEVEL_WAKE)
VIA = ("sidebar", "cli")
MAX_BODY_BYTES = 8192
MAX_MENTIONS = 20
POST_RATE_PER_MINUTE = 30
RATE_WINDOW_MS = 60_000
RETENTION_DAYS = 30
MAX_FILE_BYTES = 64 * 1024 * 1024
BUSY_TIMEOUT_MS = 5000
HOOK_BUSY_TIMEOUT_MS = 200
READ_LIMIT_DEFAULT = 20
READ_LIMIT_MAX = 50
INBOX_MENTIONS = 10
WAKE_INTERVAL_MS = 120_000
WAKE_WINDOW_MS = 3_600_000
WAKES_PER_WINDOW = 10
WAKE_LOOP_MESSAGES = 6

ROLE_MAINTAINER = "maintainer"
ROLE_AGENT = "agent"
ROLE_HOOK = "hook"
ROLES = (ROLE_MAINTAINER, ROLE_AGENT, ROLE_HOOK)

SCHEMA = """
CREATE TABLE participants (
  id TEXT PRIMARY KEY,                    -- 'maintainer' or a 32-hex launch id
  kind TEXT NOT NULL CHECK (kind IN ('maintainer', 'agent')),
  profile TEXT,
  harness TEXT,
  created INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE grants (
  launch_id TEXT PRIMARY KEY REFERENCES participants(id),
  handle TEXT NOT NULL UNIQUE CHECK (length(handle) BETWEEN 1 AND 32 AND handle <> 'maintainer'),
  level TEXT NOT NULL CHECK (level IN ('chat', 'chat+wake')),
  process_start TEXT NOT NULL,            -- claimed start token at grant time
  pane_session TEXT NOT NULL,
  pane_id TEXT NOT NULL,
  socket_dir TEXT,
  granted_at INTEGER NOT NULL,
  granted_via TEXT NOT NULL CHECK (granted_via IN ('sidebar', 'cli'))
) WITHOUT ROWID;
CREATE TABLE channels (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 32),
  created INTEGER NOT NULL,
  created_by TEXT NOT NULL REFERENCES participants(id),
  archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1))
);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  thread_id INTEGER REFERENCES messages(id), -- root message of the thread, NULL for a root
  author TEXT NOT NULL REFERENCES participants(id),
  author_handle TEXT NOT NULL,            -- handle when posted
  body TEXT NOT NULL CHECK (length(CAST(body AS BLOB)) BETWEEN 1 AND 8192),
  created INTEGER NOT NULL                -- unix ms
);
CREATE INDEX messages_by_channel ON messages(channel_id, id);
CREATE INDEX messages_by_thread ON messages(thread_id, id) WHERE thread_id IS NOT NULL;
CREATE TABLE mentions (
  target TEXT NOT NULL REFERENCES participants(id),
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  PRIMARY KEY (target, message_id)
) WITHOUT ROWID;
CREATE TABLE read_cursors (
  participant TEXT NOT NULL REFERENCES participants(id),
  channel_id INTEGER NOT NULL REFERENCES channels(id),
  last_read INTEGER NOT NULL DEFAULT 0,   -- message id
  PRIMARY KEY (participant, channel_id)
) WITHOUT ROWID;
CREATE TABLE deliveries (
  participant TEXT PRIMARY KEY REFERENCES participants(id),
  injected_through INTEGER NOT NULL DEFAULT 0,
  last_injected_at INTEGER,
  tools_listed_at INTEGER,
  last_wake INTEGER,
  wake_window_start INTEGER,
  wakes_in_window INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
"""

_WRITES = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_ALWAYS_DENIED = {
    getattr(sqlite3, name) for name in (
        "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_ALTER_TABLE", "SQLITE_REINDEX",
        "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX",
        "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
        "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW",
        "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
        "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
        "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW",
        "SQLITE_DROP_VTABLE",
    ) if hasattr(sqlite3, name)
}
_READ_PRAGMAS = {"data_version", "user_version"}
_ROLE_WRITES = {
    ROLE_AGENT: {
        "messages": {sqlite3.SQLITE_INSERT},
        "mentions": {sqlite3.SQLITE_INSERT},
        "read_cursors": {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE},
        "deliveries": {sqlite3.SQLITE_UPDATE},
    },
    ROLE_HOOK: {"deliveries": {sqlite3.SQLITE_UPDATE}},
}


class ChatError(Exception):
    """A chat call that did not run."""

    outcome = "error"

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class ChatUnavailable(ChatError):
    """There is no chat yet: the maintainer has not granted any session."""

    outcome = "unavailable"


class ChatRefused(ChatError):
    """The caller may not do this."""

    outcome = "refused"


class ChatLimited(ChatRefused):
    """A post over a rate, size or mention limit."""

    outcome = "limit"


class ChatBusy(ChatError):
    """The file stayed locked for the whole busy timeout; the caller may retry."""

    outcome = "busy"


@dataclass(frozen=True)
class Grant:
    launch_id: str
    handle: str
    level: str
    process_start: str
    pane_session: str
    pane_id: str
    socket_dir: str | None
    granted_at: int
    granted_via: str
    profile: str | None = None
    harness: str | None = None
    tools_listed_at: int | None = None

    @property
    def wake(self) -> bool:
        return self.level == LEVEL_WAKE

    def to_json(self) -> dict:
        return {
            "launch_id": self.launch_id,
            "handle": self.handle,
            "level": self.level,
            "profile": self.profile,
            "harness": self.harness,
            "pane": f"{self.pane_session}:{self.pane_id}",
            "granted_at": self.granted_at,
            "granted_via": self.granted_via,
            "tools_listed_at": self.tools_listed_at,
        }


def grant_matches(grant: Grant | None, identity) -> bool:
    """True when ``grant`` was given to ``identity``'s launch and claimed process.

    ``identity`` is anything with ``launch_id`` and ``process_start`` (the MCP
    server's ``AgentIdentity`` or the hook's lighter one), so a grant dies with
    the process it was given to.
    """
    return (
        grant is not None
        and grant.launch_id == identity.launch_id
        and grant.process_start == identity.process_start
    )


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    archived: bool
    unread: int = 0
    last_id: int = 0


@dataclass(frozen=True)
class Message:
    id: int
    channel: str
    thread_id: int | None
    author: str
    author_handle: str
    author_kind: str
    body: str
    created: int


@dataclass(frozen=True)
class Posted:
    id: int
    channel: str
    thread_id: int | None
    mentioned: tuple[str, ...]


def now_ms() -> int:
    return int(time.time() * 1000)


def is_launch_id(value) -> bool:
    """32 lowercase hex, as ``launches.is_launch_id``; kept here so the hook can
    import the store without the launcher's imports."""
    return valid_launch_value("launch_id", value)


def is_name(value) -> bool:
    """A valid handle or channel name: ``^[a-z0-9][a-z0-9-]{0,31}$``."""
    return isinstance(value, str) and bool(NAME_RE.match(value))


def chat_dir() -> Path:
    return paths.state_dir() / CHAT_DIR


def db_path() -> Path:
    return chat_dir() / DB_NAME


def exists() -> bool:
    """True when the chat file exists (a maintainer has granted a session)."""
    try:
        return stat.S_ISREG(db_path().lstat().st_mode)
    except OSError:
        return False


def file_identity() -> tuple[int, int] | None:
    """(device, inode) of the chat file, so a purged and recreated file is noticed."""
    try:
        info = db_path().lstat()
    except OSError:
        return None
    return (info.st_dev, info.st_ino) if stat.S_ISREG(info.st_mode) else None


def _authorizer(role: str):
    writable = _ROLE_WRITES[role]

    def authorize(action, arg1, arg2, database, source):
        if action in _WRITES:
            allowed = writable.get(arg1 or "", ())
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY
        if action in _ALWAYS_DENIED:
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA:
            name = (arg1 or "").lower()
            if name in _READ_PRAGMAS and arg2 is None:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorize


def _check_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ChatUnavailable(
            "chat is off: no session has been granted yet "
            "('tongs-agents chat grant LAUNCH_ID' or the sidebar)"
        ) from None
    except OSError as error:
        raise ChatError(f"{path}: cannot use the chat file: {error.strerror}") from None
    if not stat.S_ISREG(info.st_mode):
        raise ChatRefused(f"{path}: refusing to use a chat file that is not a regular file")
    if os.name != "nt":
        if info.st_uid != os.getuid():
            raise ChatRefused(f"{path}: owned by another user")
        if info.st_mode & 0o077:
            os.chmod(path, 0o600)


def _create_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        os.close(os.open(path, flags, 0o600))
    except FileExistsError:
        pass


def _uri(path: Path) -> str:
    """The ``file:`` URI SQLite opens, never creating the file.

    Built by hand: ``Path.as_uri`` imports ``urllib.request`` on Python 3.14,
    which cost the hook about 10 ms per run (#32).
    """
    text = Path(os.path.abspath(path)).as_posix()
    if not text.startswith("/"):
        text = "/" + text
    return "file:" + quote(os.fsencode(text), safe="/:") + "?mode=rw"


def _translate(error: sqlite3.Error) -> ChatError:
    text = str(error)
    if isinstance(error, sqlite3.OperationalError) and (
        "locked" in text or "busy" in text
    ):
        return ChatBusy("chat is busy, retry")
    if "not authorized" in text:
        return ChatRefused(f"chat store refused the statement: {text}")
    return ChatError(f"chat store error: {text}")


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            for statement in SCHEMA.split(";\n"):
                if statement.strip():
                    connection.execute(statement)
            created = now_ms()
            connection.execute(
                "INSERT INTO participants (id, kind, created) VALUES (?, 'maintainer', ?)",
                (MAINTAINER, created),
            )
            connection.execute(
                "INSERT INTO channels (name, created, created_by) VALUES (?, ?, ?)",
                (DEFAULT_CHANNEL, created, MAINTAINER),
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def connect(role: str, *, create: bool = False, busy_ms: int | None = None) -> sqlite3.Connection:
    """Open the chat file for ``role``; only the maintainer may ``create`` it."""
    if role not in ROLES:
        raise ValueError(f"unknown chat role {role!r}")
    if create and role != ROLE_MAINTAINER:
        raise ChatRefused("only the maintainer creates the chat file")
    if busy_ms is None:
        busy_ms = HOOK_BUSY_TIMEOUT_MS if role == ROLE_HOOK else BUSY_TIMEOUT_MS
    path = db_path()
    if create:
        try:
            paths.state_dir(create=True)
            paths.ensure_private_dir(path.parent)
            _create_file(path)
        except OSError as error:
            raise ChatError(f"cannot create the chat file: {error}") from None
    elif not paths.is_private_dir(path.parent) and path.parent.exists():
        raise ChatRefused(f"{path.parent}: not a private directory")
    _check_file(path)
    try:
        connection = sqlite3.connect(
            _uri(path), uri=True, timeout=busy_ms / 1000, isolation_level=None,
            check_same_thread=False,
        )
    except sqlite3.Error as error:
        raise _translate(error) from None
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and create:
            _create_schema(connection)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            raise ChatUnavailable("chat is off: the chat file has no schema yet")
        if version > SCHEMA_VERSION:
            raise ChatRefused(
                f"the chat file has schema version {version}, newer than this "
                f"tongs-agents understands ({SCHEMA_VERSION}); refusing to use it"
            )
        if role != ROLE_MAINTAINER:
            connection.set_authorizer(_authorizer(role))
    except sqlite3.Error as error:
        connection.close()
        raise _translate(error) from None
    except BaseException:
        connection.close()
        raise
    return connection


def _preview_body(body) -> str:
    if not isinstance(body, str):
        raise ChatError("body must be a string")
    text = body.replace("\r\n", "\n")
    for char in text:
        code = ord(char)
        if (code < 32 or 127 <= code < 160) and char not in "\t\n":
            raise ChatError(
                f"body holds control character U+{code:04X}; only tab and newline are allowed"
            )
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ChatError("body is not valid UTF-8") from None
    if not text.strip():
        raise ChatError("body is empty")
    if size > MAX_BODY_BYTES:
        raise ChatLimited(f"body is {size} bytes; the limit is {MAX_BODY_BYTES}")
    return text


def parse_mentions(body: str) -> list[str]:
    """Distinct ``@handle`` tokens in order of first appearance."""
    return list(dict.fromkeys(match.group(1) for match in MENTION_RE.finditer(body)))


class ChatStore:
    """One connection plus the participant it acts as.

    Open with ``maintainer()``, ``agent(launch_id)`` or ``hook(launch_id)``;
    the agent and hook forms need the file to exist already.
    """

    def __init__(self, connection: sqlite3.Connection, participant: str, role: str):
        self.connection = connection
        self.participant = participant
        self.role = role

    @classmethod
    def maintainer(cls, *, create: bool = False, busy_ms: int | None = None) -> ChatStore:
        return cls(connect(ROLE_MAINTAINER, create=create, busy_ms=busy_ms), MAINTAINER,
                   ROLE_MAINTAINER)

    @classmethod
    def agent(cls, launch_id: str, *, busy_ms: int | None = None) -> ChatStore:
        if not is_launch_id(launch_id):
            raise ChatRefused("not a tongs-agents launch id")
        return cls(connect(ROLE_AGENT, busy_ms=busy_ms), launch_id, ROLE_AGENT)

    @classmethod
    def hook(cls, launch_id: str, *, busy_ms: int | None = None) -> ChatStore:
        if not is_launch_id(launch_id):
            raise ChatRefused("not a tongs-agents launch id")
        return cls(connect(ROLE_HOOK, busy_ms=busy_ms), launch_id, ROLE_HOOK)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> ChatStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- Low level --

    def _rows(self, sql: str, params: Iterable = ()) -> list[tuple]:
        try:
            return self.connection.execute(sql, tuple(params)).fetchall()
        except sqlite3.Error as error:
            raise _translate(error) from None

    def _write(self, work):
        """Run ``work()`` in one IMMEDIATE transaction; translate SQLite errors."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise _translate(error) from None
        try:
            result = work()
            self.connection.execute("COMMIT")
            return result
        except sqlite3.Error as error:
            self._rollback()
            raise _translate(error) from None
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def data_version(self) -> int:
        return self._rows("PRAGMA data_version")[0][0]

    def _require_maintainer(self) -> None:
        if self.role != ROLE_MAINTAINER:
            raise ChatRefused("only the maintainer may do this")

    # -- Grants --

    _GRANT_COLUMNS = (
        "g.launch_id, g.handle, g.level, g.process_start, g.pane_session, g.pane_id, "
        "g.socket_dir, g.granted_at, g.granted_via, p.profile, p.harness, d.tools_listed_at"
    )
    _GRANT_FROM = (
        "FROM grants g JOIN participants p ON p.id = g.launch_id "
        "LEFT JOIN deliveries d ON d.participant = g.launch_id"
    )

    def grant_for(self, launch_id: str) -> Grant | None:
        rows = self._rows(
            f"SELECT {self._GRANT_COLUMNS} {self._GRANT_FROM} WHERE g.launch_id = ?",
            (launch_id,),
        )
        return Grant(*rows[0]) if rows else None

    def grants(self) -> list[Grant]:
        rows = self._rows(
            f"SELECT {self._GRANT_COLUMNS} {self._GRANT_FROM} ORDER BY g.granted_at, g.handle"
        )
        return [Grant(*row) for row in rows]

    def handles(self) -> dict[str, str]:
        """Current grant handles mapped to their launch ids."""
        return dict(self._rows("SELECT handle, launch_id FROM grants"))

    def put_grant(self, *, launch_id: str, handle: str, level: str, process_start: str,
                  pane_session: str, pane_id: str, socket_dir: str | None, via: str,
                  profile: str, harness: str, now: int | None = None) -> Grant:
        """Write (or replace) the grant of one launch. Maintainer only."""
        self._require_maintainer()
        if not is_launch_id(launch_id):
            raise ChatRefused("not a tongs-agents launch id")
        if not is_name(handle) or handle == MAINTAINER:
            raise ChatError(f"invalid handle {handle!r:.40}: use 1 to 32 of a-z, 0-9 and -")
        if level not in LEVELS or via not in VIA:
            raise ChatError("invalid grant level or source")
        stamp = now_ms() if now is None else now

        def work():
            taken = self.connection.execute(
                "SELECT launch_id FROM grants WHERE handle = ?", (handle,)
            ).fetchone()
            if taken is not None and taken[0] != launch_id:
                raise ChatRefused(f"handle @{handle} is already granted to another launch")
            self.connection.execute(
                "INSERT INTO participants (id, kind, profile, harness, created) "
                "VALUES (?, 'agent', ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "profile = excluded.profile, harness = excluded.harness",
                (launch_id, profile, harness, stamp),
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO grants (launch_id, handle, level, process_start, "
                "pane_session, pane_id, socket_dir, granted_at, granted_via) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (launch_id, handle, level, process_start, pane_session, pane_id,
                 socket_dir, stamp, via),
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO deliveries (participant) VALUES (?)", (launch_id,)
            )

        self._write(work)
        return self.grant_for(launch_id)

    def delete_grant(self, launch_id: str) -> bool:
        """Remove the grant of one launch; True when there was one. Maintainer only."""
        self._require_maintainer()

        def work():
            cursor = self.connection.execute(
                "DELETE FROM grants WHERE launch_id = ?", (launch_id,)
            )
            return cursor.rowcount > 0

        return self._write(work)

    def record_tools_listed(self, now: int | None = None) -> None:
        """Note that this agent's client listed the chat tools."""
        stamp = now_ms() if now is None else now
        self._write(lambda: self.connection.execute(
            "UPDATE deliveries SET tools_listed_at = ? WHERE participant = ?",
            (stamp, self.participant),
        ))

    # -- Channels --

    def channels(self, *, include_archived: bool = False) -> list[Channel]:
        """Channels by name, with this participant's unread count and newest id."""
        try:
            rows = self.connection.execute(
                "SELECT c.id, c.name, c.archived, "
                "(SELECT count(*) FROM messages m WHERE m.channel_id = c.id "
                " AND m.id > coalesce(r.last_read, 0) AND m.author <> :me), "
                "(SELECT coalesce(max(m.id), 0) FROM messages m WHERE m.channel_id = c.id) "
                "FROM channels c LEFT JOIN read_cursors r "
                "ON r.channel_id = c.id AND r.participant = :me "
                "WHERE :all OR c.archived = 0 ORDER BY c.name",
                {"me": self.participant, "all": int(include_archived)},
            ).fetchall()
        except sqlite3.Error as error:
            raise _translate(error) from None
        return [Channel(row[0], row[1], bool(row[2]), row[3], row[4]) for row in rows]

    def channel(self, name, *, for_posting: bool = False) -> Channel:
        if not is_name(name):
            raise ChatError(f"invalid channel name {name!r:.40}")
        rows = self._rows("SELECT id, name, archived FROM channels WHERE name = ?", (name,))
        if not rows:
            raise ChatError(f"no channel #{name}")
        channel = Channel(rows[0][0], rows[0][1], bool(rows[0][2]))
        if for_posting and channel.archived:
            raise ChatRefused(f"channel #{name} is archived")
        return channel

    def create_channel(self, name: str, now: int | None = None) -> Channel:
        self._require_maintainer()
        if not is_name(name):
            raise ChatError(f"invalid channel name {name!r:.40}: use 1 to 32 of a-z, 0-9 and -")
        stamp = now_ms() if now is None else now

        def work():
            if self.connection.execute(
                "SELECT 1 FROM channels WHERE name = ?", (name,)
            ).fetchone():
                raise ChatError(f"channel #{name} already exists")
            self.connection.execute(
                "INSERT INTO channels (name, created, created_by) VALUES (?, ?, ?)",
                (name, stamp, MAINTAINER),
            )

        self._write(work)
        return self.channel(name)

    def archive_channel(self, name: str, archived: bool = True) -> None:
        self._require_maintainer()
        channel = self.channel(name)
        self._write(lambda: self.connection.execute(
            "UPDATE channels SET archived = ? WHERE id = ?", (int(archived), channel.id)
        ))

    # -- Messages --

    _MESSAGE_COLUMNS = (
        "m.id, c.name, m.thread_id, m.author, m.author_handle, p.kind, m.body, m.created"
    )
    _MESSAGE_FROM = (
        "FROM messages m JOIN channels c ON c.id = m.channel_id "
        "JOIN participants p ON p.id = m.author"
    )

    def message(self, message_id: int) -> Message | None:
        rows = self._rows(
            f"SELECT {self._MESSAGE_COLUMNS} {self._MESSAGE_FROM} WHERE m.id = ?",
            (message_id,),
        )
        return Message(*rows[0]) if rows else None

    def cursor(self, channel_id: int) -> int:
        rows = self._rows(
            "SELECT last_read FROM read_cursors WHERE participant = ? AND channel_id = ?",
            (self.participant, channel_id),
        )
        return rows[0][0] if rows else 0

    def messages(self, channel: str, *, thread: int | None = None,
                 after_id: int | None = None, limit: int = READ_LIMIT_DEFAULT,
                 latest: bool = False) -> list[Message]:
        """Messages of ``channel`` oldest first.

        ``after_id`` None means after this participant's read cursor (unread).
        ``thread`` limits the result to one thread (its root and replies).
        ``latest`` returns the newest ``limit`` messages instead of the oldest.
        """
        if type(limit) is not int or not 1 <= limit <= READ_LIMIT_MAX:
            raise ChatError(f"limit must be from 1 to {READ_LIMIT_MAX}")
        if after_id is not None and (type(after_id) is not int or after_id < 0):
            raise ChatError("after_id must be a message id (0 or more)")
        if thread is not None and (type(thread) is not int or thread <= 0):
            raise ChatError("thread must be a message id")
        found = self.channel(channel)
        floor = self.cursor(found.id) if after_id is None else after_id
        clauses = ["m.channel_id = ?", "m.id > ?"]
        params: list = [found.id, floor]
        if thread is not None:
            clauses.append("(m.id = ? OR m.thread_id = ?)")
            params += [thread, thread]
        order = "DESC" if latest else "ASC"
        rows = self._rows(
            f"SELECT {self._MESSAGE_COLUMNS} {self._MESSAGE_FROM} "
            f"WHERE {' AND '.join(clauses)} ORDER BY m.id {order} LIMIT ?",
            (*params, limit),
        )
        result = [Message(*row) for row in rows]
        return list(reversed(result)) if latest else result

    def mark_read(self, channel: str, through_id: int) -> int:
        """Advance this participant's cursor in ``channel``; it never moves back."""
        found = self.channel(channel)
        if type(through_id) is not int or through_id < 0:
            raise ChatError("through_id must be a message id")

        def work():
            self.connection.execute(
                "INSERT INTO read_cursors (participant, channel_id, last_read) "
                "VALUES (?, ?, ?) ON CONFLICT(participant, channel_id) DO UPDATE SET "
                "last_read = max(last_read, excluded.last_read)",
                (self.participant, found.id, through_id),
            )

        self._write(work)
        return self.cursor(found.id)

    def unread_mentions(self, limit: int = INBOX_MENTIONS) -> list[Message]:
        """Unread messages that mention this participant, newest first."""
        try:
            rows = self.connection.execute(
                f"SELECT {self._MESSAGE_COLUMNS} {self._MESSAGE_FROM} "
                "JOIN mentions n ON n.message_id = m.id AND n.target = :me "
                "LEFT JOIN read_cursors r ON r.participant = :me "
                "AND r.channel_id = m.channel_id "
                "WHERE m.id > coalesce(r.last_read, 0) AND m.author <> :me "
                "ORDER BY m.id DESC LIMIT :limit",
                {"me": self.participant, "limit": limit},
            ).fetchall()
        except sqlite3.Error as error:
            raise _translate(error) from None
        return [Message(*row) for row in rows]

    def unread_mention_count(self) -> int:
        try:
            return self.connection.execute(
                "SELECT count(*) FROM mentions n JOIN messages m ON m.id = n.message_id "
                "LEFT JOIN read_cursors r ON r.participant = :me "
                "AND r.channel_id = m.channel_id "
                "WHERE n.target = :me AND m.id > coalesce(r.last_read, 0) "
                "AND m.author <> :me",
                {"me": self.participant},
            ).fetchone()[0]
        except sqlite3.Error as error:
            raise _translate(error) from None

    def _author_handle(self) -> str:
        if self.participant == MAINTAINER:
            return MAINTAINER
        row = self.connection.execute(
            "SELECT handle FROM grants WHERE launch_id = ?", (self.participant,)
        ).fetchone()
        if row is None:
            raise ChatRefused("chat is not granted for this session")
        return row[0]

    def _check_file_size(self) -> None:
        # Pages on the freelist are reusable, so a pruned file counts only the
        # pages still in use: SQLite does not shrink the file on DELETE.
        try:
            pages = self.connection.execute("PRAGMA page_count").fetchone()[0]
            free = self.connection.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        except sqlite3.Error:
            return
        size = (pages - free) * page_size
        if size > MAX_FILE_BYTES:
            raise ChatLimited(
                f"the chat file passed {MAX_FILE_BYTES // (1024 * 1024)} MiB; the "
                "maintainer can free space with 'tongs-agents chat prune'"
            )

    def post(self, channel: str, body: str, *, thread: int | None = None,
             now: int | None = None) -> Posted:
        """Add a message as this participant; mentions are parsed from ``body``."""
        if self.role == ROLE_HOOK:
            raise ChatRefused("the hook does not post")
        text = _preview_body(body)
        if thread is not None and (type(thread) is not int or thread <= 0):
            raise ChatError("thread must be a message id")
        self._check_file_size()
        found = self.channel(channel, for_posting=True)
        stamp = now_ms() if now is None else now

        def work():
            connection = self.connection
            handle = self._author_handle()
            if thread is not None:
                root = connection.execute(
                    "SELECT channel_id, thread_id FROM messages WHERE id = ?", (thread,)
                ).fetchone()
                if root is None:
                    raise ChatError(f"no message {thread}")
                if root[0] != found.id:
                    raise ChatError(f"message {thread} is in another channel")
                if root[1] is not None:
                    raise ChatError(
                        f"message {thread} is a reply; reply to its thread root {root[1]}"
                    )
            recent = connection.execute(
                "SELECT count(*) FROM messages WHERE author = ? AND created > ?",
                (self.participant, stamp - RATE_WINDOW_MS),
            ).fetchone()[0]
            if recent >= POST_RATE_PER_MINUTE:
                raise ChatLimited(
                    f"rate limit: at most {POST_RATE_PER_MINUTE} posts per minute"
                )
            known = dict(connection.execute("SELECT handle, launch_id FROM grants"))
            known[MAINTAINER] = MAINTAINER
            mentioned = [
                name for name in parse_mentions(text)
                if name in known and known[name] != self.participant
            ]
            if len(mentioned) > MAX_MENTIONS:
                raise ChatLimited(
                    f"{len(mentioned)} mentions; the limit is {MAX_MENTIONS} per message"
                )
            cursor = connection.execute(
                "INSERT INTO messages (channel_id, thread_id, author, author_handle, body, "
                "created) VALUES (?, ?, ?, ?, ?, ?)",
                (found.id, thread, self.participant, handle, text, stamp),
            )
            message_id = cursor.lastrowid
            connection.executemany(
                "INSERT OR IGNORE INTO mentions (target, message_id) VALUES (?, ?)",
                [(known[name], message_id) for name in mentioned],
            )
            return Posted(message_id, found.name, thread, tuple(mentioned))

        return self._write(work)

    # -- Wake bookkeeping (docs/work/agent-chat.md, row 12) --

    def agent_streak(self, message_id: int) -> int:
        """Agent-authored messages in a row at the end of ``message_id``'s thread.

        Counts back from ``message_id`` through its thread (root and replies)
        and stops at the first maintainer message; at most
        ``WAKE_LOOP_MESSAGES``.
        """
        root = self._thread_root(message_id)
        rows = self._rows(
            "SELECT author FROM messages WHERE id <= ? AND (id = ? OR thread_id = ?) "
            "ORDER BY id DESC LIMIT ?",
            (message_id, root, root, WAKE_LOOP_MESSAGES),
        )
        streak = 0
        for (author,) in rows:
            if author == MAINTAINER:
                break
            streak += 1
        return streak

    def _thread_root(self, message_id: int) -> int:
        row = self._rows("SELECT coalesce(thread_id, id) FROM messages WHERE id = ?",
                         (message_id,))
        return row[0][0] if row else message_id

    def _wake_row(self, target: str) -> tuple | None:
        return self.connection.execute(
            "SELECT last_wake, wake_window_start, wakes_in_window FROM deliveries "
            "WHERE participant = ?",
            (target,),
        ).fetchone()

    @staticmethod
    def _wake_allowed(row: tuple | None, now: int) -> tuple[bool, int, int]:
        """(allowed, window start, wakes in window) for a wake at ``now``."""
        if row is None:
            return False, now, 0
        last, start, count = row
        if start is None or now - start >= WAKE_WINDOW_MS:
            start, count = now, 0
        allowed = (
            (last is None or now - last >= WAKE_INTERVAL_MS) and count < WAKES_PER_WINDOW
        )
        return allowed, start, count

    def wake_rate_ok(self, target: str, now: int | None = None) -> bool:
        """True when ``target`` may be woken now: 1 per 2 min and 10 per hour."""
        stamp = now_ms() if now is None else now
        try:
            return self._wake_allowed(self._wake_row(target), stamp)[0]
        except sqlite3.Error as error:
            raise _translate(error) from None

    def claim_wake(self, target: str, now: int | None = None) -> bool:
        """Reserve one wake of ``target`` under the rate limits; True when reserved.

        The poster's process wakes the target, so this is the one statement
        that updates another participant's ``deliveries`` row, and it touches
        only the wake columns. Check and update share one transaction, so two
        posters never both wake a target inside the limit. The hook never
        wakes.
        """
        if self.role == ROLE_HOOK:
            raise ChatRefused("the hook does not wake")
        stamp = now_ms() if now is None else now

        def work():
            allowed, start, count = self._wake_allowed(self._wake_row(target), stamp)
            if allowed:
                self.connection.execute(
                    "UPDATE deliveries SET last_wake = ?, wake_window_start = ?, "
                    "wakes_in_window = ? WHERE participant = ?",
                    (stamp, start, count + 1, target),
                )
            return allowed

        return self._write(work)

    # -- Retention --

    def prune(self, *, days: int = RETENTION_DAYS, now: int | None = None) -> int:
        """Delete messages older than ``days``; a root with a newer reply stays."""
        self._require_maintainer()
        cutoff = (now_ms() if now is None else now) - days * 86_400_000

        def work():
            cursor = self.connection.execute(
                "DELETE FROM messages WHERE created < :cutoff AND NOT EXISTS ("
                "SELECT 1 FROM messages r WHERE r.thread_id = messages.id "
                "AND r.created >= :cutoff)",
                {"cutoff": cutoff},
            )
            return cursor.rowcount

        return self._write(work)


def purge() -> list[Path]:
    """Delete the chat file with its WAL and shm files. Returns what was removed."""
    removed = []
    base = db_path()
    for path in (base, base.with_name(DB_NAME + "-wal"), base.with_name(DB_NAME + "-shm")):
        try:
            if stat.S_ISREG(path.lstat().st_mode):
                path.unlink()
                removed.append(path)
        except FileNotFoundError:
            continue
    return removed
