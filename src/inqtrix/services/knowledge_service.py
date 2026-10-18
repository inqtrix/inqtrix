"""Collection/document lifecycle, revision preparation, and search.

The service owns what the knowledge routers delegate: validation of
collection/document payloads, immutable source-revision reservation, the
shared chunk/context/embed pipeline, compare-and-swap publication, and scoped
retrieval. Direct service callers may execute a revision synchronously; HTTP
ingestion reserves it first and completes the same pipeline through a durable
indexing operation.

Collections carry a canonical owner UUID. Ownerless legacy collections are
visible only in anonymous/static modes. Accepted direct shares authorize
scoped reads and edits through a live lookup; documents inherit the parent
collection decision. Deleting a collection stays owner-only. Every denial is
the indistinct :class:`CollectionNotFound`/:class:`DocumentNotFound`
(existence is not disclosed).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import AccessMode, ResourceAccess, SharePermission
from inqtrix.embedding_cards import resolve_embedding_card
from inqtrix.knowledge.chunking import ChunkSlice, chunk_text_slices
from inqtrix.knowledge.contextualize import (
    CONTEXT_OMITTED_CONTENT_FILTER,
    ContextualizationBatchCheckpoint,
)
from inqtrix.prompts import CHUNK_CONTEXT_PROMPT_VERSION
from inqtrix.knowledge.embedding_batches import embed_in_slices
from inqtrix.knowledge.page_mapping import infer_chunk_pages
from inqtrix.knowledge.parsing import extract_file, extraction_facts
from inqtrix.knowledge.source_cleanup import SourceCleanupPlan
from inqtrix.quota.models import estimate_tokens
from inqtrix.source_authority import SourceDeletionPermit

log = logging.getLogger("inqtrix")
from inqtrix.knowledge.retrieval import retrieve

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inqtrix.contextualization_circuit import ContextualizationCircuitView
    from inqtrix.knowledge.errors import KnowledgeError
    from inqtrix.auth.permissions import AuthorizationService
    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.source_authority import SourceScope
    from inqtrix.user_events import ResourceInvalidator
    from inqtrix.knowledge.parsing import DocumentParser
from inqtrix.knowledge.stores.ports import (
    CollectionMaintenanceActive,
    CollectionNotFound,
    DocumentChunk,
    DocumentNotFound,
    KnowledgeCollection,
    KnowledgeDocument,
    KnowledgeProviderContext,
    RetrievalCandidate,
    RetrievalDegradation,
    RetrievalExclusion,
    DocumentRevisionReservation,
    GenerationBuildValidation,
    GenerationDocumentValidation,
    GenerationManifestChanged,
    GenerationPruneError,
)


def canonical_source_id(metadata: dict[str, Any] | None) -> str | None:
    """Resolve one stable source identity without conflating file layers.

    ``fileId`` is the historical Research Desk asset id and therefore belongs
    to the asset lifecycle authority. ``file_id`` is the server FileRegistry
    id used by the synchronous compatibility route; it has no AssetRecord and
    must remain a separately namespaced, self-registering source. Callers that
    already provide the canonical ``source_id`` always win unchanged.
    """
    values = metadata or {}
    explicit = values.get("source_id")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    asset_id = values.get("fileId")
    if isinstance(asset_id, str) and asset_id.strip():
        value = asset_id.strip()
        return value if value.startswith("asset:") else f"asset:{value}"
    file_id = values.get("file_id")
    if isinstance(file_id, str) and file_id.strip():
        value = file_id.strip()
        return value if value.startswith("file:") else f"file:{value}"
    return None


@dataclass(frozen=True)
class SearchOutcome:
    """A scoped search result plus the visibility it silently applied.

    The debug search endpoint filters an explicit ``collection_ids``
    list to its visible members instead of rejecting the whole request
    (unlike the strict ask-path gate). That filtering must not be
    invisible to an agent planning against the results, so the outcome
    reports which requested ids were dropped — the router renders them
    as a ``collections_filtered`` warning (No Silent Fallbacks).

    Attributes:
        candidates: The scored hits over the searched collections.
        filtered_collection_ids: Requested ids the caller may not see
            (empty for an unscoped caller or when nothing was dropped).
        unverified_chunk_ids: Ranked legacy chunks excluded because their
            original source span cannot be verified.
        retrieval_exclusions: Text-free aggregate findings emitted by a store
            before unsafe points can become candidates. This is how canonical
            Postgres hydration reports rejected vector hits without exposing
            their synthetic text or internal identifiers.
        retrieval_degradations: Known technical boundaries that stopped
            canonical hydration before the requested candidate pool was
            filled, projected onto the independent final result width.
            Genuine corpus exhaustion is intentionally absent.
    """

    candidates: list[RetrievalCandidate]
    filtered_collection_ids: list[str] = field(default_factory=list)
    unverified_chunk_ids: list[str] = field(default_factory=list)
    retrieval_exclusions: list[RetrievalExclusion] = field(default_factory=list)
    retrieval_degradations: list[RetrievalDegradation] = field(
        default_factory=list
    )

    def exclusion_count(self, reason: str) -> int:
        """Return the bounded aggregate for one structured exclusion reason."""

        return sum(
            exclusion.count
            for exclusion in self.retrieval_exclusions
            if exclusion.reason == reason
        )


@dataclass(frozen=True)
class EmbeddingWorkReceipt:
    """Stable accounting facts for the exact texts sent to embeddings."""

    amount: int
    input_count: int
    input_sha256: str


def _embedding_work_receipt(embedding_texts: list[str]) -> EmbeddingWorkReceipt:
    encoded = json.dumps(
        embedding_texts,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return EmbeddingWorkReceipt(
        amount=sum(estimate_tokens(text) for text in embedding_texts),
        input_count=len(embedding_texts),
        input_sha256=hashlib.sha256(encoded).hexdigest(),
    )


@dataclass(frozen=True)
class EmbeddedDocument:
    """One fully prepared, unpublished document revision."""

    embedding_texts: list[str]
    embeddings: list[list[float]]
    source_slices: list[ChunkSlice]
    retrieval_contexts: list[str | None]
    content_hash: str
    contextualization_marker: str | None
    contextualization_batches: int
    # Chunks indexed without context: the provider's content filter blocked
    # their batch.
    context_omitted_chunks: int = 0

    @property
    def source_texts(self) -> list[str]:
        return [item.text for item in self.source_slices]

    def spans_for(self, canonical_text: str) -> list[tuple[int, int]]:
        return [item.utf8_span(canonical_text) for item in self.source_slices]

    @property
    def work_receipt(self) -> EmbeddingWorkReceipt:
        """Accounting for the already materialized provider input."""

        return _embedding_work_receipt(self.embedding_texts)


@dataclass(frozen=True)
class ReembeddedDocument:
    """A staged generation document and its exact embedding-work receipt."""

    document: KnowledgeDocument
    work_receipt: EmbeddingWorkReceipt
    # Chunks re-embedded without context (content filter); see EmbeddedDocument.
    context_omitted_chunks: int = 0


@dataclass(frozen=True)
class PreparedDocumentRevision:
    """One provider-complete revision that has not crossed the store CAS."""

    reservation: DocumentRevisionReservation
    title: str
    text: str
    metadata: dict[str, Any]
    embedded: EmbeddedDocument | None
    page_numbers: list[int | None] | None
    already_published: KnowledgeDocument | None = None


class KnowledgeValidationError(ValueError):
    """Raised for client-payload problems (maps to HTTP 400)."""


class SourceDocumentResolutionConflict(KnowledgeValidationError):
    """A legacy source maps to more than one active document in a collection."""


class ChunkNotFound(KeyError):
    """Raised when a visible document has no chunk at the given index.

    Service-level sibling of the store's
    :class:`~inqtrix.knowledge.stores.ports.DocumentNotFound`: chunk
    identity is ``(document_id, chunk_index)`` and only the service
    resolves that pair, so the store port stays untouched. Maps to
    HTTP 404 with a chunk-specific message — the document itself was
    found and visible, which is safe to disclose.
    """


class KnowledgeService:
    """Application service over the knowledge provider context.

    Args:
        knowledge: The wired knowledge capabilities.
        chunk_max_chars: Character budget per chunk at ingestion.
        max_document_chars: Upper bound on one document's text size
            for every revision source.
    """

    def __init__(
        self,
        *,
        knowledge: KnowledgeProviderContext,
        authorization: "AuthorizationService | None" = None,
        chunk_max_chars: int,
        max_document_chars: int,
        parser: "DocumentParser | None" = None,
        invalidator: "ResourceInvalidator | None" = None,
        generation_rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> None:
        self._knowledge = knowledge
        self._authorization = authorization
        self._chunk_max_chars = chunk_max_chars
        self._max_document_chars = max_document_chars
        self._parser = parser
        self._invalidator = invalidator
        self._generation_rollback_retention_seconds = max(
            0, generation_rollback_retention_seconds
        )
        self._maintenance_active_check: Callable[[str], bool] | None = None
        self._collection_deletion_active_check: Callable[[str], bool] | None = None
        self._document_deletion_active_check: Callable[[str], bool] | None = None

    async def collection_access(
        self,
        collection: KnowledgeCollection,
        visible_to: "UserContext | None",
        *,
        minimum: SharePermission = SharePermission.VIEW,
        bypass_deletion_fence: bool = False,
    ) -> ResourceAccess:
        """Resolve current owner/direct-share access for one collection."""
        if not bypass_deletion_fence and await self._collection_deletion_active(
            collection.id
        ):
            raise CollectionNotFound(collection.id)
        if visible_to is None:
            if collection.created_by_user_id is None:
                return ResourceAccess(AccessMode.UNSCOPED)
            raise CollectionNotFound(collection.id)
        if self._authorization is None:
            log_authorization_denial(
                log,
                action="read",
                principal_kind=visible_to.principal.kind,
                actor_user_id=visible_to.principal.user_id,
                tenant_id=visible_to.principal.tenant_id,
                resource_type="knowledge_collection",
                resource_id=collection.id,
            )
            raise CollectionNotFound(collection.id)
        access = await self._authorization.resolve_resource_access(
            visible_to.principal,
            owner_user_id=collection.created_by_user_id,
            resource_tenant_id=collection.tenant_id,
            resource_type="knowledge_collection",
            resource_id=collection.id,
            minimum=minimum,
        )
        if access is None:
            raise CollectionNotFound(collection.id)
        return access

    @staticmethod
    def _actor_user_id(visible_to: "UserContext | None") -> uuid.UUID | None:
        """Return the canonical actor carried by a scoped request."""
        return visible_to.principal.user_id if visible_to is not None else None

    @property
    def parser(self) -> "DocumentParser | None":
        """The wired document parser (``None`` = text-only ingestion)."""
        return self._parser

    @property
    def knowledge(self) -> KnowledgeProviderContext:
        """The wired knowledge capabilities (algorithm construction)."""
        return self._knowledge

    def bind_collection_maintenance(
        self,
        *,
        active_check: Callable[[str], bool],
    ) -> None:
        """Bind the aggregate indexing-maintenance boundary after composition.

        ``KnowledgeService`` is constructed before ``IndexingService`` at the
        composition root, so the boundary is attached explicitly afterwards.
        It protects destructive aggregate teardown. Document revisions and
        document-level deltas deliberately remain concurrent with a collection
        generation and rely on store-owned revision/generation fences.
        """
        self._maintenance_active_check = active_check

    def bind_collection_deletion(self, *, active_check: Callable[[str], bool]) -> None:
        """Hide a backing collection while its durable deletion is unresolved."""

        self._collection_deletion_active_check = active_check

    def bind_document_deletion(self, *, active_check: Callable[[str], bool]) -> None:
        """Hide a document while its durable deletion is unresolved."""

        self._document_deletion_active_check = active_check

    async def _collection_deletion_active(self, collection_id: str) -> bool:
        if self._collection_deletion_active_check is None:
            return False
        return bool(
            await asyncio.to_thread(
                self._collection_deletion_active_check, collection_id
            )
        )

    async def _document_deletion_active(self, document_id: str) -> bool:
        if self._document_deletion_active_check is None:
            return False
        return bool(
            await asyncio.to_thread(self._document_deletion_active_check, document_id)
        )

    async def _require_collection_mutable(self, collection_id: str) -> None:
        """Protect destructive collection teardown from an active worker."""
        if self._maintenance_active_check is None:
            return
        active = await asyncio.to_thread(self._maintenance_active_check, collection_id)
        if active:
            raise CollectionMaintenanceActive(collection_id)

    def contextualization_circuit_view(self) -> "ContextualizationCircuitView | None":
        """State of the configured contextualizer's provider circuit, if any."""
        reader = getattr(self._knowledge.contextualizer, "circuit_view", None)
        return reader() if callable(reader) else None

    def _build_contract_hash(
        self,
        *,
        collection: KnowledgeCollection,
        contextualize: bool | None = None,
    ) -> str:
        """Stable identity of every setting that changes derived chunks."""
        contextualizer = self._knowledge.contextualizer
        contextualization_enabled = (
            contextualizer is not None
            if contextualize is None
            else bool(contextualize and contextualizer is not None)
        )
        resolved_context_model = None
        if contextualization_enabled and contextualizer is not None:
            resolver = getattr(contextualizer, "_resolved_model", None)
            if callable(resolver):
                resolved_context_model = resolver()
            if not resolved_context_model:
                resolved_context_model = getattr(contextualizer, "_model", None)
        contract = {
            "schema": "knowledge-build-v2",
            "embedding": {
                "model": collection.embedding_model,
                "dimension": collection.embedding_dim,
            },
            "chunker": {
                "kind": "exact-contiguous-character-slices",
                "max_chars": self._chunk_max_chars,
            },
            "contextualization": {
                "enabled": contextualization_enabled,
                "implementation": (
                    type(contextualizer).__qualname__
                    if contextualization_enabled and contextualizer is not None
                    else None
                ),
                "model": resolved_context_model,
                "prompt_contract": "batched-exact-span-v2",
            },
            "parser": (
                type(self._parser).__qualname__ if self._parser is not None else None
            ),
        }
        encoded = json.dumps(
            contract,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def build_contract_hash(
        self,
        collection: KnowledgeCollection,
        *,
        contextualize: bool | None = None,
    ) -> str:
        """Public worker-facing form of the deterministic build identity."""
        return self._build_contract_hash(
            collection=collection, contextualize=contextualize
        )

    # -- collections ------------------------------------------------------ #

    async def create_collection(
        self,
        *,
        name: str,
        embedding_model: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
    ) -> KnowledgeCollection:
        """Create a collection with an immutable embedding identity.

        The embedding dimension comes from the embedding catalog when
        the model is catalogued; for uncatalogued models it is probed
        with one real embedding call so the recorded dimension is the
        truth of the actual backend, never a guess.

        Raises:
            KnowledgeValidationError: Empty name, or a model id outside
                the deployment's selectable set (when one is
                configured).
        """
        clean_name = (name or "").strip()
        if not clean_name:
            raise KnowledgeValidationError("Field 'name' is required")
        model = (
            embedding_model or ""
        ).strip() or self._knowledge.embeddings.default_model
        selectable = self._knowledge.embeddings.selectable_embedding_models
        if (
            selectable
            and model != self._knowledge.embeddings.default_model
            and model not in selectable
        ):
            raise KnowledgeValidationError(
                f"""embedding_model {model!r} is unavailable (available: {', '.join(selectable)})"""
            )
        card = resolve_embedding_card(model)
        if card is not None:
            dimension = card.dims
        else:
            probe = await asyncio.to_thread(
                self._knowledge.embeddings.embed_query,
                "dimension probe",
                model=model,
            )
            dimension = len(probe)
        collection = await self._knowledge.store.create_collection(
            name=clean_name,
            embedding_model=model,
            embedding_dim=dimension,
            created_by_user_id=created_by_user_id,
        )
        await self._invalidate_collection(collection)
        return collection

    async def list_collections(
        self,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[KnowledgeCollection]:
        """The caller's visible collections, newest first.

        Owned plus shared-in plus legacy (``created_by_user_id is None``);
        unscoped callers keep the historical see-everything view.
        """
        return [
            collection
            for collection, _access in await self.list_collections_with_access(
                visible_to=visible_to
            )
        ]

    async def list_collections_with_access(
        self,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[tuple[KnowledgeCollection, ResourceAccess]]:
        """Return one authoritative list snapshot with access annotations."""
        optimized = getattr(self._knowledge.store, "list_visible_collections", None)
        if callable(optimized):
            visible = await optimized(actor_user_id=self._actor_user_id(visible_to))
            return [
                (collection, access)
                for collection, access in visible
                if not await self._collection_deletion_active(collection.id)
            ]
        collections = await self._knowledge.store.list_collections()
        annotated: list[tuple[KnowledgeCollection, ResourceAccess]] = []
        for collection in collections:
            try:
                access = await self.collection_access(collection, visible_to)
            except CollectionNotFound:
                continue
            annotated.append((collection, access))
        return annotated

    async def delete_collection(
        self,
        collection_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> None:
        """Delete a collection with all documents (owner-only).

        Share recipients never delete — v1 grants stop at edit, and a
        deletion destroys the resource for everyone, so even an edit
        grant earns the indistinct 404 here.
        """
        collection = await self._knowledge.store.get_collection(collection_id)
        access = await self.collection_access(collection, visible_to)
        if access.mode is AccessMode.SHARED:
            raise CollectionNotFound(collection_id)
        await self._require_collection_mutable(collection_id)
        await self._knowledge.store.delete_collection(
            collection_id,
            actor_user_id=self._actor_user_id(visible_to),
        )
        if self._invalidator is not None and not getattr(
            self._knowledge.store, "atomic_resource_effects", False
        ):
            await self._invalidator.revoke_deleted(
                tenant_id=collection.tenant_id,
                owner_user_id=collection.created_by_user_id,
                resource_type="knowledge_collection",
                resource_id=collection.id,
                scope="knowledge_collections",
                actor_user_id=self._actor_user_id(visible_to),
            )

    async def delete_collection_for_aggregate(
        self,
        collection_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> None:
        """Idempotently delete an operation-fenced, owner-controlled collection."""

        try:
            collection = await self._knowledge.store.get_collection(collection_id)
        except CollectionNotFound:
            return
        access = await self.collection_access(
            collection,
            visible_to,
            bypass_deletion_fence=True,
        )
        if access.mode is AccessMode.SHARED:
            raise CollectionNotFound(collection_id)
        await self._require_collection_mutable(collection_id)
        await self._knowledge.store.delete_collection(
            collection_id,
            actor_user_id=self._actor_user_id(visible_to),
        )
        if self._invalidator is not None and not getattr(
            self._knowledge.store, "atomic_resource_effects", False
        ):
            await self._invalidator.revoke_deleted(
                tenant_id=collection.tenant_id,
                owner_user_id=collection.created_by_user_id,
                resource_type="knowledge_collection",
                resource_id=collection.id,
                scope="knowledge_collections",
                actor_user_id=self._actor_user_id(visible_to),
            )

    async def collection_residuals(
        self,
        collection_id: str,
        *,
        embedding_model: str | None,
    ) -> dict[str, int]:
        """Return canonical/vector residue after aggregate collection deletion."""

        verifier = getattr(self._knowledge.store, "count_collection_residuals", None)
        if callable(verifier) and embedding_model:
            return await verifier(
                collection_id=collection_id,
                embedding_model=embedding_model,
            )
        try:
            await self._knowledge.store.get_collection(collection_id)
        except CollectionNotFound:
            return {"collections": 0}
        return {"collections": 1}

    # -- documents --------------------------------------------------------- #

    async def add_document(
        self,
        *,
        collection_id: str,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        visible_to: "UserContext | None" = None,
        page_texts: list[str] | None = None,
    ) -> KnowledgeDocument:
        """Chunk, embed, and store one document.

        Raises:
            KnowledgeValidationError: Empty title/text or text above
                the configured revision-source size guard.
            inqtrix.knowledge.stores.ports.CollectionNotFound: Unknown
                collection.
            inqtrix.providers.embeddings.EmbeddingProviderError: When
                the embedding backend fails — surfaced, never swallowed
                into a partially indexed document.
        """
        reservation = await self.reserve_document_revision(
            collection_id=collection_id,
            title=title,
            text=text,
            metadata=metadata,
            visible_to=visible_to,
            page_texts=page_texts,
        )
        return await self.build_reserved_document_revision(
            document_id=reservation.document_id,
            revision_id=reservation.revision_id,
            actor_user_id=self._actor_user_id(visible_to),
        )

    async def reserve_document_revision(
        self,
        *,
        collection_id: str,
        title: str,
        text: str,
        metadata: dict[str, Any] | None = None,
        visible_to: "UserContext | None" = None,
        page_texts: list[str] | None = None,
        source_scope: "SourceScope | None" = None,
    ) -> DocumentRevisionReservation:
        """Persist immutable source intent before any provider work begins."""
        if not bool(
            getattr(
                self._knowledge.store,
                "supports_async_document_revisions",
                True,
            )
        ):
            raise KnowledgeValidationError(
                ("Asynchronous document revisions require a canonical store with CAS "
                "authority across workers; Qdrant-only compatibility mode does not "
                "support this lifecycle.")
            )
        clean_title = (title or "").strip()
        clean_text = (text or "").strip()
        if not clean_title:
            raise KnowledgeValidationError("Field 'title' is required")
        if not clean_text:
            raise KnowledgeValidationError("Field 'text' is required")
        if len(clean_text) > self._max_document_chars:
            raise KnowledgeValidationError(
                f'Document too large ({len(clean_text)} characters, max. {self._max_document_chars})'
            )
        collection = await self._knowledge.store.get_collection(collection_id)
        await self.collection_access(
            collection, visible_to, minimum=SharePermission.EDIT
        )
        document_metadata = dict(metadata or {})
        source_id = canonical_source_id(document_metadata)
        if source_id is None:
            source_id = f"document:{uuid.uuid4().hex}"
        else:
            document_metadata["source_id"] = source_id
        source_slices = chunk_text_slices(clean_text, max_chars=self._chunk_max_chars)
        page_numbers = infer_chunk_pages(
            [item.text for item in source_slices], page_texts
        )
        if any(page is not None for page in page_numbers):
            document_metadata["_chunk_pages"] = page_numbers
        reservation = await self._knowledge.store.reserve_document_revision(
            collection_id=collection_id,
            source_id=source_id,
            revision_id=f"rev_{uuid.uuid4().hex[:20]}",
            content_hash=hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
            build_contract_hash=self._build_contract_hash(collection=collection),
            title=clean_title,
            text=clean_text,
            metadata=document_metadata,
            source_scope=source_scope,
            actor_user_id=self._actor_user_id(visible_to),
        )
        return reservation

    async def build_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
        on_context_batch: Callable[[int, int], None] | None = None,
        on_context_checkpoint: (
            Callable[[ContextualizationBatchCheckpoint], None] | None
        ) = None,
        context_checkpoints: list[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
        on_embedding_started: Callable[[], None] | None = None,
        on_embedding_batch: Callable[[int, int], None] | None = None,
        on_embedding_wait: Callable[[int, int], None] | None = None,
        contextualize: bool = True,
        authority_check: Callable[[], None] | None = None,
    ) -> KnowledgeDocument:
        """Compatibility wrapper over the canonical prepare/publish contract."""
        prepared = await self.prepare_reserved_document_revision(
            document_id=document_id,
            revision_id=revision_id,
            actor_user_id=actor_user_id,
            on_context_batch=on_context_batch,
            on_context_checkpoint=on_context_checkpoint,
            context_checkpoints=context_checkpoints,
            cancel_check=cancel_check,
            on_embedding_started=on_embedding_started,
            on_embedding_batch=on_embedding_batch,
            on_embedding_wait=on_embedding_wait,
            contextualize=contextualize,
            authority_check=authority_check,
        )
        return await self.publish_prepared_document_revision(
            prepared,
            actor_user_id=actor_user_id,
            authority_check=authority_check,
        )

    async def prepare_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
        on_context_batch: Callable[[int, int], None] | None = None,
        on_context_checkpoint: (
            Callable[[ContextualizationBatchCheckpoint], None] | None
        ) = None,
        context_checkpoints: list[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
        on_embedding_started: Callable[[], None] | None = None,
        on_embedding_batch: Callable[[int, int], None] | None = None,
        on_embedding_wait: Callable[[int, int], None] | None = None,
        contextualize: bool = True,
        authority_check: Callable[[], None] | None = None,
    ) -> PreparedDocumentRevision:
        """Run provider work without publishing chunks or revision pointers."""
        if authority_check is not None:
            authority_check()
        reserved = await self._knowledge.store.load_reserved_document_revision(
            document_id=document_id,
            revision_id=revision_id,
            actor_user_id=actor_user_id,
        )
        reservation = reserved.reservation
        revision = reserved.revision
        if reservation.already_published:
            return PreparedDocumentRevision(
                reservation=reservation,
                title=revision.title,
                text=revision.text,
                metadata=dict(revision.metadata),
                embedded=None,
                page_numbers=None,
                already_published=await self._knowledge.store.get_document(document_id),
            )
        collection = await self._knowledge.store.get_collection(
            reservation.collection_id
        )
        prepared = await self._embed_text(
            title=revision.title,
            text=revision.text,
            embedding_model=collection.embedding_model,
            on_context_batch=on_context_batch,
            on_context_checkpoint=on_context_checkpoint,
            context_checkpoints=context_checkpoints,
            cancel_check=cancel_check,
            on_embedding_started=on_embedding_started,
            on_embedding_batch=on_embedding_batch,
            on_embedding_wait=on_embedding_wait,
            contextualize=contextualize,
        )
        if authority_check is not None:
            authority_check()
        document_metadata = dict(revision.metadata)
        if prepared.contextualization_marker is not None:
            # The marker travels with the document so degraded
            # ingestions stay diagnosable per document, not just in
            # the log stream.
            document_metadata["_chunk_context"] = prepared.contextualization_marker
            document_metadata["_chunk_context_batches"] = (
                prepared.contextualization_batches
            )
            # Which prompt situated the chunks: visible per document, so an
            # operator can tell mixed generations apart and reindex on purpose.
            document_metadata["_chunk_context_prompt_version"] = (
                CHUNK_CONTEXT_PROMPT_VERSION
            )
            if prepared.context_omitted_chunks:
                # Counted per document, never silent: how many chunks went
                # without context and why.
                document_metadata["_chunk_context_omitted"] = {
                    "chunks": prepared.context_omitted_chunks,
                    "reason": CONTEXT_OMITTED_CONTENT_FILTER,
                }
        # Best-effort 1-based source page per chunk (PDFs only). Mapped against
        # the PRE-contextualization source chunks (a synthetic prefix would not
        # match the page text). Persisted on the document so a later re-embed
        # re-aligns by chunk index without re-reading the original file.
        stored_pages = document_metadata.get("_chunk_pages")
        page_numbers = (
            list(stored_pages)
            if isinstance(stored_pages, list)
            and len(stored_pages) == len(prepared.embedding_texts)
            else None
        )
        return PreparedDocumentRevision(
            reservation=reservation,
            title=revision.title,
            text=revision.text,
            metadata=document_metadata,
            embedded=prepared,
            page_numbers=page_numbers,
        )

    async def publish_prepared_document_revision(
        self,
        prepared: PreparedDocumentRevision,
        *,
        actor_user_id: uuid.UUID | None = None,
        authority_check: Callable[[], None] | None = None,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        publication_guard: Callable[[], Any] | None = None,
    ) -> KnowledgeDocument:
        """Cross the sole revision CAS after all external receipts succeeded.

        A durable worker's claim fence is forwarded to the canonical store so
        Postgres can validate it in the same transaction as the active
        revision pointer. Process-local stores are fenced by the job handle's
        mutation boundary around this call.
        """
        if authority_check is not None:
            authority_check()
        if prepared.already_published is not None:
            return prepared.already_published
        embedded = prepared.embedded
        if embedded is None:
            raise RuntimeError("prepared revision has no embedded payload")
        stored = await self._knowledge.store.publish_document_revision(
            reservation=prepared.reservation,
            title=prepared.title,
            text=prepared.text,
            metadata=prepared.metadata,
            chunks=embedded.embedding_texts,
            embeddings=embedded.embeddings,
            source_chunks=embedded.source_texts,
            retrieval_contexts=embedded.retrieval_contexts,
            source_spans=embedded.spans_for(prepared.text),
            page_numbers=prepared.page_numbers,
            generation_id=None,
            fence_job_id=fence_job_id,
            fence_attempt=fence_attempt,
            publication_guard=publication_guard,
            actor_user_id=actor_user_id,
        )
        collection = await self._knowledge.store.get_collection(
            prepared.reservation.collection_id
        )
        await self._invalidate_collection(collection)
        return stored

    async def _embed_text(
        self,
        *,
        title: str,
        text: str,
        embedding_model: str,
        on_context_batch: Callable[[int, int], None] | None = None,
        on_context_checkpoint: (
            Callable[[ContextualizationBatchCheckpoint], None] | None
        ) = None,
        context_checkpoints: list[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
        on_embedding_started: Callable[[], None] | None = None,
        on_embedding_batch: Callable[[int, int], None] | None = None,
        on_embedding_wait: Callable[[int, int], None] | None = None,
        contextualize: bool = True,
    ) -> EmbeddedDocument:
        """Chunk, optionally contextualize, and embed one document body.

        The single embed pipeline shared by first-time ingestion
        (:meth:`add_document`) and background re-embedding
        (:meth:`reembed_document`) so the two paths cannot drift in how
        they chunk or contextualize.

        Returns a complete unpublished plan. Generated retrieval context stays
        separate from source evidence; only ``embedding_texts`` are sent to the
        embedding provider.
        """
        source_slices = chunk_text_slices(text, max_chars=self._chunk_max_chars)
        embedding_texts = [item.text for item in source_slices]
        retrieval_contexts: list[str | None] = [None] * len(source_slices)
        marker: str | None = None
        batch_count = 0
        omitted_chunks = 0
        contextualizer = self._knowledge.contextualizer
        if contextualize and contextualizer is not None:
            # Sync LLM call → off the event loop so a long
            # contextualization pass never stalls concurrent requests.
            contextualized = await asyncio.to_thread(
                contextualizer.contextualize,
                document_title=title,
                document_text=text,
                chunks=source_slices,
                on_batch_completed=on_context_batch,
                on_batch_checkpoint=on_context_checkpoint,
                completed_batches=context_checkpoints,
                cancel_check=cancel_check,
            )
            embedding_texts = contextualized.texts
            # A chunk whose batch the content filter blocked has no context.
            retrieval_contexts = [
                context or None for context in contextualized.contexts
            ]
            marker = contextualized.marker
            batch_count = contextualized.batch_count
            omitted_chunks = contextualized.omitted_chunks
        if on_embedding_started is not None:
            on_embedding_started()
        if cancel_check is not None:
            cancel_check()
        # Sliced under a character budget so one huge document can never
        # demand more than a provider's per-minute quota in a single
        # request; each slice runs off the event loop via the shared
        # tracing wrapper (one span per slice).
        embeddings = await embed_in_slices(
            embedding_texts,
            embed_fn=functools.partial(
                self._knowledge.embeddings.embed_documents,
                model=embedding_model,
            ),
            cancel_check=cancel_check,
            on_batch=on_embedding_batch,
            on_wait=on_embedding_wait,
        )
        if cancel_check is not None:
            # A provider call itself may be non-interruptible. Cancellation
            # requested while it was in flight must still fence publication.
            cancel_check()
        return EmbeddedDocument(
            embedding_texts=embedding_texts,
            embeddings=embeddings,
            source_slices=source_slices,
            retrieval_contexts=retrieval_contexts,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            contextualization_marker=marker,
            contextualization_batches=batch_count,
            context_omitted_chunks=omitted_chunks,
        )

    async def reembed_document_with_receipt(
        self,
        *,
        document: KnowledgeDocument,
        embedding_model: str,
        generation_id: str | None = None,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        on_context_batch: Callable[[int, int], None] | None = None,
        on_context_checkpoint: (
            Callable[[ContextualizationBatchCheckpoint], None] | None
        ) = None,
        context_checkpoints: list[ContextualizationBatchCheckpoint] | None = None,
        cancel_check: Callable[[], None] | None = None,
        on_embedding_started: Callable[[], None] | None = None,
        on_embedding_batch: Callable[[int, int], None] | None = None,
        on_embedding_wait: Callable[[int, int], None] | None = None,
        authority_check: Callable[[], None] | None = None,
        actor_user_id: uuid.UUID | None = None,
        contextualize: bool = True,
    ) -> ReembeddedDocument:
        """Stage one re-embedded document and return exact provider usage.

        The per-document unit of work behind a background reindex run:
        the document's stored text is re-run through the same chunk and
        embedding pipeline as first-time ingestion and its vectors are
        replaced in place (id, title, text, metadata, and creation time
        preserved — see
        :meth:`~inqtrix.knowledge.stores.ports.KnowledgeStore.reembed_document`).
        Access is the job's concern (checked once at submission); this
        method is trusted server code.

        Raises:
            inqtrix.knowledge.stores.ports.DocumentNotFound: The
                document vanished between enumeration and re-embed.
            inqtrix.providers.embeddings.EmbeddingProviderError: The
                embedding backend failed — surfaced, never swallowed
                into a partially re-embedded document.
        """
        if authority_check is not None:
            authority_check()
        prepared = await self._embed_text(
            title=document.title,
            text=document.text,
            embedding_model=embedding_model,
            on_context_batch=on_context_batch,
            on_context_checkpoint=on_context_checkpoint,
            context_checkpoints=context_checkpoints,
            cancel_check=cancel_check,
            on_embedding_started=on_embedding_started,
            on_embedding_batch=on_embedding_batch,
            on_embedding_wait=on_embedding_wait,
            contextualize=contextualize,
        )
        if authority_check is not None:
            authority_check()
        # Re-embed re-chunks the unchanged canonical text with the deterministic
        # chunker, so the chunk set is identical and page numbers captured at
        # first ingest re-align by index — carry them forward (no original file
        # to re-map against here). Truncate/pad defensively to the chunk count.
        stored_pages = document.metadata.get("_chunk_pages")
        page_numbers: list[int | None] | None = None
        if isinstance(stored_pages, list):
            if len(stored_pages) == len(prepared.embedding_texts):
                page_numbers = list(stored_pages)
            else:
                # The chunk set changed (e.g. chunk_max_chars was reconfigured
                # between ingest and reindex), so the stored pages no longer
                # align by index — drop them rather than misattribute pages
                # silently (No Silent Fallbacks). A re-ingest from the original
                # PDF is the only path that re-derives them.
                log.warning(
                    "Re-embed page mapping: stored %d pages but re-chunk "
                    "produced %d chunks for document %s (likely a chunk_max_chars "
                    "change); dropping page numbers",
                    len(stored_pages),
                    len(prepared.embedding_texts),
                    document.id,
                )
        collection = await self._knowledge.store.get_collection(document.collection_id)
        revision_id = document.active_revision_id or f"rev_{uuid.uuid4().hex[:20]}"
        updated = await self._knowledge.store.reembed_document(
            document_id=document.id,
            chunks=prepared.embedding_texts,
            embeddings=prepared.embeddings,
            source_chunks=prepared.source_texts,
            retrieval_contexts=prepared.retrieval_contexts,
            source_spans=prepared.spans_for(document.text),
            document_content_hash=prepared.content_hash,
            revision_id=revision_id,
            generation_id=generation_id or collection.active_generation_id,
            fence_job_id=fence_job_id,
            fence_attempt=fence_attempt,
            page_numbers=page_numbers,
            actor_user_id=actor_user_id,
        )
        if authority_check is not None:
            authority_check()
        return ReembeddedDocument(
            document=updated,
            work_receipt=prepared.work_receipt,
            context_omitted_chunks=prepared.context_omitted_chunks,
        )

    async def reembed_document(self, **kwargs: Any) -> KnowledgeDocument:
        """Compatibility view returning only the updated document."""

        return (await self.reembed_document_with_receipt(**kwargs)).document

    async def active_document_embedding_receipt(
        self, document_id: str
    ) -> EmbeddingWorkReceipt:
        """Reconstruct exact embedding accounting from persisted active chunks.

        This is used only when a revision was published before its worker wrote
        the final job checkpoint. It reads the materialized provider inputs;
        it never invokes contextualization or embeddings again.
        """

        chunks = await self._knowledge.store.get_chunks(document_id)
        if not chunks:
            raise RuntimeError("published document has no active embedding inputs")
        return _embedding_work_receipt([chunk.text for chunk in chunks])

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
        build_contract_hash: str = "",
    ) -> KnowledgeCollection:
        """Publish a complete staged generation through the store CAS boundary."""
        if expected_manifest is None:
            documents = await self._knowledge.store.list_documents(collection_id)
            documents_by_id = {document.id: document for document in documents}
            expected = {
                document_id: documents_by_id[document_id].active_revision_id or ""
                for document_id in expected_document_ids
                if document_id in documents_by_id
            }
        else:
            expected = dict(expected_manifest)
            documents_by_id = {}
            try:
                for document_id in expected:
                    documents_by_id[document_id] = (
                        await self._knowledge.store.get_document(document_id)
                    )
            except DocumentNotFound as exc:
                raise GenerationManifestChanged(
                    "collection manifest changed while generation validation was built"
                ) from exc
        if set(documents_by_id) != set(expected) or any(
            (documents_by_id[document_id].active_revision_id or "") != revision_id
            for document_id, revision_id in expected.items()
        ):
            raise GenerationManifestChanged(
                "collection manifest changed while generation validation was built"
            )
        validation_documents: dict[str, GenerationDocumentValidation] = {}
        for document_id, revision_id in expected.items():
            document = documents_by_id[document_id]
            slices = chunk_text_slices(document.text, max_chars=self._chunk_max_chars)
            validation_documents[document_id] = GenerationDocumentValidation(
                revision_id=revision_id,
                content_hash=hashlib.sha256(document.text.encode("utf-8")).hexdigest(),
                source_spans=tuple(item.utf8_span(document.text) for item in slices),
            )
        collection = await self._knowledge.store.get_collection(collection_id)
        expected_validation = GenerationBuildValidation(
            embedding_dim=collection.embedding_dim,
            documents=validation_documents,
        )
        return await self._knowledge.store.activate_generation(
            collection_id=collection_id,
            generation_id=generation_id,
            expected_document_ids=expected_document_ids,
            fence_job_id=fence_job_id,
            fence_attempt=fence_attempt,
            actor_user_id=actor_user_id,
            expected_manifest=expected,
            expected_validation=expected_validation,
            build_contract_hash=build_contract_hash,
            rollback_retention_seconds=(self._generation_rollback_retention_seconds),
        )

    async def begin_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        build_contract_hash: str,
        manifest: dict[str, str],
        actor_user_id: uuid.UUID | None = None,
    ):
        return await self._knowledge.store.begin_generation(
            collection_id=collection_id,
            generation_id=generation_id,
            build_contract_hash=build_contract_hash,
            manifest=manifest,
            actor_user_id=actor_user_id,
        )

    async def remove_document_from_generation(
        self,
        *,
        collection_id: str,
        document_id: str,
        generation_id: str,
    ) -> int:
        return await self._knowledge.store.remove_document_from_generation(
            collection_id=collection_id,
            document_id=document_id,
            generation_id=generation_id,
        )

    async def reset_generation_for_raw_choice(
        self,
        *,
        collection_id: str,
        generation_id: str,
    ) -> int:
        collection = await self._knowledge.store.get_collection(collection_id)
        documents = await self._knowledge.store.list_documents(collection_id)
        manifest = {
            document.id: document.active_revision_id or "" for document in documents
        }
        return await self._knowledge.store.reset_generation_for_raw_choice(
            collection_id=collection_id,
            generation_id=generation_id,
            build_contract_hash=self.build_contract_hash(
                collection, contextualize=False
            ),
            manifest=manifest,
        )

    async def rollback_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeCollection:
        return await self._knowledge.store.rollback_generation(
            collection_id=collection_id,
            generation_id=generation_id,
            actor_user_id=actor_user_id,
            rollback_retention_seconds=(self._generation_rollback_retention_seconds),
        )

    async def prune_expired_generations(
        self,
        *,
        collection_id: str,
    ) -> int:
        return await self._knowledge.store.prune_expired_generations(
            collection_id=collection_id
        )

    async def prune_expired_generations_all(self) -> dict[str, int]:
        """Run one tenant-scoped retention sweep through the canonical store."""
        collection_ids = await self._knowledge.store.generation_cleanup_collection_ids()
        removed = 0
        completed_collections = 0
        failed: list[str] = []
        for collection_id in collection_ids:
            try:
                removed += await self._knowledge.store.prune_expired_generations(
                    collection_id=collection_id
                )
                completed_collections += 1
            except GenerationPruneError as exc:
                failed.extend(exc.generation_ids)
        if failed:
            raise GenerationPruneError(failed)
        return {
            "collections": completed_collections,
            "chunks": removed,
        }

    async def discard_generation(
        self,
        *,
        collection_id: str,
        generation_id: str,
        fence_job_id: str | None = None,
        fence_attempt: int | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        """Remove an unpublished generation after cancel or supersession."""
        return await self._knowledge.store.discard_generation(
            collection_id=collection_id,
            generation_id=generation_id,
            fence_job_id=fence_job_id,
            fence_attempt=fence_attempt,
            actor_user_id=actor_user_id,
        )

    async def add_document_from_file(
        self,
        *,
        collection_id: str,
        file_name: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
        title: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> KnowledgeDocument:
        """Parse one uploaded file and ingest the resulting text.

        Raises:
            KnowledgeValidationError: When no parser is wired
                (``INQTRIX_DOCUMENT_PARSER=none``) — the caller maps
                this to a clear client error, never a silent skip.
            inqtrix.knowledge.parsing.DocumentParseError: When the file
                cannot be converted or yields no text.
        """
        prepared = await self.prepare_document_file(
            file_name=file_name,
            content=content,
            metadata=metadata,
            title=title,
        )
        return await self.add_document(
            collection_id=collection_id,
            title=prepared[0],
            text=prepared[1],
            metadata=prepared[2],
            visible_to=visible_to,
            page_texts=prepared[3],
        )

    async def prepare_document_file(
        self,
        *,
        file_name: str,
        content: bytes,
        metadata: dict[str, Any] | None = None,
        title: str | None = None,
    ) -> tuple[str, str, dict[str, Any], list[str] | None]:
        """Parse file bytes without embedding or publishing any revision."""
        if self._parser is None:
            raise KnowledgeValidationError(
                "File ingestion is disabled (INQTRIX_DOCUMENT_PARSER=none)"
            )
        extraction = await asyncio.to_thread(
            extract_file, self._parser, file_name=file_name, content=content
        )
        document_metadata = dict(metadata or {})
        document_metadata["parser"] = extraction.parser_id
        document_metadata["extraction"] = extraction_facts(
            parser_id=extraction.parser_id,
            parser_revision=extraction.parser_revision,
            file_sha256=hashlib.sha256(content).hexdigest(),
            page_texts=extraction.page_texts,
        )
        return (
            (title or "").strip() or file_name,
            extraction.text,
            document_metadata,
            list(extraction.page_texts) or None,
        )

    async def list_documents(
        self,
        collection_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[KnowledgeDocument]:
        """A collection's documents, newest first (view via parent)."""
        collection = await self._knowledge.store.get_collection(collection_id)
        await self.collection_access(collection, visible_to)
        documents = await self._knowledge.store.list_documents(collection_id)
        return [
            document
            for document in documents
            if not await self._document_deletion_active(document.id)
        ]

    async def list_documents_page(
        self,
        collection_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[list[KnowledgeDocument], str | None]:
        """One keyset page of a collection's documents (view via parent).

        Access is the single parent-collection check, so the store's
        DB-side LIMIT bounds the work without under-filling the page."""
        collection = await self._knowledge.store.get_collection(collection_id)
        await self.collection_access(collection, visible_to)
        documents, next_cursor = await self._knowledge.store.list_documents_page(
            collection_id, limit=limit, after=after
        )
        return (
            [
                document
                for document in documents
                if not await self._document_deletion_active(document.id)
            ],
            next_cursor,
        )

    async def resolve_document_by_source(
        self,
        collection_id: str,
        source_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> KnowledgeDocument:
        """Resolve one legacy member to its stable logical document.

        This is a read-only reconciliation seam for vector-index members
        persisted before ``server_document_id`` existed.  It never guesses by
        title or completion time: the canonical/legacy source identity is
        resolved inside the already-authorized parent collection.  A missing
        or ambiguous match remains blocked so the caller cannot report a
        local-only removal while searchable server state may survive.
        """

        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        collection = await self._knowledge.store.get_collection(collection_id)
        await self.collection_access(
            collection,
            visible_to,
            minimum=SharePermission.EDIT,
        )
        matches = [
            document
            for document in await self._knowledge.store.list_documents_by_source(
                canonical,
                collection_id=collection_id,
            )
            if document.lifecycle_status == "active"
            and not await self._document_deletion_active(document.id)
        ]
        if not matches:
            raise DocumentNotFound(canonical)
        if len(matches) > 1:
            raise SourceDocumentResolutionConflict(
                ("Multiple active documents have the same source identity; reconcile the"
                " index before removing them.")
            )
        return matches[0]

    async def get_document(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> KnowledgeDocument:
        """One document including its full text (the citable source view).

        Visibility is the parent collection's (view suffices); the
        denial is :class:`DocumentNotFound` so a hidden document and a
        missing one stay byte-identical.
        """
        if await self._document_deletion_active(document_id):
            raise DocumentNotFound(document_id)
        document = await self._knowledge.store.get_document(document_id)
        await self._document_parent_access(document, visible_to)
        return document

    async def document_chunks(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[DocumentChunk]:
        """Every stored chunk of one document, behind the document's access check.

        The ONE chunk-list access path: the evidence view and the citation of
        a verified quote read chunks only through it. Visibility is the parent
        collection's, exactly like :meth:`get_document`; a document being
        deleted is not found.
        """
        if await self._document_deletion_active(document_id):
            raise DocumentNotFound(document_id)
        document = await self._knowledge.store.get_document(document_id)
        await self._document_parent_access(document, visible_to)
        return await self._knowledge.store.get_chunks(document_id)

    async def get_chunk(
        self,
        document_id: str,
        chunk_index: int,
        *,
        context: int = 0,
        visible_to: "UserContext | None" = None,
    ) -> tuple[DocumentChunk, list[DocumentChunk]]:
        """One chunk plus its neighbour chunks (the citable evidence view).

        Chunk identity is ``(document_id, chunk_index)`` — stable across
        reindex, unlike the physical chunk id. *context* widens the read
        so a cited quote can be shown in its document surroundings.
        Visibility is the parent collection's, exactly like
        :meth:`get_document` (view suffices; denial stays the indistinct
        :class:`DocumentNotFound`).

        Args:
            document_id: The owning document.
            chunk_index: Zero-based chunk position within the document.
            context: Neighbour chunks to include on EACH side of the
                target (``0`` — the default — returns none). Range
                policy (0..3) is the router's concern; the service
                trusts its caller.
            visible_to: Caller scope for the parent-collection check.

        Returns:
            ``(chunk, neighbors)`` — *neighbors* are the up to
            ``2 * context`` surrounding chunks in ``chunk_index`` order,
            target excluded.

        Raises:
            DocumentNotFound: Unknown or invisible document.
            ChunkNotFound: The document is visible but has no chunk at
                *chunk_index*.
        """
        chunks = await self.document_chunks(document_id, visible_to=visible_to)
        target = next(
            (chunk for chunk in chunks if chunk.chunk_index == chunk_index),
            None,
        )
        if target is None:
            raise ChunkNotFound(f"{document_id}#{chunk_index}")
        neighbors = [
            chunk
            for chunk in chunks
            if chunk.chunk_index != chunk_index
            and abs(chunk.chunk_index - chunk_index) <= context
        ]
        return target, neighbors

    async def delete_document(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> None:
        """Delete one document and its chunks (edit via parent)."""
        document = await self._knowledge.store.get_document(document_id)
        await self._document_parent_access(
            document, visible_to, minimum=SharePermission.EDIT
        )
        await self._knowledge.store.delete_document(
            document_id,
            actor_user_id=self._actor_user_id(visible_to),
        )
        collection = await self._knowledge.store.get_collection(document.collection_id)
        await self._invalidate_collection(collection)

    async def prepare_document_deletion(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> tuple[KnowledgeDocument, KnowledgeCollection]:
        """Authorize and snapshot one document aggregate before submission."""

        if await self._document_deletion_active(document_id):
            raise DocumentNotFound(document_id)
        document = await self._knowledge.store.get_document(document_id)
        await self._document_parent_access(
            document,
            visible_to,
            minimum=SharePermission.EDIT,
        )
        collection = await self._knowledge.store.get_collection(document.collection_id)
        return document, collection

    async def authorize_knowledge_deletion(
        self,
        context: Any,
        *,
        visible_to: "UserContext | None",
    ) -> None:
        """Revalidate current ACLs immediately before destructive worker work."""

        if context.target_kind.value == "knowledge_collection":
            collection = await self._knowledge.store.get_collection(
                context.collection_id
            )
            access = await self.collection_access(
                collection,
                visible_to,
                bypass_deletion_fence=True,
            )
            if access.mode is AccessMode.SHARED:
                raise CollectionNotFound(context.collection_id)
            return
        if not context.document_id:
            raise DocumentNotFound("")
        document = await self._knowledge.store.get_document(context.document_id)
        if document.collection_id != context.collection_id:
            raise DocumentNotFound(context.document_id)
        await self._document_parent_access(
            document,
            visible_to,
            minimum=SharePermission.EDIT,
            bypass_deletion_fence=True,
        )

    async def mark_document_deleting_for_aggregate(self, document_id: str) -> None:
        await self._knowledge.store.mark_document_deleting(document_id)

    async def restore_document_after_deletion_preflight(self, document_id: str) -> None:
        await self._knowledge.store.restore_document_active(document_id)

    async def delete_document_for_aggregate(
        self,
        document_id: str,
        *,
        visible_to: "UserContext | None",
    ) -> None:
        """Idempotently delete a worker-authorized, operation-fenced document."""

        try:
            document = await self._knowledge.store.get_document(document_id)
        except DocumentNotFound:
            return
        deleter = getattr(
            self._knowledge.store,
            "delete_document_for_aggregate",
            None,
        )
        if not callable(deleter):
            raise KnowledgeError(
                "knowledge store cannot converge durable document deletion"
            )
        await deleter(
            document_id,
            actor_user_id=self._actor_user_id(visible_to),
        )
        try:
            collection = await self._knowledge.store.get_collection(
                document.collection_id
            )
        except CollectionNotFound:
            return
        await self._invalidate_collection(collection)

    async def document_residuals(
        self,
        document_id: str,
        *,
        embedding_model: str,
    ) -> dict[str, int]:
        verifier = getattr(self._knowledge.store, "count_document_residuals", None)
        if callable(verifier):
            return await verifier(
                document_id=document_id,
                embedding_model=embedding_model,
            )
        try:
            await self._knowledge.store.get_document(document_id)
        except DocumentNotFound:
            return {"documents": 0}
        return {"documents": 1}

    async def mark_source_deleting(
        self,
        source_id: str,
        *,
        visible_to: "UserContext | None" = None,
        principal: "Principal | None" = None,
        workspace_id: str | None = None,
        deletion_permit: SourceDeletionPermit | None = None,
    ) -> int:
        """Detach an asset source from every knowledge search immediately.

        ``asset:<asset_id>`` is canonical; the stores also match historical
        ``metadata.fileId``/``file_id`` records. The transition is idempotent
        and precedes physical vector/blob cleanup so a user-confirmed deletion
        cannot influence a new answer while its cleanup job is running.
        """
        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        affected = await self._knowledge.store.mark_source_deleting(
            canonical,
            deletion_permit=deletion_permit,
            actor_user_id=(
                principal.user_id
                if principal is not None
                else self._actor_user_id(visible_to)
            ),
        )
        return affected

    async def prepare_source_cleanup(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> SourceCleanupPlan:
        """Create a serializable exact-point plan bound to deletion authority."""
        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        return await self._knowledge.store.prepare_source_cleanup(
            canonical,
            deletion_permit=deletion_permit,
        )

    async def execute_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
        principal: "Principal | None" = None,
    ) -> int:
        """Execute a persisted plan without reintroducing collection ACL checks."""
        return await self._knowledge.store.execute_source_cleanup(
            plan,
            deletion_permit=deletion_permit,
            actor_user_id=(principal.user_id if principal is not None else None),
        )

    async def verify_source_cleanup(
        self,
        plan: SourceCleanupPlan,
        *,
        deletion_permit: SourceDeletionPermit,
    ) -> dict[str, int]:
        """Verify exact identifiers after canonical rows have been removed."""
        verify = getattr(self._knowledge.store, "verify_source_cleanup", None)
        if not callable(verify):
            raise RuntimeError("knowledge store lacks source cleanup verification")
        return await verify(plan, deletion_permit=deletion_permit)

    async def delete_source(
        self,
        source_id: str,
        *,
        visible_to: "UserContext | None" = None,
        principal: "Principal | None" = None,
        workspace_id: str | None = None,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> int:
        """Idempotently remove all knowledge state for a stable asset source."""
        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        return await self._knowledge.store.delete_source(
            canonical,
            deletion_permit=deletion_permit,
            cleanup_plan=cleanup_plan,
            actor_user_id=(
                principal.user_id
                if principal is not None
                else self._actor_user_id(visible_to)
            ),
        )

    async def source_residuals(
        self,
        source_id: str,
        *,
        visible_to: "UserContext | None" = None,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> dict[str, int]:
        """Return zero-residual counts for the deletion orchestrator."""
        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        if deletion_permit is None:
            documents = await self._knowledge.store.list_documents_by_source(canonical)
            for document in documents:
                await self._document_parent_access(
                    document,
                    visible_to,
                    minimum=SharePermission.EDIT,
                )
        return await self._knowledge.store.source_residuals(
            canonical,
            deletion_permit=deletion_permit,
            cleanup_plan=cleanup_plan,
        )

    async def count_source_residuals(
        self,
        source_id: str,
        *,
        principal: "Principal | None" = None,
        workspace_id: str | None = None,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> int:
        """Return a single zero/non-zero guard count for deletion completion.

        ``workspace_id`` is accepted for the common asset-deletion adapter but
        deliberately does not scope knowledge, whose collections are shared
        resources rather than workspace-owned rows.
        """
        canonical = (source_id or "").strip()
        if not canonical:
            raise KnowledgeValidationError("source_id is required")
        counts = await self._knowledge.store.source_residuals(
            canonical,
            deletion_permit=deletion_permit,
            cleanup_plan=cleanup_plan,
        )
        return sum(max(0, int(value)) for value in counts.values())

    async def _invalidate_collection(self, collection: KnowledgeCollection) -> None:
        """Publish fallback effects only for volatile knowledge stores."""
        if self._invalidator is None or getattr(
            self._knowledge.store, "atomic_resource_effects", False
        ):
            return
        await self._invalidator.invalidate(
            tenant_id=collection.tenant_id,
            owner_user_id=collection.created_by_user_id,
            resource_type="knowledge_collection",
            resource_id=collection.id,
            scope="knowledge_collections",
        )

    async def _document_parent_access(
        self,
        document: KnowledgeDocument,
        visible_to: "UserContext | None",
        *,
        minimum: SharePermission = SharePermission.VIEW,
        bypass_deletion_fence: bool = False,
    ) -> ResourceAccess:
        """The caller's grant on a document's parent collection.

        Raises:
            DocumentNotFound: No access — re-raised under the
                document's identity, never the collection's.
        """
        try:
            collection = await self._knowledge.store.get_collection(
                document.collection_id
            )
            return await self.collection_access(
                collection,
                visible_to,
                minimum=minimum,
                bypass_deletion_fence=bypass_deletion_fence,
            )
        except CollectionNotFound:
            raise DocumentNotFound(document.id) from None

    # -- retrieval ----------------------------------------------------------- #

    async def search(
        self,
        *,
        query: str,
        collection_ids: list[str] | None = None,
        top_k: int | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[RetrievalCandidate]:
        """Embed *query* and return scored chunk candidates.

        Backward-compatible shape (``list[RetrievalCandidate]``);
        callers that need the applied-visibility report use
        :meth:`search_reported`.
        """
        outcome = await self.search_reported(
            query=query,
            collection_ids=collection_ids,
            top_k=top_k,
            visible_to=visible_to,
        )
        return outcome.candidates

    async def search_reported(
        self,
        *,
        query: str,
        collection_ids: list[str] | None = None,
        top_k: int | None = None,
        visible_to: "UserContext | None" = None,
    ) -> SearchOutcome:
        """Embed *query* and return scored candidates plus dropped ids.

        Kept synchronous and side-effect-free on purpose: this is the
        debugging/evaluation surface for retrieval quality, independent
        of answer synthesis.

        Scoped callers search only what they may see: an explicit
        ``collection_ids`` list is reduced to its visible members
        (none visible raises :class:`CollectionNotFound` — the 404),
        and an unscoped search ranges over the visible set instead of
        everything. Dropped-but-requested ids are reported so the
        filtering never stays silent.
        """
        clean_query = (query or "").strip()
        if not clean_query:
            raise KnowledgeValidationError("Field 'query' is required")
        effective_ids = collection_ids
        filtered_ids: list[str] = []
        visible_ids = {
            collection.id
            for collection in await self.list_collections(visible_to=visible_to)
        }
        if collection_ids is not None:
            effective_ids = [item for item in collection_ids if item in visible_ids]
            filtered_ids = [item for item in collection_ids if item not in visible_ids]
            if not effective_ids:
                raise CollectionNotFound(collection_ids[0] if collection_ids else "")
        else:
            if not visible_ids:
                return SearchOutcome(candidates=[])
            effective_ids = sorted(visible_ids)
        # Deliberately no on_provider_retry: this debug/eval surface has no
        # event stream; rerank retries stay visible via the provider log.
        requested_top_k = top_k or self._knowledge.default_top_k
        ranked = await retrieve(
            self._knowledge,
            query=clean_query,
            collection_ids=effective_ids,
            # Canonical stores overfetch internally until active, verified
            # evidence fills this exact requested depth or the provider is
            # genuinely exhausted.  A fixed multiplier can still starve a
            # result set when stale points dominate.
            top_k=requested_top_k,
        )
        unverified = [
            candidate.chunk.id
            for candidate in ranked
            if not candidate.chunk.source_verified
        ]
        candidates = [
            candidate for candidate in ranked if candidate.chunk.source_verified
        ][:requested_top_k]
        if unverified:
            log.warning(
                "Knowledge search excluded %d retrieval chunks without "
                "canonical source-span verification; reindex is required.",
                len(unverified),
            )
        retrieval_exclusions = list(ranked.exclusions)
        if unverified:
            retrieval_exclusions.append(
                RetrievalExclusion(
                    reason="source_unverified",
                    stage="candidate_projection",
                    count=len(unverified),
                    recommended_action="reindex",
                )
            )
        return SearchOutcome(
            candidates=candidates,
            filtered_collection_ids=filtered_ids,
            unverified_chunk_ids=unverified,
            retrieval_exclusions=retrieval_exclusions,
            retrieval_degradations=[
                degradation.with_final_result(
                    final_top_k=requested_top_k,
                    returned_hits=len(candidates),
                )
                for degradation in ranked.degradations
            ],
        )

    async def assert_collections_visible(
        self,
        collection_ids: list[str],
        *,
        visible_to: "UserContext | None" = None,
    ) -> None:
        """Admission gate for the ask paths (chat + native runs).

        Strict on purpose, unlike the search filter: an ask against an
        explicit collection set must not silently answer from fewer
        collections than the caller picked — a single invisible id
        denies the whole request with the indistinct
        :class:`CollectionNotFound`. Admission persists only this bounded
        dependency set; the execution dependency authorizer then repeats the
        same live check at run safepoints so a later revoke cannot inherit the
        admission decision.
        """
        for collection_id in collection_ids:
            collection = await self._knowledge.store.get_collection(collection_id)
            await self.collection_access(collection, visible_to)

    async def resolve_ask_scope(
        self,
        collection_ids: "list[str] | None",
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[str] | None:
        """Pin an ask's retrieval scope to what the caller may see.

        The admission-time counterpart of
        :meth:`assert_collections_visible`: it normalizes the scope once at
        submit and returns the concrete id list to persist into the run's
        ``knowledge_filters``, so the worker re-executes an already bounded
        request. Authorization is not cached: the worker rechecks every pinned
        id at execution safepoints. This closes the unscoped-ask gap — an omitted,
        empty, or ``null`` filter otherwise reaches the ``mode=knowledge``
        algorithm as ``None``, and the shared retrieval pipeline
        (:mod:`inqtrix.knowledge.retrieval`) then searches EVERY
        collection in the tenant (only the agent/capability paths carry
        ``visible_to`` on their own).

        Rules:

        * an explicit, non-empty list is asserted strictly (one invisible
          id raises :class:`CollectionNotFound`) and returned unchanged —
          the caller pinned the scope on purpose;
        * an omitted / ``null`` / ``[]`` / non-list scope with a resolved
          ``visible_to`` expands to the caller-visible set (owned +
          shared-in + legacy), matching :meth:`search_reported`'s
          unscoped expansion and the agent harness's falsy-scope rule —
          an ask must answer from the caller's corpus, never 404 on a
          merely-empty selection. An empty visible set returns ``[]`` —
          fail-closed, because the stores read ``[]`` as "nothing" while
          only ``None`` means "everything";
        * ``visible_to is None`` (``AUTH_MODE`` none / static apikey)
          keeps the historical see-everything view and returns ``None``.

        A scope may span several immutable embedding models.  Admission pins
        every visible collection; the shared retrieval pipeline partitions
        that concrete set by model, embeds once per model group and fuses the
        group rankings before the common reranker.  No adapter is allowed to
        silently narrow the user's corpus to a default model.

        Args:
            collection_ids: The request's raw ``collection_ids`` filter —
                a list, an empty list, or ``None``. A non-list value is
                treated as unscoped (fail-closed to the visible set).
            visible_to: The caller's resolved visibility, or ``None`` for
                the unauthenticated see-everything modes.

        Returns:
            The concrete scope to persist: the asserted explicit list, the
            caller-visible id set (possibly empty), or ``None`` for the
            see-everything modes.

        Raises:
            CollectionNotFound: An explicit id the caller cannot see.
        """
        explicit = (
            [str(item) for item in collection_ids]
            if isinstance(collection_ids, list)
            else []
        )
        if explicit:
            await self.assert_collections_visible(explicit, visible_to=visible_to)
            return explicit
        visible = await self.list_collections(visible_to=visible_to)
        return [collection.id for collection in visible]
