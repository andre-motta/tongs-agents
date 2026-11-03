"""The user docs: every link resolves and every shown command parses.

Scope is README.md and every Markdown page under docs/. Relative links must
name an existing file, and a ``#fragment`` pointing into a Markdown page must
match one of its headings (GitHub's anchor rules). README.md uses absolute
``github.com/andre-motta/tongs-agents/blob/main/...`` links so they also work on
PyPI; those are checked against the checkout the same way.

A leading YAML frontmatter block (``---`` ... ``---``, read by the tongs.tools
Starlight site) is skipped: it is neither prose nor a code block.

Every ``tongs-agents ...`` line in a fenced shell block of README.md, the docs
pages and the tutorials is parsed with the real CLI parser, so a renamed verb
or flag fails here. Lines written as usage syntax (``[OPTION]``, ``<value>``,
``...``) are skipped.
"""

import re
import shlex
from pathlib import Path

import pytest

from tongs_agents import cli

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REPO_URL = re.compile(
    r"^https://github\.com/andre-motta/tongs-agents/(?:blob|tree)/main/(?P<path>[^#?]*)"
    r"(?:#(?P<fragment>.*))?$"
)
FENCE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE = re.compile(r"`+[^`]*`+")
INLINE_LINK = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s|$)")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SHELL_LANGS = {"", "console", "bash", "sh", "shell"}
USAGE_SYNTAX = re.compile(r"\[|<|\.\.\.")


def _pages() -> list[Path]:
    return [ROOT / "README.md", *sorted(DOCS.rglob("*.md"))]


def _user_pages() -> list[Path]:
    """README.md and the user docs; docs/work/ holds internal records."""
    return [page for page in _pages() if "work" not in page.relative_to(ROOT).parts[:2]]


def _body_lines(text: str):
    """(line number, line) after a leading frontmatter block, if there is one."""
    lines = list(enumerate(text.splitlines(), 1))
    if lines and lines[0][1].strip() == "---":
        for index, (_, line) in enumerate(lines[1:], 1):
            if line.strip() == "---":
                return lines[index + 1:]
    return lines


def _prose_lines(text: str):
    """Lines outside frontmatter and fenced code blocks, with their line numbers."""
    fenced = None
    for number, line in _body_lines(text):
        match = FENCE.match(line)
        if match:
            if fenced is None:
                fenced = match.group(1)
            elif line.strip().startswith(fenced):
                fenced = None
            continue
        if fenced is None:
            yield number, line


def _fenced_blocks(text: str):
    """(language, [(line number, line)]) for every fenced block."""
    block = None
    language = ""
    marker = ""
    for number, line in _body_lines(text):
        stripped = line.strip()
        if block is None:
            match = FENCE.match(line)
            if match:
                marker = match.group(1)
                language = stripped[len(marker):].strip().split(" ")[0]
                block = []
            continue
        if stripped.startswith(marker):
            yield language, block
            block = None
            continue
        block.append((number, line))


def _slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _anchors(page: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors = set()
    for _, line in _prose_lines(page.read_text(encoding="utf-8")):
        match = HEADING.match(line)
        if not match:
            continue
        slug = _slug(match.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def _links(page: Path):
    for number, line in _prose_lines(page.read_text(encoding="utf-8")):
        prose = INLINE_CODE.sub("", line)
        for match in INLINE_LINK.finditer(prose):
            yield number, match.group(1)
        match = REFERENCE_LINK.match(prose)
        if match:
            yield number, match.group(1)


def _resolve(page: Path, target: str) -> tuple[Path | None, str | None]:
    """The local file and fragment a link names, or (None, None) to skip it."""
    match = REPO_URL.match(target)
    if match:
        return ROOT / match.group("path"), match.group("fragment")
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.IGNORECASE):
        return None, None
    path, _, fragment = target.partition("#")
    if not path:
        return page, fragment
    return (page.parent / path), (fragment or None)


def _broken_links(pages: list[Path]) -> list[str]:
    problems = []
    for page in pages:
        relative = page.relative_to(ROOT).as_posix()
        for number, target in _links(page):
            path, fragment = _resolve(page, target)
            if path is None:
                continue
            where = f"{relative}:{number}: {target}"
            resolved = path.resolve()
            if not resolved.is_relative_to(ROOT) or not resolved.exists():
                problems.append(f"{where}: no such file")
                continue
            if fragment and resolved.suffix == ".md" and fragment not in _anchors(resolved):
                problems.append(f"{where}: no heading for #{fragment}")
    return problems


def _shell_commands(line: str) -> list[list[str]]:
    """The simple commands of one shell line, split at ; && || | and &."""
    lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    commands: list[list[str]] = [[]]
    for token in lexer:
        if token and set(token) <= set(";&|"):
            commands.append([])
        else:
            commands[-1].append(token)
    return [command for command in commands if command]


def _commands(pages: list[Path]):
    for page in pages:
        relative = page.relative_to(ROOT).as_posix()
        for language, block in _fenced_blocks(page.read_text(encoding="utf-8")):
            if language not in SHELL_LANGS:
                continue
            # A block with "$ " prompts mixes commands and their output.
            prompted = any(line.lstrip().startswith("$ ") for _, line in block)
            for number, line in block:
                line = line.strip()
                if prompted:
                    if not line.startswith("$ "):
                        continue
                    line = line[2:]
                if "tongs-agents " not in line or USAGE_SYNTAX.search(line.split(" #")[0]):
                    continue
                for argv in _shell_commands(line):
                    if argv[0] == "tongs-agents":
                        yield f"{relative}:{number}", argv


def test_user_doc_links_resolve():
    assert _broken_links(_user_pages()) == []


def test_internal_record_links_resolve():
    work = [page for page in _pages() if page not in _user_pages()]
    assert _broken_links(work) == []


def test_every_page_is_in_the_docs_index():
    index = DOCS / "README.md"
    linked = set()
    for _, target in _links(index):
        path, _ = _resolve(index, target)
        if path is not None:
            linked.add(path.resolve())
    missing = [
        page.relative_to(ROOT).as_posix()
        for page in _user_pages()
        if page.parent != ROOT and page != index and page.resolve() not in linked
        and page.name != "SDLC.md"
    ]
    assert missing == []


def test_readme_links_into_the_docs_index():
    assert any(
        target.endswith("docs/README.md") for _, target in _links(ROOT / "README.md")
    )


def _shown_commands():
    return list(_commands(_user_pages()))


def test_docs_show_commands():
    tutorials = [page for page in _user_pages() if page.parent.name == "tutorials"]
    assert tutorials
    assert list(_commands(tutorials))


@pytest.mark.parametrize(
    ("where", "argv"), _shown_commands(),
    ids=lambda value: value if isinstance(value, str) else " ".join(value),
)
def test_shown_command_parses(where, argv):
    parser, _ = cli.build_parser()
    shown = " ".join(argv)
    try:
        args = cli.parse_args(parser, argv[1:])
    except SystemExit as error:
        assert error.code == 0, f"{where}: {shown}"
        return
    # A bare tongs-agents is a command too: it opens the empty workspace.
    assert args.command is not None or argv == ["tongs-agents"], f"{where}: {shown}"


def test_frontmatter_is_not_prose():
    text = "---\ntitle: \"A [page](missing.md)\"\n# not a heading\n---\n\n## Real\n"
    assert [line for _, line in _prose_lines(text)] == ["", "## Real"]
    assert [number for number, _ in _prose_lines(text)] == [5, 6]
    assert [line for _, line in _prose_lines("---\nno closing marker\n")] == [
        "---", "no closing marker",
    ]


def test_shell_lines_split_like_a_shell():
    assert _shell_commands("printf 'a;b' | tongs-agents send ID -  # note") == [
        ["printf", "a;b"], ["tongs-agents", "send", "ID", "-"],
    ]
    assert _shell_commands('tongs-agents send ID "sleep 5; x" && echo ok') == [
        ["tongs-agents", "send", "ID", "sleep 5; x"], ["echo", "ok"],
    ]


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Upgrading from claude-fleet-monitor", "upgrading-from-claude-fleet-monitor"),
        ("Agent workspace, multiplexers and profiles", "agent-workspace-multiplexers-and-profiles"),
        ("`tongs-agents config`: status", "tongs-agents-config-status"),
        ("Step 3: [profiles](profiles.md)", "step-3-profiles"),
    ],
)
def test_slug_follows_github(heading, slug):
    assert _slug(heading) == slug
