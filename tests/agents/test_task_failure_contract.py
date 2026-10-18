"""Task-failure contract of the workspace-agent executor.

Locks the regression behind the ``'EU-AI-Act'`` failure: an unresolvable
collection reference is a CONTRACT error — the retry re-fails
identically and spends more operator quota — so it must surface as a
non-transient, human-readable failure instead of the raw KeyError repr
with a retry.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.agents.algorithm import (
    _emit_legacy_budget_notices,
    _emit_task_ended,
    _execute_task,
    _retryable_task_error,
    _task_failure_code,
)
from inqtrix.agents.control_ports import PlanTaskRecord
from inqtrix.agents.scheduler import project_child_run_outcome
from inqtrix.capabilities import CapabilityError
from inqtrix.exceptions import (
    AgentCancelled,
    AgentProviderTimeout,
    AgentTimeout,
    AgentTokenBudgetExceeded,
    RunNotFound,
)
from inqtrix.knowledge.stores.ports import CollectionNotFound


def _task(tool_kind: str, **params) -> PlanTaskRecord:
    return PlanTaskRecord(
        task_id="t1",
        plan_id="plan_x",
        run_id="run_x",
        ordinal=0,
        title="Interne Sammlung sichten",
        tool_kind=tool_kind,
        objective="Bestand pruefen",
        queries=("Welche Dokumente sind relevant?",),
        gap_ids=(),
        depends_on=(),
        budget={},
        params=params,
        expected_output="",
    )


def _deps(assert_collections) -> SimpleNamespace:
    class Control:
        def __init__(self) -> None:
            self.transitions: list[dict[str, object]] = []

        async def transition_plan_task(
            self, **kwargs: object
        ) -> PlanTaskRecord:
            self.transitions.append(kwargs)
            return PlanTaskRecord(
                **{
                    **_task("web_instant").__dict__,
                    "status": str(kwargs["status"]),
                    "child_run_id": kwargs.get("child_run_id"),
                    "result_summary": str(
                        kwargs.get("result_summary") or ""
                    ),
                    "result_payload": dict(
                        kwargs.get("result_payload") or {}
                    ),
                }
            )

    events: list[tuple[str, dict[str, object]]] = []
    return SimpleNamespace(
        emit=lambda event_type, payload: events.append((event_type, payload)),
        control=Control(),
        events=events,
        capabilities=None,
        context=SimpleNamespace(
            principal=None,
            providers=SimpleNamespace(search=None),
            workspace_id=None,
            run_id="run_x",
        ),
        request=SimpleNamespace(knowledge_filters={}),
        knowledge_collection_ids=None,
        visible_to=None,
        runtime=SimpleNamespace(
            registry=SimpleNamespace(get=lambda _mode: SimpleNamespace())
        ),
        assert_collections=assert_collections,
    )


@pytest.mark.parametrize(
    ("language", "reason"),
    [
        (
            "de",
            "Eine ausgewählte Wissenssammlung ist für diese Aufgabe nicht "
            "sichtbar oder unbekannt.",
        ),
        (
            "en",
            "A selected knowledge collection is not visible to this task or "
            "unknown.",
        ),
    ],
)
def test_unknown_collection_fails_non_transient_with_message(
    language: str, reason: str
) -> None:
    """The persisted reason is written in the run's language: a German
    literal at the failure site would bypass the message table and reach an
    English reader as German. It names no collection — the exception's
    internal id belongs in the log, never in the reader's text."""

    def _raise(_ids):
        raise CollectionNotFound("kc_missing")

    outcome = _execute_task(
        _deps(_raise),
        {"language": language},
        _task("rag_query", collection_ids=["kc_missing"]),
        attempt=1,
    )
    assert outcome.status == "failed"
    assert outcome.transient is False
    assert outcome.failure_reason == reason
    assert "kc_" not in outcome.failure_reason


def test_invalid_task_contract_is_not_retried() -> None:
    outcome = _execute_task(
        _deps(lambda ids: ids), {}, _task("no_such_tool"), attempt=1
    )
    assert outcome.status == "failed"
    assert outcome.failure_code == "task_failed"
    assert outcome.transient is False
    second = _execute_task(
        _deps(lambda ids: ids), {}, _task("no_such_tool"), attempt=2
    )
    assert second.transient is False


def test_task_failure_is_sanitized_before_row_checkpoint_and_event() -> None:
    secret = "sk-fakeSecretToken1234567890abcdef"

    class FailingCapabilities:
        async def invoke(
            self, *_args: object, **_kwargs: object
        ) -> object:
            raise RuntimeError(f"provider failed {secret}")

    deps = _deps(lambda ids: ids)
    deps.capabilities = FailingCapabilities()
    task = _task("web_instant")

    outcome = _execute_task(deps, {}, task, attempt=1)
    _emit_task_ended(deps, {"language": "de"}, task, task.task_id, outcome)

    assert secret not in outcome.failure_reason
    assert "[KEY]" in outcome.failure_reason
    assert secret not in str(deps.control.transitions)
    assert secret not in str(deps.events)


def test_token_budget_failure_code_is_distinct_from_client_cancel() -> None:
    assert (
        _task_failure_code(AgentTokenBudgetExceeded("limit"))
        == "token_budget_exceeded"
    )
    assert (
        _task_failure_code(AgentCancelled("client"))
        == "client_requested_cancel"
    )


def test_retry_allowlist_excludes_contract_and_budget_failures() -> None:
    # Provider operations already consumed their own bounded retries. The
    # Agent task layer must never replay the complete task afterwards.
    assert _retryable_task_error(AgentProviderTimeout("temporary")) is False
    assert _retryable_task_error(AgentTimeout("run deadline")) is False
    assert (
        _retryable_task_error(
            CapabilityError("upstream_5xx", "temporary", http_status=503)
        )
        is False
    )
    assert (
        _retryable_task_error(
            CapabilityError("invalid_input", "bad", http_status=400)
        )
        is False
    )
    assert _retryable_task_error(AgentTokenBudgetExceeded("limit")) is False


def test_legacy_task_budget_notice_is_checkpoint_idempotent() -> None:
    """Historic rows stay executable but expose the ignored fallback once."""
    deps = _deps(lambda ids: ids)
    state: dict[str, object] = {}
    task = PlanTaskRecord(
        **{
            **_task("web_research").__dict__,
            "budget": {"max_tokens": 1800, "max_seconds": 60},
        }
    )

    _emit_legacy_budget_notices(deps, state, [task])
    _emit_legacy_budget_notices(deps, state, [task])

    notices = [
        payload
        for event_type, payload in deps.events
        if event_type == "inqtrix.agent.activity"
        and payload.get("operation") == "task.legacy_budget_ignored"
    ]
    assert len(notices) == 1
    assert notices[0]["activity_id"] == "legacy-budget:t1"
    assert notices[0]["fallback"] is True
    assert state["legacy_budget_notice_task_ids"] == ["t1"]


def test_child_projection_only_treats_canonical_not_found_as_missing() -> None:
    class MissingStore:
        def get(self, _run_id: str) -> dict[str, object]:
            raise RunNotFound("child")

    missing = project_child_run_outcome(MissingStore(), "child", 1)
    assert missing is not None
    assert missing.failure_code == "child_row_missing"

    class BrokenStore:
        def get(self, _run_id: str) -> dict[str, object]:
            raise ConnectionError("database unavailable")

    with pytest.raises(ConnectionError, match="database unavailable"):
        project_child_run_outcome(BrokenStore(), "child", 1)


def test_child_projection_carries_the_evidence_budget_cut_as_partial_coverage() -> None:
    """A research child that could not evaluate 21 of its sources arrives at
    the mission as ``coverage == partial`` with the counts — persisted in
    the task payload — instead of as a complete-looking report."""
    from inqtrix.agents.scheduler import task_result_payload

    class Store:
        def get(self, _run_id: str) -> dict[str, object]:
            return {"status": "completed", "parent_task_attempt": 1}

        def result(self, _run_id: str) -> dict[str, object]:
            return {
                "answer": "Bericht mit Luecke [E1]",
                "references": [],
                "metrics": {
                    "evidence_contract_status": "clean",
                    "confidence": 7,
                    "omitted_evidence_record_count": 21,
                    "omitted_evidence_query_count": 3,
                },
            }

    outcome = project_child_run_outcome(Store(), "child", 1)
    assert outcome is not None and outcome.status == "completed"
    assert outcome.coverage == "partial"
    assert (outcome.omitted_evidence_count, outcome.omitted_evidence_query_count) == (21, 3)
    payload = task_result_payload(outcome, persisted_summary=outcome.summary)
    assert payload["omitted_evidence_count"] == 21
    assert payload["omitted_evidence_query_count"] == 3

    class CompleteStore(Store):
        def result(self, _run_id: str) -> dict[str, object]:
            return {"answer": "Bericht [E1]", "references": [], "metrics": {"confidence": 8}}

    complete = project_child_run_outcome(CompleteStore(), "child", 1)
    assert complete is not None and complete.coverage == "complete"
    assert "omitted_evidence_count" not in task_result_payload(
        complete, persisted_summary=complete.summary
    )
