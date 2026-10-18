"""An agent result reports no metric it did not measure.

Agent runs ride the research serialization, so every research counter
reached the wire as a hard zero. A mission with sixteen knowledge
searches and seven citations still published ``total_queries: 0`` and
``total_citations: 0`` — numbers that read as measurements and were
none. Omission is the honest shape; wall clock and tokens stay because
an agent run really does measure those.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from inqtrix.core.results import RunRequest
from inqtrix.server.runs import RunHandle, RunStore
from inqtrix.services.run_service import (
    _prune_uncollected_agent_metrics,
    execute_run_request,
)

RESEARCH_SHAPED = {
    "metrics": {
        "rounds": 0,
        "elapsed_seconds": 564.94,
        "total_queries": 0,
        "total_citations": 0,
        "confidence": 0,
        "aspect_coverage": 0.0,
        "evidence_consistency": 0,
        "evidence_sufficiency": 0,
        "sources": {"tier_counts": {}, "quality_score": 0.0},
        "claims": {"status_counts": {}, "quality_score": 0.0},
        "answer_bound_claims_count": 0,
        "prompt_tokens": 1200,
        "completion_tokens": 350,
    },
    "answer": "…",
}


def test_uncollected_research_counters_are_omitted_not_zeroed():
    payload = {**RESEARCH_SHAPED, "metrics": dict(RESEARCH_SHAPED["metrics"])}

    _prune_uncollected_agent_metrics(payload)

    for absent in (
        "rounds",
        "total_queries",
        "total_citations",
        "confidence",
        "aspect_coverage",
        "evidence_consistency",
        "evidence_sufficiency",
        "sources",
        "claims",
        "answer_bound_claims_count",
    ):
        assert absent not in payload["metrics"], absent


def test_what_an_agent_run_really_measures_survives():
    payload = {**RESEARCH_SHAPED, "metrics": dict(RESEARCH_SHAPED["metrics"])}

    _prune_uncollected_agent_metrics(payload)

    assert payload["metrics"] == {
        "elapsed_seconds": 564.94,
        "prompt_tokens": 1200,
        "completion_tokens": 350,
    }
    # Everything else in the payload is untouched.
    assert payload["answer"] == "…"


def test_a_payload_without_metrics_is_left_alone():
    payload = {"answer": "…"}

    _prune_uncollected_agent_metrics(payload)

    assert payload == {"answer": "…"}


# --- the wiring, not just the helper -------------------------------------- #


class _StubAgentAlgorithm:
    """Minimal agent algorithm: one answer, no research state."""

    def capabilities(self) -> dict:
        return {"terminal_node": "answer"}

    def run(self, request, *, runtime, context):  # noqa: ANN001, ANN201
        return SimpleNamespace(
            raw={"answer": "fertig", "usage": {}, "result_state": {}},
            cancelled=False,
            cancel_reason=None,
        )


class _CapturingPublisher:
    """Answer publisher that records the payload the run service built."""

    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, handle, answer, *, references, question):  # noqa: ANN001
        self.published.append(answer)


def _agent_run_payload() -> dict:
    """Execute one AGENT run through the real service and read its result."""
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=1_000,
    )
    release = threading.Event()
    summary = store.submit(
        question="q", stack_name="default", work=lambda handle: release.wait(5)
    )
    run_id = str(summary["run_id"])
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if store.get(run_id)["status"] == "running":
            break
        time.sleep(0.01)
    handle = RunHandle(store, run_id, threading.Event())
    try:
        execute_run_request(
            handle,
            algorithm=_StubAgentAlgorithm(),
            run_request=RunRequest(mode="agent_kernel", question="q"),
            resolved=SimpleNamespace(
                providers=SimpleNamespace(llm=None, search=None),
                strategies=SimpleNamespace(),
                agent_settings=SimpleNamespace(),
            ),
            runtime=SimpleNamespace(
                settings=SimpleNamespace(
                    quota=SimpleNamespace(max_tokens_per_run=0)
                )
            ),
            principal=None,
            answer_publisher=_CapturingPublisher(),
        )
    finally:
        release.set()
    return store._records[run_id].result or {}


def test_an_agent_run_publishes_no_fabricated_research_counters():
    """Pins the WIRING: without the prune call the shared research
    serialization puts zeros for queries and citations on the wire."""
    payload = _agent_run_payload()

    metrics = payload.get("metrics", {})
    assert "total_citations" not in metrics
    assert "total_queries" not in metrics
    assert "aspect_coverage" not in metrics
    # The honest remainder is still there.
    assert "elapsed_seconds" in metrics
