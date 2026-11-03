# Agent workspace plan

Approved by the maintainer on 2026-11-02, rescoped the same day: v0.10.0 is the first release and ships M0 to M5, and the runtime rename moved early (M1-R). Tracking: parent issue #1, items #2 to #40.

tongs-agents becomes a standalone, harness-agnostic agent workspace in the tongs
family. It owns its own Textual sidebar app and a Zellij session in which Claude
Code, Codex and pi run as real panes. tongs is an optional neighbour tab in that
session. The existing `fleet-monitor` tongs plugin stays as a thin, optional
doorway (agents screen plus "Open agent workspace"). No tongs core changes are
needed.

## Milestones

Every gate's evidence is labelled live, instrumented, mocked or unvalidated, as
[SDLC.md](../SDLC.md) requires. Review depth follows each item's risk tier.

| Milestone | Items | Exit gate | Evidence |
| --- | --- | --- | --- |
| M0: release safety foundations | #2 legacy upgrade adoption, #3 publish hardening, #4 this record, #5 harness facts | Upgrade test from a synthetic claude-fleet-monitor install leaves one hook per event and one MCP entry; release rehearsal passes; no release | Mocked upgrade tests, instrumented release rehearsal |
| M1: thin usable workspace | #40 runtime rename and 0.9.x cleanup, #7 Zellij spike, #8 AgentsPanel, #9 multiplexer protocol, #10 launch identity, #11 profiles and launcher, #12 command modules, profile-driven install and doctor, #13 pinned Zellij CI, #14 workspace and sidebar verbs, #15 plugin action, #16 live validator | `tongs-agents workspace` opens a sidebar plus one pane per profile on Zellij 0.44.2 and 0.45.1; status within 4 s; focus by row with a second client attached; no nesting | Hosted instrumented validator on both pins, plus a live maintainer run |
| M2: config layer and app shell | #17 emitter strategy, #18 owned-file writer, #19 config engine and verbs, #20 Claude, Codex and pi emitters, #21 app shell, #22 managed Zellij download | Second `config apply` changes 0 bytes; unowned files untouched; rollback on failure; bad checksum refused; hooks never use the network | Mocked snapshot tests, live per-profile config dir |
| M3: multiplexer completeness and agent control | #23 send, paste, capture and supervisor verbs, #24 tmux adapter, #25 opt-in audited MCP control | One multi-line send yields exactly one prompt on Zellij and tmux; unverified or stale panes refused; MCP control absent unless opted in | Live validators on both multiplexers |
| M4: session store hardening | #27 read-only readers and one cleanup owner (#26 and #28 superseded by #40) | Readers never write; cleanup loses no records under concurrent hook writes | Mocked stress test |
| M5: agent chat | #29 chat design gate, #30 read-only web watching, #31 chat store, tools, pane and grants, #32 mention delivery | Chat is off by default and granted per session by the maintainer; ungranted sessions see no chat tools; two granted agents hold a thread; mentions reach granted agents | Live validator with fake agents |
| M6: release v0.10.0 and sunset | #6 full pre-release review, v0.10.0 with M0 to M5, then claude-fleet-monitor v0.9.2; #33 archive and legacy retirement | Tag and archive authorized; end-of-support dates documented; no date pressure | Live clean-venv smoke test, documentation |
| M7: sandboxed agents with OpenShell | #34 integration investigation, #35 sandboxed profiles, #36 policy and sandbox commands, #37 approvals in the sidebar, #38 setup wizard | A sandboxed agent shows live status and cannot reach what its policy denies; access requests reach the maintainer | Live, on upstream OpenShell and the opendatahub-io midstream fork |

## Comparison

Facts are from a survey on 2026-11-02 (GitHub metadata and source reads).
"Not checked" means the survey did not establish it.

| Tool | Language | Multiplexer | Harnesses | Status source | Config layer | Profiles or accounts | Messaging or control | License | Telemetry | Maturity (stars, last push) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| tongs-agents (this plan) | Python | Zellij, tmux in M3 | Claude Code, Codex, pi, any command | Hooks and pi extension | Emitters (M2) | Command-wrapper profiles | MCP control (M3), chat (M5) | MIT | None | New, history from claude-fleet-monitor |
| [brizzai/fleet](https://github.com/brizzai/fleet) | Go | tmux | Claude, Codex, OpenCode, Copilot | Own hooks, pane heuristics | None | Config dir per Claude account | Supervisor CLI | Apache-2.0 | PostHog, on by default | 60, 2026-10-20 |
| [claude-squad](https://github.com/smtg-ai/claude-squad) | Go | tmux | Any program | Screen hash, prompt strings | None | None | None | Not checked | Not checked | 8.7k, 2026-08-20 |
| [ntm](https://github.com/Dicklesworthstone/ntm) | Go | tmux | Claude, Codex, Gemini, opencode, pi plugin | Screen regex per agent | Not checked | Not checked | Broadcast, MCP Agent Mail, REST and SSE | Not checked | Not checked | 465, 2026-11-01 |
| [agent-deck](https://github.com/asheshgoplani/agent-deck) | Go | tmux control mode | Claude, Gemini, OpenCode, Codex, pi | Hooks and output events | Not checked | Not checked | Conductor agents, Telegram, Slack | Not checked | Not checked | 1.2k, 2026-11-02 |
| [workmux](https://github.com/raine/workmux) | Rust | tmux, Zellij experimental | Claude, Codex, OpenCode, pi, Gemini, Copilot | Hooks | None | Not checked | None | Not checked | Not checked | 3.0k, 2026-11-02 |
| [gastown](https://github.com/gastownhall/gastown) | Go | tmux | Claude by default, Codex, Copilot, Gemini, Cursor | Watchdog agents, Claude hooks | Not checked | Not checked | Mailboxes | Not checked | Not checked | 18.7k, 2026-10-28 |
| [cli-agent-orchestrator](https://github.com/awslabs/cli-agent-orchestrator) | Python | tmux through libtmux | Kiro, Claude, Codex, Copilot | Screen patterns | None | Not checked | Supervisor handoff, MCP | Not checked | Not checked | 1.5k, 2026-11-02 |
| [herdr](https://github.com/herdrdev/herdr) | Rust | Own multiplexer | Claude, Codex, opencode, others | Per-pane agent state | Not checked | Not checked | Socket API | Not checked | Not checked | 47.6k, 2026-11-02 |
| Zellij plugins ([zj-radar](https://github.com/marktoda/zj-radar), [zellaude](https://github.com/ishefi/zellaude), [zellij-attention](https://github.com/KiryuuLight/zellij-attention), [zjctl](https://github.com/mrshu/zjctl)) | Rust, WASM | Zellij | Mostly Claude | Hooks through `zellij pipe` | None | None | zjctl pipe RPC | Not checked | Not checked | 17 to 117 stars |

A private reference config was also reviewed, for ideas only (generated
per-harness config with ownership markers, dry run, capture and doctor verbs).
[MCP Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail_rust) is an
idea source, not a dependency, because of its license rider and single-machine
design.

## Compromises

1. No terminal inside a Textual widget; agents and tongs are real Zellij panes and tabs.
2. Status comes from hooks first; screen capture is a fallback primitive, not built before M3.
3. `read_sessions` keeps its write side effects until M4.
4. The runtime names are renamed early (M1-R) before any release; claude-fleet-monitor 0.9.x leftovers are cleaned up, not migrated.
5. claude-fleet-monitor 0.9.x session records are ignored; they are live status that goes stale within minutes.
6. Live evidence covers Linux x86_64 only; macOS and Linux aarch64 are unvalidated, and `workspace` is unsupported on Windows.
7. Zellij restores layouts, not agent processes.
8. No exit-then-exec into tongs; tongs needs no core changes.
9. Outside Zellij, the plugin action shows and copies the command instead of opening a window.
10. No telemetry.
11. No daemon. Session state stays as files; the M5 chat is one SQLite file opened by each agent's MCP server, off by default and granted per session.
12. Legacy hook markers (`claude-fleet-hook`, the old MCP module path) are adopted permanently by install and uninstall.
13. claude-fleet-monitor and tongs-agents cannot be installed together; `claude-fleet uninstall` must not run after migrating.
14. Runtime dependencies stay `textual` and `mcp`; paths and layout templating are hand-written.
15. MCP control and chat widen what a same-user process can do; both are opt-in, limited to verified panes and sessions, and audited.

## Accepted decisions

- Config format TOML; Python floor raised to 3.11.
- Owned dirs: `$XDG_CONFIG_HOME/tongs-agents`, `$XDG_STATE_HOME/tongs-agents`, `$XDG_RUNTIME_DIR/tongs-agents`.
- Default workspace session `agents`; tongs tab opens when tongs is installed (`--no-tongs` opts out).
- Static layout; the sidebar spawns agent panes.
- `install` follows profiles and explicit flags, never environment variables.
- claude-fleet-monitor v0.9.2 ships a README notice, a CLI warning and an uninstall guard.
- Config emitters in M2; hosted Zellij jobs gate PRs and publishing; inferred screen status deferred.
- Runtime names (M1-R): `$XDG_STATE_HOME/tongs-agents/sessions`, MCP key `tongs-agents`, entry point `agents`, pi marker `tongs-agents`; no dual-read.
- v0.10.0 ships M0 to M5; claude-fleet-monitor v0.9.2 follows it; fast iteration (see [SDLC.md](../SDLC.md)) lasts until the tag.
- Managed Zellij download: Linux x86_64 build without web in M2; web build in M5.
- MCP control: opted-in profiles may spawn and send; per-caller rate limit; audit keeps hash and length only.
