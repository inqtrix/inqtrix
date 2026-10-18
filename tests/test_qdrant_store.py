"""Qdrant store integration tests (gated on INQTRIX_TEST_QDRANT_URL).

Port-parity with the memory store plus the hybrid branch, for the legacy
``QdrantKnowledgeStore`` sole-store (the ``qdrant`` vector backend
WITHOUT Postgres). The full-stack path (Postgres + ``QdrantVectorIndex``)
is verified separately. Start the dev stack and run with:

    INQTRIX_TEST_QDRANT_URL=http://127.0.0.1:6333 \\
    INQTRIX_TEST_QDRANT_API_KEY=inqtrix-dev-qdrant-key \\
    uv run pytest tests/test_qdrant_store.py -v
"""

from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
import pytest_asyncio

from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentNotFound,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    KnowledgeProviderContext,
    SourceDeletionConflict,
)
from inqtrix.services.knowledge_service import KnowledgeService

from tests.test_knowledge_engine import StubEmbeddings

QDRANT_URL = os.environ.get("INQTRIX_TEST_QDRANT_URL", "")
QDRANT_API_KEY = os.environ.get("INQTRIX_TEST_QDRANT_API_KEY", "")

pytestmark = pytest.mark.qdrant


@pytest_asyncio.fixture()
async def store():
    from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore

    instance = QdrantKnowledgeStore(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    created: list[str] = []
    original_create = instance.create_collection

    async def tracking_create(**kwargs):
        collection = await original_create(**kwargs)
        created.append(collection.id)
        return collection

    instance.create_collection = tracking_create  # type: ignore[method-assign]
    yield instance
    for collection_id in created:
        try:
            await instance.delete_collection(collection_id)
        except CollectionNotFound:
            pass


@pytest.fixture()
def service(store) -> KnowledgeService:
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=store,
            default_top_k=4,
        ),
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )


@pytest.mark.asyncio
async def test_collection_and_document_lifecycle(service, store):
    collection = await service.create_collection(name="Vertraege")
    assert collection.embedding_model == "stub-embed-8"
    assert collection.embedding_dim == 8

    document = await service.add_document(
        collection_id=collection.id,
        title="Rahmenvertrag",
        text="Die Haftung ist auf den Auftragswert begrenzt.",
        metadata={"source": "vertrag.pdf"},
    )
    fetched = await store.get_collection(collection.id)
    assert fetched.document_count == 1

    documents = await store.list_documents(collection.id)
    assert [doc.title for doc in documents] == ["Rahmenvertrag"]
    assert documents[0].metadata == {"source": "vertrag.pdf"}

    full = await store.get_document(document.id)
    assert "Haftung" in full.text

    await store.delete_document(document.id)
    assert (await store.get_collection(collection.id)).document_count == 0
    with pytest.raises(DocumentNotFound):
        await store.get_document(document.id)

    await store.delete_collection(collection.id)
    with pytest.raises(CollectionNotFound):
        await store.get_collection(collection.id)


@pytest.mark.asyncio
async def test_dimension_mismatch_is_rejected_loudly(store):
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
        )


@pytest.mark.asyncio
async def test_search_scopes_to_the_requested_collections(service):
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

    scoped = await service.search(
        query="Haftung Auftragswert begrenzt", collection_ids=[tech.id]
    )
    assert all(hit.document_title == "Serverhandbuch" for hit in scoped)

    everything = await service.search(
        query="Haftung Auftragswert begrenzt",
        collection_ids=[legal.id, tech.id],
    )
    assert everything[0].document_title == "Haftungsvertrag"


@pytest.mark.asyncio
async def test_hybrid_lexical_branch_finds_exact_terms(service, store):
    """BM25 must surface an exact German term even when the stub dense
    vectors are uninformative — the hybrid value proposition."""
    assert store.supports_hybrid is True
    collection = await service.create_collection(name="Hybrid")
    await service.add_document(
        collection_id=collection.id,
        title="Reisekosten",
        text="Die Verpflegungspauschale betraegt 28 Euro pro Reisetag.",
    )
    await service.add_document(
        collection_id=collection.id,
        title="Onboarding",
        text="Neue Mitarbeitende erhalten einen Buddy fuer drei Monate.",
    )

    hits = await service.search(
        query="Verpflegungspauschale", collection_ids=[collection.id]
    )
    assert hits[0].document_title == "Reisekosten"


@pytest.mark.asyncio
async def test_unknown_collection_raises(store):
    with pytest.raises(CollectionNotFound):
        await store.get_collection("kc_does_not_exist")
    with pytest.raises(CollectionNotFound):
        await store.list_documents("kc_does_not_exist")


@pytest.mark.asyncio
async def test_document_revision_cas_converges_across_store_instances(store):
    from qdrant_client import models

    from inqtrix.knowledge.stores.qdrant_store import (
        QdrantKnowledgeStore,
        _model_slug,
    )

    peer = QdrantKnowledgeStore(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    collection = await store.create_collection(
        name="Cross-instance revisions",
        embedding_model="stub-embed-8",
        embedding_dim=8,
    )
    source_id = f"test-source:{collection.id}"
    first, second = await asyncio.gather(
        store.reserve_document_revision(
            collection_id=collection.id,
            source_id=source_id,
            revision_id="rev_cross_instance_a",
            content_hash=hashlib.sha256(b"first body").hexdigest(),
            title="First",
            text="first body",
            metadata={"source_id": source_id},
        ),
        peer.reserve_document_revision(
            collection_id=collection.id,
            source_id=source_id,
            revision_id="rev_cross_instance_b",
            content_hash=hashlib.sha256(b"second body").hexdigest(),
            title="Second",
            text="second body",
            metadata={"source_id": source_id},
        ),
    )

    assert first.document_id == second.document_id
    assert {first.sequence, second.sequence} == {1, 2}
    older, desired = sorted((first, second), key=lambda item: item.sequence)
    with pytest.raises(DocumentRevisionSuperseded):
        await store.load_reserved_document_revision(
            document_id=older.document_id,
            revision_id=older.revision_id,
        )
    with pytest.raises(DocumentRevisionSuperseded):
        await peer.publish_document_revision(
            reservation=older,
            title="stale",
            text="stale body",
            metadata={},
            chunks=["stale body"],
            embeddings=[[0.1] * 8],
            source_chunks=["stale body"],
            retrieval_contexts=[None],
            source_spans=[(0, len("stale body".encode("utf-8")))],
        )

    reserved = await peer.load_reserved_document_revision(
        document_id=desired.document_id,
        revision_id=desired.revision_id,
    )
    desired_text = reserved.revision.text
    publish_kwargs = {
        "reservation": desired,
        "title": reserved.revision.title,
        "text": desired_text,
        "metadata": dict(reserved.revision.metadata),
        "chunks": [desired_text],
        "embeddings": [[0.2] * 8],
        "source_chunks": [desired_text],
        "retrieval_contexts": [None],
        "source_spans": [(0, len(desired_text.encode("utf-8")))],
    }
    published, repeated = await asyncio.gather(
        store.publish_document_revision(**publish_kwargs),
        peer.publish_document_revision(**publish_kwargs),
    )

    assert published.id == repeated.id == desired.document_id
    assert published.active_revision_id == desired.revision_id
    assert repeated.active_revision_id == desired.revision_id
    chunks_name = _model_slug(collection.embedding_model)
    point_count = peer._client.count(  # noqa: SLF001 - integration evidence
        collection_name=chunks_name,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=desired.document_id),
                ),
            ]
        ),
        exact=True,
    ).count
    assert point_count == 1

    restarted = QdrantKnowledgeStore(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    persisted = await restarted.get_document(desired.document_id)
    assert persisted.active_revision_id == desired.revision_id
    candidates = await restarted.search(
        query_embedding=[0.2] * 8,
        collection_ids=[collection.id],
        top_k=4,
        embedding_model=collection.embedding_model,
    )
    assert [candidate.document_title for candidate in candidates] == [
        reserved.revision.title
    ]


@pytest.mark.asyncio
async def test_revision_replacement_and_identical_retry_leave_one_active_chunk_set(
    service, store
):
    from qdrant_client import models

    from inqtrix.knowledge.stores.qdrant_store import (
        REGISTRY_COLLECTION,
        _model_slug,
    )

    collection = await service.create_collection(name="Revision cleanup")
    source_id = f"replacement-source:{collection.id}"
    original = await service.add_document(
        collection_id=collection.id,
        title="Original",
        text="QDRANT-OLD-MARKER canonical source",
        metadata={"source_id": source_id},
    )
    replacement = await service.add_document(
        collection_id=collection.id,
        title="Replacement",
        text="QDRANT-NEW-MARKER canonical source",
        metadata={"source_id": source_id},
    )

    assert replacement.id == original.id
    assert replacement.active_revision_id != original.active_revision_id
    assert replacement.desired_sequence == original.desired_sequence + 1
    repeated = await service.add_document(
        collection_id=collection.id,
        title="Replacement",
        text="QDRANT-NEW-MARKER canonical source",
        metadata={"source_id": source_id},
    )
    assert repeated.id == replacement.id
    assert repeated.active_revision_id == replacement.active_revision_id
    assert repeated.desired_sequence == replacement.desired_sequence

    active_chunks = await store.get_chunks(replacement.id)
    assert len(active_chunks) == 1
    assert active_chunks[0].chunk_index == 0
    assert active_chunks[0].embedding == ()
    assert active_chunks[0].revision_id == replacement.active_revision_id
    assert active_chunks[0].source_verified is True
    assert "QDRANT-NEW-MARKER" in active_chunks[0].text
    assert "QDRANT-OLD-MARKER" not in active_chunks[0].text
    receipt = await service.active_document_embedding_receipt(replacement.id)
    assert receipt.input_count == 1
    assert receipt.amount > 0
    with pytest.raises(DocumentNotFound):
        await store.get_chunks("kd_missing")

    chunks_name = _model_slug(collection.embedding_model)
    chunk_points, _offset = store._client.scroll(  # noqa: SLF001
        collection_name=chunks_name,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=replacement.id),
                )
            ]
        ),
        limit=10,
        with_payload=True,
        with_vectors=False,
        consistency=models.ReadConsistencyType.ALL,
    )
    assert len(chunk_points) == 1
    assert chunk_points[0].payload["revision_id"] == replacement.active_revision_id
    assert "QDRANT-NEW-MARKER" in chunk_points[0].payload["text"]
    assert "QDRANT-OLD-MARKER" not in chunk_points[0].payload["text"]

    revision_points, _offset = store._client.scroll(  # noqa: SLF001
        collection_name=REGISTRY_COLLECTION,
        scroll_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="kind",
                    match=models.MatchValue(value="document_revision"),
                ),
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=replacement.id),
                ),
            ]
        ),
        limit=10,
        with_payload=True,
        with_vectors=False,
        consistency=models.ReadConsistencyType.ALL,
    )
    statuses = {
        point.payload["revision_id"]: point.payload["status"]
        for point in revision_points
    }
    assert statuses == {
        original.active_revision_id: "superseded",
        replacement.active_revision_id: "active",
    }

    embedding_model = collection.embedding_model
    await store.delete_collection(collection.id)
    assert await store.count_collection_residuals(
        collection_id=collection.id,
        embedding_model=embedding_model,
    ) == {
        "collections": 0,
        "documents": 0,
        "revisions": 0,
        "vectors": 0,
    }


@pytest.mark.asyncio
async def test_source_tombstone_wins_during_revision_publication(
    store, monkeypatch
):
    from qdrant_client import models

    from inqtrix.knowledge.stores.qdrant_store import (
        QdrantKnowledgeStore,
        _model_slug,
    )

    peer = QdrantKnowledgeStore(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    collection = await store.create_collection(
        name="Deletion fence",
        embedding_model="stub-embed-8",
        embedding_dim=8,
    )
    source_id = f"deleted-source:{collection.id}"
    reservation = await store.reserve_document_revision(
        collection_id=collection.id,
        source_id=source_id,
        revision_id="rev_deleted_during_publish",
        content_hash="d" * 64,
        title="Deleted",
        text="must not publish",
        metadata={"source_id": source_id},
    )
    original_upsert = store._upsert_chunk_points  # noqa: SLF001

    def upsert_then_tombstone(*args, **kwargs):
        original_upsert(*args, **kwargs)
        peer._sync_mark_source_deleting(source_id)  # noqa: SLF001

    monkeypatch.setattr(store, "_upsert_chunk_points", upsert_then_tombstone)
    with pytest.raises(SourceDeletionConflict):
        await store.publish_document_revision(
            reservation=reservation,
            title="Deleted",
            text="must not publish",
            metadata={"source_id": source_id},
            chunks=["must not publish"],
            embeddings=[[0.3] * 8],
            source_chunks=["must not publish"],
            retrieval_contexts=[None],
            source_spans=[(0, len("must not publish".encode("utf-8")))],
        )
    with pytest.raises(SourceDeletionConflict):
        await peer.reserve_document_revision(
            collection_id=collection.id,
            source_id=source_id,
            revision_id="rev_must_not_resurrect",
            content_hash="e" * 64,
            title="Resurrection",
            text="forbidden",
        )

    chunks_name = _model_slug(collection.embedding_model)
    residuals = peer._client.count(  # noqa: SLF001 - integration evidence
        collection_name=chunks_name,
        count_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="document_id",
                    match=models.MatchValue(value=reservation.document_id),
                ),
                models.FieldCondition(
                    key="revision_id",
                    match=models.MatchValue(value=reservation.revision_id),
                ),
            ]
        ),
        exact=True,
    ).count
    assert residuals == 0
    hidden = await store.get_document(reservation.document_id)
    assert hidden.lifecycle_status == "deleting"
    assert await store.list_documents(collection.id) == []
    assert await peer.delete_source(source_id) == 1
    with pytest.raises(DocumentNotFound):
        await store.get_document(reservation.document_id)
    assert await peer.source_residuals(source_id) == {
        "documents": 0,
        "chunks": 0,
        "vectors": 0,
    }
