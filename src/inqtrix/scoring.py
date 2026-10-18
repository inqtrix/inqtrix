"""Score snapshot helpers for evidence-driven progress and stop signals."""

from __future__ import annotations

from typing import Any

SUPPORTED_STATUSES: tuple[str, ...] = ("verified", "attributed")
"""The statuses of a claim with affirmed support. ``verified``: a source's
own text was checked and states it. ``attributed``: the search provider's
answer attributes it to its sources, and no source text was checked. The
research loop (stopping, depth, cross-check planning, selection) reads
support, both alike; what a report or a count shows says which."""


def claim_assessments(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the complete consolidation for quality and verification.

    Before a complete assessment is supplied, the available consolidation
    is the only known assessment. An explicitly empty complete assessment
    is authoritative and must not fall back to a display selection.
    """
    if "consolidated_claims_full" in state:
        return state["consolidated_claims_full"] or []
    return state.get("consolidated_claims", []) or []


def build_score_snapshot(
    state: dict[str, Any],
    *,
    phase: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one score snapshot from the current agent state.

    Args:
        state: Current mutable agent state.
        phase: Phase that is writing the snapshot, such as ``"search"``,
            ``"evaluate"`` or ``"answer"``.
        extra: Optional phase-local values that are not yet projected into
            state, for example stop-cascade or answer-audit details.

    Returns:
        Plain dict suitable for ``state["score_ledger"]`` and forensic logs.
    """
    extra = dict(extra or {})
    source_tiers = dict(state.get("source_tier_counts", {}) or {})
    claim_counts = dict(state.get("claim_status_counts", {}) or {})
    evidence_records = list(state.get("evidence_ledger", []) or [])
    consolidated_claims = claim_assessments(state)
    answer_bindings = list(state.get("answer_evidence_bindings", []) or [])

    report_eligible_count = sum(
        1 for record in evidence_records if record.get("report_eligible")
    )
    verified_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "verified"
    )
    attributed_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "attributed"
    )
    contested_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "contested"
    )
    cross_checked_claim_count = sum(
        1 for claim in consolidated_claims
        if claim.get("verification_basis") == "cross_checked"
    )
    primary_supported_claim_count = sum(
        1 for claim in consolidated_claims
        if claim.get("status") in SUPPORTED_STATUSES
        and claim.get("verification_basis") == "primary_source"
    )
    unverified_claim_count = sum(
        1 for claim in consolidated_claims if claim.get("status") == "unverified"
    )
    unknown_citation_count = sum(
        1 for binding in answer_bindings
        if binding.get("binding_status") == "unknown_citation"
    )
    matched_evidence_count = sum(
        1 for binding in answer_bindings
        if binding.get("binding_status") == "matched"
    )

    snapshot = {
        "round": int(state.get("round", 0) or 0),
        "phase": phase,
        "source": {
            "total_citations": len(state.get("all_citations", []) or []),
            "tier_counts_all": source_tiers,
            "quality_score_all": float(state.get("source_quality_score", 0.0) or 0.0),
        },
        "evidence": {
            "evidence_record_count": len(evidence_records),
            "report_eligible_evidence_count": report_eligible_count,
            "verified_claim_count": verified_claim_count,
            "attributed_claim_count": attributed_claim_count,
            "contested_claim_count": contested_claim_count,
            "primary_supported_claim_count": primary_supported_claim_count,
            "cross_checked_claim_count": cross_checked_claim_count,
            "unverified_claim_count": unverified_claim_count,
            "evidence_depth_gap": dict(state.get("evidence_depth_gap", {}) or {}),
            "rendered_evidence_record_count": int(
                extra.get("rendered_evidence_record_count", 0) or 0
            ),
            "omitted_evidence_record_count": int(
                extra.get("omitted_evidence_record_count", 0) or 0
            ),
        },
        "claims": {
            "consolidated_claim_count": len(consolidated_claims),
            "materialized_claim_count": len(state.get("consolidated_claims", []) or []),
            "verified": int(claim_counts.get("verified", 0) or 0),
            "attributed": int(claim_counts.get("attributed", 0) or 0),
            "contested": int(claim_counts.get("contested", 0) or 0),
            "unverified": int(claim_counts.get("unverified", 0) or 0),
            "quality_score": float(state.get("claim_quality_score", 0.0) or 0.0),
            "needs_primary_total": int(state.get("claim_needs_primary_total", 0) or 0),
            "needs_primary_supported": int(state.get("claim_needs_primary_supported", 0) or 0),
        },
        "coverage": {
            "aspect_coverage_context": float(state.get("aspect_coverage", 0.0) or 0.0),
        },
        "evaluate": {
            "llm_confidence": int(extra.get("llm_confidence", state.get("final_confidence", 0)) or 0),
            "final_confidence": int(extra.get("final_confidence", state.get("final_confidence", 0)) or 0),
            "evidence_consistency": int(state.get("evidence_consistency", 0) or 0),
            "evidence_sufficiency": int(state.get("evidence_sufficiency", 0) or 0),
            # Mirrors the marker written by `evaluate()` into
            # `iteration_log`, so a snapshot consumer can identify an
            # unjustified confidence drop without merging the iteration log.
            # The marker is observational and does not add an automatic
            # revert or another stopping heuristic.
            "confidence_unjustified_drop": bool(extra.get("confidence_unjustified_drop", False)),
        },
        "stop": {
            "utility_score": float(extra.get("utility_score", 0.0) or 0.0),
            "stop_reason": str(extra.get("stop_reason", state.get("_stop_reason", "")) or ""),
            "done": bool(state.get("done", False)),
        },
        "answer": {
            "answer_bound_claims_count": int(extra.get("answer_bound_claims_count", 0) or 0),
            "unbound_answer_citations_count": int(
                extra.get("unbound_answer_citations_count", unknown_citation_count) or 0
            ),
            "matched_evidence_binding_count": matched_evidence_count,
            # Canonical contract comes from the answer node (via extra for the
            # answer phase, or state once stored); never recomputed here.
            "evidence_contract_status": str(
                extra.get("evidence_contract_status")
                or state.get("evidence_contract_status", "unknown")
            ),
        },
    }
    return snapshot


def append_score_snapshot(
    state: dict[str, Any],
    *,
    phase: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a score snapshot to ``state['score_ledger']``."""
    snapshot = build_score_snapshot(state, phase=phase, extra=extra)
    state.setdefault("score_ledger", []).append(snapshot)
    return snapshot
