"""Contracts between the config layer engine and the per-harness emitters.

An emitter plans ``Change`` values for one ``ProfileTarget`` and captures the
harness side back into a ``CanonicalFragment``. It never writes: the engine
applies every planned change through ``tongs_agents.owned.ChangeSet``, which
re-checks ``before_sha256`` against the file and rolls back on failure.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

ChangeKind = Literal["create", "update", "delete", "edit_entry", "unchanged", "conflict"]
CHANGE_KINDS: tuple[str, ...] = (
    "create", "update", "delete", "edit_entry", "unchanged", "conflict",
)
# Kinds that write ``content`` when applied.
WRITING_KINDS: tuple[str, ...] = ("create", "update", "edit_entry")


@dataclass(frozen=True)
class Change:
    """One planned file change.

    - ``create``: the file is absent and ``content`` will be written.
    - ``update``: a tongs-agents-owned file is rewritten whole.
    - ``delete``: a tongs-agents-owned file is removed.
    - ``edit_entry``: only our entries in a shared file change; the file
      keeps its mode and gets a ``.tongs-agents.bak`` before the first edit.
    - ``unchanged``: applying would not change a byte.
    - ``conflict``: the path exists without our owner marker; nothing is
      written and a ChangeSet holding it refuses to commit.

    ``before_sha256`` is the digest of the file when planned (None when
    absent) and ``after_sha256`` the digest after applying (None when absent).
    ``content`` holds the bytes to write for the writing kinds.
    """

    path: Path
    kind: ChangeKind
    before_sha256: str | None = None
    after_sha256: str | None = None
    content: bytes | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.kind not in CHANGE_KINDS:
            raise ValueError(f"unknown change kind {self.kind!r}")
        if self.kind in WRITING_KINDS and self.content is None:
            raise ValueError(f"a {self.kind} change needs content")


@dataclass(frozen=True)
class ProfileTarget:
    """One harness config home; ``config_dir`` None is the harness default."""

    harness: str
    config_dir: Path | None = None


@dataclass(frozen=True)
class CanonicalFragment:
    """What one target holds, in canonical terms (skills, instructions, MCP)."""

    harness: str
    target: ProfileTarget
    data: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class Emitter(Protocol):
    """Translates canonical config to and from one harness."""

    harness_id: str

    def plan(self, config: Any, target: ProfileTarget) -> list[Change]:
        """Changes that make ``target`` match ``config``; never writes."""
        ...

    def capture(self, target: ProfileTarget) -> CanonicalFragment:
        """The canonical view of what ``target`` holds today; never writes."""
        ...
