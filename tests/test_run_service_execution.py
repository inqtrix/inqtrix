"""Run-service terminal typing independent of the execution backend."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from inqtrix.core.results import AgentResult
from inqtrix.services.run_service import execute_run_request


class _Handle:
    run_id = "run-terminal-type"
    parked = False

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancel_event = threading.Event()
        if cancelled:
            self.cancel_event.set()
        self.calls: list[tuple[str, str]] = []

    def emit(self, _event_type, _payload) -> None:
        return

    def wait(self, _status) -> None:
        return

    def cancel(self, reason: str) -> None:
        self.calls.append(("cancel", reason))

    def fail(self, message: str, *, error_type: str) -> None:
        self.calls.append((error_type, message))


class _Algorithm:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def run(self, _request, *, runtime, context) -> AgentResult:
        del runtime, context
        return AgentResult(
            answer="",
            raw={
                "answer": "",
                "usage": {},
                "result_state": {
                    "cancelled": True,
                    "cancel_reason": self._reason,
                },
            },
        )


class _CompletedAlgorithm:
    def run(self, _request, *, runtime, context) -> AgentResult:
        del runtime, context
        return AgentResult(
            answer="Done",
            raw={
                "answer": "Done",
                "usage": {},
                "result_state": {},
            },
        )

    def capabilities(self) -> dict[str, str]:
        return {"terminal_node": "answer"}


class _CompletedKnowledgeAlgorithm:
    def run(self, _request, *, runtime, context) -> AgentResult:
        del runtime, context
        degradation = {
            "reason": "vector_candidate_stalled",
            "retrieval_mode": "dense",
            "stage": "vector_candidate_pool",
            "requested_candidate_pool": 40,
            "returned_candidate_pool": 6,
            "final_top_k": 4,
            "final_evidence_complete": False,
            "requested_top_k": 4,
            "returned_hits": 2,
            "candidate_cap": 64,
        }
        return AgentResult(
            answer="Done",
            result_type="knowledge_result",
            raw={
                "answer": "Done",
                "usage": {},
                "result_state": {
                    "knowledge_profile": {
                        "id": "standard",
                        "requested": "standard",
                        "auto_selected": False,
                        "auto_reason": "",
                        "degraded_stages": [],
                    },
                    "knowledge_gate": {
                        "enabled": True,
                        "sufficient": True,
                        "coverage": "full",
                        "rounds_used": 0,
                        "max_rounds": 1,
                    },
                    "knowledge_grounding": {
                        "enabled": True,
                        "marker": "_knowledge_grounding_parsed",
                        "status": "verified",
                        "failure_code": None,
                        "quotes_total": 1,
                        "quotes_verified": 1,
                        "quotes": [
                            {
                                "label": "K1",
                                "text": "private source text",
                                "verified": True,
                            }
                        ],
                    },
                    "knowledge_retrieval": {
                        "degradations": [degradation, dict(degradation)]
                    },
                    "knowledge_candidates": 6,
                    "knowledge_evidence_used": 2,
                    "queries": ["private question"],
                },
            },
        )

    def capabilities(self) -> dict[str, str]:
        return {"terminal_node": "answer"}


class _CompletedHandle(_Handle):
    def __init__(self, *, total_elapsed: float) -> None:
        super().__init__()
        self._total_elapsed = total_elapsed
        self.payload: dict | None = None
        self.snapshot: dict | None = None

    def total_elapsed_seconds(self) -> float:
        return self._total_elapsed

    def emit_answer(self, answer: str) -> None:
        assert answer == "Done"

    def complete(self, result: dict, snapshot=None) -> None:
        self.payload = result
        self.snapshot = snapshot


def _execute(handle: _Handle, *, reason: str) -> None:
    execute_run_request(
        handle,
        algorithm=_Algorithm(reason),
        run_request=SimpleNamespace(),
        resolved=SimpleNamespace(
            providers=SimpleNamespace(),
            strategies=SimpleNamespace(),
            agent_settings=SimpleNamespace(),
        ),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                quota=SimpleNamespace(max_tokens_per_run=500)
            )
        ),
        principal=None,
    )


def test_token_budget_cancellation_is_a_typed_failure() -> None:
    handle = _Handle()

    _execute(handle, reason="token_budget_exceeded")

    assert handle.calls == [
        (
            "token_budget_exceeded",
            "The run reached the server-side token budget.",
        )
    ]


def test_client_cancel_wins_even_when_a_global_token_cap_is_active() -> None:
    handle = _Handle(cancelled=True)

    _execute(handle, reason="token_budget_exceeded")

    assert handle.calls == [("cancel", "client_requested_cancel")]


def test_exported_elapsed_time_keeps_the_full_resumed_run_duration() -> None:
    handle = _CompletedHandle(total_elapsed=240.25)

    execute_run_request(
        handle,
        algorithm=_CompletedAlgorithm(),
        run_request=SimpleNamespace(mode="research"),
        resolved=SimpleNamespace(
            providers=SimpleNamespace(),
            strategies=SimpleNamespace(),
            agent_settings=SimpleNamespace(),
        ),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                quota=SimpleNamespace(max_tokens_per_run=500)
            )
        ),
        principal=None,
    )

    assert handle.payload is not None
    assert handle.payload["metrics"]["elapsed_seconds"] == 240.25


def test_rejected_completion_fence_does_not_publish_scores(monkeypatch) -> None:
    class RejectedHandle(_CompletedHandle):
        def complete(self, result, snapshot=None):
            self.terminal_landed = False

    handle = RejectedHandle(total_elapsed=1.0)
    recorded = []
    monkeypatch.setattr("inqtrix.observability.trace_scores.record_run_scores", lambda *a, **k: recorded.append((a, k)))
    execute_run_request(
        handle, algorithm=_CompletedAlgorithm(), run_request=SimpleNamespace(mode="research"),
        resolved=SimpleNamespace(providers=SimpleNamespace(), strategies=SimpleNamespace(), agent_settings=SimpleNamespace()),
        runtime=SimpleNamespace(settings=SimpleNamespace(quota=SimpleNamespace(max_tokens_per_run=0))),
        principal=None,
    )
    assert handle.payload is None
    assert recorded == []


def test_completed_knowledge_result_persists_safe_receipt_and_snapshot() -> None:
    handle = _CompletedHandle(total_elapsed=4.5)

    execute_run_request(
        handle,
        algorithm=_CompletedKnowledgeAlgorithm(),
        run_request=SimpleNamespace(mode="knowledge"),
        resolved=SimpleNamespace(
            providers=SimpleNamespace(),
            strategies=SimpleNamespace(),
            agent_settings=SimpleNamespace(),
        ),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                quota=SimpleNamespace(max_tokens_per_run=500)
            )
        ),
        principal=None,
    )

    assert handle.payload is not None
    assert handle.snapshot is not None
    assert handle.payload["knowledge_profile"] == {
        "id": "standard",
        "requested": "standard",
        "auto_selected": False,
        "degraded_stages": [],
    }
    assert "auto_reason" not in handle.payload["knowledge_profile"]
    assert "quotes" not in handle.payload["knowledge_grounding"]
    assert "queries" not in handle.payload
    assert len(handle.payload["knowledge_retrieval"]["degradations"]) == 1
    assert handle.snapshot["knowledge_retrieval"] == (
        handle.payload["knowledge_retrieval"]
    )
    assert handle.snapshot["knowledge_evidence_used"] == 2


def test_agent_cancelled_exception_terminalizes_as_client_cancel() -> None:
    """An algorithm raising AgentCancelled ends as a CANCELLED run."""
    from inqtrix.exceptions import AgentCancelled
    from inqtrix.execution_failures import terminate_native_run

    handle = _Handle()

    error_type = terminate_native_run(handle, AgentCancelled("stop"))

    assert error_type == "client_requested_cancel"
    assert handle.calls == [("cancel", "client_requested_cancel")]
