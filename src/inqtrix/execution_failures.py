"""Stable failure typing shared by native runs and agent tasks.

Provider adapters expose several concrete exception families while native
run stores need one small, durable vocabulary.  This module is the single
translation boundary: the in-memory runner, durable worker, and workspace
agent retry policy all consume the same classification instead of each
guessing from exception text.
"""

from __future__ import annotations

from typing import Protocol

from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.exceptions import (
    AgentCancelled,
    AgentContextArchiveError,
    AgentModelCapacityError,
    AgentPolicyDenied,
    AgentProviderTimeout,
    AgentProviderAttemptsExceeded,
    AgentRateLimited,
    AgentTokenBudgetExceeded,
    AgentTimeout,
    AgentToolArgumentsError,
    AgentStructuredOutputError,
    ModelCapacityReason,
    _ProviderAPIError,
)
from inqtrix.urls import sanitize_error


RETRYABLE_AGENT_TASK_ORCHESTRATION_CODES = frozenset(
    {
        "queue_dispatch_failed",
        "child_submit_failed",
        "child_wake_failed",
    }
)
"""Failures eligible for a whole Agent-Desk task retry.

Provider-level timeout, rate-limit, transport, and 5xx errors are absent on
purpose: their provider has already exhausted the shared three-attempt
operation budget, so replaying the complete task would stack retry layers.
"""


_PROVIDER_TIMEOUT_TYPES = frozenset(
    {
        "APITimeoutError",
        "ConnectTimeout",
        "ConnectTimeoutError",
        "ReadTimeout",
        "ReadTimeoutError",
        "TimeoutException",
    }
)
"""Known provider-request timeout types without a shared SDK base."""


_RETRYABLE_TRANSPORT_TYPES = frozenset(
    {
        "APIConnectionError",
        "ConnectError",
        "RemoteProtocolError",
    }
)
"""Known temporary SDK connection failures without a shared base."""


class RunExecutionFailure(RuntimeError):
    """An algorithm result that represents a failed native run.

    Some library-mode algorithms intentionally return a diagnostic answer for
    historical compatibility instead of re-raising their provider exception.
    The native run service lifts that returned marker into this exception so
    every run backend still reaches the same terminal-failure classifier.

    Attributes:
        error_type: Stable machine-readable run error type.
    """

    def __init__(self, error_type: str, message: str) -> None:
        """Construct a typed native terminal failure.

        Args:
            error_type: Stable machine-readable error code persisted on the
                run row.
            message: User-visible diagnostic text; the terminal helper
                sanitizes it before persistence.
        """
        self.error_type = error_type
        super().__init__(message)


class _RunFailureHandle(Protocol):
    """Minimal terminal surface shared by memory and fenced run handles."""

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        """Persist a failed terminal state."""

    def cancel(self, reason: str = "cancelled") -> None:
        """Persist a cancelled terminal state."""


def classify_execution_failure(
    exc: BaseException,
    *,
    fallback: str = "server_error",
) -> str:
    """Map an execution exception onto the stable run/task vocabulary.

    Args:
        exc: Failure raised by a provider, capability, algorithm, or native
            execution adapter.
        fallback: Code for an exception outside the known contract. Native
            runs use ``server_error`` while agent tasks retain their existing
            ``task_failed`` fallback.

    Returns:
        Stable machine-readable failure code.
    """
    if isinstance(exc, RunExecutionFailure):
        return exc.error_type or fallback
    if isinstance(exc, AgentTokenBudgetExceeded):
        return "token_budget_exceeded"
    if isinstance(exc, AgentCancelled):
        return "client_requested_cancel"
    if isinstance(exc, AuthorizationRevoked):
        return AuthorizationRevoked.code
    capability_code = getattr(exc, "code", None)
    capability_status = getattr(exc, "http_status", None)
    if isinstance(capability_code, str) and isinstance(
        capability_status, int
    ):
        # Capability failures intentionally cross the low-level native-run
        # boundary by their stable public fields. Importing the capability
        # package here would pull its service/catalog composition graph into
        # worker startup and create an import cycle back through RunService.
        if capability_status == 429:
            return "rate_limited"
        if capability_code:
            return capability_code
        if capability_status >= 500:
            return "upstream_5xx"
        return "invalid_input"
    if isinstance(exc, AgentRateLimited):
        return "rate_limited"
    if isinstance(exc, AgentProviderTimeout):
        return "provider_timeout"
    if isinstance(exc, AgentTimeout):
        return "run_timeout"
    if isinstance(exc, AgentModelCapacityError):
        return {
            ModelCapacityReason.CONTEXT_WINDOW: "model_context_overflow",
            ModelCapacityReason.OUTPUT_LIMIT: "model_output_limit",
        }.get(exc.reason, "model_capacity")
    if isinstance(exc, AgentToolArgumentsError):
        return "model_tool_arguments"
    if isinstance(exc, AgentStructuredOutputError):
        return "model_response_invalid"
    if isinstance(exc, AgentContextArchiveError):
        return "context_archive_unavailable"
    if isinstance(exc, AgentProviderAttemptsExceeded):
        return "provider_attempts_exhausted"
    if isinstance(exc, AgentPolicyDenied):
        return "policy_denied"
    from inqtrix.provider_failure_contract import ProviderFailureKind, classify_provider_failure

    # HTTP statuses outside an LLM adapter (for example a vector-store bad
    # request) are not evidence of a model-provider failure.
    llm_failure = (
        isinstance(exc, _ProviderAPIError)
        or type(exc).__module__.split(".", 1)[0] in {"openai", "anthropic"}
    )
    failure = classify_provider_failure(exc) if llm_failure else None
    if failure is not None and failure.status_code in {401, 403}:
        return "provider_access_denied"
    if failure is not None and failure.status_code is not None:
        return {
            ProviderFailureKind.TIMEOUT: "provider_timeout",
            ProviderFailureKind.RATE_LIMITED: "rate_limited",
            ProviderFailureKind.UNAVAILABLE: "upstream_5xx",
            ProviderFailureKind.TERMINAL: "provider_error",
        }.get(failure.kind, fallback)
    if isinstance(exc, ConnectionError):
        return "temporary_transport"
    if isinstance(exc, _ProviderAPIError):
        # Status-bearing LLM errors have already been classified above.
        original = getattr(exc, "original", None)
        if (
            original is not None
            and type(original).__name__ in _PROVIDER_TIMEOUT_TYPES
        ):
            return "provider_timeout"
        if (
            original is not None
            and type(original).__name__ in _RETRYABLE_TRANSPORT_TYPES
        ):
            return "temporary_transport"
        return "provider_error"
    # Kernel loop ceilings, matched by NAME: importing langgraph or the
    # kernel middleware here would pull the optional agent extra into
    # every worker startup (and create an import cycle via RunService).
    if type(exc).__name__ == "GraphRecursionError":
        return "iteration_limit"
    if type(exc).__name__ == "KernelToolBudgetExceeded":
        return "tool_budget_exceeded"
    if type(exc).__name__ in _PROVIDER_TIMEOUT_TYPES:
        return "provider_timeout"
    if type(exc).__name__ in _RETRYABLE_TRANSPORT_TYPES:
        return "temporary_transport"
    return fallback


def terminate_native_run(
    handle: _RunFailureHandle,
    exc: BaseException,
) -> str:
    """Persist one exception through the shared native terminal contract.

    Client cancellation remains a cancelled run. Every other exception is a
    failed run carrying the stable type that the parent agent scheduler reads
    from the child summary.

    Args:
        handle: In-memory or fenced durable run handle.
        exc: Exception that terminated execution.

    Returns:
        Stable failure code applied to the terminal transition.
    """
    error_type = classify_execution_failure(exc)
    if error_type == "client_requested_cancel":
        handle.cancel("client_requested_cancel")
    else:
        handle.fail(sanitize_error(exc), error_type=error_type)
        # The run root span is still current here (the worker's finally
        # closes it AFTER this terminal write, without exception info) —
        # mark it so a failed segment never renders as a clean run.
        from inqtrix.observability import semconv
        from inqtrix.observability.otel import (
            enrich_current_span,
            mark_current_span_error,
        )

        mark_current_span_error(error_type)
        enrich_current_span({semconv.INQTRIX_OUTCOME: semconv.OUTCOME_FAILED})
    return error_type
