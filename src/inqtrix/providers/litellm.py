"""LiteLLM provider — LLM completions via any OpenAI-compatible endpoint.

This is the default LLM provider used when no explicit provider is
configured. It wraps the OpenAI Python SDK and supports any endpoint
that speaks the OpenAI chat completions protocol (LiteLLM proxy,
OpenRouter, vLLM, Ollama, etc.). SDK retries are disabled; the provider
owns a visible retry loop so transient attempts appear in logs and UI
progress.
"""

from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Literal, Mapping

from openai import APIStatusError, AsyncOpenAI, OpenAIError, RateLimitError

from inqtrix.constants import (
    DEFAULT_LLM_MAX_OUTPUT_TOKENS,
    REASONING_TIMEOUT,
)
from inqtrix.exceptions import AgentModelCapacityError, AgentRateLimited
from inqtrix.providers.attempts import (
    AsyncNativeLLMProvider,
    call_with_retries,
    estimate_request_tokens,
)
from inqtrix.providers import _openai_resources  # noqa: F401  (resources at import)
from inqtrix.providers._openai_responses import (
    api_name as openai_api_name,
    chat_turn as openai_chat_turn,
    send as send_openai_request,
    sent_form as sent_openai_request,
    uses_responses,
)
from inqtrix.providers.base import (
    without_provider_state,
    ChatTurn,
    LLMResponse,
    StructuredLLMResponse,
    _bounded_timeout,
    _check_deadline,
    _operation_deadline,
    model_capacity_reason,
    _normalize_completion_response,
    normalize_reasoning_effort,
    parse_structured_response_content,
)
from inqtrix.providers._openai_rules import (
    reasoning_levels as openai_reasoning_levels,
    effort_to_send,
)
from inqtrix.providers._schema import strictify_json_schema
from inqtrix.settings import ModelSettings
from inqtrix.state import track_tokens

log = logging.getLogger(__name__)


class LiteLLM(AsyncNativeLLMProvider):
    """Route LLM calls through a LiteLLM or OpenAI-compatible endpoint.

    Use this provider when your reasoning models are exposed behind a
    LiteLLM proxy or any other endpoint that implements the OpenAI chat
    completions protocol. It is the default provider for the env-based
    auto-create path and is usually the simplest option when one gateway
    should front multiple upstream models.

    Attributes:
        _clients (WeakKeyDictionary): One ``AsyncOpenAI`` client per event
            loop, built on first use from ``_client_kwargs``.
        _models (ModelSettings): Effective model mapping for reasoning,
            classify, claim extraction, and evaluate roles.
    """

    complete_uses_metadata: bool = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "http://localhost:4000/v1",
        default_model: str = "gpt-4o",
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
        token_budget_parameter: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        selectable_models: list[str] | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        """Initialize the LiteLLM-backed provider.

        Use the constructor when your models are reachable through a
        single OpenAI-compatible base URL such as LiteLLM, OpenRouter,
        vLLM, or Ollama. The role-specific model arguments let you keep a
        strong default reasoning model while moving classification,
        claim extraction, or evaluation to cheaper deployments.

        Args:
            api_key: API key for the LiteLLM or OpenAI-compatible
                endpoint. This argument is required.
            base_url: Base URL of the endpoint. The default is
                ``"http://localhost:4000/v1"``. Use a different value
                when the proxy is hosted elsewhere.
            default_model: Primary model for reasoning, planning,
                evaluation fallback, and final answer synthesis. The
                default is ``"gpt-4o"``.
            classify_model: Optional cheaper override for question
                classification. When omitted, classification falls back
                to ``default_model``.
            claim_extract_model: Optional cheaper override for claim
                extraction. When omitted, extraction also uses
                ``default_model``.
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
                Stored for the tier router and forwarded as a normalized
                OpenAI-compatible ``reasoning_effort`` request field.
            default_max_tokens: Default output-token budget for reasoning
                calls when no per-call value is supplied.
            context_window_tokens: Known context-window size for the
                reasoning deployment. ``None`` means unknown capacity.
            token_budget_parameter: Which request field to use for the
                output-token budget. ``"max_tokens"`` is the legacy default
                and works with most LiteLLM proxies and OpenAI completions.
                Switch to ``"max_completion_tokens"`` when targeting OpenAI
                o-series reasoning models (o1, o3, o4) directly, where
                ``max_tokens`` is rejected in favour of the new field.
            default_headers: Optional headers sent with every request,
                for example the subscription header of an API gateway.

        Raises:
            ValueError: If ``token_budget_parameter`` is not one of
                ``"max_tokens"`` or ``"max_completion_tokens"``.

        Example:
            >>> from inqtrix import LiteLLM
            >>> llm = LiteLLM(
            ...     api_key="test-key",
            ...     base_url="http://localhost:4000/v1",
            ...     default_model="gpt-4o",
            ...     claim_extract_model="gpt-4o-mini",
            ... )
            >>> llm.models.reasoning_model
            'gpt-4o'
        """
        if token_budget_parameter not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(
                "token_budget_parameter must be 'max_tokens' or 'max_completion_tokens'."
            )
        self._client_kwargs: dict[str, Any] = {
            "base_url": base_url, "api_key": api_key, "max_retries": 0,
        }
        if default_headers:
            self._client_kwargs["default_headers"] = dict(default_headers)
        self._clients: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, AsyncOpenAI
        ] = weakref.WeakKeyDictionary()
        self._token_budget_parameter = token_budget_parameter
        self._default_max_tokens = default_max_tokens
        self._context_window_tokens = context_window_tokens
        self._selectable_models = list(selectable_models or [])
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

    # -- public interface --------------------------------------------------

    @property
    def models(self) -> ModelSettings:
        """Return the effective role-to-model mapping for the runtime.

        Returns:
            ModelSettings: Resolved model names that graph nodes use when
            selecting classify, claim extraction, evaluate, or reasoning calls.
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
    def _apply_reasoning_effort(
        create_kwargs: dict[str, object],
        reasoning_effort: str | None,
        *,
        model: str,
    ) -> None:
        """Add an explicitly enabled effort hint to an outgoing request.

        Empty values inherit gateway defaults. For a model with OpenAI rules on
        its card the value comes from
        :func:`~inqtrix.providers._openai_rules.effort_to_send`, as on Azure:
        ``none`` goes out explicitly where the card knows it, a model that
        reasons but cannot be switched off runs at its weakest level, and a
        level its API cannot send becomes the strongest allowed level below it,
        with one visible warning per model. Every other model keeps the gateway's
        handling: ``none`` omits the field and a graded value goes through
        unchanged, so a gateway that cannot honor it rejects the request visibly;
        the provider never silently removes that contract.

        Args:
            create_kwargs: Mutable OpenAI-compatible request parameters.
            reasoning_effort: Optional per-call effort override.
            model: The effective model of the call.
        """
        normalized = normalize_reasoning_effort(reasoning_effort)
        if normalized == "":
            return
        levels = openai_reasoning_levels(model)
        if levels is None:
            if normalized != "none":
                create_kwargs["reasoning_effort"] = normalized
            return
        sent = effort_to_send(model, normalized, label="LiteLLM")
        if sent is not None:
            create_kwargs["reasoning_effort"] = sent

    def _async_client(self) -> AsyncOpenAI:
        """The SDK client of the running loop: one per adapter and loop, built on first use."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            client = AsyncOpenAI(**self._client_kwargs)
            self._clients[loop] = client
        return client

    async def _acreate_chat_completion(
        self,
        *,
        create_kwargs: dict[str, object],
        model: str,
        operation: str,
        timeout: float,
        deadline: float | None,
    ) -> object:
        """Call the model's API (Chat Completions, or Responses where the card says so) with visible retries."""
        operation_deadline = _operation_deadline(timeout, deadline)
        client = self._async_client()
        return await call_with_retries(
            lambda: send_openai_request(
                client,
                {
                    **create_kwargs,
                    "timeout": _bounded_timeout(timeout, operation_deadline),
                },
                provider="litellm",
            ),
            provider_label="LiteLLM",
            model=model,
            operation=operation,
            deadline=operation_deadline,
            outer_deadline=deadline,
            timeout_label=f'LiteLLM call ({operation})',
            configured_timeout_seconds=timeout,
            estimated_input_tokens=estimate_request_tokens(sent_openai_request(create_kwargs, provider="litellm")),
            max_output_tokens=int(create_kwargs.get(self._token_budget_parameter) or 0) or None,
            reasoning_effort=str(create_kwargs.get("reasoning_effort") or "") or None,
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
        """Generate text via LiteLLM and discard token metadata.

        Use this convenience wrapper when the caller only needs visible
        response text. It delegates to ``complete_with_metadata()`` so the
        provider keeps one code path for error handling and token
        tracking.

        Args:
            prompt: User-facing input text.
            system: Optional system message. The default is ``None``.
            model: Optional per-call model override. When omitted, the
                provider uses its default reasoning model.
            timeout: Per-call timeout budget in seconds. The default is
                ``REASONING_TIMEOUT``.
            state: Optional mutable agent state for token tracking. Omit
                this in helper threads or when no token aggregation is
                needed.
            deadline: Optional absolute monotonic deadline for the full
                agent run.

        Returns:
            str: Visible assistant text for the completion.

        Raises:
            AgentTimeout: If the remaining agent budget is exhausted.
            AgentRateLimited: If the endpoint returns a fatal rate-limit
                error.
            OpenAIError: If the SDK surfaces a non-rate-limit backend
                failure.
        """
        return (
            await self.acomplete_with_metadata(
                prompt,
                system=system,
                model=model,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
                state=state,
                deadline=deadline,
                reasoning_effort=reasoning_effort,
            )
        ).content

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
        """Generate text and token metadata through the shared SDK client.

        Use this method for normal reasoning calls when the caller wants
        both the visible content and token accounting. The provider owns
        the retry loop so transient retries are observable; this method
        assembles the request, clamps the timeout against the remaining
        deadline, and maps fatal rate limits into ``AgentRateLimited``.

        Args:
            prompt: User-facing input text.
            system: Optional system instruction. The default is ``None``.
            model: Optional per-call model override. If omitted, the
                provider uses ``self._models.reasoning_model``.
            timeout: Per-call timeout budget in seconds before deadline
                clamping. The default is ``REASONING_TIMEOUT``.
            state: Optional mutable agent state that receives token
                accounting through ``track_tokens()`` when provided.
            deadline: Optional absolute monotonic deadline for the full
                run. When present, the request timeout is reduced to the
                smaller of ``timeout`` and the remaining run budget.

        Returns:
            LLMResponse: Structured response containing visible content,
            token counts, and the effective model label.

        Raises:
            AgentTimeout: If the full run deadline has already elapsed.
            AgentRateLimited: If the backend returns HTTP 429 or the SDK
                raises ``RateLimitError``.
            APIStatusError: If the backend responds with a non-429 HTTP
                error.
            OpenAIError: If the SDK raises any other client or transport
                error.
        """
        if deadline is not None:
            _check_deadline(deadline)
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        use_model = model or self._models.reasoning_model

        try:
            create_kwargs: dict[str, object] = {
                "model": use_model,
                "messages": messages,
                "timeout": _bounded_timeout(timeout, deadline),
                "stream": False,
            }
            create_kwargs[self._token_budget_parameter] = (
                max_output_tokens or self._default_max_tokens
            )
            self._apply_reasoning_effort(create_kwargs, reasoning_effort, model=use_model)
            r = await self._acreate_chat_completion(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="complete",
                timeout=timeout,
                deadline=deadline,
            )
            normalized = _normalize_completion_response(r)
            if state is not None:
                track_tokens(state, normalized)
            return LLMResponse(
                content=normalized.content,
                prompt_tokens=normalized.prompt_tokens,
                completion_tokens=normalized.completion_tokens,
                cache_read_tokens=normalized.cache_read_tokens,
                cache_creation_tokens=normalized.cache_creation_tokens,
                reasoning_tokens=normalized.reasoning_tokens,
                model=use_model,
                finish_reason=normalized.finish_reason,
                raw=normalized.raw,
                request_max_tokens=int(
                    create_kwargs.get(self._token_budget_parameter) or 0
                ),
            )
        except RateLimitError as e:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AgentRateLimited(use_model, e)
        except APIStatusError as e:
            if e.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentRateLimited(use_model, e)
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "LLM call failed (model=%s, api=%s, status=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                e.status_code,
                type(e).__name__,
            )
            raise
        except OpenAIError as e:
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_complete",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "LLM call failed (model=%s, api=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                type(e).__name__,
            )
            raise

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        """Native function calling is part of the OpenAI-compatible
        chat-completions surface LiteLLM proxies, so the provider opts in
        unconditionally; a gateway model without tool support surfaces
        its own backend error on the first :meth:`chat` call (visible,
        never silently degraded)."""
        return True

    def reasoning_levels(self, *, model: str | None = None) -> list[str] | None:
        """The efforts this adapter can send for *model*; ``None`` for models without OpenAI rules."""
        return openai_reasoning_levels(model or self._models.reasoning_model)

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        """Advertise the OpenAI-compatible JSON-schema request surface.

        Gateway/model compatibility remains authoritative at request time. A
        rejected schema therefore fails visibly instead of falling back to an
        unconstrained completion.
        """
        return True

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
        """Generate a strict JSON-schema response through the gateway.

        Args:
            prompt: User-facing input text.
            schema: JSON Schema constrained by the gateway.
            schema_name: Stable provider-facing schema name.
            schema_description: Reserved for providers that expose schema
                descriptions; the Chat Completions format does not.
            system: Optional system instruction.
            model: Optional per-call model override.
            max_output_tokens: Optional output-token budget.
            timeout: Per-call timeout before deadline clamping.
            state: Optional mutable token-accounting state.
            deadline: Optional absolute run deadline.
            reasoning_effort: Optional normalized effort override.

        Returns:
            Parsed structured response with token metadata.
        """
        del schema_description
        if deadline is not None:
            _check_deadline(deadline)
        use_model = model or self._models.reasoning_model
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        create_kwargs: dict[str, object] = {
            "model": use_model,
            "messages": messages,
            "timeout": _bounded_timeout(timeout, deadline),
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": strictify_json_schema(schema),
                },
            },
        }
        create_kwargs[self._token_budget_parameter] = (
            max_output_tokens or self._default_max_tokens
        )
        self._apply_reasoning_effort(create_kwargs, reasoning_effort, model=use_model)
        try:
            raw_response = await self._acreate_chat_completion(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="structured_response",
                timeout=timeout,
                deadline=deadline,
            )
            normalized = _normalize_completion_response(raw_response)
            response = StructuredLLMResponse(
                parsed=parse_structured_response_content(
                    normalized.content,
                    model=use_model,
                    schema_name=schema_name,
                ),
                content=normalized.content,
                prompt_tokens=normalized.prompt_tokens,
                completion_tokens=normalized.completion_tokens,
                cache_read_tokens=normalized.cache_read_tokens,
                cache_creation_tokens=normalized.cache_creation_tokens,
                reasoning_tokens=normalized.reasoning_tokens,
                model=use_model,
                finish_reason=normalized.finish_reason,
                raw=normalized.raw,
                request_max_tokens=int(
                    create_kwargs.get(self._token_budget_parameter) or 0
                ),
                schema_name=schema_name,
            )
            if state is not None:
                track_tokens(state, response)
            return response
        except RateLimitError as exc:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(exc).__name__,
            )
            raise AgentRateLimited(use_model, exc)
        except APIStatusError as exc:
            if exc.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    exc.status_code,
                    type(exc).__name__,
                )
                raise AgentRateLimited(use_model, exc)
            if (capacity_reason := model_capacity_reason(exc)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    exc.status_code,
                    type(exc).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_structured",
                    str(exc),
                    reason=capacity_reason,
                    original=exc,
                ) from exc
            log.error(
                "LLM structured call failed (model=%s, api=%s, status=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                exc.status_code,
                type(exc).__name__,
            )
            raise
        except OpenAIError as exc:
            if (capacity_reason := model_capacity_reason(exc)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(exc).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_structured",
                    str(exc),
                    reason=capacity_reason,
                    original=exc,
                ) from exc
            log.error(
                "LLM structured call failed (model=%s, api=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                type(exc).__name__,
            )
            raise

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
        """Run one native tool-calling chat turn.

        Same transport, retry loop, and error mapping as
        :meth:`complete_with_metadata` — the only differences are the
        caller-owned message array and the OpenAI ``tools`` and
        ``tool_choice`` parameters.
        Response parsing goes through the shared
        :func:`chat_turn_from_openai_response` so tool-call rules cannot
        drift between OpenAI-compatible providers.
        """
        if deadline is not None:
            _check_deadline(deadline)
        use_model = model or self._models.reasoning_model
        try:
            create_kwargs: dict[str, object] = {
                "model": use_model,
                "messages": (
                messages
                if uses_responses(use_model)
                else without_provider_state(messages, provider="litellm")
            ),
                "timeout": _bounded_timeout(timeout, deadline),
                "stream": False,
            }
            create_kwargs[self._token_budget_parameter] = (
                max_output_tokens or self._default_max_tokens
            )
            self._apply_reasoning_effort(create_kwargs, reasoning_effort, model=use_model)
            if tools:
                create_kwargs["tools"] = list(tools)
            if tool_choice is not None:
                create_kwargs["tool_choice"] = tool_choice
            response = await self._acreate_chat_completion(
                create_kwargs=create_kwargs,
                model=use_model,
                operation="chat",
                timeout=timeout,
                deadline=deadline,
            )
            return openai_chat_turn(response, provider="litellm", model=use_model)
        except RateLimitError as e:
            log.error(
                "FATAL Rate-Limit (model=%s, error_type=%s)",
                use_model,
                type(e).__name__,
            )
            raise AgentRateLimited(use_model, e)
        except APIStatusError as e:
            if e.status_code == 429:
                log.error(
                    "FATAL Rate-Limit "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentRateLimited(use_model, e)
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity "
                    "(model=%s, status=%s, error_type=%s)",
                    use_model,
                    e.status_code,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_chat",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "LLM chat failed (model=%s, api=%s, status=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                e.status_code,
                type(e).__name__,
            )
            raise
        except OpenAIError as e:
            if (capacity_reason := model_capacity_reason(e)) is not None:
                log.warning(
                    "ALGO-FAIL model_capacity (model=%s, error_type=%s)",
                    use_model,
                    type(e).__name__,
                )
                raise AgentModelCapacityError(
                    use_model,
                    "llm_chat",
                    str(e),
                    reason=capacity_reason,
                    original=e,
                ) from e
            log.error(
                "LLM chat failed (model=%s, api=%s, error_type=%s)",
                use_model,
                openai_api_name(use_model),
                type(e).__name__,
            )
            raise

    def is_available(self) -> bool:
        """Report whether the provider is configured to attempt requests.

        Configuration here means: the OpenAI SDK client configuration was
        resolved; the client itself is built per loop on first use. The
        constructor checks neither ``api_key`` nor ``base_url``, so a
        constructed ``LiteLLM`` instance always returns ``True`` here today.
        Backend reachability or key validity is not checked — those
        only surface on the first ``complete()`` call.

        Returns:
            ``True`` when the client configuration was resolved,
            otherwise ``False``.
        """
        return self._client_kwargs is not None

