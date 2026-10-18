"""Offline contract tests for bounded legacy Qdrant-only retrieval."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.knowledge.stores.ports import DocumentChunk, RetrievalCandidate
from inqtrix.knowledge.stores.qdrant_store import QdrantKnowledgeStore
from inqtrix.knowledge.stores.retrieval_contract import (
    MAX_VECTOR_CANDIDATES,
)


def _candidate(index: int = 0) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk=DocumentChunk(
            id=f"kch_{index}",
            document_id="kd",
            collection_id="kc",
            chunk_index=index,
            text=f"evidence {index}",
            source_text=f"evidence {index}",
            source_verified=True,
        ),
        score=1.0,
        document_title="Document",
    )


class _Point:
    def __init__(self, index: int, *, active: bool) -> None:
        self.id = f"point-{index}"
        self.active = active
        self.candidate = _candidate(index)


class _Client:
    def __init__(self, *, stalled: bool = False, exhausted: bool = False) -> None:
        self.stalled = stalled
        self.exhausted = exhausted
        self.depths: list[int] = []
        self.query_kwargs: list[dict] = []

    def query_points(self, *, limit: int, **kwargs):
        self.depths.append(limit)
        self.query_kwargs.append(kwargs)
        if self.exhausted:
            count = min(2, limit)
        elif self.stalled:
            count = 2
        else:
            count = limit
        return SimpleNamespace(
            points=[_Point(index, active=index == 0) for index in range(count)]
        )


def _store(
    *,
    stalled: bool = False,
    exhausted: bool = False,
    candidate_cap: int = MAX_VECTOR_CANDIDATES,
) -> QdrantKnowledgeStore:
    store = object.__new__(QdrantKnowledgeStore)
    store._client = _Client(stalled=stalled, exhausted=exhausted)
    store._vector_candidate_cap = candidate_cap
    store._resolve_target = lambda _collection_ids, _embedding_model: (
        "chunks",
        object(),
    )
    store._candidates_from_points = lambda points: [
        point.candidate for point in points if point.active
    ]
    store._sparse_enabled = True
    store._bm25 = SimpleNamespace(query=lambda _query: _SPARSE_QUERY_SENTINEL)
    return store


# The sparse query object (production: a BM25 inference Document) must reach
# the wire VERBATIM — the store passes it through without re-wrapping.
_SPARSE_QUERY_SENTINEL = SimpleNamespace(kind="sparse-query-sentinel")


def _hybrid_models():
    return SimpleNamespace(
        Prefetch=lambda **kwargs: kwargs,
        FusionQuery=lambda **kwargs: kwargs,
        Fusion=SimpleNamespace(RRF="rrf"),
    )


@pytest.mark.parametrize("candidate_cap", [0, 513, True])
def test_candidate_cap_is_validated_before_qdrant_client_creation(
    candidate_cap,
) -> None:
    with pytest.raises(ValueError, match="vector_candidate_cap"):
        QdrantKnowledgeStore(
            url="http://unused.invalid",
            vector_candidate_cap=candidate_cap,
        )


def test_valid_candidate_cap_is_bound_to_the_store(monkeypatch) -> None:
    import inqtrix.knowledge.stores.qdrant_store as module

    class Client:
        def __init__(self, **_kwargs) -> None:
            pass

        def info(self):
            # Deliberately exercise the BM25 gate's pass branch instead of
            # falling into the version-undeterminable warn branch.
            return SimpleNamespace(version="1.19.0")

    monkeypatch.setattr(module, "_require_qdrant", lambda: (Client, object()))

    store = QdrantKnowledgeStore(
        url="http://unused.invalid",
        api_key="test-only",
        vector_candidate_cap=128,
    )

    assert store._vector_candidate_cap == 128


def _search(store, retrieval_mode: str, monkeypatch):
    if retrieval_mode == "dense":
        return store._sync_search([1.0], ["kc"], 100, "m")
    import inqtrix.knowledge.stores.qdrant_store as module

    monkeypatch.setattr(
        module,
        "_require_qdrant",
        lambda: (None, _hybrid_models()),
    )
    return store._sync_hybrid_search("query", [1.0], ["kc"], 100, "m")


def test_hybrid_sparse_query_is_passed_through_verbatim(monkeypatch) -> None:
    store = _store()

    _search(store, "hybrid", monkeypatch)

    prefetch = store._client.query_kwargs[0]["prefetch"]
    assert prefetch[1]["using"] == "sparse"
    assert prefetch[1]["query"] is _SPARSE_QUERY_SENTINEL


@pytest.mark.parametrize("retrieval_mode", ["dense", "hybrid"])
def test_overfetch_is_bounded_and_reports_candidate_pool_degradation(
    retrieval_mode: str,
    monkeypatch,
) -> None:
    store = _store()

    result = _search(store, retrieval_mode, monkeypatch)

    assert len(result) == 1
    assert store._client.depths == [100, 200, 400, 512]
    degradation = result.degradations[0]
    assert degradation.reason == "vector_overfetch_cap"
    assert degradation.retrieval_mode == retrieval_mode
    assert degradation.stage == "vector_candidate_pool"
    assert degradation.requested_candidate_pool == 100
    assert degradation.returned_candidate_pool == 1
    assert degradation.final_top_k == 100
    assert degradation.returned_hits == 1
    assert degradation.candidate_cap == 512


def test_configured_candidate_cap_can_only_lower_the_safe_boundary() -> None:
    store = _store(candidate_cap=128)

    result = store._sync_search([1.0], ["kc"], 100, "m")

    assert store._client.depths == [100, 128]
    assert result.degradations[0].candidate_cap == 128


@pytest.mark.parametrize("retrieval_mode", ["dense", "hybrid"])
def test_repeated_qdrant_page_reports_stall(
    retrieval_mode: str,
    monkeypatch,
) -> None:
    store = _store(stalled=True)

    if retrieval_mode == "dense":
        result = store._sync_search([1.0], ["kc"], 2, "m")
    else:
        import inqtrix.knowledge.stores.qdrant_store as module

        monkeypatch.setattr(
            module,
            "_require_qdrant",
            lambda: (None, _hybrid_models()),
        )
        result = store._sync_hybrid_search(
            "query", [1.0], ["kc"], 2, "m"
        )

    assert store._client.depths == [2, 4]
    assert result.degradations[0].reason == "vector_candidate_stalled"
    assert result.degradations[0].returned_candidate_pool == 1


def test_first_short_page_is_genuine_exhaustion_without_degradation() -> None:
    store = _store(exhausted=True)

    result = store._sync_search([1.0], ["kc"], 5, "m")

    assert store._client.depths == [5]
    assert len(result) == 1
    assert result.degradations == ()


@pytest.mark.asyncio
async def test_explicit_empty_scope_never_reaches_qdrant() -> None:
    store = _store()

    dense = await store.search(
        query_embedding=[1.0],
        collection_ids=[],
        top_k=5,
        embedding_model="m",
    )
    hybrid = await store.hybrid_search(
        query_text="query",
        query_embedding=[1.0],
        collection_ids=[],
        top_k=5,
        embedding_model="m",
    )

    assert dense == []
    assert hybrid == []
    assert store._client.depths == []
