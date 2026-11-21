"""A provider retry during a file analysis is visible, under its own node.

The analysis reads a document window by window, each window one structured
call; a slow provider retries such a call, and the reader should see that
on the file-analysis step and in the execution phase.
"""

from __future__ import annotations

from typing import Any

from inqtrix.providers.attempts import report_retry
from inqtrix.providers.base import StructuredLLMResponse


class _WindowRetryLLM:
    """A provider that reports one transient retry on every window call."""

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return True

    def complete_structured(
        self, prompt: str, *, schema_name: str, **kwargs: Any
    ) -> StructuredLLMResponse:
        del prompt, kwargs
        if schema_name == "WindowExtraction":
            report_retry("model", {
                "attempt": 1, "max_attempts": 3, "delay_seconds": 0.25,
                "error_code": "provider_timeout", "operation": "complete_structured",
            })
        return StructuredLLMResponse(
            parsed={"findings": [], "more_relevant": False},
            content='{"findings": [], "more_relevant": false}',
            prompt_tokens=7,
            completion_tokens=5,
        )


def test_a_document_window_call_surfaces_its_provider_retry():
    """The sweep's window calls report provider retries under their node,
    so a slow provider is visible on the file-analysis task."""
    from inqtrix.agents.document_analysis import ANALYSIS_NODE
    from inqtrix.agents.patterns._structured import (
        observe_structured_retries,
        structured_call,
    )
    from inqtrix.agents.phase_models import WindowExtraction

    observed: list[tuple[str, dict[str, Any]]] = []
    with observe_structured_retries(
        lambda node, notice: observed.append((node, notice))
    ):
        structured_call(
            _WindowRetryLLM(), prompt="window", model_cls=WindowExtraction, node=ANALYSIS_NODE,
        )

    assert [(node, notice["operation"]) for node, notice in observed] == [
        ("agent_file_analysis", "complete_structured")
    ]


def test_a_file_analysis_retry_is_telemetry_of_the_execution_phase():
    """A file analysis is a step of the run's execution; its retries say so."""
    from inqtrix.agents.telemetry import model_retry_activity

    activity = model_retry_activity({"attempt": 1, "max_attempts": 3}, node="agent_file_analysis")
    assert activity["phase"] == "execution"
