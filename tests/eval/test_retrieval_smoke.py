"""Offline smoke for the eval harness (stub embeddings, no network).

Proves the HARNESS — corpus ingestion through the real chunking path,
search execution, metric assembly, artifact shape — not retrieval
quality: the deterministic word-bucket stub embeddings make the
absolute numbers meaningless. Quality thresholds live in the gated
real-embedding suite next door.
"""

from __future__ import annotations

import pytest

from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
from inqtrix.knowledge.stores.ports import KnowledgeProviderContext
from inqtrix.services.knowledge_service import KnowledgeService

from tests.eval.harness import EVAL_CHUNK_MAX_CHARS, run_retrieval_eval
from tests.test_knowledge_engine import StubEmbeddings


def make_stub_service() -> KnowledgeService:
    return KnowledgeService(
        knowledge=KnowledgeProviderContext(
            embeddings=StubEmbeddings(),
            store=MemoryKnowledgeStore(),
            default_top_k=5,
        ),
        chunk_max_chars=EVAL_CHUNK_MAX_CHARS,
        max_document_chars=100_000,
    )


@pytest.mark.asyncio
async def test_harness_runs_end_to_end_and_reports_complete_metrics():
    report = await run_retrieval_eval(make_stub_service(), top_k=5)

    assert report.embedding_model == "stub-embed-8"
    assert report.query_count == 44
    assert len(report.per_query) == 44
    for value in (
        report.recall_at_1,
        report.recall_at_3,
        report.recall_at_5,
        report.mrr,
        report.ndcg_at_5,
        report.multi_complete_at_5,
    ):
        assert 0.0 <= value <= 1.0
    # Monotonicity by construction: deeper cutoffs never lose hits.
    assert report.recall_at_1 <= report.recall_at_3 <= report.recall_at_5
    assert set(report.per_category_recall_at_5) == {
        "fact",
        "paraphrase",
        "exact",
        "multi",
    }


@pytest.mark.asyncio
async def test_harness_is_deterministic():
    first = await run_retrieval_eval(make_stub_service(), top_k=5)
    second = await run_retrieval_eval(make_stub_service(), top_k=5)
    assert first.to_payload() == second.to_payload()

@pytest.mark.asyncio
async def test_eval_chunk_budget_actually_splits_the_corpus():
    """The small eval chunk budget must keep chunking load-bearing —
    if every document stays a single chunk, crowding is impossible and
    the corpus geometry makes recall@5 nearly free."""
    service = make_stub_service()
    collection = await service.create_collection(name="chunk-check")
    from tests.eval.harness import load_corpus

    multi_chunk_docs = 0
    for _doc_id, title, text in load_corpus():
        document = await service.add_document(
            collection_id=collection.id, title=title, text=text
        )
        if document.chunk_count > 1:
            multi_chunk_docs += 1
    assert multi_chunk_docs >= 8, (
        f"only {multi_chunk_docs}/10 corpus documents split into "
        "multiple chunks — shrink EVAL_CHUNK_MAX_CHARS or grow the corpus"
    )

