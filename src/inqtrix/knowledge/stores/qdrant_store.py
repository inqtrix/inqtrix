"""Qdrant-backed knowledge: a vector index, plus a legacy sole-store.

Two consumers of one Qdrant topology:

* :class:`QdrantVectorIndex` — the vector half of the Postgres-canonical
  store (the production full-stack path). It holds ONLY vectors plus a
  lean payload (collection/document/chunk identity plus generation/revision
  selectors), keyed by the canonical chunk id. No document text lives in
  Qdrant.
* :class:`QdrantKnowledgeStore` — the legacy sole-store for the rare
  ``qdrant`` vector backend WITHOUT Postgres. It keeps a small registry
  collection for the canonical records (text in payload) and reuses the
  same chunk topology. Retained for backward compatibility; the
  full-stack path uses ``QdrantVectorIndex`` behind the Postgres store.

Topology (Qdrant's multi-tenant guidance): ONE physical Qdrant
collection per embedding-model configuration, with the logical
collection as an indexed payload field (``is_tenant`` partitioning).
Hybrid retrieval fuses a dense branch with a BM25 sparse branch that the
Qdrant server computes itself (core BM25 inference, server >= 1.15.3; a
tokenizer + Snowball stemmer ALGORITHM, German, paired with Qdrant's IDF
modifier) via reciprocal rank fusion. Nothing is downloaded at runtime.

The port is async; the synchronous ``qdrant_client`` calls run off the
event loop via ``asyncio.to_thread`` (proven sync bodies wrapped, not
rewritten). Security note: payload filters are a performance boundary,
not the authorization truth — access stays with the AuthorizationService.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import TypeVar

import asyncio
import logging
import threading
import time
import uuid
from contextlib import nullcontext
from typing import Any, Callable

from inqtrix.knowledge.evidence import source_excerpt_is_verified
from inqtrix.pagination import keyset_page
from inqtrix.knowledge.stores.ports import (
    VectorStoreUnavailable,
    CollectionNotFound,
    DocumentChunk,
    DocumentNotFound,
    DocumentRevisionReservation,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    KnowledgeCollection,
    KnowledgeDocument,
    KnowledgeDocumentRevision,
    KnowledgeError,
    RetrievalCandidate,
    RetrievalCandidateBatch,
    ReservedDocumentRevision,
    SourceDeletionConflict,
)
from inqtrix.knowledge.stores.retrieval_contract import (
    MAX_VECTOR_CANDIDATES,
    bounded_candidate_depth,
    degraded_candidates,
    validate_vector_candidate_cap,
)
from inqtrix.knowledge.stores.vector_index import (
    ChunkVector,
    VectorHit,
    VectorPointRef,
    VectorSearchScope,
)
from inqtrix.knowledge.source_cleanup import SourceCleanupPlan
from inqtrix.source_authority import SourceDeletionPermit

log = logging.getLogger("inqtrix")

REGISTRY_COLLECTION = "inqtrix_registry"
CHUNKS_PREFIX = "inqtrix_chunks__"
_REGISTRY_CAS_MAX_ATTEMPTS = 32

_IMPORT_HINT = (
    "Qdrant support requires the 'knowledge-qdrant' extra "
    "(uv sync --extra knowledge-qdrant)."
)


def _model_slug(embedding_model: str) -> str:
    return CHUNKS_PREFIX + "".join(
        ch if ch.isalnum() else "_" for ch in embedding_model.lower()
    )


def _optional_int(value: Any) -> int | None:
    """Coerce a payload value to ``int`` or ``None`` (best-effort page number)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _point_uuid(stable_id: str) -> str:
    """Deterministic Qdrant point id for a stable record/chunk id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"inqtrix://{stable_id}"))


def _source_document_id(collection_id: str, source_id: str) -> str:
    """One stable registry identity for a source inside a collection."""
    stable = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"inqtrix://knowledge-document/{collection_id}/{source_id}",
    )
    return f"kd_{stable.hex[:20]}"


def _revision_record_id(
    collection_id: str,
    source_id: str,
    content_hash: str,
    build_contract_hash: str,
) -> str:
    """One Qdrant point for the immutable source/build revision identity."""
    stable = uuid.uuid5(
        uuid.NAMESPACE_URL,
        repr((collection_id, source_id, content_hash, build_contract_hash)),
    )
    return f"krr_{stable.hex[:20]}"


def _immutable_revision_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Exclude derived chunk/build diagnostics from immutable source metadata."""
    return {
        key: value
        for key, value in metadata.items()
        if not key.startswith("_chunk_") or key == "_chunk_pages"
    }


def _payload_user_id(value: Any) -> uuid.UUID | None:
    """Restore a canonical user UUID from Qdrant's JSON payload boundary."""
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))



_T = TypeVar("_T")


def _unavailable_from(exc: BaseException) -> VectorStoreUnavailable | None:
    """Map a transport-level client failure to :class:`VectorStoreUnavailable`.

    The qdrant client wraps its httpx transport errors (connection refused,
    DNS, read timeout) in ``ResponseHandlingException``; a bare
    ``ConnectionError``/``TimeoutError`` covers other transports. Anything
    else (validation, unexpected responses) is not an outage and is left
    to the caller untouched.
    """
    transport_types: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError)
    try:
        from qdrant_client.http.exceptions import ResponseHandlingException

        transport_types = (*transport_types, ResponseHandlingException)
    except ImportError:  # pragma: no cover - env-dependent
        pass
    try:
        import httpx

        transport_types = (*transport_types, httpx.TransportError)
    except ImportError:  # pragma: no cover - env-dependent
        pass
    if isinstance(exc, transport_types):
        return VectorStoreUnavailable(
            f"vector store unreachable: {type(exc).__name__}: {exc}"
        )
    return None


async def _run_store_call(awaitable: Awaitable[_T]) -> _T:
    """Await one store call; an outage surfaces as VectorStoreUnavailable."""
    try:
        return await awaitable
    except Exception as exc:  # noqa: BLE001 - classified right below
        unavailable = _unavailable_from(exc)
        if unavailable is not None:
            raise unavailable from exc
        raise


def _require_qdrant():
    """Import the qdrant client + models, or raise the install hint."""
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(_IMPORT_HINT) from exc
    return QdrantClient, models


# The lexical (BM25) branch is monolingual. Its tokenizer language and the
# ISO-639-1 code are the SAME fact in two representations (the Document
# inference options want the English name "german"; capability manifests and
# the algorithm compare against detect_ui_language's "de"). Defined ONCE here
# so the wire options and the `sparse_language` property can never drift
# apart.
_BM25_TOKENIZER_LANGUAGE = "german"
_BM25_LANGUAGE_CODE = "de"

# BM25 inference runs inside the Qdrant server (compiled into the core since
# 1.15.2; 1.15.3 is the floor qdrant-client names for forwarding
# ``models.Document`` unchanged). The client forwards by connection MODE, not
# by server version, so a too-old server would fail per request — the startup
# gate below turns that into one actionable error instead.
_BM25_MODEL = "Qdrant/bm25"
_BM25_MIN_SERVER = (1, 15, 3)
# The server keeps arbitrarily long tokens by default; the previous
# client-side encoder dropped tokens longer than 40 characters. Pinning the
# same cap keeps pre-swap and post-swap vectors BIT-IDENTICAL (proven by the
# golden parity tests), so mixed generations coexist without a reindex.
# Lifting the cap is a deliberate future decision that requires reindexing —
# otherwise long German compounds would match only newly indexed documents.
_BM25_MAX_TOKEN_LEN = 40


class _Bm25:
    """Builds BM25 inference Documents; Qdrant expands them server-side.

    Wire objects only — no local model, no download. The server derives the
    sparse vector from the Document's position: a named-vector slot in an
    upsert gets the document TF formula, a query position gets unique tokens
    at weight 1.0, hash-compatible with client-side BM25 sparse vectors
    (murmur3-32, |i32| — proven by the golden fixtures in the live parity
    tests, so collections indexed with client-side vectors stay searchable
    without a reindex). The two methods mirror that positional distinction
    and are the
    seam offline tests stub with literal ``SparseVector`` values.
    """

    def documents(self, texts: list[str]):
        _client, models = _require_qdrant()
        return [
            models.Document(
                text=text,
                model=_BM25_MODEL,
                options={
                    "language": _BM25_TOKENIZER_LANGUAGE,
                    "max_token_len": _BM25_MAX_TOKEN_LEN,
                },
            )
            for text in texts
        ]

    def query(self, text: str):
        _client, models = _require_qdrant()
        return models.Document(
            text=text,
            model=_BM25_MODEL,
            options={
                "language": _BM25_TOKENIZER_LANGUAGE,
                "max_token_len": _BM25_MAX_TOKEN_LEN,
            },
        )


def _require_server_side_bm25(client, *, context: str) -> None:
    """Fail fast when the connected Qdrant cannot run BM25 inference.

    The sparse branch sends ``models.Document`` objects for server-side
    expansion, which needs Qdrant >= 1.15.3. qdrant-client forwards those
    Documents by connection mode without checking the server version, so a
    too-old server would reject every sparse upsert/query at request time;
    one reachable-and-too-old check at store construction converts that into
    a single actionable startup error. An UNREACHABLE server stays tolerated
    (documented startup behavior: knowledge simply is not advertised) — then
    the requirement is logged loudly and enforcement falls to the per-request
    errors plus the re-check on the next restart. Dev builds with unparseable
    version strings take the warn path as well.
    """
    minimum = ".".join(str(part) for part in _BM25_MIN_SERVER)
    try:
        raw_version = client.info().version
        parsed = tuple(int(part) for part in str(raw_version).split(".")[:3])
        if len(parsed) < 3:
            raise ValueError(f"unparseable version: {raw_version!r}")
    except Exception as exc:  # noqa: BLE001 - any probe failure => warn path
        log.warning(
            "%s: Qdrant version could not be determined (%s); "
            "INQTRIX_KNOWLEDGE_SPARSE=bm25_german requires Qdrant >= %s (server-side "
            "BM25 inference). The check runs again at next startup; an older server "
            "will fail visibly on each request.",
            context,
            exc,
            minimum,
        )
        return
    if parsed < _BM25_MIN_SERVER:
        raise RuntimeError(
            f'{context}: Qdrant {raw_version} does not support server-side BM25; INQTRIX_KNOWLEDGE_SPARSE=bm25_german requires Qdrant >= {minimum}. Upgrade the Qdrant server or set INQTRIX_KNOWLEDGE_SPARSE=off for dense search only.'
        )


def _ensure_chunks_collection(
    client, models, *, name: str, embedding_dim: int, sparse_enabled: bool
) -> None:
    """Create the per-model chunk collection if absent (idempotent)."""
    if not client.collection_exists(name):
        sparse_config = (
            {"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)}
            if sparse_enabled
            else None
        )
        client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": models.VectorParams(
                    size=embedding_dim, distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config=sparse_config,
        )
    # Create missing payload indexes for pre-existing model collections too.
    # Qdrant treats an identical create_payload_index request idempotently.
    for field_name, schema in (
        (
            "collection_id",
            models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
        ),
        ("document_id", models.PayloadSchemaType.KEYWORD),
        ("generation_id", models.PayloadSchemaType.KEYWORD),
        ("revision_id", models.PayloadSchemaType.KEYWORD),
    ):
        client.create_payload_index(
            collection_name=name,
            field_name=field_name,
            field_schema=schema,
        )


def _scope_filter(models, collection_ids: list[str]):
    return models.Filter(
        must=[
            models.FieldCondition(
                key="collection_id",
                match=models.MatchAny(any=list(collection_ids)),
            )
        ]
    )


def _active_scope_filter(models, scopes: list[VectorSearchScope]):
    """Build an exact OR of canonical collection/generation/revision scopes.

    ``IsEmptyCondition`` deliberately means absent *or* null for legacy
    payloads.  Ordinarily it is emitted only for a scope whose canonical
    pointer is explicitly ``None``.  The one additional branch is an exact
    document allow-set for the migration-marked compatibility generation:
    those persisted points predate both lineage fields, while Postgres already
    projects their canonical revision/generation and verifies their source
    bytes during hydration.
    """

    collection_conditions = []
    for scope in scopes:
        revision_conditions = []
        if scope.active_revision_ids:
            revision_conditions.append(
                models.FieldCondition(
                    key="revision_id",
                    match=models.MatchAny(any=list(scope.active_revision_ids)),
                )
            )
        if scope.legacy_document_ids:
            revision_conditions.append(
                models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchAny(
                                any=list(scope.legacy_document_ids)
                            ),
                        ),
                        models.IsEmptyCondition(
                            is_empty=models.PayloadField(key="revision_id")
                        ),
                    ]
                )
            )
        generation_condition = (
            models.IsEmptyCondition(
                is_empty=models.PayloadField(key="generation_id")
            )
            if scope.generation_id is None
            else models.FieldCondition(
                key="generation_id",
                match=models.MatchValue(value=scope.generation_id),
            )
        )
        payload_conditions = []
        if revision_conditions:
            payload_conditions.append(
                models.Filter(
                    must=[
                        generation_condition,
                        models.Filter(should=revision_conditions),
                    ]
                )
            )
        if scope.legacy_payload_chunk_ids:
            payload_conditions.append(
                models.Filter(
                    must=[
                        models.FieldCondition(
                            key="chunk_id",
                            match=models.MatchAny(
                                any=list(scope.legacy_payload_chunk_ids)
                            ),
                        ),
                        models.IsEmptyCondition(
                            is_empty=models.PayloadField(key="generation_id")
                        ),
                        models.IsEmptyCondition(
                            is_empty=models.PayloadField(key="revision_id")
                        ),
                    ]
                )
            )
        # An empty active document set must match no vector points. Omitting
        # the collection branch is safer than manufacturing a sentinel value.
        if not payload_conditions:
            continue
        collection_conditions.append(
            models.Filter(
                must=[
                    models.FieldCondition(
                        key="collection_id",
                        match=models.MatchValue(value=scope.collection_id),
                    ),
                    models.Filter(should=payload_conditions),
                ]
            )
        )
    if not collection_conditions:
        return None
    return models.Filter(should=collection_conditions)


def _hits_from_points(points) -> list[VectorHit]:
    hits: list[VectorHit] = []
    for point in points:
        payload = dict(point.payload or {})
        chunk_id = payload.get("chunk_id")
        if not chunk_id:
            continue
        hits.append(VectorHit(chunk_id=str(chunk_id), score=float(point.score or 0.0)))
    return hits


class QdrantVectorIndex:
    """Vector-only Qdrant index (lean payload, keyed by canonical chunk id).

    Implements :class:`~inqtrix.knowledge.stores.vector_index.VectorIndex`.
    The Postgres-canonical store owns documents/chunks; this index owns
    only the dense (+ optional BM25 sparse) vectors and the keys needed to
    filter and to join back.

    Args:
        url: Qdrant REST endpoint.
        api_key: Qdrant API key (empty accepted for loopback dev, logged).
        sparse: ``"bm25_german"`` enables hybrid; ``"off"`` is dense-only.
        timeout: Per-call timeout in seconds.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        sparse: str = "bm25_german",
        timeout: float = 30.0,
    ) -> None:
        QdrantClient, _models = _require_qdrant()
        if not api_key:
            log.warning(
                "QdrantVectorIndex has no API key; acceptable only for loopback "
                "development (self-hosted Qdrant has no authentication by default)."
            )
        if sparse not in ("bm25_german", "off"):
            raise ValueError(f"unknown sparse mode: {sparse!r}")
        self._client = QdrantClient(url=url, api_key=api_key or None, timeout=timeout)
        self._sparse_enabled = sparse == "bm25_german"
        self._bm25 = _Bm25()
        if self._sparse_enabled:
            _require_server_side_bm25(self._client, context="QdrantVectorIndex")

    @property
    def supports_hybrid(self) -> bool:
        return self._sparse_enabled

    @property
    def sparse_language(self) -> str | None:
        """ISO 639-1 code of the BM25 tokenizer language, ``None`` when off.

        The lexical branch tokenizes/stems in exactly one language ("de"
        today, hardcoded ``language="german"``). Read-only: surfaced so the
        capability manifest and the knowledge algorithm can make the
        monolingual limitation of keyword retrieval visible — BM25 is
        language-bound and never cross-lingual. Optional by convention;
        consumers read it via ``getattr(store, "sparse_language", None)``.
        """
        return _BM25_LANGUAGE_CODE if self._sparse_enabled else None

    async def is_available(self) -> bool:
        """Return whether the Qdrant endpoint responds to a read-only call."""
        try:
            await asyncio.to_thread(self._client.get_collections)
            return True
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            # The runtime probe only records "unavailable"; the cause
            # (refused, auth, TLS, validation) is visible here or nowhere.
            log.warning(
                "Qdrant availability probe failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    async def ensure_model(
        self, *, embedding_model: str, embedding_dim: int
    ) -> None:
        _client, models = _require_qdrant()
        await asyncio.to_thread(
            _ensure_chunks_collection,
            self._client,
            models,
            name=_model_slug(embedding_model),
            embedding_dim=embedding_dim,
            sparse_enabled=self._sparse_enabled,
        )

    async def upsert(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        document_id: str,
        vectors: list[ChunkVector],
    ) -> None:
        if not vectors:
            return
        await asyncio.to_thread(
            self._sync_upsert, embedding_model, collection_id, document_id, vectors
        )

    def _sync_upsert(
        self,
        embedding_model: str,
        collection_id: str,
        document_id: str,
        vectors: list[ChunkVector],
    ) -> None:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        sparse_vectors = (
            self._bm25.documents([v.text for v in vectors])
            if self._sparse_enabled
            else None
        )
        points = []
        for index, chunk in enumerate(vectors):
            vector: dict[str, Any] = {"dense": list(chunk.dense)}
            if sparse_vectors is not None:
                vector["sparse"] = sparse_vectors[index]
            points.append(
                models.PointStruct(
                    id=_point_uuid(chunk.chunk_id),
                    vector=vector,
                    payload={
                        "collection_id": collection_id,
                        "document_id": document_id,
                        "chunk_id": chunk.chunk_id,
                        "generation_id": chunk.generation_id,
                        "revision_id": chunk.revision_id,
                    },
                )
            )
        self._client.upsert(collection_name=name, points=points, wait=True)

    async def delete_document(
        self, *, embedding_model: str, document_id: str
    ) -> None:
        await asyncio.to_thread(
            self._sync_delete_by, embedding_model, "document_id", document_id
        )

    async def count_document(
        self, *, embedding_model: str, document_id: str
    ) -> int:
        return await asyncio.to_thread(
            self._sync_count_by, embedding_model, "document_id", document_id
        )

    async def delete_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> None:
        if not chunk_ids:
            return
        await asyncio.to_thread(
            self._sync_delete_chunks, embedding_model, chunk_ids
        )

    def _sync_delete_chunks(
        self, embedding_model: str, chunk_ids: list[str]
    ) -> None:
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return
        self._client.delete(
            collection_name=name,
            points_selector=[_point_uuid(chunk_id) for chunk_id in chunk_ids],
            wait=True,
        )

    async def count_chunks(
        self, *, embedding_model: str, chunk_ids: list[str]
    ) -> int:
        if not chunk_ids:
            return 0
        return await asyncio.to_thread(
            self._sync_count_chunks, embedding_model, chunk_ids
        )

    async def count_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> int:
        return await asyncio.to_thread(
            self._sync_count_generation,
            embedding_model,
            collection_id,
            generation_id,
        )

    async def delete_generation(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._sync_delete_generation,
            embedding_model,
            collection_id,
            generation_id,
        )

    async def count_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> int:
        return await asyncio.to_thread(
            self._sync_count_generation_document,
            embedding_model,
            collection_id,
            generation_id,
            document_id,
        )

    async def delete_generation_document(
        self,
        *,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._sync_delete_generation_document,
            embedding_model,
            collection_id,
            generation_id,
            document_id,
        )

    def _sync_delete_generation(
        self,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> None:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return
        self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="collection_id",
                            match=models.MatchValue(value=collection_id),
                        ),
                        models.FieldCondition(
                            key="generation_id",
                            match=models.MatchValue(value=generation_id),
                        ),
                    ]
                )
            ),
            wait=True,
        )

    def _sync_count_generation(
        self,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
    ) -> int:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return 0
        result = self._client.count(
            collection_name=name,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="collection_id",
                        match=models.MatchValue(value=collection_id),
                    ),
                    models.FieldCondition(
                        key="generation_id",
                        match=models.MatchValue(value=generation_id),
                    ),
                ]
            ),
            exact=True,
        )
        return int(result.count)

    def _generation_document_filter(
        self,
        models,
        *,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ):
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="collection_id",
                    match=models.MatchValue(value=collection_id),
                ),
                models.FieldCondition(
                    key="generation_id",
                    match=models.MatchValue(value=generation_id),
                ),
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=document_id),
                ),
            ]
        )

    def _sync_delete_generation_document(
        self,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> None:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return
        self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=self._generation_document_filter(
                    models,
                    collection_id=collection_id,
                    generation_id=generation_id,
                    document_id=document_id,
                )
            ),
            wait=True,
        )

    def _sync_count_generation_document(
        self,
        embedding_model: str,
        collection_id: str,
        generation_id: str,
        document_id: str,
    ) -> int:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return 0
        result = self._client.count(
            collection_name=name,
            count_filter=self._generation_document_filter(
                models,
                collection_id=collection_id,
                generation_id=generation_id,
                document_id=document_id,
            ),
            exact=True,
        )
        return int(result.count)

    def _sync_count_chunks(
        self, embedding_model: str, chunk_ids: list[str]
    ) -> int:
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return 0
        points = self._client.retrieve(
            collection_name=name,
            ids=[_point_uuid(chunk_id) for chunk_id in chunk_ids],
            with_payload=False,
            with_vectors=False,
        )
        return len(points)

    def point_ids_for_chunks(self, chunk_ids: list[str]) -> list[str]:
        return [_point_uuid(chunk_id) for chunk_id in chunk_ids]

    async def delete_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> None:
        await asyncio.to_thread(
            self._sync_delete_by, embedding_model, "collection_id", collection_id
        )

    async def count_collection(
        self, *, embedding_model: str, collection_id: str
    ) -> int:
        return await asyncio.to_thread(
            self._sync_count_by, embedding_model, "collection_id", collection_id
        )

    def _sync_count_by(self, embedding_model: str, key: str, value: str) -> int:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return 0
        result = self._client.count(
            collection_name=name,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key=key, match=models.MatchValue(value=value)
                    )
                ]
            ),
            exact=True,
        )
        return int(result.count)

    def _sync_delete_by(self, embedding_model: str, key: str, value: str) -> None:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return
        self._client.delete(
            collection_name=name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key=key, match=models.MatchValue(value=value)
                        )
                    ]
                )
            ),
            wait=True,
        )

    async def search(
        self,
        *,
        embedding_model: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        return await _run_store_call(asyncio.to_thread(
            self._sync_search, embedding_model, query_embedding, scopes, top_k
        ))

    def _sync_search(
        self,
        embedding_model: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return []
        scope = _active_scope_filter(models, scopes)
        if scope is None:
            return []
        result = self._client.query_points(
            collection_name=name,
            query=query_embedding,
            using="dense",
            query_filter=scope,
            limit=top_k,
            with_payload=True,
        )
        return _hits_from_points(result.points)

    async def hybrid_search(
        self,
        *,
        embedding_model: str,
        query_text: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        if not self._sparse_enabled:
            raise RuntimeError(
                "hybrid_search requires sparse retrieval "
                "(INQTRIX_KNOWLEDGE_SPARSE=bm25_german)"
            )
        return await _run_store_call(asyncio.to_thread(
            self._sync_hybrid_search,
            embedding_model,
            query_text,
            query_embedding,
            scopes,
            top_k,
        ))

    def _sync_hybrid_search(
        self,
        embedding_model: str,
        query_text: str,
        query_embedding: list[float],
        scopes: list[VectorSearchScope],
        top_k: int,
    ) -> list[VectorHit]:
        _client, models = _require_qdrant()
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return []
        scope = _active_scope_filter(models, scopes)
        if scope is None:
            return []
        sparse = self._bm25.query(query_text)
        prefetch_depth = max(top_k * 4, 20)
        result = self._client.query_points(
            collection_name=name,
            prefetch=[
                models.Prefetch(
                    query=query_embedding,
                    using="dense",
                    filter=scope,
                    limit=prefetch_depth,
                ),
                models.Prefetch(
                    query=sparse,
                    using="sparse",
                    filter=scope,
                    limit=prefetch_depth,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return _hits_from_points(result.points)

    async def scroll_chunk_groups(
        self, *, embedding_model: str
    ) -> set[tuple[str, str]]:
        return await asyncio.to_thread(self._sync_scroll_groups, embedding_model)

    def _sync_scroll_groups(self, embedding_model: str) -> set[tuple[str, str]]:
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return set()
        groups: set[tuple[str, str]] = set()
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = point.payload or {}
                collection_id = payload.get("collection_id")
                document_id = payload.get("document_id")
                if collection_id and document_id:
                    groups.add((str(collection_id), str(document_id)))
            if offset is None:
                break
        return groups

    async def scroll_chunk_points(
        self, *, embedding_model: str
    ) -> list[VectorPointRef]:
        return await asyncio.to_thread(self._sync_scroll_points, embedding_model)

    def _sync_scroll_points(self, embedding_model: str) -> list[VectorPointRef]:
        name = _model_slug(embedding_model)
        if not self._client.collection_exists(name):
            return []
        refs: list[VectorPointRef] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=name,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                payload = dict(point.payload or {})
                chunk_id = payload.get("chunk_id")
                collection_id = payload.get("collection_id")
                document_id = payload.get("document_id")
                if not chunk_id or not collection_id or not document_id:
                    log.warning(
                        "Qdrant reconcile skipped malformed point %s in %s; "
                        "chunk_id/collection_id/document_id payload is required",
                        point.id,
                        name,
                    )
                    continue
                refs.append(
                    VectorPointRef(
                        chunk_id=str(chunk_id),
                        collection_id=str(collection_id),
                        document_id=str(document_id),
                        generation_id=(
                            str(payload["generation_id"])
                            if payload.get("generation_id") is not None
                            else None
                        ),
                        revision_id=(
                            str(payload["revision_id"])
                            if payload.get("revision_id") is not None
                            else None
                        ),
                    )
                )
            if offset is None:
                break
        return refs


class QdrantKnowledgeStore:
    """Legacy sole-store: canonical registry + chunk vectors in Qdrant.

    Implements the async ``KnowledgeStore`` port for the ``qdrant`` vector
    backend WITHOUT Postgres. The full-stack path uses the Postgres store
    with :class:`QdrantVectorIndex` instead. The proven synchronous bodies
    are wrapped with ``asyncio.to_thread`` so the now-async port never
    blocks the event loop.

    Document revisions use a two-phase publication contract. Chunks are
    revision-addressed and remain invisible until a conditional update moves
    the canonical document pointer. Strong read-back is mandatory because
    Qdrant acknowledges a filtered no-op with the same operation status as a
    successful conditional update.

    ``vector_candidate_cap`` may lower the shared 512-candidate safety limit
    for constrained library deployments. Values above the hard limit are
    rejected instead of silently allowing unbounded geometric overfetch.
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str = "",
        sparse: str = "bm25_german",
        timeout: float = 30.0,
        vector_candidate_cap: int = MAX_VECTOR_CANDIDATES,
    ) -> None:
        safe_candidate_cap = validate_vector_candidate_cap(
            vector_candidate_cap
        )
        QdrantClient, _models = _require_qdrant()
        if not api_key:
            log.warning(
                "QdrantKnowledgeStore has no API key; acceptable only for loopback "
                "development (self-hosted Qdrant has no authentication by default)."
            )
        if sparse not in ("bm25_german", "off"):
            raise ValueError(f"unknown sparse mode: {sparse!r}")
        self._client = QdrantClient(url=url, api_key=api_key or None, timeout=timeout)
        self._sparse_enabled = sparse == "bm25_german"
        self._bm25 = _Bm25()
        self._registry_ready = False
        self._revision_lock = threading.RLock()
        self._vector_candidate_cap = safe_candidate_cap
        if self._sparse_enabled:
            _require_server_side_bm25(
                self._client, context="QdrantKnowledgeStore"
            )

    @property
    def supports_safe_reindex(self) -> bool:
        """Legacy Qdrant-only storage has no cross-process mutation fence."""
        return False

    @property
    def supports_async_document_revisions(self) -> bool:
        """Qdrant conditionally owns the cross-process document pointer."""
        return True

    @property
    def supports_collection_sharing(self) -> bool:
        """Vector-only metadata cannot form an atomic sharing boundary."""
        return False

    @property
    def supports_hybrid(self) -> bool:
        return self._sparse_enabled

    @property
    def sparse_language(self) -> str | None:
        """ISO 639-1 code of the BM25 tokenizer language, ``None`` when off.

        Same contract as :attr:`QdrantVectorIndex.sparse_language`: the lexical
        branch is monolingual ("de" today). Read-only, consumed via ``getattr``.
        """
        return _BM25_LANGUAGE_CODE if self._sparse_enabled else None

    async def is_available(self) -> bool:
        """Return whether the Qdrant endpoint responds to a read-only call."""
        try:
            await asyncio.to_thread(self._client.get_collections)
            return True
        except Exception as exc:  # noqa: BLE001 - the probe reports, never raises
            # The runtime probe only records "unavailable"; the cause
            # (refused, auth, TLS, validation) is visible here or nowhere.
            log.warning(
                "Qdrant availability probe failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            return False

    # -- async port (thin wrappers over the proven sync bodies) ---------- #

    async def create_collection(
        self,
        *,
        name,
        embedding_model,
        embedding_dim,
        created_by_user_id: uuid.UUID | None = None,
    ) -> KnowledgeCollection:
        return await asyncio.to_thread(
            self._sync_create_collection,
            name,
            embedding_model,
            embedding_dim,
            created_by_user_id,
        )

    async def list_collections(self) -> list[KnowledgeCollection]:
        # Registry reads run BEFORE every search on the ask path; an outage
        # there must be the same typed failure as an outage in the search.
        return await _run_store_call(
            asyncio.to_thread(self._sync_list_collections)
        )

    async def get_collection(self, collection_id: str) -> KnowledgeCollection:
        return await _run_store_call(
            asyncio.to_thread(self._sync_get_collection, collection_id)
        )

    async def collection_share_facts(self, collection_ids):
        """Collection sharing is unsupported by this store
        (:attr:`supports_collection_sharing`), so the share layer never asks."""
        raise NotImplementedError(
            "The Qdrant knowledge store does not support collection sharing"
        )

    async def delete_collection(
        self,
        collection_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        await asyncio.to_thread(self._sync_delete_collection, collection_id)

    async def count_collection_residuals(
        self,
        *,
        collection_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        return await asyncio.to_thread(
            self._sync_count_collection_residuals,
            collection_id,
            embedding_model,
        )

    async def add_document(
        self,
        *,
        collection_id,
        title,
        text,
        metadata,
        chunks,
        embeddings,
        source_id=None,
        source_chunks=None,
        retrieval_contexts=None,
        source_spans=None,
        document_content_hash=None,
        revision_id=None,
        generation_id=None,
        page_numbers=None,
        source_scope=None,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeDocument:
        del source_scope
        return await asyncio.to_thread(
            self._sync_add_document,
            collection_id,
            title,
            text,
            metadata,
            chunks,
            embeddings,
            source_id,
            source_chunks,
            retrieval_contexts,
            source_spans,
            document_content_hash,
            revision_id,
            generation_id,
            page_numbers,
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
        metadata: dict[str, Any] | None = None,
        source_scope=None,
        source_create_if_missing: bool = False,
        actor_user_id: uuid.UUID | None = None,
    ) -> DocumentRevisionReservation:
        return await asyncio.to_thread(
            self._sync_reserve_document_revision,
            collection_id,
            source_id,
            revision_id,
            content_hash,
            build_contract_hash,
            title,
            text,
            dict(metadata or {}),
        )

    async def load_reserved_document_revision(
        self,
        *,
        document_id: str,
        revision_id: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> ReservedDocumentRevision:
        return await asyncio.to_thread(
            self._sync_load_reserved_document_revision,
            document_id,
            revision_id,
        )

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
        del fence_job_id, fence_attempt
        return await asyncio.to_thread(
            self._sync_publish_document_revision,
            reservation,
            title,
            text,
            metadata,
            chunks,
            embeddings,
            source_chunks,
            retrieval_contexts,
            source_spans,
            page_numbers,
            generation_id,
            publication_guard,
        )

    async def list_documents(self, collection_id: str) -> list[KnowledgeDocument]:
        return await asyncio.to_thread(self._sync_list_documents, collection_id)

    async def list_documents_page(
        self,
        collection_id: str,
        *,
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[KnowledgeDocument], str | None]:
        """One keyset page (newest first, ``(created_at, id)`` tiebreak).

        Qdrant is the lean vector tier; document listing is not the
        efficiency-critical path, so this slices the full list in memory to
        keep byte-identical paging with the other tiers."""
        documents = await self.list_documents(collection_id)
        documents.sort(key=lambda d: (d.created_at, d.id), reverse=True)
        return keyset_page(
            documents,
            limit=limit,
            after=after,
            created_at_of=lambda d: d.created_at,
            id_of=lambda d: d.id,
        )

    async def get_document(self, document_id: str) -> KnowledgeDocument:
        return await asyncio.to_thread(self._sync_get_document, document_id)

    async def get_chunks(self, document_id: str) -> list[DocumentChunk]:
        """One document's active chunks ordered by index, without vectors."""
        return await asyncio.to_thread(self._sync_get_chunks, document_id)

    async def delete_document(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        await asyncio.to_thread(self._sync_delete_document, document_id)

    async def delete_document_for_aggregate(
        self,
        document_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        del actor_user_id
        await asyncio.to_thread(self._sync_delete_document, document_id)

    async def mark_document_deleting(self, document_id: str) -> None:
        await asyncio.to_thread(
            self._sync_set_document_lifecycle, document_id, "deleting"
        )

    async def restore_document_active(self, document_id: str) -> None:
        await asyncio.to_thread(
            self._sync_set_document_lifecycle,
            document_id,
            "active",
            True,
        )

    async def count_document_residuals(
        self,
        *,
        document_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        return await asyncio.to_thread(
            self._sync_count_document_residuals,
            document_id,
            embedding_model,
        )

    async def list_documents_by_source(
        self,
        source_id: str,
        *,
        collection_id: str | None = None,
    ) -> list[KnowledgeDocument]:
        documents = await asyncio.to_thread(self._sync_list_all_documents)
        return [
            document
            for document in documents
            if document.lifecycle_status != "deleted"
            and (
                collection_id is None
                or document.collection_id == collection_id
            )
            and self._document_matches_source(document, source_id)
        ]

    async def mark_source_deleting(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        if deletion_permit is not None:
            raise KnowledgeError(
                "aggregate source deletion requires the canonical Postgres "
                "or shared-memory source authority"
            )
        return await asyncio.to_thread(self._sync_mark_source_deleting, source_id)

    async def delete_source(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        if deletion_permit is not None or cleanup_plan is not None:
            raise KnowledgeError(
                "aggregate source deletion requires the canonical Postgres "
                "or shared-memory source authority"
            )
        documents = await self.list_documents_by_source(source_id)
        for document in documents:
            await self.delete_document(document.id, actor_user_id=actor_user_id)
        return len(documents)

    async def source_residuals(
        self,
        source_id: str,
        *,
        deletion_permit: SourceDeletionPermit | None = None,
        cleanup_plan: SourceCleanupPlan | None = None,
    ) -> dict[str, int]:
        if deletion_permit is not None or cleanup_plan is not None:
            raise KnowledgeError(
                "aggregate source deletion requires the canonical Postgres "
                "or shared-memory source authority"
            )
        documents = await self.list_documents_by_source(source_id)
        chunks = sum(document.chunk_count for document in documents)
        return {"documents": len(documents), "chunks": chunks, "vectors": chunks}

    async def reembed_document(
        self,
        *,
        document_id,
        chunks,
        embeddings,
        source_chunks=None,
        retrieval_contexts=None,
        source_spans=None,
        document_content_hash=None,
        revision_id=None,
        generation_id=None,
        fence_job_id=None,
        fence_attempt=None,
        page_numbers=None,
        actor_user_id: uuid.UUID | None = None,
    ) -> KnowledgeDocument:
        return await asyncio.to_thread(
            self._sync_reembed_document,
            document_id,
            chunks,
            embeddings,
            source_chunks,
            retrieval_contexts,
            source_spans,
            document_content_hash,
            revision_id,
            generation_id,
            page_numbers,
        )

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
        expected_validation=None,
        build_contract_hash: str = "",
        rollback_retention_seconds: int = 7 * 24 * 60 * 60,
    ) -> KnowledgeCollection:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot atomically activate generations"
        )

    async def begin_generation(self, **_kwargs):
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot persist generation history"
        )

    async def remove_document_from_generation(self, **_kwargs) -> int:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot reconcile generation deltas"
        )

    async def reset_generation_for_raw_choice(self, **_kwargs) -> int:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot reset generation history"
        )

    async def rollback_generation(self, **_kwargs) -> KnowledgeCollection:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot rollback generations"
        )

    async def prune_expired_generations(self, **_kwargs) -> int:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot prune generation history"
        )

    async def generation_cleanup_collection_ids(self, **_kwargs) -> list[str]:
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot inspect generation history"
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
        raise KnowledgeError(
            "legacy Qdrant-only storage cannot safely discard generations"
        )

    async def search(
        self, *, query_embedding, collection_ids, top_k, embedding_model=None
    ) -> RetrievalCandidateBatch:
        if top_k <= 0 or collection_ids == []:
            return RetrievalCandidateBatch()
        return await _run_store_call(asyncio.to_thread(
            self._sync_search, query_embedding, collection_ids, top_k, embedding_model
        ))

    async def hybrid_search(
        self,
        *,
        query_text,
        query_embedding,
        collection_ids,
        top_k,
        embedding_model=None,
    ) -> RetrievalCandidateBatch:
        if top_k <= 0 or collection_ids == []:
            return RetrievalCandidateBatch()
        if not self._sparse_enabled:
            raise RuntimeError(
                "hybrid_search requires sparse retrieval "
                "(INQTRIX_KNOWLEDGE_SPARSE=bm25_german)"
            )
        return await _run_store_call(asyncio.to_thread(
            self._sync_hybrid_search,
            query_text,
            query_embedding,
            collection_ids,
            top_k,
            embedding_model,
        ))

    # -- registry --------------------------------------------------------- #

    def _ensure_registry(self) -> None:
        if self._registry_ready:
            return
        _client, models = _require_qdrant()
        if not self._client.collection_exists(REGISTRY_COLLECTION):
            try:
                self._client.create_collection(
                    collection_name=REGISTRY_COLLECTION,
                    vectors_config={
                        "dummy": models.VectorParams(
                            size=1, distance=models.Distance.COSINE
                        )
                    },
                )
            except Exception:
                # Two cold replicas may both observe the collection as absent.
                # The create loser is harmless only when the winner's
                # collection is now visible; every other dependency failure
                # remains loud.
                if not self._client.collection_exists(REGISTRY_COLLECTION):
                    raise
        for field_name, schema in (
            ("kind", models.PayloadSchemaType.KEYWORD),
            ("record_id", models.PayloadSchemaType.KEYWORD),
            ("collection_id", models.PayloadSchemaType.KEYWORD),
            ("source_id", models.PayloadSchemaType.KEYWORD),
            ("document_id", models.PayloadSchemaType.KEYWORD),
            ("revision_id", models.PayloadSchemaType.KEYWORD),
            ("content_hash", models.PayloadSchemaType.KEYWORD),
            ("build_contract_hash", models.PayloadSchemaType.KEYWORD),
            ("status", models.PayloadSchemaType.KEYWORD),
            ("lifecycle_status", models.PayloadSchemaType.KEYWORD),
            ("desired_revision_id", models.PayloadSchemaType.KEYWORD),
            ("desired_sequence", models.PayloadSchemaType.INTEGER),
        ):
            self._client.create_payload_index(
                collection_name=REGISTRY_COLLECTION,
                field_name=field_name,
                field_schema=schema,
            )
        self._registry_ready = True

    def _registry_get(self, kind: str, record_id: str) -> dict[str, Any] | None:
        _client, models = _require_qdrant()
        self._ensure_registry()
        points = self._client.retrieve(
            collection_name=REGISTRY_COLLECTION,
            ids=[_point_uuid(record_id)],
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if not points:
            return None
        payload = dict(points[0].payload or {})
        if payload.get("kind") != kind or payload.get("record_id") != record_id:
            return None
        return payload

    @staticmethod
    def _registry_value_condition(models, key: str, value: Any):
        if value is None:
            return models.IsEmptyCondition(
                is_empty=models.PayloadField(key=key)
            )
        return models.FieldCondition(
            key=key,
            match=models.MatchValue(value=value),
        )

    @staticmethod
    def _registry_point(models, payload: dict[str, Any]):
        return models.PointStruct(
            id=_point_uuid(str(payload["record_id"])),
            vector={"dummy": [0.0]},
            payload=payload,
        )

    def _registry_upsert(self, payload: dict[str, Any]) -> None:
        _client, models = _require_qdrant()
        self._ensure_registry()
        self._client.upsert(
            collection_name=REGISTRY_COLLECTION,
            points=[self._registry_point(models, payload)],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )

    def _registry_insert_only(self, payload: dict[str, Any]) -> None:
        _client, models = _require_qdrant()
        self._ensure_registry()
        self._client.upsert(
            collection_name=REGISTRY_COLLECTION,
            points=[self._registry_point(models, payload)],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
            update_mode=models.UpdateMode.INSERT_ONLY,
        )

    def _registry_compare_and_swap(
        self,
        payload: dict[str, Any],
        *,
        expected: dict[str, Any],
        fields: tuple[str, ...],
    ) -> None:
        """Conditionally replace one registry point.

        Qdrant returns ``completed`` for both an applied conditional update and
        a filtered no-op. Callers therefore must strongly read the point back
        and compare their unique desired revision before treating this as a
        successful CAS.
        """
        _client, models = _require_qdrant()
        self._ensure_registry()
        conditions = [
            self._registry_value_condition(models, field, expected.get(field))
            for field in fields
        ]
        self._client.upsert(
            collection_name=REGISTRY_COLLECTION,
            points=[self._registry_point(models, payload)],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
            update_filter=models.Filter(must=conditions),
            update_mode=models.UpdateMode.UPDATE_ONLY,
        )

    def _registry_delete(self, record_id: str) -> None:
        _client, models = _require_qdrant()
        self._client.delete(
            collection_name=REGISTRY_COLLECTION,
            points_selector=[_point_uuid(record_id)],
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )

    def _registry_documents_for_source(
        self, collection_id: str, source_id: str
    ) -> list[dict[str, Any]]:
        """Return active or staging identities, including legacy source keys."""
        _client, models = _require_qdrant()
        self._ensure_registry()
        payloads: list[dict[str, Any]] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=REGISTRY_COLLECTION,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind",
                            match=models.MatchValue(value="document"),
                        ),
                        models.FieldCondition(
                            key="collection_id",
                            match=models.MatchValue(value=collection_id),
                        ),
                    ]
                ),
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                consistency=models.ReadConsistencyType.ALL,
            )
            for point in points:
                payload = dict(point.payload or {})
                document = self._document_payload(payload)
                if (
                    document.lifecycle_status != "deleted"
                    and self._document_matches_source(document, source_id)
                ):
                    payloads.append(payload)
            if offset is None:
                return payloads

    def _registry_revision_by_id(
        self, revision_id: str
    ) -> dict[str, Any] | None:
        """Read one revision by its public id, including legacy point ids."""
        _client, models = _require_qdrant()
        self._ensure_registry()
        points, _offset = self._client.scroll(
            collection_name=REGISTRY_COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="kind",
                        match=models.MatchValue(value="document_revision"),
                    ),
                    models.FieldCondition(
                        key="revision_id",
                        match=models.MatchValue(value=revision_id),
                    ),
                ]
            ),
            limit=2,
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if len(points) > 1:
            raise KnowledgeError(
                "Qdrant registry contains multiple records for revision "
                f"{revision_id!r}"
            )
        return dict(points[0].payload or {}) if points else None

    def _registry_revision_for_build(
        self,
        *,
        collection_id: str,
        source_id: str,
        content_hash: str,
        build_contract_hash: str,
    ) -> dict[str, Any] | None:
        """Read the unique immutable revision for one source/build identity."""
        _client, models = _require_qdrant()
        self._ensure_registry()
        points, _offset = self._client.scroll(
            collection_name=REGISTRY_COLLECTION,
            scroll_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="kind",
                        match=models.MatchValue(value="document_revision"),
                    ),
                    models.FieldCondition(
                        key="collection_id",
                        match=models.MatchValue(value=collection_id),
                    ),
                    models.FieldCondition(
                        key="source_id",
                        match=models.MatchValue(value=source_id),
                    ),
                    models.FieldCondition(
                        key="content_hash",
                        match=models.MatchValue(value=content_hash),
                    ),
                    models.FieldCondition(
                        key="build_contract_hash",
                        match=models.MatchValue(value=build_contract_hash),
                    ),
                ]
            ),
            limit=2,
            with_payload=True,
            with_vectors=False,
            consistency=models.ReadConsistencyType.ALL,
        )
        if len(points) > 1:
            raise KnowledgeError(
                "Qdrant registry contains multiple revisions for the same "
                f"source/build identity {source_id!r}"
            )
        return dict(points[0].payload or {}) if points else None

    def _mark_document_deleting_after_tombstone(
        self, payload: dict[str, Any]
    ) -> None:
        """Best-effort detach when a source tombstone wins a mutation race."""
        if payload.get("lifecycle_status") == "deleting":
            return
        deleting = dict(payload)
        deleting["lifecycle_status"] = "deleting"
        self._registry_compare_and_swap(
            deleting,
            expected=payload,
            fields=(
                "kind",
                "record_id",
                "collection_id",
                "lifecycle_status",
                "desired_revision_id",
                "active_revision_id",
                "desired_sequence",
            ),
        )

    def _set_revision_status(self, revision_id: str, status: str) -> None:
        """Conditionally update one secondary revision-status projection."""
        for _attempt in range(_REGISTRY_CAS_MAX_ATTEMPTS):
            payload = self._registry_revision_by_id(revision_id)
            if payload is None:
                return
            if payload.get("status") == status:
                return
            updated = dict(payload)
            updated["status"] = status
            if status == "active":
                updated["activated_at"] = time.time()
                updated.pop("superseded_at", None)
            elif status == "superseded":
                updated["superseded_at"] = time.time()
            self._registry_compare_and_swap(
                updated,
                expected=payload,
                fields=("kind", "record_id", "revision_id", "status"),
            )
            current = self._registry_revision_by_id(revision_id)
            if current is not None and current.get("status") == status:
                return
        raise KnowledgeError(
            "Qdrant revision status did not converge after "
            f"{_REGISTRY_CAS_MAX_ATTEMPTS} conditional updates"
        )

    def _project_revision_active(
        self, document_id: str, revision_id: str
    ) -> None:
        """Project ``active`` only while the document pointer still owns it."""
        document = self._registry_get("document", document_id)
        if document is None or document.get("active_revision_id") != revision_id:
            return
        self._set_revision_status(revision_id, "active")
        current = self._registry_get("document", document_id)
        if current is None or current.get("active_revision_id") != revision_id:
            self._set_revision_status(revision_id, "superseded")

    def _project_revision_superseded(
        self, document_id: str, revision_id: str
    ) -> None:
        """Project ``superseded`` unless a concurrent pointer reactivated it."""
        document = self._registry_get("document", document_id)
        if document is not None and document.get("active_revision_id") == revision_id:
            return
        self._set_revision_status(revision_id, "superseded")
        current = self._registry_get("document", document_id)
        if current is not None and current.get("active_revision_id") == revision_id:
            self._set_revision_status(revision_id, "active")

    def _collection_payload(self, payload: dict[str, Any]) -> KnowledgeCollection:
        return KnowledgeCollection(
            id=payload["record_id"],
            name=payload["name"],
            embedding_model=payload["embedding_model"],
            embedding_dim=payload["embedding_dim"],
            created_at=payload["created_at"],
            document_count=self._count_documents(payload["record_id"]),
            tenant_id=payload.get("tenant_id", "default"),
            created_by_user_id=_payload_user_id(
                payload.get("created_by_user_id")
            ),
            active_generation_id=payload.get("active_generation_id"),
        )

    def _document_payload(self, payload: dict[str, Any]) -> KnowledgeDocument:
        return KnowledgeDocument(
            id=payload["record_id"],
            collection_id=payload["collection_id"],
            title=payload["title"],
            text=payload.get("text", ""),
            metadata=dict(payload.get("metadata", {})),
            chunk_count=payload.get("chunk_count", 0),
            created_at=payload.get("created_at", 0.0),
            source_id=payload.get("source_id"),
            desired_revision_id=payload.get("desired_revision_id"),
            active_revision_id=payload.get("active_revision_id"),
            desired_sequence=int(payload.get("desired_sequence", 0)),
            lifecycle_status=payload.get("lifecycle_status", "active"),
        )

    def _count_documents(self, collection_id: str) -> int:
        _client, models = _require_qdrant()
        result = self._client.count(
            collection_name=REGISTRY_COLLECTION,
            count_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="kind", match=models.MatchValue(value="document")
                    ),
                    models.FieldCondition(
                        key="collection_id",
                        match=models.MatchValue(value=collection_id),
                    ),
                ]
            ),
            exact=True,
        )
        return int(result.count)

    # -- sync cores ------------------------------------------------------- #

    def _sync_create_collection(
        self,
        name,
        embedding_model,
        embedding_dim,
        created_by_user_id: uuid.UUID | None,
    ) -> KnowledgeCollection:
        _client, models = _require_qdrant()
        collection_id = f"kc_{uuid.uuid4().hex[:20]}"
        _ensure_chunks_collection(
            self._client,
            models,
            name=_model_slug(embedding_model),
            embedding_dim=embedding_dim,
            sparse_enabled=self._sparse_enabled,
        )
        self._registry_upsert(
            {
                "kind": "collection",
                "record_id": collection_id,
                "name": name,
                "embedding_model": embedding_model,
                "embedding_dim": embedding_dim,
                "created_at": time.time(),
                "tenant_id": "default",
                "created_by_user_id": created_by_user_id,
                "active_generation_id": f"gen_{uuid.uuid4().hex[:20]}",
            }
        )
        return self._sync_get_collection(collection_id)

    def _sync_list_collections(self) -> list[KnowledgeCollection]:
        _client, models = _require_qdrant()
        self._ensure_registry()
        collections: list[KnowledgeCollection] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=REGISTRY_COLLECTION,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind", match=models.MatchValue(value="collection")
                        )
                    ]
                ),
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            collections.extend(
                self._collection_payload(dict(point.payload)) for point in points
            )
            if offset is None:
                break
        return sorted(collections, key=lambda item: item.created_at, reverse=True)

    def _sync_get_collection(self, collection_id: str) -> KnowledgeCollection:
        payload = self._registry_get("collection", collection_id)
        if payload is None:
            raise CollectionNotFound(collection_id)
        return self._collection_payload(payload)

    def _sync_delete_collection(self, collection_id: str) -> None:
        _client, models = _require_qdrant()
        collection = self._sync_get_collection(collection_id)
        chunks_name = _model_slug(collection.embedding_model)
        if self._client.collection_exists(chunks_name):
            self._client.delete(
                collection_name=chunks_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="collection_id",
                                match=models.MatchValue(value=collection_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        self._client.delete(
            collection_name=REGISTRY_COLLECTION,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind", match=models.MatchValue(value="document")
                        ),
                        models.FieldCondition(
                            key="collection_id",
                            match=models.MatchValue(value=collection_id),
                        ),
                    ]
                )
            ),
            wait=True,
        )
        self._client.delete(
            collection_name=REGISTRY_COLLECTION,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind",
                            match=models.MatchValue(value="document_revision"),
                        ),
                        models.FieldCondition(
                            key="collection_id",
                            match=models.MatchValue(value=collection_id),
                        ),
                    ]
                )
            ),
            wait=True,
        )
        self._registry_delete(collection_id)

    def _sync_count_collection_residuals(
        self,
        collection_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        _client, models = _require_qdrant()
        self._ensure_registry()
        def registry_count(kind: str) -> int:
            return int(
                self._client.count(
                    collection_name=REGISTRY_COLLECTION,
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="kind", match=models.MatchValue(value=kind)
                            ),
                            models.FieldCondition(
                                key="collection_id",
                                match=models.MatchValue(value=collection_id),
                            ),
                        ]
                    ),
                    exact=True,
                ).count
            )
        vector_count = 0
        chunks_name = _model_slug(embedding_model)
        if self._client.collection_exists(chunks_name):
            vector_count = int(
                self._client.count(
                    collection_name=chunks_name,
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="collection_id",
                                match=models.MatchValue(value=collection_id),
                            )
                        ]
                    ),
                    exact=True,
                ).count
            )
        return {
            "collections": int(
                self._registry_get("collection", collection_id) is not None
            ),
            "documents": registry_count("document"),
            "revisions": registry_count("document_revision"),
            "vectors": vector_count,
        }

    def _validate(self, chunks, embeddings, embedding_dim) -> None:
        if len(chunks) != len(embeddings):
            raise EmbeddingDimensionMismatch(
                f"chunk/embedding count mismatch: {len(chunks)} chunks, "
                f"{len(embeddings)} embeddings"
            )
        for vector in embeddings:
            if len(vector) != embedding_dim:
                raise EmbeddingDimensionMismatch(
                    f"embedding dimension {len(vector)} does not match "
                    f"collection dimension {embedding_dim}"
                )

    def _upsert_chunk_points(
        self,
        collection,
        document_id,
        title,
        chunks,
        embeddings,
        source_chunks,
        retrieval_contexts=None,
        source_spans=None,
        document_content_hash=None,
        revision_id=None,
        generation_id=None,
        page_numbers=None,
    ) -> None:
        _client, models = _require_qdrant()
        chunks_name = _model_slug(collection.embedding_model)
        sparse_vectors = (
            self._bm25.documents(chunks) if self._sparse_enabled else None
        )
        points = []
        for index, (chunk_text, dense) in enumerate(zip(chunks, embeddings)):
            vector: dict[str, Any] = {"dense": dense}
            if sparse_vectors is not None:
                vector["sparse"] = sparse_vectors[index]
            points.append(
                models.PointStruct(
                    id=(
                        _point_uuid(
                            "knowledge-chunk:"
                            f"{document_id}:{revision_id}:{index}"
                        )
                        if revision_id
                        else str(uuid.uuid4())
                    ),
                    vector=vector,
                    payload={
                        "collection_id": collection.id,
                        "document_id": document_id,
                        "document_title": title,
                        "chunk_index": index,
                        "text": chunk_text,
                        "source_text": (
                            source_chunks[index]
                            if source_chunks and index < len(source_chunks)
                            else ""
                        ),
                        "retrieval_context": (
                            retrieval_contexts[index]
                            if retrieval_contexts and index < len(retrieval_contexts)
                            else None
                        ),
                        "source_start": (
                            source_spans[index][0]
                            if source_spans and index < len(source_spans)
                            else None
                        ),
                        "source_end": (
                            source_spans[index][1]
                            if source_spans and index < len(source_spans)
                            else None
                        ),
                        "document_content_hash": document_content_hash,
                        "revision_id": revision_id,
                        "generation_id": generation_id,
                        "page_number": (
                            page_numbers[index]
                            if page_numbers and index < len(page_numbers)
                            else None
                        ),
                    },
                )
            )
        if points:
            self._client.upsert(
                collection_name=chunks_name,
                points=points,
                wait=True,
                ordering=models.WriteOrdering.STRONG,
            )

    def _delete_revision_chunks(
        self,
        *,
        collection: KnowledgeCollection,
        document_id: str,
        revision_id: str,
    ) -> None:
        """Remove only one unpublished revision's staged vector points."""
        _client, models = _require_qdrant()
        chunks_name = _model_slug(collection.embedding_model)
        if not self._client.collection_exists(chunks_name):
            return
        self._client.delete(
            collection_name=chunks_name,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=document_id),
                        ),
                        models.FieldCondition(
                            key="revision_id",
                            match=models.MatchValue(value=revision_id),
                        ),
                    ]
                )
            ),
            wait=True,
            ordering=models.WriteOrdering.STRONG,
        )

    def _finalize_published_revision(
        self,
        *,
        collection: KnowledgeCollection,
        document_id: str,
        active_revision_id: str,
        previous_revision_id: str | None,
    ) -> None:
        """Repair status projections and remove only the confirmed predecessor."""
        if (
            previous_revision_id is not None
            and previous_revision_id != active_revision_id
        ):
            self._project_revision_superseded(
                document_id, previous_revision_id
            )
        self._project_revision_active(document_id, active_revision_id)
        current = self._registry_get("document", document_id)
        if (
            previous_revision_id is not None
            and previous_revision_id != active_revision_id
            and (
                current is None
                or current.get("active_revision_id") != previous_revision_id
            )
        ):
            self._delete_revision_chunks(
                collection=collection,
                document_id=document_id,
                revision_id=previous_revision_id,
            )

    def _sync_add_document(
        self, collection_id, title, text, metadata, chunks, embeddings, source_id,
        source_chunks,
        retrieval_contexts=None, source_spans=None, document_content_hash=None,
        revision_id=None, generation_id=None, page_numbers=None,
    ) -> KnowledgeDocument:
        collection = self._sync_get_collection(collection_id)
        self._validate(chunks, embeddings, collection.embedding_dim)
        document_id = f"kd_{uuid.uuid4().hex[:20]}"
        self._upsert_chunk_points(
            collection, document_id, title, chunks, embeddings, source_chunks,
            retrieval_contexts, source_spans, document_content_hash,
            revision_id, generation_id or collection.active_generation_id, page_numbers,
        )
        self._registry_upsert(
            {
                "kind": "document",
                "record_id": document_id,
                "collection_id": collection_id,
                "title": title,
                "text": text,
                "metadata": dict(metadata),
                "source_id": source_id,
                "lifecycle_status": "active",
                "desired_revision_id": revision_id,
                "active_revision_id": revision_id,
                "desired_sequence": 1 if revision_id else 0,
                "chunk_count": len(chunks),
                "created_at": time.time(),
            }
        )
        return self._sync_get_document(document_id)

    def _sync_reserve_document_revision(
        self,
        collection_id: str,
        source_id: str,
        revision_id: str,
        content_hash: str,
        build_contract_hash: str = "",
        title: str = "",
        text: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> DocumentRevisionReservation:
        revision_created_at = time.time()
        new_document_created_at = revision_created_at
        document_id = _source_document_id(collection_id, source_id)
        with self._revision_lock:
            self._sync_get_collection(collection_id)
            if self._registry_get("source_tombstone", source_id) is not None:
                raise SourceDeletionConflict(source_id)
            revision_payload = self._registry_revision_for_build(
                collection_id=collection_id,
                source_id=source_id,
                content_hash=content_hash,
                build_contract_hash=build_contract_hash,
            )
            if revision_payload is None:
                revision_record_id = _revision_record_id(
                    collection_id,
                    source_id,
                    content_hash,
                    build_contract_hash,
                )
                self._registry_insert_only(
                    {
                        "kind": "document_revision",
                        "record_id": revision_record_id,
                        "revision_id": revision_id,
                        "document_id": document_id,
                        "collection_id": collection_id,
                        "source_id": source_id,
                        "content_hash": content_hash,
                        "build_contract_hash": build_contract_hash,
                        "title": title,
                        "text": text,
                        "metadata": _immutable_revision_metadata(
                            dict(metadata or {})
                        ),
                        "status": "staging",
                        "created_at": revision_created_at,
                    }
                )
                revision_payload = self._registry_get(
                    "document_revision", revision_record_id
                )
            if (
                revision_payload is None
                or revision_payload.get("document_id") != document_id
                or revision_payload.get("collection_id") != collection_id
                or revision_payload.get("source_id") != source_id
                or revision_payload.get("content_hash") != content_hash
                or revision_payload.get("build_contract_hash")
                != build_contract_hash
            ):
                raise KnowledgeError(
                    "Qdrant revision build identity did not converge to its "
                    "canonical immutable payload"
                )
            effective_revision_id = str(revision_payload["revision_id"])
            for _attempt in range(_REGISTRY_CAS_MAX_ATTEMPTS):
                if self._registry_get("source_tombstone", source_id) is not None:
                    raise SourceDeletionConflict(source_id)
                matches = self._registry_documents_for_source(
                    collection_id, source_id
                )
                if len(matches) > 1:
                    raise KnowledgeError(
                        "Qdrant registry contains multiple live document "
                        f"identities for source {source_id!r} in collection "
                        f"{collection_id!r}"
                    )
                expected = matches[0] if matches else None
                if expected is None:
                    occupied = self._registry_get("document", document_id)
                    if occupied is not None:
                        occupied_document = self._document_payload(occupied)
                        if not self._document_matches_source(
                            occupied_document, source_id
                        ):
                            raise KnowledgeError(
                                "deterministic Qdrant document identity "
                                f"{document_id!r} is occupied by another source"
                            )
                        expected = occupied
                if expected is None:
                    sequence = 1
                    document_payload = {
                        "kind": "document",
                        "record_id": document_id,
                        "collection_id": collection_id,
                        "title": "",
                        "text": "",
                        "metadata": {"source_id": source_id},
                        "source_id": source_id,
                        "lifecycle_status": "staging",
                        "desired_revision_id": effective_revision_id,
                        "active_revision_id": None,
                        "desired_sequence": sequence,
                        "chunk_count": 0,
                        "created_at": new_document_created_at,
                    }
                else:
                    existing = self._document_payload(expected)
                    if existing.lifecycle_status not in {"active", "staging"}:
                        raise SourceDeletionConflict(source_id)
                    document_id = existing.id
                    if existing.desired_revision_id == effective_revision_id:
                        sequence = existing.desired_sequence
                    else:
                        sequence = existing.desired_sequence + 1
                    document_payload = {
                        "kind": "document",
                        "record_id": document_id,
                        "collection_id": collection_id,
                        "title": existing.title,
                        "text": existing.text,
                        "metadata": dict(existing.metadata),
                        "source_id": source_id,
                        "lifecycle_status": (
                            "active"
                            if existing.active_revision_id is not None
                            else "staging"
                        ),
                        "desired_revision_id": effective_revision_id,
                        "active_revision_id": existing.active_revision_id,
                        "desired_sequence": sequence,
                        "chunk_count": existing.chunk_count,
                        "created_at": existing.created_at,
                    }
                if expected is None:
                    self._registry_insert_only(document_payload)
                elif (
                    expected.get("desired_revision_id") != effective_revision_id
                    or int(expected.get("desired_sequence") or 0) != sequence
                    or expected.get("source_id") != source_id
                ):
                    self._registry_compare_and_swap(
                        document_payload,
                        expected=expected,
                        fields=(
                            "kind",
                            "record_id",
                            "collection_id",
                            "lifecycle_status",
                            "desired_revision_id",
                            "desired_sequence",
                        ),
                    )
                current = self._registry_get("document", document_id)
                if (
                    current is not None
                    and current.get("collection_id") == collection_id
                    and current.get("source_id") == source_id
                    and current.get("desired_revision_id")
                    == effective_revision_id
                    and int(current.get("desired_sequence") or 0) == sequence
                ):
                    if (
                        self._registry_get("source_tombstone", source_id)
                        is not None
                    ):
                        self._mark_document_deleting_after_tombstone(current)
                        raise SourceDeletionConflict(source_id)
                    return DocumentRevisionReservation(
                        document_id=document_id,
                        collection_id=collection_id,
                        source_id=source_id,
                        revision_id=effective_revision_id,
                        sequence=sequence,
                        content_hash=content_hash,
                        build_contract_hash=build_contract_hash,
                        already_published=(
                            current.get("active_revision_id")
                            == effective_revision_id
                        ),
                    )
            raise KnowledgeError(
                "Qdrant document reservation did not converge after "
                f"{_REGISTRY_CAS_MAX_ATTEMPTS} conditional updates"
            )

    def _sync_load_reserved_document_revision(
        self, document_id: str, revision_id: str
    ) -> ReservedDocumentRevision:
        with self._revision_lock:
            document = self._sync_get_document(document_id)
            payload = self._registry_revision_by_id(revision_id)
            if (
                payload is None
                or payload.get("document_id") != document_id
                or document.desired_revision_id != revision_id
            ):
                raise DocumentRevisionSuperseded(revision_id)
            source_id = str(payload.get("source_id") or document.source_id or "")
            if not source_id:
                raise DocumentRevisionSuperseded(revision_id)
            if self._registry_get("source_tombstone", source_id) is not None:
                raise SourceDeletionConflict(source_id)
            revision_status = (
                "active"
                if document.active_revision_id == revision_id
                else str(payload.get("status") or "staging")
            )
            if revision_status == "active" and payload.get("status") != "active":
                self._project_revision_active(document_id, revision_id)
            revision = KnowledgeDocumentRevision(
                revision_id=revision_id,
                document_id=document_id,
                collection_id=str(payload["collection_id"]),
                source_id=source_id,
                content_hash=str(payload["content_hash"]),
                build_contract_hash=str(payload.get("build_contract_hash") or ""),
                title=str(payload.get("title") or ""),
                text=str(payload.get("text") or ""),
                metadata=dict(payload.get("metadata") or {}),
                status=revision_status,
                created_at=float(payload.get("created_at") or 0.0),
            )
            return ReservedDocumentRevision(
                revision=revision,
                reservation=DocumentRevisionReservation(
                    document_id=document_id,
                    collection_id=document.collection_id,
                    source_id=source_id,
                    revision_id=revision_id,
                    sequence=document.desired_sequence,
                    content_hash=revision.content_hash,
                    build_contract_hash=revision.build_contract_hash,
                    already_published=(
                        document.active_revision_id == revision_id
                        and revision.status == "active"
                    ),
                ),
            )

    def _sync_publish_document_revision(
        self,
        reservation: DocumentRevisionReservation,
        title: str,
        text: str,
        metadata: dict[str, Any],
        chunks: list[str],
        embeddings: list[list[float]],
        source_chunks: list[str],
        retrieval_contexts: list[str | None],
        source_spans: list[tuple[int, int]],
        page_numbers: list[int | None] | None,
        generation_id: str | None,
        publication_guard: Callable[[], Any] | None,
    ) -> KnowledgeDocument:
        with (
            self._revision_lock,
            publication_guard() if publication_guard is not None else nullcontext(),
        ):
            if (
                self._registry_get("source_tombstone", reservation.source_id)
                is not None
            ):
                raise SourceDeletionConflict(reservation.source_id)
            expected = self._registry_get("document", reservation.document_id)
            if expected is None:
                raise DocumentNotFound(reservation.document_id)
            document = self._document_payload(expected)
            if (
                document.collection_id != reservation.collection_id
                or document.source_id != reservation.source_id
                or document.desired_revision_id != reservation.revision_id
                or document.desired_sequence != reservation.sequence
                or document.lifecycle_status not in {"active", "staging"}
            ):
                raise DocumentRevisionSuperseded(reservation.revision_id)
            revision_payload = self._registry_revision_by_id(
                reservation.revision_id
            )
            if (
                revision_payload is None
                or revision_payload.get("document_id") != document.id
                or revision_payload.get("collection_id")
                != reservation.collection_id
                or revision_payload.get("source_id") != reservation.source_id
                or revision_payload.get("content_hash")
                != reservation.content_hash
                or revision_payload.get("build_contract_hash")
                != reservation.build_contract_hash
                or revision_payload.get("title") != title
                or revision_payload.get("text") != text
                or dict(revision_payload.get("metadata") or {})
                != _immutable_revision_metadata(dict(metadata))
            ):
                raise KnowledgeError("immutable document revision payload changed")
            collection = self._sync_get_collection(reservation.collection_id)
            if document.active_revision_id == reservation.revision_id:
                self._finalize_published_revision(
                    collection=collection,
                    document_id=document.id,
                    active_revision_id=reservation.revision_id,
                    previous_revision_id=expected.get(
                        "previous_active_revision_id"
                    ),
                )
                return document
            self._validate(chunks, embeddings, collection.embedding_dim)
            self._upsert_chunk_points(
                collection,
                document.id,
                title,
                chunks,
                embeddings,
                source_chunks,
                retrieval_contexts,
                source_spans,
                reservation.content_hash,
                reservation.revision_id,
                generation_id or collection.active_generation_id,
                page_numbers,
            )
            if (
                self._registry_get("source_tombstone", reservation.source_id)
                is not None
            ):
                self._delete_revision_chunks(
                    collection=collection,
                    document_id=document.id,
                    revision_id=reservation.revision_id,
                )
                current = self._registry_get("document", document.id)
                if current is not None:
                    self._mark_document_deleting_after_tombstone(current)
                raise SourceDeletionConflict(reservation.source_id)
            published_payload = {
                "kind": "document",
                "record_id": document.id,
                "collection_id": collection.id,
                "title": title,
                "text": text,
                "metadata": dict(metadata),
                "source_id": reservation.source_id,
                "lifecycle_status": "active",
                "desired_revision_id": reservation.revision_id,
                "active_revision_id": reservation.revision_id,
                "previous_active_revision_id": document.active_revision_id,
                "desired_sequence": reservation.sequence,
                "chunk_count": len(chunks),
                "created_at": document.created_at,
            }
            self._registry_compare_and_swap(
                published_payload,
                expected=expected,
                fields=(
                    "kind",
                    "record_id",
                    "collection_id",
                    "source_id",
                    "lifecycle_status",
                    "desired_revision_id",
                    "active_revision_id",
                    "desired_sequence",
                ),
            )
            current = self._registry_get("document", document.id)
            tombstoned = (
                self._registry_get("source_tombstone", reservation.source_id)
                is not None
            )
            if tombstoned:
                self._delete_revision_chunks(
                    collection=collection,
                    document_id=document.id,
                    revision_id=reservation.revision_id,
                )
                if current is not None:
                    self._mark_document_deleting_after_tombstone(current)
                raise SourceDeletionConflict(reservation.source_id)
            if (
                current is None
                or current.get("active_revision_id") != reservation.revision_id
            ):
                self._delete_revision_chunks(
                    collection=collection,
                    document_id=document.id,
                    revision_id=reservation.revision_id,
                )
                raise DocumentRevisionSuperseded(reservation.revision_id)
            self._finalize_published_revision(
                collection=collection,
                document_id=document.id,
                active_revision_id=reservation.revision_id,
                previous_revision_id=document.active_revision_id,
            )
            return self._document_payload(current)

    def _sync_list_all_documents(self) -> list[KnowledgeDocument]:
        _client, models = _require_qdrant()
        self._ensure_registry()
        documents: list[KnowledgeDocument] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=REGISTRY_COLLECTION,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind",
                            match=models.MatchValue(value="document"),
                        )
                    ]
                ),
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                consistency=models.ReadConsistencyType.ALL,
            )
            for point in points:
                document = self._document_payload(dict(point.payload or {}))
                if document.lifecycle_status != "deleted":
                    documents.append(document)
            if offset is None:
                return documents

    @staticmethod
    def _document_matches_source(
        document: KnowledgeDocument, source_id: str
    ) -> bool:
        if document.source_id == source_id:
            return True
        legacy = document.metadata.get("fileId") or document.metadata.get("file_id")
        return isinstance(legacy, str) and source_id in {legacy, f"asset:{legacy}"}

    def _sync_mark_source_deleting(self, source_id: str) -> int:
        with self._revision_lock:
            self._registry_upsert(
                {
                    "kind": "source_tombstone",
                    "record_id": source_id,
                    "source_id": source_id,
                    "created_at": time.time(),
                }
            )
            documents = [
                document
                for document in self._sync_list_all_documents()
                if document.lifecycle_status not in {"deleted", "deleting"}
                and self._document_matches_source(document, source_id)
            ]
        for document in documents:
            # Immediate search detach: remove vector points first, then persist
            # the tombstone while retaining the registry identity for retry.
            _client, models = _require_qdrant()
            collection = self._sync_get_collection(document.collection_id)
            chunks_name = _model_slug(collection.embedding_model)
            if self._client.collection_exists(chunks_name):
                self._client.delete(
                    collection_name=chunks_name,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="document_id",
                                    match=models.MatchValue(value=document.id),
                                )
                            ]
                        )
                    ),
                    wait=True,
                )
            self._registry_upsert(
                {
                    "kind": "document",
                    "record_id": document.id,
                    "collection_id": document.collection_id,
                    "title": document.title,
                    "text": document.text,
                    "metadata": dict(document.metadata),
                    "source_id": document.source_id,
                    "lifecycle_status": "deleting",
                    "chunk_count": document.chunk_count,
                    "created_at": document.created_at,
                }
            )
        return len(documents)

    def _sync_list_documents(self, collection_id: str) -> list[KnowledgeDocument]:
        _client, models = _require_qdrant()
        self._sync_get_collection(collection_id)
        documents: list[KnowledgeDocument] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=REGISTRY_COLLECTION,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind", match=models.MatchValue(value="document")
                        ),
                        models.FieldCondition(
                            key="collection_id",
                            match=models.MatchValue(value=collection_id),
                        ),
                    ]
                ),
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                document = self._document_payload(dict(point.payload))
                if document.lifecycle_status == "active":
                    documents.append(document)
            if offset is None:
                break
        return sorted(documents, key=lambda item: item.created_at, reverse=True)

    def _sync_get_document(self, document_id: str) -> KnowledgeDocument:
        payload = self._registry_get("document", document_id)
        if payload is None:
            raise DocumentNotFound(document_id)
        return self._document_payload(payload)

    def _sync_get_chunks(self, document_id: str) -> list[DocumentChunk]:
        """Hydrate only the canonical active revision/generation projection."""
        _client, models = _require_qdrant()
        document = self._sync_get_document(document_id)
        collection = self._sync_get_collection(document.collection_id)
        chunks_name = _model_slug(collection.embedding_model)
        if not self._client.collection_exists(chunks_name):
            return []
        conditions = [
            models.FieldCondition(
                key="collection_id",
                match=models.MatchValue(value=document.collection_id),
            ),
            models.FieldCondition(
                key="document_id",
                match=models.MatchValue(value=document_id),
            ),
            self._registry_value_condition(
                models, "revision_id", document.active_revision_id
            ),
            self._registry_value_condition(
                models, "generation_id", collection.active_generation_id
            ),
        ]
        points = []
        offset = None
        while True:
            page, offset = self._client.scroll(
                collection_name=chunks_name,
                scroll_filter=models.Filter(must=conditions),
                limit=128,
                offset=offset,
                with_payload=True,
                with_vectors=False,
                consistency=models.ReadConsistencyType.ALL,
            )
            points.extend(page)
            if offset is None:
                break
        chunks = []
        for point in points:
            payload = dict(point.payload or {})
            source_text = str(payload.get("source_text") or "")
            source_start = _optional_int(payload.get("source_start"))
            source_end = _optional_int(payload.get("source_end"))
            content_hash = payload.get("document_content_hash")
            chunks.append(
                DocumentChunk(
                    id=str(point.id),
                    document_id=document_id,
                    collection_id=document.collection_id,
                    chunk_index=int(payload.get("chunk_index", 0)),
                    text=str(payload.get("text") or ""),
                    source_text=source_text,
                    retrieval_context=payload.get("retrieval_context"),
                    source_start=source_start,
                    source_end=source_end,
                    document_content_hash=content_hash,
                    revision_id=payload.get("revision_id"),
                    generation_id=payload.get("generation_id"),
                    page_number=_optional_int(payload.get("page_number")),
                    source_verified=source_excerpt_is_verified(
                        canonical_text=document.text,
                        source_text=source_text,
                        source_start=source_start,
                        source_end=source_end,
                        document_content_hash=content_hash,
                    ),
                )
            )
        return sorted(chunks, key=lambda chunk: chunk.chunk_index)

    def _sync_set_document_lifecycle(
        self,
        document_id: str,
        lifecycle_status: str,
        only_if_deleting: bool = False,
    ) -> None:
        payload = self._registry_get("document", document_id)
        if payload is None:
            if only_if_deleting:
                return
            raise DocumentNotFound(document_id)
        if only_if_deleting and payload.get("lifecycle_status", "active") != "deleting":
            return
        payload["lifecycle_status"] = lifecycle_status
        self._registry_upsert(payload)

    def _sync_count_document_residuals(
        self,
        document_id: str,
        embedding_model: str,
    ) -> dict[str, int]:
        _client, models = _require_qdrant()
        vector_count = 0
        chunks_name = _model_slug(embedding_model)
        if self._client.collection_exists(chunks_name):
            vector_count = int(
                self._client.count(
                    collection_name=chunks_name,
                    count_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id",
                                match=models.MatchValue(value=document_id),
                            )
                        ]
                    ),
                    exact=True,
                ).count
            )
        revision_count = int(
            self._client.count(
                collection_name=REGISTRY_COLLECTION,
                count_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind",
                            match=models.MatchValue(value="document_revision"),
                        ),
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=document_id),
                        ),
                    ]
                ),
                exact=True,
            ).count
        )
        return {
            "documents": int(
                self._registry_get("document", document_id) is not None
            ),
            "revisions": revision_count,
            "vectors": vector_count,
        }

    def _sync_delete_document(self, document_id: str) -> None:
        _client, models = _require_qdrant()
        document = self._sync_get_document(document_id)
        collection = self._sync_get_collection(document.collection_id)
        chunks_name = _model_slug(collection.embedding_model)
        if self._client.collection_exists(chunks_name):
            self._client.delete(
                collection_name=chunks_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id",
                                match=models.MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        self._client.delete(
            collection_name=REGISTRY_COLLECTION,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="kind",
                            match=models.MatchValue(value="document_revision"),
                        ),
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(value=document_id),
                        ),
                    ]
                )
            ),
            wait=True,
        )
        self._registry_delete(document_id)

    def _sync_reembed_document(
        self, document_id, chunks, embeddings, source_chunks,
        retrieval_contexts=None, source_spans=None, document_content_hash=None,
        revision_id=None, generation_id=None, page_numbers=None
    ) -> KnowledgeDocument:
        _client, models = _require_qdrant()
        document = self._sync_get_document(document_id)
        collection = self._sync_get_collection(document.collection_id)
        self._validate(chunks, embeddings, collection.embedding_dim)
        chunks_name = _model_slug(collection.embedding_model)
        if self._client.collection_exists(chunks_name):
            self._client.delete(
                collection_name=chunks_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="document_id",
                                match=models.MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        self._upsert_chunk_points(
            collection, document_id, document.title, chunks, embeddings, source_chunks,
            retrieval_contexts, source_spans, document_content_hash,
            revision_id, generation_id, page_numbers,
        )
        self._registry_upsert(
            {
                "kind": "document",
                "record_id": document_id,
                "collection_id": document.collection_id,
                "title": document.title,
                "text": document.text,
                "metadata": dict(document.metadata),
                "source_id": document.source_id,
                "lifecycle_status": document.lifecycle_status,
                "desired_revision_id": revision_id or document.desired_revision_id,
                "active_revision_id": revision_id or document.active_revision_id,
                "desired_sequence": document.desired_sequence,
                "chunk_count": len(chunks),
                "created_at": document.created_at,
            }
        )
        return self._sync_get_document(document_id)

    def _resolve_target(self, collection_ids, embedding_model):
        explicit = collection_ids is not None
        if explicit and not collection_ids:
            # ``None`` means the intentionally unscoped search-all mode;
            # ``[]`` is an explicit empty authorization scope and must never
            # expand to every collection.
            return _model_slug(embedding_model or ""), None
        _client, models = _require_qdrant()
        if explicit:
            collections = [self._sync_get_collection(cid) for cid in collection_ids]
        else:
            collections = self._sync_list_collections()
            if not collections:
                raise KnowledgeError("no knowledge collections exist")
        models_in_scope = {c.embedding_model for c in collections}
        active_model = embedding_model or collections[0].embedding_model
        if explicit:
            # Canonical contract (Postgres + memory parity): an explicit
            # multi-model selection is a HARD error, never a silently narrowed
            # result set. The narrowing below belongs only to
            # the implicit search-all path, where per-model narrowing is
            # required because the vector index is per embedding model.
            if len(models_in_scope) > 1:
                raise KnowledgeError(
                    "scoped collections use different embedding models "
                    f"({sorted(models_in_scope)}); query one model scope at a time"
                )
        elif embedding_model is not None and models_in_scope - {embedding_model}:
            scoped = [c for c in collections if c.embedding_model == embedding_model]
            if not scoped:
                raise KnowledgeError(
                    f"no scoped collection uses embedding model {embedding_model!r}"
                )
            collections = scoped
        return _model_slug(active_model), _scope_filter(
            models, [c.id for c in collections]
        )

    def _candidates_from_points(self, points) -> list[RetrievalCandidate]:
        candidates = []
        for point in points:
            payload = dict(point.payload or {})
            document_id = str(payload.get("document_id", ""))
            document = self._registry_get("document", document_id)
            if document is None or document.get("lifecycle_status", "active") != "active":
                continue
            collection_id = str(payload.get("collection_id", ""))
            collection = self._registry_get("collection", collection_id)
            if collection is None:
                continue
            active_generation = collection.get("active_generation_id")
            point_generation = payload.get("generation_id")
            if (
                (active_generation is None and point_generation is not None)
                or (
                    active_generation is not None
                    and point_generation != active_generation
                )
            ):
                continue
            active_revision = document.get("active_revision_id")
            point_revision = payload.get("revision_id")
            if (
                (active_revision is None and point_revision is not None)
                or (
                    active_revision is not None
                    and point_revision != active_revision
                )
            ):
                continue
            source_text = payload.get("source_text", "")
            source_start = _optional_int(payload.get("source_start"))
            source_end = _optional_int(payload.get("source_end"))
            source_verified = source_excerpt_is_verified(
                canonical_text=str(document.get("text", "")),
                source_text=source_text,
                source_start=source_start,
                source_end=source_end,
                document_content_hash=payload.get("document_content_hash"),
            )
            if not source_verified:
                continue
            candidates.append(
                RetrievalCandidate(
                    chunk=DocumentChunk(
                        id=str(point.id),
                        document_id=document_id,
                        collection_id=collection_id,
                        chunk_index=int(payload.get("chunk_index", 0)),
                        text=payload.get("text", ""),
                        source_text=source_text,
                        retrieval_context=payload.get("retrieval_context"),
                        source_start=source_start,
                        source_end=source_end,
                        document_content_hash=payload.get("document_content_hash"),
                        revision_id=payload.get("revision_id"),
                        generation_id=payload.get("generation_id"),
                        page_number=_optional_int(payload.get("page_number")),
                        source_verified=True,
                    ),
                    score=float(point.score or 0.0),
                    document_title=document.get(
                        "title", payload.get("document_title", "")
                    ),
                )
            )
        return candidates

    def _sync_search(
        self, query_embedding, collection_ids, top_k, embedding_model
    ) -> RetrievalCandidateBatch:
        if top_k <= 0 or collection_ids == []:
            return RetrievalCandidateBatch()
        chunks_name, scope_filter = self._resolve_target(collection_ids, embedding_model)
        if scope_filter is None:
            return RetrievalCandidateBatch()

        def fetch_points(depth: int):
            return self._client.query_points(
                collection_name=chunks_name,
                query=query_embedding,
                using="dense",
                query_filter=scope_filter,
                limit=depth,
                with_payload=True,
            ).points

        return self._bounded_candidate_search(
            top_k=top_k,
            retrieval_mode="dense",
            fetch_points=fetch_points,
        )

    def _sync_hybrid_search(
        self, query_text, query_embedding, collection_ids, top_k, embedding_model
    ) -> RetrievalCandidateBatch:
        if top_k <= 0 or collection_ids == []:
            return RetrievalCandidateBatch()
        _client, models = _require_qdrant()
        chunks_name, scope_filter = self._resolve_target(collection_ids, embedding_model)
        if scope_filter is None:
            return RetrievalCandidateBatch()
        sparse = self._bm25.query(query_text)

        def fetch_points(depth: int):
            return self._client.query_points(
                collection_name=chunks_name,
                prefetch=[
                    models.Prefetch(
                        query=query_embedding,
                        using="dense",
                        filter=scope_filter,
                        limit=depth,
                    ),
                    models.Prefetch(
                        query=sparse,
                        using="sparse",
                        filter=scope_filter,
                        limit=depth,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=depth,
                with_payload=True,
            ).points

        return self._bounded_candidate_search(
            top_k=top_k,
            retrieval_mode="hybrid",
            fetch_points=fetch_points,
        )

    def _bounded_candidate_search(
        self,
        *,
        top_k: int,
        retrieval_mode: str,
        fetch_points,
    ) -> RetrievalCandidateBatch:
        """Hydrate a geometrically widened but strictly bounded Qdrant pool."""

        if top_k <= 0:
            return RetrievalCandidateBatch()
        configured_cap = getattr(
            self,
            "_vector_candidate_cap",
            MAX_VECTOR_CANDIDATES,
        )
        max_depth = bounded_candidate_depth(
            top_k,
            configured_cap=configured_cap,
        )
        depth = min(max(1, top_k), max_depth)
        previous_ids: tuple[str, ...] = ()
        while True:
            points = list(fetch_points(depth))
            candidates = self._candidates_from_points(points)
            if len(candidates) >= top_k:
                return RetrievalCandidateBatch(candidates[:top_k])
            current_ids = tuple(str(point.id) for point in points)
            if previous_ids and current_ids == previous_ids:
                self._log_candidate_degradation(
                    reason="vector_candidate_stalled",
                    retrieval_mode=retrieval_mode,
                    requested_candidate_pool=top_k,
                    returned_candidate_pool=len(candidates),
                    candidate_cap=len(points),
                )
                return degraded_candidates(
                    candidates,
                    reason="vector_candidate_stalled",
                    retrieval_mode=retrieval_mode,
                    requested_candidate_pool=top_k,
                    candidate_cap=len(points),
                )
            if len(points) < depth:
                # The first short page is genuine corpus exhaustion. A repeated
                # short page was handled above as a backend stall.
                return RetrievalCandidateBatch(candidates[:top_k])
            if depth >= max_depth:
                self._log_candidate_degradation(
                    reason="vector_overfetch_cap",
                    retrieval_mode=retrieval_mode,
                    requested_candidate_pool=top_k,
                    returned_candidate_pool=len(candidates),
                    candidate_cap=max_depth,
                )
                return degraded_candidates(
                    candidates,
                    reason="vector_overfetch_cap",
                    retrieval_mode=retrieval_mode,
                    requested_candidate_pool=top_k,
                    candidate_cap=max_depth,
                )
            previous_ids = current_ids
            depth = min(depth * 2, max_depth)

    @staticmethod
    def _log_candidate_degradation(
        *,
        reason: str,
        retrieval_mode: str,
        requested_candidate_pool: int,
        returned_candidate_pool: int,
        candidate_cap: int,
    ) -> None:
        log.warning(
            "qdrant-only knowledge search degraded: reason=%s mode=%s "
            "candidate_pool=%d/%d candidate_cap=%d",
            reason,
            retrieval_mode,
            returned_candidate_pool,
            requested_candidate_pool,
            candidate_cap,
            extra={
                "event": "knowledge.retrieval.degraded",
                "stage": "vector_candidate_pool",
                "retrieval_mode": retrieval_mode,
                "degradation_reason": reason,
                "candidate_cap": candidate_cap,
                "requested_candidate_pool": requested_candidate_pool,
                "returned_candidate_pool": returned_candidate_pool,
                # Direct store callers use candidate-pool k as final k. The
                # shared retrieval pipeline replaces these final counters when
                # a reranker requested a deeper intermediate pool.
                "final_top_k": requested_candidate_pool,
                "requested_top_k": requested_candidate_pool,
                "active_verified_hits": returned_candidate_pool,
            },
        )
