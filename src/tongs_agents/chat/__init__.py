"""Agent chat: one SQLite file shared by the maintainer and granted sessions.

The approved design is docs/work/agent-chat.md; the user guide is
docs/chat.md. The package is split by concern:

- ``store``: the file (``state_dir()/chat/chat.db``, WAL, 0600), schema v1,
  connection roles with their SQLite authorizers, limits and retention;
- ``identity``: the agent identity check against the launch record, and the
  maintainer check (outside every live agent process tree);
- ``grants``: grant, revoke and stale-grant handling, used only by the CLI and
  the sidebar;
- ``audit``: the chat audit log ``state_dir()/audit/chat.jsonl``;
- ``frame``: the data framing and previews every reader sees;
- ``inbox``: the read-mostly seam the hook uses for mention injection (M5-C).

No daemon: every participant opens the file directly.
"""
