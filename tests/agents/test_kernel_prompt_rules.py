"""Rules the kernel's prompts carry, pinned by their wording.

The system prompt tells the kernel once that a web query leaves the server
and what an outdated project document calls for; the review rubric says how
a reading record counts and what the review reports about material it
cannot confirm.
"""

from __future__ import annotations

from inqtrix.agents.prompts import (
    EXTERNAL_SEARCH_RULE,
    PROJECT_DOCUMENT_CURRENCY_RULE,
    build_agent_kernel_system_prompt,
)


def test_the_kernel_is_told_once_that_web_text_leaves_the_server():
    assert build_agent_kernel_system_prompt().count(EXTERNAL_SEARCH_RULE) == 1


def test_the_kernel_is_told_once_what_an_outdated_document_calls_for():
    kernel = build_agent_kernel_system_prompt()
    assert kernel.count(PROJECT_DOCUMENT_CURRENCY_RULE) == 1


def test_the_review_rubric_says_how_a_reading_record_counts():
    """The rule stands in the trusted rubric, never inside the fenced record."""
    from inqtrix.agents.kernel.evidence_read import READING_RECORD_METHOD
    from inqtrix.agents.prompts import DEEP_REVIEW_RUBRIC

    assert "counts as read for exactly what it records" in DEEP_REVIEW_RUBRIC
    assert "a limit it names stays open" in DEEP_REVIEW_RUBRIC
    assert "several records about the same material are not independent confirmations" in DEEP_REVIEW_RUBRIC
    # The fenced description only describes; it addresses no reader.
    for rule_word in ("counts as read", "you", "confirm", "treat", "claim", "must"):
        assert rule_word not in READING_RECORD_METHOD.lower(), rule_word


def test_the_review_rubric_asks_to_report_what_the_material_does_not_carry():
    """What the material does not carry is reported - a finding on its
    passage, or not_checked - and never written into an output as open;
    which statements rest on the search provider's context the server shows
    the reader itself, so the rubric demands no attribution formula."""
    from inqtrix.agents.phase_models import DeepReviewVerdict
    from inqtrix.agents.prompts import DEEP_REVIEW_RUBRIC

    assert "Provider context shows what the search provider wrote, not what the source says" in DEEP_REVIEW_RUBRIC
    assert "named as open" not in DEEP_REVIEW_RUBRIC
    assert "honestly named" not in DEEP_REVIEW_RUBRIC
    assert "according to [W3]" not in DEEP_REVIEW_RUBRIC
    assert "it stays as written and is reported" in DEEP_REVIEW_RUBRIC
    grounded = DeepReviewVerdict.model_fields["grounded"].description or ""
    assert "explicitly open" not in grounded
    assert "reported" in grounded
