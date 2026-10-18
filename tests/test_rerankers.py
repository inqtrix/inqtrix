"""Offline tests: rerank adapter, service rerank stage, hybrid dispatch."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from inqtrix.knowledge.retrieval import retrieve
from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import (
    KnowledgeProviderContext,
    RetrievalCandidate,
    RetrievalCandidateBatch,
    RetrievalDegradation,
)
from inqtrix.providers.base import _RetryNoticeMixin
from inqtrix.providers.rerankers import CohereRerank, RerankerError, RerankResult
from inqtrix.services.knowledge_service import KnowledgeService

from tests.test_knowledge_engine import StubEmbeddings


def make_cohere(transport: httpx.MockTransport) -> CohereRerank:
    reranker = CohereRerank(
        api_key="stub-key",
        base_url="https://rerank.example",
        default_model="rerank-v3.5",
    )
    original_post = httpx.post

    def patched_post(url, **kwargs):
        client = httpx.Client(transport=transport)
        try:
            return client.post(url, **kwargs)
        finally:
            client.close()

    httpx.post = patched_post
    reranker._restore = lambda: setattr(httpx, "post", original_post)
    return reranker


# ------------------------------------------------------------------ #
# CohereRerank adapter (mocked transport)
# ------------------------------------------------------------------ #


def test_cohere_rerank_parses_results_best_first():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        captured["auth"] = request.headers.get("authorization", "")
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.40},
                ]
            },
        )

    reranker = make_cohere(httpx.MockTransport(handler))
    try:
        results = reranker.rerank(
            "Haftung", ["a", "b", "c"], top_n=2
        )
    finally:
        reranker._restore()

    assert results == [
        RerankResult(index=2, relevance_score=0.91),
        RerankResult(index=0, relevance_score=0.40),
    ]
    assert captured["model"] == "rerank-v3.5"
    assert captured["top_n"] == 2
    assert captured["documents"] == ["a", "b", "c"]
    assert captured["auth"] == "Bearer stub-key"


def test_cohere_rerank_http_error_raises_loudly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad key"})

    reranker = make_cohere(httpx.MockTransport(handler))
    try:
        with pytest.raises(RerankerError, match="Rerank call failed"):
            reranker.rerank("q", ["a"], top_n=1)
    finally:
        reranker._restore()


def test_cohere_rerank_retries_on_429_then_succeeds():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
        )

    reranker = make_cohere(httpx.MockTransport(handler))
    try:
        results = reranker.rerank("q", ["a"], top_n=1)
    finally:
        reranker._restore()

    assert attempts["count"] == 2
    assert results[0].index == 0


def test_cohere_rerank_retry_records_notice():
    """Retries are diagnosable, not log-only (No Silent Fallbacks)."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(
            200, json={"results": [{"index": 0, "relevance_score": 0.5}]}
        )

    reranker = make_cohere(httpx.MockTransport(handler))
    try:
        reranker.rerank("q", ["a"], top_n=1)
    finally:
        reranker._restore()

    notices = reranker.consume_retry_notices()
    assert len(notices) == 1
    assert notices[0]["provider"] == "CohereRerank"
    assert notices[0]["operation"] == "rerank"
    assert notices[0]["attempt"] == 1
    assert notices[0]["max_attempts"] == 3
    assert notices[0]["status_code"] == 429
    assert reranker.consume_retry_notices() == []


def test_cohere_rerank_never_exceeds_three_total_attempts():
    attempts = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(503)

    reranker = make_cohere(httpx.MockTransport(handler))
    try:
        with pytest.raises(RerankerError):
            reranker.rerank("q", ["a"], top_n=1)
    finally:
        reranker._restore()

    assert attempts["count"] == 3


def test_cohere_rerank_verbatim_url_when_path_already_present():
    reranker = CohereRerank(
        api_key="k",
        base_url="https://x.example/providers/cohere/v2/rerank",
        default_model="m",
    )
    assert reranker._url == "https://x.example/providers/cohere/v2/rerank"


def test_cohere_rerank_empty_documents_short_circuits():
    reranker = CohereRerank(
        api_key="k", base_url="https://rerank.example", default_model="m"
    )
    assert reranker.rerank("q", [], top_n=5) == []


def test_cohere_rerank_rejects_blank_construction():
    with pytest.raises(ValueError, match="api_key"):
        CohereRerank(api_key=" ", base_url="https://x", default_model="m")
    with pytest.raises(ValueError, match="base_url"):
        CohereRerank(api_key="k", base_url="", default_model="m")
    with pytest.raises(ValueError, match="default_model"):
        CohereRerank(api_key="k", base_url="https://x", default_model="")


# ------------------------------------------------------------------ #
# Service rerank stage + hybrid dispatch (fakes)
# ------------------------------------------------------------------ #


class FakeReranker:
    """Reverses candidate order — detectable reordering."""

    default_model = "fake-rerank"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def rerank(self, query, documents, *, top_n, model=None):
        self.calls.append(
            {"query": query, "documents": list(documents), "top_n": top_n}
        )
        order = list(reversed(range(len(documents))))[:top_n]
        return [
            RerankResult(index=index, relevance_score=1.0 - rank * 0.1)
            for rank, index in enumerate(order)
        ]


def make_service(**context_overrides) -> KnowledgeService:
    context = KnowledgeProviderContext(
        embeddings=StubEmbeddings(),
        store=context_overrides.pop("store", MemoryKnowledgeStore()),
        default_top_k=4,
        **context_overrides,
    )
    return KnowledgeService(
        knowledge=context, chunk_max_chars=2_000, max_document_chars=100_000
    )


async def seed_documents(service: KnowledgeService) -> str:
    collection = await service.create_collection(name="K")
    for index in range(3):
        await service.add_document(
            collection_id=collection.id,
            title=f"Doc {index}",
            text=f"Haftung Klausel Nummer {index}.",
        )
    return collection.id


@pytest.mark.asyncio
async def test_rerank_stage_reorders_and_caps_results():
    reranker = FakeReranker()
    service = make_service(reranker=reranker, rerank_candidate_depth=10)
    collection_id = await seed_documents(service)

    hits = await service.search(
        query="Haftung", collection_ids=[collection_id], top_k=2
    )

    assert len(hits) == 2
    # The fake reverses retrieval order — proof the stage is live.
    assert reranker.calls[0]["top_n"] == 2
    assert len(reranker.calls[0]["documents"]) == 3
    assert hits[0].score == pytest.approx(1.0)


class NotifyingReranker(_RetryNoticeMixin, FakeReranker):
    """Fake that reports one retry through the shared mixin seam."""

    def rerank(self, query, documents, *, top_n, model=None):
        self._append_retry_notice({
            "provider": "FakeRerank",
            "model": "fake-rerank",
            "operation": "rerank",
            "attempt": 1,
            "max_attempts": 3,
        })
        return super().rerank(query, documents, top_n=top_n, model=model)


@pytest.mark.asyncio
async def test_retrieve_forwards_rerank_retry_notices():
    """The pipeline forwards rerank retries to the caller's observer."""
    reranker = NotifyingReranker()
    context = KnowledgeProviderContext(
        embeddings=StubEmbeddings(),
        store=MemoryKnowledgeStore(),
        default_top_k=4,
        reranker=reranker,
        rerank_candidate_depth=10,
    )
    service = KnowledgeService(
        knowledge=context, chunk_max_chars=2_000, max_document_chars=100_000
    )
    collection_id = await seed_documents(service)
    observed: list[dict[str, Any]] = []

    candidates = await retrieve(
        context,
        query="Haftung",
        collection_ids=[collection_id],
        top_k=2,
        on_provider_retry=observed.append,
    )

    assert len(candidates) == 2
    assert len(observed) == 1
    assert observed[0]["provider"] == "FakeRerank"
    # The mixin marks callback-delivered notices, proving the observer was
    # invoked live on the rerank worker thread (not merely stored).
    assert observed[0]["progress_emitted"] is True


@pytest.mark.asyncio
async def test_without_reranker_results_are_untouched():
    service = make_service()
    collection_id = await seed_documents(service)
    hits = await service.search(
        query="Haftung", collection_ids=[collection_id], top_k=2
    )
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_broken_reranker_fails_loudly_not_silently():
    class BrokenReranker(FakeReranker):
        def rerank(self, *args, **kwargs):
            raise RerankerError("endpoint down")

    service = make_service(reranker=BrokenReranker())
    collection_id = await seed_documents(service)
    with pytest.raises(RerankerError):
        await service.search(query="Haftung", collection_ids=[collection_id])


class FakeHybridStore(MemoryKnowledgeStore):
    """Memory store that records hybrid dispatch."""

    def __init__(self) -> None:
        super().__init__()
        self.hybrid_calls = 0

    @property
    def supports_hybrid(self) -> bool:
        return True

    async def hybrid_search(
        self,
        *,
        query_text,
        query_embedding,
        collection_ids,
        top_k,
        embedding_model=None,
    ) -> list[RetrievalCandidate]:
        self.hybrid_calls += 1
        return await self.search(
            query_embedding=query_embedding,
            collection_ids=collection_ids,
            top_k=top_k,
        )


class CandidatePoolDegradedStore(MemoryKnowledgeStore):
    """Reports a shallow pool while retaining enough final evidence."""

    async def search(
        self,
        *,
        query_embedding,
        collection_ids,
        top_k,
        embedding_model=None,
    ) -> RetrievalCandidateBatch:
        candidates = await super().search(
            query_embedding=query_embedding,
            collection_ids=collection_ids,
            top_k=top_k,
            embedding_model=embedding_model,
        )
        return RetrievalCandidateBatch(
            candidates,
            degradations=(
                RetrievalDegradation(
                    reason="vector_overfetch_cap",
                    retrieval_mode="dense",
                    requested_top_k=top_k,
                    returned_hits=len(candidates),
                    candidate_cap=64,
                    requested_candidate_pool=top_k,
                    returned_candidate_pool=len(candidates),
                    final_top_k=top_k,
                ),
            ),
        )


@pytest.mark.asyncio
async def test_service_dispatches_to_hybrid_capable_stores():
    store = FakeHybridStore()
    service = make_service(store=store)
    collection_id = await seed_documents(service)

    hits = await service.search(query="Haftung", collection_ids=[collection_id])

    assert store.hybrid_calls == 1
    assert hits


@pytest.mark.asyncio
async def test_candidate_pool_underfill_does_not_claim_final_hit_underfill():
    reranker = FakeReranker()
    store = CandidatePoolDegradedStore()
    context = KnowledgeProviderContext(
        embeddings=StubEmbeddings(),
        store=store,
        default_top_k=4,
        reranker=reranker,
        rerank_candidate_depth=10,
    )
    service = KnowledgeService(
        knowledge=context,
        chunk_max_chars=2_000,
        max_document_chars=100_000,
    )
    collection_id = await seed_documents(service)

    outcome = await service.search_reported(
        query="Haftung",
        collection_ids=[collection_id],
        top_k=2,
    )

    assert len(outcome.candidates) == 2
    degradation = outcome.retrieval_degradations[0]
    assert degradation.stage == "vector_candidate_pool"
    assert degradation.requested_candidate_pool == 10
    assert degradation.returned_candidate_pool == 3
    assert degradation.final_top_k == 2
    assert degradation.requested_top_k == 2
    assert degradation.returned_hits == 2
    assert degradation.final_evidence_complete is True


# ------------------------------------------------------------------ #
# Settings bridge validation
# ------------------------------------------------------------------ #


def test_cohere_provider_without_credentials_fails_loudly():
    from inqtrix.server.container import build_knowledge_context
    from inqtrix.settings import KnowledgeSettings, Settings

    settings = Settings(
        knowledge=KnowledgeSettings(
            enabled=True,
            reranker_provider="cohere",
            reranker_base_url="",
        )
    )
    with pytest.raises(RuntimeError, match="INQTRIX_RERANKER_BASE_URL"):
        build_knowledge_context(settings)


def test_azure_embedding_provider_without_endpoint_fails_loudly():
    from inqtrix.server.container import build_knowledge_context
    from inqtrix.settings import KnowledgeSettings, Settings

    settings = Settings(
        knowledge=KnowledgeSettings(
            enabled=True,
            embedding_provider="azure",
            embedding_azure_endpoint="",
            embedding_azure_api_key="key",
        )
    )
    with pytest.raises(RuntimeError, match="Azure endpoint"):
        build_knowledge_context(settings)


# ------------------------------------------------------------------ #
# LLMReranker (listwise fallback through the deployment LLM)
# ------------------------------------------------------------------ #


class RankingLLM:
    """Returns a fixed completion for the listwise rerank prompt."""

    def __init__(self, content: str) -> None:
        self._content = content
        self.prompts: list[str] = []

    def complete_with_metadata(self, prompt: str, **kwargs):
        self.prompts.append(prompt)
        from inqtrix.providers.base import LLMResponse

        return LLMResponse(
            content=self._content,
            prompt_tokens=10,
            completion_tokens=5,
            model="stub-rerank",
            finish_reason="stop",
        )


def test_llm_reranker_orders_best_first_with_monotone_scores():
    from inqtrix.providers.rerankers import LLMReranker

    llm = RankingLLM('{"ranking": [3, 1, 2]}')
    reranker = LLMReranker(llm)
    results = reranker.rerank("Frage", ["a", "b", "c"], top_n=2)
    assert [result.index for result in results] == [2, 0]
    assert results[0].relevance_score > results[1].relevance_score


def test_llm_reranker_truncates_deep_pools_visibly(caplog):
    from inqtrix.providers.rerankers import LLMReranker

    ranking = list(range(1, 4))
    llm = RankingLLM('{"ranking": ' + str(ranking) + "}")
    reranker = LLMReranker(llm, max_candidates=3)
    with caplog.at_level("WARNING", logger="inqtrix"):
        results = reranker.rerank(
            "Frage", ["a", "b", "c", "d", "e"], top_n=3
        )
    assert "LLM-Rerank: candidate list reduced from 5 to 3" in caplog.text
    assert len(results) == 3
    # The prompt must only carry the truncated candidate list.
    assert "[4]" not in llm.prompts[0]


def test_llm_reranker_caps_per_document_chars():
    from inqtrix.providers.rerankers import LLMReranker

    llm = RankingLLM('{"ranking": [1, 2]}')
    reranker = LLMReranker(llm, max_chars_per_document=10)
    reranker.rerank("Frage", ["x" * 500, "y"], top_n=2)
    assert "x" * 11 not in llm.prompts[0]


def test_llm_reranker_malformed_json_raises_loudly():
    from inqtrix.providers.rerankers import LLMReranker, RerankerError

    reranker = LLMReranker(RankingLLM("kein json"))
    with pytest.raises(RerankerError, match="no JSON"):
        reranker.rerank("Frage", ["a", "b"], top_n=2)


def test_llm_reranker_duplicate_indices_raise():
    from inqtrix.providers.rerankers import LLMReranker, RerankerError

    reranker = LLMReranker(RankingLLM('{"ranking": [1, 1]}'))
    with pytest.raises(RerankerError, match="duplicates"):
        reranker.rerank("Frage", ["a", "b"], top_n=2)


def test_llm_reranker_out_of_range_indices_raise():
    from inqtrix.providers.rerankers import LLMReranker, RerankerError

    reranker = LLMReranker(RankingLLM('{"ranking": [1, 5]}'))
    with pytest.raises(RerankerError, match="out-of-range"):
        reranker.rerank("Frage", ["a", "b"], top_n=2)


def test_llm_reranker_incomplete_ranking_raises():
    from inqtrix.providers.rerankers import LLMReranker, RerankerError

    reranker = LLMReranker(RankingLLM('{"ranking": [2]}'))
    with pytest.raises(RerankerError, match="incomplete"):
        reranker.rerank("Frage", ["a", "b", "c"], top_n=2)


def test_llm_reranker_transport_failure_wraps_loudly():
    from inqtrix.providers.rerankers import LLMReranker, RerankerError

    class ExplodingLLM:
        def complete_with_metadata(self, prompt, **kwargs):
            raise TimeoutError("upstream timeout")

    reranker = LLMReranker(ExplodingLLM())
    with pytest.raises(RerankerError, match="LLM rerank call failed"):
        reranker.rerank("Frage", ["a", "b"], top_n=2)


def test_container_llm_reranker_requires_an_llm():
    from inqtrix.server.container import build_knowledge_context
    from inqtrix.settings import Settings

    settings = Settings()
    settings.knowledge.enabled = True
    settings.knowledge.reranker_provider = "llm"
    with pytest.raises(RuntimeError, match="LLM provider"):
        build_knowledge_context(settings, llm=None)
