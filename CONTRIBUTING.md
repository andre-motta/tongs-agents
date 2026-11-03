# Contributing to Tongs Agents

## Development Setup

Python 3.11 or newer is required (the config file is read with the standard
library `tomllib`). CI tests 3.11, 3.12 and 3.13.

```bash
git clone https://github.com/andre-motta/tongs-agents.git
cd tongs-agents
pip install -e ".[dev]"
```

## Running Tests

```bash
pytest
```

Tests use `tmp_path` fixtures and mock the filesystem. No running Claude Code or Codex sessions or terminal emulators are needed.

## Code Style

- Pure Python core. The approved Pi adapter may package an optional dependency-free JavaScript extension. No bash, jq, or shell dependencies in Python code.
- Cross-platform. Use `sys.platform` checks for OS-specific paths (`/proc` on Linux, `lsof`/`ps` on macOS, `tasklist` on Windows).
- Module-level imports unless avoiding circular deps.
- No comments unless the "why" is non-obvious.
- No em-dashes in text.

## Architecture

```
hooks (hook.py)  -->  $XDG_STATE_HOME/tongs-agents/sessions/*.json  <--  discovery.py
                                              <--  mcp_server.py
                                              <--  tui.py
                                              <--  focus.py
```

- **hook.py**: Claude Code and Codex hook handler, writes session status JSON on each event.
- **discovery.py**: Cross-platform process discovery + session reading/dedup/cleanup. Shared by all consumers.
- **mcp_server.py**: MCP server exposing fleet tools to Claude Code and Codex sessions.
- **tui.py**: Textual-based interactive dashboard.
- **focus.py**: Cross-platform terminal window/tab focus (KDE/GNOME/macOS/Windows).
- **cli.py**: Entry point for `tongs-agents` command.
- **profiles.py**, **launches.py**, **launch.py**: agent profiles from `config.toml`, owned launch records and the in-pane launcher. See [docs/profiles.md](docs/profiles.md).

## Docs

User documentation lives in [docs/](docs/README.md): tutorials in
`docs/tutorials/`, how-to and reference pages in `docs/`. The tongs website,
www.tongs.tools, publishes these pages as its `/agents/` section from a
pinned tongs-agents version; `docs/work/` holds internal design records and
is not published.

Every published page starts with a YAML frontmatter block with a `title` and
a one-sentence `description`; the site renders `title` as the page heading,
so do not repeat it as a `# ` heading. Link between pages with relative
links, and list a new page in [docs/README.md](docs/README.md).
`pytest tests/test_docs_links.py tests/test_docs_frontmatter.py` checks
links, frontmatter and every `tongs-agents` command shown in a shell block.

## Agent-led initiatives

For substantial agent-led work, follow [docs/SDLC.md](docs/SDLC.md), including
isolated worktrees, independent review, signed-off local commits within approved
scope, and the feature-branch/CTO gates in the profile. Ordinary contributors can use the PR
process below without installing the private orchestration skill.

## Pull Requests

1. Fork and create a feature branch.
2. Write tests for new functionality.
3. Run `pytest` and ensure all tests pass.
4. Keep PRs focused. One feature or fix per PR.
5. Describe what changed and why in the PR description.

## Reporting Issues

Use the GitHub issue templates. Include your OS, terminal emulator, Python version, and the output of `tongs-agents status`.
