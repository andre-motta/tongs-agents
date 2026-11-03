# Session store

Hooks, `tongs-agents notify` and bridges write one JSON record per agent
session to the sessions directory, `paths.sessions_dir()`:
`$TONGS_AGENTS_STATE_DIR/sessions`, else `$XDG_STATE_HOME/tongs-agents/sessions`,
else `~/.local/state/tongs-agents/sessions`. The directory is private (0700)
and never a symlink. There is no daemon and no database: every consumer reads
the files.

## Readers only read

These never write, rename or unlink anything in the store, never create it,
and never scan processes:

- `discovery.read_sessions()`, `read_session_records()`,
  `read_stored_sessions()` and `read_session_record()`;
- the MCP tools `fleet_status`, `fleet_session` and
  `fleet_sessions_needing_attention`;
- `tongs-agents status`, `tongs-agents focus` and the MCP `fleet_focus` lookup;
- the control layer (`agents_status`, `agents_wait`, supervisor commands).

`discovery.annotate_session(record, now)` is the one place that derives the
fields consumers show: `age_seconds` (never negative), `needs_attention`
(`waiting`, or `idle` for more than 120 seconds), `launch_id` and
`profile_id`. It returns a copy and leaves the stored record alone.

A reader shows what the store holds. Agent processes that no hook reported,
and records whose session is gone, appear or disappear at the next
maintenance run.

## One maintainer

`session_store.maintain_store()` is the only code that changes the store
besides the writers. One run:

1. takes the store-wide maintenance lock (`.fleet-maintenance.lock`) without
   waiting; when another process holds it the run is skipped as `busy`;
2. skips as `recent` when the stamp (`.fleet-maintenance.stamp`) is less than
   `MIN_INTERVAL_SECONDS` (5 seconds) old, unless forced; a stamp in the
   future or unreadable does not count;
3. writes the stamp, records agent processes that no hook reported as
   `discovered`, then removes:
   - `ended` records older than 5 minutes;
   - records whose pid no longer names the recorded live process
     (`discovery.resolve_session_process`: same start time and, for Claude
     Code and Codex, a process of that harness);
   - pid-less records older than 10 minutes;
   - pid-less `other` records whose launch ended.

Before each unlink it takes that record's lock, the lock every writer holds,
and re-reads the record. When a hook wrote a newer version after cleanup
judged it, the new version is kept and judged again on the next run. Legacy
schema 0 files have no writer and are re-read without a record lock.

Store errors never raise: an unusable store is reported as `unavailable`.

### Who calls it

| Caller | When |
| --- | --- |
| `SessionFeed` (the workspace sidebar and `tongs-agents monitor`, standalone or in Tongs) | Before every poll; the stamp limits the store to one run per interval however many sidebars and monitors are open |
| `tongs-agents cleanup` | On demand, forced; exits 1 when another maintainer holds the lock or the store is unavailable |
| MCP `fleet_cleanup` | On demand, forced; replies `{"removed": N}`, plus `"skipped": "busy"` or `"unavailable"` when it did not run |

A `SessionFeed` given its own reader does not maintain unless it is also
given a `maintainer`.

## Writing imported records

`session_store.import_session_record(record) -> bool` is the entry point for
records produced outside the hook, for example by a sandbox bridge. The
record must be a complete schema 1 record (the shape `hook.build_session_record`
returns). It is written exactly like a hook event:

- the same validation (`discovery.write_session_record`), so a forged
  `canonical_id`, an unknown harness or status, or an oversized field is
  refused;
- the session identity lock, then the record lock;
- a record whose pid or process start differs from the stored one is
  refused, and a `sequence` must increase when the stored record has one;
- an atomic replace through a temporary file in the sessions directory.

It returns False instead of raising, and never changes the caller's dict.
Imported records are maintained like any other: a record with a pid is
removed once that pid no longer names the recorded live local process, and a
pid-less record must be refreshed within 10 minutes.

## Hook cost

The hook imports only `discovery` and its small dependencies; it never loads
`session_store`, takes the maintenance lock or scans processes. Its write path
(validation, record lock, atomic replace) is unchanged by maintenance.
