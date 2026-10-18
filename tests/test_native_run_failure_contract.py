"""Native child failures retain stable types through the run store."""

from __future__ import annotations

import subprocess
import sys
import time
from types import SimpleNamespace
from typing import Callable

import pytest

from inqtrix.agents.algorithm import _child_task_outcome
from inqtrix.core.results import AgentResult
from inqtrix.exceptions import (
    AgentCancelled,
    AgentPolicyDenied,
    AgentProviderTimeout,
    AgentRateLimited,
    AgentTokenBudgetExceeded,
    AgentTimeout,
    AzureOpenAIAPIError,
)
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.execution_failures import classify_execution_failure
from inqtrix.server.runs import RunStore
from inqtrix.services.run_service import RunService
from inqtrix.settings import AgentSettings


@pytest.mark.parametrize(
    "module",
    ("inqtrix.execution_failures", "inqtrix.worker.loop"),
)
def test_failure_contract_modules_cold_import_without_service_cycle(
    module: str,
) -> None:
    """Worker failure typing must not depend on a lucky import order."""
    completed = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_kernel_loop_ceilings_classify_by_name() -> None:
    """The two kernel loop ceilings map onto stable codes WITHOUT the
    classifier importing langgraph or the kernel middleware (worker
    startup stays free of the optional agent extra)."""

    class GraphRecursionError(RuntimeError):
        pass

    class KernelToolBudgetExceeded(RuntimeError):
        pass

    assert (
        classify_execution_failure(GraphRecursionError("limit"))
        == "iteration_limit"
    )
    assert (
        classify_execution_failure(KernelToolBudgetExceeded("31 > 30"))
        == "tool_budget_exceeded"
    )


def test_explicit_failure_codes_beat_ambiguous_http_and_builtin_types() -> None:
    class StructuredCapabilityFailure(RuntimeError):
        code = "provider_error"
        http_status = 502

    assert (
        classify_execution_failure(StructuredCapabilityFailure())
        == "provider_error"
    )
    assert (
        classify_execution_failure(ValueError("internal bug"))
        == "server_error"
    )
    assert (
        classify_execution_failure(TypeError("internal bug"))
        == "server_error"
    )
    assert (
        classify_execution_failure(TimeoutError("local operation"))
        == "server_error"
    )
    assert (
        classify_execution_failure(AgentPolicyDenied("blocked"))
        == "policy_denied"
    )
    assert (
        classify_execution_failure(AuthorizationRevoked("share revoked"))
        == "authorization_revoked"
    )
    assert issubclass(AgentProviderTimeout, AgentTimeout)
    assert (
        classify_execution_failure(AgentProviderTimeout("request"))
        == "provider_timeout"
    )
    sdk_timeout = type("APITimeoutError", (RuntimeError,), {})()
    assert classify_execution_failure(sdk_timeout) == "provider_timeout"
    wrapped_timeout = AzureOpenAIAPIError(
        model="deployment",
        message="timed out",
        original=sdk_timeout,
    )
    assert classify_execution_failure(wrapped_timeout) == "provider_timeout"
    http_timeout = AzureOpenAIAPIError(
        model="deployment",
        status_code=408,
        message="request timeout",
    )
    assert classify_execution_failure(http_timeout) == "provider_timeout"


class _Registry:
    def __init__(self, algorithm: object) -> None:
        self._algorithm = algorithm

    def get(self, _mode: str) -> object:
        return self._algorithm


class _RaisingAlgorithm:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def run(self, _request, *, runtime, context) -> AgentResult:
        del runtime, context
        raise self._exc


class _ReturnedFailureAlgorithm:
    def run(self, _request, *, runtime, context) -> AgentResult:
        del runtime, context
        return AgentResult(
            answer="Bitte spaeter erneut versuchen.",
            raw={
                "answer": "Bitte spaeter erneut versuchen.",
                "usage": {},
                "result_state": {
                    "_terminal_failure": {
                        "type": "rate_limited",
                        "message": "Provider rate limit exhausted.",
                    }
                },
            },
        )


def _wait_terminal(store: RunStore, run_id: str) -> dict:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        summary = store.get(run_id)
        if summary["status"] in {"completed", "failed", "cancelled"}:
            return summary
        time.sleep(0.01)
    pytest.fail("native child did not reach a terminal state")


def _submit_child(algorithm: object) -> tuple[RunService, dict]:
    store = RunStore(
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=30,
        event_buffer_size=20,
    )
    service = RunService(
        registry=_Registry(algorithm),
        runtime=SimpleNamespace(
            settings=SimpleNamespace(
                quota=SimpleNamespace(max_tokens_per_run=0)
            )
        ),
        run_store=store,
    )
    parent = store.submit(
        question="Parent agent",
        stack_name="default",
        work=lambda handle: handle.wait("waiting_for_input"),
        kind="agent",
    )
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if store.get(parent["run_id"])["status"] == "waiting_for_input":
            break
        time.sleep(0.01)
    else:
        pytest.fail("native parent did not park before child submission")
    summary = service.submit(
        question="Research child",
        history="",
        messages=[],
        resolved=SimpleNamespace(
            mode="research",
            stack_name="",
            agent_overrides={},
            knowledge_filters={},
            providers=SimpleNamespace(),
            strategies=SimpleNamespace(),
            agent_settings=AgentSettings(),
        ),
        workspace_id="workspace-a",
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
        parent_task_id="task-1",
        parent_task_attempt=1,
    )
    return service, _wait_terminal(store, str(summary["run_id"]))


@pytest.mark.parametrize(
    ("exception_factory", "status", "error_type", "retryable"),
    [
        (
            lambda: AgentRateLimited("model-a", RuntimeError("429")),
            "failed",
            "rate_limited",
            False,
        ),
        (
            lambda: AgentProviderTimeout("provider request"),
            "failed",
            "provider_timeout",
            False,
        ),
        (
            lambda: AgentTimeout("run deadline"),
            "failed",
            "run_timeout",
            False,
        ),
        (
            lambda: ConnectionError("connection reset"),
            "failed",
            "temporary_transport",
            False,
        ),
        (
            lambda: AzureOpenAIAPIError(
                model="deployment", status_code=503, message="unavailable"
            ),
            "failed",
            "upstream_5xx",
            False,
        ),
        (
            lambda: AzureOpenAIAPIError(
                model="deployment", status_code=400, message="invalid"
            ),
            "failed",
            "provider_error",
            False,
        ),
        (
            lambda: AgentTokenBudgetExceeded("budget"),
            "failed",
            "token_budget_exceeded",
            False,
        ),
        (
            lambda: AgentCancelled("client"),
            "cancelled",
            "cancelled",
            False,
        ),
    ],
    ids=[
        "rate-limit",
        "timeout",
        "run-timeout",
        "transport",
        "upstream-5xx",
        "provider-4xx",
        "token-budget",
        "client-cancel",
    ],
)
def test_native_child_terminal_type_drives_parent_retry_contract(
    exception_factory: Callable[[], Exception],
    status: str,
    error_type: str,
    retryable: bool,
) -> None:
    service, child = _submit_child(_RaisingAlgorithm(exception_factory()))

    assert child["status"] == status
    if status == "failed":
        assert child["error"]["type"] == error_type
    else:
        assert child["error"] is None

    outcome = _child_task_outcome(
        SimpleNamespace(run_service=service), child["run_id"], 1
    )
    assert outcome is not None
    assert outcome.failure_code == error_type
    assert outcome.transient is retryable


def test_returned_graph_rate_limit_marker_cannot_complete_native_child() -> None:
    service, child = _submit_child(_ReturnedFailureAlgorithm())

    assert child["status"] == "failed"
    assert child["error"]["type"] == "rate_limited"
    outcome = _child_task_outcome(
        SimpleNamespace(run_service=service), child["run_id"], 1
    )
    assert outcome is not None
    assert outcome.failure_code == "rate_limited"
    assert outcome.transient is False
