"""Citation-label and artifact-reference contracts for agent synthesis."""

from __future__ import annotations

from typing import Any

import pytest

from inqtrix.agents.synthesis import (
    CitationValidationFailed,
    citation_coverage,
    cited_references,
    unknown_citation_labels,
    write_section,
)
from inqtrix.providers.base import StructuredLLMResponse


class _CitationLLM:
    """Structured provider whose first text and repair are independently set."""

    def __init__(self, initial: str, repaired: str) -> None:
        self.initial = initial
        self.repaired = repaired
        self.schemas: list[str] = []

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self,
        prompt: str,
        *,
        schema_name: str,
        **kwargs: Any,
    ) -> StructuredLLMResponse:
        del prompt, kwargs
        self.schemas.append(schema_name)
        markdown = self.initial if schema_name == "SectionText" else self.repaired
        return StructuredLLMResponse(
            parsed={"markdown": markdown},
            content=markdown,
            prompt_tokens=2,
            completion_tokens=3,
        )


def _write(llm: _CitationLLM, internal_ids: list[str] | None = None) -> tuple[str, dict[str, int]]:
    return write_section(
        llm,
        question="Question",
        section_title="Result",
        section_focus="Answer",
        evidence_digest="[W1] Evidence",
        evidence_register="[W1] Evidence",
        contradictions_digest="",
        model=None,
        reasoning_effort=None,
        timeout=600,
        known_labels=["W1"],
        internal_reference_ids=internal_ids,
    )


def test_citation_metrics_count_complete_labels_not_prefix_letters() -> None:
    coverage = citation_coverage("First claim [W1].\n\nSecond [W2] and [K10].")

    assert coverage["labels_used"] == 3
    assert coverage["cited_paragraphs"] == 2


def test_artifact_references_include_only_known_labels_used_in_output() -> None:
    references = [
        {"label": "W1", "url": "https://example.test/one"},
        {"label": "W2", "url": "https://example.test/two"},
        {"label": "K1", "document_id": "doc-1", "chunk_index": 0},
    ]

    selected = cited_references("Claim [W2] and detail [K1].", references)

    assert [reference["label"] for reference in selected] == ["W2", "K1"]
    assert unknown_citation_labels("Known [W1], invented [W9].", references) == [
        "W9"
    ]


def test_unknown_label_is_repaired_once_and_usage_remains_metered() -> None:
    llm = _CitationLLM("Unsupported [W9].", "Supported [W1].")

    markdown, usage = _write(llm)

    assert markdown == "Supported [W1]."
    assert usage == {"prompt_tokens": 4, "completion_tokens": 6}
    assert llm.schemas == ["SectionText", "CitationRepairText"]


def test_failed_single_repair_rejects_the_output() -> None:
    llm = _CitationLLM("Unsupported [W9].", "Still unsupported [W8].")

    with pytest.raises(
        CitationValidationFailed, match="remaining=.*W8"
    ) as exc_info:
        _write(llm)

    assert llm.schemas == ["SectionText", "CitationRepairText"]
    assert exc_info.value.usage == {
        "prompt_tokens": 4,
        "completion_tokens": 6,
    }


def test_registered_internal_identifiers_are_repaired_without_changing_valid_citations() -> None:
    llm = _CitationLLM("Supported [W1].\nMetadata: opaque-evidence-key", "Supported [W1].")
    markdown, usage = _write(llm, ["opaque-evidence-key"])
    assert markdown == "Supported [W1]."
    assert llm.schemas == ["SectionText", "CitationRepairText"]
    assert usage == {"prompt_tokens": 4, "completion_tokens": 6}


def test_clean_prose_needs_no_repair_when_internal_identifiers_are_registered() -> None:
    llm = _CitationLLM("Supported [W1].", "Unexpected repair")
    markdown, usage = _write(llm, ["opaque-evidence-key"])
    assert markdown == "Supported [W1]."
    assert llm.schemas == ["SectionText"]
    assert usage == {"prompt_tokens": 2, "completion_tokens": 3}


def test_internal_identifier_remaining_after_one_repair_rejects_the_output() -> None:
    llm = _CitationLLM("Supported [W1]. opaque-evidence-key", "Still opaque-evidence-key [W1].")
    with pytest.raises(CitationValidationFailed, match="internal_ids=1"):
        _write(llm, ["opaque-evidence-key"])
    assert llm.schemas == ["SectionText", "CitationRepairText"]
