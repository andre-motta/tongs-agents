"""OpenShell access requests: read, approve and reject pending network rules (#37).

OpenShell drafts a network rule ("chunk") for every connection a sandbox's
policy blocks and, with the policy advisor on, for every proposal the agent
submits. This module is the only place tongs-agents runs ``openshell rule``
(docs/work/openshell-sandbox.md, rows 14 and 15):

- ``rule get <sandbox> --status pending`` has no JSON output, so its labelled
  text (upstream ``sandbox_draft_get``) is parsed strictly: a known first
  line, the announced number of blocks, known labels in their printed order,
  each at most once. Anything else raises ``ParseError`` and callers show the
  raw text instead, so a format change fails visibly and is never approved
  from a partial reading.
- ``approve`` and ``reject`` act on one proposal the maintainer reviewed.
  Both fetch the pending list again first; ``approve`` refuses when the
  proposal is gone or changed since it was shown. ``reject`` needs a reason.
- Nothing here runs ``rule approve-all``, sets an approval mode or touches
  ``proposal_approval_mode``. The callers are the sidebar (after a
  confirmation dialog) and ``tongs-agents sandbox rules`` (after a prompt on
  a terminal); no MCP tool reaches this module.

Gateway text is untrusted (an agent writes proposal rationales), so escape
sequences and control characters are removed before parsing or display.
Newlines stay, and OpenShell prints the rationale as it is, so a rationale can
add lines that parse as later labels (``Security``, ``Prover``, ``Endpoints``
and so on) when OpenShell printed no such line itself. Only the lines before
``Rationale`` are OpenShell's for certain: ``summary`` uses the Rule line, and
``Proposal.text`` quotes the Rationale line and every line after it under
``UNVERIFIED_HEADING`` so no screen shows them as OpenShell's own fields.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from tongs_agents.sandbox.openshell import OpenShell, OpenShellError

PENDING = "pending"
STATUSES = ("pending", "approved", "rejected")
RULE_TIMEOUT = 15.0
MAX_RULE_BYTES = 1024 * 1024
MAX_REASON = 500
CHUNK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
NO_RULES_RE = re.compile(r"No network rules for sandbox '([^']*)'")
HEADER_RE = re.compile(r"Network Rules:  \(version (\d{1,12}), (\d{1,6}) chunks?\)")
LINE_RE = re.compile(r"  ([A-Z][A-Za-z]*):(?: (.*))?")
CONFIDENCE_RE = re.compile(r"\d{1,3}%")
HITS_RE = re.compile(r"\d{1,12} \(first seen .*, last seen .*\)")
# Printed order of sandbox_draft_get; ``Validation`` is the pre-ba302a6 name of ``Prover``.
FIELDS = (
    ("Chunk", "chunk_id"),
    ("Status", "status"),
    ("Rule", "rule"),
    ("Binary", "binary"),
    ("Confidence", "confidence"),
    ("Rationale", "rationale"),
    ("Security", "security"),
    ("Prover", "prover"),
    ("Validation", "prover"),
    ("Application", "application"),
    ("Candidate", "candidate"),
    ("Endpoints", "endpoints"),
    ("Binaries", "binaries"),
    ("Hits", "hits"),
)
LABEL_ORDER = {label: index for index, (label, _name) in enumerate(FIELDS)}
LABEL_FIELD = dict(FIELDS)
REQUIRED = ("Chunk", "Status", "Rule", "Confidence", "Rationale")
UNVERIFIED_HEADING = ("From Rationale on, any line may be the agent's text "
                      "(it can imitate OpenShell's fields):")
QUOTE = "> "


class ParseError(ValueError):
    """``rule get`` printed something this parser does not recognise."""


class ApprovalError(RuntimeError):
    """An approval or rejection was refused or failed; the message says why."""


def clean(text: str) -> str:
    """Gateway text without terminal escapes or control characters (newlines kept)."""
    return CONTROL_RE.sub("", ESCAPE_RE.sub("", text or ""))


@dataclass(frozen=True)
class Proposal:
    """One drafted network rule as ``rule get`` prints it.

    The fields after ``rationale`` (``security`` to ``hits``) may be the
    agent's text (see the module notes); a spoofed one can only add to them,
    since a label OpenShell also printed repeats and fails the parse. ``text``
    is the proposal for review: the lines before Rationale as printed, then
    ``UNVERIFIED_HEADING`` and the rest, each line prefixed with ``QUOTE``.
    """

    chunk_id: str
    status: str
    rule: str
    confidence: str
    rationale: str
    binary: str = ""
    security: str = ""
    prover: str = ""
    application: str = ""
    candidate: str = ""
    endpoints: str = ""
    binaries: str = ""
    hits: str = ""
    text: str = ""

    @property
    def flagged(self) -> bool:
        """OpenShell attached security notes (shown first in the review)."""
        return bool(self.security)

    def fingerprint(self) -> tuple:
        """Everything a reviewer decided on; the hit count may grow meanwhile."""
        return (self.chunk_id, self.status, self.rule, self.binary, self.confidence,
                self.rationale, self.security, self.prover, self.application,
                self.candidate, self.endpoints, self.binaries)

    def summary(self) -> str:
        """The Rule line, printed before Rationale, so the agent cannot replace it."""
        return self.rule

    @property
    def unverified_fields(self) -> tuple[str, ...]:
        """Labels after Rationale that hold a value, possibly written by the agent."""
        return tuple(label for label, name in FIELDS[LABEL_ORDER["Rationale"] + 1:]
                     if getattr(self, name) and label != "Validation")


@dataclass(frozen=True)
class RuleListing:
    """The pending rules of one sandbox, or why they could not be read."""

    sandbox: str
    gateway: str | None = None
    proposals: tuple[Proposal, ...] = ()
    raw: str = ""
    version: int | None = None
    parse_error: str | None = None
    failure: str | None = None

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.gateway, self.sandbox)

    @property
    def needs_you(self) -> bool:
        """Pending proposals, or output that must be read by a person."""
        return bool(self.proposals) or self.parse_error is not None

    def proposal(self, chunk_id: str) -> Proposal | None:
        return next((item for item in self.proposals if item.chunk_id == chunk_id), None)


def _parse_block(lines: list[str], status: str | None) -> Proposal:
    values: dict[str, str] = {}
    last = -1
    for line in lines:
        match = LINE_RE.fullmatch(line)
        if match is None:
            raise ParseError(f"unrecognised line {line[:80]!r}")
        label, value = match.group(1), match.group(2) or ""
        order = LABEL_ORDER.get(label)
        if order is None:
            raise ParseError(f"unknown label {label!r}")
        if order <= last:
            raise ParseError(f"label {label!r} repeated or out of order")
        if label == "Validation" and "prover" in values:
            raise ParseError("both Prover and Validation lines")
        last = order
        values[LABEL_FIELD[label]] = value
    missing = [label for label in REQUIRED if LABEL_FIELD[label] not in values]
    if missing:
        raise ParseError(f"a block lacks {', '.join(missing)}")
    if not CHUNK_ID_RE.fullmatch(values["chunk_id"]):
        raise ParseError(f"invalid chunk id {values['chunk_id'][:80]!r}")
    if values["status"] not in STATUSES:
        raise ParseError(f"unknown status {values['status'][:40]!r}")
    if status is not None and values["status"] != status:
        raise ParseError(f"a {values['status']} rule in a --status {status} listing")
    if not CONFIDENCE_RE.fullmatch(values["confidence"]):
        raise ParseError(f"unrecognised confidence {values['confidence'][:40]!r}")
    if "hits" in values and not HITS_RE.fullmatch(values["hits"]):
        raise ParseError(f"unrecognised hits {values['hits'][:80]!r}")
    return Proposal(**values, text=_review_text(lines))


def _review_text(lines: list[str]) -> str:
    """The block for review, quoting the Rationale line and every line after it."""
    split = next(index for index, line in enumerate(lines)
                 if LINE_RE.fullmatch(line).group(1) == "Rationale")
    shown = [line[2:] for line in lines[:split]]
    shown.append(UNVERIFIED_HEADING)
    shown.extend(QUOTE + line[2:] for line in lines[split:])
    return "\n".join(shown)


def parse_rules(text: str, *, sandbox: str | None = None,
                status: str | None = PENDING) -> tuple[int | None, tuple[Proposal, ...]]:
    """``(draft version, proposals)`` from ``rule get`` text; raises ParseError."""
    lines = clean(text).split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        raise ParseError("no output")
    none = NO_RULES_RE.fullmatch(lines[0])
    if none is not None:
        if len(lines) != 1:
            raise ParseError("text after the no-rules line")
        if sandbox is not None and none.group(1) != sandbox:
            raise ParseError(f"rules of another sandbox {none.group(1)[:80]!r}")
        return None, ()
    header = HEADER_RE.fullmatch(lines[0])
    if header is None:
        raise ParseError(f"unrecognised first line {lines[0][:80]!r}")
    version, count = int(header.group(1)), int(header.group(2))
    if len(lines) < 3 or lines[1] != "":
        raise ParseError("no blank line after the header")
    blocks: list[list[str]] = [[]]
    for line in lines[2:]:
        if line == "":
            if not blocks[-1]:
                raise ParseError("unexpected blank line")
            blocks.append([])
        else:
            blocks[-1].append(line)
    if len(blocks) != count:
        raise ParseError(f"the header announces {count} rule(s), the text has {len(blocks)}")
    proposals = tuple(_parse_block(block, status) for block in blocks)
    ids = [proposal.chunk_id for proposal in proposals]
    if len(set(ids)) != len(ids):
        raise ParseError("a chunk id appears twice")
    return version, proposals


def check_chunk_id(value: str) -> str:
    if not isinstance(value, str) or not CHUNK_ID_RE.fullmatch(value):
        raise ApprovalError(f"{str(value)[:80]!r} is not a chunk id")
    return value


def check_reason(value: str) -> str:
    """A rejection reason: required, one printable line, at most MAX_REASON characters."""
    reason = (value or "").strip()
    if not reason:
        raise ApprovalError("a rejection needs a reason")
    if len(reason) > MAX_REASON or not reason.isprintable():
        raise ApprovalError(f"the reason must be one printable line of at most {MAX_REASON} "
                            "characters")
    return reason


def list_rules(client: OpenShell, sandbox: str, *, status: str | None = PENDING,
               timeout: float = RULE_TIMEOUT) -> RuleListing:
    """``rule get`` for one sandbox; never raises (``failure`` or ``parse_error`` say why)."""
    args = ["rule", "get", sandbox]
    if status is not None:
        args += ["--status", status]
    gateway = client.gateway
    try:
        result = client.run(*args, timeout=timeout, max_bytes=MAX_RULE_BYTES)
    except OpenShellError as error:
        return RuleListing(sandbox, gateway, failure=str(error))
    if not result.ok:
        return RuleListing(sandbox, gateway, failure=f"openshell rule get {sandbox} failed: "
                                                     f"{clean(result.error_text())}")
    raw = clean(result.stdout)
    try:
        version, proposals = parse_rules(raw, sandbox=sandbox, status=status)
    except ParseError as error:
        return RuleListing(sandbox, gateway, raw=raw, parse_error=str(error))
    return RuleListing(sandbox, gateway, proposals=proposals, raw=raw, version=version)


def _still_pending(client: OpenShell, sandbox: str, chunk_id: str) -> Proposal:
    listing = list_rules(client, sandbox)
    if listing.failure is not None:
        raise ApprovalError(listing.failure)
    if listing.parse_error is not None:
        raise ApprovalError(f"cannot read the pending rules of {sandbox}: "
                            f"{listing.parse_error}")
    current = listing.proposal(chunk_id)
    if current is None:
        raise ApprovalError(f"rule {chunk_id} of {sandbox} is no longer pending")
    return current


def approve(client: OpenShell, sandbox: str, reviewed: Proposal) -> str:
    """Approve one proposal the maintainer reviewed; returns OpenShell's confirmation."""
    chunk_id = check_chunk_id(reviewed.chunk_id)
    current = _still_pending(client, sandbox, chunk_id)
    if current.fingerprint() != reviewed.fingerprint():
        raise ApprovalError(f"rule {chunk_id} of {sandbox} changed since it was shown; "
                            "review it again")
    try:
        result = client.run("rule", "approve", sandbox, "--chunk-id", chunk_id,
                            timeout=RULE_TIMEOUT, max_bytes=64 * 1024)
    except OpenShellError as error:
        raise ApprovalError(str(error)) from None
    if not result.ok:
        raise ApprovalError(f"openshell rule approve failed: {clean(result.error_text())}")
    return " ".join(clean(result.stdout).split()) or f"Approved {chunk_id}"


def reject(client: OpenShell, sandbox: str, reviewed: Proposal, reason: str) -> str:
    """Reject one pending proposal with a reason; returns OpenShell's confirmation."""
    chunk_id = check_chunk_id(reviewed.chunk_id)
    reason = check_reason(reason)
    _still_pending(client, sandbox, chunk_id)
    try:
        result = client.run("rule", "reject", sandbox, "--chunk-id", chunk_id,
                            "--reason", reason, timeout=RULE_TIMEOUT, max_bytes=64 * 1024)
    except OpenShellError as error:
        raise ApprovalError(str(error)) from None
    if not result.ok:
        raise ApprovalError(f"openshell rule reject failed: {clean(result.error_text())}")
    return " ".join(clean(result.stdout).split()) or f"Rejected {chunk_id}"


def poll(targets: Iterable[tuple[str | None, str]],
         client_for: Callable[[str | None], OpenShell] | None = None,
         ) -> dict[tuple[str | None, str], RuleListing]:
    """The pending rules of each ``(gateway, sandbox)``, one ``rule get`` each; never raises.

    Only ``rule get`` runs here. A missing CLI becomes each listing's ``failure``.
    """
    factory = client_for or (lambda gateway: OpenShell(gateway=gateway))
    clients: dict[str | None, OpenShell | str] = {}
    listings: dict[tuple[str | None, str], RuleListing] = {}
    for gateway, sandbox in sorted(set(targets), key=lambda key: (key[0] or "", key[1])):
        if gateway not in clients:
            try:
                clients[gateway] = factory(gateway)
            except OpenShellError as error:
                clients[gateway] = str(error)
        client = clients[gateway]
        if isinstance(client, str):
            listings[(gateway, sandbox)] = RuleListing(sandbox, gateway, failure=client)
        else:
            listings[(gateway, sandbox)] = list_rules(client, sandbox)
    return listings


def new_proposals(before: Mapping[tuple[str | None, str], RuleListing],
                  after: Mapping[tuple[str | None, str], RuleListing]
                  ) -> list[tuple[RuleListing, Proposal]]:
    """Proposals in ``after`` that ``before`` did not list, for one notification each."""
    seen = {(key, proposal.chunk_id) for key, listing in before.items()
            for proposal in listing.proposals}
    return [(listing, proposal) for key, listing in after.items()
            for proposal in listing.proposals if (key, proposal.chunk_id) not in seen]
