"""Tests for score-ledger snapshots."""

from inqtrix.scoring import append_score_snapshot


def test_append_score_snapshot_groups_evidence_claim_stop_and_answer_metrics():
    state = {
        "round": 2,
        "all_citations": ["https://example.com/a", "https://example.com/b"],
        "source_tier_counts": {"primary": 1, "mainstream": 1, "unknown": 0, "low": 0, "stakeholder": 0},
        "source_quality_score": 0.9,
        "evidence_ledger": [
            {"evidence_id": "ev_1", "report_eligible": True},
            {"evidence_id": "ev_2", "report_eligible": False},
        ],
        "consolidated_claims": [
            {
                "claim_id": "claim_1",
                "status": "verified",
                "verification_basis": "primary_source",
            },
            {
                "claim_id": "claim_3",
                "status": "attributed",
                "verification_basis": "primary_source",
            },
            {"claim_id": "claim_2", "status": "unverified", "verification_basis": "weak_evidence"},
        ],
        "claim_status_counts": {"verified": 1, "attributed": 1, "contested": 0, "unverified": 1},
        "claim_quality_score": 0.5,
        "claim_needs_primary_total": 1,
        "claim_needs_primary_supported": 1,
        "aspect_coverage": 0.667,
        "evidence_consistency": 8,
        "evidence_sufficiency": 4,
        "answer_evidence_bindings": [{"binding_status": "unknown_citation"}],
        # Canonical contract is set by the answer node and stored on state; the
        # snapshot reads it verbatim (never recomputed inside scoring.py).
        "evidence_contract_status": "needs_review",
        "done": True,
    }

    snapshot = append_score_snapshot(
        state,
        phase="evaluate",
        extra={"utility_score": 0.43, "stop_reason": "stagnation_low_evidence"},
    )

    assert snapshot["source"]["total_citations"] == 2
    assert snapshot["evidence"]["report_eligible_evidence_count"] == 1
    assert snapshot["evidence"]["verified_claim_count"] == 1
    assert snapshot["evidence"]["attributed_claim_count"] == 1
    # A primary-source structure supports a claim checked or attributed.
    assert snapshot["evidence"]["primary_supported_claim_count"] == 2
    assert snapshot["evidence"]["unverified_claim_count"] == 1
    assert snapshot["claims"]["attributed"] == 1
    assert snapshot["claims"]["needs_primary_supported"] == 1
    assert snapshot["claims"]["quality_score"] == 0.5
    assert snapshot["coverage"] == {"aspect_coverage_context": 0.667}
    assert snapshot["stop"]["stop_reason"] == "stagnation_low_evidence"
    assert snapshot["answer"]["evidence_contract_status"] == "needs_review"
    assert state["score_ledger"] == [snapshot]
