"""Contracts of the knowledge engine (Baukasten ports).

The knowledge engine deliberately does NOT extend ``ProviderContext``
or ``LLMProvider``: embeddings, vector search, and document storage
are different capabilities with different lifecycles, so they get
their own ports bundled into :class:`KnowledgeProviderContext` — a
sibling of the web provider bundle, wired by the composition root.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from inqtrix.source_authority import SourceDeletionPermit, SourceScope
from inqtrix.knowledge.source_cleanup import SourceCleanupPlan

if TYPE_CHECKING:
    from collections.abc import Sequence

    from inqtrix.auth.shares import ShareFacts
    from inqtrix.knowledge.contextualize import ChunkContextualizer
    from inqtrix.providers.rerankers import RerankerProvider
    from inqtrix.providers.embeddings import EmbeddingProvider


class KnowledgeError(RuntimeError):
    """Base error for knowledge-store failures."""


class VectorStoreUnavailable(KnowledgeError):
    """The vector store did not respond (connection refused, transport error,
    timeout). Raised by the store adapters so callers can name the outage —
    a run must fail loudly at the tool, never fall back to another source."""


class CollectionNotFound(KeyError):
    """Raised when a collection id is unknown to the store."""


class CollectionMaintenanceActive(KnowledgeError):
    """Raised when an active reindex owns a collection's mutation boundary."""


class DocumentNotFound(KeyError):
    """Raised when a document id is unknown to the store."""


class DocumentRevisionSuperseded(KnowledgeError):
    """Raised when a newer source revision won before publication.

    The caller must not retry publication under the stale reservation.  The
    logical document id remains stable and the newer request owns the desired
    revision pointer.
    """


class IndexGenerationSuperseded(KnowledgeError):
    """A reclaimed or cancelled indexing attempt lost publication authority."""


class GenerationManifestChanged(KnowledgeError):
    """Source revisions changed while a shadow generation was being built."""


class GenerationValidationError(KnowledgeError):
    """A shadow generation contradicts its persisted build manifest."""


class GenerationPruneError(KnowledgeError):
    """One or more expired generations could not be cleaned completely."""

    def __init__(self, generation_ids: list[str]) -> None:
        self.generation_ids = tuple(generation_ids)
        super().__init__(
            "generation cleanup failed for " + ", ".join(self.generation_ids)
        )


class SourceDeletionConflict(KnowledgeError):
    """A retained deletion receipt forbids recreating this stable source."""


class EmbeddingDimensionMismatch(KnowledgeError):
    """Raised when a vector's dimension contradicts its collection.

    A collection's embedding model and dimension are immutable after
    creation; a mismatch means the wrong model produced the vector.
    Padded or truncated vectors would silently corrupt retrieval
    quality, so the mismatch is always a hard, visible failure.
    """


@dataclass(frozen=True)
class KnowledgeCollection:
    """One logical document collection with a fixed embedding model.

    Attributes:
        id: Server-assigned stable identifier (``kc_...``).
        name: Operator/user-facing label.
        embedding_model: The embedding model id every chunk in this
            collection was embedded with. Immutable after creation.
        embedding_dim: Vector dimension of ``embedding_model``. Set at
            creation (from the embedding catalog or the first embed
            call) and enforced on every subsequent upsert.
        created_at: Unix timestamp of creation.
        document_count: Number of live documents in the collection.
        tenant_id: Tenant scope carried from day one (v1 runs one
            tenant per deployment).
        created_by_user_id: Canonical UUID of the creator. ``None`` is
            reserved for collections created in anonymous/static modes.
    """

    id: str
    name: str
    embedding_model: str
    embedding_dim: int
    created_at: float
    document_count: int = 0
    tenant_id: str = "default"
    created_by_user_id: uuid.UUID | None = None
    active_generation_id: str | None = None


@dataclass(frozen=True)
class KnowledgeDocument:
    """One ingested document inside a collection.

    Attributes:
        id: Server-assigned stable identifier (``kd_...``).
        collection_id: Owning collection.
        title: Display title used in citations and references.
        text: Full extracted text as provided by the client.
        metadata: Free-form client metadata (source filename, page
            counts, tags). Stored verbatim, never interpreted.
        chunk_count: Number of chunks the document was split into.
        created_at: Unix timestamp of ingestion.
    """

    id: str
    collection_id: str
    title: str
    text: str = field(repr=False, default="")
    metadata: dict[str, Any] = field(default_factory=dict)
    chunk_count: int = 0
    created_at: float = 0.0
    source_id: str | None = None
    # Server-minted source authority.  These fields are deliberately
    # separate from free-form metadata: client metadata may help identify a
    # source, but it can never establish the owner/workspace that deletion is
    # allowed to mutate.
    source_owner_user_id: uuid.UUID | None = None
    source_workspace_id: str | None = None
    source_scope_bound: bool = False
    desired_revision_id: str | None = None
    active_revision_id: str | None = None
    desired_sequence: int = 0
    lifecycle_status: str = "active"


@dataclass(frozen=True)
class KnowledgeDocumentRevision:
    """Immutable source plus build-contract identity for one document."""

    revision_id: str
    document_id: str
    collection_id: str
    source_id: str | None
    content_hash: str
    build_contract_hash: str
    title: str
    text: str = field(repr=False)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "staging"
    created_at: float = 0.0
    activated_at: float | None = None
    superseded_at: float | None = None


@dataclass(frozen=True)
class KnowledgeIndexGeneration:
    """One physical collection generation and its rollback manifest."""

    generation_id: str
    collection_id: str
    build_contract_hash: str
    status: str = "building"
    manifest: dict[str, str] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    activated_at: float | None = None
    superseded_at: float | None = None
    rollback_until: float | None = None


@dataclass(frozen=True)
class GenerationDocumentValidation:
    """Expected canonical rows for one document in a shadow generation."""

    revision_id: str
    content_hash: str
    source_spans: tuple[tuple[int, int], ...]

    @property
    def chunk_count(self) -> int:
        return len(self.source_spans)

    def as_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "content_hash": self.content_hash,
            "chunk_count": self.chunk_count,
            "source_spans": [list(span) for span in self.source_spans],
        }


@dataclass(frozen=True)
class GenerationBuildValidation:
    """Independent publication contract derived from canonical source text."""

    embedding_dim: int
    documents: dict[str, GenerationDocumentValidation]

    @property
    def document_count(self) -> int:
        return len(self.documents)

    @property
    def chunk_count(self) -> int:
        return sum(item.chunk_count for item in self.documents.values())

    @property
    def point_count(self) -> int:
        return self.chunk_count

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "document_count": self.document_count,
            "chunk_count": self.chunk_count,
            "point_count": self.point_count,
            "embedding_dim": self.embedding_dim,
            "documents": {
                document_id: item.as_dict()
                for document_id, item in sorted(self.documents.items())
            },
        }


@dataclass(frozen=True)
class DocumentRevisionReservation:
    """Fencing token for one requested revision of a stable source document."""

    document_id: str
    collection_id: str
    source_id: str
    revision_id: str
    sequence: int
    content_hash: str
    build_contract_hash: str = ""
    already_published: bool = False
    source_scope: SourceScope | None = None
    source_epoch: int = 0


@dataclass(frozen=True)
class ReservedDocumentRevision:
    """Canonical immutable input plus its current publication fence.

    Workers reload this value for every attempt.  The store fails with
    :class:`DocumentRevisionSuperseded` when the revision is no longer the
    desired source intent, so a restarted worker cannot revive stale text.
    """

    revision: KnowledgeDocumentRevision
    reservation: DocumentRevisionReservation


@dataclass(frozen=True)
class DocumentChunk:
    """One embedded retrieval unit of a document.

    Attributes:
        id: Stable chunk identifier (``kch_...``).
        document_id: Owning document.
        collection_id: Owning collection (denormalized for scoping).
        chunk_index: Zero-based position within the document.
        text: Internal retrieval text handed to the embedding model and the
            keyword index. May carry an ingestion-time contextualization
            prefix, so it is a RETRIEVAL artifact — the answer prompt and
            every reader-facing excerpt use ``source_text`` instead.
        embedding: Dense vector for the chunk. Dimension must equal
            the collection's ``embedding_dim``.
        source_text: The chunk's ORIGINAL document text without any
            synthetic prefix — the corpus quote verification runs
            against (a quote must exist in the cited source, not in
            machine-generated scaffolding). Empty for chunks ingested
            before the field existed. Reader-facing consumers must fail
            closed; falling back to retrieval ``text`` is forbidden.
        retrieval_context: Optional generated retrieval context, separate from
            the source even though legacy storage may also carry the composed
            internal retrieval text in ``text``.
        source_start/source_end: UTF-8 byte offsets into the canonical document.
        document_content_hash: SHA-256 identity of that canonical document.
        revision_id/generation_id: Active logical revision and physical index
            generation that produced this hit. Empty only for legacy rows.
        page_number: Best-effort 1-based source page this chunk maps to
            (PDFs only), captured at ingest by overlapping the chunk text
            against per-page text. ``None`` when the source carries no
            page concept, the mapping was inconclusive, or the chunk was
            ingested before the field existed — never a guessed value
            (No Silent Fallbacks). Enables a page-level "open PDF at page
            N" jump; NOT an exact bounding box.
    """

    id: str
    document_id: str
    collection_id: str
    chunk_index: int
    text: str
    embedding: tuple[float, ...] = field(repr=False, default=())
    source_text: str = ""
    page_number: int | None = None
    retrieval_context: str | None = None
    source_start: int | None = None
    source_end: int | None = None
    document_content_hash: str | None = None
    revision_id: str | None = None
    generation_id: str | None = None
    source_verified: bool = False


@dataclass(frozen=True)
class RetrievalCandidate:
    """One scored retrieval hit.

    Attributes:
        chunk: The matched chunk.
        score: Similarity score (cosine, higher is better).
        document_title: Title of the owning document, resolved by the
            store so consumers need no second lookup.
    """

    chunk: DocumentChunk
    score: float
    document_title: str


@dataclass(frozen=True)
class RetrievalDegradation:
    """A bounded retrieval-stage degradation with an explicit final outcome.

    Genuine corpus exhaustion is not a degradation. A vector store emits this
    record only when a technical candidate boundary or stalled backend page
    stops canonical hydration before the requested *candidate pool* is full.
    The shared retrieval/service projection then records the independent final
    evidence target and result. This distinction matters when a reranker asks
    for forty candidates but can still return all four final evidence hits from
    a smaller pool.

    ``requested_top_k`` and ``returned_hits`` remain the compatibility fields
    consumed by existing clients. They deliberately describe the final
    evidence outcome; the candidate-stage counters have unambiguous names.
    """

    reason: str
    retrieval_mode: str
    requested_top_k: int
    returned_hits: int
    candidate_cap: int | None = None
    stage: str = "vector_candidate_pool"
    requested_candidate_pool: int | None = None
    returned_candidate_pool: int | None = None
    final_top_k: int | None = None

    def __post_init__(self) -> None:
        final_top_k = (
            self.requested_top_k
            if self.final_top_k is None
            else self.final_top_k
        )
        requested_pool = (
            self.requested_top_k
            if self.requested_candidate_pool is None
            else self.requested_candidate_pool
        )
        returned_pool = (
            self.returned_hits
            if self.returned_candidate_pool is None
            else self.returned_candidate_pool
        )
        for field_name, value in (
            ("requested_top_k", self.requested_top_k),
            ("returned_hits", self.returned_hits),
            ("requested_candidate_pool", requested_pool),
            ("returned_candidate_pool", returned_pool),
            ("final_top_k", final_top_k),
        ):
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        object.__setattr__(self, "requested_top_k", int(final_top_k))
        object.__setattr__(self, "returned_hits", int(self.returned_hits))
        object.__setattr__(self, "requested_candidate_pool", int(requested_pool))
        object.__setattr__(self, "returned_candidate_pool", int(returned_pool))
        object.__setattr__(self, "final_top_k", int(final_top_k))

    @property
    def final_evidence_complete(self) -> bool:
        """Whether the technical pool boundary still filled final evidence."""

        return self.returned_hits >= (self.final_top_k or 0)

    def with_final_result(
        self,
        *,
        final_top_k: int,
        returned_hits: int,
    ) -> "RetrievalDegradation":
        """Project one candidate-stage record onto the final result width."""

        return RetrievalDegradation(
            reason=self.reason,
            retrieval_mode=self.retrieval_mode,
            requested_top_k=final_top_k,
            returned_hits=returned_hits,
            candidate_cap=self.candidate_cap,
            stage=self.stage,
            requested_candidate_pool=self.requested_candidate_pool,
            returned_candidate_pool=self.returned_candidate_pool,
            final_top_k=final_top_k,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "retrieval_mode": self.retrieval_mode,
            "stage": self.stage,
            "requested_candidate_pool": self.requested_candidate_pool,
            "returned_candidate_pool": self.returned_candidate_pool,
            "final_top_k": self.final_top_k,
            "final_evidence_complete": self.final_evidence_complete,
            "requested_top_k": self.requested_top_k,
            "returned_hits": self.returned_hits,
            "candidate_cap": self.candidate_cap,
        }


class RetrievalCandidateBatch(list[RetrievalCandidate]):
    """List-compatible candidates carrying bounded retrieval diagnostics."""

    def __init__(
        self,
        candidates=(),
        *,
        degradations: tuple[RetrievalDegradation, ...] = (),
        exclusions: tuple["RetrievalExclusion", ...] = (),
    ) -> None:
        super().__init__(candidates)
        self.degradations = tuple(degradations)
        self.exclusions = tuple(exclusions)

    def __getitem__(self, index):
        value = super().__getitem__(index)
        if isinstance(index, slice):
            return RetrievalCandidateBatch(
                value,
                degradations=self.degradations,
                exclusions=self.exclusions,
            )
        return value


@dataclass(frozen=True)
class RetrievalExclusion:
    """Text-free account of candidates rejected by canonical hydration.

    Vector-backed stores may have to reject a ranked point before a
    :class:`RetrievalCandidate` can be constructed, for example when its
    original source span cannot be verified.  Carrying only the surviving
    candidates made that safety decision invisible to the shared service.
    This bounded aggregate deliberately contains no chunk text or identifiers;
    it is safe to project to an Agent or UI warning.
    """

    reason: str
    stage: str
    count: int
    recommended_action: str | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("retrieval exclusion reason is required")
        if not self.stage.strip():
            raise ValueError("retrieval exclusion stage is required")
        if self.count < 1:
            raise ValueError("retrieval exclusion count must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "stage": self.stage,
            "count": self.count,
            "recommended_action": self.recommended_action,
        }


@runtime_checkable
class KnowledgeStore(Protocol):
    """Persistence and retrieval port for collections/documents/chunks.

    The first implementation is in-memory
    (:class:`~inqtrix.knowledge.stores.memory.MemoryKnowledgeStore`);
    Postgres-canonical and Qdrant-backed implementations are
    drop-ins. **All methods are async**: the platform persistence layer
    is async end-to-end (asyncpg), and a uniform async port lets the
    HTTP routes ``await`` directly while the synchronous research graph
    and the reindex worker bridge through ``asyncio.run`` in their own
    threads (the established sync-bridge pattern).
    """

    async def create_collection(
        self,
        *,
        name: str,
        embedding_model: str,
        embedding_dim: int,
        created_by_user_id: uuid.UUID | None = None,
    ) -> KnowledgeCollection: ...

    async def list_collections(self) -> list[KnowledgeCollection]: ...

    @property
    def supports_safe_reindex(self) -> bool:
        """Whether background reindex can serialize against mutations."""
        ...

    @property
    def supports_async_document_revisions(self) -> bool:
        """Whether revision CAS authority survives every configured worker."""
        ...

    @property
    def supports_collection_sharing(self) -> bool:
        """Whether collection metadata and share writes have a durable fence.

        A vector-only store cannot make resource ownership, edits, and direct
        share lifecycle one coherent security boundary. Such deployments must
        expose collection sharing as unsupported instead of relying on
        process-local coordination.
        """
        ...

    async def get_collection(self, collection_id: str) -> KnowledgeCollection: ...

    async def collection_share_facts(
        self, collection_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        """Owner and name of each listed collection, in one read and
        regardless of visibility (share layer); unknown ids are absent. Only
        a store that supports collection sharing is asked."""
        ...

    async def delete_collection(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None: ...

    async def add_document(
        self,
        *,
        collection_id: str,
        title: str,
        text: str,
        metadata: dict[str, Any],
        chunks: list[str],
        embeddings: list[list[float]],
        source_id: str | None = None,
        source_chunks: list[str] | None = None,
        retrieval_contexts: list[str | None] | None = None,
        source_spans: list[tuple[int, int]] | None = None,
        document_content_hash: str | None = None,
        revision_id: str | None = None,
        generation_id: str | None = None,
        page_numbers: list[int | None] | None = None,
        source_scope: SourceScope | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeDocument: ...

    async def reserve_document_revision(
        self,
        *,
        collection_id: str,
        source_id: str,
        revision_id: str,
        content_hash: str,
        build_contract_hash: str = "",
        title: str = "",
        text: str = "",
        metadata: dict[str, Any] | None = None,
        source_scope: SourceScope | None = None,
        source_create_if_missing: bool = False,
        actor_user_id: uuid.UUID | None = None,
    ) -> DocumentRevisionReservation:
        """Make *revision_id* the newest intent for a stable source."""
        ...

    async def publish_document_revision(
        self,
        *,
        reservation: DocumentRevisionReservation,
        title: str,
        text: str,
        metadata: dict[str, Any],
        chunks: list[str],
        embeddings: list[list[float]],
        source_chunks: list[str],
        retrieval_contexts: list[str | None],
        source_spans: list[tuple[int, int]],
        page_numbers: list[int | None] | None = None,
        generation_id: str | None = None,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        publication_guard: Callable[[], Any] | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeDocument:
        """CAS-publish a fully built revision, preserving the prior active one.

        Durable workers pass their job id and claim attempt. Stores that share
        the indexing ledger's transaction boundary must lock and validate that
        fence, including a non-cancelled running status and the exact
        document/revision identity, in the same transaction that activates the
        revision pointer.
        """
        ...

    async def load_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> ReservedDocumentRevision:
        """Reload immutable revision text and its current source/CAS fence."""
        ...

    async def list_documents(self, collection_id: str) -> list[KnowledgeDocument]: ...

    async def prepare_source_cleanup(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        """Capture the exact canonical/vector identifiers before deletion."""
        ...

    async def execute_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Delete and verify exactly one server-minted cleanup plan."""
        ...

    async def list_documents_page(
        self,
        collection_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[KnowledgeDocument], str | None]:
        """One keyset page of a collection's documents (newest first).

        Separate from :meth:`list_documents` (which returns ALL documents —
        the reindex worker re-embeds the whole collection): this is the
        bounded HTTP-listing path. Returns the page and the ``next_cursor``
        (``None`` on the last page). Ordering is ``(created_at, id)``
        descending; the id is the tiebreaker for the float-epoch
        ``created_at``."""
        ...

    async def get_document(self, document_id: str) -> KnowledgeDocument: ...

    async def get_chunks(self, document_id: str) -> list["DocumentChunk"]:
        """One document's chunks ordered by ``chunk_index`` (no vectors).

        The read side of chunk identity: the citable-provenance surface
        (a single chunk plus its neighbours) and the reindex
        id-stability contract are both verified against this. The
        ``embedding`` field is NOT hydrated (both backends return it
        empty) — this is a provenance read, not a vector read. Raises
        :class:`DocumentNotFound` for an unknown document; an existing
        document with no chunks yet returns an empty list.
        """
        ...

    async def delete_document(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None: ...

    async def delete_document_for_aggregate(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Delete after a durable operation has already fixed authority.

        This internal primitive must not re-evaluate a direct share that may
        have been revoked after the operation crossed its first destructive
        checkpoint. The operation layer owns that one-time authorization and
        still supplies the initiating actor for the audit effect.
        """
        ...

    async def mark_document_deleting(self, document_id: str) -> None:
        """Detach one document from retrieval before aggregate cleanup."""
        ...

    async def restore_document_active(self, document_id: str) -> None:
        """Undo a pre-destructive tombstone after authorization revalidation fails."""
        ...

    async def list_documents_by_source(
        self,
        source_id: str,
        *,
        collection_id: str | None = None,
    ) -> list[KnowledgeDocument]:
        """Return non-deleted source matches, optionally within one collection.

        Legacy member reconciliation must supply ``collection_id`` after the
        parent collection has been authorized. Aggregate source cleanup does
        not obtain authority from this read helper; it uses a deletion permit.
        """
        ...

    async def mark_source_deleting(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Atomically detach a source from retrieval; return affected documents."""
        ...

    async def delete_source(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Idempotently remove every authorized document for *source_id*."""
        ...

    async def source_residuals(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> dict[str, int]:
        """Return canonical residual counts after source cleanup."""
        ...

    async def reembed_document(
        self,
        *,
        document_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        source_chunks: list[str] | None = None,
        retrieval_contexts: list[str | None] | None = None,
        source_spans: list[tuple[int, int]] | None = None,
        document_content_hash: str | None = None,
        revision_id: str | None = None,
        generation_id: str | None = None,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        page_numbers: list[int | None] | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeDocument:
        """Replace a document's chunks/vectors in place, keeping its id.

        The re-embed primitive behind background reindexing: the
        document's identity, title, text, metadata, and creation time
        are preserved (citations and client references stay valid),
        only the chunk set and embeddings are rebuilt. ``chunk_count``
        is updated to ``len(chunks)``.

        Optional because durable stores adopt it incrementally; the
        :class:`~inqtrix.services.indexing_service.IndexingService`
        checks for support and fails the job visibly when a store
        lacks it, never silently no-ops.

        Raises:
            DocumentNotFound: Unknown *document_id*.
            EmbeddingDimensionMismatch: A vector's dimension contradicts
                the collection, or chunk/embedding counts differ.
        """
        ...

    async def activate_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        expected_document_ids: list[str],
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        actor_user_id: uuid.UUID | None = None,
        expected_manifest: dict[str, str] | None = None,
        expected_validation: GenerationBuildValidation | None = None,
        build_contract_hash: str = "",
        rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> KnowledgeCollection:
        """Atomically publish a fully staged collection generation."""
        ...

    async def begin_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeIndexGeneration:
        """Create or validate the durable shadow-generation ledger row."""
        ...

    async def remove_document_from_generation(
        self,
        *,
        collection_id: str,
        document_id: str,
        generation_id: str,
    ) -> int:
        """Remove a snapshot member deleted before shadow publication."""
        ...

    async def reset_generation_for_raw_choice(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
    ) -> int:
        """Clear an unpublished contextual build for explicit raw rebuild."""
        ...

    async def rollback_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        actor_user_id: uuid.UUID | None = None,
        rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> KnowledgeCollection:
        """Atomically restore a retained, still-valid generation."""
        ...

    async def prune_expired_generations(
        self,
        *,
        collection_id: str,
        now: float | None = None,
    ) -> int:
        """Delete expired rollback vectors only after exact verification."""
        ...

    async def generation_cleanup_collection_ids(
        self,
        *,
        now: float | None = None,
    ) -> list[str]:
        """List tenant-scoped collections with due or interrupted cleanup."""
        ...

    async def discard_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Idempotently remove an unpublished generation's chunks/vectors."""
        ...

    async def search(
        self,
        *,
        query_embedding: list[float],
        collection_ids: list[str] | None,
        top_k: int,
        embedding_model: str | None = None,
    ) -> list[RetrievalCandidate]: ...


@runtime_checkable
class HybridKnowledgeStore(KnowledgeStore, Protocol):
    """Optional store capability: fused dense + sparse retrieval.

    Stores advertising this protocol fuse a semantic (dense) and a
    lexical (sparse/BM25) branch server-side; the service dispatches
    here automatically and ``/v1/capabilities`` reports
    ``features.hybrid_retrieval`` so the upgrade is visible, never
    silent.
    """

    @property
    def supports_hybrid(self) -> bool:
        """Whether the lexical branch is actually configured.

        ``runtime_checkable`` protocols only verify method EXISTENCE —
        this flag carries the real capability (a store may implement
        the method but run dense-only by configuration)."""
        ...

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_embedding: list[float],
        collection_ids: list[str] | None,
        top_k: int,
        embedding_model: str | None = None,
    ) -> list[RetrievalCandidate]: ...


@dataclass(frozen=True)
class KnowledgeProviderContext:
    """Bundle of knowledge capabilities wired by the composition root.

    Attributes:
        embeddings: The dense embedding provider (query + document
            embedding plus the selectable-model catalog surface).
        store: The collection/document/vector store.
        default_top_k: Default number of evidence chunks retrieved per
            question when the request does not override it.
        reranker: Optional cross-encoder re-scoring stage applied
            after retrieval. ``None`` skips the stage — visible via
            ``features.reranker`` in the capability manifest.
        rerank_candidate_depth: Candidate pool retrieved before
            reranking when a reranker is wired (the reranker reduces
            this pool to the requested top_k).
        contextualizer: Optional ingestion-time chunk
            contextualization (contextual retrieval). ``None`` ingests
            raw chunks — visible via ``features.contextual_retrieval``.
    """

    embeddings: "EmbeddingProvider"
    store: KnowledgeStore
    default_top_k: int = 8
    reranker: "RerankerProvider | None" = None
    rerank_candidate_depth: int = 40
    contextualizer: "ChunkContextualizer | None" = None
