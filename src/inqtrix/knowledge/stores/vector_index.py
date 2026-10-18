"""Vector-index port: chunk vectors only, keyed by chunk id.

The Postgres-canonical knowledge store keeps collections/documents/chunks
relationally (the source of truth) and delegates ONLY the embedding
vectors to a :class:`VectorIndex`. This is the standard split — Postgres
is the canonical store, the vector DB is a derived index holding vectors
plus a lean payload (just the keys needed to filter and to join back to
the canonical rows).

Two implementations:

* :class:`MemoryVectorIndex` — in-process dense cosine, zero
  infrastructure (dev/test, and the ``postgres`` storage tier without a
  Qdrant service). Lost on restart; rebuildable from the canonical text
  via reindex. Dense-only (no hybrid).
* ``QdrantVectorIndex`` (in :mod:`inqtrix.knowledge.stores.qdrant_store`)
  — dense + optional BM25 sparse, persistent.

The port is async to match the knowledge store; the Qdrant client calls
run off the event loop via ``asyncio.to_thread``.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChunkVector:
    """One chunk's dense vector plus the text the sparse branch needs.

    Attributes:
        chunk_id: Canonical chunk id (``kch_...``) — the join key back to
            the Postgres ``knowledge_chunks`` row.
        dense: The dense embedding for the chunk.
        text: The embedded chunk text; a hybrid index recomputes its BM25
            sparse vector from this. Dense-only indexes ignore it.
        generation_id: Physical index generation this point belongs to.
            ``None`` is the explicit legacy generation, not a wildcard.
        revision_id: Immutable document revision this point represents.
            ``None`` is the explicit legacy revision, not a wildcard.
    """

    chunk_id: str
    dense: tuple[float, ...]
    text: str = field(default="", repr=False)
    generation_id: str | None = None
    revision_id: str | None = None


@dataclass(frozen=True)
class VectorSearchScope:
    """Canonical active vector scope for one logical collection.

    A collection generation is selected atomically in Postgres.  Document
    revisions are selected independently inside that generation.  Passing both
    selectors into the vector index prevents unpublished points from competing
    in nearest-neighbour or sparse ranking before canonical hydration runs.

    ``generation_id=None`` matches only points whose generation payload is
    absent/null.  Likewise, legacy documents are explicitly named in
    ``legacy_document_ids`` and match only points without a revision payload.
    ``legacy_payload_chunk_ids`` is narrower still: it is populated only
    while the migration-marked compatibility generation is active and admits
    old points that predate *both* payload fields for those exact verified
    canonical chunk ids.
    Canonical hydration still requires the active generation/revision and a
    verified source span.  None of these values means "all".
    """

    collection_id: str
    generation_id: str | None
    active_revision_ids: tuple[str, ...] = ()
    legacy_document_ids: tuple[str, ...] = ()
    legacy_payload_chunk_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class VectorPointRef:
    """Lean identity of one derived point returned by reconciliation scroll."""

    chunk_id: str
    collection_id: str
    document_id: str
    generation_id: str | None = None
    revision_id: str | None = None


@dataclass(frozen=True)
class VectorHit:
    """One scored vector match (the chunk id + similarity score).

    The knowledge store hydrates the chunk text/source_text and document
    title from Postgres for these ids — the vector index never carries
    that content.
    """

    chunk_id: str
    score: float


def _cosine(a: tuple[float, ...], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


@runtime_checkable
class VectorIndex(Protocol):
    """Dense (+ optional sparse) vector store keyed by canonical chunk id."""

    @property
    def supports_hybrid(self) -> bool:
        """Whether a lexical (BM25) branch is configured for hybrid search."""
        ...

    async def is_available(self) -> bool:
        """Return whether the backing vector service is reachable now."""
        ...

    async def ensure_model(
        self, *, embedding_model: str, embedding_dim: int
    ) -> None:
        """Prepare the per-model index space (no-op if already present)."""
        ...

    async def upsert(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        document_id: str,
        vectors: list[ChunkVector],
    ) -> None:
        """Insert/replace the vectors for one document's chunks."""
        ...

    async def delete_document(
        self, *, embedding_model: str, document_id: str
    ) -> None:
        """Remove all chunk vectors of one document."""
        ...

    async def count_document(
        self, *, embedding_model: str, document_id: str
    ) -> int:
        """Count one logical document's residual points after deletion."""
        ...

    async def delete_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> None:
        """Remove exactly the identified vectors after an atomic pointer swap."""
        ...

    async def count_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> int:
        """Count exactly identified points for deletion verification."""
        ...

    async def count_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> int:
        """Count every point in one logical collection generation."""
        ...

    async def delete_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        """Delete one exact logical generation without a large id payload."""
        ...

    async def count_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> int:
        """Count one document's points inside an exact shadow generation."""
        ...

    async def delete_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> None:
        """Delete one document scope, including points unknown to Postgres."""
        ...

    def point_ids_for_chunks(self, chunk_ids: list[str]) -> list[str]:
        """Return stable physical point identifiers for a cleanup manifest."""
        ...

    async def delete_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> None:
        """Remove all chunk vectors of one logical collection."""
        ...

    async def count_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> int:
        """Count a logical collection's residual points after deletion."""
        ...

    async def search(
        self,
        *,
        embedding_model: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        """Dense nearest-neighbour search within canonical active scopes."""
        ...

    async def hybrid_search(
        self,
        *,
        embedding_model: str,
        query_text: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        """Fused dense + sparse search (only when ``supports_hybrid``)."""
        ...

    async def scroll_chunk_groups(
        self, *, embedding_model: str
    ) -> set[tuple[str, str]]:
        """Distinct ``(collection_id, document_id)`` groups present in the
        model's vector space. The reverse-reconcile input: the canonical store
        diffs this against its Postgres rows to find vectors whose document was
        deleted (cross-store deletes are non-atomic, so a crash can strand
        them)."""
        ...

    async def scroll_chunk_points(
        self, *, embedding_model: str
    ) -> list[VectorPointRef]:
        """Return every point identity in one model space for exact reconcile."""
        ...


@dataclass
class _MemoryEntry:
    dense: tuple[float, ...]
    collection_id: str
    document_id: str
    embedding_model: str
    generation_id: str | None
    revision_id: str | None


class MemoryVectorIndex:
    """In-process dense cosine vector index (zero infrastructure).

    Vectors live in a thread-safe dict keyed by chunk id; search is exact
    O(vectors) cosine over the scoped collections, mirroring the
    in-memory knowledge store's honesty about scale. Lost on restart and
    rebuildable from the canonical text via reindex. Dense-only — it does
    not advertise the hybrid capability.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _MemoryEntry] = {}
        self._lock = threading.RLock()

    @property
    def supports_hybrid(self) -> bool:
        return False

    async def is_available(self) -> bool:
        """In-memory vectors are available whenever the process is alive."""
        return True

    async def ensure_model(
        self, *, embedding_model: str, embedding_dim: int
    ) -> None:
        # Nothing to prepare for an in-process dict.
        return None

    async def upsert(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        document_id: str,
        vectors: list[ChunkVector],
    ) -> None:
        with self._lock:
            for vector in vectors:
                self._entries[vector.chunk_id] = _MemoryEntry(
                    dense=tuple(vector.dense),
                    collection_id=collection_id,
                    document_id=document_id,
                    embedding_model=embedding_model,
                    generation_id=vector.generation_id,
                    revision_id=vector.revision_id,
                )

    async def delete_document(
        self, *, embedding_model: str, document_id: str
    ) -> None:
        with self._lock:
            doomed = [
                chunk_id
                for chunk_id, entry in self._entries.items()
                if entry.document_id == document_id
            ]
            for chunk_id in doomed:
                del self._entries[chunk_id]

    async def count_document(
        self, *, embedding_model: str, document_id: str
    ) -> int:
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if entry.embedding_model == embedding_model
                and entry.document_id == document_id
            )

    async def delete_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> None:
        with self._lock:
            for chunk_id in chunk_ids:
                entry = self._entries.get(chunk_id)
                if entry is not None and entry.embedding_model == embedding_model:
                    del self._entries[chunk_id]

    async def count_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> int:
        wanted = set(chunk_ids)
        if not wanted:
            return 0
        with self._lock:
            return sum(
                1
                for chunk_id, entry in self._entries.items()
                if chunk_id in wanted
                and entry.embedding_model == embedding_model
            )

    async def count_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> int:
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if entry.embedding_model == embedding_model
                and entry.collection_id == collection_id
                and entry.generation_id == generation_id
            )

    async def delete_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        with self._lock:
            doomed = [
                chunk_id
                for chunk_id, entry in self._entries.items()
                if entry.embedding_model == embedding_model
                and entry.collection_id == collection_id
                and entry.generation_id == generation_id
            ]
            for chunk_id in doomed:
                del self._entries[chunk_id]

    async def count_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> int:
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if entry.embedding_model == embedding_model
                and entry.collection_id == collection_id
                and entry.generation_id == generation_id
                and entry.document_id == document_id
            )

    async def delete_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> None:
        with self._lock:
            doomed = [
                chunk_id
                for chunk_id, entry in self._entries.items()
                if entry.embedding_model == embedding_model
                and entry.collection_id == collection_id
                and entry.generation_id == generation_id
                and entry.document_id == document_id
            ]
            for chunk_id in doomed:
                del self._entries[chunk_id]

    def point_ids_for_chunks(self, chunk_ids: list[str]) -> list[str]:
        return list(chunk_ids)

    async def delete_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> None:
        with self._lock:
            doomed = [
                chunk_id
                for chunk_id, entry in self._entries.items()
                if entry.collection_id == collection_id
            ]
            for chunk_id in doomed:
                del self._entries[chunk_id]

    async def count_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> int:
        with self._lock:
            return sum(
                1
                for entry in self._entries.values()
                if entry.embedding_model == embedding_model
                and entry.collection_id == collection_id
            )

    async def search(
        self,
        *,
        embedding_model: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        by_collection = {scope.collection_id: scope for scope in scopes}
        with self._lock:
            hits = [
                VectorHit(
                    chunk_id=chunk_id,
                    score=_cosine(entry.dense, query_embedding),
                )
                for chunk_id, entry in self._entries.items()
                if entry.embedding_model == embedding_model
                and _memory_entry_is_active(chunk_id, entry, by_collection)
            ]
        hits.sort(key=lambda hit: hit.score, reverse=True)
        return hits[: max(0, top_k)]

    async def hybrid_search(
        self,
        *,
        embedding_model: str,
        query_text: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        # Dense-only index: callers dispatch on ``supports_hybrid`` and
        # never reach this. Failing loud beats a silent dense fallback.
        raise NotImplementedError(
            "MemoryVectorIndex is dense-only (supports_hybrid is False)"
        )

    async def scroll_chunk_groups(
        self, *, embedding_model: str
    ) -> set[tuple[str, str]]:
        with self._lock:
            return {
                (entry.collection_id, entry.document_id)
                for entry in self._entries.values()
                if entry.embedding_model == embedding_model
            }

    async def scroll_chunk_points(
        self, *, embedding_model: str
    ) -> list[VectorPointRef]:
        with self._lock:
            return [
                VectorPointRef(
                    chunk_id=chunk_id,
                    collection_id=entry.collection_id,
                    document_id=entry.document_id,
                    generation_id=entry.generation_id,
                    revision_id=entry.revision_id,
                )
                for chunk_id, entry in self._entries.items()
                if entry.embedding_model == embedding_model
            ]


def _memory_entry_is_active(
    chunk_id: str,
    entry: _MemoryEntry,
    by_collection: dict[str, VectorSearchScope],
) -> bool:
    """Apply the same exact generation/revision contract as Qdrant."""

    scope = by_collection.get(entry.collection_id)
    if scope is None:
        return False
    if (
        entry.generation_id is None
        and entry.revision_id is None
        and chunk_id in scope.legacy_payload_chunk_ids
    ):
        return True
    if entry.generation_id != scope.generation_id:
        return False
    if entry.revision_id is None:
        return entry.document_id in scope.legacy_document_ids
    return entry.revision_id in scope.active_revision_ids
