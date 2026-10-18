"""Tests for the knowledge engine: chunking, memory store, cards, service."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.embedding_cards import (
    EMBEDDING_CARDS,
    build_embedding_catalog,
    resolve_embedding_card,
)
from inqtrix.knowledge.chunk_identity import deterministic_chunk_id
from inqtrix.knowledge.chunking import chunk_text
from inqtrix.knowledge.source_cleanup import SourceCleanupPlan
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentNotFound,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    GenerationBuildValidation,
    GenerationDocumentValidation,
    GenerationValidationError,
    KnowledgeProviderContext,
    SourceDeletionConflict,
)
from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore
from inqtrix.services.knowledge_service import (
    KnowledgeService,
    KnowledgeValidationError,
    canonical_source_id,
)
from inqtrix.source_authority import SourceScope


class StubEmbeddings:
    """Deterministic word-bucket embeddings for network-free tests."""

    DIM = 8

    def __init__(self, *, selectable: list[str] | None = None) -> None:
        self._selectable = selectable or []
        self.document_calls = 0
        self.query_calls = 0

    @property
    def selectable_embedding_models(self) -> list[str]:
        return list(self._selectable)

    @property
    def default_model(self) -> str:
        return "stub-embed-8"

    def _vector(self, text: str) -> list[float]:
        # zlib.crc32 instead of hash(): the builtin is salted per
        # process (PYTHONHASHSEED), which would make test vectors
        # non-deterministic across runs.
        import zlib

        vector = [0.0] * self.DIM
        for word in text.lower().split():
            vector[zlib.crc32(word.encode("utf-8")) % self.DIM] += 1.0
        return vector

    def embed_documents(self, texts, *, model=None):
        self.document_calls += 1
        return [self._vector(text) for text in texts]

    def embed_query(self, text, *, model=None):
        self.query_calls += 1
        return self._vector(text)


def make_knowledge_context(**kwargs) -> KnowledgeProviderContext:
    return KnowledgeProviderContext(
        embeddings=kwargs.pop("embeddings", StubEmbeddings()),
        store=kwargs.pop("store", MemoryKnowledgeStore()),
        **kwargs,
    )


def make_service(context: KnowledgeProviderContext | None = None) -> KnowledgeService:
    identity = MemoryIdentityStore()
    return KnowledgeService(
        knowledge=context or make_knowledge_context(),
        authorization=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )


def test_chunk_identity_is_retry_stable_and_build_scoped() -> None:
    coordinates = {
        "document_id": "kd_1",
        "generation_id": "gen_1",
        "revision_id": "rev_1",
        "content_hash": "abc",
        "chunk_index": 2,
    }
    first = deterministic_chunk_id(**coordinates)
    assert deterministic_chunk_id(**coordinates) == first
    assert deterministic_chunk_id(
        **{**coordinates, "generation_id": "gen_2"}
    ) != first
    assert deterministic_chunk_id(
        **{**coordinates, "revision_id": "rev_2"}
    ) != first
    assert deterministic_chunk_id(
        **{**coordinates, "content_hash": "def"}
    ) != first


@pytest.mark.asyncio
async def test_async_revision_is_rejected_without_cross_worker_cas_authority():
    class ProcessLocalRevisionStore(MemoryKnowledgeStore):
        @property
        def supports_async_document_revisions(self) -> bool:
            return False

    service = make_service(
        make_knowledge_context(store=ProcessLocalRevisionStore())
    )
    collection = await service.create_collection(name="Compatibility")

    with pytest.raises(KnowledgeValidationError, match="CAS authority across workers"):
        await service.reserve_document_revision(
            collection_id=collection.id,
            title="D",
            text="canonical",
        )


def test_qdrant_only_store_excludes_points_from_inactive_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild may stage the same active revision in a new generation."""

    store = object.__new__(QdrantKnowledgeStore)
    canonical = "Aktiver Originaltext"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def registry_get(kind: str, record_id: str):
        if kind == "collection" and record_id == "kc_1":
            return {"active_generation_id": "gen_new"}
        if kind == "document" and record_id == "kd_1":
            return {
                "active_revision_id": "rev_1",
                "lifecycle_status": "active",
                "text": canonical,
                "title": "Dokument",
            }
        return None

    monkeypatch.setattr(store, "_registry_get", registry_get)
    base_payload = {
        "chunk_index": 0,
        "collection_id": "kc_1",
        "document_content_hash": digest,
        "document_id": "kd_1",
        "revision_id": "rev_1",
        "source_end": len(canonical.encode("utf-8")),
        "source_start": 0,
        "source_text": canonical,
        "text": canonical,
    }
    points = [
        SimpleNamespace(
            id="old", payload={**base_payload, "generation_id": "gen_old"}, score=0.99
        ),
        SimpleNamespace(
            id="new", payload={**base_payload, "generation_id": "gen_new"}, score=0.91
        ),
    ]

    candidates = store._candidates_from_points(points)

    assert [candidate.chunk.id for candidate in candidates] == ["new"]
    assert candidates[0].chunk.generation_id == "gen_new"


def register_asset_source(store: MemoryKnowledgeStore, asset_id: str) -> None:
    store.source_lifecycle_authority.register_active(
        SourceScope(
            tenant_id="default",
            source_id=f"asset:{asset_id}",
            owner_user_id=None,
            workspace_id=None,
        )
    )


def test_source_identity_preserves_explicit_and_distinguishes_file_layers():
    assert canonical_source_id({"source_id": "external:contract-7"}) == (
        "external:contract-7"
    )
    assert canonical_source_id({"fileId": "asset-7"}) == "asset:asset-7"
    assert canonical_source_id({"file_id": "fl_7"}) == "file:fl_7"


# ------------------------------------------------------------------ #
# Chunking
# ------------------------------------------------------------------ #


def test_chunker_keeps_short_text_whole():
    assert chunk_text("Ein kurzer Absatz.") == ["Ein kurzer Absatz."]


def test_chunker_packs_paragraphs_up_to_budget():
    text = "Absatz eins.\n\nAbsatz zwei.\n\nAbsatz drei."
    assert chunk_text(text, max_chars=30) == [
        "Absatz eins.\n\nAbsatz zwei.",
        "Absatz drei.",
    ]


def test_chunker_splits_oversize_paragraph_on_sentences():
    text = "Erster Satz hier. Zweiter Satz hier. Dritter Satz hier."
    chunks = chunk_text(text, max_chars=25)
    assert chunks == ["Erster Satz hier.", "Zweiter Satz hier.", "Dritter Satz hier."]


def test_chunker_hard_wraps_single_oversize_sentence():
    chunks = chunk_text("x" * 50, max_chars=20)
    assert chunks == ["x" * 20, "x" * 20, "x" * 10]


def test_chunker_rejects_non_positive_budget():
    with pytest.raises(ValueError, match="positive"):
        chunk_text("text", max_chars=0)


def test_chunker_empty_input_yields_no_chunks():
    assert chunk_text("   \n\n  ") == []


# ------------------------------------------------------------------ #
# Memory store
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_collection_lifecycle_and_document_count():
    store = MemoryKnowledgeStore()
    collection = await store.create_collection(
        name="Vertraege", embedding_model="stub-embed-8", embedding_dim=8
    )
    assert collection.embedding_model == "stub-embed-8"

    await store.add_document(
        collection_id=collection.id,
        title="Rahmenvertrag",
        text="Inhalt",
        metadata={},
        chunks=["Inhalt"],
        embeddings=[[1.0] * 8],
        actor_user_id=None,
    )
    assert (await store.get_collection(collection.id)).document_count == 1

    documents = await store.list_documents(collection.id)
    assert [doc.title for doc in documents] == ["Rahmenvertrag"]

    await store.delete_document(documents[0].id, actor_user_id=None)
    assert (await store.get_collection(collection.id)).document_count == 0

    await store.delete_collection(collection.id, actor_user_id=None)
    with pytest.raises(CollectionNotFound):
        await store.get_collection(collection.id)


@pytest.mark.asyncio
async def test_dimension_mismatch_is_rejected_loudly():
    store = MemoryKnowledgeStore()
    collection = await store.create_collection(
        name="K", embedding_model="stub-embed-8", embedding_dim=8
    )
    with pytest.raises(EmbeddingDimensionMismatch, match="dimension 4"):
        await store.add_document(
            collection_id=collection.id,
            title="Falsch",
            text="x",
            metadata={},
            chunks=["x"],
            embeddings=[[1.0] * 4],
            actor_user_id=None,
        )


@pytest.mark.asyncio
async def test_chunk_embedding_count_mismatch_is_rejected():
    store = MemoryKnowledgeStore()
    collection = await store.create_collection(
        name="K", embedding_model="stub-embed-8", embedding_dim=8
    )
    with pytest.raises(EmbeddingDimensionMismatch, match="count mismatch"):
        await store.add_document(
            collection_id=collection.id,
            title="Falsch",
            text="x",
            metadata={},
            chunks=["a", "b"],
            embeddings=[[1.0] * 8],
            actor_user_id=None,
        )


@pytest.mark.asyncio
async def test_reingesting_a_source_file_replaces_its_document():
    """A source file is present in a collection at most once.

    A client cannot always observe the outcome of its own ingest — a cancelled
    request whose server-side work completes anyway, a retry after a timeout —
    so re-ingesting the same file must replace the previous document instead of
    leaving a second copy that answers every query twice.
    """
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")

    first = await service.add_document(
        collection_id=collection.id,
        title="Vertrag.pdf",
        text="Die Haftung ist begrenzt.",
        metadata={"fileId": "file-a"},
    )
    second = await service.add_document(
        collection_id=collection.id,
        title="Vertrag.pdf",
        text="Die Haftung ist begrenzt.",
        metadata={"fileId": "file-a"},
    )

    documents = await store.list_documents(collection.id)
    assert [document.id for document in documents] == [second.id]
    assert first.id == second.id
    assert second.source_id == "asset:file-a"
    assert second.metadata["source_id"] == "asset:file-a"
    assert first.active_revision_id == second.active_revision_id
    assert second.desired_sequence == 1


@pytest.mark.asyncio
async def test_slower_source_revision_cannot_delete_or_replace_newer_retry(
    monkeypatch,
):
    """Intent order, not provider completion order, controls publication."""
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    real_embed = service._embed_text
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    invocation = 0

    async def ordered_embed(**kwargs):
        nonlocal invocation
        invocation += 1
        if invocation == 1:
            first_started.set()
            await release_first.wait()
        return await real_embed(**kwargs)

    monkeypatch.setattr(service, "_embed_text", ordered_embed)
    first_task = asyncio.create_task(
        service.add_document(
            collection_id=collection.id,
            title="Vertrag.pdf",
            text="Alte Fassung.",
            metadata={"fileId": "file-a"},
        )
    )
    await first_started.wait()
    retry = await service.add_document(
        collection_id=collection.id,
        title="Vertrag.pdf",
        text="Neue Fassung.",
        metadata={"fileId": "file-a"},
    )
    release_first.set()
    with pytest.raises(DocumentRevisionSuperseded):
        await first_task

    surviving = await store.get_document(retry.id)
    assert surviving.id == retry.id
    assert surviving.text == "Neue Fassung."
    assert [doc.id for doc in await store.list_documents(collection.id)] == [retry.id]


@pytest.mark.asyncio
async def test_three_overlapping_source_revisions_publish_only_latest_intent(
    monkeypatch,
):
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    real_embed = service._embed_text
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()
    invocation = 0

    async def ordered_embed(**kwargs):
        nonlocal invocation
        invocation += 1
        if invocation == 1:
            started_a.set()
            await release_a.wait()
        elif invocation == 2:
            started_b.set()
            await release_b.wait()
        return await real_embed(**kwargs)

    monkeypatch.setattr(service, "_embed_text", ordered_embed)

    async def ingest(text: str):
        return await service.add_document(
            collection_id=collection.id,
            title="Vertrag.pdf",
            text=text,
            metadata={"fileId": "file-a"},
        )

    attempt_a = asyncio.create_task(ingest("Fassung A"))
    await started_a.wait()
    attempt_b = asyncio.create_task(ingest("Fassung B"))
    await started_b.wait()
    winner = await ingest("Fassung C")

    release_b.set()
    with pytest.raises(DocumentRevisionSuperseded):
        await attempt_b
    release_a.set()
    with pytest.raises(DocumentRevisionSuperseded):
        await attempt_a

    surviving = await store.get_document(winner.id)
    assert surviving.text == "Fassung C"
    assert surviving.desired_revision_id == winner.active_revision_id
    assert surviving.desired_sequence == 3


@pytest.mark.asyncio
async def test_delayed_ingest_cannot_publish_after_source_deletion(
    monkeypatch,
):
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    real_embed = service._embed_text
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_embed(**kwargs):
        started.set()
        await release.wait()
        return await real_embed(**kwargs)

    monkeypatch.setattr(service, "_embed_text", delayed_embed)
    ingest = asyncio.create_task(
        service.add_document(
            collection_id=collection.id,
            title="Vertrag.pdf",
            text="Darf nicht wiederkehren.",
            metadata={"fileId": "file-a"},
        )
    )
    await started.wait()
    scope = store.source_lifecycle_authority.resolve_scope(
        tenant_id="default", source_id="asset:file-a"
    )
    permit = store.source_lifecycle_authority.begin_delete(
        scope, operation_id="del_delayed_ingest"
    )
    await store.mark_source_deleting(
        "asset:file-a", deletion_permit=permit
    )
    plan = await store.prepare_source_cleanup(
        "asset:file-a", deletion_permit=permit
    )
    await store.execute_source_cleanup(plan, deletion_permit=permit)
    store.source_lifecycle_authority.complete_delete(permit)
    release.set()

    with pytest.raises(SourceDeletionConflict):
        await ingest
    with pytest.raises(SourceDeletionConflict):
        await service.add_document(
            collection_id=collection.id,
            title="Vertrag.pdf",
            text="Auch ein neuer Versuch bleibt blockiert.",
            metadata={"fileId": "file-a"},
        )
    assert await store.list_documents_by_source("asset:file-a") == []


@pytest.mark.asyncio
async def test_legacy_source_lookup_is_scoped_to_authorized_collection():
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    first_collection = await service.create_collection(name="First")
    second_collection = await service.create_collection(name="Second")
    source_id = "document:shared-legacy-source"

    first = await service.add_document(
        collection_id=first_collection.id,
        title="First.pdf",
        text="First collection evidence.",
        metadata={"source_id": source_id},
    )
    second = await service.add_document(
        collection_id=second_collection.id,
        title="Second.pdf",
        text="Second collection evidence.",
        metadata={"source_id": source_id},
    )

    assert await store.list_documents_by_source(
        source_id,
        collection_id=first_collection.id,
    ) == [first]
    assert await store.list_documents_by_source(
        source_id,
        collection_id=second_collection.id,
    ) == [second]
    assert (
        await service.resolve_document_by_source(
            first_collection.id,
            source_id,
        )
    ).id == first.id


@pytest.mark.asyncio
async def test_source_cleanup_plan_survives_worker_resume_and_verifies_ids():
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    await service.add_document(
        collection_id=collection.id,
        title="Vertrag.pdf",
        text="Ein kanonischer Beleg.",
        metadata={"fileId": "file-a"},
    )
    scope = store.source_lifecycle_authority.resolve_scope(
        tenant_id="default", source_id="asset:file-a"
    )
    permit = store.source_lifecycle_authority.begin_delete(
        scope, operation_id="del_resumable_cleanup"
    )
    await store.mark_source_deleting(
        "asset:file-a", deletion_permit=permit
    )
    prepared = await store.prepare_source_cleanup(
        "asset:file-a", deletion_permit=permit
    )
    restored = SourceCleanupPlan.from_dict(prepared.as_dict())

    assert restored == prepared
    assert restored.document_count == 1
    assert restored.chunk_count == restored.point_count > 0
    assert await store.execute_source_cleanup(
        restored, deletion_permit=permit
    ) == 1
    assert await store.verify_source_cleanup(
        restored, deletion_permit=permit
    ) == {"documents": 0, "chunks": 0, "vectors": 0}


@pytest.mark.asyncio
async def test_source_cleanup_isolated_by_owner_and_workspace_scope():
    store = MemoryKnowledgeStore()
    source_id = "asset:shared-source-id"
    owner_a = uuid.uuid4()
    owner_b = uuid.uuid4()
    scopes = (
        SourceScope("default", source_id, owner_a, "ws-a"),
        SourceScope("default", source_id, owner_b, "ws-b"),
        SourceScope("default", source_id, owner_a, "ws-c"),
    )
    for scope in scopes:
        store.source_lifecycle_authority.register_active(scope)

    collections = (
        await store.create_collection(
            name="Owner A / workspace A",
            embedding_model="stub-embed-8",
            embedding_dim=8,
            created_by_user_id=owner_a,
        ),
        await store.create_collection(
            name="Owner B / workspace B",
            embedding_model="stub-embed-8",
            embedding_dim=8,
            created_by_user_id=owner_b,
        ),
        await store.create_collection(
            name="Owner A / workspace C",
            embedding_model="stub-embed-8",
            embedding_dim=8,
            created_by_user_id=owner_a,
        ),
    )
    documents = []
    for index, (collection, scope) in enumerate(zip(collections, scopes)):
        documents.append(
            await store.add_document(
                collection_id=collection.id,
                title=f"Scoped {index}",
                text=f"scope {index}",
                metadata={"source_id": source_id},
                chunks=[f"scope {index}"],
                embeddings=[[1.0] + [0.0] * 7],
                source_id=source_id,
                source_chunks=[f"scope {index}"],
                source_scope=scope,
                actor_user_id=scope.owner_user_id,
            )
        )

    unbound = await store.add_document(
        collection_id=collections[0].id,
        title="Unbound metadata hint",
        text="must survive",
        metadata={"fileId": "shared-source-id"},
        chunks=["must survive"],
        embeddings=[[1.0] + [0.0] * 7],
        source_chunks=["must survive"],
        actor_user_id=owner_a,
    )

    permit = store.source_lifecycle_authority.begin_delete(
        scopes[0],
        operation_id="del_scope_a",
    )
    assert await store.mark_source_deleting(
        source_id,
        deletion_permit=permit,
    ) == 1
    plan = await store.prepare_source_cleanup(
        source_id,
        deletion_permit=permit,
    )
    assert [target.document_id for target in plan.targets] == [documents[0].id]
    assert await store.execute_source_cleanup(
        plan,
        deletion_permit=permit,
        actor_user_id=owner_a,
    ) == 1

    with pytest.raises(DocumentNotFound):
        await store.get_document(documents[0].id)
    assert (await store.get_document(documents[1].id)).lifecycle_status == "active"
    assert (await store.get_document(documents[2].id)).lifecycle_status == "active"
    assert (await store.get_document(unbound.id)).lifecycle_status == "active"


@pytest.mark.asyncio
async def test_ingest_publishes_into_generation_active_at_commit(
    monkeypatch,
):
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    real_embed = service._embed_text
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_embed(**kwargs):
        started.set()
        await release.wait()
        return await real_embed(**kwargs)

    monkeypatch.setattr(service, "_embed_text", delayed_embed)
    ingest = asyncio.create_task(
        service.add_document(
            collection_id=collection.id,
            title="Vertrag.pdf",
            text="Neue Quelle.",
            metadata={"fileId": "file-a"},
        )
    )
    await started.wait()
    generation_id = "gen_after_upload_started"
    build_contract_hash = service.build_contract_hash(collection)
    await service.begin_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash=build_contract_hash,
        manifest={},
    )
    await service.activate_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        expected_document_ids=[],
        expected_manifest={},
        build_contract_hash=build_contract_hash,
    )
    release.set()
    document = await ingest

    chunks = await store.get_chunks(document.id)
    assert chunks
    assert {chunk.generation_id for chunk in chunks} == {generation_id}


@pytest.mark.asyncio
async def test_shadow_retry_keeps_active_projection_until_atomic_activation():
    store = MemoryKnowledgeStore()
    collection = await store.create_collection(
        name="Projection",
        embedding_model="m",
        embedding_dim=2,
    )
    canonical = "alpha beta"
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text=canonical,
        metadata={},
        chunks=[canonical],
        embeddings=[[1.0, 0.0]],
        source_chunks=[canonical],
        source_spans=[(0, len(canonical.encode("utf-8")))],
        document_content_hash=content_hash,
        revision_id="rev_projection",
    )
    generation_id = "gen_projection_shadow"
    manifest = {document.id: "rev_projection"}
    await store.begin_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash="projection-contract",
        manifest=manifest,
    )
    kwargs = {
        "document_id": document.id,
        "chunks": ["alpha", " beta"],
        "embeddings": [[1.0, 0.0], [0.0, 1.0]],
        "source_chunks": ["alpha", " beta"],
        "source_spans": [(0, 5), (5, 10)],
        "document_content_hash": content_hash,
        "revision_id": "rev_projection",
        "generation_id": generation_id,
    }
    await store.reembed_document(**kwargs)
    with store._lock:
        first_ids = [
            chunk.id
            for chunk in store._chunks[document.id]
            if chunk.generation_id == generation_id
        ]
    assert (await store.get_document(document.id)).chunk_count == 1

    await store.reembed_document(**kwargs)
    with store._lock:
        retry_ids = [
            chunk.id
            for chunk in store._chunks[document.id]
            if chunk.generation_id == generation_id
        ]
    assert retry_ids == first_ids
    assert (await store.get_document(document.id)).chunk_count == 1

    validation = GenerationBuildValidation(
        embedding_dim=2,
        documents={
            document.id: GenerationDocumentValidation(
                revision_id="rev_projection",
                content_hash=content_hash,
                source_spans=((0, 5), (5, 10)),
            )
        },
    )
    await store.activate_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        expected_document_ids=[document.id],
        expected_manifest=manifest,
        expected_validation=validation,
        build_contract_hash="projection-contract",
    )
    assert (await store.get_document(document.id)).chunk_count == 2
    assert (await store.get_collection(collection.id)).active_generation_id == (
        generation_id
    )


@pytest.mark.asyncio
async def test_generation_validation_rejects_corrupt_source_span_before_publish():
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Validated")
    document = await service.add_document(
        collection_id=collection.id,
        title="Source",
        text="Äußerst genaue Quelle",
        metadata={},
    )
    prior_generation_id = (
        await store.get_collection(collection.id)
    ).active_generation_id
    generation_id = "gen_corrupt_span"
    build_contract_hash = service.build_contract_hash(
        collection, contextualize=False
    )
    manifest = {document.id: document.active_revision_id or ""}
    await service.begin_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash=build_contract_hash,
        manifest=manifest,
    )
    await service.reembed_document(
        document=document,
        embedding_model=collection.embedding_model,
        generation_id=generation_id,
        contextualize=False,
    )
    with store._lock:
        chunks = store._chunks[document.id]
        staged_index = next(
            index
            for index, chunk in enumerate(chunks)
            if chunk.generation_id == generation_id
        )
        chunks[staged_index] = replace(
            chunks[staged_index],
            source_end=(chunks[staged_index].source_end or 0) - 1,
        )

    with pytest.raises(GenerationValidationError):
        await service.activate_generation(
            collection_id=collection.id,
            generation_id=generation_id,
            expected_document_ids=[document.id],
            expected_manifest=manifest,
            build_contract_hash=build_contract_hash,
        )

    assert (
        await store.get_collection(collection.id)
    ).active_generation_id == prior_generation_id


@pytest.mark.asyncio
async def test_memory_generation_retention_sweeps_without_reindex_submission():
    store = MemoryKnowledgeStore()
    context = make_knowledge_context(store=store)
    service = KnowledgeService(
        knowledge=context,
        chunk_max_chars=2_000,
        max_document_chars=100_000,
        generation_rollback_retention_seconds=0,
    )
    collection = await service.create_collection(name="Retention")
    document = await service.add_document(
        collection_id=collection.id,
        title="Source",
        text="canonical source",
        metadata={},
    )

    async def publish(generation_id: str) -> None:
        current = await store.get_document(document.id)
        manifest = {current.id: current.active_revision_id or ""}
        build_hash = service.build_contract_hash(
            collection, contextualize=False
        )
        await service.begin_generation(
            collection_id=collection.id,
            generation_id=generation_id,
            build_contract_hash=build_hash,
            manifest=manifest,
        )
        await service.reembed_document(
            document=current,
            embedding_model=collection.embedding_model,
            generation_id=generation_id,
            contextualize=False,
        )
        await service.activate_generation(
            collection_id=collection.id,
            generation_id=generation_id,
            expected_document_ids=[current.id],
            expected_manifest=manifest,
            build_contract_hash=build_hash,
        )

    await publish("gen_retained")
    await publish("gen_active")
    assert await store.generation_cleanup_collection_ids() == [collection.id]

    report = await service.prune_expired_generations_all()

    assert report == {"collections": 1, "chunks": 2}
    assert store._generations["gen_retained"].status == "deleted"
    assert store._generations["gen_active"].status == "active"


@pytest.mark.asyncio
async def test_prepared_revision_publish_retry_reads_existing_cas_result():
    embeddings = StubEmbeddings()
    store = MemoryKnowledgeStore()
    service = make_service(
        make_knowledge_context(store=store, embeddings=embeddings)
    )
    collection = await service.create_collection(name="Idempotent")
    reservation = await service.reserve_document_revision(
        collection_id=collection.id,
        title="Source",
        text="canonical source",
        metadata={"source_id": "document:idempotent"},
    )
    prepared = await service.prepare_reserved_document_revision(
        document_id=reservation.document_id,
        revision_id=reservation.revision_id,
        contextualize=False,
    )
    published = await service.publish_prepared_document_revision(prepared)
    calls_after_publish = embeddings.document_calls

    retry = await service.prepare_reserved_document_revision(
        document_id=reservation.document_id,
        revision_id=reservation.revision_id,
        contextualize=False,
    )
    published_retry = await service.publish_prepared_document_revision(retry)

    assert retry.embedded is None
    assert retry.already_published is not None
    assert published_retry.id == published.id
    assert published_retry.active_revision_id == reservation.revision_id
    assert embeddings.document_calls == calls_after_publish


@pytest.mark.asyncio
async def test_documents_from_different_sources_coexist():
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    register_asset_source(store, "file-a")
    register_asset_source(store, "file-b")

    await service.add_document(
        collection_id=collection.id, title="A.pdf", text="Text A",
        metadata={"fileId": "file-a"},
    )
    await service.add_document(
        collection_id=collection.id, title="B.pdf", text="Text B",
        metadata={"fileId": "file-b"},
    )
    # Documents without a source file are never coalesced either.
    await service.add_document(collection_id=collection.id, title="C", text="Text C")
    await service.add_document(collection_id=collection.id, title="D", text="Text D")

    assert len(await store.list_documents(collection.id)) == 4


@pytest.mark.asyncio
async def test_search_ranks_matching_document_first_and_scopes_collections():
    embeddings = StubEmbeddings()
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(embeddings=embeddings, store=store))

    legal = await service.create_collection(name="Recht")
    tech = await service.create_collection(name="Technik")
    await service.add_document(
        collection_id=legal.id,
        title="Haftungsvertrag",
        text="Die Haftung ist auf den Auftragswert begrenzt.",
    )
    await service.add_document(
        collection_id=tech.id,
        title="Serverhandbuch",
        text="Der Server startet mit systemctl start inqtrix.",
    )

    hits = await service.search(query="Haftung Auftragswert begrenzt")
    assert hits[0].document_title == "Haftungsvertrag"

    scoped = await service.search(
        query="Haftung Auftragswert begrenzt", collection_ids=[tech.id]
    )
    assert all(hit.document_title == "Serverhandbuch" for hit in scoped)


@pytest.mark.asyncio
async def test_search_with_unknown_collection_raises():
    service = make_service()
    with pytest.raises(CollectionNotFound):
        await service.search(query="x", collection_ids=["kc_unknown"])


@pytest.mark.asyncio
async def test_reembed_preserves_the_stable_citation_key():
    """Physical chunk ids may rotate; the public citation key must not."""
    embeddings = StubEmbeddings()
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(embeddings=embeddings, store=store))

    collection = await service.create_collection(name="Recht")
    document = await service.add_document(
        collection_id=collection.id,
        title="Haftungsvertrag",
        text="Die Haftung ist begrenzt.\n\nDie Frist betraegt 24 Stunden.",
    )

    before = await store.get_chunks(document.id)
    assert before, "fixture must produce at least one chunk"
    keys_before = [
        (chunk.document_id, chunk.chunk_index) for chunk in before
    ]

    await service.reembed_document(
        document=document,
        embedding_model=collection.embedding_model,
        actor_user_id=None,
    )

    after = await store.get_chunks(document.id)
    keys_after = [(chunk.document_id, chunk.chunk_index) for chunk in after]
    assert keys_after == keys_before
    # The read surface carries no vectors (lockstep with Postgres).
    assert all(chunk.embedding == () for chunk in after)


@pytest.mark.asyncio
async def test_get_chunks_orders_by_index_and_404s():
    """``get_chunks`` returns chunk_index order regardless of physical
    order, and raises for an unknown document."""
    store = MemoryKnowledgeStore()
    service = make_service(make_knowledge_context(store=store))
    collection = await service.create_collection(name="Recht")
    document = await service.add_document(
        collection_id=collection.id,
        title="D",
        text="Erster Absatz.\n\nZweiter Absatz.\n\nDritter Absatz.",
    )
    # Physically reverse the stored chunk list; get_chunks must still
    # return chunk_index order (0,1,2), not physical order.
    store._chunks[document.id] = list(reversed(store._chunks[document.id]))
    chunks = await store.get_chunks(document.id)
    assert [chunk.chunk_index for chunk in chunks] == sorted(
        chunk.chunk_index for chunk in chunks
    )
    with pytest.raises(DocumentNotFound):
        await store.get_chunks("kd_unknown")


@pytest.mark.asyncio
async def test_delete_unknown_document_raises():
    store = MemoryKnowledgeStore()
    with pytest.raises(DocumentNotFound):
        await store.delete_document("kd_unknown", actor_user_id=None)


@pytest.mark.asyncio
async def test_memory_vector_index_scrolls_distinct_groups_scoped_to_model():
    """scroll_chunk_groups returns the distinct (collection, document) groups
    for one model — the reverse-reconcile input. Chunks of one doc collapse to
    a single group; other models are excluded."""
    from inqtrix.knowledge.stores.vector_index import ChunkVector, MemoryVectorIndex

    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m", collection_id="kc1", document_id="kd1",
        vectors=[ChunkVector("ch1", (1.0,)), ChunkVector("ch2", (0.0,))],
    )
    await index.upsert(
        embedding_model="m", collection_id="kc1", document_id="kd2",
        vectors=[ChunkVector("ch3", (1.0,))],
    )
    await index.upsert(
        embedding_model="other", collection_id="kc9", document_id="kd9",
        vectors=[ChunkVector("ch9", (1.0,))],
    )
    groups = await index.scroll_chunk_groups(embedding_model="m")
    assert groups == {("kc1", "kd1"), ("kc1", "kd2")}


# ------------------------------------------------------------------ #
# Service validation
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_service_rejects_empty_fields():
    service = make_service()
    with pytest.raises(KnowledgeValidationError, match="'name'"):
        await service.create_collection(name="  ")
    collection = await service.create_collection(name="K")
    with pytest.raises(KnowledgeValidationError, match="'title'"):
        await service.add_document(collection_id=collection.id, title=" ", text="x")
    with pytest.raises(KnowledgeValidationError, match="'text'"):
        await service.add_document(collection_id=collection.id, title="T", text=" ")
    with pytest.raises(KnowledgeValidationError, match="'query'"):
        await service.search(query="  ")


@pytest.mark.asyncio
async def test_service_enforces_document_size_guard():
    identity = MemoryIdentityStore()
    service = KnowledgeService(
        knowledge=make_knowledge_context(),
        authorization=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        chunk_max_chars=2_000,
        max_document_chars=100,
    )
    collection = await service.create_collection(name="K")
    with pytest.raises(KnowledgeValidationError, match="Document too large"):
        await service.add_document(
            collection_id=collection.id, title="T", text="x" * 200
        )


@pytest.mark.asyncio
async def test_service_rejects_unselectable_embedding_model():
    context = make_knowledge_context(
        embeddings=StubEmbeddings(selectable=["allowed-model"]),
    )
    service = make_service(context)
    with pytest.raises(KnowledgeValidationError, match="is unavailable"):
        await service.create_collection(name="K", embedding_model="forbidden-model")


@pytest.mark.asyncio
async def test_service_probes_dimension_for_uncatalogued_model():
    service = make_service()
    collection = await service.create_collection(name="K")
    assert collection.embedding_dim == StubEmbeddings.DIM


# ------------------------------------------------------------------ #
# Embedding cards
# ------------------------------------------------------------------ #


def test_catalogued_models_resolve_with_dims():
    card = resolve_embedding_card("BAAI/bge-m3")
    assert card is not None
    assert card.dims == 1024
    assert card.multilingual is True


def test_unknown_model_degrades_to_none_card():
    catalog = build_embedding_catalog(["text-embedding-3-large", "custom/model"])
    assert catalog[0]["card"]["dims"] == 3072
    assert catalog[1] == {"model_id": "custom/model", "card": None}


def test_every_card_has_positive_dims_and_source():
    for card in EMBEDDING_CARDS:
        assert card.dims > 0
        assert card.max_input_tokens > 0
        assert card.source_url.startswith("https://")
