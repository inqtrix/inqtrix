"""The evidence contract of a research answer: status, causes and ceiling.

The answer node decides the contract once. These tests pin the decision
itself (which status, which cause holds it there, which confidence ceiling
applies and why) and the one rule that names what a run's cited references
could be checked against.
"""

from __future__ import annotations

import pytest

from inqtrix.evidence import cited_material_basis, reference_material_kind
from inqtrix.nodes import _evidence_contract


def _contract(**overrides):
    values = {
        "blocked": False,
        "citable": True,
        "matched_claims": 1,
        "unknown_citations": 0,
        "citations_without_claim": 0,
        "failures": False,
        "depth_gap": {},
    }
    values.update(overrides)
    return _evidence_contract(**values)


def test_a_bound_run_with_nothing_open_is_clean_and_uncapped() -> None:
    contract = _contract()

    assert contract.status == "clean"
    assert contract.causes == []
    assert contract.confidence_cap is None
    assert contract.cap_reasons == []


@pytest.mark.parametrize(
    ("overrides", "cause"),
    [
        ({"unknown_citations": 2}, "unknown_citation"),
        ({"citations_without_claim": 1}, "citation_without_claim"),
        ({"failures": True}, "algorithm_failure"),
    ],
)
def test_each_cause_of_needs_review_has_its_own_name(overrides, cause) -> None:
    """A link without a record, a record without a statement and a failed
    step are three different things; the contract names which one it was,
    and the ceiling carries the same name."""
    contract = _contract(**overrides)

    assert contract.status == "needs_review"
    assert contract.causes == [cause]
    assert contract.confidence_cap == 6
    assert contract.cap_reasons == [cause]


def test_the_depth_gap_alone_caps_a_clean_run_with_its_own_reasons() -> None:
    """Nothing cited is unsubstantiated, yet the supported statements rest
    on too few independent sources. The gap says how deep the support is,
    not whether the answer is bound to it: the run stays clean, and the
    ceiling of 6 names the gap's own reasons instead of a blanket word."""
    contract = _contract(depth_gap={
        "active": True,
        "reason": "no_cross_checked_claims,majority_single_source_claims",
    })

    assert contract.status == "clean"
    assert contract.causes == []
    assert contract.confidence_cap == 6
    assert contract.cap_reasons == [
        "no_cross_checked_claims", "majority_single_source_claims",
    ]


def test_an_active_depth_gap_caps_even_without_a_recorded_reason() -> None:
    """The gap's own flag decides the ceiling, not whether it recorded a
    reason: a gap marked active keeps the ceiling of 6."""
    contract = _contract(depth_gap={"active": True})

    assert contract.status == "clean"
    assert contract.confidence_cap == 6
    assert contract.cap_reasons == []


def test_an_inactive_depth_gap_is_no_cause_and_no_ceiling() -> None:
    contract = _contract(depth_gap={
        "active": False, "reason": "no_cross_checked_claims",
    })

    assert contract.status == "clean"
    assert contract.causes == []
    assert contract.confidence_cap is None
    assert contract.cap_reasons == []


def test_a_binding_cause_is_not_hidden_behind_the_depth_gap() -> None:
    """Both can hold at once: the binding cause holds the status, the gap
    adds no cause, and the ceiling names the cause first, then the gap's
    reasons."""
    contract = _contract(
        citations_without_claim=3,
        depth_gap={"active": True, "reason": "no_cross_checked_claims"},
    )

    assert contract.status == "needs_review"
    assert contract.causes == ["citation_without_claim"]
    assert contract.confidence_cap == 6
    assert contract.cap_reasons == [
        "citation_without_claim", "no_cross_checked_claims",
    ]


@pytest.mark.parametrize(
    ("overrides", "status", "cap"),
    [
        ({"blocked": True}, "algorithm_failed", 3),
        ({"matched_claims": 0}, "source_context_only", 4),
    ],
)
def test_the_harder_statuses_name_themselves_as_the_reason(overrides, status, cap) -> None:
    """Below needs_review the status is its own reason; a depth gap or a
    failed step beside it adds no cause and does not soften the status."""
    contract = _contract(
        failures=True,
        unknown_citations=1,
        depth_gap={"active": True, "reason": "no_cross_checked_claims"},
        **overrides,
    )

    assert contract.status == status
    assert contract.causes == []
    assert contract.confidence_cap == cap
    assert contract.cap_reasons == [status]


def test_a_run_without_citable_evidence_is_unknown_and_uncapped() -> None:
    contract = _contract(citable=False, matched_claims=0, failures=True)

    assert contract.status == "unknown"
    assert contract.causes == []
    assert contract.confidence_cap is None
    assert contract.cap_reasons == []


def test_a_blocked_report_wins_over_a_missing_evidence_base() -> None:
    contract = _contract(blocked=True, citable=False, matched_claims=0)

    assert contract.status == "algorithm_failed"


# --------------------------------------------------------------------------- #
# What the cited references could be checked against
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reference", "search_context", "kind"),
    [
        ({"excerpt": "Wortlaut"}, False, "retained_source_excerpt"),
        ({"source_text": "Wortlaut"}, True, "retained_source_excerpt"),
        ({"grounded_support": "Der Anbieter schreibt ..."}, False, "provider_context"),
        ({"url": "https://example.org"}, True, "provider_context"),
        ({"url": "https://example.org", "provider_snippet": "Ausschnitt"}, False,
         "source_metadata_only"),
    ],
)
def test_material_kind_of_one_reference(reference, search_context, kind) -> None:
    """Stored source text decides first, then provider answer context; a
    page snippet the provider supplied is not consulted."""
    assert reference_material_kind(reference, search_context=search_context) == kind


def test_basis_of_the_cited_references() -> None:
    provider = "provider_context"
    excerpt = "retained_source_excerpt"

    assert cited_material_basis([]) == ""
    assert cited_material_basis([provider, provider]) == "provider_context"
    assert cited_material_basis([provider, excerpt]) == "evidence"
    assert cited_material_basis([excerpt]) == "evidence"
    # Any other mix reads as evidence too, a reference without stored
    # material included: the rule only tells provider context apart.
    assert cited_material_basis([provider, "source_metadata_only"]) == "evidence"
