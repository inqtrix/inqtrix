"""Exact captured text, immutable archive parts and versioned read windows.

The archive stores captured text, not a repaired Markdown rendering. Offsets
refer to Unicode code points in that text. Existing rendered archive rows
remain readable, but cannot attest the fidelity of their original input.
"""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


CONTEXT_ARCHIVE_SECTION_CHARS = 20_000
"""Storage part size; splitting never removes or inserts characters."""
ARCHIVE_READ_WINDOW_CHARS = 20_000
"""Maximum text transfer in one archive read; more text stays readable."""
ARCHIVE_CATALOGUE_PAGE_SIZE = 50
ARCHIVE_CATALOGUE_MAX_PAGE_SIZE = 100
ARCHIVE_PAYLOAD_KEY = "context_archive"
ARCHIVE_SCHEMA = 1
TRANSCRIPT_PAYLOAD_KEY = "kernel_transcript"


def is_private_archive(payload: Mapping[str, Any]) -> bool:
    """Internal snapshots never inherit published-result access."""
    return TRANSCRIPT_PAYLOAD_KEY in payload


class ArchiveIntegrityError(ValueError):
    """Stored archive parts do not establish an intact captured source."""


class ArchiveVersionConflict(ValueError):
    """A continuation does not name the currently readable source version."""


class ArchiveCursorError(ValueError):
    """An archive catalogue cursor is invalid or belongs to an older set."""


def archive_prefix(run_id: str) -> str:
    return f"art_{run_id[-12:]}_ctx"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_archive_data(value: Any) -> str:
    """JSON keeps exact text on decode and cannot close a data boundary."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


def is_exact_archive(payload: Mapping[str, Any]) -> bool:
    """Unknown future archive schemas also remain protected from mutation."""
    return ARCHIVE_PAYLOAD_KEY in payload


@dataclass(frozen=True)
class ArchivePart:
    artifact_id: str
    title: str
    text: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class ArchivePartInfo:
    artifact_id: str
    source_id: str
    title: str
    content_hash: str
    part_hash: str
    index: int
    count: int
    start: int
    end: int
    total_chars: int


def prepare_archive(
    run_id: str, title: str, text: str, *, transcript: dict[str, Any] | None = None,
) -> tuple[ArchivePart, ...]:
    """Create content-addressed parts; identical writes have identical ids."""
    if (
        not isinstance(text, str)
        or not isinstance(title, str)
        or not isinstance(run_id, str)
        or not run_id
    ):
        raise ValueError("Archive text, title and run identity are required.")
    if "\x00" in text or "\x00" in title:
        raise ValueError("Archive text cannot contain null code points.")
    if not text:
        return ()
    digest = text_hash(text)
    identity_parts: list[Any] = [ARCHIVE_SCHEMA, run_id, title, digest]
    if transcript is not None:
        transcript = deepcopy(transcript)
        if not isinstance(transcript, dict) or transcript.get("schema") != 1 or not isinstance(transcript.get("checkpoint_id"), str):
            raise ValueError("A versioned transcript descriptor is required.")
        identity_parts.append({TRANSCRIPT_PAYLOAD_KEY: transcript})
    identity = text_hash(json.dumps(identity_parts, ensure_ascii=False, sort_keys=True))
    source_id = f"{archive_prefix(run_id)}_{identity[:20]}"
    count = (
        len(text) + CONTEXT_ARCHIVE_SECTION_CHARS - 1
    ) // CONTEXT_ARCHIVE_SECTION_CHARS
    parts = []
    for index, start in enumerate(range(0, len(text), CONTEXT_ARCHIVE_SECTION_CHARS)):
        body = text[start : start + CONTEXT_ARCHIVE_SECTION_CHARS]
        part_id = source_id if index == 0 else f"{source_id}_{index}"
        metadata = {
            "schema": ARCHIVE_SCHEMA,
            "source_id": source_id,
            "title": title,
            "content_hash": digest,
            "part_hash": text_hash(body),
            "index": index,
            "count": count,
            "start": start,
            "end": start + len(body),
            "total_chars": len(text),
        }
        parts.append(
            ArchivePart(
                artifact_id=part_id,
                title=title if count == 1 else f"{title} ({index + 1}/{count})",
                text=body,
                payload={
                    ARCHIVE_PAYLOAD_KEY: metadata,
                    **({TRANSCRIPT_PAYLOAD_KEY: transcript} if transcript is not None else {}),
                },
            )
        )
    return tuple(parts)


def part_info(artifact_id: str, payload: Mapping[str, Any]) -> ArchivePartInfo | None:
    if not is_exact_archive(payload):
        return None
    meta = payload[ARCHIVE_PAYLOAD_KEY]
    required = {
        "schema",
        "source_id",
        "title",
        "content_hash",
        "part_hash",
        "index",
        "count",
        "start",
        "end",
        "total_chars",
    }
    if (
        not isinstance(meta, dict)
        or set(meta) != required
        or type(meta["schema"]) is not int
        or meta["schema"] != ARCHIVE_SCHEMA
    ):
        raise ArchiveIntegrityError("Unsupported or invalid archive metadata.")
    if any(
        type(meta[key]) is not int
        for key in ("index", "count", "start", "end", "total_chars")
    ):
        raise ArchiveIntegrityError("Archive coordinates must be integers.")
    if not all(
        isinstance(meta[key], str)
        for key in ("source_id", "title", "content_hash", "part_hash")
    ):
        raise ArchiveIntegrityError("Archive identities must be text.")
    if any(
        len(meta[key]) != 64 or any(c not in "0123456789abcdef" for c in meta[key])
        for key in ("content_hash", "part_hash")
    ):
        raise ArchiveIntegrityError("Archive fingerprint is invalid.")
    if not (
        0 <= meta["index"] < meta["count"]
        and 0 <= meta["start"] < meta["end"] <= meta["total_chars"]
    ):
        raise ArchiveIntegrityError("Archive coordinates are inconsistent.")
    expected_id = (
        meta["source_id"]
        if meta["index"] == 0
        else f"{meta['source_id']}_{meta['index']}"
    )
    if artifact_id != expected_id:
        raise ArchiveIntegrityError("Archive part identity is inconsistent.")
    return ArchivePartInfo(
        artifact_id=artifact_id, **{key: meta[key] for key in required - {"schema"}}
    )


def validate_parts(parts: Sequence[ArchivePartInfo]) -> tuple[ArchivePartInfo, ...]:
    ordered = tuple(sorted(parts, key=lambda part: part.index))
    if not ordered or len(ordered) != ordered[0].count:
        raise ArchiveIntegrityError("Archive is incomplete.")
    first = ordered[0]
    end = 0
    for index, part in enumerate(ordered):
        if (
            part.index != index
            or part.start != end
            or part.count != first.count
            or part.source_id != first.source_id
            or part.content_hash != first.content_hash
            or part.total_chars != first.total_chars
            or part.title != first.title
        ):
            raise ArchiveIntegrityError(
                "Archive parts do not form one contiguous source."
            )
        end = part.end
    if end != first.total_chars:
        raise ArchiveIntegrityError("Archive does not reach the end of its source.")
    return ordered


@dataclass(frozen=True)
class ArchiveWindow:
    source_id: str
    title: str
    content_hash: str
    text: str
    offset: int
    end: int
    total_chars: int
    next_offset: int | None
    fidelity: str

    @property
    def is_full_source(self) -> bool:
        return self.offset == 0 and self.end == self.total_chars


def window_bounds(
    total: int, offset: int, max_chars: int, expected_hash: str, actual_hash: str
) -> tuple[int, int]:
    if type(offset) is not int or not 0 <= offset <= total:
        raise ValueError("Archive offset is outside the source.")
    if type(max_chars) is not int or not 1 <= max_chars <= ARCHIVE_READ_WINDOW_CHARS:
        raise ValueError(
            f"Archive read size must be between 1 and {ARCHIVE_READ_WINDOW_CHARS} characters."
        )
    if expected_hash and expected_hash != actual_hash:
        raise ArchiveVersionConflict(
            "Archive content fingerprint differs; read the source again."
        )
    return offset, min(total, offset + max_chars)


def window_from_parts(
    parts: Sequence[ArchivePartInfo],
    bodies: Mapping[str, str],
    *,
    offset: int,
    max_chars: int,
    content_hash: str = "",
) -> ArchiveWindow:
    ordered = validate_parts(parts)
    first = ordered[0]
    start, end = window_bounds(
        first.total_chars, offset, max_chars, content_hash, first.content_hash
    )
    selected = []
    for part in ordered:
        if part.end <= start or part.start >= end:
            continue
        body = bodies.get(part.artifact_id)
        if (
            body is None
            or len(body) != part.end - part.start
            or text_hash(body) != part.part_hash
        ):
            raise ArchiveIntegrityError(
                "Archive part text does not match its fingerprint."
            )
        selected.append(
            body[max(0, start - part.start) : min(len(body), end - part.start)]
        )
    text = "".join(selected)
    if len(text) != end - start:
        raise ArchiveIntegrityError("Archive window is incomplete.")
    return ArchiveWindow(
        first.source_id,
        first.title,
        first.content_hash,
        text,
        start,
        end,
        first.total_chars,
        end if end < first.total_chars else None,
        "verbatim_captured_text",
    )


def legacy_window(
    artifact_id: str,
    title: str,
    text: str,
    *,
    offset: int,
    max_chars: int,
    content_hash: str = "",
) -> ArchiveWindow:
    digest = text_hash(text)
    start, end = window_bounds(len(text), offset, max_chars, content_hash, digest)
    return ArchiveWindow(
        artifact_id,
        title,
        digest,
        text[start:end],
        start,
        end,
        len(text),
        end if end < len(text) else None,
        "legacy_rendered_text",
    )


@dataclass(frozen=True)
class ArchiveCatalogueEntry:
    source_id: str
    title: str
    revision: int
    total_chars: int
    parts: int
    content_hash: str | None
    fidelity: str
    created_at: float
    transcript: dict[str, Any] | None = field(default=None, repr=False)


def catalogue_entry(
    artifact_id: str,
    title: str,
    revision: int,
    payload: Mapping[str, Any],
    stored_chars: int,
    created_at: float,
) -> ArchiveCatalogueEntry | None:
    part = part_info(artifact_id, payload)
    if part is not None:
        if part.index:
            return None
        return ArchiveCatalogueEntry(
            part.source_id,
            part.title,
            revision,
            part.total_chars,
            part.count,
            part.content_hash,
            "verbatim_captured_text",
            created_at,
            deepcopy(payload[TRANSCRIPT_PAYLOAD_KEY]) if is_private_archive(payload) else None,
        )
    return ArchiveCatalogueEntry(
        artifact_id,
        title,
        revision,
        stored_chars,
        1,
        None,
        "legacy_rendered_text",
        created_at,
    )


@dataclass(frozen=True)
class ArchiveCataloguePage:
    entries: tuple[ArchiveCatalogueEntry, ...]
    total_sources: int
    next_cursor: str | None
    snapshot_hash: str
    offset: int = 0
    """Index of the first entry of this page within the ordered catalogue."""


def catalogue_page(
    run_id: str,
    entries: Sequence[ArchiveCatalogueEntry],
    *,
    cursor: str = "",
    limit: int = ARCHIVE_CATALOGUE_PAGE_SIZE,
) -> ArchiveCataloguePage:
    """Bind pages to a whole metadata snapshot, not an insertion-id watermark."""
    if type(limit) is not int or not 1 <= limit <= ARCHIVE_CATALOGUE_MAX_PAGE_SIZE:
        raise ValueError(
            f"Archive catalogue size must be between 1 and {ARCHIVE_CATALOGUE_MAX_PAGE_SIZE}."
        )
    if not isinstance(cursor, str):
        raise ArchiveCursorError("Archive cursor must be text.")
    ordered = tuple(
        sorted(entries, key=lambda entry: (entry.created_at, entry.source_id))
    )
    digest = text_hash(
        json.dumps(
            [asdict(entry) for entry in ordered], sort_keys=True, ensure_ascii=False
        )
    )
    start = 0
    if cursor:
        try:
            if len(cursor) > 1024:
                raise ValueError("oversized cursor")
            value = json.loads(
                base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
            )
            if (
                not isinstance(value, list)
                or len(value) != 4
                or type(value[0]) is not int
                or value[0] != 1
                or value[1] != run_id
                or value[2] != digest
                or type(value[3]) is not int
                or not 0 <= value[3] <= len(ordered)
            ):
                raise ValueError("inconsistent cursor")
            start = value[3]
        except (ValueError, UnicodeError) as exc:
            raise ArchiveCursorError(
                "Archive catalogue changed or cursor is invalid; list it again without a cursor."
            ) from exc
    end = min(len(ordered), start + limit)
    continuation = None
    if end < len(ordered):
        continuation = base64.urlsafe_b64encode(
            json.dumps([1, run_id, digest, end]).encode()
        ).decode("ascii")
    return ArchiveCataloguePage(ordered[start:end], len(ordered), continuation, digest, start)
