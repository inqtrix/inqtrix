"""Postgres-canonical knowledge store (the standard RAG data split).

Collections, documents (full canonical text), and chunk metadata live
relationally in Postgres — the source of truth, queryable and joinable
for ownership/sharing and pagination. The embedding VECTORS live in a
:class:`~inqtrix.knowledge.stores.vector_index.VectorIndex` (Qdrant in
production, in-process for dev/test); original binaries live in the
object store. This store owns the canonical half and delegates vectors
to the index, hydrating retrieval hits back from Postgres.

Sync is flag-based, not an outbox: document mutations hold the collection
row lock while the vector side effect runs, then commit the canonical rows
with ``vector_synced=true``. Reindex submit takes that same lock and therefore
cannot snapshot a half-finished mutation. A vector failure propagates and
rolls back the canonical transaction. A partial first insert is an orphan the
existing reconcile removes; a failed in-place replacement remains a visible
failed reindex and is repaired by rerunning that same operation. There is no
vector duplication in Postgres and no separate sync worker.

Every operation runs inside :func:`~inqtrix.storage.db.tenant_session`
(restricted role + transaction-local tenant GUC), with explicit tenant
predicates as layer 1 and row-level security as layer 2 — identical to
the other Postgres repositories. The engine is its own NullPool engine
(loop-agnostic): this store is awaited from the HTTP loop AND bridged
from the synchronous research graph / reindex worker via
``run_coro_sync`` (a fresh per-call loop), which a pooled asyncpg
connection could not survive.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import (
    Text,
    and_,
    any_,
    bindparam,
    delete,
    func,
    insert,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from inqtrix.auth.permissions import ResourceAccess, SharePermission
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
    GenerationPruneError,
    GenerationValidationError,
    IndexGenerationSuperseded,
    KnowledgeCollection,
    KnowledgeDocument,
    KnowledgeDocumentRevision,
    KnowledgeError,
    KnowledgeIndexGeneration,
    ReservedDocumentRevision,
    RetrievalCandidate,
    RetrievalCandidateBatch,
    RetrievalExclusion,
    SourceDeletionConflict,
)
from inqtrix.knowledge.stores.retrieval_contract import (
    bounded_candidate_depth,
    degraded_candidates,
)
from inqtrix.knowledge.stores.vector_index import (
    ChunkVector,
    VectorIndex,
    VectorSearchScope,
)
from inqtrix.pagination import encode_cursor
from inqtrix.source_authority import (
    PostgresSourceLifecycleAuthority,
    SourceDeletionPermit,
    SourceLifecycleConflict,
    SourceScope,
    SourceWritePermit,
)
from inqtrix.storage.db import build_session_factory, tenant_session
from inqtrix.storage.indexing_orm import indexing_jobs
from inqtrix.storage.knowledge_orm import (
    knowledge_chunks,
    knowledge_collections,
    knowledge_document_revisions,
    knowledge_documents,
    knowledge_index_generations,
)
from inqtrix.storage.resource_access import (
    VISIBLE_SHARE_PERMISSION,
    append_resource_effects,
    listed_resource_access,
    lock_active_users,
    lock_resource_access,
    revoke_resource_shares,
    visible_resource_select,
)

log = logging.getLogger("inqtrix")

_DEFAULT_TENANT = "default"
def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _log_snapshot_advanced_retry(
    *,
    retrieval_mode: str,
    old_scopes: list[VectorSearchScope],
    new_scopes: list[VectorSearchScope],
) -> None:
    """Expose the one safe whole-query retry without logging source content."""

    old_revision_count = sum(
        len(scope.active_revision_ids) + len(scope.legacy_document_ids)
        for scope in old_scopes
    )
    new_revision_count = sum(
        len(scope.active_revision_ids) + len(scope.legacy_document_ids)
        for scope in new_scopes
    )
    log.warning(
        "knowledge %s search restarted under advanced canonical snapshot "
        "(snapshot_advanced_retry=1, old_revisions=%d, new_revisions=%d)",
        retrieval_mode,
        old_revision_count,
        new_revision_count,
        extra={
            "event": "knowledge.retrieval.snapshot_advanced",
            "retrieval_mode": retrieval_mode,
            "snapshot_advanced_retry": 1,
            "old_revision_count": old_revision_count,
            "new_revision_count": new_revision_count,
        },
    )


def _immutable_revision_metadata(metadata: dict) -> dict:
    """Exclude derived chunk/build diagnostics from immutable source metadata."""
    return {
        key: value
        for key, value in metadata.items()
        if not key.startswith("_chunk_") or key == "_chunk_pages"
    }


class PostgresKnowledgeStore:
    """Canonical knowledge store over Postgres + a vector index.

    Args:
        engine: A dedicated NullPool async engine (loop-agnostic) for the
            knowledge schema — never the shared HTTP-loop engine.
        app_role: Restricted Postgres role for the tenant sessions.
        vector_index: Where chunk vectors live (Qdrant or in-process).
    """

    def __init__(
        self,
        *,
        engine: AsyncEngine,
        app_role: str,
        vector_index: VectorIndex,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
    ) -> None:
        self._engine = engine
        self._session_factory = build_session_factory(engine)
        self._app_role = app_role
        self._vectors = vector_index
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled
        self._source_authority = PostgresSourceLifecycleAuthority()

    @property
    def source_lifecycle_authority(self) -> PostgresSourceLifecycleAuthority:
        """Return the shared durable source write/delete authority."""
        return self._source_authority

    @property
    def atomic_resource_effects(self) -> bool:
        """Audit, invalidation, and share cleanup join canonical writes."""
        return True

    @property
    def supports_safe_reindex(self) -> bool:
        """Collection-row locking serializes jobs against canonical writes."""
        return True

    @property
    def supports_async_document_revisions(self) -> bool:
        """Revision intent and CAS fences are durable Postgres rows."""
        return True

    @property
    def supports_collection_sharing(self) -> bool:
        """Canonical collection and share rows share Postgres transactions."""
        return True

    @property
    def supports_hybrid(self) -> bool:
        """Delegated to the vector index (hybrid dispatch flag)."""
        return self._vectors.supports_hybrid

    @property
    def sparse_language(self) -> str | None:
        """Delegated BM25 tokenizer language of the vector index.

        ``None`` when the index has no lexical branch (e.g. the in-process
        :class:`MemoryVectorIndex`, which carries no such property). Surfaced
        so the canonical Postgres+Qdrant path also reports the monolingual
        sparse limitation to the capability manifest and the algorithm.
        """
        return getattr(self._vectors, "sparse_language", None)

    async def is_available(self) -> bool:
        """Return whether the delegated vector index is reachable now."""
        return await self._vectors.is_available()

    def _session(self):
        return tenant_session(
            self._session_factory,
            tenant_id=_DEFAULT_TENANT,
            app_role=self._app_role,
        )

    async def aclose(self) -> None:
        """Dispose the dedicated engine at application shutdown."""
        await self._engine.dispose()

    # -- maintenance ------------------------------------------------------ #

    async def reconcile_orphans(self) -> dict[str, object]:
        """Delete vector points whose canonical Postgres chunks are gone.

        The reverse of the forward ``vector_synced`` repair (see the module
        docstring): a cross-store DELETE is non-atomic — Postgres commits, then
        the vector-index delete runs — so an interruption can strand vectors
        with no canonical chunk. This sweep scrolls each model's vector space,
        left-anti-joins the exact canonical ``knowledge_chunks`` ids for that
        model, and deletes only the missing point ids. It therefore also repairs
        stale points left behind by a failed replacement of a document that
        still exists. Idempotent; safe to run repeatedly.

        Boundary: ``models`` is derived from existing ``knowledge_collections``,
        so a physical chunk collection for a model with NO remaining collection
        is not scrolled — its vectors persist until a collection of that model
        exists again. Acceptable (it never causes over-deletion); revisit if
        models are routinely fully removed.

        Returns:
            Counts plus document- and chunk-level deletion details.  The
            document count remains backward-compatible and counts only points
            whose whole canonical document is absent.
        """
        async with self._session() as session:
            model_rows = (
                await session.execute(
                    select(knowledge_collections.c.embedding_model)
                    .where(knowledge_collections.c.tenant_id == _DEFAULT_TENANT)
                    .distinct()
                )
            ).all()
        models = {row[0] for row in model_rows}
        deleted_documents: set[tuple[str, str]] = set()
        deleted_chunks: list[dict[str, str]] = []
        for model in models:
            point_refs = await self._vectors.scroll_chunk_points(
                embedding_model=model
            )
            if not point_refs:
                continue
            point_collection_ids = sorted(
                {point.collection_id for point in point_refs}
            )
            async with self._session() as session:
                # Every canonical vector mutation holds its collection row lock
                # until the external write and the Postgres transaction finish.
                # Lock the exact collections observed in the vector snapshot
                # (plus every current collection for this model) before taking
                # the anti-join snapshot. A point from an uncommitted writer can
                # therefore never be mistaken for an orphan and deleted.
                await session.execute(
                    select(knowledge_collections.c.id)
                    .where(
                        knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                        or_(
                            knowledge_collections.c.embedding_model == model,
                            knowledge_collections.c.id.in_(point_collection_ids),
                        ),
                    )
                    .order_by(knowledge_collections.c.id)
                    .with_for_update()
                )
                canonical_ids = set(
                    (
                        await session.execute(
                            select(knowledge_chunks.c.id)
                            .select_from(
                                knowledge_chunks.join(
                                    knowledge_collections,
                                    and_(
                                        knowledge_chunks.c.collection_id
                                        == knowledge_collections.c.id,
                                        knowledge_chunks.c.tenant_id
                                        == knowledge_collections.c.tenant_id,
                                    ),
                                )
                            )
                            .where(
                                knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                                knowledge_collections.c.embedding_model == model,
                            )
                        )
                    ).scalars()
                )
                pg_doc_ids = set(
                    (
                        await session.execute(
                            select(knowledge_documents.c.id).where(
                                knowledge_documents.c.tenant_id == _DEFAULT_TENANT
                            )
                        )
                    ).scalars()
                )
                orphan_refs = [
                    point
                    for point in point_refs
                    if point.chunk_id not in canonical_ids
                ]
                if not orphan_refs:
                    continue
                orphan_chunk_ids = list(
                    dict.fromkeys(point.chunk_id for point in orphan_refs)
                )
                await self._vectors.delete_chunks(
                    embedding_model=model,
                    chunk_ids=orphan_chunk_ids,
                )
                residual = await self._vectors.count_chunks(
                    embedding_model=model,
                    chunk_ids=orphan_chunk_ids,
                )
                if residual:
                    raise KnowledgeError(
                        f"orphan reconcile left {residual} vector points"
                    )
            sample = orphan_chunk_ids[:20]
            log.warning(
                "Knowledge-Reconcile: removed %d orphan vector points by exact chunk_id"
                " (embedding_model=%s, chunk_ids=%s%s)",
                len(orphan_chunk_ids),
                model,
                sample,
                " …" if len(orphan_chunk_ids) > len(sample) else "",
            )
            for point in orphan_refs:
                deleted_chunks.append(
                    {
                        "chunk_id": point.chunk_id,
                        "document_id": point.document_id,
                        "collection_id": point.collection_id,
                        "embedding_model": model,
                    }
                )
                if point.document_id not in pg_doc_ids:
                    deleted_documents.add((point.document_id, model))
        document_details = [
            {"document_id": document_id, "embedding_model": model}
            for document_id, model in sorted(deleted_documents)
        ]
        return {
            "deleted_documents": len(document_details),
            "deleted_chunks": len(deleted_chunks),
            "details": document_details,
            "chunk_details": deleted_chunks,
        }

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
        await self._vectors.ensure_model(
            embedding_model=embedding_model, embedding_dim=embedding_dim
        )
        collection = KnowledgeCollection(
            id=_new_id("kc"),
            name=name,
            embedding_model=embedding_model,
            embedding_dim=embedding_dim,
            created_at=time.time(),
            document_count=0,
            tenant_id=_DEFAULT_TENANT,
            created_by_user_id=created_by_user_id,
            active_generation_id=_new_id("gen"),
        )
        async with self._session() as session:
            if (
                created_by_user_id is not None
                and not await lock_active_users(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    user_ids=(created_by_user_id,),
                )
            ):
                raise CollectionNotFound(collection.id)
            await session.execute(
                insert(knowledge_collections).values(
                    id=collection.id,
                    tenant_id=collection.tenant_id,
                    name=collection.name,
                    embedding_model=collection.embedding_model,
                    embedding_dim=collection.embedding_dim,
                    created_by_user_id=collection.created_by_user_id,
                    active_generation_id=collection.active_generation_id,
                    created_at=collection.created_at,
                )
            )
            await session.execute(
                insert(knowledge_index_generations).values(
                    generation_id=collection.active_generation_id,
                    tenant_id=collection.tenant_id,
                    collection_id=collection.id,
                    build_contract_hash="initial",
                    status="active",
                    manifest={},
                    validation={"empty_collection": True},
                    created_at=collection.created_at,
                    activated_at=collection.created_at,
                )
            )
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=created_by_user_id,
                owner_user_id=created_by_user_id,
                action="knowledge_collection.created",
                resource_type="knowledge_collection",
                resource_id=collection.id,
                scope="knowledge_collections",
            )
        return collection

    async def list_collections(self) -> list[KnowledgeCollection]:
        """All collections, newest first (with live document counts)."""
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(knowledge_collections)
                    .where(knowledge_collections.c.tenant_id == _DEFAULT_TENANT)
                    .order_by(knowledge_collections.c.created_at.desc())
                )
            ).all()
            counts = await self._document_counts(session)
        return [self._collection_from_row(row, counts) for row in rows]

    async def list_visible_collections(
        self, *, actor_user_id: uuid.UUID | None
    ) -> list[tuple[KnowledgeCollection, ResourceAccess]]:
        """List owned and accepted-shared collections in one live SQL query."""
        statement = visible_resource_select(
            resource_table=knowledge_collections,
            id_column=knowledge_collections.c.id,
            owner_column=knowledge_collections.c.created_by_user_id,
            resource_type="knowledge_collection",
            tenant_id=_DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        ).order_by(knowledge_collections.c.created_at.desc())
        async with self._session() as session:
            rows = (await session.execute(statement)).all()
            counts = await self._document_counts(session)
        return [
            (
                self._collection_from_row(row, counts),
                listed_resource_access(
                    owner_user_id=row.created_by_user_id,
                    actor_user_id=actor_user_id,
                    share_permission=getattr(row, VISIBLE_SHARE_PERMISSION),
                ),
            )
            for row in rows
        ]

    async def get_collection(self, collection_id: str) -> KnowledgeCollection:
        """One collection or :class:`CollectionNotFound`."""
        async with self._session() as session:
            row = await self._collection_row(session, collection_id)
            counts = await self._document_counts(session, collection_id)
        return self._collection_from_row(row, counts)

    async def collection_share_facts(
        self, collection_ids: Sequence[str]
    ) -> dict[str, ShareFacts]:
        """Owner and name of each listed collection (share layer), without
        the document counts a collection read carries."""
        wanted = sorted(set(collection_ids))
        if not wanted:
            return {}
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(
                        knowledge_collections.c.id,
                        knowledge_collections.c.created_by_user_id,
                        knowledge_collections.c.name,
                    ).where(
                        knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_collections.c.id
                        == any_(bindparam("collection_ids", wanted, type_=ARRAY(Text))),
                    )
                )
            ).all()
        return {
            row.id: ShareFacts(owner_user_id=row.created_by_user_id, title=row.name)
            for row in rows
        }

    async def delete_collection(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Delete a collection with all documents/chunks (DB cascade + vectors)."""
        async with self._session() as session:
            row, access = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
                owner_only=True,
            )
            embedding_model = row.embedding_model
            recipients = await revoke_resource_shares(
                session,
                tenant_id=_DEFAULT_TENANT,
                resource_type="knowledge_collection",
                resource_id=collection_id,
                revoked_by_user_id=actor_user_id,
            )
            await session.execute(
                delete(knowledge_collections).where(
                    knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_collections.c.id == collection_id,
                )
            )
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="knowledge_collection.deleted",
                resource_type="knowledge_collection",
                resource_id=collection_id,
                scope="knowledge_collections",
                additional_targets=recipients,
            )
            # Keep the collection row lock until the external side effect
            # finishes. Reindex submit takes the same row lock, so it cannot
            # snapshot a half-finished delete and later resurrect vectors.
            await self._vectors.delete_collection(
                embedding_model=embedding_model,
                collection_id=collection_id,
            )

    async def count_collection_residuals(
        self,
        *,
        collection_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        """Count canonical and derived residue for terminal deletion proof."""

        async with self._session() as session:
            collection_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_collections)
                    .where(
                        knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_collections.c.id == collection_id,
                    )
                )
                or 0
            )
            document_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.collection_id == collection_id,
                    )
                )
                or 0
            )
            chunk_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_chunks)
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.collection_id == collection_id,
                    )
                )
                or 0
            )
            revision_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_document_revisions)
                    .where(
                        knowledge_document_revisions.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_document_revisions.c.collection_id == collection_id,
                    )
                )
                or 0
            )
            generation_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_index_generations.c.collection_id == collection_id,
                    )
                )
                or 0
            )
        vector_count = await self._vectors.count_collection(
            embedding_model=embedding_model,
            collection_id=collection_id,
        )
        return {
            "collections": collection_count,
            "documents": document_count,
            "chunks": chunk_count,
            "revisions": revision_count,
            "generations": generation_count,
            "vectors": int(vector_count),
        }

    # -- documents --------------------------------------------------------- #

    async def add_document(
        self,
        *,
        collection_id: str,
        title: str,
        text: str,
        metadata: dict,
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
        """Store a document's canonical rows, then sync its vectors."""
        async with self._session() as session:
            collection, access = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
            )
            source_permit = await self._source_write_permit(
                session,
                source_id=source_id,
                collection_owner_user_id=collection.created_by_user_id,
                source_scope=source_scope,
            )
            self._validate_embeddings(chunks, embeddings, collection.embedding_dim)
            document_id = _new_id("kd")
            created_at = time.time()
            effective_content_hash = document_content_hash or hashlib.sha256(
                text.encode("utf-8")
            ).hexdigest()
            chunk_rows = self._build_chunk_rows(
                document_id=document_id,
                collection_id=collection_id,
                chunks=chunks,
                source_chunks=source_chunks,
                retrieval_contexts=retrieval_contexts,
                source_spans=source_spans,
                document_content_hash=effective_content_hash,
                revision_id=revision_id,
                generation_id=(generation_id or collection.active_generation_id),
                page_numbers=page_numbers,
                created_at=created_at,
            )
            await session.execute(
                insert(knowledge_documents).values(
                    id=document_id,
                    collection_id=collection_id,
                    tenant_id=_DEFAULT_TENANT,
                    title=title,
                    text=text,
                    metadata=dict(metadata),
                    source_id=source_id,
                    source_owner_user_id=(
                        source_permit.scope.owner_user_id
                        if source_permit is not None
                        else None
                    ),
                    source_workspace_id=(
                        source_permit.scope.workspace_id
                        if source_permit is not None
                        else None
                    ),
                    source_scope_bound=source_permit is not None,
                    desired_revision_id=revision_id,
                    active_revision_id=revision_id,
                    desired_sequence=1 if revision_id else 0,
                    lifecycle_status="active",
                    chunk_count=len(chunks),
                    vector_synced=False,
                    created_at=created_at,
                )
            )
            if chunk_rows:
                await session.execute(insert(knowledge_chunks), chunk_rows)
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="knowledge_document.created",
                resource_type="knowledge_collection",
                resource_id=collection_id,
                scope="knowledge_collections",
            )
            embedding_model = collection.embedding_model
            await self._sync_vectors(
                session=session,
                embedding_model=embedding_model,
                collection_id=collection_id,
                document_id=document_id,
                chunk_rows=chunk_rows,
                embeddings=embeddings,
            )
        return KnowledgeDocument(
            id=document_id,
            collection_id=collection_id,
            title=title,
            text=text,
            metadata=dict(metadata),
            chunk_count=len(chunks),
            created_at=created_at,
            source_id=source_id,
            source_owner_user_id=(
                source_permit.scope.owner_user_id
                if source_permit is not None
                else None
            ),
            source_workspace_id=(
                source_permit.scope.workspace_id
                if source_permit is not None
                else None
            ),
            source_scope_bound=source_permit is not None,
            desired_revision_id=revision_id,
            active_revision_id=revision_id,
            desired_sequence=1 if revision_id else 0,
        )

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
        metadata: dict | None = None,
        source_scope: SourceScope | None = None,
        source_create_if_missing: bool = False,
        actor_user_id: uuid.UUID | None = None,
    ) -> DocumentRevisionReservation:
        """Insert-or-get an immutable source/build revision before model work."""
        document_id = _new_id("kd")
        created_at = time.time()
        async with self._session() as session:
            collection, _access = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
            )
            source_permit = await self._source_write_permit(
                session,
                source_id=source_id,
                collection_owner_user_id=collection.created_by_user_id,
                source_scope=source_scope,
                create_if_missing=source_create_if_missing,
            )
            inserted = await session.execute(
                pg_insert(knowledge_documents)
                .values(
                    id=document_id,
                    collection_id=collection_id,
                    tenant_id=_DEFAULT_TENANT,
                    title="",
                    text="",
                    metadata={"source_id": source_id},
                    source_id=source_id,
                    source_owner_user_id=source_permit.scope.owner_user_id,
                    source_workspace_id=source_permit.scope.workspace_id,
                    source_scope_bound=True,
                    desired_revision_id=None,
                    active_revision_id=None,
                    desired_sequence=0,
                    lifecycle_status="staging",
                    chunk_count=0,
                    vector_synced=False,
                    created_at=created_at,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        knowledge_documents.c.collection_id,
                        knowledge_documents.c.source_id,
                    ],
                    index_where=knowledge_documents.c.source_id.is_not(None),
                )
            )
            if not inserted.rowcount:
                document_id = (
                    await session.execute(
                        select(knowledge_documents.c.id).where(
                            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_documents.c.collection_id == collection_id,
                            knowledge_documents.c.source_id == source_id,
                            knowledge_documents.c.source_owner_user_id.is_not_distinct_from(
                                source_permit.scope.owner_user_id
                            ),
                            knowledge_documents.c.source_workspace_id.is_not_distinct_from(
                                source_permit.scope.workspace_id
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if document_id is None:
                    raise SourceDeletionConflict(source_id)
            row = (
                await session.execute(
                    select(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id == document_id,
                    )
                    .with_for_update()
                )
            ).one()
            if (
                row.source_owner_user_id != source_permit.scope.owner_user_id
                or row.source_workspace_id != source_permit.scope.workspace_id
                or not row.source_scope_bound
            ):
                raise SourceDeletionConflict(source_id)
            revision_insert = await session.execute(
                pg_insert(knowledge_document_revisions)
                .values(
                    revision_id=revision_id,
                    tenant_id=_DEFAULT_TENANT,
                    document_id=document_id,
                    collection_id=collection_id,
                    source_id=source_id,
                    content_hash=content_hash,
                    build_contract_hash=build_contract_hash,
                    title=title,
                    text=text,
                    metadata=_immutable_revision_metadata(dict(metadata or {})),
                    status="staging",
                    created_at=created_at,
                )
                .on_conflict_do_nothing(
                    constraint="uq_knowledge_revision_build_identity"
                )
            )
            if revision_insert.rowcount:
                revision_row = (
                    await session.execute(
                        select(knowledge_document_revisions).where(
                            knowledge_document_revisions.c.revision_id
                            == revision_id
                        )
                    )
                ).one()
            else:
                revision_row = (
                    await session.execute(
                        select(knowledge_document_revisions).where(
                            knowledge_document_revisions.c.tenant_id
                            == _DEFAULT_TENANT,
                            knowledge_document_revisions.c.collection_id
                            == collection_id,
                            knowledge_document_revisions.c.source_id == source_id,
                            knowledge_document_revisions.c.content_hash
                            == content_hash,
                            knowledge_document_revisions.c.build_contract_hash
                            == build_contract_hash,
                        )
                    )
                ).one()
                revision_id = revision_row.revision_id
            if row.desired_revision_id == revision_id:
                sequence = int(row.desired_sequence)
            else:
                sequence = int(row.desired_sequence) + 1
                await session.execute(
                    update(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id == document_id,
                    )
                    .values(
                        desired_revision_id=revision_id,
                        desired_sequence=sequence,
                        lifecycle_status=(
                            "active"
                            if row.active_revision_id is not None
                            else "staging"
                        ),
                    )
                )
            already_published = (
                revision_row.status == "active"
                and row.active_revision_id == revision_id
            )
        return DocumentRevisionReservation(
            document_id=document_id,
            collection_id=collection_id,
            source_id=source_id,
            revision_id=revision_id,
            sequence=sequence,
            content_hash=content_hash,
            build_contract_hash=build_contract_hash,
            already_published=already_published,
            source_scope=source_permit.scope,
            source_epoch=source_permit.epoch,
        )

    async def load_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> ReservedDocumentRevision:
        """Read immutable work input under collection and source authority."""
        async with self._session() as session:
            preliminary = await self._document_row(session, document_id)
            collection, _access = await self._mutable_collection_row(
                session,
                preliminary.collection_id,
                actor_user_id=actor_user_id,
            )
            document = await self._document_row(
                session, document_id, for_update=True
            )
            revision_row = (
                await session.execute(
                    select(knowledge_document_revisions).where(
                        knowledge_document_revisions.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_document_revisions.c.revision_id
                        == revision_id,
                        knowledge_document_revisions.c.document_id
                        == document_id,
                    )
                )
            ).one_or_none()
            if (
                revision_row is None
                or document.desired_revision_id != revision_id
            ):
                raise DocumentRevisionSuperseded(revision_id)
            source_id = revision_row.source_id or document.source_id
            if not source_id:
                raise DocumentRevisionSuperseded(revision_id)
            source_scope = SourceScope(
                tenant_id=_DEFAULT_TENANT,
                source_id=source_id,
                owner_user_id=document.source_owner_user_id,
                workspace_id=document.source_workspace_id,
            )
            source_permit = await self._source_write_permit(
                session,
                source_id=source_id,
                collection_owner_user_id=collection.created_by_user_id,
                source_scope=source_scope,
            )
            if source_permit is None:
                raise SourceDeletionConflict(source_id)
            revision = KnowledgeDocumentRevision(
                revision_id=revision_row.revision_id,
                document_id=revision_row.document_id,
                collection_id=revision_row.collection_id,
                source_id=revision_row.source_id,
                content_hash=revision_row.content_hash,
                build_contract_hash=revision_row.build_contract_hash,
                title=revision_row.title,
                text=revision_row.text,
                metadata=dict(revision_row.metadata or {}),
                status=revision_row.status,
                created_at=float(revision_row.created_at),
                activated_at=revision_row.activated_at,
                superseded_at=revision_row.superseded_at,
            )
            return ReservedDocumentRevision(
                revision=revision,
                reservation=DocumentRevisionReservation(
                    document_id=document_id,
                    collection_id=document.collection_id,
                    source_id=source_id,
                    revision_id=revision_id,
                    sequence=int(document.desired_sequence),
                    content_hash=revision.content_hash,
                    build_contract_hash=revision.build_contract_hash,
                    already_published=(
                        revision.status == "active"
                        and document.active_revision_id == revision_id
                    ),
                    source_scope=source_permit.scope,
                    source_epoch=source_permit.epoch,
                ),
            )

    async def publish_document_revision(
        self,
        *,
        reservation: DocumentRevisionReservation,
        title: str,
        text: str,
        metadata: dict,
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
        """CAS-publish a revision and clean only its predecessor's vectors."""
        del publication_guard
        new_rows: list[dict] = []
        old_chunk_ids: list[str] = []
        embedding_model = ""
        try:
            async with self._session() as session:
                collection, access = await self._mutable_collection_row(
                    session,
                    reservation.collection_id,
                    actor_user_id=actor_user_id,
                )
                await self._assert_indexing_fence(
                    session,
                    job_id=fence_job_id,
                    attempt=fence_attempt,
                    document_id=reservation.document_id,
                    revision_id=reservation.revision_id,
                )
                source_permit = await self._source_write_permit(
                    session,
                    source_id=reservation.source_id,
                    collection_owner_user_id=collection.created_by_user_id,
                    source_scope=reservation.source_scope,
                    expected_epoch=reservation.source_epoch or None,
                )
                document = await self._document_row(
                    session, reservation.document_id, for_update=True
                )
                if (
                    document.collection_id != reservation.collection_id
                    or document.source_id != reservation.source_id
                    or document.source_owner_user_id
                    != source_permit.scope.owner_user_id
                    or document.source_workspace_id
                    != source_permit.scope.workspace_id
                    or not document.source_scope_bound
                ):
                    raise DocumentNotFound(reservation.document_id)
                if (
                    document.desired_revision_id != reservation.revision_id
                    or int(document.desired_sequence) != reservation.sequence
                ):
                    raise DocumentRevisionSuperseded(reservation.revision_id)
                revision_row = (
                    await session.execute(
                        select(knowledge_document_revisions)
                        .where(
                            knowledge_document_revisions.c.tenant_id
                            == _DEFAULT_TENANT,
                            knowledge_document_revisions.c.revision_id
                            == reservation.revision_id,
                            knowledge_document_revisions.c.document_id
                            == reservation.document_id,
                        )
                        .with_for_update()
                    )
                ).one_or_none()
                if revision_row is None:
                    raise DocumentRevisionSuperseded(reservation.revision_id)
                if (
                    revision_row.content_hash != reservation.content_hash
                    or revision_row.build_contract_hash
                    != reservation.build_contract_hash
                    or revision_row.title != title
                    or revision_row.text != text
                    or dict(revision_row.metadata or {})
                    != _immutable_revision_metadata(dict(metadata))
                ):
                    raise KnowledgeError(
                        "immutable document revision payload changed"
                    )
                if (
                    revision_row.status == "active"
                    and document.active_revision_id == reservation.revision_id
                ):
                    return self._document_from_row(document)
                self._validate_embeddings(
                    chunks, embeddings, collection.embedding_dim
                )
                embedding_model = collection.embedding_model
                target_generation = generation_id or collection.active_generation_id
                old_chunk_ids = [
                    row.id
                    for row in (
                        await session.execute(
                            select(knowledge_chunks.c.id).where(
                                knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                                knowledge_chunks.c.document_id
                                == reservation.document_id,
                                knowledge_chunks.c.generation_id
                                == target_generation,
                            )
                        )
                    ).all()
                ]
                new_rows = self._build_chunk_rows(
                    document_id=reservation.document_id,
                    collection_id=reservation.collection_id,
                    chunks=chunks,
                    source_chunks=source_chunks,
                    retrieval_contexts=retrieval_contexts,
                    source_spans=source_spans,
                    document_content_hash=reservation.content_hash,
                    revision_id=reservation.revision_id,
                    generation_id=(
                        target_generation
                    ),
                    page_numbers=page_numbers,
                    created_at=time.time(),
                )
                # New points are invisible until their canonical rows and the
                # active revision pointer commit. Hydration drops them meanwhile.
                await self._vectors.upsert(
                    embedding_model=embedding_model,
                    collection_id=reservation.collection_id,
                    document_id=reservation.document_id,
                    vectors=[
                        ChunkVector(
                            chunk_id=row["id"],
                            dense=tuple(embedding),
                            text=row["text"],
                            generation_id=row["generation_id"],
                            revision_id=row["revision_id"],
                        )
                        for row, embedding in zip(new_rows, embeddings)
                    ],
                )
                await session.execute(
                    delete(knowledge_chunks).where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.document_id == reservation.document_id,
                        knowledge_chunks.c.generation_id == target_generation,
                    )
                )
                if new_rows:
                    await session.execute(insert(knowledge_chunks), new_rows)
                await session.execute(
                    update(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id == reservation.document_id,
                        knowledge_documents.c.desired_revision_id
                        == reservation.revision_id,
                        knowledge_documents.c.desired_sequence
                        == reservation.sequence,
                    )
                    .values(
                        title=title,
                        text=text,
                        metadata=dict(metadata),
                        active_revision_id=reservation.revision_id,
                        lifecycle_status="active",
                        chunk_count=len(chunks),
                        vector_synced=True,
                    )
                )
                now = time.time()
                if (
                    document.active_revision_id is not None
                    and document.active_revision_id != reservation.revision_id
                ):
                    await session.execute(
                        update(knowledge_document_revisions)
                        .where(
                            knowledge_document_revisions.c.tenant_id
                            == _DEFAULT_TENANT,
                            knowledge_document_revisions.c.revision_id
                            == document.active_revision_id,
                        )
                        .values(status="superseded", superseded_at=now)
                    )
                await session.execute(
                    update(knowledge_document_revisions)
                    .where(
                        knowledge_document_revisions.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_document_revisions.c.revision_id
                        == reservation.revision_id,
                    )
                    .values(
                        status="active",
                        activated_at=now,
                        superseded_at=None,
                    )
                )
                await append_resource_effects(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="knowledge_document.revision_published",
                    resource_type="knowledge_collection",
                    resource_id=reservation.collection_id,
                    scope="knowledge_collections",
                )
        except Exception:
            if new_rows and embedding_model:
                delete_chunks = getattr(self._vectors, "delete_chunks", None)
                if callable(delete_chunks):
                    try:
                        await delete_chunks(
                            embedding_model=embedding_model,
                            chunk_ids=[row["id"] for row in new_rows],
                        )
                    except Exception as exc:  # noqa: BLE001 - canonical CAS still failed
                        log.error(
                            "Failed to clean unpublished vectors for revision %s "
                            "(error_type=%s)",
                            reservation.revision_id,
                            type(exc).__name__,
                        )
            raise
        if old_chunk_ids:
            delete_chunks = getattr(self._vectors, "delete_chunks", None)
            if callable(delete_chunks):
                try:
                    await delete_chunks(
                        embedding_model=embedding_model,
                        chunk_ids=old_chunk_ids,
                    )
                except Exception as exc:  # noqa: BLE001 - stale points cannot hydrate
                    log.error(
                        "Published revision %s but stale-vector cleanup failed "
                        "(error_type=%s)",
                        reservation.revision_id,
                        type(exc).__name__,
                    )
        return await self.get_document(reservation.document_id)

    async def list_documents(self, collection_id: str) -> list[KnowledgeDocument]:
        """A collection's documents, newest first."""
        async with self._session() as session:
            await self._collection_row(session, collection_id)
            rows = (
                await session.execute(
                    select(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.collection_id == collection_id,
                        knowledge_documents.c.lifecycle_status == "active",
                    )
                    .order_by(knowledge_documents.c.created_at.desc())
                )
            ).all()
        return [self._document_from_row(row) for row in rows]

    async def list_documents_page(
        self,
        collection_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[KnowledgeDocument], str | None]:
        """One keyset page (newest first); the DB does the LIMIT.

        Visibility is the single parent-collection check (``_collection_row``)
        — all of a readable collection's documents are visible — so the
        DB-side ``LIMIT`` never under-fills a page.
        """
        async with self._session() as session:
            await self._collection_row(session, collection_id)
            query = select(knowledge_documents).where(
                knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                knowledge_documents.c.collection_id == collection_id,
                knowledge_documents.c.lifecycle_status == "active",
            )
            if after is not None:
                query = query.where(
                    tuple_(
                        knowledge_documents.c.created_at,
                        knowledge_documents.c.id,
                    )
                    < tuple_(after[0], after[1])
                )
            query = query.order_by(
                knowledge_documents.c.created_at.desc(),
                knowledge_documents.c.id.desc(),
            ).limit(limit + 1)
            rows = (await session.execute(query)).all()
        documents = [self._document_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_cursor(documents[-1].created_at, documents[-1].id)
            if len(rows) > limit and documents
            else None
        )
        return documents, next_cursor

    async def get_document(self, document_id: str) -> KnowledgeDocument:
        """One document (full text) or :class:`DocumentNotFound`."""
        async with self._session() as session:
            row = await self._document_row(session, document_id)
        return self._document_from_row(row)

    async def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        """One document's chunks ordered by ``chunk_index`` (no vectors)."""
        async with self._session() as session:
            document = await self._document_row(
                session, document_id
            )  # 404 if unknown
            active_revision = document.active_revision_id
            collection = await self._collection_row(
                session, document.collection_id
            )
            active_generation = collection.active_generation_id
            rows = (
                await session.execute(
                    select(
                        knowledge_chunks.c.id,
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.collection_id,
                        knowledge_chunks.c.chunk_index,
                        knowledge_chunks.c.text,
                        knowledge_chunks.c.source_text,
                        knowledge_chunks.c.retrieval_context,
                        knowledge_chunks.c.source_start,
                        knowledge_chunks.c.source_end,
                        knowledge_chunks.c.document_content_hash,
                        knowledge_chunks.c.revision_id,
                        knowledge_chunks.c.generation_id,
                        knowledge_chunks.c.page_number,
                    )
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.document_id == document_id,
                        (
                            knowledge_chunks.c.revision_id.is_(None)
                            if active_revision is None
                            else knowledge_chunks.c.revision_id == active_revision
                        ),
                        (
                            knowledge_chunks.c.generation_id.is_(None)
                            if active_generation is None
                            else knowledge_chunks.c.generation_id
                            == active_generation
                        ),
                    )
                    .order_by(knowledge_chunks.c.chunk_index)
                )
            ).all()
        return [
            DocumentChunk(
                id=row[0],
                document_id=row[1],
                collection_id=row[2],
                chunk_index=row[3],
                text=row[4],
                source_text=row[5],
                retrieval_context=row[6],
                source_start=row[7],
                source_end=row[8],
                document_content_hash=row[9],
                revision_id=row[10],
                generation_id=row[11],
                page_number=row[12],
                source_verified=source_excerpt_is_verified(
                    canonical_text=document.text,
                    source_text=row[5],
                    source_start=row[7],
                    source_end=row[8],
                    document_content_hash=row[9],
                ),
            )
            for row in rows
        ]

    async def delete_document(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Delete one document and its chunks (DB cascade + vectors)."""
        async with self._session() as session:
            preliminary = await self._document_row(session, document_id)
            collection, access = await self._mutable_collection_row(
                session,
                preliminary.collection_id,
                actor_user_id=actor_user_id,
            )
            await self._delete_document_with_locked_collection(
                session,
                document_id=document_id,
                expected_collection_id=preliminary.collection_id,
                collection=collection,
                owner_user_id=access.owner_user_id,
                actor_user_id=actor_user_id,
            )

    async def delete_document_for_aggregate(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        """Converge a document deletion after durable operation authorization."""

        async with self._session() as session:
            preliminary = await self._document_row(session, document_id)
            collection = await self._collection_row(
                session,
                preliminary.collection_id,
                for_update=True,
            )
            await self._delete_document_with_locked_collection(
                session,
                document_id=document_id,
                expected_collection_id=preliminary.collection_id,
                collection=collection,
                owner_user_id=collection.created_by_user_id,
                actor_user_id=actor_user_id,
            )

    async def mark_document_deleting(self, document_id: str) -> None:
        """Idempotently detach one aggregate-deletion target from retrieval."""

        async with self._session() as session:
            result = await session.execute(
                update(knowledge_documents)
                .where(
                    knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_documents.c.id == document_id,
                    knowledge_documents.c.lifecycle_status.in_(
                        ("active", "deleting")
                    ),
                )
                .values(lifecycle_status="deleting")
            )
            if result.rowcount != 1:
                raise DocumentNotFound(document_id)

    async def restore_document_active(self, document_id: str) -> None:
        """Restore a tombstone only before aggregate cleanup has begun."""

        async with self._session() as session:
            await session.execute(
                update(knowledge_documents)
                .where(
                    knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_documents.c.id == document_id,
                    knowledge_documents.c.lifecycle_status == "deleting",
                )
                .values(lifecycle_status="active")
            )

    async def count_document_residuals(
        self,
        *,
        document_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        """Count canonical and derived residue for terminal document proof."""

        async with self._session() as session:
            document_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id == document_id,
                    )
                )
                or 0
            )
            chunk_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_chunks)
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.document_id == document_id,
                    )
                )
                or 0
            )
            revision_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_document_revisions)
                    .where(
                        knowledge_document_revisions.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_document_revisions.c.document_id
                        == document_id,
                    )
                )
                or 0
            )
        vector_count = await self._vectors.count_document(
            embedding_model=embedding_model,
            document_id=document_id,
        )
        return {
            "documents": document_count,
            "chunks": chunk_count,
            "revisions": revision_count,
            "vectors": int(vector_count),
        }

    async def _source_write_permit(
        self,
        session: AsyncSession,
        *,
        source_id: str | None,
        collection_owner_user_id: uuid.UUID | None,
        source_scope: SourceScope | None = None,
        expected_epoch: int | None = None,
        create_if_missing: bool = False,
    ) -> SourceWritePermit | None:
        """Validate the source epoch inside the canonical write transaction."""
        if source_id is None:
            return None
        try:
            if source_scope is not None:
                if (
                    source_scope.tenant_id != _DEFAULT_TENANT
                    or source_scope.source_id != source_id
                ):
                    raise SourceLifecycleConflict(source_id)
                scope = source_scope
            elif source_id.startswith("asset:"):
                # Asset scope was minted by AssetStore from the canonical row.
                # Knowledge metadata and the current caller never establish it.
                scope = await self._source_authority.resolve_scope(
                    session,
                    tenant_id=_DEFAULT_TENANT,
                    source_id=source_id,
                )
            else:
                scope = SourceScope(
                    tenant_id=_DEFAULT_TENANT,
                    source_id=source_id,
                    owner_user_id=collection_owner_user_id,
                    workspace_id=None,
                )
            return await self._source_authority.active_write(
                session,
                scope,
                expected_epoch=expected_epoch,
                create_if_missing=(
                    (create_if_missing or not scope.is_asset)
                    and not scope.is_asset
                ),
            )
        except SourceLifecycleConflict:
            raise SourceDeletionConflict(source_id)

    @staticmethod
    def _source_predicate(source_id: str):
        legacy = source_id.removeprefix("asset:")
        return or_(
            knowledge_documents.c.source_id == source_id,
            knowledge_documents.c.metadata["fileId"].as_string() == legacy,
            knowledge_documents.c.metadata["file_id"].as_string() == legacy,
        )

    @classmethod
    def _source_scope_predicate(cls, scope: SourceScope):
        """Restrict a source match to its server-minted owner/workspace."""

        return and_(
            cls._source_predicate(scope.source_id),
            knowledge_documents.c.source_scope_bound.is_(True),
            knowledge_documents.c.source_owner_user_id.is_not_distinct_from(
                scope.owner_user_id
            ),
            knowledge_documents.c.source_workspace_id.is_not_distinct_from(
                scope.workspace_id
            ),
        )

    async def list_documents_by_source(
        self,
        source_id: str,
        *,
        collection_id: str | None = None,
    ) -> list[KnowledgeDocument]:
        predicates = [
            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
            self._source_predicate(source_id),
            knowledge_documents.c.lifecycle_status != "deleted",
        ]
        if collection_id is not None:
            predicates.append(
                knowledge_documents.c.collection_id == collection_id
            )
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(knowledge_documents).where(*predicates)
                )
            ).all()
        return [self._document_from_row(row) for row in rows]

    async def mark_source_deleting(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Detach source rows from hydration before physical vector cleanup."""
        async with self._session() as session:
            if deletion_permit is not None:
                self._assert_permit_source(source_id, deletion_permit)
                try:
                    await self._source_authority.validate_deletion(
                        session, deletion_permit
                    )
                except SourceLifecycleConflict as exc:
                    raise SourceDeletionConflict(source_id) from exc
            rows = (
                await session.execute(
                    select(
                        knowledge_documents.c.id,
                        knowledge_documents.c.collection_id,
                    ).where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        (
                            self._source_scope_predicate(
                                deletion_permit.scope
                            )
                            if deletion_permit is not None
                            else self._source_predicate(source_id)
                        ),
                        knowledge_documents.c.lifecycle_status.notin_(
                            ("deleted", "deleting")
                        ),
                    )
                )
            ).all()
            if not rows:
                return 0
            if deletion_permit is None:
                for collection_id in sorted(
                    {row.collection_id for row in rows}
                ):
                    await self._mutable_collection_row(
                        session,
                        collection_id,
                        actor_user_id=actor_user_id,
                    )
            ids = [row.id for row in rows]
            result = await session.execute(
                update(knowledge_documents)
                .where(
                    knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_documents.c.id.in_(ids),
                    knowledge_documents.c.lifecycle_status.notin_(
                        ("deleted", "deleting")
                    ),
                )
                .values(lifecycle_status="deleting")
            )
            return int(result.rowcount or 0)

    @staticmethod
    def _assert_permit_source(
        source_id: str, deletion_permit: SourceDeletionPermit
    ) -> None:
        if (
            deletion_permit.scope.tenant_id != _DEFAULT_TENANT
            or deletion_permit.scope.source_id != source_id
        ):
            raise SourceDeletionConflict(source_id)

    async def _prepare_source_cleanup_in_session(
        self,
        session: AsyncSession,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        self._assert_permit_source(source_id, deletion_permit)
        try:
            await self._source_authority.validate_deletion(
                session, deletion_permit
            )
        except SourceLifecycleConflict as exc:
            raise SourceDeletionConflict(source_id) from exc
        documents = (
            await session.execute(
                select(
                    knowledge_documents.c.id,
                    knowledge_documents.c.collection_id,
                    knowledge_collections.c.embedding_model,
                )
                .select_from(
                    knowledge_documents.join(
                        knowledge_collections,
                        and_(
                            knowledge_documents.c.collection_id
                            == knowledge_collections.c.id,
                            knowledge_documents.c.tenant_id
                            == knowledge_collections.c.tenant_id,
                        ),
                    )
                )
                .where(
                    knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                    self._source_scope_predicate(deletion_permit.scope),
                )
                .order_by(knowledge_documents.c.id)
            )
        ).all()
        document_ids = [row.id for row in documents]
        chunk_rows = (
            (
                await session.execute(
                    select(
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.id,
                    )
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.document_id.in_(document_ids),
                    )
                    .order_by(
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.id,
                    )
                )
            ).all()
            if document_ids
            else []
        )
        chunks_by_document: dict[str, list[str]] = {
            document_id: [] for document_id in document_ids
        }
        for row in chunk_rows:
            chunks_by_document[row.document_id].append(row.id)
        targets = []
        for row in documents:
            chunk_ids = chunks_by_document[row.id]
            targets.append(
                SourceCleanupTarget(
                    collection_id=row.collection_id,
                    document_id=row.id,
                    embedding_model=row.embedding_model,
                    chunk_ids=tuple(chunk_ids),
                    point_ids=tuple(
                        self._vectors.point_ids_for_chunks(chunk_ids)
                    ),
                )
            )
        return SourceCleanupPlan(
            scope=deletion_permit.scope,
            authority_epoch=deletion_permit.epoch,
            operation_id=deletion_permit.operation_id,
            targets=tuple(targets),
        )

    async def prepare_source_cleanup(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        """Checkpoint exact documents and physical points before cleanup."""
        async with self._session() as session:
            return await self._prepare_source_cleanup_in_session(
                session,
                source_id,
                deletion_permit=deletion_permit,
            )

    async def execute_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Remove exact points first, then their still-matching canonical rows."""
        try:
            plan.assert_permit(deletion_permit)
        except ValueError as exc:
            raise SourceDeletionConflict(plan.scope.source_id) from exc
        source_id = plan.scope.source_id
        for target in plan.targets:
            await self._vectors.delete_chunks(
                embedding_model=target.embedding_model,
                chunk_ids=list(target.chunk_ids),
            )
            remaining = await self._vectors.count_chunks(
                embedding_model=target.embedding_model,
                chunk_ids=list(target.chunk_ids),
            )
            if remaining:
                raise KnowledgeError(
                    f"source vector cleanup incomplete for {target.document_id}: "
                    f"{remaining} point(s) remain"
                )

        async with self._session() as session:
            current = await self._prepare_source_cleanup_in_session(
                session,
                source_id,
                deletion_permit=deletion_permit,
            )
            if current.targets != plan.targets:
                raise SourceDeletionConflict(source_id)
            document_ids = [target.document_id for target in plan.targets]
            if document_ids:
                collection_rows = (
                    await session.execute(
                        select(
                            knowledge_collections.c.id,
                            knowledge_collections.c.created_by_user_id,
                        ).where(
                            knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_collections.c.id.in_(
                                {
                                    target.collection_id
                                    for target in plan.targets
                                }
                            ),
                        )
                    )
                ).all()
                result = await session.execute(
                    delete(knowledge_documents).where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id.in_(document_ids),
                        self._source_scope_predicate(plan.scope),
                    )
                )
                for collection in collection_rows:
                    await append_resource_effects(
                        session,
                        tenant_id=_DEFAULT_TENANT,
                        actor_user_id=actor_user_id,
                        owner_user_id=collection.created_by_user_id,
                        action="knowledge_source.deleted",
                        resource_type="knowledge_collection",
                        resource_id=collection.id,
                        scope="knowledge_collections",
                    )
                deleted = int(result.rowcount or 0)
            else:
                deleted = 0
        residuals = await self.verify_source_cleanup(
            plan, deletion_permit=deletion_permit
        )
        if any(residuals.values()):
            raise KnowledgeError(
                f"source cleanup residuals remain: {residuals}"
            )
        return deleted

    async def verify_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> dict[str, int]:
        """Verify relational and exact physical residuals after row deletion."""
        try:
            plan.assert_permit(deletion_permit)
        except ValueError as exc:
            raise SourceDeletionConflict(plan.scope.source_id) from exc
        async with self._session() as session:
            try:
                await self._source_authority.validate_deletion(
                    session, deletion_permit
                )
            except SourceLifecycleConflict as exc:
                raise SourceDeletionConflict(plan.scope.source_id) from exc
            document_ids = [target.document_id for target in plan.targets]
            documents = int(
                await session.scalar(
                    select(func.count(knowledge_documents.c.id)).where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.id.in_(document_ids),
                        self._source_scope_predicate(plan.scope),
                    )
                )
                or 0
            )
            chunks = int(
                await session.scalar(
                    select(func.count(knowledge_chunks.c.id)).where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.id.in_(
                            [
                                chunk_id
                                for target in plan.targets
                                for chunk_id in target.chunk_ids
                            ]
                        ),
                    )
                )
                or 0
            )
        vectors = 0
        for target in plan.targets:
            vectors += await self._vectors.count_chunks(
                embedding_model=target.embedding_model,
                chunk_ids=list(target.chunk_ids),
            )
        return {"documents": documents, "chunks": chunks, "vectors": vectors}

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
            try:
                await self.delete_document(
                    document.id, actor_user_id=actor_user_id
                )
            except DocumentNotFound:
                continue
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
            self._assert_permit_source(source_id, deletion_permit)
            return await self.verify_source_cleanup(
                cleanup_plan, deletion_permit=deletion_permit
            )
        async with self._session() as session:
            document_ids = select(knowledge_documents.c.id).where(
                knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                (
                    self._source_scope_predicate(deletion_permit.scope)
                    if deletion_permit is not None
                    else self._source_predicate(source_id)
                ),
            )
            documents = int(
                await session.scalar(
                    select(func.count()).select_from(
                        document_ids.subquery()
                    )
                )
                or 0
            )
            chunks = int(
                await session.scalar(
                    select(func.count(knowledge_chunks.c.id)).where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.document_id.in_(document_ids),
                    )
                )
                or 0
            )
            vector_rows = (
                await session.execute(
                    select(
                        knowledge_collections.c.embedding_model,
                        knowledge_chunks.c.id,
                    )
                    .select_from(
                        knowledge_chunks.join(
                            knowledge_documents,
                            and_(
                                knowledge_chunks.c.document_id
                                == knowledge_documents.c.id,
                                knowledge_chunks.c.tenant_id
                                == knowledge_documents.c.tenant_id,
                            ),
                        ).join(
                            knowledge_collections,
                            and_(
                                knowledge_documents.c.collection_id
                                == knowledge_collections.c.id,
                                knowledge_documents.c.tenant_id
                                == knowledge_collections.c.tenant_id,
                            ),
                        )
                    )
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        (
                            self._source_scope_predicate(
                                deletion_permit.scope
                            )
                            if deletion_permit is not None
                            else self._source_predicate(source_id)
                        ),
                    )
                )
            ).all()
            plan = None
            if deletion_permit is not None:
                plan = await self._prepare_source_cleanup_in_session(
                    session,
                    source_id,
                    deletion_permit=deletion_permit,
                )
        groups: dict[str, list[str]] = {}
        for row in vector_rows:
            groups.setdefault(row.embedding_model, []).append(row.id)
        vectors = 0
        for embedding_model, chunk_ids in groups.items():
            vectors += await self._vectors.count_chunks(
                embedding_model=embedding_model,
                chunk_ids=chunk_ids,
            )
        if plan is not None:
            # ``plan`` is authority-validated; its groups are identical to the
            # canonical rows above and are retained only for caller checkpointing.
            if plan.chunk_count != len(vector_rows):
                raise SourceDeletionConflict(source_id)
        return {"documents": documents, "chunks": chunks, "vectors": vectors}

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
        """Rebuild one document while its prior active chunks stay readable."""
        chunk_rows: list[dict] = []
        prior_ids: list[str] = []
        embedding_model = ""
        collection_id = ""
        staged = False
        try:
            async with self._session() as session:
                preliminary = await self._document_row(session, document_id)
                collection, access = await self._mutable_collection_row(
                    session,
                    preliminary.collection_id,
                    actor_user_id=actor_user_id,
                    allow_active_maintenance=True,
                )
                await self._assert_indexing_fence(
                    session,
                    job_id=fence_job_id,
                    attempt=fence_attempt,
                    generation_id=generation_id,
                )
                document = await self._document_row(
                    session, document_id, for_update=True
                )
                if document.collection_id != preliminary.collection_id:
                    raise DocumentNotFound(document_id)
                await self._source_write_permit(
                    session,
                    source_id=document.source_id,
                    collection_owner_user_id=collection.created_by_user_id,
                )
                self._validate_embeddings(
                    chunks, embeddings, collection.embedding_dim
                )
                collection_id = document.collection_id
                embedding_model = collection.embedding_model
                effective_revision = revision_id or document.active_revision_id
                effective_generation = (
                    generation_id or collection.active_generation_id
                )
                staged = (
                    effective_generation is not None
                    and effective_generation != collection.active_generation_id
                )
                prior_query = select(knowledge_chunks.c.id).where(
                    knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_chunks.c.document_id == document_id,
                    knowledge_chunks.c.generation_id == effective_generation,
                )
                prior_ids = [
                    row.id
                    for row in (await session.execute(prior_query)).all()
                ]
                # The immutable build coordinates produce stable ids. A shadow
                # generation/revision still has a disjoint id space from the
                # active evidence, while a retry overwrites its own points.
                effective_content_hash = (
                    document_content_hash
                    or hashlib.sha256(document.text.encode("utf-8")).hexdigest()
                )
                chunk_rows = self._build_chunk_rows(
                    document_id=document_id,
                    collection_id=collection_id,
                    chunks=chunks,
                    source_chunks=source_chunks,
                    retrieval_contexts=retrieval_contexts,
                    source_spans=source_spans,
                    document_content_hash=effective_content_hash,
                    revision_id=effective_revision,
                    generation_id=effective_generation,
                    page_numbers=page_numbers,
                    created_at=time.time(),
                )
                if staged:
                    # A retry/repair owns this exact document scope through the
                    # durable job fence and collection row lock. Clear the
                    # complete derived scope, including points that survived a
                    # prior vector write whose Postgres transaction rolled back.
                    await self._vectors.delete_generation_document(
                        embedding_model=embedding_model,
                        collection_id=collection_id,
                        generation_id=effective_generation,
                        document_id=document_id,
                    )
                    residual = await self._vectors.count_generation_document(
                        embedding_model=embedding_model,
                        collection_id=collection_id,
                        generation_id=effective_generation,
                        document_id=document_id,
                    )
                    if residual:
                        raise KnowledgeError(
                            "shadow document reset left "
                            f"{residual} vector points"
                        )
                await self._vectors.upsert(
                    embedding_model=embedding_model,
                    collection_id=collection_id,
                    document_id=document_id,
                    vectors=[
                        ChunkVector(
                            chunk_id=row["id"],
                            dense=tuple(embedding),
                            text=row["text"],
                            generation_id=row["generation_id"],
                            revision_id=row["revision_id"],
                        )
                        for row, embedding in zip(chunk_rows, embeddings)
                    ],
                )
                delete_query = delete(knowledge_chunks).where(
                    knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_chunks.c.document_id == document_id,
                    knowledge_chunks.c.generation_id == effective_generation,
                )
                await session.execute(delete_query)
                if chunk_rows:
                    await session.execute(insert(knowledge_chunks), chunk_rows)
                if not staged:
                    await session.execute(
                        update(knowledge_documents)
                        .where(
                            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_documents.c.id == document_id,
                        )
                        .values(
                            chunk_count=len(chunks),
                            vector_synced=True,
                            desired_revision_id=(
                                effective_revision or document.desired_revision_id
                            ),
                            active_revision_id=(
                                effective_revision or document.active_revision_id
                            ),
                        )
                    )
                    await append_resource_effects(
                        session,
                        tenant_id=_DEFAULT_TENANT,
                        actor_user_id=actor_user_id,
                        owner_user_id=access.owner_user_id,
                        action="knowledge_document.reembedded",
                        resource_type="knowledge_collection",
                        resource_id=collection_id,
                        scope="knowledge_collections",
                    )
        except Exception:
            delete_chunks = getattr(self._vectors, "delete_chunks", None)
            cleanup_ids = sorted(
                {row["id"] for row in chunk_rows} - set(prior_ids)
            )
            if (
                not staged
                and cleanup_ids
                and embedding_model
                and callable(delete_chunks)
            ):
                try:
                    await delete_chunks(
                        embedding_model=embedding_model,
                        chunk_ids=cleanup_ids,
                    )
                except Exception as exc:  # noqa: BLE001 - unhydrated points are inert
                    log.error(
                        "Failed to clean unpublished reindex vectors for %s "
                        "(error_type=%s)",
                        document_id,
                        type(exc).__name__,
                    )
            raise
        delete_chunks = getattr(self._vectors, "delete_chunks", None)
        stale_prior_ids = sorted(
            set(prior_ids) - {row["id"] for row in chunk_rows}
        )
        if stale_prior_ids and callable(delete_chunks):
            try:
                await delete_chunks(
                    embedding_model=embedding_model,
                    chunk_ids=stale_prior_ids,
                )
            except Exception as exc:  # noqa: BLE001 - stale point ids cannot hydrate
                log.error(
                    "Reindex published for %s but stale-vector cleanup failed "
                    "(error_type=%s)",
                    document_id,
                    type(exc).__name__,
                )
        return await self.get_document(document_id)

    async def begin_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeIndexGeneration:
        """Persist the immutable identity of a shadow build before work."""
        now = time.time()
        async with self._session() as session:
            await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
                allow_active_maintenance=True,
            )
            await session.execute(
                pg_insert(knowledge_index_generations)
                .values(
                    generation_id=generation_id,
                    tenant_id=_DEFAULT_TENANT,
                    collection_id=collection_id,
                    build_contract_hash=build_contract_hash,
                    status="building",
                    manifest=dict(manifest),
                    validation={},
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[knowledge_index_generations.c.generation_id]
                )
            )
            row = (
                await session.execute(
                    select(knowledge_index_generations).where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                    )
                )
            ).one()
            if (
                row.collection_id != collection_id
                or row.build_contract_hash != build_contract_hash
                or row.status not in ("building", "active")
            ):
                raise IndexGenerationSuperseded(generation_id)
        return KnowledgeIndexGeneration(
            generation_id=row.generation_id,
            collection_id=row.collection_id,
            build_contract_hash=row.build_contract_hash,
            status=row.status,
            manifest=dict(row.manifest or {}),
            validation=dict(row.validation or {}),
            created_at=row.created_at,
            activated_at=row.activated_at,
            superseded_at=row.superseded_at,
            rollback_until=row.rollback_until,
        )

    async def remove_document_from_generation(
        self,
        *,
        collection_id: str,
        document_id: str,
        generation_id: str,
    ) -> int:
        """Remove exactly one deleted snapshot member from a shadow build."""
        async with self._session() as session:
            collection = await self._collection_row(
                session, collection_id, for_update=True
            )
            if collection.active_generation_id == generation_id:
                raise KnowledgeError(
                    "active generation documents cannot be removed as deltas"
                )
            rows = (
                await session.execute(
                    select(knowledge_chunks.c.id).where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.collection_id == collection_id,
                        knowledge_chunks.c.document_id == document_id,
                        knowledge_chunks.c.generation_id == generation_id,
                    )
                )
            ).all()
            chunk_ids = [row.id for row in rows]
            await self._vectors.delete_generation_document(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
                document_id=document_id,
            )
            residual = await self._vectors.count_generation_document(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
                document_id=document_id,
            )
            if residual:
                raise KnowledgeError(
                    "generation delta cleanup left "
                    f"{residual} vector points"
                )
            await session.execute(
                delete(knowledge_chunks).where(
                    knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_chunks.c.collection_id == collection_id,
                    knowledge_chunks.c.document_id == document_id,
                    knowledge_chunks.c.generation_id == generation_id,
                )
            )
        return len(chunk_ids)

    async def reset_generation_for_raw_choice(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
    ) -> int:
        """Clear and verify one paused shadow build before raw rebuilding."""
        async with self._session() as session:
            collection = await self._collection_row(
                session, collection_id, for_update=True
            )
            if collection.active_generation_id == generation_id:
                raise KnowledgeError("active generation cannot be reset")
            generation = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if generation is None or generation.status != "building":
                raise KnowledgeError("only an unpublished generation can be reset")
            chunk_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(knowledge_chunks)
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.collection_id == collection_id,
                        knowledge_chunks.c.generation_id == generation_id,
                    )
                )
                or 0
            )
            await self._vectors.delete_generation(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
            )
            residual = await self._vectors.count_generation(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
            )
            if residual:
                raise KnowledgeError(
                    f"raw rebuild reset left {residual} vector points"
                )
            await session.execute(
                delete(knowledge_chunks).where(
                    knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_chunks.c.collection_id == collection_id,
                    knowledge_chunks.c.generation_id == generation_id,
                )
            )
            await session.execute(
                update(knowledge_index_generations)
                .where(
                    knowledge_index_generations.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_index_generations.c.generation_id == generation_id,
                )
                .values(
                    build_contract_hash=build_contract_hash,
                    manifest=dict(manifest),
                    validation={"raw_by_user_choice": True},
                )
            )
        return chunk_count

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
        """Validate a complete shadow generation and switch one DB pointer."""
        if expected_validation is None:
            raise GenerationValidationError(
                "generation publication requires a build validation manifest"
            )

        # Qdrant verification can be slow for a large generation. Snapshot the
        # canonical point ids and build ledger in a short transaction, release
        # its locks, then perform the external counts. The publication CAS below
        # reacquires the collection/job fences and rechecks the manifest, build
        # hash, and exact canonical id set before it moves the active pointer.
        async with self._session() as session:
            preflight_collection, _ = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
                allow_active_maintenance=True,
            )
            await self._assert_indexing_fence(
                session,
                job_id=fence_job_id,
                attempt=fence_attempt,
                generation_id=generation_id,
            )
            if preflight_collection.active_generation_id == generation_id:
                return self._collection_from_row(
                    preflight_collection,
                    await self._document_counts(session, collection_id),
                )
            preflight_generation = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if (
                preflight_generation is None
                or preflight_generation.build_contract_hash
                != build_contract_hash
                or preflight_generation.status != "building"
            ):
                raise GenerationValidationError(
                    "generation ledger contradicts the active build contract"
                )
            preflight_chunk_ids = tuple(
                (
                    await session.execute(
                        select(knowledge_chunks.c.id)
                        .where(
                            knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_chunks.c.collection_id == collection_id,
                            knowledge_chunks.c.generation_id == generation_id,
                        )
                        .order_by(
                            knowledge_chunks.c.document_id,
                            knowledge_chunks.c.chunk_index,
                        )
                    )
                ).scalars()
            )
            embedding_model = preflight_collection.embedding_model

        point_count = await self._vectors.count_chunks(
            embedding_model=embedding_model,
            chunk_ids=list(preflight_chunk_ids),
        )
        generation_point_count = await self._vectors.count_generation(
            embedding_model=embedding_model,
            collection_id=collection_id,
            generation_id=generation_id,
        )
        if point_count != expected_validation.point_count:
            raise GenerationValidationError(
                f"generation {generation_id} vector point count mismatch: "
                f"expected {expected_validation.point_count}, got {point_count}"
            )
        if generation_point_count != expected_validation.point_count:
            raise GenerationValidationError(
                f"generation {generation_id} total vector point count "
                f"mismatch: expected {expected_validation.point_count}, "
                f"got {generation_point_count}"
            )

        async with self._session() as session:
            collection, access = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
                allow_active_maintenance=True,
            )
            await self._assert_indexing_fence(
                session,
                job_id=fence_job_id,
                attempt=fence_attempt,
                generation_id=generation_id,
            )
            if collection.active_generation_id == generation_id:
                return self._collection_from_row(
                    collection, await self._document_counts(session, collection_id)
                )
            current_manifest = {
                row.id: row.active_revision_id or ""
                for row in (
                    await session.execute(
                        select(
                            knowledge_documents.c.id,
                            knowledge_documents.c.active_revision_id,
                        ).where(
                            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_documents.c.collection_id == collection_id,
                            knowledge_documents.c.lifecycle_status == "active",
                        )
                    )
                ).all()
            }
            expected = expected_manifest or {
                document_id: current_manifest.get(document_id, "")
                for document_id in expected_document_ids
            }
            if current_manifest != expected:
                raise GenerationManifestChanged(
                    "collection manifest changed while the generation was built"
                )
            if expected_validation.embedding_dim != collection.embedding_dim:
                raise GenerationValidationError(
                    "generation embedding dimension contradicts collection"
                )
            if {
                document_id: item.revision_id
                for document_id, item in expected_validation.documents.items()
            } != expected:
                raise GenerationValidationError(
                    "generation validation revisions contradict source manifest"
                )
            staged_rows = (
                await session.execute(
                    select(
                        knowledge_chunks.c.id,
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.chunk_index,
                        knowledge_chunks.c.revision_id,
                        knowledge_chunks.c.source_start,
                        knowledge_chunks.c.source_end,
                        knowledge_chunks.c.source_text,
                        knowledge_chunks.c.document_content_hash,
                        knowledge_documents.c.text.label("document_text"),
                    )
                    .select_from(
                        knowledge_chunks.join(
                            knowledge_documents,
                            and_(
                                knowledge_chunks.c.document_id
                                == knowledge_documents.c.id,
                                knowledge_chunks.c.tenant_id
                                == knowledge_documents.c.tenant_id,
                            ),
                        )
                    )
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.collection_id == collection_id,
                        knowledge_chunks.c.generation_id == generation_id,
                    )
                    .order_by(
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.chunk_index,
                    )
                )
            ).all()
            staged_manifest = {
                row.document_id: row.revision_id or "" for row in staged_rows
            }
            if staged_manifest != expected:
                raise KnowledgeError(
                    f"generation {generation_id} is incomplete; missing "
                    f"{sorted(set(expected) - set(staged_manifest))}"
                )
            rows_by_document: dict[str, list] = {
                document_id: [] for document_id in expected
            }
            for row in staged_rows:
                rows_by_document.setdefault(row.document_id, []).append(row)
            for document_id, expected_document in (
                expected_validation.documents.items()
            ):
                rows = rows_by_document.get(document_id, [])
                if len(rows) != expected_document.chunk_count:
                    raise GenerationValidationError(
                        f"generation {generation_id} chunk count mismatch for "
                        f"{document_id}"
                    )
                canonical_bytes = rows[0].document_text.encode("utf-8")
                if hashlib.sha256(canonical_bytes).hexdigest() != (
                    expected_document.content_hash
                ):
                    raise GenerationValidationError(
                        f"generation {generation_id} source hash mismatch for "
                        f"{document_id}"
                    )
                for chunk_index, (row, expected_span) in enumerate(
                    zip(rows, expected_document.source_spans)
                ):
                    start, end = expected_span
                    canonical_bytes = row.document_text.encode("utf-8")
                    try:
                        source_slice = canonical_bytes[start:end].decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise GenerationValidationError(
                            f"generation {generation_id} has a non-UTF-8 "
                            f"source boundary for {document_id}"
                        ) from exc
                    if (
                        row.chunk_index != chunk_index
                        or (row.revision_id or "")
                        != expected_document.revision_id
                        or row.document_content_hash
                        != expected_document.content_hash
                        or (row.source_start, row.source_end) != expected_span
                        or not 0 <= start < end <= len(canonical_bytes)
                        or source_slice != row.source_text
                    ):
                        raise GenerationValidationError(
                            f"generation {generation_id} source validation "
                            f"failed for {document_id} chunk {chunk_index}"
                        )
            chunk_ids = [row.id for row in staged_rows]
            if len(chunk_ids) != expected_validation.chunk_count:
                raise GenerationValidationError(
                    f"generation {generation_id} total chunk count mismatch"
                )
            if tuple(chunk_ids) != preflight_chunk_ids:
                raise GenerationValidationError(
                    "generation canonical rows changed after vector validation"
                )
            now = time.time()
            await session.execute(
                pg_insert(knowledge_index_generations)
                .values(
                    generation_id=generation_id,
                    tenant_id=_DEFAULT_TENANT,
                    collection_id=collection_id,
                    build_contract_hash=build_contract_hash,
                    status="building",
                    manifest=dict(expected),
                    validation={},
                    created_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[knowledge_index_generations.c.generation_id]
                )
            )
            generation_row = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                    )
                    .with_for_update()
                )
            ).one()
            if (
                generation_row.build_contract_hash != build_contract_hash
                or generation_row.status != "building"
            ):
                raise GenerationValidationError(
                    "generation ledger contradicts the active build contract"
                )
            validation = expected_validation.as_dict()
            for document_id, rows in rows_by_document.items():
                revision_id = expected[document_id]
                active_revision_predicate = (
                    knowledge_documents.c.active_revision_id == revision_id
                    if revision_id
                    else knowledge_documents.c.active_revision_id.is_(None)
                )
                projection = await session.execute(
                    update(knowledge_documents)
                    .where(
                        knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_documents.c.collection_id == collection_id,
                        knowledge_documents.c.id == document_id,
                        active_revision_predicate,
                    )
                    .values(chunk_count=len(rows), vector_synced=True)
                )
                if projection.rowcount != 1:
                    raise GenerationManifestChanged(
                        "document projection changed before generation CAS"
                    )
            if collection.active_generation_id is not None:
                await session.execute(
                    update(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == collection.active_generation_id,
                    )
                    .values(
                        status="rollback_available",
                        superseded_at=now,
                        rollback_until=now + rollback_retention_seconds,
                    )
                )
            await session.execute(
                update(knowledge_index_generations)
                .where(
                    knowledge_index_generations.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_index_generations.c.generation_id == generation_id,
                    knowledge_index_generations.c.collection_id == collection_id,
                )
                .values(
                    status="active",
                    manifest=dict(expected),
                    validation=validation,
                    activated_at=now,
                    superseded_at=None,
                    rollback_until=None,
                )
            )
            await session.execute(
                update(knowledge_collections)
                .where(
                    knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_collections.c.id == collection_id,
                )
                .values(active_generation_id=generation_id)
            )
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="knowledge_generation.activated",
                resource_type="knowledge_collection",
                resource_id=collection_id,
                scope="knowledge_collections",
            )
        return await self.get_collection(collection_id)

    async def rollback_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        actor_user_id: uuid.UUID | None = None,
        rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> KnowledgeCollection:
        """Switch to a retained generation only if source revisions agree."""
        now = time.time()
        async with self._session() as session:
            collection, access = await self._mutable_collection_row(
                session,
                collection_id,
                actor_user_id=actor_user_id,
                allow_active_maintenance=True,
            )
            target = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if (
                target is None
                or target.status != "rollback_available"
                or target.rollback_until is None
                or float(target.rollback_until) < now
            ):
                raise KnowledgeError("generation is not rollback-available")
            current_manifest = {
                row.id: row.active_revision_id or ""
                for row in (
                    await session.execute(
                        select(
                            knowledge_documents.c.id,
                            knowledge_documents.c.active_revision_id,
                        ).where(
                            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_documents.c.collection_id == collection_id,
                            knowledge_documents.c.lifecycle_status == "active",
                        )
                    )
                ).all()
            }
            if current_manifest != dict(target.manifest or {}):
                raise GenerationManifestChanged(
                    "source revisions changed since this generation was active"
                )
            staged_manifest = {
                row.document_id: row.revision_id or ""
                for row in (
                    await session.execute(
                        select(
                            knowledge_chunks.c.document_id,
                            knowledge_chunks.c.revision_id,
                        )
                        .where(
                            knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_chunks.c.collection_id == collection_id,
                            knowledge_chunks.c.generation_id == generation_id,
                        )
                        .distinct()
                    )
                ).all()
            }
            if staged_manifest != current_manifest:
                raise KnowledgeError("retained generation failed manifest validation")
            if collection.active_generation_id is not None:
                await session.execute(
                    update(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == collection.active_generation_id,
                    )
                    .values(
                        status="rollback_available",
                        superseded_at=now,
                        rollback_until=now + rollback_retention_seconds,
                    )
                )
            await session.execute(
                update(knowledge_index_generations)
                .where(
                    knowledge_index_generations.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_index_generations.c.generation_id == generation_id,
                )
                .values(
                    status="active",
                    activated_at=now,
                    superseded_at=None,
                    rollback_until=None,
                )
            )
            await session.execute(
                update(knowledge_collections)
                .where(
                    knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_collections.c.id == collection_id,
                )
                .values(active_generation_id=generation_id)
            )
            await append_resource_effects(
                session,
                tenant_id=_DEFAULT_TENANT,
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="knowledge_generation.rolled_back",
                resource_type="knowledge_collection",
                resource_id=collection_id,
                scope="knowledge_collections",
            )
        return await self.get_collection(collection_id)

    async def prune_expired_generations(
        self,
        *,
        collection_id: str,
        now: float | None = None,
    ) -> int:
        """Converge expired generation cleanup across DB and vector crashes."""
        cutoff = time.time() if now is None else now
        async with self._session() as session:
            collection = await self._collection_row(
                session, collection_id, for_update=True
            )
            generation_rows = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                        or_(
                            and_(
                                knowledge_index_generations.c.status
                                == "rollback_available",
                                knowledge_index_generations.c.rollback_until.is_not(
                                    None
                                ),
                                knowledge_index_generations.c.rollback_until
                                <= cutoff,
                            ),
                            knowledge_index_generations.c.status.in_(
                                ("deleting", "cleanup_failed")
                            ),
                        ),
                        knowledge_index_generations.c.generation_id
                        != collection.active_generation_id,
                    )
                    .with_for_update()
                )
            ).all()
            generation_ids = [row.generation_id for row in generation_rows]
            if not generation_ids:
                return 0
            for row in generation_rows:
                await session.execute(
                    update(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == row.generation_id,
                    )
                    .values(
                        status="deleting",
                        validation={
                            **dict(row.validation or {}),
                            "cleanup_started_at": cutoff,
                            "cleanup_error_type": None,
                        },
                    )
                )

        removed = 0
        failed: list[str] = []
        for generation_id in generation_ids:
            try:
                async with self._session() as session:
                    collection = await self._collection_row(
                        session, collection_id
                    )
                    generation = (
                        await session.execute(
                            select(knowledge_index_generations).where(
                                knowledge_index_generations.c.tenant_id
                                == _DEFAULT_TENANT,
                                knowledge_index_generations.c.collection_id
                                == collection_id,
                                knowledge_index_generations.c.generation_id
                                == generation_id,
                            )
                        )
                    ).one_or_none()
                    if generation is None or generation.status == "deleted":
                        continue
                    if (
                        generation.status != "deleting"
                        or collection.active_generation_id == generation_id
                    ):
                        raise KnowledgeError(
                            "generation cleanup lost its deletion fence"
                        )
                    chunk_ids = [
                        row.id
                        for row in (
                            await session.execute(
                                select(knowledge_chunks.c.id).where(
                                    knowledge_chunks.c.tenant_id
                                    == _DEFAULT_TENANT,
                                    knowledge_chunks.c.collection_id
                                    == collection_id,
                                    knowledge_chunks.c.generation_id
                                    == generation_id,
                                )
                            )
                        ).all()
                    ]
                    embedding_model = collection.embedding_model
                await self._vectors.delete_generation(
                    embedding_model=embedding_model,
                    collection_id=collection_id,
                    generation_id=generation_id,
                )
                residual = await self._vectors.count_generation(
                    embedding_model=embedding_model,
                    collection_id=collection_id,
                    generation_id=generation_id,
                )
                if residual:
                    raise KnowledgeError(
                        "expired generation vector cleanup left "
                        f"{residual} points"
                    )
                # Migration-era points predate the generation payload, so a
                # generation filter cannot see or remove them.  The canonical
                # chunk manifest is already locked in above; deleting those
                # exact physical ids closes that compatibility gap without a
                # document- or collection-wide fallback.
                await self._vectors.delete_chunks(
                    embedding_model=embedding_model,
                    chunk_ids=chunk_ids,
                )
                exact_residual = await self._vectors.count_chunks(
                    embedding_model=embedding_model,
                    chunk_ids=chunk_ids,
                )
                if exact_residual:
                    raise KnowledgeError(
                        "expired generation exact vector cleanup left "
                        f"{exact_residual} points"
                    )
                async with self._session() as session:
                    collection = await self._collection_row(
                        session, collection_id, for_update=True
                    )
                    generation = (
                        await session.execute(
                            select(knowledge_index_generations)
                            .where(
                                knowledge_index_generations.c.tenant_id
                                == _DEFAULT_TENANT,
                                knowledge_index_generations.c.collection_id
                                == collection_id,
                                knowledge_index_generations.c.generation_id
                                == generation_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if generation is None or generation.status == "deleted":
                        continue
                    if (
                        generation.status != "deleting"
                        or collection.active_generation_id == generation_id
                    ):
                        raise KnowledgeError(
                            "generation cleanup lost its deletion fence"
                        )
                    await session.execute(
                        delete(knowledge_chunks).where(
                            knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_chunks.c.collection_id == collection_id,
                            knowledge_chunks.c.generation_id == generation_id,
                        )
                    )
                    await session.execute(
                        update(knowledge_index_generations)
                        .where(
                            knowledge_index_generations.c.tenant_id
                            == _DEFAULT_TENANT,
                            knowledge_index_generations.c.generation_id
                            == generation_id,
                        )
                        .values(
                            status="deleted",
                            validation={
                                **dict(generation.validation or {}),
                                "cleanup_completed_at": cutoff,
                                "cleanup_error_type": None,
                            },
                        )
                    )
                removed += len(chunk_ids)
            except Exception as exc:  # noqa: BLE001 - persist retryable state
                failed.append(generation_id)
                log.warning(
                    "Expired generation %s cleanup failed (%s)",
                    generation_id,
                    type(exc).__name__,
                    extra={
                        "event": "knowledge.generation.retention.failed",
                        "collection_id": collection_id,
                        "generation_id": generation_id,
                        "error_type": type(exc).__name__,
                    },
                )
                async with self._session() as session:
                    row = (
                        await session.execute(
                            select(knowledge_index_generations)
                            .where(
                                knowledge_index_generations.c.tenant_id
                                == _DEFAULT_TENANT,
                                knowledge_index_generations.c.collection_id
                                == collection_id,
                                knowledge_index_generations.c.generation_id
                                == generation_id,
                            )
                            .with_for_update()
                        )
                    ).one_or_none()
                    if row is not None and row.status in {
                        "deleting",
                        "cleanup_failed",
                    }:
                        await session.execute(
                            update(knowledge_index_generations)
                            .where(
                                knowledge_index_generations.c.tenant_id
                                == _DEFAULT_TENANT,
                                knowledge_index_generations.c.generation_id
                                == generation_id,
                            )
                            .values(
                                status="cleanup_failed",
                                validation={
                                    **dict(row.validation or {}),
                                    "cleanup_failed_at": time.time(),
                                    "cleanup_error_type": type(exc).__name__,
                                },
                            )
                        )
        if failed:
            raise GenerationPruneError(failed)
        return removed

    async def generation_cleanup_collection_ids(
        self,
        *,
        now: float | None = None,
    ) -> list[str]:
        """Return tenant-local collections with due or interrupted cleanup."""
        cutoff = time.time() if now is None else now
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(knowledge_index_generations.c.collection_id)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        or_(
                            and_(
                                knowledge_index_generations.c.status
                                == "rollback_available",
                                knowledge_index_generations.c.rollback_until.is_not(
                                    None
                                ),
                                knowledge_index_generations.c.rollback_until
                                <= cutoff,
                            ),
                            knowledge_index_generations.c.status.in_(
                                ("deleting", "cleanup_failed")
                            ),
                        ),
                    )
                    .distinct()
                )
            ).all()
        return sorted(row.collection_id for row in rows)

    @staticmethod
    async def _assert_indexing_fence(
        session: AsyncSession,
        *,
        job_id: str | None,
        attempt: int | None,
        generation_id: str | None = None,
        document_id: str | None = None,
        revision_id: str | None = None,
        allowed_statuses: tuple[str, ...] = ("running",),
    ) -> None:
        """Lock and validate durable worker authority in the write transaction."""
        if job_id is None and attempt is None:
            return
        generation_fence = generation_id is not None
        revision_fence = document_id is not None and revision_id is not None
        if (
            job_id is None
            or attempt is None
            or generation_fence == revision_fence
        ):
            raise IndexGenerationSuperseded("incomplete indexing fence")
        row = (
            await session.execute(
                select(
                    indexing_jobs.c.status,
                    indexing_jobs.c.attempt,
                    indexing_jobs.c.generation_id,
                    indexing_jobs.c.operation_kind,
                    indexing_jobs.c.document_id,
                    indexing_jobs.c.revision_id,
                    indexing_jobs.c.cancel_requested,
                )
                .where(indexing_jobs.c.job_id == job_id)
                .with_for_update()
            )
        ).one_or_none()
        if (
            row is None
            or row.status not in allowed_statuses
            or int(row.attempt) != int(attempt)
            or (
                generation_fence
                and (
                    row.operation_kind != "collection_generation"
                    or row.generation_id != generation_id
                )
            )
            or (
                revision_fence
                and (
                    row.operation_kind != "document_revision"
                    or row.document_id != document_id
                    or row.revision_id != revision_id
                )
            )
            or bool(row.cancel_requested)
        ):
            raise IndexGenerationSuperseded(
                f"indexing attempt {job_id}:{attempt} no longer owns publication"
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
        """Remove only unpublished chunks for a cancelled/superseded build."""
        async with self._session() as session:
            # Internal cleanup is authority-independent: a permission revoke
            # may stop publication, but must never strand staged vectors.
            collection = await self._collection_row(
                session, collection_id, for_update=True
            )
            await self._assert_indexing_fence(
                session,
                job_id=fence_job_id,
                attempt=fence_attempt,
                generation_id=generation_id,
                allowed_statuses=("running", "cancelling"),
            )
            if collection.active_generation_id == generation_id:
                raise KnowledgeError("the active generation cannot be discarded")
            generation = (
                await session.execute(
                    select(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                        knowledge_index_generations.c.collection_id
                        == collection_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if generation is not None and generation.status not in {
                "building",
                "deleted",
            }:
                raise KnowledgeError(
                    "only an unpublished generation can be discarded"
                )
            chunk_ids = [
                row.id
                for row in (
                    await session.execute(
                        select(knowledge_chunks.c.id).where(
                            knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                            knowledge_chunks.c.collection_id == collection_id,
                            knowledge_chunks.c.generation_id == generation_id,
                        )
                    )
                ).all()
            ]
            await self._vectors.delete_generation(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
            )
            residual = await self._vectors.count_generation(
                embedding_model=collection.embedding_model,
                collection_id=collection_id,
                generation_id=generation_id,
            )
            if residual:
                raise KnowledgeError(
                    f"generation discard left {residual} vector points"
                )
            await session.execute(
                delete(knowledge_chunks).where(
                    knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                    knowledge_chunks.c.collection_id == collection_id,
                    knowledge_chunks.c.generation_id == generation_id,
                )
            )
            if generation is not None:
                await session.execute(
                    update(knowledge_index_generations)
                    .where(
                        knowledge_index_generations.c.tenant_id
                        == _DEFAULT_TENANT,
                        knowledge_index_generations.c.generation_id
                        == generation_id,
                    )
                    .values(status="deleted", superseded_at=time.time())
                )
        return len(chunk_ids)

    # -- retrieval --------------------------------------------------------- #

    async def search(
        self,
        *,
        query_embedding: list[float],
        collection_ids: list[str] | None,
        top_k: int,
        embedding_model: str | None = None,
    ) -> list[RetrievalCandidate]:
        """Dense retrieval: vector index ranks, Postgres hydrates."""
        model, scopes = await self._resolve_scope(embedding_model, collection_ids)
        if not scopes or top_k <= 0:
            return []
        max_depth = bounded_candidate_depth(top_k)
        depth = min(max(1, top_k), max_depth)
        previous_ids: tuple[str, ...] = ()
        snapshot_retry_used = False
        while True:
            hits = await self._vectors.search(
                embedding_model=model,
                query_embedding=query_embedding,
                scopes=scopes,
                top_k=depth,
            )
            hydrated = await self._hydrate(hits, scopes=scopes)
            if (
                not snapshot_retry_used
                and len(hydrated) < top_k
                and (
                    not hits
                    or len(hydrated) < len(hits)
                    or len(hits) < depth
                )
            ):
                refreshed_model, refreshed_scopes = await self._resolve_scope(
                    embedding_model, collection_ids
                )
                snapshot_retry_used = True
                if (refreshed_model, refreshed_scopes) != (model, scopes):
                    _log_snapshot_advanced_retry(
                        retrieval_mode="dense",
                        old_scopes=scopes,
                        new_scopes=refreshed_scopes,
                    )
                    model, scopes = refreshed_model, refreshed_scopes
                    if not scopes:
                        return []
                    depth = min(max(1, top_k), max_depth)
                    previous_ids = ()
                    # Discard every candidate from the old snapshot.  The next
                    # iteration re-ranks and hydrates only the new generation.
                    continue
            if len(hydrated) >= top_k:
                return hydrated[:top_k]
            current_ids = tuple(hit.chunk_id for hit in hits)
            if previous_ids and current_ids == previous_ids:
                log.warning(
                    "knowledge search exhausted/capped after %d vector hits; "
                    "only %d canonically active, verified hits remain",
                    len(hits),
                    len(hydrated),
                    extra={
                        "event": "knowledge.retrieval.degraded",
                        "stage": "vector_candidate_pool",
                        "retrieval_mode": "dense",
                        "degradation_reason": "vector_candidate_stalled",
                        "candidate_cap": len(hits),
                        "requested_candidate_pool": top_k,
                        "returned_candidate_pool": len(hydrated),
                        "final_top_k": top_k,
                        "requested_top_k": top_k,
                        "active_verified_hits": len(hydrated),
                    },
                )
                return degraded_candidates(
                    hydrated,
                    reason="vector_candidate_stalled",
                    retrieval_mode="dense",
                    requested_candidate_pool=top_k,
                    candidate_cap=len(hits),
                )
            if len(hits) < depth:
                return hydrated[:top_k]
            previous_ids = current_ids
            if depth >= max_depth:
                log.warning(
                    "knowledge search degraded: vector_overfetch_cap=%d "
                    "reached; requested=%d active_verified=%d",
                    max_depth,
                    top_k,
                    len(hydrated),
                    extra={
                        "event": "knowledge.retrieval.degraded",
                        "stage": "vector_candidate_pool",
                        "retrieval_mode": "dense",
                        "degradation_reason": "vector_overfetch_cap",
                        "candidate_cap": max_depth,
                        "requested_candidate_pool": top_k,
                        "returned_candidate_pool": len(hydrated),
                        "final_top_k": top_k,
                        "requested_top_k": top_k,
                        "active_verified_hits": len(hydrated),
                    },
                )
                return degraded_candidates(
                    hydrated,
                    reason="vector_overfetch_cap",
                    retrieval_mode="dense",
                    requested_candidate_pool=top_k,
                    candidate_cap=max_depth,
                )
            depth = min(depth * 2, max_depth)

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_embedding: list[float],
        collection_ids: list[str] | None,
        top_k: int,
        embedding_model: str | None = None,
    ) -> list[RetrievalCandidate]:
        """Fused dense + sparse retrieval, Postgres-hydrated."""
        model, scopes = await self._resolve_scope(embedding_model, collection_ids)
        if not scopes or top_k <= 0:
            return []
        max_depth = bounded_candidate_depth(top_k)
        depth = min(max(1, top_k), max_depth)
        previous_ids: tuple[str, ...] = ()
        snapshot_retry_used = False
        while True:
            hits = await self._vectors.hybrid_search(
                embedding_model=model,
                query_text=query_text,
                query_embedding=query_embedding,
                scopes=scopes,
                top_k=depth,
            )
            hydrated = await self._hydrate(hits, scopes=scopes)
            if (
                not snapshot_retry_used
                and len(hydrated) < top_k
                and (
                    not hits
                    or len(hydrated) < len(hits)
                    or len(hits) < depth
                )
            ):
                refreshed_model, refreshed_scopes = await self._resolve_scope(
                    embedding_model, collection_ids
                )
                snapshot_retry_used = True
                if (refreshed_model, refreshed_scopes) != (model, scopes):
                    _log_snapshot_advanced_retry(
                        retrieval_mode="hybrid",
                        old_scopes=scopes,
                        new_scopes=refreshed_scopes,
                    )
                    model, scopes = refreshed_model, refreshed_scopes
                    if not scopes:
                        return []
                    depth = min(max(1, top_k), max_depth)
                    previous_ids = ()
                    continue
            if len(hydrated) >= top_k:
                return hydrated[:top_k]
            current_ids = tuple(hit.chunk_id for hit in hits)
            if previous_ids and current_ids == previous_ids:
                log.warning(
                    "knowledge hybrid search exhausted/capped after %d vector "
                    "hits; only %d canonically active, verified hits remain",
                    len(hits),
                    len(hydrated),
                    extra={
                        "event": "knowledge.retrieval.degraded",
                        "stage": "vector_candidate_pool",
                        "retrieval_mode": "hybrid",
                        "degradation_reason": "vector_candidate_stalled",
                        "candidate_cap": len(hits),
                        "requested_candidate_pool": top_k,
                        "returned_candidate_pool": len(hydrated),
                        "final_top_k": top_k,
                        "requested_top_k": top_k,
                        "active_verified_hits": len(hydrated),
                    },
                )
                return degraded_candidates(
                    hydrated,
                    reason="vector_candidate_stalled",
                    retrieval_mode="hybrid",
                    requested_candidate_pool=top_k,
                    candidate_cap=len(hits),
                )
            if len(hits) < depth:
                return hydrated[:top_k]
            previous_ids = current_ids
            if depth >= max_depth:
                log.warning(
                    "knowledge hybrid search degraded: "
                    "vector_overfetch_cap=%d reached; requested=%d "
                    "active_verified=%d",
                    max_depth,
                    top_k,
                    len(hydrated),
                    extra={
                        "event": "knowledge.retrieval.degraded",
                        "stage": "vector_candidate_pool",
                        "retrieval_mode": "hybrid",
                        "degradation_reason": "vector_overfetch_cap",
                        "candidate_cap": max_depth,
                        "requested_candidate_pool": top_k,
                        "returned_candidate_pool": len(hydrated),
                        "final_top_k": top_k,
                        "requested_top_k": top_k,
                        "active_verified_hits": len(hydrated),
                    },
                )
                return degraded_candidates(
                    hydrated,
                    reason="vector_overfetch_cap",
                    retrieval_mode="hybrid",
                    requested_candidate_pool=top_k,
                    candidate_cap=max_depth,
                )
            depth = min(depth * 2, max_depth)

    # -- internals --------------------------------------------------------- #

    async def _collection_row(
        self, session, collection_id: str, *, for_update: bool = False
    ):
        statement = select(knowledge_collections).where(
            knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
            knowledge_collections.c.id == collection_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise CollectionNotFound(collection_id)
        return row

    async def _mutable_collection_row(
        self,
        session,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None,
        owner_only: bool = False,
        allow_active_maintenance: bool = False,
    ):
        """Lock and authorize one collection mutation.

        Normal source writes remain available during a shadow rebuild.  The
        generation publisher validates an exact revision manifest and folds
        concurrent changes into a delta before its pointer CAS.
        """
        access = await lock_resource_access(
            session,
            tenant_id=_DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            resource_type="knowledge_collection",
            resource_table=knowledge_collections,
            id_column=knowledge_collections.c.id,
            resource_id=collection_id,
            owner_column=knowledge_collections.c.created_by_user_id,
            minimum=(
                SharePermission.VIEW if owner_only else SharePermission.EDIT
            ),
            restrict_to_workspace_members=(
                self._restrict_to_workspace_members
            ),
            sharing_enabled=self._sharing_enabled,
            owner_only=owner_only,
        )
        if access is None:
            raise CollectionNotFound(collection_id)
        return await self._collection_row(session, collection_id), access

    async def _document_row(
        self,
        session,
        document_id: str,
        *,
        for_update: bool = False,
    ):
        statement = select(knowledge_documents).where(
            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
            knowledge_documents.c.id == document_id,
        )
        if for_update:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).one_or_none()
        if row is None:
            raise DocumentNotFound(document_id)
        return row

    async def _delete_document_with_locked_collection(
        self,
        session,
        *,
        document_id: str,
        expected_collection_id: str,
        collection,
        owner_user_id: uuid.UUID | None,
        actor_user_id: uuid.UUID | None,
    ) -> None:
        row = await self._document_row(session, document_id, for_update=True)
        if row.collection_id != expected_collection_id:
            raise DocumentNotFound(document_id)
        result = await session.execute(
            delete(knowledge_documents).where(
                knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                knowledge_documents.c.id == document_id,
            )
        )
        if not result.rowcount:
            raise DocumentNotFound(document_id)
        await append_resource_effects(
            session,
            tenant_id=_DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            owner_user_id=owner_user_id,
            action="knowledge_document.deleted",
            resource_type="knowledge_collection",
            resource_id=row.collection_id,
            scope="knowledge_collections",
        )
        await self._vectors.delete_document(
            embedding_model=collection.embedding_model,
            document_id=document_id,
        )

    async def _document_counts(
        self, session, collection_id: str | None = None
    ) -> dict[str, int]:
        statement = select(
            knowledge_documents.c.collection_id,
            func.count(knowledge_documents.c.id),
        ).where(
            knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
            knowledge_documents.c.lifecycle_status == "active",
        )
        if collection_id is not None:
            statement = statement.where(
                knowledge_documents.c.collection_id == collection_id
            )
        statement = statement.group_by(knowledge_documents.c.collection_id)
        rows = (await session.execute(statement)).all()
        return {row[0]: int(row[1]) for row in rows}

    def _collection_from_row(self, row, counts: dict[str, int]) -> KnowledgeCollection:
        return KnowledgeCollection(
            id=row.id,
            name=row.name,
            embedding_model=row.embedding_model,
            embedding_dim=row.embedding_dim,
            created_at=row.created_at,
            document_count=counts.get(row.id, 0),
            tenant_id=row.tenant_id,
            created_by_user_id=row.created_by_user_id,
            active_generation_id=row.active_generation_id,
        )

    def _document_from_row(self, row) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=row.id,
            collection_id=row.collection_id,
            title=row.title,
            text=row.text,
            metadata=dict(row.metadata or {}),
            chunk_count=row.chunk_count,
            created_at=row.created_at,
            source_id=row.source_id,
            source_owner_user_id=row.source_owner_user_id,
            source_workspace_id=row.source_workspace_id,
            source_scope_bound=bool(row.source_scope_bound),
            desired_revision_id=row.desired_revision_id,
            active_revision_id=row.active_revision_id,
            desired_sequence=row.desired_sequence,
            lifecycle_status=row.lifecycle_status,
        )

    @staticmethod
    def _validate_embeddings(
        chunks: list[str], embeddings: list[list[float]], embedding_dim: int
    ) -> None:
        if len(chunks) != len(embeddings):
            raise EmbeddingDimensionMismatch(
                f"chunk/embedding count mismatch: {len(chunks)} chunks vs "
                f"{len(embeddings)} embeddings"
            )
        for index, embedding in enumerate(embeddings):
            if len(embedding) != embedding_dim:
                raise EmbeddingDimensionMismatch(
                    f"chunk {index} has dimension {len(embedding)}, "
                    f"collection requires {embedding_dim}"
                )

    @staticmethod
    def _build_chunk_rows(
        *,
        document_id: str,
        collection_id: str,
        chunks: list[str],
        source_chunks: list[str] | None,
        retrieval_contexts: list[str | None] | None,
        source_spans: list[tuple[int, int]] | None,
        document_content_hash: str | None,
        revision_id: str | None,
        generation_id: str | None,
        created_at: float,
        page_numbers: list[int | None] | None = None,
        reuse_ids: list[str] | None = None,
    ) -> list[dict]:
        sources = source_chunks or []
        contexts = retrieval_contexts or []
        spans = source_spans or []
        pages = page_numbers or []
        prior_ids = reuse_ids or []
        identity_content_hash = document_content_hash
        if identity_content_hash is None:
            digest = hashlib.sha256()
            for item in sources or chunks:
                encoded = item.encode("utf-8")
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
            identity_content_hash = digest.hexdigest()
        return [
            {
                "id": (
                    prior_ids[index]
                    if index < len(prior_ids)
                    else deterministic_chunk_id(
                        document_id=document_id,
                        generation_id=generation_id,
                        revision_id=revision_id,
                        content_hash=identity_content_hash,
                        chunk_index=index,
                    )
                ),
                "document_id": document_id,
                "collection_id": collection_id,
                "tenant_id": _DEFAULT_TENANT,
                "chunk_index": index,
                "text": chunk_text,
                "source_text": (
                    sources[index] if index < len(sources) else ""
                ),
                "retrieval_context": (
                    contexts[index] if index < len(contexts) else None
                ),
                "source_start": spans[index][0] if index < len(spans) else None,
                "source_end": spans[index][1] if index < len(spans) else None,
                "document_content_hash": document_content_hash,
                "revision_id": revision_id,
                "generation_id": generation_id,
                "page_number": pages[index] if index < len(pages) else None,
                "created_at": created_at,
            }
            for index, chunk_text in enumerate(chunks)
        ]

    async def _sync_vectors(
        self,
        *,
        session: AsyncSession,
        embedding_model: str,
        collection_id: str,
        document_id: str,
        chunk_rows: list[dict],
        embeddings: list[list[float]],
    ) -> None:
        """Upsert vectors and mark the locked canonical row synchronized.

        The caller owns the collection-row transaction. A failure propagates
        and rolls back the canonical mutation; reindex submission cannot pass
        that row lock while the vector side effect is still in flight.
        """
        vectors = [
            ChunkVector(
                chunk_id=row["id"],
                dense=tuple(embedding),
                text=row["text"],
                generation_id=row["generation_id"],
                revision_id=row["revision_id"],
            )
            for row, embedding in zip(chunk_rows, embeddings)
        ]
        await self._vectors.upsert(
            embedding_model=embedding_model,
            collection_id=collection_id,
            document_id=document_id,
            vectors=vectors,
        )
        await session.execute(
            update(knowledge_documents)
            .where(
                knowledge_documents.c.tenant_id == _DEFAULT_TENANT,
                knowledge_documents.c.id == document_id,
            )
            .values(vector_synced=True)
        )

    async def _resolve_scope(
        self, embedding_model: str | None, collection_ids: list[str] | None
    ) -> tuple[str, list[VectorSearchScope]]:
        """Resolve one atomic active generation/revision search snapshot.

        The vector index is per-model and needs concrete ids; an unscoped
        search (``collection_ids is None``) expands to every collection
        of the resolved model.  Collection pointers and active document
        revisions are read in one SQL statement so the vector filter cannot
        combine states from opposite sides of a concurrent pointer swap.
        """
        requested_ids = (
            list(dict.fromkeys(collection_ids))
            if collection_ids is not None
            else None
        )
        if requested_ids == []:
            return embedding_model or "", []
        if requested_ids is None and embedding_model is None:
            return "", []

        scope_filter = (
            knowledge_collections.c.id.in_(requested_ids)
            if requested_ids is not None
            else knowledge_collections.c.embedding_model == embedding_model
        )
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(
                        knowledge_collections.c.id,
                        knowledge_collections.c.embedding_model,
                        knowledge_collections.c.active_generation_id,
                        knowledge_documents.c.id,
                        knowledge_documents.c.active_revision_id,
                        knowledge_index_generations.c.build_contract_hash,
                        knowledge_chunks.c.id,
                    )
                    .select_from(
                        knowledge_collections.outerjoin(
                            knowledge_documents,
                            and_(
                                knowledge_documents.c.collection_id
                                == knowledge_collections.c.id,
                                knowledge_documents.c.tenant_id
                                == knowledge_collections.c.tenant_id,
                                knowledge_documents.c.lifecycle_status == "active",
                            ),
                        ).outerjoin(
                            knowledge_index_generations,
                            and_(
                                knowledge_index_generations.c.tenant_id
                                == knowledge_collections.c.tenant_id,
                                knowledge_index_generations.c.collection_id
                                == knowledge_collections.c.id,
                                knowledge_index_generations.c.generation_id
                                == knowledge_collections.c.active_generation_id,
                            ),
                        ).outerjoin(
                            knowledge_chunks,
                            and_(
                                knowledge_chunks.c.tenant_id
                                == knowledge_collections.c.tenant_id,
                                knowledge_chunks.c.collection_id
                                == knowledge_collections.c.id,
                                knowledge_chunks.c.document_id
                                == knowledge_documents.c.id,
                                knowledge_chunks.c.generation_id
                                == knowledge_collections.c.active_generation_id,
                                knowledge_chunks.c.revision_id
                                == knowledge_documents.c.active_revision_id,
                                knowledge_index_generations.c.build_contract_hash
                                == "legacy-unverified-build",
                                knowledge_chunks.c.revision_id.like(
                                    "rev_legacy_%"
                                ),
                                knowledge_chunks.c.source_text != "",
                                knowledge_chunks.c.source_start.is_not(None),
                                knowledge_chunks.c.source_end.is_not(None),
                                knowledge_chunks.c.source_start >= 0,
                                knowledge_chunks.c.source_end
                                > knowledge_chunks.c.source_start,
                                knowledge_chunks.c.document_content_hash.is_not(
                                    None
                                ),
                                knowledge_chunks.c.document_content_hash != "",
                            ),
                        )
                    )
                    .where(
                        knowledge_collections.c.tenant_id == _DEFAULT_TENANT,
                        scope_filter,
                    )
                )
            ).all()
        collections: dict[str, tuple[str, str | None, bool]] = {}
        revision_ids: dict[str, list[str]] = {}
        legacy_document_ids: dict[str, list[str]] = {}
        legacy_payload_chunk_ids: dict[str, list[str]] = {}
        for (
            collection_id,
            model,
            generation_id,
            document_id,
            revision_id,
            build_contract_hash,
            legacy_chunk_id,
        ) in rows:
            legacy_compatibility = (
                build_contract_hash == "legacy-unverified-build"
            )
            collections[collection_id] = (
                model,
                generation_id,
                legacy_compatibility,
            )
            if document_id is None:
                continue
            if revision_id is None:
                legacy_document_ids.setdefault(collection_id, []).append(document_id)
            else:
                revision_ids.setdefault(collection_id, []).append(revision_id)
            if legacy_compatibility and legacy_chunk_id is not None:
                legacy_payload_chunk_ids.setdefault(collection_id, []).append(
                    legacy_chunk_id
                )
        if requested_ids is not None:
            # Parity with the memory + sole-store contract: an unknown
            # explicit id is CollectionNotFound, a mixed-model scope is a
            # hard error — never a silently narrowed result set.
            for cid in requested_ids:
                if cid not in collections:
                    raise CollectionNotFound(cid)
            scoped_models = {collections[cid][0] for cid in requested_ids}
            if len(scoped_models) > 1:
                raise KnowledgeError(
                    "scoped collections use different embedding models "
                    f"({sorted(scoped_models)}); query one model scope at a time"
                )
            resolved_model = collections[requested_ids[0]][0]
            if embedding_model is not None and embedding_model != resolved_model:
                raise KnowledgeError(
                    "scoped collection embedding model does not match the "
                    f"requested model ({embedding_model!r} != {resolved_model!r})"
                )
            model = embedding_model or resolved_model
            selected_ids = requested_ids
        else:
            assert embedding_model is not None
            model = embedding_model
            selected_ids = list(collections)
        scopes = [
            VectorSearchScope(
                collection_id=collection_id,
                generation_id=collections[collection_id][1],
                active_revision_ids=tuple(
                    sorted(set(revision_ids.get(collection_id, ())))
                ),
                legacy_document_ids=tuple(
                    sorted(set(legacy_document_ids.get(collection_id, ())))
                ),
                legacy_payload_chunk_ids=(
                    tuple(
                        sorted(
                            set(
                                legacy_payload_chunk_ids.get(
                                    collection_id, ()
                                )
                            )
                        )
                    )
                    if collections[collection_id][2]
                    else ()
                ),
            )
            for collection_id in selected_ids
        ]
        return model, scopes

    async def _hydrate(
        self,
        hits,
        *,
        scopes: list[VectorSearchScope],
    ) -> RetrievalCandidateBatch:
        """Hydrate against the same active-pointer snapshot used for ranking."""
        if not hits:
            return RetrievalCandidateBatch()
        scope_predicates = []
        for scope in scopes:
            revision_predicates = []
            if scope.active_revision_ids:
                revision_predicates.append(
                    knowledge_chunks.c.revision_id.in_(scope.active_revision_ids)
                )
            if scope.legacy_document_ids:
                revision_predicates.append(
                    and_(
                        knowledge_chunks.c.document_id.in_(
                            scope.legacy_document_ids
                        ),
                        knowledge_chunks.c.revision_id.is_(None),
                    )
                )
            if not revision_predicates:
                continue
            generation_predicate = (
                knowledge_chunks.c.generation_id.is_(None)
                if scope.generation_id is None
                else knowledge_chunks.c.generation_id == scope.generation_id
            )
            scope_predicates.append(
                and_(
                    knowledge_chunks.c.collection_id == scope.collection_id,
                    generation_predicate,
                    or_(*revision_predicates),
                )
            )
        if not scope_predicates:
            return RetrievalCandidateBatch()
        chunk_ids = [hit.chunk_id for hit in hits]
        async with self._session() as session:
            rows = (
                await session.execute(
                    select(
                        knowledge_chunks.c.id,
                        knowledge_chunks.c.document_id,
                        knowledge_chunks.c.collection_id,
                        knowledge_chunks.c.chunk_index,
                        knowledge_chunks.c.text,
                        knowledge_chunks.c.source_text,
                        knowledge_chunks.c.retrieval_context,
                        knowledge_chunks.c.source_start,
                        knowledge_chunks.c.source_end,
                        knowledge_chunks.c.document_content_hash,
                        knowledge_chunks.c.revision_id,
                        knowledge_chunks.c.generation_id,
                        knowledge_chunks.c.page_number,
                        func.coalesce(
                            knowledge_document_revisions.c.title,
                            knowledge_documents.c.title,
                        ),
                        func.coalesce(
                            knowledge_document_revisions.c.text,
                            knowledge_documents.c.text,
                        ),
                    )
                    .select_from(
                        knowledge_chunks.join(
                            knowledge_documents,
                            knowledge_chunks.c.document_id
                            == knowledge_documents.c.id,
                        ).outerjoin(
                            knowledge_document_revisions,
                            and_(
                                knowledge_document_revisions.c.tenant_id
                                == knowledge_chunks.c.tenant_id,
                                knowledge_document_revisions.c.document_id
                                == knowledge_chunks.c.document_id,
                                knowledge_document_revisions.c.revision_id
                                == knowledge_chunks.c.revision_id,
                            ),
                        )
                    )
                    .where(
                        knowledge_chunks.c.tenant_id == _DEFAULT_TENANT,
                        knowledge_chunks.c.id.in_(chunk_ids),
                        knowledge_documents.c.lifecycle_status == "active",
                        or_(*scope_predicates),
                    )
                )
            ).all()
        by_chunk = {row[0]: row for row in rows}
        candidates: list[RetrievalCandidate] = []
        missing_canonical_count = 0
        unverified_source_count = 0
        duplicate_document_count = 0
        # content hash -> first document that contributed it. Rank order
        # is already best-first, so the winner is the strongest hit.
        seen_content_hashes: dict[str, str] = {}
        for hit in hits:
            row = by_chunk.get(hit.chunk_id)
            if row is None:
                # Vector index ahead of canonical (mid-reconcile or a chunk
                # deleted between search and hydrate); skip rather than emit
                # a hit we cannot cite — but visibly (No Silent Fallbacks).
                log.warning(
                    "knowledge hydrate: chunk %s ranked by the vector index "
                    "but absent in Postgres (mid-reconcile); skipping",
                    hit.chunk_id,
                )
                missing_canonical_count += 1
                continue
            source_verified = source_excerpt_is_verified(
                canonical_text=row[14],
                source_text=row[5],
                source_start=row[7],
                source_end=row[8],
                document_content_hash=row[9],
            )
            if not source_verified:
                log.warning(
                    "knowledge hydrate: chunk %s failed canonical source-span "
                    "verification; excluding it until a verified rebuild",
                    hit.chunk_id,
                )
                unverified_source_count += 1
                continue
            # One document per content hash within a retrieval. Two files with
            # different names are two documents by design (source-bound reuse),
            # so an identical copy otherwise fills several evidence slots with
            # the same passages. Deliberately document-level, not passage-
            # level: two DIFFERENT documents quoting the same clause both stay
            # visible, because "both say it" is itself information.
            content_hash = row[9]
            if content_hash:
                first_document = seen_content_hashes.get(content_hash)
                if first_document is None:
                    seen_content_hashes[content_hash] = row[1]
                elif first_document != row[1]:
                    duplicate_document_count += 1
                    continue
            candidates.append(
                RetrievalCandidate(
                    chunk=DocumentChunk(
                        id=row[0],
                        document_id=row[1],
                        collection_id=row[2],
                        chunk_index=row[3],
                        text=row[4],
                        source_text=row[5],
                        retrieval_context=row[6],
                        source_start=row[7],
                        source_end=row[8],
                        document_content_hash=row[9],
                        revision_id=row[10],
                        generation_id=row[11],
                        page_number=row[12],
                        source_verified=source_verified,
                    ),
                    score=hit.score,
                    document_title=row[13],
                )
            )
        exclusions: list[RetrievalExclusion] = []
        if duplicate_document_count:
            exclusions.append(
                RetrievalExclusion(
                    reason="duplicate_document",
                    stage="canonical_hydration",
                    count=duplicate_document_count,
                    recommended_action=None,
                )
            )
        if unverified_source_count:
            exclusions.append(
                RetrievalExclusion(
                    reason="source_unverified",
                    stage="canonical_hydration",
                    count=unverified_source_count,
                    recommended_action="reindex",
                )
            )
        if missing_canonical_count:
            exclusions.append(
                RetrievalExclusion(
                    reason="canonical_chunk_unavailable",
                    stage="canonical_hydration",
                    count=missing_canonical_count,
                    recommended_action="reconcile",
                )
            )
        return RetrievalCandidateBatch(
            candidates,
            exclusions=tuple(exclusions),
        )
