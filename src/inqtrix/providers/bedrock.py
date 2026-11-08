"""Direct Amazon Bedrock adapter for the LLMProvider interface.

This module calls the Bedrock **Converse** API via ``boto3`` — no
LiteLLM proxy.  The Converse API is AWS's recommended model-agnostic
inference endpoint for Bedrock; it provides structured request/response
formats, native reasoning (thinking) support, and consistent error
handling across all supported models.

Authentication is either a **Bedrock API key** or **AWS credentials**.
An ``api_key`` passed to the constructor signs every call of this provider
as a bearer token, held by the provider's own botocore session. Without
it, boto3 resolves credentials as usual: a named profile, the standard AWS
credential chain (env vars, instance role, etc.), or the API key boto3
itself reads from ``AWS_BEARER_TOKEN_BEDROCK``.

Key design decisions mirror :mod:`inqtrix.providers.anthropic`:

* **Retry with jitter** — Bedrock returns ``ThrottlingException``
  under sustained load.  Each boto3 call runs on the provider lane of the
  process loop and is one attempt of the shared retry loop
  (:func:`~inqtrix.providers.attempts.call_with_retries`) with jittered
  exponential backoff.  boto3 built-in retries are **disabled** to avoid
  double-retry and to allow deadline-aware backoff.  The sync methods are
  faces of the async ones.

* **Per-call reasoning control** — Extended thinking is valuable for
  complex reasoning calls but wastes tokens on short helper calls.  Like
  :class:`AnthropicLLM`, each ``complete*`` call takes a ``reasoning_effort``
  token (``""`` inherits the constructor default, ``"none"`` forces it off,
  a graded level turns it on).

* **Token-budget auto-raise** — Bedrock (Claude models) counts
  thinking tokens inside ``maxTokens``.  The provider auto-raises
  the budget to a safe minimum when thinking is enabled.

Requires ``boto3``::

    uv sync
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from bisect import bisect_right
from collections import OrderedDict
from typing import Any

from inqtrix import aio
from inqtrix.constants import (
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT,
)
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRateLimited,
    AgentRequiredToolCallMissing,
    BedrockAPIError,
)
from inqtrix.providers._bedrock_messages import (
    chat_turn,
    converse_tool_choice,
    to_converse,
    to_converse_tool,
    with_instruction,
)
from inqtrix.providers._claude import (
    THINKING_BINDING_BETA,
    _first_warning,
    model_supports_effort,
    models_rejecting_sampling,
    output_budget,
    thinking_and_effort,
    tool_forcing_allowed,
    with_block_binding,
)
from inqtrix.providers.attempts import (
    AsyncNativeLLMProvider,
    call_with_retries,
    estimate_request_tokens,
)
from inqtrix.providers.base import (
    ChatTurn,
    LLMResponse,
    StructuredLLMResponse,
    _check_deadline,
    _operation_deadline,
    model_capacity_reason,
    extract_usage_tokens,
    cache_usage_details,
    parse_structured_response_content,
)
from inqtrix.providers._schema import strictify_json_schema
from inqtrix.settings import ModelSettings
from inqtrix.state import track_tokens

import boto3  # type: ignore[import-untyped]
import botocore.session  # type: ignore[import-untyped]
from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    ConnectionError as BotoConnectionError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.tokens import FrozenAuthToken  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

API_KEY_ENV = "AWS_BEARER_TOKEN_BEDROCK"
"""The variable boto3 itself reads a Bedrock API key from (process environment only)."""


class _ApiKey:
    """Hands one Bedrock API key to the botocore session of one provider.

    Registered as that session's token provider, it signs the provider's calls
    as a bearer token without touching the process environment, so other AWS
    clients in the process keep their own credentials.
    """

    def __init__(self, key: str) -> None:
        self._token = FrozenAuthToken(key)

    def load_token(self, **kwargs: Any) -> FrozenAuthToken:
        """The key, for whichever service botocore asks it for."""
        return self._token

# ---------------------------------------------------------------------------
# Transient Converse failures
#
# Bedrock returns ThrottlingException under sustained load — especially
# when many parallel claim-extraction calls hit the API at once. These
# error codes and transport failures are the ones the shared retry loop
# repeats (_is_transient); the backoff timing comes from base.py.
# ---------------------------------------------------------------------------
_RETRYABLE_BEDROCK_ERRORS = frozenset({
    "ThrottlingException",
    "ModelTimeoutException",
    "InternalServerException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
})
_RETRYABLE_BEDROCK_TRANSPORT_ERRORS = (
    BotoConnectionError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
    ConnectTimeoutError,
)

# ---------------------------------------------------------------------------
# Transport-client cache
#
# botocore fixes read_timeout at client construction, so honoring a
# deadline at the socket layer requires one client per timeout class.
# Remaining budgets are rounded DOWN onto this fixed rung ladder: the
# socket bound never outlives the deadline, and a continuously shrinking
# outer run budget (agent deep runs) maps onto a handful of reusable
# clients instead of one fresh client — with its own connection pool —
# per whole second of remaining budget. Rungs are denser near the top so
# the down-rounding loss on long default budgets (REASONING_TIMEOUT=600)
# stays within one minute; sub-second rungs only bound how long a doomed
# call may hang. The LRU cap is a backstop that bounds live clients even
# if the ladder grows.
# ---------------------------------------------------------------------------
_TRANSPORT_TIMEOUT_RUNGS: tuple[float, ...] = (
    0.001, 0.01, 0.1, 0.5,
    1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 45.0, 60.0, 90.0, 120.0, 180.0,
    240.0, 300.0, 360.0, 420.0, 480.0, 540.0, 600.0,
)
_MAX_TRANSPORT_CLIENTS = 8

_STRUCTURED_OUTPUT_MODEL_FRAGMENTS: tuple[str, ...] = (
    "claude-fable-5",
    "claude-haiku-4-5",
    "claude-haiku-5",
    "claude-mythos",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-opus-5",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-sonnet-5",
)



def _model_supports_structured_output(model: str) -> bool:
    """Return True when the Bedrock model id advertises JSON-schema output."""
    if not model:
        return False
    lowered = model.lower()
    return any(
        fragment in lowered for fragment in _STRUCTURED_OUTPUT_MODEL_FRAGMENTS
    )


def _client_error_code(exc: BaseException) -> str:
    """The Converse error code of a boto3 ``ClientError``, else ``""``."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str((response.get("Error") or {}).get("Code") or "")


def _is_transient(exc: BaseException) -> bool:
    """Whether a failed Converse call is worth another attempt."""
    if isinstance(exc, ClientError):
        return _client_error_code(exc) in _RETRYABLE_BEDROCK_ERRORS
    return isinstance(exc, _RETRYABLE_BEDROCK_TRANSPORT_ERRORS)


def _retry_error_code(exc: BaseException) -> str:
    """The Converse error code for a retry notice, else the exception name."""
    return _client_error_code(exc) or type(exc).__name__


def _retry_request_id(exc: BaseException) -> str:
    """The request-id of a failed Converse call, for its retry notice."""
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return ""
    return str((response.get("ResponseMetadata") or {}).get("RequestId") or "")


class BedrockLLM(
    AsyncNativeLLMProvider,
):
    """Call Amazon Bedrock Converse directly via boto3.

    Use this provider when Claude or other supported models should run on
    Amazon Bedrock rather than on direct Anthropic or LiteLLM-backed
    infrastructure. It is the right choice when AWS regions, named
    profiles, or Bedrock-specific access controls are operational
    requirements.

    Attributes:
        _default_model (str): Primary Bedrock model ID for reasoning
            calls.
        _claim_extract_model (str): Bedrock model ID used for claim extraction.
        _default_max_tokens (int): Output-token budget for reasoning
            requests before thinking-related auto-raise.
        _temperature (float | None): Optional sampling temperature.
        _thinking (dict[str, Any] | None): Extended-thinking config applied
            on reasoning calls when the per-call ``reasoning_effort`` inherits
            the constructor default.
        _models (ModelSettings): Effective role-to-model mapping exposed
            to the runtime.
        _client (Any): boto3 Bedrock Runtime client configured without
            built-in retries.
    """

    complete_uses_metadata: bool = True

    def __init__(
        self,
        *,
        api_key: str | None = None,
        profile_name: str | None = None,
        region_name: str = "eu-central-1",
        default_model: str = "",
        classify_model: str = "",
        claim_extract_model: str = "",
        evaluate_model: str = "",
        plan_model: str = "",
        answer_model: str = "",
        direct_chat_model: str = "",
        tier_high_model: str = "",
        tier_mid_model: str = "",
        tier_fast_model: str = "",
        tier_high_effort: str = "",
        tier_mid_effort: str = "",
        tier_fast_effort: str = "",
        default_max_tokens: int = DEFAULT_LLM_MAX_OUTPUT_TOKENS,
        context_window_tokens: int | None = None,
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
        selectable_models: list[str] | None = None,
        timeout: float = REASONING_TIMEOUT,
    ) -> None:
        """Initialize the Bedrock provider.

        Use the constructor when Bedrock should handle reasoning calls via
        the Converse API. The provider disables boto3 retries and runs its
        own deadline-aware retry loop, so constructor-level choices mainly
        decide credentials, region, model roles, and token budgets.

        Args:
            api_key: Optional Bedrock API key. Set, every call of this
                provider carries it as a bearer token instead of being
                signed with AWS credentials; the process environment stays
                untouched. Without it, boto3 still signs with
                ``AWS_BEARER_TOKEN_BEDROCK`` when that variable is set in the
                process environment. A blank key, here or in that variable,
                counts as none.
            profile_name: Optional AWS named profile from
                ``~/.aws/config`` or ``~/.aws/credentials``. When omitted,
                boto3 falls back to the default AWS credential chain.
            region_name: AWS region for the Bedrock endpoint. The default
                is ``"eu-central-1"``.
            default_model: Primary Bedrock model ID for classify, plan,
                evaluate fallback, and answer calls.
            classify_model: Optional cheaper override for classification.
                When omitted, classification falls back to
                ``default_model``.
            claim_extract_model: Bedrock model ID used for claim extraction.
            evaluate_model: Optional override for evidence evaluation.
                When omitted, evaluation falls back to ``default_model``.
            plan_model: Optional per-node model override for the plan node.
            answer_model: Optional per-node model override for the answer node.
            direct_chat_model: Optional per-node model override for the
                skip-search direct-chat node.
            tier_high_model: Model for the high tier (answer by default).
            tier_mid_model: Model for the mid tier (plan/evaluate/direct_chat).
            tier_fast_model: Model for the fast tier (classify/claim_extract).
                Nodes map to a tier via
                ``inqtrix.model_routing.NODE_TIER_ASSIGNMENT``; empty tiers and
                per-node overrides both fall back to ``default_model``.
            tier_high_effort: Per-tier reasoning effort for the high tier.
            tier_mid_effort: Per-tier reasoning effort for the mid tier.
            tier_fast_effort: Per-tier reasoning effort for the fast tier.
                ``""`` inherits the constructor ``effort``/``thinking``,
                ``"none"`` forces reasoning off, and a graded level enables it.
            default_max_tokens: Output-token budget for reasoning calls
                before any thinking-related auto-raise.
            context_window_tokens: Known context-window size for the
                Bedrock model. ``None`` means unknown capacity.
            temperature: Optional sampling temperature. Do not set this
                together with ``thinking`` because Bedrock Claude models
                reject that combination.
            thinking: Optional Bedrock thinking configuration forwarded via
                ``additionalModelRequestFields``.
            effort: Optional ``effort`` value (``"low"``, ``"medium"``,
                ``"high"``, ``"xhigh"``, ``"max"``). Forwarded to Bedrock
                via ``additionalModelRequestFields["output_config"]``.
                Controls overall token spend (text + tool calls + thinking).
                Works with or without ``thinking`` enabled. ``"xhigh"`` is
                only supported on Claude Opus 4.7+. The Anthropic API
                default is ``"high"`` (= same as omitting the parameter).
            timeout: Socket-read and logical-operation timeout in seconds.
                Retries share this one budget.

        Raises:
            ValueError: If both ``temperature`` and ``thinking`` are set, if
                both ``api_key`` and ``profile_name`` are set, or if
                ``effort`` is not one of the accepted values.

        Example:
            >>> from inqtrix import BedrockLLM
            >>> llm = BedrockLLM(
            ...     profile_name="default",
            ...     region_name="eu-central-1",
            ...     default_model="eu.anthropic.claude-sonnet-4-6",
            ... )
            >>> llm.models.reasoning_model
            'eu.anthropic.claude-sonnet-4-6'
        """
        if temperature is not None and thinking is not None:
            raise ValueError(
                "temperature and thinking are mutually exclusive — "
                "the Bedrock API (Claude models) rejects requests that set both."
            )
        api_key = (api_key or "").strip() or None
        if api_key and profile_name:
            raise ValueError(
                "api_key and profile_name are mutually exclusive: a Bedrock API "
                "key replaces the AWS credentials of a profile."
            )
        if temperature is not None:
            rejecting = models_rejecting_sampling([
                default_model, classify_model, claim_extract_model, evaluate_model,
                plan_model, answer_model, direct_chat_model, tier_high_model,
                tier_mid_model, tier_fast_model, *(selectable_models or []),
            ])
            if rejecting:
                raise ValueError(
                    "temperature is not accepted by "
                    + ", ".join(rejecting)
                    + "; leave INQTRIX_TEMPERATURE unset for these models."
                )
        if effort is not None and effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                "effort must be one of 'low', 'medium', 'high', 'xhigh', or 'max'."
            )
        self._default_model = default_model
        # Store the raw arg (empty when unset) so the central tier router can
        # fall through claim_extract -> fast tier -> reasoning_model. Eagerly
        # pinning it to default_model here would shadow the fast tier and force
        # the expensive reasoning model on the highest-volume node.
        self._claim_extract_model = claim_extract_model
        self._default_max_tokens = default_max_tokens
        self._context_window_tokens = context_window_tokens
        self._selectable_models = list(selectable_models or [])
        self._temperature = temperature
        self._thinking = thinking
        self._effort = effort
        self._timeout = float(timeout)
        self._models = ModelSettings(
            reasoning_model=default_model,
            search_model="",
            classify_model=classify_model,
            claim_extract_model=self._claim_extract_model,
            evaluate_model=evaluate_model,
            plan_model=plan_model,
            answer_model=answer_model,
            direct_chat_model=direct_chat_model,
            tier_high_model=tier_high_model,
            tier_mid_model=tier_mid_model,
            tier_fast_model=tier_fast_model,
            tier_high_effort=tier_high_effort,
            tier_mid_effort=tier_mid_effort,
            tier_fast_effort=tier_fast_effort,
        )

        # Disable boto3 built-in retries — we run our own retry loop
        # with deadline enforcement and jittered backoff.
        self._uses_api_key = bool(api_key)
        # boto3 signs with AWS_BEARER_TOKEN_BEDROCK whenever the variable is
        # present, even blank, and Bedrock refuses a blank key. Such a
        # variable counts as unset here, as it does in the settings.
        self._blank_env_key = (
            not api_key
            and API_KEY_ENV in os.environ
            and not os.environ[API_KEY_ENV].strip()
        )
        if api_key:
            core = botocore.session.Session()
            core.register_component("token_provider", _ApiKey(api_key))
            session = boto3.Session(botocore_session=core, region_name=region_name)
        else:
            session = boto3.Session(
                profile_name=profile_name,
                region_name=region_name,
            )
        self._session = session
        self._client_lock = threading.Lock()
        self._clients_by_read_timeout: OrderedDict[float, Any] = OrderedDict()
        self._client = self._build_client(self._timeout)
        self._clients_by_read_timeout[self._timeout] = self._client

        # Surface effort/model incompatibilities before the first call, like
        # AnthropicLLM. Drained by classify on the first run.
        self._effort_config_warnings: list[str] = self._collect_effort_warnings(
            default_model=default_model,
            classify_model=classify_model,
            claim_extract_model=self._claim_extract_model,
            evaluate_model=evaluate_model,
        )
        for warning in self._effort_config_warnings:
            log.warning(warning)
        self._log_authentication(profile=profile_name, region_name=region_name)

    def _log_authentication(self, *, profile: str | None, region_name: str) -> None:
        """Say once how this provider's calls authenticate, never with the key."""
        if self._uses_api_key:
            log.info(
                "BedrockLLM: Bedrock calls authenticate with the configured API key (region=%s).",
                region_name,
            )
            return
        if self._blank_env_key:
            log.warning(
                "CONFIG: BedrockLLM: AWS_BEARER_TOKEN_BEDROCK is set but empty, so Bedrock "
                "calls authenticate through the AWS credential chain instead "
                "(profile=%s, region=%s). Remove the variable or give it a key.",
                profile or "-",
                region_name,
            )
            return
        if os.environ.get(API_KEY_ENV):
            if profile:
                log.warning(
                    "CONFIG: BedrockLLM: AWS_BEARER_TOKEN_BEDROCK is set, so boto3 signs "
                    "Bedrock calls with that API key; profile %s is not used for them.",
                    profile,
                )
            log.info(
                "BedrockLLM: Bedrock calls authenticate with the API key from "
                "AWS_BEARER_TOKEN_BEDROCK (region=%s).",
                region_name,
            )
            return
        log.info(
            "BedrockLLM: Bedrock calls authenticate through the AWS credential chain "
            "(profile=%s, region=%s).",
            profile or "-",
            region_name,
        )

    def _build_client(self, read_timeout: float) -> Any:
        """Build one retry-free transport client for a deadline-bound class.

        With an API key the client prefers bearer authentication, which the
        session's token provider answers with that key. With a blank
        ``AWS_BEARER_TOKEN_BEDROCK`` it prefers SigV4, so boto3 does not send
        the blank value as a key.
        """
        preference = (
            {"auth_scheme_preference": "httpBearerAuth"} if self._uses_api_key
            else {"auth_scheme_preference": "sigv4"} if self._blank_env_key
            else {}
        )
        return self._session.client(
            "bedrock-runtime",
            config=BotoConfig(
                retries={"max_attempts": 0},
                connect_timeout=read_timeout,
                read_timeout=read_timeout,
                **preference,
            ),
        )

    def _client_for_deadline(self, deadline: float) -> tuple[Any, float]:
        """Return a client whose socket timeout cannot outlive *deadline*.

        Botocore fixes ``read_timeout`` at client construction. A single
        provider-wide client therefore cannot honor a shorter request override
        or a nearly exhausted outer run budget. The remaining budget is
        rounded down onto :data:`_TRANSPORT_TIMEOUT_RUNGS`, so the transport
        bound stays honest while a shrinking deadline reuses a bounded set of
        clients instead of building one per whole second. Budgets above the
        top rung are capped at the top rung: still deadline-honest, at the
        cost of limiting one transport attempt to that rung. The cache evicts
        least-recently-used clients beyond :data:`_MAX_TRANSPORT_CLIENTS`;
        ``self._client`` stays referenced for availability checks even when
        its cache entry is evicted.

        Args:
            deadline: Absolute monotonic deadline the socket timeout must
                not outlive.

        Returns:
            A ``(client, read_timeout)`` tuple where ``read_timeout`` is the
            rung actually configured on the returned client's transport.
        """
        remaining = max(0.001, deadline - time.monotonic())
        rung_index = bisect_right(_TRANSPORT_TIMEOUT_RUNGS, remaining) - 1
        read_timeout = _TRANSPORT_TIMEOUT_RUNGS[max(0, rung_index)]
        with self._client_lock:
            client = self._clients_by_read_timeout.get(read_timeout)
            if client is None:
                client = self._build_client(read_timeout)
                self._clients_by_read_timeout[read_timeout] = client
            else:
                self._clients_by_read_timeout.move_to_end(read_timeout)
            while len(self._clients_by_read_timeout) > _MAX_TRANSPORT_CLIENTS:
                self._clients_by_read_timeout.popitem(last=False)
        return client, read_timeout

    def _collect_effort_warnings(
        self,
        *,
        default_model: str,
        classify_model: str,
        claim_extract_model: str,
        evaluate_model: str,
    ) -> list[str]:
        if self._effort is None:
            return []
        warnings: list[str] = []
        roles: list[tuple[str, str]] = [
            ("default_model (reasoning/answer)", default_model),
            ("classify_model", classify_model),
            ("claim_extract_model", claim_extract_model),
            ("evaluate_model", evaluate_model),
        ]
        for role, model in roles:
            if not model:
                continue
            if model_supports_effort(model):
                continue
            warnings.append(
                f"BedrockLLM: effort='{self._effort}' is not sent for {role}='{model}' "
                "because this Bedrock model does not accept output_config.effort "
                "(Haiku class). Other roles with effort-capable models "
                "(Opus/Sonnet 4.6+) still receive effort. Set effort=None "
                "or choose an effort-capable model to resolve this mismatch."
            )
        return warnings

    def consume_effort_config_warnings(self) -> list[str]:
        """Return and clear the buffered effort-config warnings.

        Side effects:
            Clears the internal warning buffer so each warning is
            surfaced exactly once. Subsequent calls return an empty
            list until new ``effort=`` mismatches are detected (e.g.
            after rotating in a Haiku-class claim-extraction model).

        Returns:
            The list of buffered English diagnostic warning strings,
            in the order they were detected. Each entry describes a
            specific role-to-model combination where the configured
            ``effort`` parameter is silently dropped because the
            target model does not accept ``output_config.effort``
            (Haiku-class). Empty list when no mismatch has been
            detected yet.
        """
        out = list(self._effort_config_warnings)
        self._effort_config_warnings = []
        return out

    @property
    def models(self) -> ModelSettings:
        """Return the effective role-to-model mapping for the runtime.

        Returns:
            ModelSettings: Resolved Bedrock model IDs used by graph nodes.
        """
        return self._models

    @property
    def selectable_models(self) -> list[str]:
        """Return the operator-curated model ids offered for direct selection."""
        return self._selectable_models

    @property
    def context_window_tokens(self) -> int | None:
        """Return the configured context window for capacity checks."""
        return self._context_window_tokens

    @staticmethod
    def _extract_error_details(exc: ClientError) -> dict[str, Any]:
        """Extract structured error information from a boto3 ClientError."""
        response = getattr(exc, "response", None) or {}
        error = response.get("Error", {})
        metadata = response.get("ResponseMetadata", {})
        return {
            "error_code": error.get("Code", ""),
            "message": error.get("Message", ""),
            "status_code": metadata.get("HTTPStatusCode"),
            "request_id": metadata.get("RequestId"),
        }

    @staticmethod
    def _build_api_error(
        *,
        model: str,
        details: dict[str, Any] | None = None,
        message: str = "",
        original: Exception,
    ) -> BedrockAPIError:
        details = details or {}
        sc = details.get("status_code")
        return BedrockAPIError(
            model=model,
            error_code=str(details.get("error_code") or "").strip(),
            status_code=sc if isinstance(sc, int) else None,
            message=message.strip() or str(details.get("message") or "").strip() or str(original),
            request_id=str(details.get("request_id") or "").strip() or None,
            original=original,
        )

    async def _aconverse(
        self,
        *,
        params: dict[str, Any],
        timeout: float,
        deadline: float | None = None,
        operation: str = "converse",
    ) -> dict[str, Any]:
        """Call Bedrock Converse through the shared retry loop.

        boto3 is sync: each request runs on the provider lane of the process
        loop and is one attempt of the shared retry loop, which repeats the
        transient Converse errors (``ThrottlingException``,
        ``ServiceUnavailableException``, ...) and transport failures within
        one operation deadline. The client's ``read_timeout`` bounds the
        thread; the attempt's deadline ends the wait even while the thread
        still runs. A ``ThrottlingException`` that outlasts its attempts
        becomes ``AgentRateLimited``.
        """
        use_model = str(params.get("modelId") or self._default_model)
        operation_deadline = _operation_deadline(timeout, deadline)

        async def send() -> dict[str, Any]:
            client, _read_timeout = self._client_for_deadline(operation_deadline)
            response = await aio.run_provider_blocking(client.converse, **params)
            return response if isinstance(response, dict) else {}

        try:
            # The requests run on the provider lane of the process loop. A
            # caller on a loop of its own (a library user's asyncio, a
            # notebook) hands the whole call over, as every provider without
            # an async client does.
            return await aio.on_process_loop(call_with_retries(
                send,
                provider_label="Bedrock",
                model=use_model,
                operation=operation,
                deadline=operation_deadline,
                outer_deadline=deadline,
                timeout_label="Bedrock call",
                configured_timeout_seconds=timeout,
                error_code_for=_retry_error_code,
                request_id_for=_retry_request_id,
                transient_for=_is_transient,
                estimated_input_tokens=estimate_request_tokens(params),
                max_output_tokens=int(params.get("inferenceConfig", {}).get("maxTokens") or 0) or None,
            ))
        except ClientError as exc:
            details = self._extract_error_details(exc)
            if details.get("error_code") == "ThrottlingException":
                raise AgentRateLimited(use_model, exc) from exc
            api_error = self._build_api_error(
                model=use_model, details=details, original=exc)
            if (capacity_reason := model_capacity_reason(api_error)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, error_type=%s)",
                    use_model,
                    type(api_error).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(api_error),
                    reason=capacity_reason,
                    original=api_error,
                ) from exc
            raise api_error from exc
        except _RETRYABLE_BEDROCK_TRANSPORT_ERRORS as exc:
            raise self._build_api_error(
                model=use_model,
                details={"error_code": type(exc).__name__},
                original=exc,
            ) from exc

    # ------------------------------------------------------------------ #
    # Response extraction
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_text(response: dict[str, Any]) -> str:
        """Extract visible text from a Bedrock Converse response.

        Collects only ``{"text": ...}`` content blocks — reasoning
        (thinking) content blocks are intentionally skipped.
        """
        parts: list[str] = []
        output = response.get("output", {})
        message = output.get("message", {}) if isinstance(output, dict) else {}
        content = message.get("content", []) if isinstance(message, dict) else []
        for block in content or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_usage(response: dict[str, Any]) -> tuple[int, int]:
        prompt_tokens, completion_tokens = extract_usage_tokens(
            response,
            input_keys=("inputTokens",),
            output_keys=("outputTokens",),
        )
        details = BedrockLLM._cache_usage(response)
        return prompt_tokens + sum(value or 0 for value in details.values()), completion_tokens

    @staticmethod
    def _cache_usage(payload: dict[str, Any]) -> dict[str, int | None]:
        # Converse inputTokens excludes cache reads and writes.
        return cache_usage_details(
            payload.get("usage"), read_key="cacheReadInputTokens",
            creation_key="cacheWriteInputTokens",
        )

    # ------------------------------------------------------------------ #
    # LLMProvider interface
    # ------------------------------------------------------------------ #

    def _call_thinking_and_effort(
        self, reasoning_effort: str | None, *, use_model: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve the (thinking, effort) to apply for a single Converse call.

        Same contract as the Anthropic provider: ``None``/``""`` inherits the
        constructor ``self._thinking`` / ``self._effort``; ``"none"`` forces
        reasoning off; a graded level turns on adaptive thinking and sets
        ``output_config`` effort where the model accepts it (Haiku class gets
        adaptive thinking without an effort cap and a visible warning).

        Args:
            reasoning_effort: The per-call effort override.
            use_model: The effective Bedrock model id for this call.

        Returns:
            A ``(thinking, effort)`` tuple destined for
            ``additionalModelRequestFields``.
        """
        return thinking_and_effort(
            reasoning_effort,
            model=use_model,
            inherit_thinking=self._thinking,
            inherit_effort=self._effort,
            label="BedrockLLM",
        )

    @staticmethod
    def _model_request_fields(
        thinking_cfg: dict[str, Any] | None,
        effort_value: str | None,
        *,
        beta: str | None = None,
    ) -> dict[str, Any]:
        """The ``additionalModelRequestFields`` entry of one call, or ``{}``.

        Converse routes Claude's own request fields through this dict: ``thinking``,
        ``output_config`` with the effort, and ``anthropic_beta``. The per-call
        resolver already dropped effort for models that reject it (Haiku class)
        and for none / inherit-without-effort calls.
        See https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
        """
        fields: dict[str, Any] = {}
        if thinking_cfg is not None:
            fields["thinking"] = thinking_cfg
        if effort_value is not None:
            fields["output_config"] = {"effort": effort_value}
        if beta is not None:
            fields["anthropic_beta"] = [beta]
        return {"additionalModelRequestFields": fields} if fields else {}

    async def acomplete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        """Generate text through Bedrock and discard token metadata.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction.
            model: Optional per-call Bedrock model override. When omitted,
                the provider uses ``self._default_model``.
            timeout: Per-call timeout budget in seconds.
            state: Optional mutable agent state for token tracking.
            deadline: Optional absolute monotonic deadline for the full
                run.

        Returns:
            str: Visible assistant text extracted from the Converse
            response.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Bedrock throttles the request fatally.
            BedrockAPIError: If a non-retryable Bedrock error occurs.
        """
        response = await self.acomplete_with_metadata(
            prompt,
            system=system,
            model=model,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            state=state,
            deadline=deadline,
            reasoning_effort=reasoning_effort,
        )
        return response.content

    async def acomplete_with_metadata(
        self,
        prompt: str,
        *,
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Generate text and token metadata through Bedrock Converse.

        Use this method for reasoning calls when the runtime wants both
        visible content and token usage. When thinking is enabled, the
        method raises ``maxTokens`` so hidden reasoning does not crowd out
        the visible answer. Request execution itself is delegated to
        ``_aconverse()``, whose calls are attempts of the shared retry loop
        for throttling and transient Bedrock failures.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction.
            model: Optional per-call model override. When omitted, the
                provider uses ``self._default_model``.
            timeout: Per-call timeout budget in seconds. Bedrock uses the
                provider's retry loop instead of a dedicated per-request
                timeout field.
            state: Optional mutable agent state that receives token counts
                through ``track_tokens()`` when provided.
            deadline: Optional absolute monotonic deadline for the full
                run.

        Returns:
            LLMResponse: Structured response containing visible content,
            token counts, and the effective model label.

        Raises:
            AgentTimeout: If the full run deadline has already elapsed.
            AgentRateLimited: If Bedrock throttles the request after retry
                exhaustion.
            BedrockAPIError: If a non-retryable Bedrock or transport error
                occurs.
        """
        if deadline is not None:
            _check_deadline(deadline)

        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        max_tokens = output_budget(max_output_tokens or self._default_max_tokens, thinking_cfg)

        params: dict[str, Any] = {
            "modelId": use_model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            params["system"] = [{"text": system}]
        if self._temperature is not None:
            params["inferenceConfig"]["temperature"] = self._temperature
        params.update(self._model_request_fields(thinking_cfg, effort_value))

        raw = await self._aconverse(
            params=params,
            timeout=timeout,
            deadline=deadline,
            operation="complete",
        )

        prompt_tokens, completion_tokens = self._extract_usage(raw)
        response = LLMResponse(
            content=self._extract_text(raw),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            **self._cache_usage(raw),
            model=use_model,
            finish_reason=str(raw.get("stopReason") or raw.get("stop_reason") or ""),
            raw=raw,
            request_max_tokens=int(
                params.get("inferenceConfig", {}).get("maxTokens") or 0
            ),
        )
        if state is not None:
            track_tokens(state, response)
        return response

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Whether the kernel can run on *model*: Converse tool use, mapped for Claude.

        Converse takes tools for other model families too, but this adapter maps
        only Claude's request rules and reasoning blocks, so on any other model the
        kernel reports itself unavailable instead of failing mid-run. An application
        inference profile ARN does not name its model and counts as another model.
        """
        return "claude" in (model or self._default_model).lower()

    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
        tool_choice: str | None = None,
    ) -> ChatTurn:
        """Run one tool-calling turn through Converse.

        The OpenAI-shaped conversation becomes the Converse shape (``system``
        blocks, ``toolUse`` in the assistant turn, every tool result of a turn as a
        ``toolResult`` block in one user message); Claude's reasoning blocks go
        back unchanged with the turn that produced them. ``tool_choice``
        ``required`` forces a tool where the model and the request's thinking allow
        it; elsewhere the turn gets ``auto`` and an instruction in the system
        prompt, and a text answer raises
        :class:`~inqtrix.exceptions.AgentRequiredToolCallMissing` for the kernel's
        bounded correction. Converse has no ``none`` choice: such a turn gets
        ``auto`` with one WARNING per model, and the kernel's admission still
        refuses a call it does not allow. On a model whose thinking blocks are
        bound to an unchanged prefix, a request that returns blocks asks Bedrock to
        drop a block whose prefix changed.

        Raises:
            AgentTimeout: The run deadline has passed.
            AgentRateLimited: Bedrock kept throttling the request.
            BedrockAPIError: The call failed after retrying.
            AgentStructuredOutputError: A guardrail or a content filter stopped the
                answer, or Bedrock could not read it.
            AgentToolArgumentsError: The output limit stopped the turn while it
                wrote tool calls, or a tool call's input is no JSON object.
            AgentRequiredToolCallMissing: A turn that had to call a tool answered in text.
            ValueError: The conversation has no Converse form.
        """
        if deadline is not None:
            _check_deadline(deadline)
        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        max_tokens = output_budget(max_output_tokens or self._default_max_tokens, thinking_cfg)
        system, wire_messages, returns_reasoning = to_converse(messages)
        tool_names = [str((tool.get("function") or tool).get("name") or "") for tool in tools or []]
        choice, instruction, none_as_auto = converse_tool_choice(
            tool_choice,
            tool_names=tool_names,
            forced_allowed=tool_forcing_allowed(use_model, thinking_cfg),
        )
        if none_as_auto and _first_warning("BedrockLLM", use_model, "tool_choice_none"):
            log.warning(
                "CONFIG: BedrockLLM: Converse has no tool_choice 'none'; "
                "model=%s gets 'auto' on such turns.",
                use_model,
            )
        if instruction:
            system = with_instruction(system, instruction)
        thinking_cfg, bound = with_block_binding(
            thinking_cfg, model=use_model, returns_thinking=returns_reasoning
        )
        params: dict[str, Any] = {
            "modelId": use_model,
            "messages": wire_messages,
            "inferenceConfig": {"maxTokens": max_tokens},
        }
        if system:
            params["system"] = system
        if tools:
            params["toolConfig"] = {"tools": [to_converse_tool(tool) for tool in tools]}
            if choice is not None:
                params["toolConfig"]["toolChoice"] = choice
        if self._temperature is not None:
            params["inferenceConfig"]["temperature"] = self._temperature
        params.update(self._model_request_fields(
            thinking_cfg, effort_value, beta=THINKING_BINDING_BETA if bound else None
        ))

        raw = await self._aconverse(
            params=params, timeout=timeout, deadline=deadline, operation="chat"
        )
        prompt_tokens, completion_tokens = self._extract_usage(raw)
        turn = chat_turn(
            raw,
            model=use_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache=self._cache_usage(raw),
        )
        if instruction and not turn.tool_calls:
            raise AgentRequiredToolCallMissing(
                use_model, turn=turn, tool_names=tuple(tool_names)
            )
        return turn

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Return whether the selected Bedrock model supports JSON schemas.

        Args:
            model: Optional model override. When omitted, the provider's
                default reasoning model is checked.

        Returns:
            ``True`` for Bedrock Claude model ids documented to support
            Converse ``outputConfig.textFormat`` structured outputs.
        """
        return _model_supports_structured_output(model or self._default_model)

    async def acomplete_structured(
        self,
        prompt: str,
        *,
        schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        system: str | None = None,
        model: str | None = None,
        max_output_tokens: int | None = None,
        timeout: float = REASONING_TIMEOUT,
        state: dict | None = None,
        deadline: float | None = None,
        reasoning_effort: str | None = None,
    ) -> StructuredLLMResponse:
        """Generate a JSON-schema-constrained Bedrock Converse response.

        Args:
            prompt: User-facing input text.
            schema: JSON Schema object sent through
                ``outputConfig.textFormat``.
            schema_name: Stable schema name used by Bedrock grammar
                caching and local diagnostics.
            schema_description: Optional schema purpose shown to Bedrock.
            system: Optional system instruction.
            model: Optional Bedrock model override.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout accepted for interface parity; the
                Bedrock client uses its configured read timeout and the
                shared retry loop.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute monotonic deadline.

        Returns:
            StructuredLLMResponse with parsed top-level JSON object.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Bedrock throttles the request fatally.
            BedrockAPIError: If a non-retryable Bedrock error occurs.
            AgentStructuredOutputError: If the visible structured JSON
                cannot be parsed into a dictionary.
        """
        if deadline is not None:
            _check_deadline(deadline)

        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        max_tokens = output_budget(max_output_tokens or self._default_max_tokens, thinking_cfg)

        params: dict[str, Any] = {
            "modelId": use_model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens},
            "outputConfig": {
                "textFormat": {
                    "type": "json_schema",
                    "structure": {
                        "jsonSchema": {
                            # Adapt the canonical schema to the strict json_schema
                            # contract (every property required, no `default`) —
                            # the provider owns its endpoint's schema shape.
                            "schema": json.dumps(
                                strictify_json_schema(schema),
                                ensure_ascii=False,
                            ),
                            "name": schema_name,
                            "description": schema_description or schema_name,
                        }
                    },
                }
            },
        }
        if system:
            params["system"] = [{"text": system}]
        if self._temperature is not None:
            params["inferenceConfig"]["temperature"] = self._temperature
        params.update(self._model_request_fields(thinking_cfg, effort_value))

        raw = await self._aconverse(
            params=params,
            timeout=timeout,
            deadline=deadline,
            operation="structured_response",
        )

        prompt_tokens, completion_tokens = self._extract_usage(raw)
        content = self._extract_text(raw)
        response = StructuredLLMResponse(
            parsed=parse_structured_response_content(
                content,
                model=use_model,
                schema_name=schema_name,
            ),
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            **self._cache_usage(raw),
            model=use_model,
            finish_reason=str(raw.get("stopReason") or raw.get("stop_reason") or ""),
            raw=raw,
            request_max_tokens=int(
                params.get("inferenceConfig", {}).get("maxTokens") or 0
            ),
            schema_name=schema_name,
        )
        if state is not None:
            track_tokens(state, response)
        return response

    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Configuration here means: a boto3 ``bedrock-runtime`` client
        was successfully constructed. AWS credential resolution
        (profile, IAM role, env vars) happens lazily inside boto3 on
        the first request, so a successful constructor does not
        guarantee that credentials are valid — invalid credentials
        surface as :class:`BedrockAPIError` with
        ``error_code="UnrecognizedClientException"`` on the first
        ``complete()`` call.

        Returns:
            ``True`` when the internal Bedrock Runtime client was
            constructed, otherwise ``False``.
        """
        return self._client is not None
