"""Canonical projection from retrieval candidates to reader-safe evidence.

Retrieval text is an internal ranking artifact and may contain model-generated
context.  Every user-, answer-, and agent-facing knowledge consumer must pass
through :class:`KnowledgeEvidenceProjector`, which exposes only the original
source text and fails closed when it is missing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from inqtrix.knowledge.stores.ports import DocumentChunk, RetrievalCandidate


class UnverifiedKnowledgeEvidence(ValueError):
    """A retrieval hit has no separately stored source evidence."""


def source_excerpt_is_verified(
    *,
    canonical_text: str,
    source_text: str,
    source_start: int | None,
    source_end: int | None,
    document_content_hash: str | None,
) -> bool:
    """Verify an excerpt against the immutable canonical document bytes.

    Character offsets are insufficient here because chunk provenance is stored
    as UTF-8 byte offsets.  The hash prevents a valid span from a previous
    revision being accepted against a later document body; the exact byte
    slice prevents repeated boilerplate from being attributed to the wrong
    occurrence.
    """
    if (
        not source_text
        or source_start is None
        or source_end is None
        or source_start < 0
        or source_end < source_start
        or not document_content_hash
    ):
        return False
    canonical_bytes = canonical_text.encode("utf-8")
    if hashlib.sha256(canonical_bytes).hexdigest() != document_content_hash:
        return False
    if source_end > len(canonical_bytes):
        return False
    return canonical_bytes[source_start:source_end] == source_text.encode("utf-8")


@dataclass(frozen=True)
class KnowledgeEvidenceHit:
    """Reader-safe knowledge evidence with optional exact provenance."""

    reference_id: str
    chunk_id: str
    document_id: str
    collection_id: str
    chunk_index: int
    title: str
    excerpt: str
    page_number: int | None
    score: float
    source_start: int | None
    source_end: int | None
    document_content_hash: str | None
    revision_id: str | None
    generation_id: str | None
    provenance_status: str

    def as_dict(self) -> dict[str, Any]:
        span = None
        if self.source_start is not None and self.source_end is not None:
            span = {
                "start": self.source_start,
                "end": self.source_end,
                "offset_unit": "utf8_byte",
                "document_content_hash": self.document_content_hash,
            }
        return {
            "reference_id": self.reference_id,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "collection_id": self.collection_id,
            "chunk_index": self.chunk_index,
            "title": self.title,
            "excerpt": self.excerpt,
            "page_number": self.page_number,
            "score": self.score,
            "source_span": span,
            "revision_id": self.revision_id,
            "generation_id": self.generation_id,
            "provenance_status": self.provenance_status,
        }


class KnowledgeEvidenceProjector:
    """The sole supported projection of a ranked hit into evidence."""

    @staticmethod
    def project(
        candidate: RetrievalCandidate,
        *,
        reference_id: str,
    ) -> KnowledgeEvidenceHit:
        return KnowledgeEvidenceProjector.project_chunk(
            candidate.chunk,
            reference_id=reference_id,
            title=candidate.document_title,
            score=candidate.score,
        )

    @staticmethod
    def project_chunk(
        chunk: DocumentChunk,
        *,
        reference_id: str,
        title: str,
        score: float = 0.0,
    ) -> KnowledgeEvidenceHit:
        """Project an explicitly addressed chunk through the same evidence gate.

        Chunk-detail and neighbour views do not originate from a ranked search,
        but they carry the same source-verification responsibility.  Keeping
        this entry point beside :meth:`project` prevents those readers from
        bypassing the fail-closed provenance contract.
        """
        source_text = chunk.source_text
        if (
            not source_text
            or not source_text.strip()
            or not chunk.source_verified
        ):
            raise UnverifiedKnowledgeEvidence(
                f"chunk {chunk.id} has no canonical span verification"
            )
        has_span = (
            chunk.source_start is not None
            and chunk.source_end is not None
            and chunk.source_start >= 0
            and chunk.source_end >= chunk.source_start
            and bool(chunk.document_content_hash)
        )
        if not has_span:
            raise UnverifiedKnowledgeEvidence(
                f"chunk {chunk.id} has no complete canonical span"
            )
        return KnowledgeEvidenceHit(
            reference_id=reference_id,
            chunk_id=chunk.id,
            document_id=chunk.document_id,
            collection_id=chunk.collection_id,
            chunk_index=chunk.chunk_index,
            title=title,
            excerpt=source_text,
            page_number=chunk.page_number,
            score=score,
            source_start=chunk.source_start,
            source_end=chunk.source_end,
            document_content_hash=chunk.document_content_hash,
            revision_id=chunk.revision_id,
            generation_id=chunk.generation_id,
            provenance_status="verified_span",
        )
