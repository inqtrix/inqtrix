"""Postgres integration tests for the canonical knowledge store (gated).

Same gating/conventions as the other storage suites: a disposable
database via ``INQTRIX_TEST_DATABASE_URL``, operations under the
restricted app role, RLS as the second defense layer. Uses the
in-process :class:`MemoryVectorIndex` for vectors so the suite needs
only Postgres (no Qdrant). Verifies the full async ``KnowledgeStore``
contract against Postgres: collection/document lifecycle, document
counts, chunk persistence, retrieval hydration, in-place reembed, cascade
delete, and the dimension/absence error paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError

from inqtrix.knowledge.stores.ports import (
    CollectionNotFound,
    DocumentNotFound,
    DocumentRevisionSuperseded,
    EmbeddingDimensionMismatch,
    GenerationBuildValidation,
    GenerationDocumentValidation,
    GenerationPruneError,
    GenerationValidationError,
    IndexGenerationSuperseded,
)
from inqtrix.knowledge.stores.postgres_store import PostgresKnowledgeStore
from inqtrix.knowledge.stores.vector_index import ChunkVector, MemoryVectorIndex
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import users
from inqtrix.storage.indexing_orm import indexing_job_events, indexing_jobs
from inqtrix.storage.knowledge_orm import (
    knowledge_chunks,
    knowledge_collections,
    knowledge_document_revisions,
    knowledge_documents,
    knowledge_index_generations,
)
from inqtrix.storage.source_lifecycle_orm import source_lifecycles
from inqtrix.source_authority import SourceScope
from inqtrix.storage.migrate import run_migrations

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
STRANGER_USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture(scope="session", autouse=True)
def knowledge_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def store():
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user (cross-tenant cleanup)."
                )
            # Child→parent order (FK), though CASCADE would handle it.
            await session.execute(indexing_job_events.delete())
            await session.execute(indexing_jobs.delete())
            await session.execute(knowledge_chunks.delete())
            await session.execute(knowledge_documents.delete())
            await session.execute(knowledge_collections.delete())
            await session.execute(
                delete(source_lifecycles).where(
                    source_lifecycles.c.source_id
                    == "asset:knowledge-scope-test"
                )
            )
            for user_id, subject in (
                (OWNER_USER_ID, "knowledge-store-owner"),
                (STRANGER_USER_ID, "knowledge-store-stranger"),
            ):
                statement = pg_insert(users).values(
                    id=user_id,
                    tenant_id="default",
                    issuer="http://idp.example",
                    subject=subject,
                    email=f"{subject}@example.com",
                    email_verified=True,
                    display_name=subject,
                    disabled_at=None,
                )
                await session.execute(
                    statement.on_conflict_do_update(
                        index_elements=[users.c.id],
                        set_={"disabled_at": None},
                    )
                )
    knowledge_store = PostgresKnowledgeStore(
        engine=engine, app_role=APP_ROLE, vector_index=MemoryVectorIndex()
    )
    yield knowledge_store
    await knowledge_store.aclose()


@pytest.mark.asyncio
async def test_collection_share_facts_read_owner_and_name_only(store) -> None:
    from sqlalchemy import event

    from inqtrix.auth.shares import ShareFacts

    owned = await store.create_collection(
        name="Owned", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    legacy = await store.create_collection(
        name="Legacy", embedding_model="m", embedding_dim=2,
    )
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._engine.sync_engine, "before_cursor_execute", record)
    try:
        facts = await store.collection_share_facts(
            [owned.id, legacy.id, "kc_missing"]
        )
    finally:
        event.remove(store._engine.sync_engine, "before_cursor_execute", record)

    assert facts == {
        owned.id: ShareFacts(OWNER_USER_ID, "Owned"),
        legacy.id: ShareFacts(None, "Legacy"),
    }
    reads = [statement for statement in statements if "knowledge" in statement]
    assert len(reads) == 1
    assert "knowledge_documents" not in reads[0]
    assert await store.collection_share_facts([]) == {}


@pytest.mark.asyncio
async def test_collection_lifecycle_and_document_count(store) -> None:
    collection = await store.create_collection(
        name="Vertraege", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    assert collection.embedding_model == "m"
    assert collection.document_count == 0

    fetched = await store.get_collection(collection.id)
    assert fetched.created_by_user_id == OWNER_USER_ID
    assert (await store.list_collections())[0].id == collection.id

    document = await store.add_document(
        collection_id=collection.id, title="Doc A", text="alpha beta",
        metadata={"source": "a.pdf"},
        chunks=["alpha", "beta"], embeddings=[[1.0, 0.0], [0.0, 1.0]],
        source_chunks=["alpha", "beta"],
        actor_user_id=OWNER_USER_ID,
    )
    assert document.chunk_count == 2
    assert (await store.get_collection(collection.id)).document_count == 1

    docs = await store.list_documents(collection.id)
    assert [d.id for d in docs] == [document.id]
    assert (await store.get_document(document.id)).text == "alpha beta"
    assert (await store.get_document(document.id)).metadata == {"source": "a.pdf"}


@pytest.mark.asyncio
async def test_source_cleanup_isolated_by_owner_and_workspace_scope(store) -> None:
    source_id = "asset:knowledge-scope-test"
    scopes = (
        SourceScope("default", source_id, OWNER_USER_ID, "ws-a"),
        SourceScope("default", source_id, STRANGER_USER_ID, "ws-b"),
        SourceScope("default", source_id, OWNER_USER_ID, "ws-c"),
    )
    async with store._session() as session:
        for scope in scopes:
            await store._source_authority.register_active_in_session(
                session,
                scope,
            )

    collections = (
        await store.create_collection(
            name="Owner A / workspace A",
            embedding_model="m",
            embedding_dim=2,
            created_by_user_id=OWNER_USER_ID,
        ),
        await store.create_collection(
            name="Owner B / workspace B",
            embedding_model="m",
            embedding_dim=2,
            created_by_user_id=STRANGER_USER_ID,
        ),
        await store.create_collection(
            name="Owner A / workspace C",
            embedding_model="m",
            embedding_dim=2,
            created_by_user_id=OWNER_USER_ID,
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
                embeddings=[[1.0, 0.0]],
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
        metadata={"fileId": "knowledge-scope-test"},
        chunks=["must survive"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["must survive"],
        actor_user_id=OWNER_USER_ID,
    )

    async with store._session() as session:
        permit = await store._source_authority.begin_delete_in_session(
            session,
            scopes[0],
            operation_id="del_knowledge_scope_a",
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
        actor_user_id=OWNER_USER_ID,
    ) == 1

    with pytest.raises(DocumentNotFound):
        await store.get_document(documents[0].id)
    assert (await store.get_document(documents[1].id)).lifecycle_status == "active"
    assert (await store.get_document(documents[2].id)).lifecycle_status == "active"
    assert (await store.get_document(unbound.id)).lifecycle_status == "active"


@pytest.mark.asyncio
async def test_migration_marked_generation_admits_only_its_document_payloads(
    store,
) -> None:
    collection = await store.create_collection(
        name="Legacy payload projection",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    await store.add_document(
        collection_id=collection.id,
        title="Migrated",
        text="verified source",
        metadata={},
        chunks=["verified source"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["verified source"],
        source_spans=[(0, len("verified source"))],
        document_content_hash=hashlib.sha256(
            b"verified source"
        ).hexdigest(),
        revision_id="rev_legacy_verified_payload",
        actor_user_id=OWNER_USER_ID,
    )
    active = await store.get_collection(collection.id)
    assert active.active_generation_id is not None
    async with store._session() as session:
        await session.execute(
            update(knowledge_index_generations)
            .where(
                knowledge_index_generations.c.tenant_id == "default",
                knowledge_index_generations.c.generation_id
                == active.active_generation_id,
            )
            .values(build_contract_hash="legacy-unverified-build")
        )

    _model, scopes = await store._resolve_scope("m", [collection.id])
    assert len(scopes[0].legacy_payload_chunk_ids) == 1

    async with store._session() as session:
        await session.execute(
            update(knowledge_index_generations)
            .where(
                knowledge_index_generations.c.tenant_id == "default",
                knowledge_index_generations.c.generation_id
                == active.active_generation_id,
            )
            .values(build_contract_hash="current-build")
        )
    _model, modern_scopes = await store._resolve_scope("m", [collection.id])
    assert modern_scopes[0].legacy_payload_chunk_ids == ()


@pytest.mark.asyncio
async def test_search_reports_unverified_hydration_exclusion(store) -> None:
    """A rejected Postgres row remains visible as a text-free reindex signal."""

    collection = await store.create_collection(
        name="Legacy exclusion",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id,
        title="Legacy",
        text="canonical source",
        metadata={},
        chunks=["canonical source"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["canonical source"],
        source_spans=[(0, len("canonical source"))],
        actor_user_id=OWNER_USER_ID,
    )
    async with store._session() as session:
        await session.execute(
            update(knowledge_chunks)
            .where(
                knowledge_chunks.c.tenant_id == "default",
                knowledge_chunks.c.document_id == document.id,
            )
            .values(source_start=None)
        )

    result = await store.search(
        query_embedding=[1.0, 0.0],
        collection_ids=[collection.id],
        top_k=1,
        embedding_model="m",
    )

    assert result == []
    assert [exclusion.as_dict() for exclusion in result.exclusions] == [
        {
            "reason": "source_unverified",
            "stage": "canonical_hydration",
            "count": 1,
            "recommended_action": "reindex",
        }
    ]


@pytest.mark.asyncio
async def test_list_documents_page_keyset_walks_without_skip_or_repeat(store) -> None:
    """The DB keyset (tuple_(created_at, id) < cursor) pages a collection's
    documents with no gaps or duplicates, including across a created_at tie."""
    collection = await store.create_collection(
        name="Paged", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    added = []
    for n in range(5):
        doc = await store.add_document(
            collection_id=collection.id, title=f"Doc {n}", text="x",
            metadata={}, chunks=["x"], embeddings=[[1.0, 0.0]],
            source_chunks=["x"],
            actor_user_id=OWNER_USER_ID,
        )
        added.append(doc.id)

    full = [d.id for d in await store.list_documents(collection.id)]
    seen: list[str] = []
    cursor = None
    for _ in range(10):  # generous bound
        page, next_cursor = await store.list_documents_page(
            collection.id, limit=2, after=cursor,
        )
        seen.extend(d.id for d in page)
        assert len(page) <= 2
        if next_cursor is None:
            break
        from inqtrix.pagination import decode_cursor
        cursor = decode_cursor(next_cursor)

    assert seen == full  # same order as the unpaginated list
    assert len(seen) == len(set(seen)) == 5


@pytest.mark.asyncio
async def test_search_hydrates_from_postgres(store) -> None:
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    await store.add_document(
        collection_id=collection.id, title="Haftung", text="limit clause",
        metadata={}, chunks=["limit clause"], embeddings=[[1.0, 0.0]],
        source_chunks=["limit clause"],
        source_spans=[(0, len("limit clause"))],
        actor_user_id=OWNER_USER_ID,
    )
    candidates = await store.search(
        query_embedding=[1.0, 0.0], collection_ids=[collection.id],
        top_k=5, embedding_model="m",
    )
    assert len(candidates) == 1
    hit = candidates[0]
    # Text + title were hydrated from Postgres, not the vector index.
    assert hit.chunk.text == "limit clause"
    assert hit.chunk.source_text == "limit clause"
    assert hit.document_title == "Haftung"
    assert hit.score > 0


@pytest.mark.asyncio
async def test_reembed_replaces_chunks_in_place(store) -> None:
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id, title="D", text="one two",
        metadata={}, chunks=["one"], embeddings=[[1.0, 0.0]],
        source_chunks=["one"], source_spans=[(0, 3)],
        actor_user_id=OWNER_USER_ID,
    )
    updated = await store.reembed_document(
        document_id=document.id,
        chunks=["one", "two"], embeddings=[[1.0, 0.0], [0.0, 1.0]],
        source_chunks=["one", "two"],
        source_spans=[(0, 3), (4, 7)],
        actor_user_id=OWNER_USER_ID,
    )
    assert updated.id == document.id
    assert updated.chunk_count == 2
    hits = await store.search(
        query_embedding=[0.0, 1.0], collection_ids=[collection.id],
        top_k=5, embedding_model="m",
    )
    assert hits and hits[0].chunk.text == "two"


@pytest.mark.asyncio
async def test_legacy_reembed_preserves_chunk_ids_by_position(store) -> None:
    """The compatibility path without a revision id keeps positional ids."""
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id, title="D", text="one two three",
        metadata={}, chunks=["one", "two"],
        embeddings=[[1.0, 0.0], [0.0, 1.0]],
        source_chunks=["one", "two"],
        source_spans=[(0, 3), (4, 7)],
        actor_user_id=OWNER_USER_ID,
    )
    before = await store.get_chunks(document.id)
    assert [c.chunk_index for c in before] == [0, 1]
    id0, id1 = before[0].id, before[1].id

    # Grow to three chunks: positions 0/1 keep their ids, 2 is fresh.
    await store.reembed_document(
        document_id=document.id,
        chunks=["one", "two", "three"],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        source_chunks=["one", "two", "three"],
        source_spans=[(0, 3), (4, 7), (8, 13)],
        actor_user_id=OWNER_USER_ID,
    )
    grown = await store.get_chunks(document.id)
    assert [chunk.id for chunk in grown[:2]] == [id0, id1]
    assert grown[2].id not in {id0, id1}

    # Shrink back to one chunk: position 0 keeps its original id.
    await store.reembed_document(
        document_id=document.id, chunks=["one"], embeddings=[[1.0, 0.0]],
        source_chunks=["one"], source_spans=[(0, 3)],
        actor_user_id=OWNER_USER_ID,
    )
    shrunk = await store.get_chunks(document.id)
    assert [chunk.id for chunk in shrunk] == [id0]


@pytest.mark.asyncio
async def test_get_chunks_orders_and_404s(store) -> None:
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id, title="D", text="t",
        metadata={}, chunks=["a", "b", "c"],
        embeddings=[[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]],
        actor_user_id=OWNER_USER_ID,
    )
    chunks = await store.get_chunks(document.id)
    assert [c.chunk_index for c in chunks] == [0, 1, 2]
    with pytest.raises(DocumentNotFound):
        await store.get_chunks("kd_unknown")


@pytest.mark.asyncio
async def test_delete_document_and_collection_cascade(store) -> None:
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id, title="D", text="t",
        metadata={}, chunks=["one"], embeddings=[[1.0, 0.0]],
        actor_user_id=OWNER_USER_ID,
    )
    await store.delete_document(
        document.id, actor_user_id=OWNER_USER_ID
    )
    assert (await store.get_collection(collection.id)).document_count == 0
    with pytest.raises(DocumentNotFound):
        await store.get_document(document.id)

    await store.delete_collection(
        collection.id, actor_user_id=OWNER_USER_ID
    )
    with pytest.raises(CollectionNotFound):
        await store.get_collection(collection.id)


@pytest.mark.asyncio
async def test_owned_mutations_reject_a_different_actor(store) -> None:
    collection = await store.create_collection(
        name="C",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )

    with pytest.raises(CollectionNotFound):
        await store.add_document(
            collection_id=collection.id,
            title="Denied",
            text="x",
            metadata={},
            chunks=["x"],
            embeddings=[[1.0, 0.0]],
            actor_user_id=STRANGER_USER_ID,
        )

    document = await store.add_document(
        collection_id=collection.id,
        title="Allowed",
        text="x",
        metadata={},
        chunks=["x"],
        embeddings=[[1.0, 0.0]],
        actor_user_id=OWNER_USER_ID,
    )
    with pytest.raises(CollectionNotFound):
        await store.delete_document(
            document.id,
            actor_user_id=STRANGER_USER_ID,
        )
    with pytest.raises(CollectionNotFound):
        await store.delete_collection(
            collection.id,
            actor_user_id=STRANGER_USER_ID,
        )


@pytest.mark.asyncio
async def test_document_revision_publish_fences_cancel_before_vector_write(
    store,
    monkeypatch,
) -> None:
    collection = await store.create_collection(
        name="Revision fence",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    previous = await store.add_document(
        collection_id=collection.id,
        title="Source",
        text="previous canonical body",
        metadata={},
        chunks=["previous canonical body"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["previous canonical body"],
        source_id="document:postgres-publication-fence",
        revision_id="rev_postgres_previous",
        actor_user_id=OWNER_USER_ID,
    )
    replacement_text = "replacement must remain staged"
    replacement_hash = hashlib.sha256(replacement_text.encode("utf-8")).hexdigest()
    reservation = await store.reserve_document_revision(
        collection_id=collection.id,
        source_id="document:postgres-publication-fence",
        revision_id="rev_postgres_cancelled",
        content_hash=replacement_hash,
        build_contract_hash="revision-fence-contract",
        title="Source",
        text=replacement_text,
        metadata={},
        actor_user_id=OWNER_USER_ID,
    )
    job_id = "ix_document_publication_cancelled"
    async with store._session() as session:
        await session.execute(
            insert(indexing_jobs).values(
                job_id=job_id,
                tenant_id="default",
                collection_id=collection.id,
                collection_name=collection.name,
                embedding_model=collection.embedding_model,
                operation_kind="document_revision",
                document_id=previous.id,
                revision_id=reservation.revision_id,
                # The cancel bit is the authority fact. Keeping the historical
                # status at running proves publication does not rely only on
                # the UI-facing status transition.
                status="running",
                cancel_requested=True,
                claimed_by="pytest-index-worker",
                attempt=3,
                created_by_user_id=OWNER_USER_ID,
                created_by_tenant_id="default",
                created_at=time.time(),
            )
        )
    vector_upserts = 0
    real_upsert = store._vectors.upsert

    async def count_upserts(**kwargs):
        nonlocal vector_upserts
        vector_upserts += 1
        return await real_upsert(**kwargs)

    monkeypatch.setattr(store._vectors, "upsert", count_upserts)

    with pytest.raises(IndexGenerationSuperseded):
        await store.publish_document_revision(
            reservation=reservation,
            title="Source",
            text=replacement_text,
            metadata={},
            chunks=[replacement_text],
            embeddings=[[0.0, 1.0]],
            source_chunks=[replacement_text],
            retrieval_contexts=[None],
            source_spans=[(0, len(replacement_text.encode("utf-8")))],
            fence_job_id=job_id,
            fence_attempt=3,
            actor_user_id=OWNER_USER_ID,
        )

    current = await store.get_document(previous.id)
    assert vector_upserts == 0
    assert current.active_revision_id == previous.active_revision_id
    assert current.text == previous.text
    chunks = await store.get_chunks(previous.id)
    assert {chunk.revision_id for chunk in chunks} == {previous.active_revision_id}
    assert (
        await store._vectors.count_document(
            embedding_model=collection.embedding_model,
            document_id=previous.id,
        )
        == len(chunks)
    )
    async with store._session() as session:
        revision_status = await session.scalar(
            select(knowledge_document_revisions.c.status).where(
                knowledge_document_revisions.c.revision_id
                == reservation.revision_id
            )
        )
    assert revision_status == "staging"


@pytest.mark.asyncio
async def test_current_attempt_publishes_then_stale_revision_is_rejected(
    store,
) -> None:
    collection = await store.create_collection(
        name="Current revision fence",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    previous = await store.add_document(
        collection_id=collection.id,
        title="Source",
        text="previous",
        metadata={},
        chunks=["previous"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["previous"],
        source_id="document:postgres-current-fence",
        revision_id="rev_postgres_current_previous",
        actor_user_id=OWNER_USER_ID,
    )
    replacement_text = "current replacement"
    replacement = await store.reserve_document_revision(
        collection_id=collection.id,
        source_id="document:postgres-current-fence",
        revision_id="rev_postgres_current",
        content_hash=hashlib.sha256(
            replacement_text.encode("utf-8")
        ).hexdigest(),
        build_contract_hash="current-contract",
        title="Source",
        text=replacement_text,
        metadata={},
        actor_user_id=OWNER_USER_ID,
    )
    job_id = "ix_document_publication_current"
    async with store._session() as session:
        await session.execute(
            insert(indexing_jobs).values(
                job_id=job_id,
                tenant_id="default",
                collection_id=collection.id,
                collection_name=collection.name,
                embedding_model=collection.embedding_model,
                operation_kind="document_revision",
                document_id=previous.id,
                revision_id=replacement.revision_id,
                status="running",
                cancel_requested=False,
                claimed_by="pytest-index-worker",
                attempt=2,
                created_by_user_id=OWNER_USER_ID,
                created_by_tenant_id="default",
                created_at=time.time(),
            )
        )
    published = await store.publish_document_revision(
        reservation=replacement,
        title="Source",
        text=replacement_text,
        metadata={},
        chunks=[replacement_text],
        embeddings=[[0.0, 1.0]],
        source_chunks=[replacement_text],
        retrieval_contexts=[None],
        source_spans=[(0, len(replacement_text.encode("utf-8")))],
        fence_job_id=job_id,
        fence_attempt=2,
        actor_user_id=OWNER_USER_ID,
    )
    assert published.active_revision_id == replacement.revision_id

    await store.reserve_document_revision(
        collection_id=collection.id,
        source_id="document:postgres-current-fence",
        revision_id="rev_postgres_successor",
        content_hash=hashlib.sha256(b"successor").hexdigest(),
        build_contract_hash="successor-contract",
        title="Source",
        text="successor",
        metadata={},
        actor_user_id=OWNER_USER_ID,
    )
    with pytest.raises(DocumentRevisionSuperseded):
        await store.publish_document_revision(
            reservation=replacement,
            title="Source",
            text=replacement_text,
            metadata={},
            chunks=[replacement_text],
            embeddings=[[0.0, 1.0]],
            source_chunks=[replacement_text],
            retrieval_contexts=[None],
            source_spans=[(0, len(replacement_text.encode("utf-8")))],
            fence_job_id=job_id,
            fence_attempt=2,
            actor_user_id=OWNER_USER_ID,
        )


@pytest.mark.asyncio
async def test_shadow_generation_keeps_source_writes_available(
    store,
) -> None:
    collection = await store.create_collection(
        name="C",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text="canonical",
        metadata={},
        chunks=["canonical"],
        embeddings=[[1.0, 0.0]],
        actor_user_id=OWNER_USER_ID,
    )
    job_id = "ix_knowledge_store_maintenance"
    async with store._session() as session:
        await session.execute(
            insert(indexing_jobs).values(
                job_id=job_id,
                tenant_id="default",
                collection_id=collection.id,
                collection_name=collection.name,
                embedding_model=collection.embedding_model,
                generation_id="gen_knowledge_store_maintenance",
                status="running",
                created_by_user_id=str(OWNER_USER_ID),
                created_by_tenant_id="default",
                created_at=time.time(),
            )
        )
    try:
        concurrent = await store.add_document(
            collection_id=collection.id,
            title="Concurrent source revision",
            text="x",
            metadata={},
            chunks=["x"],
            embeddings=[[1.0, 0.0]],
            source_chunks=["x"],
            source_spans=[(0, 1)],
            actor_user_id=OWNER_USER_ID,
        )
        assert concurrent.collection_id == collection.id

        updated = await store.reembed_document(
            document_id=document.id,
            chunks=["canonical"],
            embeddings=[[0.0, 1.0]],
            source_chunks=["canonical"],
            actor_user_id=OWNER_USER_ID,
        )
        assert updated.id == document.id
    finally:
        async with store._session() as session:
            await session.execute(
                indexing_jobs.delete().where(
                    indexing_jobs.c.job_id == job_id
                )
            )


@pytest.mark.asyncio
async def test_vector_side_effect_keeps_collection_locked_against_reindex_submit(
    store, monkeypatch
) -> None:
    """The canonical mutation ends only after its vector write finishes."""
    collection = await store.create_collection(
        name="C",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    vector_started = asyncio.Event()
    release_vector = asyncio.Event()
    real_upsert = store._vectors.upsert

    async def blocking_upsert(**kwargs):
        vector_started.set()
        await release_vector.wait()
        return await real_upsert(**kwargs)

    monkeypatch.setattr(store._vectors, "upsert", blocking_upsert)
    mutation = asyncio.create_task(
        store.add_document(
            collection_id=collection.id,
            title="D",
            text="canonical",
            metadata={},
            chunks=["canonical"],
            embeddings=[[1.0, 0.0]],
            actor_user_id=OWNER_USER_ID,
        )
    )
    await vector_started.wait()
    try:
        # Reindex submission locks this exact row. NOWAIT turns the expected
        # block into a deterministic assertion without sleeps or timeouts.
        with pytest.raises(DBAPIError):
            async with store._session() as session:
                await session.execute(
                    select(knowledge_collections.c.id)
                    .where(knowledge_collections.c.id == collection.id)
                    .with_for_update(nowait=True)
                )
    finally:
        release_vector.set()
    await mutation


@pytest.mark.asyncio
async def test_reconcile_deletes_orphan_vectors_keeps_canonical(store) -> None:
    """The reverse reconcile sweep removes vectors whose canonical Postgres
    document is gone (non-atomic cross-store delete residue) and leaves the
    documents that still exist untouched."""
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id, title="D", text="t",
        metadata={}, chunks=["one"], embeddings=[[1.0, 0.0]],
        actor_user_id=OWNER_USER_ID,
    )
    # Strand vectors for a document that has NO canonical Postgres row,
    # exactly the drift a crash between the PG commit and the vector delete
    # leaves behind.
    await store._vectors.upsert(
        embedding_model="m", collection_id="kc_ghost", document_id="kd_ghost",
        vectors=[ChunkVector(chunk_id="kch_ghost", dense=(0.0, 1.0), text="x")],
    )
    before = await store._vectors.scroll_chunk_groups(embedding_model="m")
    assert ("kc_ghost", "kd_ghost") in before
    assert (collection.id, document.id) in before

    report = await store.reconcile_orphans()
    assert report["deleted_documents"] == 1
    assert report["details"] == [
        {"document_id": "kd_ghost", "embedding_model": "m"}
    ]

    after = await store._vectors.scroll_chunk_groups(embedding_model="m")
    assert ("kc_ghost", "kd_ghost") not in after  # orphan swept
    assert (collection.id, document.id) in after  # canonical untouched


@pytest.mark.asyncio
async def test_reconcile_deletes_orphan_chunk_of_live_document(store) -> None:
    """Point-level reconciliation catches stale chunks even when their
    document group still has a canonical row."""

    collection = await store.create_collection(
        name="C",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text="canonical",
        metadata={},
        chunks=["canonical"],
        embeddings=[[1.0, 0.0]],
        source_chunks=["canonical"],
        actor_user_id=OWNER_USER_ID,
    )
    await store._vectors.upsert(
        embedding_model="m",
        collection_id=collection.id,
        document_id=document.id,
        vectors=[
            ChunkVector(
                chunk_id="kch_stale_live_document",
                dense=(0.0, 1.0),
                text="stale",
                generation_id=collection.active_generation_id,
                revision_id=None,
            )
        ],
    )

    report = await store.reconcile_orphans()

    assert report["deleted_documents"] == 0
    assert report["deleted_chunks"] == 1
    assert report["chunk_details"] == [
        {
            "chunk_id": "kch_stale_live_document",
            "document_id": document.id,
            "collection_id": collection.id,
            "embedding_model": "m",
        }
    ]
    assert await store._vectors.count_chunks(
        embedding_model="m", chunk_ids=["kch_stale_live_document"]
    ) == 0
    assert await store._vectors.count_document(
        embedding_model="m", document_id=document.id
    ) == 1


@pytest.mark.asyncio
async def test_reconcile_waits_for_uncommitted_vector_mutation_and_keeps_point(
    store, monkeypatch
) -> None:
    collection = await store.create_collection(
        name="C",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    vector_written = asyncio.Event()
    release_mutation = asyncio.Event()
    scroll_completed = asyncio.Event()
    real_upsert = store._vectors.upsert
    real_scroll = store._vectors.scroll_chunk_points

    async def write_then_block(**kwargs):
        result = await real_upsert(**kwargs)
        vector_written.set()
        await release_mutation.wait()
        return result

    async def observed_scroll(**kwargs):
        result = await real_scroll(**kwargs)
        scroll_completed.set()
        return result

    monkeypatch.setattr(store._vectors, "upsert", write_then_block)
    monkeypatch.setattr(store._vectors, "scroll_chunk_points", observed_scroll)
    mutation = asyncio.create_task(
        store.add_document(
            collection_id=collection.id,
            title="D",
            text="canonical",
            metadata={},
            chunks=["canonical"],
            embeddings=[[1.0, 0.0]],
            source_chunks=["canonical"],
            actor_user_id=OWNER_USER_ID,
        )
    )
    await vector_written.wait()
    reconcile = asyncio.create_task(store.reconcile_orphans())
    await scroll_completed.wait()
    # The reconcile has observed the Qdrant point, but it must wait for the
    # exact collection mutation lock before deciding whether the point is an
    # orphan. Give the task one scheduling turn to reach that lock request.
    await asyncio.sleep(0)
    assert not reconcile.done()

    release_mutation.set()
    document = await mutation
    report = await reconcile

    assert report["deleted_chunks"] == 0
    assert await store._vectors.count_document(
        embedding_model="m", document_id=document.id
    ) == 1


async def _stage_validated_generation(
    store: PostgresKnowledgeStore,
    *,
    collection,
    document,
    generation_id: str,
) -> tuple[dict[str, str], GenerationBuildValidation, list[str]]:
    canonical = document.text
    revision_id = document.active_revision_id or ""
    manifest = {document.id: revision_id}
    source_span = (0, len(canonical.encode("utf-8")))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    validation = GenerationBuildValidation(
        embedding_dim=collection.embedding_dim,
        documents={
            document.id: GenerationDocumentValidation(
                revision_id=revision_id,
                content_hash=content_hash,
                source_spans=(source_span,),
            )
        },
    )
    await store.begin_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash="test-contract",
        manifest=manifest,
        actor_user_id=OWNER_USER_ID,
    )
    await store.reembed_document(
        document_id=document.id,
        chunks=[canonical],
        embeddings=[[0.0, 1.0]],
        source_chunks=[canonical],
        source_spans=[source_span],
        document_content_hash=content_hash,
        revision_id=revision_id,
        generation_id=generation_id,
        actor_user_id=OWNER_USER_ID,
    )
    async with store._session() as session:
        chunk_ids = [
            row.id
            for row in (
                await session.execute(
                    select(knowledge_chunks.c.id).where(
                        knowledge_chunks.c.generation_id == generation_id
                    )
                )
            ).all()
        ]
    return manifest, validation, chunk_ids


@pytest.mark.asyncio
async def test_generation_publish_rejects_missing_vector_point(store) -> None:
    collection = await store.create_collection(
        name="Validated",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    canonical = "Äußerst genaue Quelle"
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text=canonical,
        metadata={},
        chunks=[canonical],
        embeddings=[[1.0, 0.0]],
        source_chunks=[canonical],
        source_spans=[(0, len(canonical.encode("utf-8")))],
        document_content_hash=hashlib.sha256(
            canonical.encode("utf-8")
        ).hexdigest(),
        actor_user_id=OWNER_USER_ID,
    )
    prior_generation_id = (
        await store.get_collection(collection.id)
    ).active_generation_id
    manifest, validation, chunk_ids = await _stage_validated_generation(
        store,
        collection=collection,
        document=document,
        generation_id="gen_missing_point",
    )
    await store._vectors.delete_chunks(
        embedding_model=collection.embedding_model,
        chunk_ids=chunk_ids,
    )

    with pytest.raises(GenerationValidationError):
        await store.activate_generation(
            collection_id=collection.id,
            generation_id="gen_missing_point",
            expected_document_ids=[document.id],
            expected_manifest=manifest,
            expected_validation=validation,
            build_contract_hash="test-contract",
            actor_user_id=OWNER_USER_ID,
        )

    assert (
        await store.get_collection(collection.id)
    ).active_generation_id == prior_generation_id


@pytest.mark.asyncio
async def test_shadow_retry_replaces_unknown_vector_orphans_deterministically(
    store,
) -> None:
    collection = await store.create_collection(
        name="Retry",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    canonical = "retry source"
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
        revision_id="rev_retry_scope",
        actor_user_id=OWNER_USER_ID,
    )
    generation_id = "gen_retry_scope"
    await store.begin_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash="retry-contract",
        manifest={document.id: "rev_retry_scope"},
        actor_user_id=OWNER_USER_ID,
    )

    async def rebuild() -> str:
        await store.reembed_document(
            document_id=document.id,
            chunks=[canonical],
            embeddings=[[0.0, 1.0]],
            source_chunks=[canonical],
            source_spans=[(0, len(canonical.encode("utf-8")))],
            document_content_hash=content_hash,
            revision_id="rev_retry_scope",
            generation_id=generation_id,
            actor_user_id=OWNER_USER_ID,
        )
        async with store._session() as session:
            return str(
                await session.scalar(
                    select(knowledge_chunks.c.id).where(
                        knowledge_chunks.c.document_id == document.id,
                        knowledge_chunks.c.generation_id == generation_id,
                    )
                )
            )

    first_id = await rebuild()
    await store._vectors.upsert(
        embedding_model="m",
        collection_id=collection.id,
        document_id=document.id,
        vectors=[
            ChunkVector(
                chunk_id="kch_unknown_rolled_back_write",
                dense=(0.5, 0.5),
                generation_id=generation_id,
                revision_id="rev_retry_scope",
            )
        ],
    )
    assert await store._vectors.count_generation_document(
        embedding_model="m",
        collection_id=collection.id,
        generation_id=generation_id,
        document_id=document.id,
    ) == 2

    second_id = await rebuild()

    assert second_id == first_id
    assert await store._vectors.count_generation_document(
        embedding_model="m",
        collection_id=collection.id,
        generation_id=generation_id,
        document_id=document.id,
    ) == 1
    assert await store._vectors.count_chunks(
        embedding_model="m",
        chunk_ids=["kch_unknown_rolled_back_write"],
    ) == 0
    # Building a shadow generation must not project its derived count early.
    assert (await store.get_document(document.id)).chunk_count == 1


@pytest.mark.asyncio
async def test_generation_vector_validation_does_not_hold_pointer_cas_lock(
    store, monkeypatch
) -> None:
    collection = await store.create_collection(
        name="Outside lock",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    canonical = "validated"
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text=canonical,
        metadata={},
        chunks=[canonical],
        embeddings=[[1.0, 0.0]],
        source_chunks=[canonical],
        source_spans=[(0, len(canonical.encode("utf-8")))],
        document_content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        actor_user_id=OWNER_USER_ID,
    )
    manifest, validation, _ = await _stage_validated_generation(
        store,
        collection=collection,
        document=document,
        generation_id="gen_external_validation",
    )
    validation_started = asyncio.Event()
    release_validation = asyncio.Event()
    real_count = store._vectors.count_generation

    async def blocking_count(**kwargs):
        validation_started.set()
        await release_validation.wait()
        return await real_count(**kwargs)

    monkeypatch.setattr(store._vectors, "count_generation", blocking_count)
    activation = asyncio.create_task(
        store.activate_generation(
            collection_id=collection.id,
            generation_id="gen_external_validation",
            expected_document_ids=[document.id],
            expected_manifest=manifest,
            expected_validation=validation,
            build_contract_hash="test-contract",
            actor_user_id=OWNER_USER_ID,
        )
    )
    await validation_started.wait()
    try:
        async with store._session() as session:
            locked = await session.scalar(
                select(knowledge_collections.c.id)
                .where(knowledge_collections.c.id == collection.id)
                .with_for_update(nowait=True)
            )
            assert locked == collection.id
    finally:
        release_validation.set()
    published = await activation
    assert published.active_generation_id == "gen_external_validation"


@pytest.mark.asyncio
async def test_reset_and_discard_remove_complete_generation_scope(store) -> None:
    collection = await store.create_collection(
        name="Cleanup",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    canonical = "cleanup"
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text=canonical,
        metadata={},
        chunks=[canonical],
        embeddings=[[1.0, 0.0]],
        source_chunks=[canonical],
        source_spans=[(0, len(canonical.encode("utf-8")))],
        document_content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        actor_user_id=OWNER_USER_ID,
    )
    generation_id = "gen_cleanup_complete_scope"
    manifest, _validation, _ = await _stage_validated_generation(
        store,
        collection=collection,
        document=document,
        generation_id=generation_id,
    )

    async def add_unknown(chunk_id: str) -> None:
        await store._vectors.upsert(
            embedding_model="m",
            collection_id=collection.id,
            document_id=document.id,
            vectors=[
                ChunkVector(
                    chunk_id=chunk_id,
                    dense=(0.5, 0.5),
                    generation_id=generation_id,
                    revision_id=document.active_revision_id,
                )
            ],
        )

    await add_unknown("kch_unknown_before_reset")
    removed = await store.reset_generation_for_raw_choice(
        collection_id=collection.id,
        generation_id=generation_id,
        build_contract_hash="raw-contract",
        manifest=manifest,
    )
    assert removed == 1
    assert await store._vectors.count_generation(
        embedding_model="m",
        collection_id=collection.id,
        generation_id=generation_id,
    ) == 0

    await add_unknown("kch_unknown_before_discard")
    assert await store.discard_generation(
        collection_id=collection.id,
        generation_id=generation_id,
        actor_user_id=OWNER_USER_ID,
    ) == 0
    assert await store._vectors.count_generation(
        embedding_model="m",
        collection_id=collection.id,
        generation_id=generation_id,
    ) == 0


@pytest.mark.asyncio
async def test_generation_cleanup_failure_is_not_rollback_available(
    store, monkeypatch
) -> None:
    collection = await store.create_collection(
        name="Retention",
        embedding_model="m",
        embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    canonical = "canonical"
    document = await store.add_document(
        collection_id=collection.id,
        title="D",
        text=canonical,
        metadata={},
        chunks=[canonical],
        embeddings=[[1.0, 0.0]],
        source_chunks=[canonical],
        source_spans=[(0, len(canonical))],
        document_content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
        actor_user_id=OWNER_USER_ID,
    )
    first_manifest, first_validation, first_chunk_ids = (
        await _stage_validated_generation(
            store,
            collection=collection,
            document=document,
            generation_id="gen_retained",
        )
    )
    await store.activate_generation(
        collection_id=collection.id,
        generation_id="gen_retained",
        expected_document_ids=[document.id],
        expected_manifest=first_manifest,
        expected_validation=first_validation,
        build_contract_hash="test-contract",
        rollback_retention_seconds=3_600,
        actor_user_id=OWNER_USER_ID,
    )
    document = await store.get_document(document.id)
    second_manifest, second_validation, _ = await _stage_validated_generation(
        store,
        collection=collection,
        document=document,
        generation_id="gen_active",
    )
    await store.activate_generation(
        collection_id=collection.id,
        generation_id="gen_active",
        expected_document_ids=[document.id],
        expected_manifest=second_manifest,
        expected_validation=second_validation,
        build_contract_hash="test-contract",
        rollback_retention_seconds=0,
        actor_user_id=OWNER_USER_ID,
    )

    real_delete = store._vectors.delete_generation

    async def fail_delete(**_kwargs):
        raise RuntimeError("vector dependency unavailable")

    monkeypatch.setattr(store._vectors, "delete_generation", fail_delete)
    with pytest.raises(GenerationPruneError):
        await store.prune_expired_generations(collection_id=collection.id)

    async with store._session() as session:
        failed_status = await session.scalar(
            select(knowledge_index_generations.c.status).where(
                knowledge_index_generations.c.generation_id == "gen_retained"
            )
        )
    assert failed_status == "cleanup_failed"
    assert await store.generation_cleanup_collection_ids() == [collection.id]
    assert (
        await store.get_collection(collection.id)
    ).active_generation_id == "gen_active"

    # Reproduce a pre-lineage Qdrant payload for a canonical chunk in the
    # expired generation.  Generation-scoped deletion cannot see this point;
    # cleanup must also consume the exact canonical chunk manifest.
    await store._vectors.upsert(
        embedding_model=collection.embedding_model,
        collection_id=collection.id,
        document_id=document.id,
        vectors=[
            ChunkVector(
                chunk_id=first_chunk_ids[0],
                dense=(1.0, 0.0),
                text=canonical,
            )
        ],
    )

    monkeypatch.setattr(store._vectors, "delete_generation", real_delete)
    assert await store.prune_expired_generations(
        collection_id=collection.id
    ) == len(first_chunk_ids)
    async with store._session() as session:
        final_status = await session.scalar(
            select(knowledge_index_generations.c.status).where(
                knowledge_index_generations.c.generation_id == "gen_retained"
            )
        )
        residual_chunks = await session.scalar(
            select(func.count()).select_from(knowledge_chunks).where(
                knowledge_chunks.c.generation_id == "gen_retained"
            )
        )
    assert final_status == "deleted"
    assert residual_chunks == 0
    assert await store._vectors.count_chunks(
        embedding_model=collection.embedding_model,
        chunk_ids=first_chunk_ids,
    ) == 0


@pytest.mark.asyncio
async def test_error_paths(store) -> None:
    with pytest.raises(CollectionNotFound):
        await store.get_collection("kc_missing")
    with pytest.raises(DocumentNotFound):
        await store.get_document("kd_missing")
    collection = await store.create_collection(
        name="C", embedding_model="m", embedding_dim=2,
        created_by_user_id=OWNER_USER_ID,
    )
    with pytest.raises(EmbeddingDimensionMismatch):
        await store.add_document(
            collection_id=collection.id, title="D", text="t", metadata={},
            chunks=["one"], embeddings=[[1.0, 0.0, 0.0]],  # wrong dim
            actor_user_id=OWNER_USER_ID,
        )
    with pytest.raises(EmbeddingDimensionMismatch):
        await store.add_document(
            collection_id=collection.id, title="D", text="t", metadata={},
            chunks=["one", "two"], embeddings=[[1.0, 0.0]],  # count mismatch
            actor_user_id=OWNER_USER_ID,
        )
