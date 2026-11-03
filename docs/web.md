---
title: "Watching the workspace from a browser"
description: "Watch the agent workspace read-only from a browser or a phone with tongs-agents web."
---

`tongs-agents web` lets you watch the agent workspace read-only from a
browser or a phone. It is off by default and follows the approved design in
[agent chat and read-only web watching](https://github.com/andre-motta/tongs-agents/blob/main/docs/work/agent-chat.md) (rows 16 and 17
and the M5-A review).

Scope: Linux x86_64, where the Zellij web build is pinned. On any other
platform `web enable` exits 2.

## Quick start

```console
tongs-agents zellij install --web     # once: downloads the pinned web build
tongs-agents web enable               # starts the server, prints a token once
tongs-agents workspace                # a new session, now shared read-only
```

`web enable` prints the URL (`http://127.0.0.1:8083/<session>` by default)
and a read-only token. Open the URL, paste the token, and you watch the
session. The token is shown once; it is not stored or logged anywhere.

The session must be started after `web enable`: a Zellij session can only be
watched when it runs the web build with web sharing on. If the workspace was
already running, quit it and run `tongs-agents workspace` again.

From another device, use an SSH local forward rather than opening the port:

```console
ssh -L 8083:127.0.0.1:8083 <workstation>
```

then open `http://127.0.0.1:8083/<session>` on that device.

## Commands

| Command | What it does |
| --- | --- |
| `tongs-agents web` (or `web status`) | Shows whether watching is on, the web build, the server and the URL |
| `tongs-agents web enable [--port N]` | Starts the loopback server if needed and prints a new read-only token once. Run it again for another token |
| `tongs-agents web tokens list` | Lists token names and dates (never the tokens) |
| `tongs-agents web tokens revoke NAME` / `--all` | Revokes, then restarts a running server so open connections close |
| `tongs-agents web disable` | Stops the server, revokes every token and turns watching off |

## What tongs-agents does and does not do

- **Download.** Only `tongs-agents zellij install --web` fetches the web
  build, with the same HTTPS, size, archive and sha256 checks as the no-web
  download ([Zellij pins](zellij-pins.md)). It installs beside the no-web build
  as `state_dir()/bin/zellij-web`. Nothing else downloads it.
- **Which Zellij runs.** While watching is enabled, tongs-agents uses the web
  build for the workspace, and a new workspace session starts with
  `options --web-sharing on` for that session only. When watching is off, the
  usual Zellij is used and no session is shared. Your own Zellij config is
  never read or changed.
- **Bind address.** The server uses a config that tongs-agents owns,
  `state_dir()/web/config.kdl`, which binds `127.0.0.1` only. Plain HTTP is
  used because traffic stays on the host. Other addresses, certificates and
  HTTPS are not offered; `tongs-agents doctor` warns if that config, or the
  recorded state, names any other address.
- **Tokens.** Only read-only tokens are minted. Zellij keeps their hashes in
  `state_dir()/web/data/zellij/tokens.db` (0600, in a 0700 directory), apart
  from any token store of your own. The server accepts only tokens minted
  there. A token appears once on stdout and never in a file, log, error
  message or validation evidence.
- **Revoking.** Zellij 0.45.1 rejects a revoked token's cookie on new
  requests, but leaves an already open terminal connection streaming. So
  `web tokens revoke` restarts a running server, which closes every
  connection; viewers holding other tokens reconnect on their own.
- **State.** `state_dir()/web/state.json` (0600) records whether watching is
  on and the port. The server runs only between `web enable` and `web disable`;
  Zellij's own `web_server` auto-start stays off.

## Limits

- A read-only viewer cannot type into any pane and cannot watch a session
  that runs without web sharing; both are checked live by
  `validation/zellij_web.py`
  ([evidence](../validation/evidence/zellij-web-linux-x86_64.json)). That a
  read-only login cannot create a session comes from the design probe
  ([evidence](../validation/evidence/chat-zellij-web-0.45.1.json)).
- Same-user code can still start its own `zellij web` and mint a read-write
  token; see the threat notes in the design record. Web watching is a
  convenience boundary, not a sandbox.
- Not tested: a real browser, HTTPS, any address other than 127.0.0.1, the
  share plugin's runtime toggle, and Zellij 0.44.2 (no web build is pinned
  for it).
- Zellij 0.45.1 names tokens `token_N` by count. After revoking one of several
  tokens the next mint can fail with "already exists"; run
  `tongs-agents web tokens revoke --all` and `web enable` again.

## Validation

```console
python validation/zellij_web.py [--tarball zellij-x86_64-unknown-linux-musl.tar.gz]
```

Local-only, Linux x86_64, with Zellij 0.45.1 on `PATH`. It runs in a private
temporary root (HOME, XDG dirs, `ZELLIJ_SOCKET_DIR`, `ZELLIJ_CONFIG_DIR`),
binds only 127.0.0.1, starts uniquely named sessions, and stops every process
it started. `--tarball` feeds a local copy of the pinned asset through the real
installer, which still checks its sha256; without it the installer downloads
it.
