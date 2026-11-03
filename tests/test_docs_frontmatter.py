"""Starlight frontmatter on every published docs page.

The tongs.tools site (Astro + Starlight) publishes docs/ as its ``/agents/``
section, without docs/work/. Starlight needs a ``title`` and uses
``description`` for the page summary, and renders ``title`` as the page
heading, so a page must not repeat it as a leading ``# `` heading. Links
between published pages stay relative, so they work on GitHub and on the
site alike: an absolute ``github.com/andre-motta/tongs-agents/blob/...`` link
to a published page is refused (README.md at the root keeps absolute links
for PyPI and is not checked here).

The repository has no YAML dependency, so the frontmatter is read with a
small parser for top-level ``key: value`` scalars, which is all these pages
use.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
ABSOLUTE_DOCS_LINK = re.compile(
    r"https?://github\.com/andre-motta/tongs-agents/(?:blob|tree)/[^/\s)]+/docs/"
    r"(?P<path>[^\s)#?]*)"
)
KEY = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):(?:\s+(?P<value>.*?))?\s*$")
SENTENCE_END = re.compile(r"[.!?]\s+[A-Z`]")


def _published_pages() -> list[Path]:
    return [
        page for page in sorted(DOCS.rglob("*.md"))
        if page.relative_to(DOCS).parts[0] != "work"
    ]


def _scalar(value: str) -> str:
    if value.startswith('"'):
        return json.loads(value)
    if value.startswith("'") and value.endswith("'") and len(value) > 1:
        return value[1:-1].replace("''", "'")
    return value


def frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Top-level scalar keys of a leading ``---`` block and the body after it."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            break
    else:
        return None
    fields = {}
    for line in lines[1:end]:
        if not line.strip() or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = KEY.match(line)
        assert match, f"unreadable frontmatter line: {line!r}"
        fields[match.group("key")] = _scalar(match.group("value") or "")
    return fields, "\n".join(lines[end + 1:])


def _page_id(page: Path) -> str:
    return page.relative_to(ROOT).as_posix()


def test_published_pages_exist():
    pages = [_page_id(page) for page in _published_pages()]
    assert "docs/README.md" in pages
    assert "docs/tutorials/sandbox.md" in pages
    assert not any(page.startswith("docs/work/") for page in pages)


@pytest.mark.parametrize("page", _published_pages(), ids=_page_id)
def test_page_has_title_and_description(page):
    parsed = frontmatter(page.read_text(encoding="utf-8"))
    assert parsed is not None, f"{_page_id(page)}: no frontmatter block"
    fields, _ = parsed
    assert fields.get("title", "").strip(), f"{_page_id(page)}: empty title"
    description = fields.get("description", "").strip()
    assert description, f"{_page_id(page)}: empty description"
    assert description.endswith("."), f"{_page_id(page)}: description is not a sentence"
    assert not SENTENCE_END.search(description), (
        f"{_page_id(page)}: description is more than one sentence"
    )


@pytest.mark.parametrize("page", _published_pages(), ids=_page_id)
def test_title_is_not_repeated_as_heading(page):
    parsed = frontmatter(page.read_text(encoding="utf-8"))
    assert parsed is not None
    fields, body = parsed
    first = next((line for line in body.split("\n") if line.strip()), "")
    if first.startswith("# "):
        heading = first[2:].replace("`", "").strip()
        assert heading != fields.get("title", "").strip(), (
            f"{_page_id(page)}: '# {heading}' repeats the title Starlight renders"
        )


@pytest.mark.parametrize("page", _published_pages(), ids=_page_id)
def test_links_between_pages_are_relative(page):
    absolute = [
        f"{_page_id(page)}:{number}: {match.group(0)}"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        for match in ABSOLUTE_DOCS_LINK.finditer(line)
        if not match.group("path").startswith("work/")
    ]
    assert absolute == []


RELATIVE_WORK_LINK = re.compile(r"\]\((?:\.\./)*work/")


@pytest.mark.parametrize("page", _published_pages(), ids=_page_id)
def test_published_pages_link_work_records_on_github(page):
    """docs/work/ is not published on the site, so a relative link would 404 there."""
    relative = [
        f"{_page_id(page)}:{number}"
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1)
        if RELATIVE_WORK_LINK.search(line)
    ]
    assert relative == []


def test_parser_reads_quoted_and_plain_scalars():
    text = (
        '---\ntitle: "Config layer: tongs-agents config"\n'
        "description: 'It''s one sentence.'\nsidebar:\n  order: 3\n---\n\nBody\n"
    )
    fields, body = frontmatter(text)
    assert fields == {
        "title": "Config layer: tongs-agents config",
        "description": "It's one sentence.",
        "sidebar": "",
    }
    assert body == "\nBody\n"
    assert frontmatter("# Title\n") is None
    assert frontmatter("---\ntitle: x\n") is None


def test_absolute_docs_link_pattern():
    assert ABSOLUTE_DOCS_LINK.search(
        "[x](https://github.com/andre-motta/tongs-agents/blob/main/docs/sandbox.md#keys)"
    )
    assert ABSOLUTE_DOCS_LINK.search(
        "https://github.com/andre-motta/tongs-agents/tree/v0.10.0/docs/tutorials/"
    )
    assert not ABSOLUTE_DOCS_LINK.search("https://github.com/andre-motta/tongs-agents/issues/111")
    assert not ABSOLUTE_DOCS_LINK.search("https://github.com/andre-motta/tongs-agents.git")
