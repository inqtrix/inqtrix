"""Unit tests for the in-process vector index (offline, no infra).

Covers the behaviour the Postgres-canonical store relies on: dense
ranking, collection + embedding-model scoping, deletion, top_k capping,
and the dense-only hybrid guard.
"""

from __future__ import annotations

import pytest

from inqtrix.knowledge.stores.vector_index import (
    ChunkVector,
    MemoryVectorIndex,
    VectorHit,
    VectorSearchScope,
)


def _cv(chunk_id: str, dense: list[float]) -> ChunkVector:
    return ChunkVector(chunk_id=chunk_id, dense=tuple(dense), text=chunk_id)


def _legacy_scope(collection_id: str, *document_ids: str) -> VectorSearchScope:
    return VectorSearchScope(
        collection_id=collection_id,
        generation_id=None,
        legacy_document_ids=tuple(document_ids),
    )


@pytest.mark.asyncio
async def test_search_ranks_by_cosine_within_scope() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m",
        collection_id="c1",
        document_id="d1",
        vectors=[_cv("a", [1.0, 0.0]), _cv("b", [0.0, 1.0])],
    )
    hits = await index.search(
        embedding_model="m",
        query_embedding=[1.0, 0.0],
        scopes=[_legacy_scope("c1", "d1")],
        top_k=2,
    )
    assert [hit.chunk_id for hit in hits] == ["a", "b"]
    assert hits[0].score > hits[1].score


@pytest.mark.asyncio
async def test_search_scopes_by_collection_and_model() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m", collection_id="c1", document_id="d1",
        vectors=[_cv("a", [1.0, 0.0])],
    )
    await index.upsert(
        embedding_model="m", collection_id="c2", document_id="d2",
        vectors=[_cv("b", [1.0, 0.0])],
    )
    await index.upsert(
        embedding_model="other", collection_id="c1", document_id="d3",
        vectors=[_cv("c", [1.0, 0.0])],
    )
    hits = await index.search(
        embedding_model="m", query_embedding=[1.0, 0.0],
        scopes=[_legacy_scope("c1", "d1", "d3")], top_k=10,
    )
    # Only the c1 + model "m" chunk; c2 (other collection) and the
    # "other"-model chunk are out of scope.
    assert [hit.chunk_id for hit in hits] == ["a"]


@pytest.mark.asyncio
async def test_top_k_caps_results() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m", collection_id="c1", document_id="d1",
        vectors=[_cv(str(i), [float(i), 1.0]) for i in range(5)],
    )
    hits = await index.search(
        embedding_model="m", query_embedding=[1.0, 1.0],
        scopes=[_legacy_scope("c1", "d1")], top_k=2,
    )
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_delete_document_and_collection() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m", collection_id="c1", document_id="d1",
        vectors=[_cv("a", [1.0, 0.0])],
    )
    await index.upsert(
        embedding_model="m", collection_id="c1", document_id="d2",
        vectors=[_cv("b", [0.0, 1.0])],
    )
    await index.delete_document(embedding_model="m", document_id="d1")
    hits = await index.search(
        embedding_model="m", query_embedding=[1.0, 0.0],
        scopes=[_legacy_scope("c1", "d1", "d2")], top_k=10,
    )
    assert {hit.chunk_id for hit in hits} == {"b"}
    await index.delete_collection(embedding_model="m", collection_id="c1")
    hits = await index.search(
        embedding_model="m", query_embedding=[1.0, 0.0],
        scopes=[_legacy_scope("c1", "d1", "d2")], top_k=10,
    )
    assert hits == []


@pytest.mark.asyncio
async def test_hybrid_search_is_unsupported() -> None:
    index = MemoryVectorIndex()
    assert index.supports_hybrid is False
    with pytest.raises(NotImplementedError):
        await index.hybrid_search(
            embedding_model="m", query_text="q", query_embedding=[1.0],
            scopes=[_legacy_scope("c1", "d1")], top_k=1,
        )


@pytest.mark.asyncio
async def test_generation_and_revision_filter_before_ranking() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m",
        collection_id="c1",
        document_id="d1",
        vectors=[
            ChunkVector(
                "active",
                (0.8, 0.2),
                generation_id="gen_active",
                revision_id="rev_active",
            ),
            ChunkVector(
                "staged_generation",
                (1.0, 0.0),
                generation_id="gen_staged",
                revision_id="rev_active",
            ),
            ChunkVector(
                "staged_revision",
                (1.0, 0.0),
                generation_id="gen_active",
                revision_id="rev_staged",
            ),
        ],
    )
    hits = await index.search(
        embedding_model="m",
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="c1",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
            )
        ],
        top_k=3,
    )
    assert [hit.chunk_id for hit in hits] == ["active"]


@pytest.mark.asyncio
async def test_generation_count_and_delete_are_exactly_scoped() -> None:
    index = MemoryVectorIndex()
    for collection_id, chunk_id, generation_id in (
        ("c1", "target", "gen_target"),
        ("c1", "other_generation", "gen_other"),
        ("c2", "other_collection", "gen_target"),
    ):
        await index.upsert(
            embedding_model="m",
            collection_id=collection_id,
            document_id=f"d_{chunk_id}",
            vectors=[
                ChunkVector(
                    chunk_id,
                    (1.0, 0.0),
                    generation_id=generation_id,
                    revision_id="rev",
                )
            ],
        )

    assert await index.count_generation(
        embedding_model="m",
        collection_id="c1",
        generation_id="gen_target",
    ) == 1
    await index.delete_generation(
        embedding_model="m",
        collection_id="c1",
        generation_id="gen_target",
    )
    assert await index.count_generation(
        embedding_model="m",
        collection_id="c1",
        generation_id="gen_target",
    ) == 0
    assert await index.count_collection(
        embedding_model="m", collection_id="c1"
    ) == 1
    assert await index.count_collection(
        embedding_model="m", collection_id="c2"
    ) == 1


@pytest.mark.asyncio
async def test_legacy_none_scope_is_exact_not_a_wildcard() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m",
        collection_id="legacy",
        document_id="legacy_doc",
        vectors=[ChunkVector("legacy_point", (1.0, 0.0))],
    )
    await index.upsert(
        embedding_model="m",
        collection_id="modern",
        document_id="modern_doc",
        vectors=[
            ChunkVector(
                "modern_point",
                (0.8, 0.2),
                generation_id="gen_modern",
                revision_id="rev_modern",
            ),
            ChunkVector("missing_modern_scope", (1.0, 0.0)),
        ],
    )
    hits = await index.search(
        embedding_model="m",
        query_embedding=[1.0, 0.0],
        scopes=[
            _legacy_scope("legacy", "legacy_doc"),
            VectorSearchScope(
                collection_id="modern",
                generation_id="gen_modern",
                active_revision_ids=("rev_modern",),
            ),
        ],
        top_k=5,
    )
    assert {hit.chunk_id for hit in hits} == {"legacy_point", "modern_point"}


@pytest.mark.asyncio
async def test_migrated_payload_compatibility_is_chunk_exact() -> None:
    index = MemoryVectorIndex()
    await index.upsert(
        embedding_model="m",
        collection_id="modern",
        document_id="migrated_doc",
        vectors=[ChunkVector("migrated_point", (0.9, 0.1))],
    )
    await index.upsert(
        embedding_model="m",
        collection_id="modern",
        document_id="migrated_doc",
        vectors=[ChunkVector("same_document_unverified", (0.95, 0.05))],
    )
    await index.upsert(
        embedding_model="m",
        collection_id="modern",
        document_id="unrelated_doc",
        vectors=[ChunkVector("unrelated_missing_lineage", (1.0, 0.0))],
    )
    await index.upsert(
        embedding_model="m",
        collection_id="modern",
        document_id="active_doc",
        vectors=[
            ChunkVector(
                "active_point",
                (0.8, 0.2),
                generation_id="gen_modern",
                revision_id="rev_modern",
            )
        ],
    )

    hits = await index.search(
        embedding_model="m",
        query_embedding=[1.0, 0.0],
        scopes=[
            VectorSearchScope(
                collection_id="modern",
                generation_id="gen_modern",
                active_revision_ids=("rev_modern", "rev_migrated"),
                legacy_payload_chunk_ids=("migrated_point",),
            )
        ],
        top_k=5,
    )

    assert {hit.chunk_id for hit in hits} == {"active_point", "migrated_point"}


def test_vector_hit_shape() -> None:
    hit = VectorHit(chunk_id="kch_x", score=0.5)
    assert hit.chunk_id == "kch_x"
    assert hit.score == 0.5
