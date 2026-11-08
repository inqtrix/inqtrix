"""Direct Anthropic adapter for the LLMProvider interface.

This module calls the Anthropic Messages API with ``httpx`` on the process
loop, one async client per adapter and loop — no SDK, no LiteLLM proxy.
Each request is one attempt of the shared retry loop
(:func:`~inqtrix.providers.attempts.call_with_retries`), and the sync methods
are faces of the async ones.  The direct approach gives full control over
headers and extended-thinking configuration, but it also means we must
handle error parsing and token-budget adjustments ourselves.

Key design decisions:

* **Retry with jitter** — Anthropic returns HTTP 529 ("Overloaded")
  when capacity is tight.  The research agent fires parallel
  claim-extraction requests for search results, which can trigger
  529 bursts.  A simple fixed-interval retry makes
  things worse (thundering herd), so the shared loop uses exponential
  backoff with random jitter so parallel calls spread their retries.

* **Per-call reasoning control** — Extended thinking (``thinking={"type":
  "adaptive"}``) is valuable for complex reasoning calls (classify,
  plan, evaluate, answer), but it wastes tokens on short helper
  calls like claim extraction.  Each ``complete*`` call takes a
  ``reasoning_effort`` token: ``""`` inherits the constructor default,
  ``"none"`` forces thinking off for that call, and a graded level turns
  it on -- so callers tune thinking per code path without mutating shared
  config.

* **Token-budget capacity** — Anthropic counts thinking tokens
  *inside* ``max_tokens``.  The provider defaults to a large output
  budget and still auto-raises ``max_tokens`` when thinking is enabled
  and a caller supplied a smaller per-call budget.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import weakref
from collections import Counter
from typing import Any, Mapping

import httpx

from inqtrix.constants import (
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT,
)
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRateLimited,
    AgentRequiredToolCallMissing,
    AnthropicAPIError,
)
from inqtrix.providers._anthropic_messages import (
    anthropic_tool_choice,
    chat_turn,
    to_anthropic,
    to_anthropic_tool,
    with_instruction,
)
from inqtrix.providers._claude import (
    THINKING_BINDING_BETA,
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
    _bounded_timeout,
    _check_deadline,
    _operation_deadline,
    model_capacity_reason,
    extract_usage_tokens,
    cache_usage_details,
    parse_structured_response_content,
)
from inqtrix.settings import ModelSettings
from inqtrix.state import track_tokens

log = logging.getLogger(__name__)

_ANTHROPIC_MAX_TOKENS = 64_000

_BETA_UNSEEN_MODELS: set[str] = set()
_BETA_UNSEEN_LOCK = threading.Lock()


def _check_beta_arrival(response: Mapping[str, Any], *, model: str) -> None:
    """Say once per model when the thinking-binding beta did not reach the API; count dropped blocks.

    With the beta header every answer carries ``input_transformations``, empty
    when nothing was dropped. Without the field a proxy probably stripped the
    header, and the thinking blocks the API dropped stay invisible.
    """
    transformations = response.get("input_transformations")
    if transformations is None:
        with _BETA_UNSEEN_LOCK:
            first = model not in _BETA_UNSEEN_MODELS
            _BETA_UNSEEN_MODELS.add(model)
        if first:
            log.warning(
                "Claude answered model %s without input_transformations although the "
                "thinking-binding beta was sent; a proxy may strip the beta header, so "
                "dropped thinking blocks stay invisible.",
                model,
            )
        return
    dropped: Counter[str] = Counter()
    for entry in transformations if isinstance(transformations, list) else ():
        if isinstance(entry, Mapping) and entry.get("type") == "thinking_dropped":
            dropped[str(entry.get("reason") or "unspecified")] += 1
    if dropped:
        log.info(
            "Claude dropped thinking blocks for model %s: %s",
            model,
            ", ".join(f"{reason}={count}" for reason, count in sorted(dropped.items())),
        )

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


def _clamp_max_tokens(max_tokens: int, *, model: str, operation: str) -> int:
    """Clamp Anthropic output budgets to the Messages API hard limit."""
    if max_tokens <= _ANTHROPIC_MAX_TOKENS:
        return max_tokens
    log.warning(
        "CONFIG: Anthropic max_tokens=%d exceeds provider cap %d "
        "(model=%s, operation=%s); clamping.",
        max_tokens,
        _ANTHROPIC_MAX_TOKENS,
        model,
        operation,
    )
    return _ANTHROPIC_MAX_TOKENS




def _model_supports_structured_output(model: str) -> bool:
    """Return True when the Anthropic model advertises JSON-schema output."""
    if not model:
        return False
    lowered = model.lower()
    return any(
        fragment in lowered for fragment in _STRUCTURED_OUTPUT_MODEL_FRAGMENTS
    )


def _error_details(response: httpx.Response) -> dict[str, str | int | None]:
    """Parse structured error information from an Anthropic HTTP error.

    Anthropic returns errors as JSON with request-id in headers and an
    ``{"error": {"type": ..., "message": ...}}`` body. This extracts all
    available details so that ``AnthropicAPIError`` can surface them for
    debugging, including the request-id which is essential for support
    tickets.
    """
    headers = response.headers
    request_id = headers.get("request-id") or headers.get("anthropic-request-id")
    retry_after = headers.get("retry-after")
    raw_body = response.text

    error_type = ""
    message = ""
    if raw_body:
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            message = raw_body.strip()
        else:
            if isinstance(payload, dict):
                body_request_id = payload.get("request_id")
                if isinstance(body_request_id, str) and body_request_id.strip():
                    request_id = request_id or body_request_id.strip()
                error = payload.get("error")
                if isinstance(error, dict):
                    raw_type = error.get("type")
                    raw_message = error.get("message")
                    if isinstance(raw_type, str):
                        error_type = raw_type.strip()
                    if isinstance(raw_message, str):
                        message = raw_message.strip()
                if not message:
                    top_level_message = payload.get("message")
                    if isinstance(top_level_message, str):
                        message = top_level_message.strip()

    return {
        "status_code": response.status_code,
        "request_id": request_id,
        "retry_after": retry_after,
        "error_type": error_type,
        "message": message,
    }


def _retry_error_code(exc: BaseException) -> str:
    """Anthropic's error type for a retry notice, else the status or the exception name."""
    if isinstance(exc, httpx.HTTPStatusError):
        details = _error_details(exc.response)
        return str(details["error_type"] or f"HTTP {details['status_code']}")
    return type(exc).__name__


def _retry_request_id(exc: BaseException) -> str:
    """The request-id of a failed attempt, for its retry notice."""
    if isinstance(exc, httpx.HTTPStatusError):
        return str(_error_details(exc.response)["request_id"] or "")
    return ""


class AnthropicLLM(
    AsyncNativeLLMProvider,
):
    """Call the Anthropic Messages API directly without a proxy.

    Use this provider when you want native Anthropic behavior, direct
    access to the Messages API, and explicit control over headers,
    retries, and extended thinking. It is the right choice when the
    deployment should not depend on LiteLLM and when reasoning quality or
    Anthropic-specific diagnostics matter more than sharing one generic
    gateway.

    Attributes:
        _api_key (str): API key used for direct Anthropic requests.
        _base_url (str): Messages endpoint URL.
        _anthropic_version (str): Version header forwarded on every
            request.
        _default_model (str): Primary reasoning model for classify, plan,
            evaluate fallback, and answer calls.
        _claim_extract_model (str): Helper model for claim extraction.
        _default_max_tokens (int): Default token budget for reasoning
            calls before thinking-related auto-raise.
        _user_agent (str): User-Agent header for direct HTTP requests.
        _temperature (float | None): Optional sampling temperature.
        _thinking (dict[str, Any] | None): Extended-thinking config applied
            on reasoning calls when the per-call ``reasoning_effort`` inherits
            the constructor default.
        _models (ModelSettings): Effective role-to-model mapping exposed
            to the runtime.
    """

    complete_uses_metadata: bool = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com/v1/messages",
        anthropic_version: str = "2023-06-01",
        default_model: str = "claude-sonnet-4-6",
        classify_model: str = "",
        claim_extract_model: str = "claude-haiku-4-5",
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
        user_agent: str = "inqtrix/0.1",
        temperature: float | None = None,
        thinking: dict[str, Any] | None = None,
        effort: str | None = None,
        selectable_models: list[str] | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the direct Anthropic provider.

        Use the constructor when you have a native Anthropic API key and
        want direct control over retry behavior, error reporting, and
        thinking configuration. The role-specific model arguments let you
        keep strong reasoning on Claude Sonnet or Opus while delegating
        high-volume helper work to Haiku or another cheaper model.

        Args:
            api_key: Anthropic API key for the direct Messages API.
            base_url: Messages endpoint URL. The default is
                ``"https://api.anthropic.com/v1/messages"``.
            anthropic_version: Value for the ``anthropic-version`` header.
                The default is ``"2023-06-01"``.
            default_model: Primary model for classify, plan, evaluate
                fallback, and answer calls. The default is
                ``"claude-sonnet-4-6"``.
            classify_model: Optional cheaper override for classification.
                When omitted, classification falls back to
                ``default_model``.
            claim_extract_model: Helper model used for claim extraction.
                The default is ``"claude-haiku-4-5"``.
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
                ``"none"`` forces reasoning off, and a graded level enables it
                (see ``reasoning_effort`` on :meth:`complete`).
            default_max_tokens: Output-token budget for reasoning calls
                before any thinking-related auto-raise.
            context_window_tokens: Known context-window size for the
                reasoning model. ``None`` means unknown capacity.
            user_agent: User-Agent header value. The default is
                ``"inqtrix/0.1"``.
            temperature: Optional sampling temperature. Do not set this
                together with ``thinking`` because the Anthropic API rejects
                that combination.
            thinking: Optional Anthropic thinking configuration, for
                example ``{"type": "adaptive"}``. Applied to calls whose
                per-call ``reasoning_effort`` inherits the constructor
                default (``""``); a per-call ``"none"`` overrides it off.
            effort: Optional ``effort`` value (``"low"``, ``"medium"``,
                ``"high"``, ``"xhigh"``, ``"max"``). Forwarded to the
                Anthropic Messages API as
                ``output_config: {"effort": ...}``. Controls overall
                token spend (text + tool calls + thinking). Works with
                or without ``thinking`` enabled. ``"xhigh"`` is only
                supported on Claude Opus 4.7+. The API default is
                ``"high"`` (= same as omitting the parameter).
            default_headers: Optional headers sent with every request,
                for example the subscription header of an API gateway.
                They win over the provider's own headers of the same name,
                except ``x-api-key`` and ``Content-Type``.

        Raises:
            ValueError: If both ``temperature`` and ``thinking`` are set,
                or if ``effort`` is not one of the accepted values.

        Example:
            >>> from inqtrix import AnthropicLLM
            >>> llm = AnthropicLLM(
            ...     api_key="test-key",
            ...     default_model="claude-sonnet-4-6",
            ...     claim_extract_model="claude-haiku-4-5",
            ... )
            >>> llm.models.effective_claim_extract_model
            'claude-haiku-4-5'
        """
        if temperature is not None and thinking is not None:
            raise ValueError(
                "temperature and thinking are mutually exclusive — "
                "the Anthropic API rejects requests that set both."
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
        self._api_key = api_key
        self._base_url = base_url
        self._default_headers = dict(default_headers or {})
        # Tests put a transport here; the client is built per loop on first use.
        self._client_kwargs: dict[str, Any] = {}
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, httpx.AsyncClient
        ] = weakref.WeakKeyDictionary()
        self._anthropic_version = anthropic_version
        self._default_model = default_model
        self._claim_extract_model = claim_extract_model
        self._default_max_tokens = default_max_tokens
        self._context_window_tokens = context_window_tokens
        self._selectable_models = list(selectable_models or [])
        self._user_agent = user_agent
        self._temperature = temperature
        self._thinking = thinking
        self._effort = effort
        self._models = ModelSettings(
            reasoning_model=default_model,
            search_model="",
            classify_model=classify_model,
            claim_extract_model=claim_extract_model,
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
        # Collect effort/model incompatibility warnings before the first call
        # so the operator knows why effort is omitted. Warnings
        # are exposed via ``consume_effort_config_warnings`` which the classify
        # node drains on the first run (visible in both log and progress stream).
        self._effort_config_warnings: list[str] = self._collect_effort_warnings(
            default_model=default_model,
            classify_model=classify_model,
            claim_extract_model=claim_extract_model,
            evaluate_model=evaluate_model,
        )
        for warning in self._effort_config_warnings:
            log.warning(warning)

    def _collect_effort_warnings(
        self,
        *,
        default_model: str,
        classify_model: str,
        claim_extract_model: str,
        evaluate_model: str,
    ) -> list[str]:
        """Return user-facing warnings for effort/model incompatibilities."""
        if self._effort is None:
            return []

        warnings: list[str] = []
        # role label → model id
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
                f"AnthropicLLM: effort='{self._effort}' is not sent for {role}='{model}' "
                "because the model does not accept output_config.effort "
                "(Haiku class). Other roles with effort-capable models "
                "(Opus/Sonnet 4.6+) still receive effort. Set effort=None "
                "or choose an effort-capable model to resolve this mismatch."
            )
        return warnings

    def consume_effort_config_warnings(self) -> list[str]:
        """Return and clear the constructor-side effort-config warnings.

        Called once by the classify node on the first run so the warnings
        appear in both the log stream and the user-facing progress feed.
        """
        out = list(self._effort_config_warnings)
        self._effort_config_warnings = []
        return out

    @property
    def models(self) -> ModelSettings:
        """Return the effective role-to-model mapping for the runtime.

        Returns:
            ModelSettings: Resolved model aliases used by graph nodes.
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

    # ------------------------------------------------------------------ #
    # Retry helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_api_error(
        *,
        model: str,
        details: dict[str, str | int | None] | None = None,
        message: str = "",
        original: Exception,
    ) -> AnthropicAPIError:
        details = details or {}
        sc = details.get("status_code")
        return AnthropicAPIError(
            model=model,
            status_code=sc if isinstance(sc, int) else None,
            error_type=str(details.get("error_type") or "").strip(),
            message=message.strip() or str(details.get("message") or "").strip() or str(original),
            request_id=str(details.get("request_id") or "").strip() or None,
            retry_after=str(details.get("retry_after") or "").strip() or None,
            original=original,
        )

    def _async_client(self) -> httpx.AsyncClient:
        """The HTTP client of the running loop: one per adapter and loop, built on first use."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = httpx.AsyncClient(**self._client_kwargs)
            self._clients[loop] = client
        return client

    async def _arequest_json(
        self,
        *,
        payload: dict[str, Any],
        timeout: float,
        deadline: float | None = None,
        beta: str | None = None,
    ) -> dict[str, Any]:
        """Send a POST to the Anthropic Messages API.

        The central HTTP method shared by every call. Each request is one
        attempt of the shared retry loop, which retries HTTP 408, 409, 429,
        500, 502, 503, 504, the 529 overload and transport errors within one
        operation deadline, waiting as long as a ``retry-after`` asks while
        that wait ends before the deadline. A failure that is not retried becomes
        ``AgentRateLimited`` (429), ``AgentModelCapacityError`` or an
        ``AnthropicAPIError`` with the request-id and error details, which are
        essential for support tickets.
        """
        use_model = str(payload.get("model") or self._default_model)
        operation_deadline = _operation_deadline(timeout, deadline)
        # Header names ignore case: an extra header replaces the adapter's own
        # of the same name, and the key and content type replace any extra.
        headers = httpx.Headers({
            "User-Agent": self._user_agent,
            "anthropic-version": self._anthropic_version,
            **({"anthropic-beta": beta} if beta else {}),
        })
        headers.update(self._default_headers)
        if beta:
            # An operator's beta joins this request's beta instead of
            # replacing it: the binding of thinking blocks needs its value.
            betas = [b.strip() for b in headers.get("anthropic-beta", "").split(",") if b.strip()]
            headers["anthropic-beta"] = ",".join([*betas, *([] if beta in betas else [beta])])
        headers["Content-Type"] = "application/json"
        headers["x-api-key"] = self._api_key
        body = json.dumps(payload).encode("utf-8")
        client = self._async_client()

        async def send() -> dict[str, Any]:
            response = await client.post(
                self._base_url,
                content=body,
                headers=headers,
                timeout=_bounded_timeout(timeout, operation_deadline),
            )
            response.raise_for_status()
            data = json.loads(response.text)
            return data if isinstance(data, dict) else {}

        try:
            return await call_with_retries(
                send,
                provider_label="Anthropic",
                model=use_model,
                operation="messages",
                deadline=operation_deadline,
                outer_deadline=deadline,
                timeout_label="Anthropic call",
                configured_timeout_seconds=timeout,
                error_code_for=_retry_error_code,
                request_id_for=_retry_request_id,
                estimated_input_tokens=estimate_request_tokens(payload),
                max_output_tokens=int(payload.get("max_tokens") or 0) or None,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                raise AgentRateLimited(use_model, exc) from exc
            api_error = self._build_api_error(
                model=use_model, details=_error_details(exc.response), original=exc
            )
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
        except (httpx.TransportError, ValueError) as exc:
            raise self._build_api_error(model=use_model, original=exc) from exc

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        """Extract visible text from an Anthropic response.

        The Messages API returns a ``content`` array with typed blocks.
        We only collect ``{"type": "text"}`` blocks — thinking blocks
        (``{"type": "thinking"}``) are intentionally skipped so they
        never leak into the user-visible output.
        """
        parts: list[str] = []
        for block in payload.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        return "".join(parts)

    @staticmethod
    def _extract_usage(payload: dict[str, Any]) -> tuple[int, int]:
        prompt_tokens, completion_tokens = extract_usage_tokens(
            payload,
            input_keys=("input_tokens",),
            output_keys=("output_tokens",),
        )
        details = AnthropicLLM._cache_usage(payload)
        return prompt_tokens + sum(value or 0 for value in details.values()), completion_tokens

    @staticmethod
    def _cache_usage(payload: dict[str, Any]) -> dict[str, int | None]:
        # Native input_tokens excludes both cache buckets.
        return cache_usage_details(
            payload.get("usage"), read_key="cache_read_input_tokens",
            creation_key="cache_creation_input_tokens",
        )

    def _call_thinking_and_effort(
        self, reasoning_effort: str | None, *, use_model: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve the (thinking, effort) to apply for a single call.

        Encapsulates the per-call reasoning contract so ``complete*`` stay
        uniform:

        - ``reasoning_effort`` ``None``/``""`` inherits the constructor
          ``self._thinking`` / ``self._effort`` (the historical behaviour).
        - ``"none"`` forces reasoning off for this call.
        - A graded level turns on adaptive thinking and sets
          ``output_config.effort`` where the model accepts it; Haiku-class
          models get adaptive thinking without an effort cap and a visible
          warning. Unsupported levels are downgraded with a warning.

        Args:
            reasoning_effort: The per-call effort override.
            use_model: The effective model id for this call.

        Returns:
            A ``(thinking, effort)`` tuple. ``thinking`` is the Anthropic
            thinking config dict or ``None``; ``effort`` is the
            ``output_config.effort`` value or ``None``.
        """
        return thinking_and_effort(
            reasoning_effort,
            model=use_model,
            inherit_thinking=self._thinking,
            inherit_effort=self._effort,
            label="AnthropicLLM",
        )

    def _output_budget(
        self,
        max_output_tokens: int | None,
        thinking_cfg: dict[str, Any] | None,
        *,
        model: str,
        operation: str,
    ) -> int:
        """The ``max_tokens`` to send for one call.

        The thinking rule of :func:`~inqtrix.providers._claude.output_budget`,
        clamped to the Messages API's hard limit.
        """
        return _clamp_max_tokens(
            output_budget(max_output_tokens or self._default_max_tokens, thinking_cfg),
            model=model,
            operation=operation,
        )

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
        """Generate text through the direct Anthropic provider.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction for the Messages API.
            model: Optional per-call model override. When omitted, the
                provider uses ``self._default_model``.
            timeout: Per-call timeout budget in seconds.
            state: Optional mutable agent state for token tracking.
            deadline: Optional absolute monotonic deadline for the full
                run.

        Returns:
            str: Visible assistant text extracted from the Anthropic
            response payload.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Anthropic returns HTTP 429.
            AnthropicAPIError: If a non-retryable direct API failure
                occurs.
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
        """Generate text and token metadata through the Messages API.

        Use this method for reasoning calls when the runtime wants both
        visible output and token usage. It applies Anthropic-specific
        thinking behavior: when thinking is enabled, the method raises
        ``max_tokens`` to accommodate both hidden reasoning and the final
        visible answer. Request execution itself is delegated to
        ``_arequest_json()``, whose requests are attempts of the shared
        retry loop (5xx, the HTTP 529 overload, 429 and transport errors).

        Args:
            prompt: User-facing input text.
            system: Optional system prompt forwarded as the Anthropic
                ``system`` field.
            model: Optional per-call model override. When omitted, the
                provider uses ``self._default_model``.
            timeout: Per-call timeout budget in seconds before retry and
                deadline clamping. The default is ``REASONING_TIMEOUT``.
            state: Optional mutable agent state that receives token counts
                through ``track_tokens()`` when provided.
            deadline: Optional absolute monotonic deadline for the full
                agent run.

        Returns:
            LLMResponse: Structured response containing visible content,
            token counts, and the effective model label.

        Raises:
            AgentTimeout: If the full run deadline has already elapsed.
            AgentRateLimited: If Anthropic returns HTTP 429.
            AnthropicAPIError: If the direct API call fails after retrying
                or the response cannot be normalized.
        """
        if deadline is not None:
            _check_deadline(deadline)

        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        use_thinking = thinking_cfg is not None
        max_tokens = self._output_budget(
            max_output_tokens, thinking_cfg, model=use_model, operation="complete"
        )
        payload: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            payload["system"] = system
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if use_thinking:
            payload["thinking"] = thinking_cfg
        # Effort rides inside output_config (top-level returns HTTP 400). The
        # per-call resolver already dropped it for models that reject it
        # (Haiku class) and for none / inherit-without-effort calls.
        # See https://docs.anthropic.com/en/docs/build-with-claude/effort
        if effort_value is not None:
            payload["output_config"] = {"effort": effort_value}

        raw = await self._arequest_json(
            payload=payload,
            timeout=timeout,
            deadline=deadline,
        )

        prompt_tokens, completion_tokens = self._extract_usage(raw)
        response = LLMResponse(
            content=self._extract_text(raw),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            **self._cache_usage(raw),
            model=use_model,
            finish_reason=str(raw.get("stop_reason") or raw.get("stopReason") or ""),
            raw=raw,
            request_max_tokens=int(payload.get("max_tokens") or 0),
        )
        if state is not None:
            track_tokens(state, response)
        return response

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Claude takes tools natively on every model this adapter serves."""
        return True

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
        """Run one tool-calling turn through the Messages API.

        The OpenAI-shaped conversation becomes the Messages API shape (top-level
        ``system``, ``tool_use`` in the assistant turn, every tool result of a turn
        as a ``tool_result`` block in one user message); Claude's thinking blocks
        go back unchanged with the turn that produced them. ``tool_choice``
        ``required`` forces a tool where the model and the request's thinking allow
        it; elsewhere the turn gets ``auto`` and an instruction in the system
        prompt, and a text answer raises
        :class:`~inqtrix.exceptions.AgentRequiredToolCallMissing` for the kernel's
        bounded correction. On a model whose thinking blocks are bound to an
        unchanged prefix, a request that returns blocks asks the API to drop a
        block whose prefix changed (the kernel's own compaction changes it).

        Raises:
            AgentTimeout: The run deadline has passed.
            AgentRateLimited: Anthropic kept answering HTTP 429.
            AnthropicAPIError: The call failed after retrying.
            AgentStructuredOutputError: The model declined the request.
            AgentToolArgumentsError: The output limit stopped the turn while it
                wrote tool calls, or a tool call's input is no JSON object.
            AgentRequiredToolCallMissing: A turn that had to call a tool answered in text.
            ValueError: The conversation has no Messages API form.
        """
        if deadline is not None:
            _check_deadline(deadline)
        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        max_tokens = self._output_budget(
            max_output_tokens, thinking_cfg, model=use_model, operation="chat"
        )
        system, wire_messages, returns_thinking = to_anthropic(messages)
        tool_names = [str((tool.get("function") or tool).get("name") or "") for tool in tools or []]
        choice, instruction = anthropic_tool_choice(
            tool_choice,
            tool_names=tool_names,
            forced_allowed=tool_forcing_allowed(use_model, thinking_cfg),
        )
        if instruction:
            system = with_instruction(system, instruction)
        thinking_cfg, bound = with_block_binding(
            thinking_cfg, model=use_model, returns_thinking=returns_thinking
        )
        beta = THINKING_BINDING_BETA if bound else None
        payload: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens,
            "messages": wire_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [to_anthropic_tool(tool) for tool in tools]
        if choice is not None:
            payload["tool_choice"] = choice
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if thinking_cfg is not None:
            payload["thinking"] = thinking_cfg
        if effort_value is not None:
            payload["output_config"] = {"effort": effort_value}

        raw = await self._arequest_json(
            payload=payload, timeout=timeout, deadline=deadline, beta=beta
        )
        if bound:
            _check_beta_arrival(raw, model=use_model)
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
        """Return whether the selected Anthropic model supports JSON schemas.

        Args:
            model: Optional model override. When omitted, the provider's
                default reasoning model is checked.

        Returns:
            ``True`` for Claude model families documented to support
            ``output_config.format`` structured outputs.
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
        """Generate a JSON-schema-constrained Anthropic response.

        Args:
            prompt: User-facing input text.
            schema: JSON Schema object sent through
                ``output_config.format``.
            schema_name: Stable schema name used in diagnostics.
            schema_description: Optional schema purpose. The direct
                Anthropic API does not currently consume this field, but
                the provider contract keeps it available for other
                backends.
            system: Optional system prompt.
            model: Optional model override. Defaults to
                ``self._default_model``.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout before deadline clamping.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute monotonic deadline.

        Returns:
            StructuredLLMResponse with parsed top-level JSON object.

        Raises:
            AgentTimeout: If the full run deadline has elapsed.
            AgentRateLimited: If Anthropic returns HTTP 429.
            AnthropicAPIError: If the provider call fails.
            AgentStructuredOutputError: If the visible structured JSON
                cannot be parsed into a dictionary.
        """
        if deadline is not None:
            _check_deadline(deadline)

        use_model = model or self._default_model
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        use_thinking = thinking_cfg is not None
        max_tokens = self._output_budget(
            max_output_tokens, thinking_cfg, model=use_model, operation="complete_structured"
        )

        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": schema,
            }
        }
        if effort_value is not None:
            output_config["effort"] = effort_value

        payload: dict[str, Any] = {
            "model": use_model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if system:
            payload["system"] = system
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if use_thinking:
            payload["thinking"] = thinking_cfg

        raw = await self._arequest_json(
            payload=payload,
            timeout=timeout,
            deadline=deadline,
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
            finish_reason=str(raw.get("stop_reason") or raw.get("stopReason") or ""),
            raw=raw,
            request_max_tokens=int(payload.get("max_tokens") or 0),
            schema_name=schema_name,
        )
        if state is not None:
            track_tokens(state, response)
        return response

    def is_available(self) -> bool:
        """Report whether the provider has enough config to run.

        Configuration here means: a non-empty Anthropic API key was
        supplied to the constructor. This does not validate the key
        against the Anthropic backend — invalid keys only surface on
        the first ``complete()`` call as
        :class:`AnthropicAPIError` (HTTP 401).

        Returns:
            ``True`` when ``api_key`` is non-empty, otherwise ``False``.
        """
        return bool(self._api_key)
