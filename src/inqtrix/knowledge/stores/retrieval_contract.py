"""Shared bounded candidate-pool contract for canonical vector retrieval."""

from __future__ import annotations

from collections.abc import Sequence

from inqtrix.knowledge.stores.ports import (
    RetrievalCandidate,
    RetrievalCandidateBatch,
    RetrievalDegradation,
)

MIN_VECTOR_CANDIDATES = 64
MAX_VECTOR_CANDIDATES = 512
VECTOR_OVERFETCH_FACTOR = 8


def validate_vector_candidate_cap(candidate_cap: int) -> int:
    """Accept a bounded library override without allowing an unsafe increase."""

    if isinstance(candidate_cap, bool) or not isinstance(candidate_cap, int):
        raise ValueError("vector_candidate_cap must be an integer")
    if candidate_cap < 1 or candidate_cap > MAX_VECTOR_CANDIDATES:
        raise ValueError(
            "vector_candidate_cap must be between 1 and "
            f"{MAX_VECTOR_CANDIDATES}"
        )
    return candidate_cap


def bounded_candidate_depth(
    requested_candidate_pool: int,
    *,
    configured_cap: int = MAX_VECTOR_CANDIDATES,
) -> int:
    """Derive the geometric overfetch ceiling within the safe hard cap."""

    safe_cap = validate_vector_candidate_cap(configured_cap)
    requested = max(1, int(requested_candidate_pool))
    derived = max(
        MIN_VECTOR_CANDIDATES,
        requested * VECTOR_OVERFETCH_FACTOR,
    )
    return min(safe_cap, derived)


def degraded_candidates(
    candidates: Sequence[RetrievalCandidate],
    *,
    reason: str,
    retrieval_mode: str,
    requested_candidate_pool: int,
    candidate_cap: int | None,
) -> RetrievalCandidateBatch:
    """Return the verified pool with an explicit candidate-stage boundary."""

    visible = list(candidates[:requested_candidate_pool])
    exclusions = (
        candidates.exclusions
        if isinstance(candidates, RetrievalCandidateBatch)
        else ()
    )
    return RetrievalCandidateBatch(
        visible,
        degradations=(
            RetrievalDegradation(
                reason=reason,
                retrieval_mode=retrieval_mode,
                # Until the shared pipeline projects its independent final
                # width, a direct store call has final_k == candidate-pool k.
                requested_top_k=requested_candidate_pool,
                returned_hits=len(visible),
                candidate_cap=candidate_cap,
                stage="vector_candidate_pool",
                requested_candidate_pool=requested_candidate_pool,
                returned_candidate_pool=len(visible),
                final_top_k=requested_candidate_pool,
            ),
        ),
        exclusions=exclusions,
    )
