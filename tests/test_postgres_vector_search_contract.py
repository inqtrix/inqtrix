"""Offline regressions for bounded canonical vector hydration."""

from __future__ import annotations

import logging

import pytest

from inqtrix.knowledge.stores.postgres_store import PostgresKnowledgeStore
from inqtrix.knowledge.stores.vector_index import VectorHit, VectorSearchScope


class _FullVectorPage:
    supports_hybrid = True

    def __init__(self) -> None:
        self.depths: list[int] = []

    async def search(self, *, top_k: int, **_kwargs):
        self.depths.append(top_k)
        return [
            VectorHit(chunk_id=f"kch_{index}", score=1.0)
            for index in range(top_k)
        ]

    async def hybrid_search(self, *, top_k: int, **_kwargs):
        return await self.search(top_k=top_k)


class _SnapshotVectorPage:
    supports_hybrid = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def search(self, *, scopes, **_kwargs):
        generation = scopes[0].generation_id
        self.calls.append(("dense", generation))
        return [VectorHit(chunk_id=f"kch_{generation}", score=1.0)]

    async def hybrid_search(self, *, scopes, **_kwargs):
        generation = scopes[0].generation_id
        self.calls.append(("hybrid", generation))
        return [VectorHit(chunk_id=f"kch_{generation}", score=1.0)]


class _StalledVectorPage:
    supports_hybrid = True

    def __init__(self) -> None:
        self.depths: list[int] = []

    async def search(self, *, top_k: int, **_kwargs):
        self.depths.append(top_k)
        return [
            VectorHit(chunk_id="kch_active", score=1.0),
            VectorHit(chunk_id="kch_stale", score=0.9),
        ]

    async def hybrid_search(self, *, top_k: int, **_kwargs):
        return await self.search(top_k=top_k)


@pytest.mark.asyncio
async def test_dense_overfetch_is_bounded_and_reports_degradation(caplog) -> None:
    store = object.__new__(PostgresKnowledgeStore)
    vectors = _FullVectorPage()
    store._vectors = vectors

    async def resolve_scope(_embedding_model, _collection_ids):
        return "m", [
            VectorSearchScope(
                collection_id="kc",
                generation_id="gen_active",
                active_revision_ids=("rev_active",),
            )
        ]

    active_candidate = object()

    async def hydrate(_hits, *, scopes):
        # Everything except this canonical result is rejected by hydration.
        assert scopes[0].generation_id == "gen_active"
        return [active_candidate]

    store._resolve_scope = resolve_scope
    store._hydrate = hydrate

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        result = await store.search(
            query_embedding=[1.0],
            collection_ids=["kc"],
            top_k=100,
            embedding_model="m",
        )

    assert result == [active_candidate]
    assert len(result.degradations) == 1
    degradation = result.degradations[0]
    assert degradation.reason == "vector_overfetch_cap"
    assert degradation.retrieval_mode == "dense"
    assert degradation.stage == "vector_candidate_pool"
    assert degradation.requested_candidate_pool == 100
    assert degradation.returned_candidate_pool == 1
    assert degradation.final_top_k == 100
    assert degradation.final_evidence_complete is False
    assert degradation.requested_top_k == 100
    assert degradation.returned_hits == 1
    assert degradation.candidate_cap == 512
    assert vectors.depths == [100, 200, 400, 512]
    assert "vector_overfetch_cap=512" in caplog.text
    assert "active_verified=1" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval_mode", ["dense", "hybrid"])
async def test_repeated_vector_page_reports_stall_before_short_page_exhaustion(
    retrieval_mode: str,
) -> None:
    store = object.__new__(PostgresKnowledgeStore)
    vectors = _StalledVectorPage()
    store._vectors = vectors
    scope = VectorSearchScope(
        collection_id="kc",
        generation_id="gen_active",
        active_revision_ids=("rev_active",),
    )

    async def resolve_scope(_embedding_model, _collection_ids):
        return "m", [scope]

    active_candidate = object()

    async def hydrate(_hits, *, scopes):
        assert scopes == [scope]
        return [active_candidate]

    store._resolve_scope = resolve_scope
    store._hydrate = hydrate

    if retrieval_mode == "dense":
        result = await store.search(
            query_embedding=[1.0],
            collection_ids=["kc"],
            top_k=2,
            embedding_model="m",
        )
    else:
        result = await store.hybrid_search(
            query_text="query",
            query_embedding=[1.0],
            collection_ids=["kc"],
            top_k=2,
            embedding_model="m",
        )

    assert result == [active_candidate]
    assert vectors.depths == [2, 4]
    assert result.degradations[0].reason == "vector_candidate_stalled"
    assert result.degradations[0].requested_candidate_pool == 2
    assert result.degradations[0].returned_candidate_pool == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval_mode", ["dense", "hybrid"])
async def test_unchanged_snapshot_is_refreshed_only_once(
    retrieval_mode: str,
) -> None:
    """Canonical rejection may overfetch, but cannot re-query SQL per page."""

    store = object.__new__(PostgresKnowledgeStore)
    vectors = _FullVectorPage()
    store._vectors = vectors
    scope = VectorSearchScope(
        collection_id="kc",
        generation_id="gen_active",
        active_revision_ids=("rev_active",),
    )
    resolved = 0

    async def resolve_scope(_embedding_model, _collection_ids):
        nonlocal resolved
        resolved += 1
        return "m", [scope]

    active_candidate = object()

    async def hydrate(_hits, *, scopes):
        assert scopes == [scope]
        return [active_candidate]

    store._resolve_scope = resolve_scope
    store._hydrate = hydrate

    if retrieval_mode == "dense":
        result = await store.search(
            query_embedding=[1.0],
            collection_ids=["kc"],
            top_k=100,
            embedding_model="m",
        )
    else:
        result = await store.hybrid_search(
            query_text="query",
            query_embedding=[1.0],
            collection_ids=["kc"],
            top_k=100,
            embedding_model="m",
        )

    assert result == [active_candidate]
    assert vectors.depths == [100, 200, 400, 512]
    assert resolved == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("retrieval_mode", ["dense", "hybrid"])
async def test_snapshot_advance_retries_whole_query_once_without_mixing(
    retrieval_mode: str,
    caplog,
) -> None:
    store = object.__new__(PostgresKnowledgeStore)
    vectors = _SnapshotVectorPage()
    store._vectors = vectors
    old_scope = VectorSearchScope(
        collection_id="kc",
        generation_id="gen_old",
        active_revision_ids=("rev_old",),
    )
    new_scope = VectorSearchScope(
        collection_id="kc",
        generation_id="gen_new",
        active_revision_ids=("rev_new",),
    )
    resolved = 0

    async def resolve_scope(_embedding_model, _collection_ids):
        nonlocal resolved
        resolved += 1
        return ("m", [old_scope] if resolved == 1 else [new_scope])

    old_candidate = object()
    new_candidate = object()

    async def hydrate(_hits, *, scopes):
        if scopes == [old_scope]:
            # The publish CAS removed old chunk rows between vector ranking and
            # canonical hydration.  A partial/empty old answer is forbidden.
            return []
        assert scopes == [new_scope]
        return [new_candidate]

    store._resolve_scope = resolve_scope
    store._hydrate = hydrate

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        if retrieval_mode == "dense":
            result = await store.search(
                query_embedding=[1.0],
                collection_ids=["kc"],
                top_k=1,
                embedding_model="m",
            )
        else:
            result = await store.hybrid_search(
                query_text="query",
                query_embedding=[1.0],
                collection_ids=["kc"],
                top_k=1,
                embedding_model="m",
            )

    assert result == [new_candidate]
    assert old_candidate not in result
    assert vectors.calls == [
        (retrieval_mode, "gen_old"),
        (retrieval_mode, "gen_new"),
    ]
    assert resolved == 2
    assert "snapshot_advanced_retry=1" in caplog.text
