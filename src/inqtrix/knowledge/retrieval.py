"""Shared retrieval pipeline: embed, (hybrid) search, rerank.

Defined exactly once (a single source of truth) and consumed by BOTH the
``/v1/knowledge/search`` debug surface and the ``mode=knowledge``
answer path, so the rerank and hybrid stages apply to answers exactly
as they apply to the debug endpoint.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Any, Callable

from inqtrix.knowledge.stores.ports import (
    KnowledgeProviderContext,
    RetrievalCandidate,
    RetrievalCandidateBatch,
)
from inqtrix.providers.base import (
    observe_provider_retries,
    provider_cancel_scope,
)


def _project_final_batch(
    candidates: list[RetrievalCandidate],
    *,
    source_batch: RetrievalCandidateBatch,
    final_top_k: int,
) -> RetrievalCandidateBatch:
    """Bind candidate-pool diagnostics to the independent final hit width."""

    final_candidates = candidates[:final_top_k]
    return RetrievalCandidateBatch(
        final_candidates,
        degradations=tuple(
            degradation.with_final_result(
                final_top_k=final_top_k,
                returned_hits=len(final_candidates),
            )
            for degradation in source_batch.degradations
        ),
        exclusions=source_batch.exclusions,
    )


async def retrieve(
    knowledge: KnowledgeProviderContext,
    *,
    query: str,
    collection_ids: list[str] | None,
    top_k: int,
    use_reranker: bool = True,
    rerank_candidate_depth: int | None = None,
    on_provider_retry: Callable[[dict[str, Any]], None] | None = None,
    cancel_probe: Callable[[], bool] | None = None,
) -> RetrievalCandidateBatch:
    """Run the full retrieval pipeline for one query.

    Stages: query embedding (with the scope's collection model) →
    hybrid search when the store is hybrid-capable, dense otherwise →
    optional cross-encoder rerank reducing a deeper candidate pool to
    *top_k*. A configured-but-broken reranker raises loudly; only the
    unconfigured state skips the stage.

    Args:
        use_reranker: Profile hook — ``False`` skips the rerank stage
            even when a reranker is wired (the fast profile). The
            default keeps every pre-profile caller byte-stable.
        rerank_candidate_depth: Profile hook — candidate pool fetched
            ahead of the rerank stage; ``None`` uses the configured
            default depth.
        on_provider_retry: Optional observer for rerank-provider retry
            notices (the shared ``_RetryNoticeMixin`` dict shape).
            Retries would otherwise only reach the server log; callers
            with an event surface forward them there (no silent
            fallbacks). ``None`` keeps the historical behaviour.
        cancel_probe: Optional run-cancellation probe, re-bound on the
            embed and rerank worker threads (the provider scope is
            thread-local and does not travel through ``to_thread``).
            ``None`` keeps the historical behaviour.
    """
    reranker = knowledge.reranker if use_reranker else None
    candidate_depth = (
        rerank_candidate_depth
        if rerank_candidate_depth is not None
        else knowledge.rerank_candidate_depth
    )
    retrieve_top_k = (
        max(top_k, candidate_depth) if reranker is not None else top_k
    )

    store = knowledge.store
    # One vector cannot be compared across embedding spaces.  Resolve the
    # concrete collection scope into model-homogeneous groups, run the SAME
    # canonical store pipeline for each group, then fuse ranks before the
    # shared reranker/final projection.  This keeps mixed-model coverage in
    # the Knowledge subsystem instead of silently discarding collections at
    # an API or Agent adapter.
    if collection_ids == []:
        return RetrievalCandidateBatch()
    if collection_ids is None:
        scoped_collections = await store.list_collections()
    else:
        scoped_collections = [
            await store.get_collection(collection_id)
            for collection_id in collection_ids
        ]
    model_scopes: dict[str, list[str]] = {}
    for collection in scoped_collections:
        model_scopes.setdefault(collection.embedding_model, []).append(
            collection.id
        )
    if not model_scopes:
        return RetrievalCandidateBatch()

    group_batches: list[RetrievalCandidateBatch] = []
    for embedding_model, scoped_ids in model_scopes.items():

        def _embed_scoped(model_name: str = embedding_model) -> Any:
            # Bind the cancel probe on the worker thread itself — the
            # provider scope is thread-local.
            with provider_cancel_scope(cancel_probe):
                return knowledge.embeddings.embed_query(
                    query, model=model_name
                )

        query_embedding = await asyncio.to_thread(_embed_scoped)
        search_started = time.monotonic()
        if getattr(store, "supports_hybrid", False):
            candidates = await store.hybrid_search(
                query_text=query,
                query_embedding=query_embedding,
                collection_ids=scoped_ids,
                top_k=retrieve_top_k,
                embedding_model=embedding_model,
            )
        else:
            candidates = await store.search(
                query_embedding=query_embedding,
                collection_ids=scoped_ids,
                top_k=retrieve_top_k,
                embedding_model=embedding_model,
            )
        _observe_retrieval_step("hybrid_search", search_started)
        group_batches.append(
            candidates
            if isinstance(candidates, RetrievalCandidateBatch)
            else RetrievalCandidateBatch(candidates)
        )

    if len(group_batches) == 1:
        merged_candidates = list(group_batches[0])
    else:
        # Scores from different embedding spaces are not assumed comparable.
        # Round-robin rank fusion gives every selected model group a bounded
        # opportunity to contribute; a configured cross-encoder then supplies
        # the single final relevance scale.
        merged_candidates = interleave_candidates(
            group_batches,
            limit=retrieve_top_k,
        )
    batch = RetrievalCandidateBatch(
        merged_candidates,
        degradations=tuple(
            degradation
            for group in group_batches
            for degradation in group.degradations
        ),
        exclusions=tuple(
            exclusion
            for group in group_batches
            for exclusion in group.exclusions
        ),
    )
    if reranker is None or len(batch) <= 1:
        return _project_final_batch(
            list(batch),
            source_batch=batch,
            final_top_k=top_k,
        )
    candidate_texts = [candidate.chunk.text for candidate in batch]

    def _rerank_observed() -> list[Any]:
        # Observer binding, the cancel scope, the call, and the
        # thread-local cleanup must all run on the SAME thread as the
        # rerank (the mixin state is threading.local), hence one closure
        # handed to to_thread.
        with provider_cancel_scope(cancel_probe):
            with observe_provider_retries(reranker, on_provider_retry):
                try:
                    return reranker.rerank(
                        query, candidate_texts, top_n=top_k
                    )
                finally:
                    consume = getattr(reranker, "consume_retry_notices", None)
                    if callable(consume):
                        # Clear leftover notices so a reused executor
                        # thread cannot bleed them into a later request.
                        consume()

    rerank_started = time.monotonic()
    results = await asyncio.to_thread(_rerank_observed)
    _observe_retrieval_step("rerank", rerank_started)
    return _project_final_batch(
        [
            RetrievalCandidate(
                chunk=batch[result.index].chunk,
                score=result.relevance_score,
                document_title=batch[result.index].document_title,
            )
            for result in results
        ],
        source_batch=batch,
        final_top_k=top_k,
    )


def interleave_candidates(
    result_lists: Sequence[Sequence[RetrievalCandidate]],
    *,
    limit: int,
) -> RetrievalCandidateBatch:
    """Round-robin merge of per-sub-query result lists, capped at *limit*.

    The decomposition merge: taking one candidate per list in rotation
    guarantees EVERY aspect contributes to the top-k instead of the
    first list crowding the others out (the aggregation failure class
    a plain first-wins union reproduces). The original question's list
    goes first in the rotation; duplicates collapse on chunk id.
    """
    merged: list[RetrievalCandidate] = []
    seen: set[str] = set()
    cursors = [0] * len(result_lists)
    while len(merged) < limit:
        progressed = False
        for list_index, candidates in enumerate(result_lists):
            cursor = cursors[list_index]
            while cursor < len(candidates):
                candidate = candidates[cursor]
                cursor += 1
                if candidate.chunk.id not in seen:
                    seen.add(candidate.chunk.id)
                    merged.append(candidate)
                    progressed = True
                    break
            cursors[list_index] = cursor
            if len(merged) >= limit:
                break
        if not progressed:
            break
    return RetrievalCandidateBatch(
        merged,
        degradations=tuple(
            degradation
            for candidates in result_lists
            if isinstance(candidates, RetrievalCandidateBatch)
            for degradation in candidates.degradations
        ),
        exclusions=tuple(
            exclusion
            for candidates in result_lists
            if isinstance(candidates, RetrievalCandidateBatch)
            for exclusion in candidates.exclusions
        ),
    )


def merge_candidates(
    primary: Sequence[RetrievalCandidate],
    secondary: Sequence[RetrievalCandidate],
    *,
    limit: int,
) -> RetrievalCandidateBatch:
    """Union two candidate lists, first occurrence wins, capped at *limit*.

    Used by the second retrieval pass: the original ranking stays
    authoritative, rewritten-query hits fill the remaining slots.
    """
    merged: list[RetrievalCandidate] = []
    seen: set[str] = set()
    for candidate in [*primary, *secondary]:
        if candidate.chunk.id in seen:
            continue
        seen.add(candidate.chunk.id)
        merged.append(candidate)
        if len(merged) >= limit:
            break
    batches = (primary, secondary)
    return RetrievalCandidateBatch(
        merged,
        degradations=tuple(
            degradation
            for candidates in batches
            if isinstance(candidates, RetrievalCandidateBatch)
            for degradation in candidates.degradations
        ),
        exclusions=tuple(
            exclusion
            for candidates in batches
            if isinstance(candidates, RetrievalCandidateBatch)
            for exclusion in candidates.exclusions
        ),
    )


def _observe_retrieval_step(step: str, started: float) -> None:
    """retrieval_duration histogram feed (hybrid_search | rerank)."""
    from inqtrix.observability.metrics_defs import active_metrics

    metrics = active_metrics()
    if metrics is not None:
        metrics.observe_retrieval_step(
            step=step, duration_seconds=time.monotonic() - started
        )
