"""Bounded, versioned source transfers over the saved conversation."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictInt, field_validator

from inqtrix.agents.context_archive import (
    ARCHIVE_READ_WINDOW_CHARS, ArchiveVersionConflict, text_hash, window_bounds,
)
from inqtrix.urls import scrub_credential_urls

log = logging.getLogger(__name__)

SOURCE_PAGE_SIZE = 50
SOURCE_MAX_MATCHES = 20
SOURCE_SCAN_CHARS = 100_000
SOURCE_QUERY_CHARS = 512
SOURCE_UNSCANNED_LISTED = 12
"""Sources an incomplete search names as not reached yet, at most.

A limited result states where the remainder lives so the next call can
address it by ``source_id`` instead of paging blindly; ``unscanned_total``
carries the full count and an empty query lists every source."""
SOURCE_RECEIPT = "context_source_authorization"
MATERIAL_DEPENDENCIES = "material_dependencies"
"""``ToolMessage.artifact`` key under which a producing reader names the
knowledge documents (windows and search hits) and editor documents (with the
comment ids it actually transferred) a run took into its transcript.

These material dependencies travel with the private transcript descriptor
(``materials``) and are re-authorized against the reader's CURRENT rights and
the current deletion state whenever a later run of the same conversation
opens that transcript. A guessed id, a search hit alone or a text inside a
document never creates a dependency; only the producing tool does."""
SourceKind = Literal["question", "result", "artifact", "archive", "transcript", "clarification", "approval"]


def merge_materials(items) -> dict[str, list[dict[str, Any]]]:
    """Union material dependencies: documents by id, comment ids as a set."""
    knowledge: dict[str, dict[str, Any]] = {}
    editor: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for row in item.get("knowledge_documents") or ():
            if not isinstance(row, dict):
                continue
            document_id = str(row.get("document_id") or "")
            if not document_id:
                continue
            collection_id = str(row.get("collection_id") or "")
            entry = knowledge.setdefault(document_id, {"document_id": document_id, "collection_id": collection_id})
            if collection_id and not entry["collection_id"]:
                entry["collection_id"] = collection_id
        for row in item.get("editor_documents") or ():
            if not isinstance(row, dict):
                continue
            document_id = str(row.get("document_id") or "")
            if not document_id:
                continue
            entry = editor.setdefault(document_id, {"document_id": document_id, "comment_ids": set()})
            entry["comment_ids"].update(str(comment_id) for comment_id in (row.get("comment_ids") or ()) if comment_id)
    return {
        "knowledge_documents": [knowledge[key] for key in sorted(knowledge)],
        "editor_documents": [
            {"document_id": key, "comment_ids": sorted(editor[key]["comment_ids"])} for key in sorted(editor)
        ],
    }


def merge_source_receipts(receipts) -> dict[str, Any]:
    """Union trusted dependency receipts, retaining owner-only provenance.

    ``rule_revisions`` binds prompt-library rules by revision exactly like
    ``skill_revisions`` binds skills; it is present only when a receipt
    named a rule, so receipts of runs without rules keep their shape.
    """
    fields = ("collection_ids", "origin_run_ids", "owner_origin_run_ids")
    sets = {name: set() for name in fields}
    skills = {}
    rules = {}
    restricted = False
    materials = []
    for receipt in receipts:
        for name in fields:
            sets[name].update(receipt.get(name) or ())
        for skill_id, revision in (receipt.get("skill_revisions") or {}).items():
            if skill_id in skills and skills[skill_id] != revision:
                raise ValueError("Source dependencies contain conflicting skill versions.")
            skills[skill_id] = revision
        for template_id, revision in (receipt.get("rule_revisions") or {}).items():
            if template_id in rules and rules[template_id] != revision:
                raise ValueError("Source dependencies contain conflicting rule versions.")
            rules[template_id] = revision
        restricted |= bool(receipt.get("restricted_inputs"))
        materials.append(receipt.get("materials"))
    return {**{name: sorted(values) for name, values in sets.items()},
            "skill_revisions": skills, **({"rule_revisions": rules} if rules else {}),
            "restricted_inputs": restricted, "materials": merge_materials(materials)}


class ContextSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(default="", max_length=SOURCE_QUERY_CHARS)
    source_id: str = ""
    cursor: str = ""
    case_sensitive: bool = False


class TextWindowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: StrictInt = Field(default=0, ge=0)
    content_hash: str = ""
    max_chars: StrictInt = Field(default=ARCHIVE_READ_WINDOW_CHARS, ge=1, le=ARCHIVE_READ_WINDOW_CHARS)


class ContextReadInput(TextWindowInput):
    source_id: str = Field(min_length=1)


class MaterialReadInput(TextWindowInput):
    """A direct resource reader pins both representation and content."""
    version: str = ""


class MaterialWindow(BaseModel):
    version: str
    content_hash: str
    offset: int
    end: int
    total_chars: int
    is_full_source: bool
    next_read: dict[str, Any] | None


class MaterialReadOutput(BaseModel):
    representation: str = "text"
    window: MaterialWindow


def material_version(text: str, version_prefix: str) -> tuple[str, str]:
    """``(version, content_hash)`` of one text representation — the ONE spelling.

    Every reader pins its continuation to this pair, and every later check
    against a pinned read (a quote's citation, a resumed window) recomputes
    it here instead of rebuilding the format.
    """
    digest = text_hash(text)
    return f"{version_prefix}:{digest}", digest


def material_window(text: str, version_prefix: str, payload: MaterialReadInput) -> tuple[str, MaterialWindow]:
    """Slice the existing text; identifiers alone never assert content integrity."""
    version, digest = material_version(text, version_prefix)
    if payload.offset and (not payload.version or not payload.content_hash):
        raise ValueError("A continuation requires the source version and content fingerprint.")
    if payload.version and payload.version != version:
        raise ArchiveVersionConflict("Document changed; restart the read against its current version.")
    start, end = window_bounds(len(text), payload.offset, payload.max_chars, payload.content_hash, digest)
    return text[start:end], MaterialWindow(
        version=version, content_hash=digest, offset=start, end=end, total_chars=len(text),
        is_full_source=start == 0 and end == len(text),
        next_read={**payload.model_dump(), "offset": end, "version": version, "content_hash": digest}
        if end < len(text) else None,
    )


SOURCE_COVERAGE = "source_coverage"
"""``ToolMessage.artifact`` key under which every windowed reader attaches
its coverage records (a list of :class:`SourceCoverageRecord` dumps).

The artifact is checkpointed with the message, like the source receipt,
so the run-wide ledger is derived from the transcript on every segment
start and cannot be forged through user or tool text."""

SOURCE_COVERAGE_DIGEST_MAX_SOURCES = 25
"""Sources listed in the judge digest at most; the ledger itself is unbounded."""


class SourceCoverageRecord(BaseModel):
    """What one reader result actually transferred or scanned of one source version.

    ``read_ranges`` are text windows (or catalogue entry ranges when
    ``unit`` is ``entries``); ``scanned_ranges`` are searched spans.
    ``continuation_open`` names a PAGED continuation the result offered —
    a further search page, catalogue page or comment page. An unread text
    remainder is never flagged here; the ledger derives it from the ranges,
    so windows read in any order still close the source.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    version: str = ""
    unit: Literal["chars", "entries"] = "chars"
    total: StrictInt = Field(ge=0)
    read_ranges: list[list[int]] = Field(default_factory=list)
    scanned_ranges: list[list[int]] = Field(default_factory=list)
    continuation_open: bool = False

    @field_validator("read_ranges", "scanned_ranges")
    @classmethod
    def ordered_pairs(cls, value: list[list[int]]) -> list[list[int]]:
        # Pairs stay plain lists: the record round-trips through the
        # checkpoint serializer and the JSON snapshot unchanged.
        for pair in value:
            if len(pair) != 2 or pair[0] < 0 or pair[1] < pair[0]:
                raise ValueError("A coverage range is a pair with 0 <= start <= end.")
        return [[int(start), int(end)] for start, end in value]


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted union of half-open ranges; overlapping and adjacent ranges fuse."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted((int(a), int(b)) for a, b in ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def unread_ranges(read: Iterable[tuple[int, int]], total: int) -> list[tuple[int, int]]:
    """The complement of the merged *read* ranges within ``[0, total)``."""
    gaps: list[tuple[int, int]] = []
    cursor = 0
    for start, end in merge_ranges(read):
        if start > cursor:
            gaps.append((cursor, min(start, total)))
        cursor = max(cursor, end)
    if cursor < total:
        gaps.append((cursor, total))
    return [gap for gap in gaps if gap[1] > gap[0]]


def coverage_records_from_artifact(artifact: Any) -> list[SourceCoverageRecord]:
    """Validated records of one tool artifact; a malformed record is skipped loudly."""
    if not isinstance(artifact, dict):
        return []
    raw = artifact.get(SOURCE_COVERAGE)
    if not isinstance(raw, list):
        return []
    records: list[SourceCoverageRecord] = []
    for item in raw:
        try:
            records.append(SourceCoverageRecord.model_validate(item))
        except ValueError:
            log.warning("A source coverage record in a tool artifact is malformed and is ignored.")
    return records


class SourceCoverageLedger:
    """Run-wide union of what every reader transferred, per source version.

    Derived state: fed from checkpointed tool artifacts at segment start and
    from the live update stream afterwards. A source has an OPEN continuation
    while its read ranges leave a remainder, while its latest search page
    offered a further page, or while further catalogue or comment pages
    remain — the sufficiency judge advises on exactly this state.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str], dict[str, Any]] = {}

    def absorb(self, record: SourceCoverageRecord) -> None:
        key = (record.kind, record.source_id, record.version)
        entry = self._entries.get(key)
        if entry is None:
            entry = {
                "kind": record.kind, "source_id": record.source_id, "version": record.version,
                "unit": record.unit, "total": int(record.total),
                "read": [], "scanned": [], "search_open": False, "pages_open": False,
            }
            self._entries[key] = entry
        entry["total"] = max(int(entry["total"]), int(record.total))
        entry["read"] = merge_ranges([*entry["read"], *record.read_ranges])
        entry["scanned"] = merge_ranges([*entry["scanned"], *record.scanned_ranges])
        if record.scanned_ranges:
            entry["search_open"] = bool(record.continuation_open)
        else:
            entry["pages_open"] = bool(record.continuation_open)

    def absorb_many(self, records: Iterable[SourceCoverageRecord]) -> None:
        for record in records:
            self.absorb(record)

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[dict[str, Any]]:
        """Canonical, JSON-ready projection in first-seen order."""
        projected: list[dict[str, Any]] = []
        for entry in self._entries.values():
            total = int(entry["total"])
            read = merge_ranges(entry["read"])
            complete = total == 0 or read == [(0, total)]
            gaps = unread_ranges(read, total) if read else []
            projected.append({
                "kind": entry["kind"],
                "source_id": entry["source_id"],
                "version": entry["version"],
                "unit": entry["unit"],
                "total": total,
                "read_units": sum(end - start for start, end in read),
                "read_ranges": [[start, end] for start, end in read],
                "scanned_ranges": [[start, end] for start, end in merge_ranges(entry["scanned"])],
                "unread_ranges": [[start, end] for start, end in gaps],
                "complete": complete,
                "continuation_open": bool(
                    (read and not complete) or entry["search_open"] or entry["pages_open"]
                ),
            })
        return projected

    def open_entries(self) -> list[dict[str, Any]]:
        return [entry for entry in self.entries() if entry["continuation_open"]]

    def digest(self, *, max_sources: int = SOURCE_COVERAGE_DIGEST_MAX_SOURCES) -> str:
        """Compact prompt lines for the sufficiency judge; empty when nothing was read."""
        lines: list[str] = []
        for entry in self.entries()[:max_sources]:
            version = entry["version"]
            shown_version = version if len(version) <= 16 else version[:16] + "…"
            ranges = ", ".join(f"[{start}, {end})" for start, end in entry["read_ranges"])
            if entry["complete"] and entry["read_ranges"]:
                state = f"read completely ({entry['total']} {entry['unit']})"
            elif entry["read_ranges"]:
                state = f"read {ranges} of {entry['total']} {entry['unit']}"
            else:
                state = f"not read ({entry['total']} {entry['unit']})"
            if entry["unread_ranges"]:
                state += "; unread " + ", ".join(f"[{start}, {end})" for start, end in entry["unread_ranges"])
            if entry["scanned_ranges"]:
                state += "; searched " + ", ".join(f"[{start}, {end})" for start, end in entry["scanned_ranges"])
            state += "; continuation open" if entry["continuation_open"] else "; no open continuation"
            lines.append(f"- {entry['kind']} {entry['source_id']} (version {shown_version}): {state}")
        remaining = len(self) - min(len(self), max_sources)
        if remaining:
            lines.append(f"- {remaining} further sources not listed")
        return "\n".join(lines)

    def remainder_summary(self, *, max_sources: int = 5) -> str:
        """Names the open sources with their unread remainders, for the advisory nudge."""
        parts: list[str] = []
        for entry in self.open_entries()[:max_sources]:
            gaps = ", ".join(f"[{start}, {end})" for start, end in entry["unread_ranges"]) or "a further page"
            parts.append(f"{entry['kind']} {entry['source_id']}: unread {gaps} of {entry['total']} {entry['unit']}")
        return "; ".join(parts)


class ContextSource(BaseModel):
    source_id: str
    kind: SourceKind
    title: str
    origin_run_id: str
    version: str
    available: bool = True
    unavailable_reason: str = ""

    @field_validator("title")
    @classmethod
    def credential_safe_title(cls, value: str) -> str:
        """Display metadata must not bypass the original reader's protection."""
        return scrub_credential_urls(value)


class ContextWindow(BaseModel):
    source: ContextSource
    text: str
    offset: int
    end: int
    total_chars: int
    content_hash: str
    representation: str
    is_full_source: bool
    next_read: dict[str, Any] | None
    _authorization_receipt: dict[str, Any] = PrivateAttr(default_factory=dict)


class ContextSearchOutput(BaseModel):
    """Coverage belongs to this response; a supplied cursor is not proof of prior reading."""
    sources: list[ContextSource]
    matches: list[dict[str, Any]] = Field(default_factory=list)
    scanned_ranges: list[dict[str, Any]] = Field(default_factory=list)
    unavailable_sources: list[ContextSource] = Field(default_factory=list)
    unscanned_sources: list[ContextSource] = Field(default_factory=list)
    unscanned_total: int = 0
    scan_complete: bool
    corpus_complete: bool
    next_cursor: str | None
    next_search: ContextSearchInput | None = None
    query: str
    _authorization_receipt: dict[str, Any] = PrivateAttr(default_factory=dict)


def encode_source(kind: str, run_id: str, local_id: str, version: str) -> str:
    value = json.dumps([kind, run_id, local_id, version], ensure_ascii=False).encode()
    return "src_" + base64.urlsafe_b64encode(value).decode()


def decode_source(value: str) -> tuple[str, str, str, str]:
    try:
        if not value.startswith("src_") or len(value) > 4096:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value[4:].encode("ascii")))
        if not isinstance(decoded, list) or len(decoded) != 4 or not all(isinstance(x, str) for x in decoded):
            raise ValueError()
        if decoded[0] not in {"question", "result", "artifact", "archive", "transcript", "clarification", "approval"}:
            raise ValueError()
        return tuple(decoded)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Invalid source reference.") from exc


def has_external_input(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("type") in {"file", "image", "input_file", "input_image"}:
            return True
        if any(key in value for key in ("file_id", "file_url", "image_url", "document_id")):
            return True
        return any(has_external_input(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(has_external_input(item) for item in value)
    return False
