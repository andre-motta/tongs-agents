"""Stale claude-fleet names may only remain in files that own the migration or history."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("docs", "src", "tests", "validation")
LEGACY = re.compile(r"claude_fleet_monitor|claude-fleet-hook|CLAUDE_FLEET_MONITOR")
ALLOWED_FILES = {
    "src/tongs_agents/legacy.py",
    "tests/test_upgrade.py",
    "tests/test_legacy_names.py",
    "docs/migration.md",
    "docs/work/harnesses-desktop.md",
    "docs/work/harness-facts.md",
}


def _allowed(relative: str) -> bool:
    if relative in ALLOWED_FILES:
        return True
    parts = relative.split("/")
    # docs/work holds dated planning and investigation records.
    if parts[:2] == ["docs", "work"]:
        return True
    # Committed evidence (validation/evidence/, validation/shells/evidence/, ...)
    # is historical and keeps the pre-rename names until it is rerun.
    return parts[0] == "validation" and "evidence" in parts[1:-1]


def _hits() -> list[str]:
    hits = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            if _allowed(relative):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if LEGACY.search(line):
                    hits.append(f"{relative}:{number}: {line.strip()}")
    return hits


def test_no_legacy_names_outside_allowlist():
    assert _hits() == []


def test_allowlist_covers_evidence_but_not_neighbours():
    assert _allowed("validation/evidence/tmux-fedora44.json")
    assert _allowed("validation/shells/evidence/baseline.json")
    assert not _allowed("validation/tmux.py")
    assert not _allowed("validation/shells/run.py")
    assert not _allowed("docs/work/harness-contract.md")


def test_pattern_matches_each_legacy_spelling():
    for name in ("claude_fleet_monitor", "claude-fleet-hook", "CLAUDE_FLEET_MONITOR"):
        assert LEGACY.search(f"x {name} y")
    assert not LEGACY.search("claude-fleet-monitor.json")
