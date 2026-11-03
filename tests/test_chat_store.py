"""The chat store: schema, file modes, connection roles, limits and retention.

Real SQLite files in the private test state dir; no process or multiplexer.
"""

import os
import sqlite3
import stat
import sys
import threading

import pytest

from tongs_agents.chat import store as chat_store
from tongs_agents.chat.store import (
    ChatBusy,
    ChatError,
    ChatLimited,
    ChatRefused,
    ChatStore,
    ChatUnavailable,
)

ALPHA = "a" * 32
BETA = "b" * 32
GAMMA = "c" * 32
NOW = 1_800_000_000_000


def put(maintainer, launch_id, handle, level="chat", start="100"):
    return maintainer.put_grant(
        launch_id=launch_id, handle=handle, level=level, process_start=start,
        pane_session="agents", pane_id=handle, socket_dir=None, via="cli",
        profile=handle, harness="claude",
    )


@pytest.fixture
def maintainer():
    with ChatStore.maintainer(create=True) as current:
        put(current, ALPHA, "alpha")
        put(current, BETA, "beta")
        yield current


@pytest.fixture
def alpha(maintainer):
    with ChatStore.agent(ALPHA) as current:
        yield current


def denied(connection, sql, params=()):
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        connection.execute(sql, params)


def test_nothing_opens_the_file_before_the_maintainer_creates_it():
    assert not chat_store.exists()
    for opener in (lambda: ChatStore.agent(ALPHA), lambda: ChatStore.hook(ALPHA),
                   ChatStore.maintainer):
        with pytest.raises(ChatUnavailable):
            opener()
    assert not chat_store.db_path().exists()
    with pytest.raises(ChatRefused):
        chat_store.connect(chat_store.ROLE_AGENT, create=True)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_the_file_is_private_and_in_wal_mode(maintainer):
    directory = chat_store.chat_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    names = sorted(path.name for path in directory.iterdir())
    assert names == ["chat.db", "chat.db-shm", "chat.db-wal"]
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    pragma = maintainer.connection.execute
    assert pragma("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert pragma("PRAGMA synchronous").fetchone()[0] == 1
    assert pragma("PRAGMA foreign_keys").fetchone()[0] == 1
    assert pragma("PRAGMA user_version").fetchone()[0] == chat_store.SCHEMA_VERSION
    assert [channel.name for channel in maintainer.channels()] == ["general"]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlinked_chat_file_is_refused(tmp_path):
    chat_store.chat_dir().mkdir(parents=True, mode=0o700)
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    os.symlink(target, chat_store.db_path())
    with pytest.raises(ChatRefused, match="regular file"):
        ChatStore.maintainer(create=True)


def test_a_newer_schema_is_refused_and_left_alone(maintainer):
    maintainer.connection.execute("PRAGMA user_version = 2")
    with pytest.raises(ChatRefused, match="newer"):
        ChatStore.agent(ALPHA)
    with pytest.raises(ChatRefused, match="newer"):
        ChatStore.maintainer(create=True)
    raw = sqlite3.connect(chat_store.db_path())
    assert raw.execute("PRAGMA user_version").fetchone()[0] == 2
    raw.close()


@pytest.mark.parametrize("sql", [
    "INSERT INTO grants VALUES ('{g}', 'x', 'chat', '1', 's', '1', NULL, 1, 'cli')",
    "UPDATE grants SET level = 'chat+wake'",
    "DELETE FROM grants",
    "INSERT INTO participants VALUES ('{g}', 'agent', NULL, NULL, 1)",
    "UPDATE participants SET kind = 'maintainer'",
    "INSERT INTO channels (name, created, created_by) VALUES ('x', 1, 'maintainer')",
    "UPDATE channels SET archived = 1",
    "DELETE FROM messages",
    "DELETE FROM read_cursors",
    "CREATE TABLE extra (a)",
    "DROP TABLE messages",
    "PRAGMA user_version = 9",
    "ATTACH DATABASE ':memory:' AS other",
])
def test_the_agent_role_cannot_change_grants_participants_channels_or_schema(alpha, sql):
    denied(alpha.connection, sql.format(g=GAMMA))


def test_the_hook_role_only_updates_deliveries(maintainer):
    with ChatStore.hook(ALPHA) as hook:
        hook.connection.execute(
            "UPDATE deliveries SET injected_through = 5 WHERE participant = ?", (ALPHA,)
        )
        denied(hook.connection, "INSERT INTO messages (channel_id, author, author_handle, "
               "body, created) VALUES (1, ?, 'alpha', 'x', 1)", (ALPHA,))
        denied(hook.connection, "INSERT INTO read_cursors VALUES (?, 1, 9)", (ALPHA,))
        denied(hook.connection, "DELETE FROM deliveries")
        with pytest.raises(ChatRefused):
            hook.post("general", "hello")
    assert maintainer.connection.execute(
        "SELECT injected_through FROM deliveries WHERE participant = ?", (ALPHA,)
    ).fetchone()[0] == 5


def test_maintainer_only_operations_refuse_other_roles(alpha):
    with pytest.raises(ChatRefused):
        alpha.create_channel("ops")
    with pytest.raises(ChatRefused):
        alpha.delete_grant(BETA)
    with pytest.raises(ChatRefused):
        put(alpha, GAMMA, "gamma")
    with pytest.raises(ChatRefused):
        alpha.prune()


def test_post_sets_author_from_the_store_participant_and_parses_mentions(maintainer, alpha):
    posted = alpha.post("general", "@beta and @maintainer, not @nobody or @alpha "
                        "or mail@beta", now=NOW)
    assert posted.mentioned == ("beta", "maintainer")
    message = maintainer.message(posted.id)
    assert (message.author, message.author_handle, message.author_kind) == (
        ALPHA, "alpha", "agent",
    )
    targets = maintainer.connection.execute(
        "SELECT target FROM mentions WHERE message_id = ? ORDER BY target", (posted.id,)
    ).fetchall()
    assert targets == [(BETA,), ("maintainer",)]


def test_an_ungranted_agent_cannot_post(maintainer, alpha):
    maintainer.delete_grant(ALPHA)
    with pytest.raises(ChatRefused, match="not granted"):
        alpha.post("general", "hello")


@pytest.mark.parametrize("body,error", [
    ("", ChatError),
    ("   \n", ChatError),
    ("x" * 8193, ChatLimited),
    ("bell\x07", ChatError),
    ("escape \x1b[31m", ChatError),
    (b"bytes", ChatError),
])
def test_bodies_are_checked(alpha, body, error):
    with pytest.raises(error):
        alpha.post("general", body)


def test_a_body_of_exactly_the_limit_and_tabs_and_newlines_pass(alpha):
    assert alpha.post("general", "x" * 8192).id
    assert alpha.post("general", "a\tb\r\nc").id
    assert alpha.messages("general", after_id=0)[-1].body == "a\tb\nc"


def test_the_body_check_constraint_backs_the_writer(maintainer):
    with pytest.raises(sqlite3.IntegrityError):
        maintainer.connection.execute(
            "INSERT INTO messages (channel_id, author, author_handle, body, created) "
            "VALUES (1, 'maintainer', 'maintainer', ?, 1)", ("x" * 8193,),
        )


def test_more_than_twenty_mentions_are_refused(maintainer):
    for number in range(21):
        put(maintainer, f"{number:032x}", f"h{number}")
    body = " ".join(f"@h{number}" for number in range(21))
    with pytest.raises(ChatLimited, match="mentions"):
        maintainer.post("general", body)
    assert maintainer.post("general", " ".join(f"@h{n}" for n in range(20))).mentioned


def test_posts_are_rate_limited_per_author(maintainer, alpha):
    for number in range(30):
        alpha.post("general", f"post {number}", now=NOW + number)
    with pytest.raises(ChatLimited, match="rate limit"):
        alpha.post("general", "one more", now=NOW + 100)
    maintainer.post("general", "the maintainer is counted apart", now=NOW + 100)
    assert alpha.post("general", "a minute later", now=NOW + 60_001).id


def test_threads_need_a_root_in_the_same_channel(maintainer, alpha):
    maintainer.create_channel("ops")
    root = maintainer.post("general", "root")
    reply = alpha.post("general", "reply", thread=root.id)
    assert reply.thread_id == root.id
    with pytest.raises(ChatError, match="is a reply"):
        alpha.post("general", "nested", thread=reply.id)
    with pytest.raises(ChatError, match="another channel"):
        alpha.post("ops", "elsewhere", thread=root.id)
    with pytest.raises(ChatError, match="no message"):
        alpha.post("general", "missing", thread=999)
    thread = alpha.messages("general", thread=root.id, after_id=0)
    assert [message.id for message in thread] == [root.id, reply.id]


def test_archived_channels_take_no_posts(maintainer, alpha):
    maintainer.create_channel("old")
    maintainer.archive_channel("old")
    with pytest.raises(ChatRefused, match="archived"):
        alpha.post("old", "hi")
    assert "old" not in [channel.name for channel in alpha.channels()]
    with pytest.raises(ChatError, match="no channel"):
        alpha.post("missing", "hi")


def test_cursors_unread_counts_and_mentions(maintainer, alpha):
    first = maintainer.post("general", "@alpha first")
    alpha.post("general", "own posts are never unread")
    second = maintainer.post("general", "@alpha second")
    assert [channel.unread for channel in alpha.channels()] == [2]
    assert [message.id for message in alpha.unread_mentions()] == [second.id, first.id]
    assert alpha.unread_mention_count() == 2
    assert [m.id for m in alpha.messages("general", limit=1)] == [first.id]
    assert alpha.mark_read("general", first.id) == first.id
    assert alpha.mark_read("general", 0) == first.id
    assert [message.id for message in alpha.unread_mentions()] == [second.id]
    assert [m.id for m in alpha.messages("general")][-1] == second.id
    assert maintainer.cursor(1) == 0


def test_read_arguments_are_checked(alpha):
    for kwargs in ({"limit": 0}, {"limit": 51}, {"after_id": -1}, {"thread": 0}):
        with pytest.raises(ChatError):
            alpha.messages("general", **kwargs)


def test_handles_are_unique_among_grants(maintainer):
    with pytest.raises(ChatRefused, match="already granted"):
        put(maintainer, GAMMA, "alpha")
    for handle in ("maintainer", "Upper", "-dash", "x" * 33, ""):
        with pytest.raises(ChatError):
            put(maintainer, GAMMA, handle)
    regrant = put(maintainer, ALPHA, "alpha", level="chat+wake", start="200")
    assert (regrant.level, regrant.process_start) == ("chat+wake", "200")


def test_a_post_over_the_file_cap_names_prune(maintainer, monkeypatch):
    monkeypatch.setattr(chat_store, "MAX_FILE_BYTES", 1)
    with pytest.raises(ChatLimited, match="chat prune"):
        maintainer.post("general", "too big")


def test_prune_frees_room_under_the_file_cap(maintainer, monkeypatch):
    day = 86_400_000
    for index in range(400):
        stamp = NOW - 40 * day + index * 3000
        maintainer.post("general", f"old {index} " + "x" * 2000, now=stamp)
    used = maintainer.connection.execute(
        "SELECT (page_count - freelist_count) * page_size "
        "FROM pragma_page_count, pragma_freelist_count, pragma_page_size"
    ).fetchone()[0]
    monkeypatch.setattr(chat_store, "MAX_FILE_BYTES", used - 1)
    with pytest.raises(ChatLimited):
        maintainer.post("general", "refused")
    assert maintainer.prune(now=NOW) == 400
    assert maintainer.post("general", "fits again", now=NOW).id


def test_prune_keeps_roots_with_recent_replies(maintainer, alpha):
    day = 86_400_000
    old_root = maintainer.post("general", "old root @alpha", now=NOW - 40 * day)
    lone = maintainer.post("general", "old alone", now=NOW - 40 * day)
    old_reply = alpha.post("general", "old reply", thread=old_root.id, now=NOW - 39 * day)
    fresh = alpha.post("general", "fresh reply", thread=old_root.id, now=NOW)
    gone_root = maintainer.post("general", "gone root", now=NOW - 40 * day)
    alpha.post("general", "gone reply", thread=gone_root.id, now=NOW - 35 * day)
    assert maintainer.prune(now=NOW) == 4
    left = [message.id for message in maintainer.messages("general", after_id=0)]
    assert left == [old_root.id, fresh.id]
    assert lone.id not in left and old_reply.id not in left


def test_purge_removes_the_file_and_its_wal(maintainer):
    maintainer.post("general", "bye")
    removed = chat_store.purge()
    assert sorted(path.name for path in removed) == ["chat.db", "chat.db-shm", "chat.db-wal"]
    assert not chat_store.exists()
    assert list(chat_store.chat_dir().iterdir()) == []


def test_a_locked_file_reports_busy_never_drops_silently(maintainer):
    blocker = sqlite3.connect(chat_store.db_path(), timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with ChatStore.agent(ALPHA, busy_ms=50) as alpha:
            with pytest.raises(ChatBusy, match="busy, retry"):
                alpha.post("general", "blocked")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert maintainer.messages("general", after_id=0) == []


def test_concurrent_writers_keep_every_message(maintainer):
    errors = []

    def write(launch_id):
        try:
            with ChatStore.agent(launch_id) as current:
                for number in range(20):
                    current.post("general", f"{launch_id[:1]} {number}")
        except ChatError as error:
            errors.append(error)

    threads = [threading.Thread(target=write, args=(launch_id,)) for launch_id in (ALPHA, BETA)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(maintainer.messages("general", after_id=0, limit=50)) == 40


def test_file_identity_changes_when_the_file_is_recreated(maintainer):
    assert chat_store.file_identity() is not None
    maintainer.close()
    chat_store.purge()
    assert chat_store.file_identity() is None
    ChatStore.maintainer(create=True).close()
    assert chat_store.file_identity() is not None
