"""In-memory knowledge store: the zero-infrastructure default.

Same philosophy as the in-memory run store: a thread-safe, in-process
implementation that keeps every knowledge feature fully functional
without a database or vector service. Contents are lost on restart —
acceptable for development and tests; the durable stores implement the
same :class:`~inqtrix.knowledge.stores.ports.KnowledgeStore` port.

The port is async (uniform with the Postgres-backed store), but this
store does no I/O: its public coroutines wrap synchronous,
lock-guarded work over in-process dicts. The work never awaits while
holding the lock, so a ``threading.RLock`` still gives correct
cross-thread safety (the synchronous research graph and the reindex
worker reach it via ``asyncio.run`` on their own threads).

Retrieval is exact cosine similarity over all candidate chunks. That
is O(chunks) per query, which is the honest and correct choice at the
document volumes an in-process store holds; approximate indexes are a
property of the later vector-store backends, not of this port.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.shares import ShareFacts
from inqtrix.knowledge.chunk_identity import deterministic_chunk_id
from inqtrix.knowledge.evidence import source_excerpt_is_verified
from inqtrix.knowledge.source_cleanup import (
    SourceCleanupPlan,
    SourceCleanupTarget,
)
from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentChunk,
    DocumentNotFound,
    DocumentRevisionReservation,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    GenerationBuildValidation,
    GenerationManifestChanged,
    GenerationValidationError,
    IndexGenerationSuperseded,
    KnowledgeCollection,
    KnowledgeDocument,
    KnowledgeDocumentRevision,
    KnowledgeError,
    KnowledgeIndexGeneration,
    ReservedDocumentRevision,
    RetrievalCandidate,
    SourceDeletionConflict,
)
from inqtrix.pagination import keyset_page
from inqtrix.source_authority import (
    MemorySourceLifecycleAuthority,
    SourceDeletionPermit,
    SourceLifecycleConflict,
    SourceScope,
)

if TYPE_CHECKING:
    from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _cosine(a: tuple[float, ...], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _document_matches_source(document: KnowledgeDocument, source_id: str) -> bool:
    """Match canonical source identity and both historical metadata spellings."""
    if document.source_id == source_id:
        return True
    legacy = document.metadata.get("fileId") or document.metadata.get("file_id")
    if not isinstance(legacy, str) or not legacy:
        return False
    return source_id in {legacy, f"asset:{legacy}"}


def _document_matches_source_scope(
    document: KnowledgeDocument,
    scope: SourceScope,
) -> bool:
    """Match source identity and its server-owned authorization scope."""

    return (
        document.source_scope_bound
        and _document_matches_source(document, scope.source_id)
        and document.source_owner_user_id == scope.owner_user_id
        and document.source_workspace_id == scope.workspace_id
    )


def _immutable_revision_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Exclude derived chunk/build diagnostics from immutable source metadata."""
    return {
        key: value
        for key, value in metadata.items()
        if not key.startswith("_chunk_") or key == "_chunk_pages"
    }


class MemoryKnowledgeStore:
    """Thread-safe in-process implementation of the knowledge store port."""

    def __init__(self) -> None:
        self._collections: dict[str, KnowledgeCollection] = {}
        self._documents: dict[str, KnowledgeDocument] = {}
        self._chunks: dict[str, list[DocumentChunk]] = {}
        self._revisions: dict[str, KnowledgeDocumentRevision] = {}
        self._generations: dict[str, KnowledgeIndexGeneration] = {}
        self._source_authority = MemorySourceLifecycleAuthority()
        self._lock = threading.RLock()
        self._resource_access_guard: Callable[..., Any] | None = None
        self._authority: MemoryAuthorityCoordinator | None = None

    @property
    def source_lifecycle_authority(self) -> MemorySourceLifecycleAuthority:
        return self._source_authority

    def bind_source_lifecycle_authority(
        self, authority: MemorySourceLifecycleAuthority
    ) -> None:
        """Share the aggregate source fence with Asset and Deletion stores."""
        self._source_authority = authority

    @contextmanager
    def _active_source_guard(
        self,
        scope: SourceScope,
        *,
        expected_epoch: int | None = None,
        create_if_missing: bool,
    ) -> Iterator[None]:
        try:
            with self._source_authority.active_write(
                scope,
                expected_epoch=expected_epoch,
                create_if_missing=create_if_missing,
            ):
                yield
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(scope.source_id) from exc

    @property
    def atomic_resource_effects(self) -> bool:
        """Whether resource writes include audit and invalidations atomically."""
        return self._authority is not None

    def bind_authority_coordinator(
        self, coordinator: "MemoryAuthorityCoordinator"
    ) -> None:
        """Join the single process-local identity/resource boundary."""
        self._authority = coordinator
        self._lock = coordinator.lock
        self._resource_access_guard = coordinator.resource_access_guard
        coordinator.register_resource(
            "knowledge_collection", self._resource_snapshot
        )

    def _resource_snapshot(self, tenant_id: str, collection_id: str):
        """Return existence and owner while the shared lock is held."""
        from inqtrix.auth.memory_authority import MemoryResourceSnapshot

        collection = self._collections.get(collection_id)
        return MemoryResourceSnapshot(
            exists=(
                collection is not None and collection.tenant_id == tenant_id
            ),
            owner_user_id=(
                collection.created_by_user_id
                if collection is not None and collection.tenant_id == tenant_id
                else None
            ),
        )

    def bind_authorization(
        self,
        *,
        resource_access_guard: Callable[..., Any],
    ) -> None:
        """Bind the identity-store lock used by shared collection writes.

        The collection lock is acquired before this guard. That order matches
        the durable resource-then-share transaction order and turns a revoke
        racing the final in-memory write into one observable outcome.
        """
        self._resource_access_guard = resource_access_guard

    @contextmanager
    def _collection_edit_guard(
        self,
        collection: KnowledgeCollection,
        *,
        actor_user_id: uuid.UUID | None,
        denied_resource_id: str,
        denied_error: type[KeyError],
        owner_only: bool = False,
    ) -> Iterator[None]:
        """Hold live edit authority across one already-locked mutation."""
        if self._resource_access_guard is None:
            if actor_user_id == collection.created_by_user_id:
                yield
                return
            raise denied_error(denied_resource_id)

        from inqtrix.execution_authority import AuthorizationRevoked

        try:
            guard = (
                self._authority.resource_access_guard
                if self._authority is not None
                else self._resource_access_guard
            )
            assert guard is not None
            kwargs = {"owner_only": owner_only} if self._authority is not None else {}
            with guard(
                tenant_id=collection.tenant_id,
                owner_user_id=collection.created_by_user_id,
                actor_user_id=actor_user_id,
                resource_type="knowledge_collection",
                resource_id=collection.id,
                minimum=SharePermission.EDIT,
                **kwargs,
            ):
                yield
        except AuthorizationRevoked as exc:
            raise denied_error(denied_resource_id) from exc

    @property
    def supports_safe_reindex(self) -> bool:
        """In-process jobs and mutations share one serialized boundary."""
        return True

    @property
    def supports_async_document_revisions(self) -> bool:
        """The in-process worker and store share the same authority lock."""
        return True

    @property
    def supports_collection_sharing(self) -> bool:
        """The in-process collection and identity stores share one process."""
        return True

    async def is_available(self) -> bool:
        """In-memory knowledge is available whenever the process is alive."""
        return True

    def _get_collection(self, collection_id: str) -> KnowledgeCollection:
        """Locked lookup shared by the public coroutine and internal callers."""
        with self._lock:
            collection = self._collections.get(collection_id)
            if collection is None:
                raise CollectionNotFound(collection_id)
            return collection

    # -- collections ------------------------------------------------------ #

    async def create_collection(
        self,
        *,
        name: str,
        embedding_model: str,
        embedding_dim: int,
        created_by_user_id: uuid.UUID | None = None,
    ) -> KnowledgeCollection:
        """Create a collection with its immutable embedding identity."""
        if embedding_dim <= 0:
            raise EmbeddingDimensionMismatch(
                f"embedding_dim must be positive, got {embedding_dim}"
            )
        with self._lock:
            guard = (
                self._authority.creation_guard(
                    tenant_id="default",
                    actor_user_id=created_by_user_id,
                )
                if self._authority is not None
                else nullcontext()
            )
            with guard:
                collection = KnowledgeCollection(
                    id=_new_id("kc"),
                    name=name,
                    embedding_model=embedding_model,
                    embedding_dim=embedding_dim,
                    created_at=time.time(),
                    created_by_user_id=created_by_user_id,
                    active_generation_id=_new_id("gen"),
                )
                self._collections[collection.id] = collection
                self._generations[collection.active_generation_id or ""] = (
                    KnowledgeIndexGeneration(
                        generation_id=collection.active_generation_id or "",
                        collection_id=collection.id,
                        build_contract_hash="initial",
                        status="active",
                        created_at=collection.created_at,
                        activated_at=collection.created_at,
                    )
                )
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=collection.tenant_id,
                        actor_user_id=created_by_user_id,
                        owner_user_id=created_by_user_id,
                        action="knowledge_collection.created",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
                return collection

    async def list_collections(self) -> list[KnowledgeCollection]:
        """Return all collections, newest first."""
        with self._lock:
            return sorted(
                self._collections.values(),
                key=lambda item: item.created_at,
                reverse=True,
            )

    async def get_collection(self, collection_id: str) -> KnowledgeCollection:
        """Return one collection or raise :class:`CollectionNotFound`."""
        return self._get_collection(collection_id)

    async def collection_share_facts(
        self, collection_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        """Owner and name of each listed collection (share layer)."""
        with self._lock:
            return {
                collection_id: ShareFacts(
                    owner_user_id=collection.created_by_user_id,
                    title=collection.name,
                )
                for collection_id in collection_ids
                if (collection := self._collections.get(collection_id)) is not None
            }

    async def delete_collection(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Delete a collection and every document/chunk inside it."""
        with self._lock:
            collection = self._collections.get(collection_id)
            if collection is None:
                raise CollectionNotFound(collection_id)
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=collection_id,
                denied_error=CollectionNotFound,
                owner_only=True,
            ):
                if collection.created_by_user_id != actor_user_id:
                    raise CollectionNotFound(collection_id)
                if self._authority is not None:
                    self._authority.revoke_deleted_resource(
                        tenant_id=collection.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_collection.deleted",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
                del self._collections[collection_id]
                doomed = [
                    document_id
                    for document_id, document in self._documents.items()
                    if document.collection_id == collection_id
                ]
                for document_id in doomed:
                    self._documents.pop(document_id, None)
                    self._chunks.pop(document_id, None)
                    self._revisions = {
                        revision_id: revision
                        for revision_id, revision in self._revisions.items()
                        if revision.document_id != document_id
                    }
                self._generations = {
                    generation_id: generation
                    for generation_id, generation in self._generations.items()
                    if generation.collection_id != collection_id
                }

    # -- documents --------------------------------------------------------- #

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
    ) -> KnowledgeDocument:
        """Store a document with its pre-chunked, pre-embedded content.

        Raises:
            CollectionNotFound: Unknown *collection_id*.
            EmbeddingDimensionMismatch: When any embedding's dimension
                contradicts the collection, or the chunk/embedding
                counts differ (both indicate a broken ingestion call,
                never something to reconcile silently).
        """
        if len(chunks) != len(embeddings):
            raise EmbeddingDimensionMismatch(
                f"chunk/embedding count mismatch: {len(chunks)} chunks vs "
                f"{len(embeddings)} embeddings"
            )
        with self._lock:
            collection = self._get_collection(collection_id)
            if source_scope is not None:
                if (
                    source_id is None
                    or source_scope.tenant_id != collection.tenant_id
                    or source_scope.source_id != source_id
                ):
                    raise SourceDeletionConflict(source_id or "")
                resolved_source_scope = source_scope
            elif source_id is not None and source_id.startswith("asset:"):
                try:
                    resolved_source_scope = self._source_authority.resolve_scope(
                        tenant_id=collection.tenant_id,
                        source_id=source_id,
                    )
                except SourceLifecycleConflict as exc:
                    raise SourceDeletionConflict(source_id) from exc
            elif source_id is not None:
                resolved_source_scope = SourceScope(
                    tenant_id=collection.tenant_id,
                    source_id=source_id,
                    owner_user_id=collection.created_by_user_id,
                    workspace_id=None,
                )
            else:
                resolved_source_scope = None
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=collection_id,
                denied_error=CollectionNotFound,
            ):
                for index, embedding in enumerate(embeddings):
                    if len(embedding) != collection.embedding_dim:
                        raise EmbeddingDimensionMismatch(
                            f"chunk {index} has dimension {len(embedding)}, "
                            f"collection {collection_id} requires "
                            f"{collection.embedding_dim} "
                            f"(model {collection.embedding_model})"
                        )
                document = KnowledgeDocument(
                    id=_new_id("kd"),
                    collection_id=collection_id,
                    title=title,
                    text=text,
                    metadata=dict(metadata),
                    chunk_count=len(chunks),
                    created_at=time.time(),
                    source_id=source_id,
                    source_owner_user_id=(
                        resolved_source_scope.owner_user_id
                        if resolved_source_scope is not None
                        else None
                    ),
                    source_workspace_id=(
                        resolved_source_scope.workspace_id
                        if resolved_source_scope is not None
                        else None
                    ),
                    source_scope_bound=resolved_source_scope is not None,
                    desired_revision_id=revision_id,
                    active_revision_id=revision_id,
                    desired_sequence=1 if revision_id else 0,
                )
                self._documents[document.id] = document
                sources = source_chunks or []
                contexts = retrieval_contexts or []
                spans = source_spans or []
                pages = page_numbers or []
                self._chunks[document.id] = [
                    DocumentChunk(
                        id=deterministic_chunk_id(
                            document_id=document.id,
                            generation_id=(
                                generation_id or collection.active_generation_id
                            ),
                            revision_id=revision_id,
                            content_hash=(
                                document_content_hash
                                or hashlib.sha256(text.encode("utf-8")).hexdigest()
                            ),
                            chunk_index=index,
                        ),
                        document_id=document.id,
                        collection_id=collection_id,
                        chunk_index=index,
                        text=chunk_text,
                        embedding=tuple(embedding),
                        source_text=(
                            sources[index] if index < len(sources) else ""
                        ),
                        retrieval_context=(
                            contexts[index] if index < len(contexts) else None
                        ),
                        source_start=(
                            spans[index][0] if index < len(spans) else None
                        ),
                        source_end=(
                            spans[index][1] if index < len(spans) else None
                        ),
                        document_content_hash=document_content_hash,
                        revision_id=revision_id,
                        generation_id=(
                            generation_id or collection.active_generation_id
                        ),
                        page_number=(
                            pages[index] if index < len(pages) else None
                        ),
                    )
                    for index, (chunk_text, embedding) in enumerate(
                        zip(chunks, embeddings)
                    )
                ]
                # dataclasses.replace, NOT field-by-field reconstruction:
                # a manual copy silently drops every field added later
                # (created_by_user_id was lost exactly that way).
                self._collections[collection_id] = replace(
                    collection, document_count=collection.document_count + 1
                )
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=collection.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_document.added",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
                return document

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
        """Reserve the newest source intent without disturbing active chunks."""
        with self._lock:
            initial_collection = self._get_collection(collection_id)
        if source_scope is not None:
            if (
                source_scope.tenant_id != initial_collection.tenant_id
                or source_scope.source_id != source_id
            ):
                raise SourceDeletionConflict(source_id)
            scope = source_scope
        elif source_id.startswith("asset:"):
            try:
                scope = self._source_authority.resolve_scope(
                    tenant_id=initial_collection.tenant_id,
                    source_id=source_id,
                )
            except SourceLifecycleConflict as exc:
                raise SourceDeletionConflict(source_id) from exc
        else:
            scope = SourceScope(
                tenant_id=initial_collection.tenant_id,
                source_id=source_id,
                owner_user_id=initial_collection.created_by_user_id,
                workspace_id=None,
            )
        try:
            authority_guard = self._source_authority.active_write(
                scope,
                create_if_missing=(
                    (source_create_if_missing or not scope.is_asset)
                    and not scope.is_asset
                ),
            )
            with authority_guard as permit, self._lock:
                collection = self._get_collection(collection_id)
                with self._collection_edit_guard(
                    collection,
                    actor_user_id=actor_user_id,
                    denied_resource_id=collection_id,
                    denied_error=CollectionNotFound,
                ):
                    document = next(
                        (
                            candidate
                            for candidate in self._documents.values()
                            if candidate.collection_id == collection_id
                            and candidate.source_id == source_id
                            and candidate.source_owner_user_id
                            == scope.owner_user_id
                            and candidate.source_workspace_id
                            == scope.workspace_id
                            and candidate.lifecycle_status != "deleted"
                        ),
                        None,
                    )
                    existing_revision = next(
                        (
                            candidate
                            for candidate in self._revisions.values()
                            if candidate.collection_id == collection_id
                            and candidate.source_id == source_id
                            and _document_matches_source_scope(
                                self._documents[candidate.document_id],
                                scope,
                            )
                            and candidate.content_hash == content_hash
                            and candidate.build_contract_hash
                            == build_contract_hash
                        ),
                        None,
                    )
                    if document is None:
                        document = KnowledgeDocument(
                            id=_new_id("kd"),
                            collection_id=collection_id,
                            title="",
                            text="",
                            metadata={"source_id": source_id},
                            chunk_count=0,
                            created_at=time.time(),
                            source_id=source_id,
                            source_owner_user_id=scope.owner_user_id,
                            source_workspace_id=scope.workspace_id,
                            source_scope_bound=True,
                            desired_revision_id=(
                                existing_revision.revision_id
                                if existing_revision is not None
                                else revision_id
                            ),
                            desired_sequence=1,
                            lifecycle_status="staging",
                        )
                        sequence = 1
                    else:
                        effective_revision_id = (
                            existing_revision.revision_id
                            if existing_revision is not None
                            else revision_id
                        )
                        if document.desired_revision_id == effective_revision_id:
                            sequence = document.desired_sequence
                        else:
                            sequence = document.desired_sequence + 1
                            document = replace(
                                document,
                                desired_revision_id=effective_revision_id,
                                desired_sequence=sequence,
                                lifecycle_status=(
                                    "active"
                                    if document.active_revision_id is not None
                                    else "staging"
                                ),
                            )
                    self._documents[document.id] = document
                    effective_revision_id = document.desired_revision_id or revision_id
                    if existing_revision is None:
                        self._revisions[effective_revision_id] = (
                            KnowledgeDocumentRevision(
                                revision_id=effective_revision_id,
                                document_id=document.id,
                                collection_id=collection_id,
                                source_id=source_id,
                                content_hash=content_hash,
                                build_contract_hash=build_contract_hash,
                                title=title,
                                text=text,
                                metadata=_immutable_revision_metadata(
                                    dict(metadata or {})
                                ),
                                status="staging",
                                created_at=time.time(),
                            )
                        )
                    return DocumentRevisionReservation(
                        document_id=document.id,
                        collection_id=collection_id,
                        source_id=source_id,
                        revision_id=effective_revision_id,
                        sequence=sequence,
                        content_hash=content_hash,
                        build_contract_hash=build_contract_hash,
                        already_published=(
                            existing_revision is not None
                            and existing_revision.status == "active"
                            and document.active_revision_id
                            == existing_revision.revision_id
                        ),
                        source_scope=scope,
                        source_epoch=permit.epoch,
                    )
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(source_id) from exc

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
        """Publish only while the reservation is still the desired revision."""
        del fence_job_id, fence_attempt
        if len(chunks) != len(embeddings):
            raise EmbeddingDimensionMismatch(
                f"chunk/embedding count mismatch: {len(chunks)} chunks vs "
                f"{len(embeddings)} embeddings"
            )
        if reservation.source_scope is not None:
            scope = reservation.source_scope
        elif reservation.source_id.startswith("asset:"):
            try:
                scope = self._source_authority.resolve_scope(
                    tenant_id="default",
                    source_id=reservation.source_id,
                )
            except SourceLifecycleConflict as exc:
                raise SourceDeletionConflict(reservation.source_id) from exc
        else:
            scope = SourceScope(
                tenant_id="default",
                source_id=reservation.source_id,
                owner_user_id=actor_user_id,
                workspace_id=None,
            )
        try:
            authority_guard = self._source_authority.active_write(
                scope,
                expected_epoch=reservation.source_epoch or None,
                create_if_missing=(
                    reservation.source_scope is None and not scope.is_asset
                ),
            )
            with (
                authority_guard,
                self._lock,
                publication_guard() if publication_guard is not None else nullcontext(),
            ):
                document = self._documents.get(reservation.document_id)
                if document is None:
                    raise DocumentNotFound(reservation.document_id)
                collection = self._get_collection(reservation.collection_id)
                with self._collection_edit_guard(
                    collection,
                    actor_user_id=actor_user_id,
                    denied_resource_id=reservation.document_id,
                    denied_error=DocumentNotFound,
                ):
                    if (
                        document.collection_id != reservation.collection_id
                        or document.source_id != reservation.source_id
                        or document.source_owner_user_id
                        != scope.owner_user_id
                        or document.source_workspace_id != scope.workspace_id
                        or not document.source_scope_bound
                        or document.desired_revision_id
                        != reservation.revision_id
                        or document.desired_sequence != reservation.sequence
                    ):
                        raise DocumentRevisionSuperseded(reservation.revision_id)
                    revision = self._revisions.get(reservation.revision_id)
                    if revision is None:
                        raise DocumentRevisionSuperseded(reservation.revision_id)
                    if (
                        revision.content_hash != reservation.content_hash
                        or revision.build_contract_hash
                        != reservation.build_contract_hash
                        or revision.title != title
                        or revision.text != text
                        or revision.metadata
                        != _immutable_revision_metadata(dict(metadata))
                    ):
                        raise KnowledgeError(
                            "immutable document revision payload changed"
                        )
                    if (
                        revision.status == "active"
                        and document.active_revision_id == revision.revision_id
                    ):
                        return document
                    for index, embedding in enumerate(embeddings):
                        if len(embedding) != collection.embedding_dim:
                            raise EmbeddingDimensionMismatch(
                                f"chunk {index} has dimension {len(embedding)}, "
                                f"collection {collection.id} requires "
                                f"{collection.embedding_dim}"
                            )
                    contexts = retrieval_contexts or []
                    spans = source_spans or []
                    pages = page_numbers or []
                    target_generation = (
                        generation_id or collection.active_generation_id
                    )
                    rebuilt_chunks = [
                        DocumentChunk(
                            id=deterministic_chunk_id(
                                document_id=document.id,
                                generation_id=target_generation,
                                revision_id=reservation.revision_id,
                                content_hash=reservation.content_hash,
                                chunk_index=index,
                            ),
                            document_id=document.id,
                            collection_id=collection.id,
                            chunk_index=index,
                            text=chunk_text,
                            embedding=tuple(embedding),
                            source_text=source_chunks[index],
                            retrieval_context=(
                                contexts[index] if index < len(contexts) else None
                            ),
                            source_start=(
                                spans[index][0] if index < len(spans) else None
                            ),
                            source_end=(
                                spans[index][1] if index < len(spans) else None
                            ),
                            document_content_hash=reservation.content_hash,
                            revision_id=reservation.revision_id,
                            generation_id=target_generation,
                            page_number=(
                                pages[index] if index < len(pages) else None
                            ),
                        )
                        for index, (chunk_text, embedding) in enumerate(
                            zip(chunks, embeddings)
                        )
                    ]
                    self._chunks[document.id] = [
                        chunk
                        for chunk in self._chunks.get(document.id, [])
                        if chunk.generation_id != target_generation
                    ] + rebuilt_chunks
                    was_visible = document.lifecycle_status == "active"
                    now = time.time()
                    if (
                        document.active_revision_id is not None
                        and document.active_revision_id != reservation.revision_id
                        and document.active_revision_id in self._revisions
                    ):
                        prior = self._revisions[document.active_revision_id]
                        self._revisions[prior.revision_id] = replace(
                            prior,
                            status="superseded",
                            superseded_at=now,
                        )
                    self._revisions[revision.revision_id] = replace(
                        revision,
                        status="active",
                        activated_at=now,
                        superseded_at=None,
                    )
                    published = replace(
                        document,
                        title=title,
                        text=text,
                        metadata=dict(metadata),
                        chunk_count=len(chunks),
                        active_revision_id=reservation.revision_id,
                        lifecycle_status="active",
                    )
                    self._documents[document.id] = published
                    if not was_visible:
                        self._collections[collection.id] = replace(
                            collection,
                            document_count=collection.document_count + 1,
                        )
                    return published
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(reservation.source_id) from exc

    async def load_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> ReservedDocumentRevision:
        """Reload an immutable desired revision and mint a fresh source fence."""
        with self._lock:
            document = self._documents.get(document_id)
            revision = self._revisions.get(revision_id)
            if document is None or revision is None:
                raise DocumentRevisionSuperseded(revision_id)
            collection = self._get_collection(document.collection_id)
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=document_id,
                denied_error=DocumentNotFound,
            ):
                if (
                    document.desired_revision_id != revision_id
                    or revision.document_id != document_id
                ):
                    raise DocumentRevisionSuperseded(revision_id)
                source_id = revision.source_id or document.source_id
                sequence = document.desired_sequence
        if not source_id:
            raise DocumentRevisionSuperseded(revision_id)
        scope = SourceScope(
            tenant_id=collection.tenant_id,
            source_id=source_id,
            owner_user_id=document.source_owner_user_id,
            workspace_id=document.source_workspace_id,
        )
        try:
            with self._source_authority.active_write(
                scope,
                create_if_missing=not scope.is_asset,
            ) as permit, self._lock:
                document = self._documents.get(document_id)
                revision = self._revisions.get(revision_id)
                if (
                    document is None
                    or revision is None
                    or document.desired_revision_id != revision_id
                    or document.desired_sequence != sequence
                ):
                    raise DocumentRevisionSuperseded(revision_id)
                return ReservedDocumentRevision(
                    revision=revision,
                    reservation=DocumentRevisionReservation(
                        document_id=document_id,
                        collection_id=document.collection_id,
                        source_id=source_id,
                        revision_id=revision_id,
                        sequence=sequence,
                        content_hash=revision.content_hash,
                        build_contract_hash=revision.build_contract_hash,
                        already_published=(
                            revision.status == "active"
                            and document.active_revision_id == revision_id
                        ),
                        source_scope=scope,
                        source_epoch=permit.epoch,
                    ),
                )
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(source_id) from exc

    async def list_documents(self, collection_id: str) -> list[KnowledgeDocument]:
        """Return a collection's documents, newest first."""
        with self._lock:
            self._get_collection(collection_id)
            return sorted(
                (
                    document
                    for document in self._documents.values()
                    if document.collection_id == collection_id
                    and document.lifecycle_status == "active"
                ),
                key=lambda item: item.created_at,
                reverse=True,
            )

    async def list_documents_page(
        self,
        collection_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[KnowledgeDocument], str | None]:
        """One keyset page (newest first, ``(created_at, id)`` tiebreak)."""
        with self._lock:
            self._get_collection(collection_id)
            ordered = sorted(
                (
                    document
                    for document in self._documents.values()
                    if document.collection_id == collection_id
                    and document.lifecycle_status == "active"
                ),
                key=lambda item: (item.created_at, item.id),
                reverse=True,
            )
        return keyset_page(
            ordered,
            limit=limit,
            after=after,
            created_at_of=lambda d: d.created_at,
            id_of=lambda d: d.id,
        )

    async def get_document(self, document_id: str) -> KnowledgeDocument:
        """Return one document or raise :class:`DocumentNotFound`."""
        with self._lock:
            document = self._documents.get(document_id)
            if document is None:
                raise DocumentNotFound(document_id)
            return document

    async def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        """One document's chunks ordered by ``chunk_index`` (no vectors).

        The embedding is stripped so this read surface is byte-identical
        to the Postgres store, which never hydrates vectors here — the
        two backends must not diverge on an observable field.
        """
        with self._lock:
            document = self._documents.get(document_id)
            if document is None:
                raise DocumentNotFound(document_id)
            collection = self._get_collection(document.collection_id)
            ordered = sorted(
                (
                    chunk
                    for chunk in self._chunks.get(document_id, [])
                    if (
                        chunk.generation_id == collection.active_generation_id
                        or (
                            collection.active_generation_id is None
                            and chunk.generation_id is None
                        )
                    )
                    and (
                        chunk.revision_id == document.active_revision_id
                        or (
                            document.active_revision_id is None
                            and chunk.revision_id is None
                        )
                    )
                ),
                key=lambda chunk: chunk.chunk_index,
            )
            return [
                replace(
                    chunk,
                    embedding=(),
                    source_verified=source_excerpt_is_verified(
                        canonical_text=document.text,
                        source_text=chunk.source_text,
                        source_start=chunk.source_start,
                        source_end=chunk.source_end,
                        document_content_hash=chunk.document_content_hash,
                    ),
                )
                for chunk in ordered
            ]

    async def delete_document(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Delete one document and its chunks."""
        with self._lock:
            document = self._documents.get(document_id)
            if document is None:
                raise DocumentNotFound(document_id)
            collection = self._collections.get(document.collection_id)
            if collection is None:
                raise DocumentNotFound(document_id)
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=document_id,
                denied_error=DocumentNotFound,
            ):
                self._delete_document_locked(
                    document,
                    collection,
                    actor_user_id=actor_user_id,
                )

    async def delete_document_for_aggregate(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Converge an already-authorized durable document deletion."""

        with self._lock:
            document = self._documents.get(document_id)
            if document is None:
                raise DocumentNotFound(document_id)
            collection = self._collections.get(document.collection_id)
            if collection is None:
                raise DocumentNotFound(document_id)
            self._delete_document_locked(
                document,
                collection,
                actor_user_id=actor_user_id,
            )

    def _delete_document_locked(
        self,
        document: KnowledgeDocument,
        collection: KnowledgeCollection,
        *,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        self._documents.pop(document.id, None)
        self._chunks.pop(document.id, None)
        self._revisions = {
            revision_id: revision
            for revision_id, revision in self._revisions.items()
            if revision.document_id != document.id
        }
        self._collections[collection.id] = replace(
            collection,
            document_count=max(0, collection.document_count - 1),
        )
        if self._authority is not None:
            self._authority.append_resource_effects(
                tenant_id=collection.tenant_id,
                actor_user_id=actor_user_id,
                owner_user_id=collection.created_by_user_id,
                action="knowledge_document.deleted",
                resource_type="knowledge_collection",
                resource_id=collection.id,
                scope="knowledge_collections",
            )

    async def mark_document_deleting(self, document_id: str) -> None:
        """Hide one document while its durable aggregate deletion is pending."""

        with self._lock:
            document = self._documents.get(document_id)
            if document is None or document.lifecycle_status == "deleted":
                raise DocumentNotFound(document_id)
            self._documents[document_id] = replace(
                document, lifecycle_status="deleting"
            )

    async def restore_document_active(self, document_id: str) -> None:
        """Restore only a retained pre-destructive document tombstone."""

        with self._lock:
            document = self._documents.get(document_id)
            if document is None:
                return
            if document.lifecycle_status == "deleting":
                self._documents[document_id] = replace(
                    document, lifecycle_status="active"
                )

    async def count_document_residuals(
        self,
        *,
        document_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        """Count canonical/derived memory residue for one document."""

        del embedding_model
        with self._lock:
            return {
                "documents": int(document_id in self._documents),
                "chunks": len(self._chunks.get(document_id, ())),
                "revisions": sum(
                    1
                    for revision in self._revisions.values()
                    if revision.document_id == document_id
                ),
            }

    async def list_documents_by_source(
        self,
        source_id: str,
        *,
        collection_id: str | None = None,
    ) -> list[KnowledgeDocument]:
        with self._lock:
            return [
                document
                for document in self._documents.values()
                if document.lifecycle_status != "deleted"
                and (
                    collection_id is None
                    or document.collection_id == collection_id
                )
                and _document_matches_source(document, source_id)
            ]

    async def mark_source_deleting(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        if deletion_permit is not None:
            if deletion_permit.scope.source_id != source_id:
                raise SourceDeletionConflict(source_id)
            try:
                self._source_authority.validate_deletion(deletion_permit)
            except SourceLifecycleConflict as exc:
                raise SourceDeletionConflict(source_id) from exc
        with self._lock:
            matched = [
                document
                for document in self._documents.values()
                if document.lifecycle_status not in {"deleted", "deleting"}
                and (
                    _document_matches_source_scope(
                        document, deletion_permit.scope
                    )
                    if deletion_permit is not None
                    else _document_matches_source(document, source_id)
                )
            ]
            for document in matched:
                collection = self._get_collection(document.collection_id)
                access_guard = (
                    nullcontext()
                    if deletion_permit is not None
                    else self._collection_edit_guard(
                        collection,
                        actor_user_id=actor_user_id,
                        denied_resource_id=document.id,
                        denied_error=DocumentNotFound,
                    )
                )
                with access_guard:
                    self._documents[document.id] = replace(
                        document, lifecycle_status="deleting"
                    )
            return len(matched)

    async def prepare_source_cleanup(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        if deletion_permit.scope.source_id != source_id:
            raise SourceDeletionConflict(source_id)
        try:
            self._source_authority.validate_deletion(deletion_permit)
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(source_id) from exc
        with self._lock:
            targets = []
            for document in sorted(
                (
                    candidate
                    for candidate in self._documents.values()
                    if _document_matches_source_scope(
                        candidate, deletion_permit.scope
                    )
                ),
                key=lambda candidate: candidate.id,
            ):
                collection = self._get_collection(document.collection_id)
                chunk_ids = tuple(
                    chunk.id
                    for chunk in sorted(
                        self._chunks.get(document.id, []),
                        key=lambda chunk: chunk.id,
                    )
                )
                targets.append(
                    SourceCleanupTarget(
                        collection_id=collection.id,
                        document_id=document.id,
                        embedding_model=collection.embedding_model,
                        chunk_ids=chunk_ids,
                        point_ids=chunk_ids,
                    )
                )
        return SourceCleanupPlan(
            scope=deletion_permit.scope,
            authority_epoch=deletion_permit.epoch,
            operation_id=deletion_permit.operation_id,
            targets=tuple(targets),
        )

    async def execute_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        try:
            plan.assert_permit(deletion_permit)
            self._source_authority.validate_deletion(deletion_permit)
        except (SourceLifecycleConflict, ValueError) as exc:
            raise SourceDeletionConflict(plan.scope.source_id) from exc
        current = await self.prepare_source_cleanup(
            plan.scope.source_id,
            deletion_permit=deletion_permit,
        )
        if current.targets != plan.targets:
            raise SourceDeletionConflict(plan.scope.source_id)
        with self._lock:
            deleted = 0
            collection_counts: dict[str, int] = {}
            for target in plan.targets:
                document = self._documents.get(target.document_id)
                if document is None:
                    continue
                if not _document_matches_source_scope(document, plan.scope):
                    raise SourceDeletionConflict(plan.scope.source_id)
                self._documents.pop(target.document_id, None)
                self._chunks.pop(target.document_id, None)
                self._revisions = {
                    revision_id: revision
                    for revision_id, revision in self._revisions.items()
                    if revision.document_id != target.document_id
                }
                collection_counts[target.collection_id] = (
                    collection_counts.get(target.collection_id, 0) + 1
                )
                deleted += 1
            for collection_id, count in collection_counts.items():
                collection = self._get_collection(collection_id)
                self._collections[collection_id] = replace(
                    collection,
                    document_count=max(0, collection.document_count - count),
                )
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=collection.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_source.deleted",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
        residuals = await self.verify_source_cleanup(
            plan, deletion_permit=deletion_permit
        )
        if any(residuals.values()):
            raise KnowledgeError(f"source cleanup residuals remain: {residuals}")
        return deleted

    async def verify_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> dict[str, int]:
        try:
            plan.assert_permit(deletion_permit)
            self._source_authority.validate_deletion(deletion_permit)
        except (SourceLifecycleConflict, ValueError) as exc:
            raise SourceDeletionConflict(plan.scope.source_id) from exc
        document_ids = {target.document_id for target in plan.targets}
        chunk_ids = {
            chunk_id for target in plan.targets for chunk_id in target.chunk_ids
        }
        with self._lock:
            documents = sum(
                1
                for document_id in document_ids
                if document_id in self._documents
            )
            chunks = sum(
                1
                for items in self._chunks.values()
                for chunk in items
                if chunk.id in chunk_ids
            )
        return {"documents": documents, "chunks": chunks, "vectors": chunks}

    async def delete_source(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        if deletion_permit is not None:
            plan = cleanup_plan or await self.prepare_source_cleanup(
                source_id, deletion_permit=deletion_permit
            )
            return await self.execute_source_cleanup(
                plan,
                deletion_permit=deletion_permit,
                actor_user_id=actor_user_id,
            )
        documents = await self.list_documents_by_source(source_id)
        deleted = 0
        for document in documents:
            await self.delete_document(
                document.id, actor_user_id=actor_user_id
            )
            deleted += 1
        return deleted

    async def source_residuals(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> dict[str, int]:
        if deletion_permit is not None and cleanup_plan is not None:
            if deletion_permit.scope.source_id != source_id:
                raise SourceDeletionConflict(source_id)
            return await self.verify_source_cleanup(
                cleanup_plan, deletion_permit=deletion_permit
            )
        with self._lock:
            documents = [
                document
                for document in self._documents.values()
                if (
                    _document_matches_source_scope(
                        document, deletion_permit.scope
                    )
                    if deletion_permit is not None
                    else _document_matches_source(document, source_id)
                )
            ]
            return {
                "documents": len(documents),
                "chunks": sum(
                    len(self._chunks.get(document.id, []))
                    for document in documents
                ),
                "vectors": sum(
                    len(self._chunks.get(document.id, []))
                    for document in documents
                ),
            }

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
        """Rebuild one document's chunks/vectors in place (keep its id).

        Raises:
            DocumentNotFound: Unknown *document_id*.
            EmbeddingDimensionMismatch: When chunk/embedding counts
                differ or any vector contradicts the collection's
                dimension (both indicate a broken re-embed call, never
                something to reconcile silently).
        """
        if len(chunks) != len(embeddings):
            raise EmbeddingDimensionMismatch(
                f"chunk/embedding count mismatch: {len(chunks)} chunks vs "
                f"{len(embeddings)} embeddings"
            )
        with self._lock:
            preliminary = self._documents.get(document_id)
            if preliminary is None:
                raise DocumentNotFound(document_id)
            preliminary_collection = self._get_collection(
                preliminary.collection_id
            )
        source_guard = nullcontext()
        if preliminary.source_id is not None:
            scope = SourceScope(
                tenant_id=preliminary_collection.tenant_id,
                source_id=preliminary.source_id,
                owner_user_id=preliminary.source_owner_user_id,
                workspace_id=preliminary.source_workspace_id,
            )
            source_guard = self._active_source_guard(
                scope,
                create_if_missing=not scope.is_asset,
            )
        with source_guard, self._lock:
            document = self._documents.get(document_id)
            if document is None:
                raise DocumentNotFound(document_id)
            collection = self._get_collection(document.collection_id)
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=document_id,
                denied_error=DocumentNotFound,
            ):
                for index, embedding in enumerate(embeddings):
                    if len(embedding) != collection.embedding_dim:
                        raise EmbeddingDimensionMismatch(
                            f"chunk {index} has dimension {len(embedding)}, "
                            f"collection {document.collection_id} requires "
                            f"{collection.embedding_dim} "
                            f"(model {collection.embedding_model})"
                        )
                sources = source_chunks or []
                contexts = retrieval_contexts or []
                spans = source_spans or []
                pages = page_numbers or []
                effective_revision = revision_id or document.active_revision_id
                effective_generation = (
                    generation_id or collection.active_generation_id
                )
                staged = (
                    effective_generation is not None
                    and effective_generation != collection.active_generation_id
                )
                rebuilt_chunks = [
                    DocumentChunk(
                        id=deterministic_chunk_id(
                            document_id=document_id,
                            generation_id=effective_generation,
                            revision_id=effective_revision,
                            content_hash=(
                                document_content_hash
                                or hashlib.sha256(
                                    document.text.encode("utf-8")
                                ).hexdigest()
                            ),
                            chunk_index=index,
                        ),
                        document_id=document_id,
                        collection_id=document.collection_id,
                        chunk_index=index,
                        text=chunk_text,
                        embedding=tuple(embedding),
                        source_text=(
                            sources[index] if index < len(sources) else ""
                        ),
                        retrieval_context=(
                            contexts[index] if index < len(contexts) else None
                        ),
                        source_start=(
                            spans[index][0] if index < len(spans) else None
                        ),
                        source_end=(
                            spans[index][1] if index < len(spans) else None
                        ),
                        document_content_hash=document_content_hash,
                        revision_id=effective_revision,
                        generation_id=effective_generation,
                        page_number=(
                            pages[index] if index < len(pages) else None
                        ),
                    )
                    for index, (chunk_text, embedding) in enumerate(
                        zip(chunks, embeddings)
                    )
                ]
                self._chunks[document_id] = [
                    chunk
                    for chunk in self._chunks.get(document_id, [])
                    if chunk.generation_id != effective_generation
                ] + rebuilt_chunks
                if staged:
                    return document
                # dataclasses.replace, NOT field-by-field reconstruction:
                # a manual copy silently drops every field added later.
                updated = replace(
                    document,
                    chunk_count=len(chunks),
                    desired_revision_id=revision_id or document.desired_revision_id,
                    active_revision_id=revision_id or document.active_revision_id,
                )
                self._documents[document_id] = updated
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=collection.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_document.reindexed",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
                return updated

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
        with self._lock:
            collection = self._get_collection(collection_id)
            with self._collection_edit_guard(
                collection,
                actor_user_id=actor_user_id,
                denied_resource_id=collection_id,
                denied_error=CollectionNotFound,
            ):
                current_manifest = {
                    document.id: document.active_revision_id or ""
                    for document in self._documents.values()
                    if document.collection_id == collection_id
                    and document.lifecycle_status == "active"
                }
                expected_values = expected_manifest or {
                    document_id: current_manifest.get(document_id, "")
                    for document_id in expected_document_ids
                }
                if current_manifest != expected_values:
                    raise GenerationManifestChanged(
                        "collection manifest changed while generation was built"
                    )
                if expected_validation is None:
                    raise GenerationValidationError(
                        "generation publication requires a build validation manifest"
                    )
                if expected_validation.embedding_dim != collection.embedding_dim:
                    raise GenerationValidationError(
                        "generation embedding dimension contradicts collection"
                    )
                if {
                    document_id: item.revision_id
                    for document_id, item in expected_validation.documents.items()
                } != expected_values:
                    raise GenerationValidationError(
                        "generation validation revisions contradict source manifest"
                    )
                staged = {
                    document_id: revision_id
                    for document_id, revision_id in expected_values.items()
                    if any(
                        chunk.generation_id == generation_id
                        and (chunk.revision_id or "") == revision_id
                        for chunk in self._chunks.get(document_id, [])
                    )
                }
                if staged != expected_values:
                    missing = sorted(set(expected_values) - set(staged))
                    raise KnowledgeError(
                        f"generation {generation_id} is incomplete; missing {missing}"
                    )
                staged_chunks = {
                    document_id: sorted(
                        (
                            chunk
                            for chunk in self._chunks.get(document_id, [])
                            if chunk.generation_id == generation_id
                        ),
                        key=lambda chunk: chunk.chunk_index,
                    )
                    for document_id in expected_values
                }
                for document_id, expected_document in (
                    expected_validation.documents.items()
                ):
                    document = self._documents[document_id]
                    chunks = staged_chunks[document_id]
                    if len(chunks) != expected_document.chunk_count:
                        raise GenerationValidationError(
                            f"generation {generation_id} chunk count mismatch for "
                            f"{document_id}"
                        )
                    canonical_bytes = document.text.encode("utf-8")
                    if hashlib.sha256(canonical_bytes).hexdigest() != (
                        expected_document.content_hash
                    ):
                        raise GenerationValidationError(
                            f"generation {generation_id} source hash mismatch for "
                            f"{document_id}"
                        )
                    for chunk_index, (chunk, expected_span) in enumerate(
                        zip(chunks, expected_document.source_spans)
                    ):
                        start, end = expected_span
                        try:
                            source_slice = canonical_bytes[start:end].decode(
                                "utf-8"
                            )
                        except UnicodeDecodeError as exc:
                            raise GenerationValidationError(
                                f"generation {generation_id} has a non-UTF-8 "
                                f"source boundary for {document_id}"
                            ) from exc
                        if (
                            chunk.chunk_index != chunk_index
                            or (chunk.revision_id or "")
                            != expected_document.revision_id
                            or chunk.document_content_hash
                            != expected_document.content_hash
                            or (chunk.source_start, chunk.source_end)
                            != expected_span
                            or len(chunk.embedding) != collection.embedding_dim
                            or not 0 <= start < end <= len(canonical_bytes)
                            or source_slice != chunk.source_text
                        ):
                            raise GenerationValidationError(
                                f"generation {generation_id} source validation "
                                f"failed for {document_id} chunk {chunk_index}"
                            )
                if (
                    sum(len(chunks) for chunks in staged_chunks.values())
                    != expected_validation.point_count
                ):
                    raise GenerationValidationError(
                        f"generation {generation_id} point count mismatch"
                    )
                now = time.time()
                prior_generation = collection.active_generation_id
                existing = self._generations.get(generation_id)
                if (
                    existing is None
                    or existing.build_contract_hash != build_contract_hash
                    or existing.status != "building"
                ):
                    raise GenerationValidationError(
                        "generation ledger contradicts the active build contract"
                    )
                published = replace(
                    collection, active_generation_id=generation_id
                )
                for document_id, chunks in staged_chunks.items():
                    self._documents[document_id] = replace(
                        self._documents[document_id],
                        chunk_count=len(chunks),
                    )
                self._collections[collection_id] = published
                if prior_generation and prior_generation in self._generations:
                    prior = self._generations[prior_generation]
                    self._generations[prior_generation] = replace(
                        prior,
                        status="rollback_available",
                        superseded_at=now,
                        rollback_until=now + rollback_retention_seconds,
                    )
                self._generations[generation_id] = KnowledgeIndexGeneration(
                    generation_id=generation_id,
                    collection_id=collection_id,
                    build_contract_hash=(
                        build_contract_hash
                        or (existing.build_contract_hash if existing else "")
                    ),
                    status="active",
                    manifest=dict(expected_values),
                    validation=expected_validation.as_dict(),
                    created_at=existing.created_at if existing else now,
                    activated_at=now,
                )
                if self._authority is not None:
                    self._authority.append_resource_effects(
                        tenant_id=collection.tenant_id,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_generation.activated",
                        resource_type="knowledge_collection",
                        resource_id=collection_id,
                        scope="knowledge_collections",
                    )
                return published

    async def begin_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeIndexGeneration:
        with self._lock:
            self._get_collection(collection_id)
            existing = self._generations.get(generation_id)
            if existing is not None:
                if (
                    existing.collection_id != collection_id
                    or existing.build_contract_hash != build_contract_hash
                ):
                    raise IndexGenerationSuperseded(generation_id)
                return existing
            generation = KnowledgeIndexGeneration(
                generation_id=generation_id,
                collection_id=collection_id,
                build_contract_hash=build_contract_hash,
                manifest=dict(manifest),
                created_at=time.time(),
            )
            self._generations[generation_id] = generation
            return generation

    async def remove_document_from_generation(
        self,
        *,
        collection_id: str,
        document_id: str,
        generation_id: str,
    ) -> int:
        with self._lock:
            chunks = self._chunks.get(document_id, [])
            removed = sum(
                chunk.generation_id == generation_id for chunk in chunks
            )
            self._chunks[document_id] = [
                chunk for chunk in chunks if chunk.generation_id != generation_id
            ]
            return int(removed)

    async def reset_generation_for_raw_choice(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
    ) -> int:
        with self._lock:
            collection = self._get_collection(collection_id)
            if collection.active_generation_id == generation_id:
                raise KnowledgeError("active generation cannot be reset")
            generation = self._generations.get(generation_id)
            if generation is None or generation.status != "building":
                raise KnowledgeError("only an unpublished generation can be reset")
            removed = 0
            for document_id, chunks in list(self._chunks.items()):
                kept = [
                    chunk
                    for chunk in chunks
                    if chunk.generation_id != generation_id
                ]
                removed += len(chunks) - len(kept)
                self._chunks[document_id] = kept
            self._generations[generation_id] = replace(
                generation,
                build_contract_hash=build_contract_hash,
                manifest=dict(manifest),
                validation={"raw_by_user_choice": True},
            )
            return removed

    async def rollback_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        actor_user_id: uuid.UUID | None = None,
        rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> KnowledgeCollection:
        with self._lock:
            collection = self._get_collection(collection_id)
            target = self._generations.get(generation_id)
            now = time.time()
            if (
                target is None
                or target.collection_id != collection_id
                or target.status != "rollback_available"
                or target.rollback_until is None
                or target.rollback_until < now
            ):
                raise KnowledgeError("generation is not rollback-available")
            current_manifest = {
                document.id: document.active_revision_id or ""
                for document in self._documents.values()
                if document.collection_id == collection_id
                and document.lifecycle_status == "active"
            }
            if current_manifest != target.manifest:
                raise GenerationManifestChanged(
                    "source revisions changed since this generation was active"
                )
            current = self._generations.get(collection.active_generation_id or "")
            if current is not None:
                self._generations[current.generation_id] = replace(
                    current,
                    status="rollback_available",
                    superseded_at=now,
                    rollback_until=now + rollback_retention_seconds,
                )
            self._generations[generation_id] = replace(
                target,
                status="active",
                activated_at=now,
                superseded_at=None,
                rollback_until=None,
            )
            published = replace(collection, active_generation_id=generation_id)
            self._collections[collection_id] = published
            return published

    async def prune_expired_generations(
        self,
        *,
        collection_id: str,
        now: float | None = None,
    ) -> int:
        cutoff = time.time() if now is None else now
        with self._lock:
            expired = {
                generation_id
                for generation_id, generation in self._generations.items()
                if generation.collection_id == collection_id
                and (
                    generation.status in {"deleting", "cleanup_failed"}
                    or (
                        generation.status == "rollback_available"
                        and generation.rollback_until is not None
                        and generation.rollback_until <= cutoff
                    )
                )
                and self._collections[collection_id].active_generation_id
                != generation_id
            }
            for generation_id in expired:
                generation = self._generations[generation_id]
                self._generations[generation_id] = replace(
                    generation,
                    status="deleting",
                    validation={
                        **generation.validation,
                        "cleanup_started_at": cutoff,
                    },
                )
            removed = 0
            for document_id, chunks in list(self._chunks.items()):
                kept = [
                    chunk
                    for chunk in chunks
                    if chunk.generation_id not in expired
                ]
                removed += len(chunks) - len(kept)
                self._chunks[document_id] = kept
            for generation_id in expired:
                generation = self._generations[generation_id]
                self._generations[generation_id] = replace(
                    generation,
                    status="deleted",
                    validation={
                        **generation.validation,
                        "cleanup_completed_at": cutoff,
                    },
                )
            return removed

    async def generation_cleanup_collection_ids(
        self,
        *,
        now: float | None = None,
    ) -> list[str]:
        cutoff = time.time() if now is None else now
        with self._lock:
            return sorted(
                {
                    generation.collection_id
                    for generation in self._generations.values()
                    if self._collections[
                        generation.collection_id
                    ].active_generation_id
                    != generation.generation_id
                    and (
                        generation.status in {"deleting", "cleanup_failed"}
                        or (
                            generation.status == "rollback_available"
                            and generation.rollback_until is not None
                            and generation.rollback_until <= cutoff
                        )
                    )
                }
            )

    async def discard_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        with self._lock:
            collection = self._get_collection(collection_id)
            if collection.active_generation_id == generation_id:
                raise KnowledgeError("the active generation cannot be discarded")
            generation = self._generations.get(generation_id)
            if generation is not None and generation.status != "building":
                raise KnowledgeError("only an unpublished generation can be discarded")
            removed = 0
            for document_id, chunks in list(self._chunks.items()):
                if (
                    self._documents.get(document_id) is None
                    or self._documents[document_id].collection_id != collection_id
                ):
                    continue
                kept = [
                    chunk
                    for chunk in chunks
                    if chunk.generation_id != generation_id
                ]
                removed += len(chunks) - len(kept)
                self._chunks[document_id] = kept
            if generation is not None:
                self._generations[generation_id] = replace(
                    generation, status="deleted"
                )
            return removed

    # -- retrieval ----------------------------------------------------------- #

    async def search(
        self,
        *,
        query_embedding: list[float],
        collection_ids: list[str] | None,
        top_k: int,
        embedding_model: str | None = None,
    ) -> list[RetrievalCandidate]:
        """Exact cosine search over the scoped collections.

        Args:
            query_embedding: The embedded query vector.
            collection_ids: Collections to search; ``None`` searches
                every collection WHOSE DIMENSION MATCHES the query
                vector (collections embedded with a different model
                are skipped — comparing vectors across models would be
                meaningless, and an explicit id with a mismatching
                dimension raises instead).
            top_k: Maximum number of candidates to return.

        Raises:
            CollectionNotFound: An explicitly requested collection id
                is unknown.
            EmbeddingDimensionMismatch: An explicitly requested
                collection has a different embedding dimension than
                the query vector.
        """
        with self._lock:
            if collection_ids is None:
                scoped = [
                    collection
                    for collection in self._collections.values()
                    if collection.embedding_dim == len(query_embedding)
                ]
            else:
                scoped = []
                for collection_id in collection_ids:
                    collection = self._get_collection(collection_id)
                    if collection.embedding_dim != len(query_embedding):
                        raise EmbeddingDimensionMismatch(
                            f"query embedding has dimension "
                            f"{len(query_embedding)}, collection "
                            f"{collection_id} requires {collection.embedding_dim}"
                        )
                    scoped.append(collection)
            scoped_ids = {collection.id for collection in scoped}

            candidates: list[RetrievalCandidate] = []
            for document_id, chunks in self._chunks.items():
                document = self._documents.get(document_id)
                if (
                    document is None
                    or document.collection_id not in scoped_ids
                    or document.lifecycle_status != "active"
                ):
                    continue
                active_collection = self._collections[document.collection_id]
                for chunk in chunks:
                    if not (
                        chunk.generation_id
                        == active_collection.active_generation_id
                        or (
                            active_collection.active_generation_id is None
                            and chunk.generation_id is None
                        )
                    ):
                        continue
                    if not (
                        chunk.revision_id == document.active_revision_id
                        or (
                            document.active_revision_id is None
                            and chunk.revision_id is None
                        )
                    ):
                        continue
                    verified_chunk = replace(
                        chunk,
                        source_verified=source_excerpt_is_verified(
                            canonical_text=document.text,
                            source_text=chunk.source_text,
                            source_start=chunk.source_start,
                            source_end=chunk.source_end,
                            document_content_hash=chunk.document_content_hash,
                        ),
                    )
                    if not verified_chunk.source_verified:
                        continue
                    candidates.append(
                        RetrievalCandidate(
                            chunk=verified_chunk,
                            score=_cosine(chunk.embedding, query_embedding),
                            document_title=document.title,
                        )
                    )
            candidates.sort(key=lambda item: item.score, reverse=True)
            # One document per content hash, mirroring the Postgres store.
            # Collapsed AFTER the sort, so the strongest hit wins here too.
            # Document-level on purpose: two DIFFERENT documents quoting the
            # same clause both stay visible.
            seen_content_hashes: dict[str, str] = {}
            collapsed: list[RetrievalCandidate] = []
            for candidate in candidates:
                content_hash = candidate.chunk.document_content_hash
                if content_hash:
                    first = seen_content_hashes.get(content_hash)
                    if first is None:
                        seen_content_hashes[content_hash] = candidate.chunk.document_id
                    elif first != candidate.chunk.document_id:
                        continue
                collapsed.append(candidate)
            return collapsed[: max(0, top_k)]
