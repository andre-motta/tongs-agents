"""``tongs-agents chat``: the chat pane, grants and maintainer posts.

With no verb it runs the chat pane (channel list, message log and input). The
verbs act as the maintainer and every one of them, the pane included, refuses
inside an agent process tree (``TONGS_AGENTS_LAUNCH_ID`` set, or a live
claimed launch among this process's ancestors): agents use their MCP chat
tools. Exit codes: 0 ok, 1 error, 5 refused.

- ``grant LAUNCH_ID [--wake] [--as HANDLE]`` checks the pane through the
  control layer, shows pane, profile and launch id, and needs the maintainer
  to type the handle at an interactive terminal. There is no ``--yes``.
- ``revoke LAUNCH_ID``, ``grants``, ``channels``, ``channel-create NAME``,
  ``channel-archive NAME``, ``post CHANNEL TEXT [--thread ID]``,
  ``read CHANNEL``, ``prune`` and ``purge``. A post wakes the idle
  ``chat+wake`` sessions it mentions (``chat_wake``) and reports that on
  stderr.

See docs/chat.md.
"""

from __future__ import annotations

import json
import sys

from textual.app import App, ComposeResult
from textual.binding import Binding

from tongs_agents import chat_wake, control
from tongs_agents.app_config import AppConfigError, load_app_config
from tongs_agents.chat import frame, grants
from tongs_agents.chat import store as chat_store
from tongs_agents.chat.identity import maintainer_refusal
from tongs_agents.chat.store import (
    DEFAULT_CHANNEL,
    READ_LIMIT_DEFAULT,
    ChatError,
    ChatRefused,
    ChatStore,
)
from tongs_agents.commands import CommandParser
from tongs_agents.widgets.chat_panel import ChatPanel, message_text
from tongs_agents.widgets.shell import AppFooter, AppHeader, apply_theme

NAME = "chat"
HELP = "Agent chat: the chat pane, grants and maintainer posts"
STDIN = "-"
EXIT_OK = control.EXIT_OK
EXIT_ERROR = control.EXIT_ERROR
EXIT_REFUSED = control.EXIT_REFUSED


class ChatApp(App):
    """The standalone chat pane."""

    TITLE = "tongs-agents chat"
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=True)]

    def __init__(self, theme: str | None = None) -> None:
        super().__init__()
        self.theme_name = theme

    def compose(self) -> ComposeResult:
        yield AppHeader()
        yield ChatPanel(id="chat-panel")
        yield AppFooter()

    def on_mount(self) -> None:
        if self.theme_name:
            apply_theme(self, self.theme_name)


def add_arguments(parser):
    verbs = parser.add_subparsers(dest="verb", metavar="VERB")
    grant = verbs.add_parser("grant", help="Let one launched pane use chat (interactive)")
    grant.add_argument("launch_id", help="Launch id of a pane spawned by tongs-agents")
    grant.add_argument("--wake", action="store_true",
                       help="Also allow waking the pane on mention (chat+wake)")
    grant.add_argument("--as", dest="handle", metavar="HANDLE",
                       help="Handle to use instead of the profile name")
    revoke = verbs.add_parser("revoke", help="Take chat away from one launched pane")
    revoke.add_argument("launch_id", help="Launch id of a granted pane")
    listing = verbs.add_parser("grants", help="List chat grants and their state")
    listing.add_argument("--json", action="store_true", help="Print JSON")
    verbs.add_parser("channels", help="List channels with unread counts")
    create = verbs.add_parser("channel-create", help="Create a channel")
    create.add_argument("name", help="Channel name: 1 to 32 of a-z, 0-9 and -")
    archive = verbs.add_parser("channel-archive", help="Archive a channel (no new posts)")
    archive.add_argument("name", help="Channel name")
    archive.add_argument("--undo", action="store_true", help="Unarchive it")
    post = verbs.add_parser("post", help="Post a message as @maintainer")
    post.add_argument("channel", help="Channel name")
    post.add_argument("text", help="Message text, or - to read it from standard input")
    post.add_argument("--thread", type=int, help="Reply in the thread of this message id")
    read = verbs.add_parser("read", help="Print the newest messages of a channel")
    read.add_argument("channel", nargs="?", default=DEFAULT_CHANNEL,
                      help=f"Channel name (default {DEFAULT_CHANNEL})")
    read.add_argument("--thread", type=int, help="Only this thread")
    read.add_argument("--limit", type=int, default=READ_LIMIT_DEFAULT,
                      help=f"Messages to show, 1 to {chat_store.READ_LIMIT_MAX}")
    read.add_argument("--json", action="store_true", help="Print JSON")
    verbs.add_parser("prune", help=f"Delete messages older than {chat_store.RETENTION_DAYS} days")
    purge = verbs.add_parser("purge", help="Delete the chat file and every message")
    purge.add_argument("--yes", action="store_true", help="Do not ask for confirmation")


def _fail(error: ChatError) -> int:
    print(f"tongs-agents chat: {error.detail}", file=sys.stderr)
    return EXIT_REFUSED if isinstance(error, ChatRefused) else EXIT_ERROR


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, OSError, ValueError):
        return False


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def run_grant(args) -> int:
    if not _interactive():
        raise ChatRefused(
            "chat grant needs an interactive terminal to confirm; it has no --yes"
        )
    request = grants.prepare(args.launch_id, wake=args.wake, handle=args.handle,
                             actor="cli")
    print(f"Grant {request.level} to {request.describe()}")
    for note in request.notes:
        print(f"Note: {note}")
    typed = _ask(f"Type the handle to confirm (@{request.handle}): ")
    if typed is None or typed.strip().lstrip("@") != request.handle:
        print("Not granted.", file=sys.stderr)
        return EXIT_ERROR
    grant = grants.apply(request, via="cli", actor="cli")
    print(f"Granted {grant.level} to @{grant.handle} (launch {grant.launch_id}).")
    return EXIT_OK


def run_revoke(args) -> int:
    if grants.revoke(args.launch_id, actor="cli"):
        print(f"Revoked chat for launch {args.launch_id}.")
        return EXIT_OK
    print(f"Launch {args.launch_id} has no chat grant.", file=sys.stderr)
    return EXIT_ERROR


def run_grants(args) -> int:
    rows = []
    if chat_store.exists():
        with ChatStore.maintainer() as current:
            rows = [(grant, grants.grant_state(grant)) for grant in current.grants()]
    if args.json:
        print(json.dumps([{**grant.to_json(), "state": state} for grant, state in rows],
                         indent=2))
        return EXIT_OK
    if not rows:
        print("No chat grants.")
    for grant, state in rows:
        print(f"@{grant.handle}  {grant.launch_id}  {grant.profile}  "
              f"{grant.pane_session}:{grant.pane_id}  {grant.level}  {state}")
    return EXIT_OK


def run_channels(args) -> int:
    with ChatStore.maintainer() as current:
        for channel in current.channels(include_archived=True):
            flags = " (archived)" if channel.archived else ""
            unread = f"  {channel.unread} unread" if channel.unread else ""
            print(f"#{channel.name}{flags}{unread}")
    return EXIT_OK


def run_channel_create(args) -> int:
    with ChatStore.maintainer() as current:
        current.create_channel(args.name)
    print(f"Created #{args.name}.")
    return EXIT_OK


def run_channel_archive(args) -> int:
    with ChatStore.maintainer() as current:
        current.archive_channel(args.name, not args.undo)
    print(f"{'Unarchived' if args.undo else 'Archived'} #{args.name}.")
    return EXIT_OK


def _stdin_text() -> str:
    stream = getattr(sys.stdin, "buffer", None)
    if stream is None:
        raise ChatError("no standard input to read the text from")
    raw = stream.read(chat_store.MAX_BODY_BYTES + 2)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ChatError("standard input is not valid UTF-8") from None
    return text[:-1] if text.endswith("\n") else text


def run_post(args) -> int:
    text = _stdin_text() if args.text == STDIN else args.text
    with ChatStore.maintainer() as current:
        posted = current.post(args.channel, text, thread=args.thread)
        woken = chat_wake.wake_mentioned(current, posted)
    mentioned = " ".join(f"@{name}" for name in posted.mentioned)
    print(f"{posted.id}" + (f" mentioned {mentioned}" if mentioned else ""))
    for result in woken:
        shown = "woke" if result.outcome == chat_wake.WOKEN else f"did not wake ({result.outcome})"
        print(f"{shown} @{result.handle}", file=sys.stderr)
    return EXIT_OK


def run_read(args) -> int:
    with ChatStore.maintainer() as current:
        messages = current.messages(args.channel, thread=args.thread, after_id=0,
                                    limit=args.limit, latest=True)
    if args.json:
        print(json.dumps([frame.message_json(message) for message in messages], indent=2))
        return EXIT_OK
    for message in messages:
        print(message_text(message).plain)
    return EXIT_OK


def run_prune(args) -> int:
    if not chat_store.exists():
        print("No chat file.")
        return EXIT_OK
    with ChatStore.maintainer() as current:
        removed = current.prune()
    print(f"Deleted {removed} message{'s' if removed != 1 else ''}.")
    return EXIT_OK


def run_purge(args) -> int:
    if not chat_store.exists():
        print("No chat file.")
        return EXIT_OK
    if not args.yes:
        if not _interactive():
            raise ChatRefused("chat purge needs --yes outside an interactive terminal")
        typed = _ask(f"Delete {chat_store.db_path()} and every message? Type 'purge': ")
        if typed is None or typed.strip() != "purge":
            print("Nothing deleted.", file=sys.stderr)
            return EXIT_ERROR
    removed = chat_store.purge()
    print(f"Deleted {len(removed)} file{'s' if len(removed) != 1 else ''}.")
    return EXIT_OK


VERBS = {
    "grant": run_grant,
    "revoke": run_revoke,
    "grants": run_grants,
    "channels": run_channels,
    "channel-create": run_channel_create,
    "channel-archive": run_channel_archive,
    "post": run_post,
    "read": run_read,
    "prune": run_prune,
    "purge": run_purge,
}


def run(args) -> int:
    reason = maintainer_refusal()
    if reason is not None:
        print(f"tongs-agents chat: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    verb = getattr(args, "verb", None)
    if verb is None:
        try:
            theme = load_app_config().ui.theme
        except AppConfigError:
            theme = None
        ChatApp(theme).run()
        return EXIT_OK
    try:
        return VERBS[verb](args)
    except ChatError as error:
        return _fail(error)


def main(argv: list[str] | None = None) -> int:
    parser = CommandParser(prog="tongs-agents chat", description=HELP)
    add_arguments(parser)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
