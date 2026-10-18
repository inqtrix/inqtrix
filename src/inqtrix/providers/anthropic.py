"""Direct Anthropic adapter for the LLMProvider interface.

This module calls the Anthropic Messages API via ``urllib`` — no SDK, no
LiteLLM proxy.  The direct approach gives full control over headers,
retry behaviour, and extended-thinking configuration, but it also means
we must handle transient failures, error parsing, and token-budget
adjustments ourselves.

Key design decisions:

* **Retry with jitter** — Anthropic returns HTTP 529 ("Overloaded")
  when capacity is tight.  The research agent fires parallel
  claim-extraction requests for search results, which can trigger
  529 bursts.  A simple fixed-interval retry makes
  things worse (thundering herd), so we use exponential backoff with
  random jitter so parallel threads spread their retries over time.

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

import json
import logging
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from inqtrix.constants import (
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT,
)
from inqtrix.exceptions import (
    AgentModelCapacityError,
    AgentRateLimited,
    AgentTimeout,
    AnthropicAPIError,
)
from inqtrix.providers.base import (
    LLMProvider,
    LLMResponse,
    MAX_PROVIDER_ATTEMPTS,
    StructuredLLMResponse,
    _NonFatalNoticeMixin,
    _RetryNoticeMixin,
    _THINKING_MIN_MAX_TOKENS,
    normalize_reasoning_effort,
    validate_reasoning_effort,
    _SDK_RATE_LIMIT_MAX_RETRIES,
    _bounded_timeout,
    _check_deadline,
    _check_provider_cancel,
    _check_provider_operation_deadline,
    _operation_deadline,
    model_capacity_reason,
    _retry_delay_seconds,
    _sleep_before_retry,
    extract_usage_tokens,
    cache_usage_details,
    parse_structured_response_content,
)
from inqtrix.settings import ModelSettings
from inqtrix.state import track_tokens

log = logging.getLogger("inqtrix")

# ---------------------------------------------------------------------------
# Retry & backoff constants
#
# Anthropic returns 529 ("Overloaded") under sustained load — especially
# when many parallel claim-extraction threads hit the API at once. The
# following constants control the retry loop in _request_json.
#
# Shared backoff constants (_BACKOFF_BASE_SECONDS, _BACKOFF_MAX_SECONDS,
# _JITTER_RANGE, _THINKING_MIN_MAX_TOKENS) are imported from base.py.
#
# _RETRYABLE_HTTP_STATUS: status codes that trigger a retry instead of
#     an immediate hard failure.  529 is Anthropic-specific; 500/502/503/
#     504 cover standard transient infrastructure errors.
#
# _MAX_ANTHROPIC_ATTEMPTS: total tries (initial + retries). All failure
#     classes share this counter and the logical-operation deadline.
# ---------------------------------------------------------------------------
_RETRYABLE_HTTP_STATUS = frozenset({500, 502, 503, 504, 529})
_MAX_ANTHROPIC_ATTEMPTS = MAX_PROVIDER_ATTEMPTS
_ANTHROPIC_MAX_TOKENS = 64_000

# ---------------------------------------------------------------------------
# Effort capability blacklist
#
# Per Anthropic docs (https://docs.anthropic.com/en/docs/build-with-claude/effort)
# the effort parameter is supported only on Claude Mythos Preview, Opus 4.7,
# Opus 4.6, Sonnet 4.6, and Opus 4.5. Other models (notably Haiku 4.5) reject
# ``output_config.effort`` with HTTP 400 ("Extra inputs are not permitted").
#
# Substring match keeps the list tiny. If Anthropic releases a Haiku that
# DOES support effort, change "haiku" to a more specific fragment here.
# ---------------------------------------------------------------------------
_EFFORT_UNSUPPORTED_FRAGMENTS: tuple[str, ...] = ("haiku",)
# Effort levels the Anthropic Messages API accepts in ``output_config.effort``
# (per https://docs.anthropic.com/en/docs/build-with-claude/effort). Note this
# differs from the neutral vocabulary: Anthropic has no ``minimal`` but does
# have ``max``. ``none`` (off) and ``""`` (inherit) are handled separately.
_ANTHROPIC_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
_STRUCTURED_OUTPUT_MODEL_FRAGMENTS: tuple[str, ...] = (
    "claude-haiku-4-5",
    "claude-mythos",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
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


def _model_supports_effort(model: str) -> bool:
    """Return True when the given Anthropic model name accepts output_config.effort."""
    if not model:
        return True  # No model name → assume the caller's default does support it.
    lowered = model.lower()
    return not any(fragment in lowered for fragment in _EFFORT_UNSUPPORTED_FRAGMENTS)


def _model_supports_structured_output(model: str) -> bool:
    """Return True when the Anthropic model advertises JSON-schema output."""
    if not model:
        return False
    lowered = model.lower()
    return any(
        fragment in lowered for fragment in _STRUCTURED_OUTPUT_MODEL_FRAGMENTS
    )


class AnthropicLLM(
    _RetryNoticeMixin,
    _NonFatalNoticeMixin,
    LLMProvider,
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
        if effort is not None and effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(
                "effort must be one of 'low', 'medium', 'high', 'xhigh', or 'max'."
            )
        self._api_key = api_key
        self._base_url = base_url
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
            if _model_supports_effort(model):
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
    def _is_retryable_http_status(status_code: int) -> bool:
        return status_code in _RETRYABLE_HTTP_STATUS

    @staticmethod
    def _extract_http_error_details(exc: HTTPError) -> dict[str, str | int | None]:
        """Parse structured error information from an Anthropic HTTP error.

        Anthropic returns errors as JSON with request-id in headers and
        an ``{"error": {"type": ..., "message": ...}}`` body.  This
        method extracts all available details so that ``AnthropicAPIError``
        can surface them for debugging — including the request-id which
        is essential for support tickets.
        """
        headers = getattr(exc, "headers", None) or getattr(exc, "hdrs", None)
        request_id = None
        retry_after = None
        if headers is not None:
            request_id = headers.get("request-id") or headers.get("anthropic-request-id")
            retry_after = headers.get("retry-after")

        raw_body = ""
        try:
            raw_bytes = exc.read()
        except Exception:
            raw_bytes = b""
        if raw_bytes:
            raw_body = raw_bytes.decode("utf-8", errors="replace")

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
            "status_code": exc.code,
            "request_id": request_id,
            "retry_after": retry_after,
            "error_type": error_type,
            "message": message,
        }

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

    def _request_json(
        self,
        *,
        payload: dict[str, Any],
        timeout: float,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Send a POST to the Anthropic Messages API with retry.

        This is the central HTTP method shared by completion and
        structured-output calls. It implements:

        1. Deadline enforcement — abort early if the agent time budget
           has been exceeded.
        2. Retry with jittered backoff for transient server errors and 429.
           Every failure type shares one total attempt counter and one
           operation deadline, so mixed failures cannot stack budgets.
        3. Structured error extraction for non-retryable failures,
           surfacing request-id and error details in the exception.
        """
        use_model = str(payload.get("model") or self._default_model)
        self._clear_retry_notices()

        operation_deadline = _operation_deadline(timeout, deadline)
        effective_timeout_seconds = max(
            0.0, operation_deadline - time.monotonic()
        )

        def _sleep(delay: float) -> None:
            try:
                _sleep_before_retry(delay, operation_deadline)
            except AgentTimeout:
                _check_provider_operation_deadline(
                    operation_deadline,
                    deadline,
                    label="Anthropic call",
                )
                raise

        attempt = 1
        while True:
            _check_provider_cancel(label="Anthropic call")
            _check_provider_operation_deadline(
                operation_deadline,
                deadline,
                label="Anthropic call",
            )

            request = Request(
                self._base_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": self._user_agent,
                    "x-api-key": self._api_key,
                    "anthropic-version": self._anthropic_version,
                },
                method="POST",
            )

            try:
                with urlopen(
                    request,
                    timeout=_bounded_timeout(timeout, operation_deadline),
                ) as response:
                    raw = response.read().decode("utf-8")
                data = json.loads(raw)
                return data if isinstance(data, dict) else {}
            except HTTPError as exc:
                details = self._extract_http_error_details(exc)
                if exc.code == 429:
                    max_attempts = _SDK_RATE_LIMIT_MAX_RETRIES + 1
                    if attempt >= max_attempts:
                        raise AgentRateLimited(use_model, exc)
                    delay = _retry_delay_seconds(
                        attempt - 1, details.get("retry_after")
                    )
                    self._append_retry_notice({
                        "provider": "Anthropic",
                        "model": use_model,
                        "operation": "messages",
                        "error_code": str(details.get("error_type") or "HTTP 429"),
                        "status_code": 429,
                        "request_id": str(details.get("request_id") or ""),
                        "attempt": attempt,
                        "max_attempts": max_attempts,
                        "delay_seconds": round(delay, 3),
                        "configured_timeout_seconds": round(timeout, 3),
                        "effective_timeout_seconds": round(
                            effective_timeout_seconds, 3
                        ),
                    })
                    log.warning(
                        "Anthropic rate limit (%s, request-id=%s, attempt=%d/%d). Retrying in %.2fs.",
                        use_model,
                        details.get("request_id") or "-",
                        attempt,
                        max_attempts,
                        delay,
                    )
                    _sleep(delay)
                    attempt += 1
                    continue

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
                if (
                    self._is_retryable_http_status(exc.code)
                    and attempt < _MAX_ANTHROPIC_ATTEMPTS
                ):
                    delay = _retry_delay_seconds(
                        attempt - 1, details.get("retry_after")
                    )
                    self._append_retry_notice({
                        "provider": "Anthropic",
                        "model": use_model,
                        "operation": "messages",
                        "error_code": str(details.get("error_type") or f"HTTP {exc.code}"),
                        "status_code": exc.code,
                        "request_id": str(details.get("request_id") or ""),
                        "attempt": attempt,
                        "max_attempts": _MAX_ANTHROPIC_ATTEMPTS,
                        "delay_seconds": round(delay, 3),
                        "configured_timeout_seconds": round(timeout, 3),
                        "effective_timeout_seconds": round(
                            effective_timeout_seconds, 3
                        ),
                    })
                    log.warning(
                        "Anthropic transient HTTP error (%s, status=%s, type=%s, request-id=%s, attempt=%d/%d). Retrying in %.2fs.",
                        use_model,
                        exc.code,
                        details.get("error_type") or "unknown",
                        details.get("request_id") or "-",
                        attempt,
                        _MAX_ANTHROPIC_ATTEMPTS,
                        delay,
                    )
                    _sleep(delay)
                    attempt += 1
                    continue
                raise api_error from exc
            except (URLError, OSError) as exc:
                api_error = self._build_api_error(model=use_model, original=exc)
                if attempt < _MAX_ANTHROPIC_ATTEMPTS:
                    delay = _retry_delay_seconds(attempt - 1)
                    self._append_retry_notice({
                        "provider": "Anthropic",
                        "model": use_model,
                        "operation": "messages",
                        "error_code": type(exc).__name__,
                        "attempt": attempt,
                        "max_attempts": _MAX_ANTHROPIC_ATTEMPTS,
                        "delay_seconds": round(delay, 3),
                        "configured_timeout_seconds": round(timeout, 3),
                        "effective_timeout_seconds": round(
                            effective_timeout_seconds, 3
                        ),
                    })
                    log.warning(
                        "Anthropic transport error "
                        "(model=%s, error_type=%s, attempt=%d/%d). "
                        "Retrying in %.2fs.",
                        use_model,
                        type(exc).__name__,
                        attempt,
                        _MAX_ANTHROPIC_ATTEMPTS,
                        delay,
                    )
                    _sleep(delay)
                    attempt += 1
                    continue
                raise api_error from exc
            except ValueError as exc:
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
        token = normalize_reasoning_effort(reasoning_effort)
        if token == "":
            thinking = self._thinking if self._thinking is not None else None
            effort = (
                self._effort
                if (self._effort is not None and _model_supports_effort(use_model))
                else None
            )
            return thinking, effort
        if token == "none":
            return None, None
        effort, warnings = validate_reasoning_effort(
            token,
            supported_levels=_ANTHROPIC_EFFORT_LEVELS,
            label=f"AnthropicLLM({use_model})",
        )
        for warning in warnings:
            log.warning("CONFIG: %s", warning)
        if effort in ("", "none"):
            return None, None
        if _model_supports_effort(use_model):
            return {"type": "adaptive"}, effort
        log.warning(
            "CONFIG: AnthropicLLM: reasoning_effort=%r requested on model=%s, "
            "which does not accept output_config.effort (Haiku class); using "
            "adaptive thinking without an effort cap.",
            token,
            use_model,
        )
        return {"type": "adaptive"}, None

    def complete(
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
        return self.complete_with_metadata(
            prompt,
            system=system,
            model=model,
            max_output_tokens=max_output_tokens,
            timeout=timeout,
            state=state,
            deadline=deadline,
            reasoning_effort=reasoning_effort,
        ).content

    def complete_with_metadata(
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
        ``_request_json()``, which owns deadline-aware retries for 5xx and
        HTTP 529 overload responses.

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
        max_tokens = max_output_tokens or self._default_max_tokens
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        use_thinking = thinking_cfg is not None
        if use_thinking:
            # Anthropic counts thinking tokens *inside* max_tokens, so raise
            # the budget: honour an explicit budget_tokens, and always enforce
            # the _THINKING_MIN_MAX_TOKENS floor so neither hidden reasoning
            # nor the visible answer gets truncated.
            budget = thinking_cfg.get("budget_tokens")
            if isinstance(budget, int) and budget >= max_tokens:
                max_tokens = budget + 1024
            if max_tokens < _THINKING_MIN_MAX_TOKENS:
                log.debug(
                    "max_tokens auto-raised from %d to %d (thinking enabled)",
                    max_tokens,
                    _THINKING_MIN_MAX_TOKENS,
                )
                max_tokens = _THINKING_MIN_MAX_TOKENS
        max_tokens = _clamp_max_tokens(
            max_tokens,
            model=use_model,
            operation="complete",
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

        raw = self._request_json(
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

    def complete_structured(
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
        max_tokens = max_output_tokens or self._default_max_tokens
        thinking_cfg, effort_value = self._call_thinking_and_effort(
            reasoning_effort, use_model=use_model
        )
        use_thinking = thinking_cfg is not None
        if use_thinking:
            budget = thinking_cfg.get("budget_tokens")
            if isinstance(budget, int) and budget >= max_tokens:
                max_tokens = budget + 1024
            if max_tokens < _THINKING_MIN_MAX_TOKENS:
                log.debug(
                    "max_tokens auto-raised from %d to %d (thinking enabled)",
                    max_tokens,
                    _THINKING_MIN_MAX_TOKENS,
                )
                max_tokens = _THINKING_MIN_MAX_TOKENS
        max_tokens = _clamp_max_tokens(
            max_tokens,
            model=use_model,
            operation="complete_structured",
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

        raw = self._request_json(
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
