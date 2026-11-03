---
title: "Zellij pins and the managed download"
description: "The Zellij releases tongs-agents trusts and how it downloads a pinned, checksum-verified build."
---

tongs-agents supports Zellij from a floor release up to a validated ceiling.
The releases it trusts are listed in one packaged table,
[`src/tongs_agents/data/zellij_pins.json`](../src/tongs_agents/data/zellij_pins.json).
Hosted CI downloads from it, and so does the managed download described
below. The table's fields are documented in the
[validation README](../validation/README.md#zellij-pin-table).

Zellij 0.44.2 can briefly answer `There is no active session!` for a running
session, so read-only queries and `new-pane` try up to five times, 0.1 s
apart; a refused `new-pane` created no pane, and other failures are not retried.

## Managed download

```console
tongs-agents doctor --fix zellij
tongs-agents zellij install [--version X.Y.Z]
tongs-agents zellij
```

`doctor --fix zellij` installs only when no Zellij at or above the floor is
available; `zellij install` always installs the requested pin (default: the
validated ceiling). Plain `tongs-agents zellij` shows which binary is in use.
These two verbs are the only tongs-agents code that opens a network
connection. Doctor's checks only read, and the hook (`tongs-agents-hook`)
never imports the downloader.

Scope is Linux x86_64 and the `no-web` build. On any other platform both verbs
print `unsupported, install zellij >= <floor> manually` and exit 2.

`tongs-agents zellij install --web` installs the pinned full build with the
web server instead (the `web_pins` table below), into
`state_dir()/bin/zellij-web-<version>/` behind the symlink
`state_dir()/bin/zellij-web`, with the same checks. It never replaces the
no-web link, `doctor --fix zellij` never installs it, and it is used only
while [web watching](web.md) is enabled.

What the installer does, refusing at the first doubt:

1. Picks the pin for this platform from the packaged table.
2. Downloads it over HTTPS (redirects must stay HTTPS) with a timeout, reading
   no more than the pinned size, into a new empty 0700 temporary directory
   under `state_dir()/bin`.
3. Checks the tarball's SHA-256 before opening it. A mismatch removes the
   temporary directory, so nothing is extracted.
4. Accepts the archive only when no member has an absolute path, a `..`
   component, or is a link or device, and exactly one member is the regular
   file `zellij`. Only that member is extracted, with tarfile's `data` filter
   when the running Python has it, and otherwise by copying the checked member
   into a new file.
5. Checks the binary against `binary_sha256`, moves it into
   `state_dir()/bin/zellij-<version>/` (mode 0700) and atomically repoints the
   symlink `state_dir()/bin/zellij` at it. A `bin/zellij` that is not a
   symlink is left alone and the install is refused.

`state_dir()` is `$TONGS_AGENTS_STATE_DIR`, else
`$XDG_STATE_HOME/tongs-agents`, else `~/.local/state/tongs-agents`.

Binary resolution: a `zellij` on `PATH` at or above the floor wins; otherwise
the managed binary is used when its link resolves; otherwise the `PATH`
binary is returned so doctor can report its version. The managed directory is
not added to `PATH`, so a shell started inside the workspace still runs the
`PATH` Zellij, if any.

Evidence of a live run on Linux x86_64 is recorded in
[`validation/evidence/zellij-download-linux-x86_64.json`](../validation/evidence/zellij-download-linux-x86_64.json).

## Web build pins

`web_pins` has the same shape as `pins` (version, then platform), and each
entry's `flavor` is `web`. Its asset is the official full build
(`zellij-x86_64-unknown-linux-musl.tar.gz`, no `no-web` prefix), and
`binary_sha256` is required: the installer refuses a web pin without it.
Hosted CI does not download web pins; they are checked by
`python validation/zellij_web.py` (see [web watching](web.md)). The 0.45.1 web
pin was approved with the [chat and web design](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-chat.md) (row 17);
its digests match the release's published checksum.

## Bumping a pin

The project maintainer owns the pin table. An agent or contributor may
prepare a bump in a pull request; it merges only with the maintainer's
approval, because every user's managed download trusts these digests.

To add a release or move the floor or ceiling:

1. Download the official `no-web` Linux x86_64 tarball from the Zellij GitHub
   release into a new empty directory. Record its size and SHA-256, extract
   only `zellij`, and record the binary's SHA-256. Compare the binary digest
   with upstream's published `.sha256sum` for the release.
2. Add the entry to `zellij_pins.json` with every required field and
   `binary_sha256` (the hosted workflow requires it for `no-web` pins).
3. When the floor or ceiling moves, change `floor` or `validated_ceiling` and
   keep the code in step: `ZELLIJ_FLOOR` and `ZELLIJ_CEILING` in
   `src/tongs_agents/multiplexers/zellij_binary.py` and `VALIDATED_VERSION` in
   `src/tongs_agents/terminal_apis/zellij.py`.
4. Rerun the validators against the new binary, not only the unit tests:
   `python validation/zellij.py` and `python validation/workspace.py` (see the
   [validation README](../validation/README.md) for isolation rules), plus the
   hosted `zellij-validation` and `workspace-validation` jobs, which run on
   each pinned release. Run `pytest tests/test_zellij_pins.py
   tests/test_zellij_download.py`.
5. Record new evidence under `validation/evidence/` for each release that
   becomes the floor or ceiling, then rerun the managed download once
   (`tongs-agents zellij install --version X.Y.Z` with a temporary
   `TONGS_AGENTS_STATE_DIR`) to confirm the new pin installs.

A web pin is bumped the same way from the release's full (not `no-web`)
tarball, followed by `python validation/zellij_web.py` instead of the hosted
jobs.

Never edit a digest to make a failing download pass. A digest mismatch means
the asset changed upstream or in transit; stop and investigate.
