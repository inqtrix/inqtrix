"""Custom exceptions for the research agent.

Two domain exceptions (:class:`AgentTimeout`, :class:`AgentRateLimited`)
flag run-level failure modes that downstream code may want to recover
from. The provider-specific :class:`_ProviderAPIError` family wraps SDK
errors with an English header, structured detail parts (status code,
error code, request id, retry-after) and the original exception.

Catch :class:`_ProviderAPIError` to handle any provider failure
uniformly, or one of the concrete subclasses to react per backend.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from inqtrix.providers.base import ChatTurn


class RunNotFound(KeyError):
    """Raised when a run is absent or intentionally indistinguishable.

    Run stores use the same exception for unknown ids and authorization
    denials. Keeping the type below the server layer lets schedulers and
    services distinguish true retention/deletion from infrastructure
    failures without importing FastAPI-facing modules.
    """


class RunCleanupFailed(RuntimeError):
    """External execution data could not be removed; retain its run anchor."""


class AgentTimeout(Exception):
    """Raised when the run exceeds its wall-clock deadline.

    The deadline is :attr:`AgentConfig.max_total_seconds <inqtrix.AgentConfig>`
    (default ``300`` for COMPACT, ``540`` for DEEP). The graph checks
    the deadline at node boundaries; in-flight provider calls may run
    slightly past the deadline before the next check. Callers may
    catch this to surface a partial result or to retry with a longer
    deadline.
    """


class AgentProviderTimeout(AgentTimeout):
    """Raised when one provider request times out before the run deadline.

    Provider-internal attempts already exhausted the one logical-operation
    budget. Agent orchestration therefore records this distinct failure but
    never replays the complete task. The subclass preserves existing library
    callers that catch :class:`AgentTimeout`.
    """


class AgentProviderAttemptsExceeded(RuntimeError):
    """One logical model request has spent its shared provider attempt budget."""


class AgentRateLimited(Exception):
    """Raised on a 429 rate-limit or daily-token-limit response.

    Raised after the Inqtrix-owned three-attempt operation budget is
    exhausted, or immediately for a recognized hard daily cap. Hidden SDK
    retries are disabled. The
    failing model id and the original SDK exception are preserved as
    instance attributes for diagnostics.

    Attributes:
        model: Identifier of the model that triggered the rate limit
            (e.g. ``"gpt-4o"``, ``"claude-sonnet-4-6"``).
        original: The underlying SDK exception that prompted the
            rate-limit classification. Provider modules use this to
            extract retry-after headers and request IDs for logging.
    """

    def __init__(self, model: str, original: Exception):
        """Construct an ``AgentRateLimited`` from the failing model + SDK error.

        Args:
            model: Identifier of the model that surfaced the rate
                limit. Used both as an instance attribute and as part
                of the formatted message.
            original: The underlying SDK exception. Preserved for
                diagnostics; the caller may inspect headers / response
                body via this attribute.
        """
        self.model = model
        self.original = original
        super().__init__(f"Rate limit reached for model '{model}': {original}")


class ModelCapacityReason(str, Enum):
    """Why a provider rejected the requested token capacity."""

    CONTEXT_WINDOW = "context_window"
    OUTPUT_LIMIT = "output_limit"
    UNKNOWN = "unknown"


class AgentModelCapacityError(Exception):
    """Raised when a model cannot satisfy the requested token capacity.

    This exception is intentionally separate from generic provider API
    errors. Context-window and max-output rejections are algorithmically
    relevant for Inqtrix because a normal report would otherwise be built
    from a silently under-powered model configuration.

    Attributes:
        model: Model or deployment identifier that rejected the request.
        phase: Agent phase where the capacity problem appeared.
        original: Original provider exception, when available.
        reason: Proven context/output classification; UNKNOWN is not recoverable as context pressure.
    """

    def __init__(
        self,
        model: str,
        phase: str,
        message: str,
        *,
        original: Exception | None = None,
        reason: ModelCapacityReason = ModelCapacityReason.UNKNOWN,
    ) -> None:
        """Construct a capacity error with model and phase metadata.

        Args:
            model: Provider-specific model or deployment identifier.
            phase: Agent phase such as ``"answer"`` or
                ``"claim_extraction"``.
            message: Human-readable capacity failure detail.
            original: Optional upstream provider exception.
            reason: Specific capacity reason, or UNKNOWN when not established.
        """
        self.model = model
        self.phase = phase
        self.original = original
        self.reason = ModelCapacityReason(reason)
        super().__init__(f"Model capacity failure ({phase}, {model}): {message}")


class AgentStructuredOutputError(Exception):
    """Raised when a structured-output response cannot be consumed.

    Native structured-output APIs validate the model's response against
    a JSON schema before returning it, but provider adapters still have
    to parse the visible response body into Python objects. This error
    keeps those parse/shape failures distinct from generic transport
    failures so callers can surface an algorithmically meaningful
    ``ALGO-FAIL`` without pretending that the provider itself was down.

    Attributes:
        model: Model or deployment identifier that produced the response.
        schema_name: Stable schema name sent to the backend.
        original: Original parse or validation exception, when available.
    """

    def __init__(
        self,
        model: str,
        schema_name: str,
        message: str,
        *,
        original: Exception | None = None,
    ) -> None:
        """Construct a structured-output parse/shape error.

        Args:
            model: Provider-specific model or deployment identifier.
            schema_name: JSON schema name used for the structured call.
            message: Human-readable failure detail.
            original: Optional underlying exception that caused the
                structured response to be rejected locally.
        """
        self.model = model
        self.schema_name = schema_name
        self.original = original
        super().__init__(
            f"Structured output failure ({schema_name}, {model}): {message}"
        )


class AgentToolArgumentsError(AgentStructuredOutputError):
    """A rejected tool batch with its original response and billed usage.

    No call in this batch may execute. The kernel can present the exact calls
    and validation failures back to the same model for a bounded correction.
    Other consumers retain the strict structured-output exception contract.
    """

    def __init__(
        self,
        model: str,
        *,
        turn: ChatTurn,
        raw_tool_calls: tuple[dict[str, Any], ...],
        validation_errors: tuple[str, ...],
        original: Exception | None = None,
    ) -> None:
        self.turn = turn
        self.raw_tool_calls = raw_tool_calls
        self.validation_errors = validation_errors
        super().__init__(
            model,
            "tool_calls",
            "Provider returned invalid tool-call arguments.",
            original=original,
        )


class AgentContextArchiveError(RuntimeError):
    """Context cannot be reduced because its original was not archived."""


class AgentCancelled(Exception):
    """Raised when a run observes its per-run cancel event has been set.

    Used by the implicit-cancel-on-disconnect pathway in the HTTP
    server and by explicit run cancellation: the layer that owns the
    run flips a :class:`threading.Event` carried inside the agent
    state, and the next cancellation checkpoint raises this exception.
    :func:`inqtrix.graph.run` catches it and returns a result marked
    ``cancelled=True`` instead of propagating.

    Checkpoints are: node entry (:func:`inqtrix.state.check_cancel_event`),
    every provider retry attempt and backoff sleep
    (:func:`inqtrix.providers.base.provider_cancel_scope`), the search and
    claim-extraction fan-out coordinators, and the per-section loop of
    answer composition. An in-flight provider HTTP attempt is still not
    aborted mid-request — the residual latency from the request to the
    actual stop is the remainder of that ONE attempt, bounded by its
    transport timeout (full transport-level abort remains a deferred
    design decision, a known open item).
    """

    partial_state: Mapping[str, Any] | None = None
    """Current graph-node state retained when cancellation unwinds a node.

    The graph runner's input is not its evolving node state. Carrying the
    interrupted state preserves recorded provider usage and progress even
    when invocation ends without returning a final state. It belongs to this
    exception instance, never to the cached graph or another run.
    """


class AgentTokenBudgetExceeded(AgentCancelled):
    """Raised when the server-managed per-run token ceiling is reached.

    This remains an :class:`AgentCancelled` subclass so existing graph
    unwinding stays graceful, while callers can distinguish a resource
    failure from an explicit client cancellation without inspecting text.
    """

    def __init__(
        self, message: str, *, usage: dict[str, int] | None = None
    ) -> None:
        """Create a typed budget stop with the consumed usage snapshot.

        Args:
            message: User-safe explanation of the operator limit.
            usage: Cumulative run usage at the boundary that reached the
                limit. Algorithms use it to preserve quota accounting while
                unwinding without a normal node return.
        """
        super().__init__(message)
        self.usage = dict(usage or {})


class AgentPolicyDenied(PermissionError):
    """Raised when an admitted agent action violates an effective policy.

    The dedicated type keeps source/skill denials distinct from arbitrary
    filesystem or implementation ``PermissionError`` instances at the native
    run boundary. It remains a ``PermissionError`` subclass for compatibility
    with existing tool adapters and callers.
    """



class _ProviderAPIError(RuntimeError):
    """Base class for provider-specific API errors.

    Wraps an upstream SDK error with an English header template and
    structured detail parts so logs and re-raises remain
    grep-friendly. Subclasses provide a per-provider header template
    (e.g. ``"Anthropic call failed ({identifier})"``) and a
    matching keyword-only ``__init__`` that maps a provider-specific
    identifier (``model``, ``agent_id``, ``agent_name``) onto the
    shared base.

    Subclass convention:

    - ``_header_template`` is a format string with one ``{identifier}``
      placeholder.
    - ``__init__`` is keyword-only and accepts the provider's natural
      identifier name plus a subset of ``status_code``,
      ``error_code``, ``error_type``, ``message``, ``request_id``,
      ``retry_after``, ``original``.
    - The subclass stores the identifier on a named attribute
      (``self.model = model``) so call-sites can read it without
      parsing the message.

    Attributes:
        status_code: HTTP status code from the upstream response, or
            ``None`` when the failure occurred before a response was
            received.
        error_code: Provider-specific error code string (e.g. Bedrock
            ``"ThrottlingException"``). Empty string when not provided
            by the SDK.
        error_type: Anthropic-style error category (e.g.
            ``"invalid_request_error"``). Empty string when not
            provided.
        request_id: Upstream request correlation id, or ``None``.
            Critical for support escalations to the provider.
        retry_after: Raw ``Retry-After`` header value, or ``None``.
            Provided as string (seconds or HTTP-date) — caller is
            responsible for parsing if needed.
        original: The underlying SDK exception for diagnostics, or
            ``None`` when the error was constructed without an SDK
            origin.
    """

    _header_template: str = "API call failed ({identifier})"

    def __init__(
        self,
        *,
        _identifier: str,
        status_code: int | None = None,
        error_code: str = "",
        error_type: str = "",
        message: str = "",
        request_id: str | None = None,
        retry_after: str | None = None,
        original: Exception | None = None,
    ) -> None:
        """Construct the formatted message and store metadata on the instance.

        Args:
            _identifier: Provider-specific identifier substituted into
                ``_header_template``. Underscored to mark it as
                subclass-internal — direct callers should not pass
                this; subclass ``__init__`` constructors translate
                their public ``model`` / ``agent_id`` / ``agent_name``
                argument into this slot.
            status_code: HTTP status code from the failed response, or
                ``None`` if no response was received.
            error_code: Provider-specific error code (e.g.
                ``"ThrottlingException"``). Empty string when unknown.
            error_type: Provider-specific error category (e.g.
                ``"invalid_request_error"``). Empty string when
                unknown.
            message: Optional human-readable message. When empty, the
                message falls back to ``str(original)`` or a default.
            request_id: Upstream request correlation id, or ``None``.
            retry_after: Raw ``Retry-After`` header value, or ``None``.
            original: The underlying SDK exception. Stored as
                ``self.original`` for diagnostics.
        """
        self.status_code = status_code
        self.error_code = error_code
        self.error_type = error_type
        self.request_id = request_id
        self.retry_after = retry_after
        self.original = original

        detail_parts: list[str] = []
        if status_code is not None:
            detail_parts.append(f"HTTP {status_code}")
        if error_code:
            detail_parts.append(error_code)
        if error_type:
            detail_parts.append(error_type)
        if request_id:
            detail_parts.append(f"request-id={request_id}")
        if retry_after:
            detail_parts.append(f"retry-after={retry_after}")

        header = self._header_template.format(identifier=_identifier)
        if detail_parts:
            header = f"{header} [{' | '.join(detail_parts)}]"

        default_error = f'Unknown {self.__class__.__name__}'
        final_message = message.strip() if message else str(original or default_error)
        super().__init__(f"{header}: {final_message}")


class AnthropicAPIError(_ProviderAPIError):
    """Raised when a direct Anthropic Messages API call fails after retries.

    Surfaced by :class:`~inqtrix.providers.AnthropicLLM` after the
    SDK's own retry budget is exhausted. The failing model name is
    attached as :attr:`model` for routing-aware error handling
    (e.g. fallback to a different deployment).

    Attributes:
        model: Anthropic model id that failed (e.g.
            ``"claude-sonnet-4-6"``).
    """

    _header_template = "Anthropic call failed ({identifier})"

    def __init__(
        self,
        *,
        model: str,
        status_code: int | None = None,
        error_type: str = "",
        message: str = "",
        request_id: str | None = None,
        retry_after: str | None = None,
        original: Exception | None = None,
    ) -> None:
        """Construct an Anthropic-flavoured provider error.

        Args:
            model: Anthropic model id that failed. Stored on
                :attr:`model`.
            status_code: HTTP status from the Anthropic response.
            error_type: Anthropic ``type`` field from the error body
                (e.g. ``"invalid_request_error"``,
                ``"overloaded_error"``).
            message: Human-readable Anthropic message text. When empty,
                falls back to ``str(original)``.
            request_id: Anthropic ``request-id`` header.
            retry_after: ``Retry-After`` header from the response.
            original: The underlying SDK exception for diagnostics.
        """
        self.model = model
        super().__init__(
            _identifier=model,
            status_code=status_code,
            error_type=error_type,
            message=message,
            request_id=request_id,
            retry_after=retry_after,
            original=original,
        )


class BedrockAPIError(_ProviderAPIError):
    """Raised when an Amazon Bedrock Converse API call fails after retries.

    Surfaced by :class:`~inqtrix.providers.BedrockLLM` after the boto3
    client's own retry budget is exhausted. The failing model id is
    attached as :attr:`model`.

    Attributes:
        model: Bedrock model id (e.g.
            ``"eu.anthropic.claude-sonnet-4-6"``) or inference profile
            id that failed.
    """

    _header_template = "Bedrock call failed ({identifier})"

    def __init__(
        self,
        *,
        model: str,
        error_code: str = "",
        status_code: int | None = None,
        message: str = "",
        request_id: str | None = None,
        original: Exception | None = None,
    ) -> None:
        """Construct a Bedrock-flavoured provider error.

        Args:
            model: Bedrock model id or inference-profile id that
                failed. Stored on :attr:`model`.
            error_code: Bedrock-specific error code, typically the
                exception class name from the SDK
                (e.g. ``"ThrottlingException"``,
                ``"ValidationException"``).
            status_code: HTTP status code where available; many
                Bedrock errors do not include one.
            message: Bedrock error message text.
            request_id: ``x-amzn-RequestId`` header value when
                available.
            original: The underlying boto3 / botocore exception.
        """
        self.model = model
        super().__init__(
            _identifier=model,
            status_code=status_code,
            error_code=error_code,
            message=message,
            request_id=request_id,
            original=original,
        )


class AzureOpenAIAPIError(_ProviderAPIError):
    """Raised when an Azure OpenAI Chat Completions / Responses call fails.

    Surfaced by :class:`~inqtrix.providers.AzureOpenAILLM` and the
    Azure-specific search adapters when the OpenAI SDK's own retry
    budget is exhausted. The failing **deployment name** (not the
    underlying model id) is attached as :attr:`model`.

    Attributes:
        model: Azure deployment name that failed (e.g.
            ``"my-gpt4o-deployment"``).
    """

    _header_template = "Azure OpenAI call failed ({identifier})"

    def __init__(
        self,
        *,
        model: str,
        status_code: int | None = None,
        error_code: str = "",
        message: str = "",
        request_id: str | None = None,
        original: Exception | None = None,
    ) -> None:
        """Construct an Azure-OpenAI-flavoured provider error.

        Args:
            model: Azure deployment name that failed. Stored on
                :attr:`model`. Note this is the deployment name, not
                the underlying model id — Azure deployments do not
                expose the model id in the error path.
            status_code: HTTP status from the Azure response.
            error_code: Azure / OpenAI ``code`` field
                (e.g. ``"DeploymentNotFound"``,
                ``"content_filter"``).
            message: Azure ``message`` field text.
            request_id: ``x-ms-request-id`` header value.
            original: The underlying OpenAI SDK exception.
        """
        self.model = model
        super().__init__(
            _identifier=model,
            status_code=status_code,
            error_code=error_code,
            message=message,
            request_id=request_id,
            original=original,
        )


class AzureFoundryWebSearchAPIError(_ProviderAPIError):
    """Raised when an Azure Foundry Web Search (Responses API) call fails.

    Surfaced by :class:`~inqtrix.providers.AzureFoundryWebSearch` when
    the Foundry agent invocation fails. Identified by the **agent
    name** (not the agent id, because Web Search uses the named
    ``agent_reference`` path).

    Attributes:
        agent_name: Foundry agent name that was invoked.
    """

    _header_template = "Azure Foundry web search call failed (agent={identifier})"

    def __init__(
        self,
        *,
        agent_name: str,
        status_code: int | None = None,
        error_code: str = "",
        message: str = "",
        request_id: str | None = None,
        original: Exception | None = None,
    ) -> None:
        """Construct a Foundry-WebSearch-flavoured provider error.

        Args:
            agent_name: Foundry agent name that failed. Stored on
                :attr:`agent_name`.
            status_code: HTTP status from the Foundry response.
            error_code: Foundry / Responses-runtime error code.
            message: Foundry error message text.
            request_id: ``x-ms-request-id`` header value.
            original: The underlying SDK exception.
        """
        self.agent_name = agent_name
        super().__init__(
            _identifier=agent_name,
            status_code=status_code,
            error_code=error_code,
            message=message,
            request_id=request_id,
            original=original,
        )
