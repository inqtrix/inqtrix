"""Offline run-publication cases, not a semantic or live-cost benchmark.

Scripted algorithm results exercise the real result serialization, publication
boundary and score projection. They can detect lost/mislabelled judgements;
they cannot establish that a model's judgement or answer is correct.
"""

from types import SimpleNamespace

import pytest

from inqtrix.core.results import AgentResult
from inqtrix.services.run_service import execute_run_request
from tests.test_run_service_execution import _CompletedHandle

pytestmark = pytest.mark.eval

CASES = [
    (
        "comparison",
        "research",
        "Compare phishing resistance and recovery for two authentication methods.",
        {
            "final_confidence": 7,
            "claim_quality_score": 0.7,
            "source_quality_score": 0.8,
            "aspect_coverage": 0.5,
            "omitted_evidence_record_count": 3,
        },
        {
            "final_confidence": 7,
            "claim_quality_score": 0.7,
            "source_quality_score": 0.8,
            "aspect_coverage": 0.5,
            "omitted_evidence_records": 3,
        },
    ),
    (
        "time_window",
        "research",
        "Fasse die belegten Änderungen einer Woche zusammen und nenne Lücken.",
        {
            "answer_contract": "events",
            "_stop_reason": "max_rounds",
            "evidence_contract_status": "needs_review",
        },
        {
            "answer_contract": "events",
            "stop_reason": "max_rounds",
            "evidence_contract_status": "needs_review",
        },
    ),
    (
        "point_fact",
        "research",
        "Welche Aufgabe hat die Europäische Zentralbank?",
        {
            "final_confidence": 6,
            "answer_contract": "general",
            "omitted_evidence_record_count": 0,
        },
        {
            "final_confidence": 6,
            "answer_contract": "general",
            "omitted_evidence_records": 0,
        },
    ),
    (
        "mission",
        "workspace_agent",
        "Erstelle aus einer Sachrecherche ein Memo mit zwei Abschnitten.",
        {"critic_report": {"verdict": "revise"}},
        {"critic_verdict": "revise"},
    ),
]


@pytest.mark.parametrize(
    "case_id,mode,question,state,expected", CASES, ids=[case[0] for case in CASES]
)
@pytest.mark.parametrize("trace_root", [True, False])
def test_published_algorithm_judgements_reach_trace_without_default_metrics(
    monkeypatch,
    case_id,
    mode,
    question,
    state,
    expected,
    trace_root,
):
    handle = _CompletedHandle(total_elapsed=2.0)
    recorded = []

    def enrich(attributes):
        if attributes.get("inqtrix.scores_ready"):
            assert handle.payload is not None, (
                "Scores preceded successful result publication"
            )
            recorded.append(attributes)

    monkeypatch.setattr("inqtrix.observability.otel.enrich_current_span", enrich)

    class Algorithm:
        def run(self, request, *, runtime, context):
            assert request.question == question
            return AgentResult(
                answer="Done",
                raw={"answer": "Done", "usage": {}, "result_state": state},
            )

        def capabilities(self):
            return {"terminal_node": "answer"}

    execute_run_request(
        handle,
        algorithm=Algorithm(),
        run_request=SimpleNamespace(mode=mode, question=question),
        resolved=SimpleNamespace(
            providers=SimpleNamespace(),
            strategies=SimpleNamespace(),
            agent_settings=SimpleNamespace(),
        ),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(quota=SimpleNamespace(max_tokens_per_run=0))
        ),
        principal=None,
        trace_root=trace_root,
        answer_publisher=SimpleNamespace(publish=lambda *_, **__: None),
    )
    assert recorded == [
        {
            "inqtrix.scores_ready": True,
            "inqtrix.scores_trace_root": trace_root,
            **{"inqtrix.score." + key: value for key, value in expected.items()},
        }
    ]
    assert handle.payload["answer"] == "Done"
