"""Measuring wrappers around the provider chokepoints.

ONE wrap point per capability covers every call site in the codebase:
``TracingLLMProvider`` around the :class:`~inqtrix.providers.base.LLMProvider`
ABC (all ``complete*``/``chat`` callers — research nodes, kernel via
``_DepsChatProvider``, knowledge, chat, editor), ``TracingSearchProvider``
around :class:`~inqtrix.providers.base.SearchProvider`, and
``TracingEmbeddingProvider`` around the knowledge embedding client.

Each call gets: wall-clock duration (``duration_ms`` on the response
DTOs — measured for the first time anywhere in the codebase), a
``gen_ai``-semconv span (metadata always; CONTENT only through the
:mod:`~inqtrix.observability.content` policy), and error visibility
(exception recorded on the span, status ERROR). Without the
``observability`` extra or with tracing off, the wrappers degrade to
pure duration measurement — no OpenTelemetry import is required.

Transparency contract: the wrappers subclass the ABCs, delegate every
non-instrumented member (including private attributes) to the wrapped
provider via ``__getattr__``, and expose it as ``_provider`` so the
existing unwrap helpers (``provider_label``,
``runtime_logging.unwrap_provider``) reach the real backend class.
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Iterator

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.exceptions import AgentToolArgumentsError
from inqtrix.observability import semconv
from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.providers.base import (
    INCOMPLETE_FINISH_REASONS,
    LLMProvider,
    SearchProvider,
    reported_capability,
)
from inqtrix.providers.embeddings import EmbeddingProvider

log = logging.getLogger("inqtrix")

try:  # Optional dependency: the wrappers work without OpenTelemetry.
    from opentelemetry import trace as _otel_trace
except Exception:  # noqa: BLE001 — extra not installed
    _otel_trace = None

if TYPE_CHECKING:
    from inqtrix.providers.base import (
        ChatTurn,
        LLMResponse,
        StructuredLLMResponse,
    )
    from inqtrix.search_result import GroundedSearchResult


def _set_content(
    span: Any, key: str, value: Any, policy: ContentCapturePolicy,
    *, json_text_key: str | None = None,
) -> None:
    """Set one CONTENT attribute through the policy (redact + cap).

    Every truncation raises the ``inqtrix.truncation`` span event so an
    accidentally capped prompt is findable instead of silently thin.
    """
    clipped = (
        policy.clip_text(value, json_string=bool(json_text_key))
        if isinstance(value, str)
        else policy.clip_payload(value)
    )
    recorded_key = json_text_key or key
    span.set_attribute(recorded_key, clipped.text)
    if json_text_key:
        # Both views use one redacted/capped value. JSON encoding keeps the
        # native input/output a string even when the text itself is JSON.
        span.set_attribute(key, json.loads(clipped.text))
    if clipped.truncated:
        span.add_event(
            semconv.TRUNCATION_EVENT,
            {
                semconv.TRUNCATION_LIMIT_NAME: recorded_key,
                semconv.TRUNCATION_ORIGINAL_SIZE: clipped.original_size,
                semconv.TRUNCATION_CAPPED_SIZE: len(
                    clipped.text.encode("utf-8")
                ),
            },
        )


class _TracingBase:
    """Shared span plumbing for the three wrappers."""

    def __init__(
        self,
        provider: Any,
        *,
        policy: ContentCapturePolicy,
        tracer_provider: Any | None = None,
    ) -> None:
        self._provider = provider
        self._policy = policy
        self._tracer_provider = tracer_provider

    def __getattr__(self, name: str) -> Any:
        # Reached only when normal lookup fails. Guard the delegate
        # attribute itself so a half-constructed instance cannot recurse.
        if name == "_provider":
            raise AttributeError(name)
        return getattr(self._provider, name)

    @staticmethod
    def _context_attributes() -> dict[str, Any]:
        """Attribution of the call to its run, task and call site.

        Read from the bound purpose (:func:`bind_purpose`) and the log
        context: the wrapper is the one wrap point and cannot know its
        caller, so the caller says who it is. The purpose is mirrored
        into the filterable Langfuse metadata namespace; the rest stays
        an Inqtrix attribute (visible in the observation, joinable with
        the run events by ``run_id``).
        """
        from inqtrix.observability.context import (
            current_log_context,
            current_purpose,
        )

        attributes: dict[str, Any] = {}
        run_id = current_log_context().get("run_id") or ""
        if run_id:
            attributes[semconv.INQTRIX_RUN_ID] = run_id
        purpose = current_purpose()
        if purpose is None:
            return attributes
        if purpose.purpose:
            attributes[semconv.INQTRIX_PURPOSE] = purpose.purpose
            attributes[semconv.observation_metadata_key("purpose")] = (
                purpose.purpose
            )
        if purpose.task_id:
            attributes[semconv.INQTRIX_TASK_ID] = purpose.task_id
        if purpose.task_kind:
            attributes[semconv.INQTRIX_TASK_KIND] = purpose.task_kind
        if purpose.step:
            attributes[semconv.INQTRIX_STEP] = purpose.step
        return attributes

    @contextmanager
    def _span(
        self, name: str, attributes: dict[str, Any]
    ) -> Iterator[Any | None]:
        if _otel_trace is None:
            yield None
            return
        from inqtrix.observability.otel import projected_exceptions

        tracer = _otel_trace.get_tracer(
            "inqtrix.providers", tracer_provider=self._tracer_provider
        )
        with tracer.start_as_current_span(
            name,
            attributes=attributes,
            record_exception=False,
            set_status_on_exception=False,
        ) as span, projected_exceptions(span):
            # A non-recording span (tracing off, or sampled out) exports
            # nothing — yield None so the whole wrapper collapses to the
            # real call plus duration, and no content redaction/
            # serialization runs on the hot path.
            yield span if span.is_recording() else None


class _CallMeter:
    """Mutable per-call carrier the metered block fills in (model/usage)."""

    __slots__ = ("model", "prompt_tokens", "completion_tokens", "finish_reason")

    def __init__(self, model: str | None) -> None:
        self.model = model
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.finish_reason = ""


@contextmanager
def _metered_llm(
    provider_name: str, operation: str, model: str | None
) -> Iterator[_CallMeter]:
    """Prometheus feed for one LLM call — success AND error.

    Independent of tracing state on purpose: metrics must not vanish
    when spans are off or sampled out. No-op without an active holder;
    recording failures warn once and never touch the call.
    """
    from inqtrix.observability.metrics_defs import (
        active_metrics,
        metric_model_label,
    )

    meter = _CallMeter(model)
    metrics = active_metrics()
    started = time.monotonic()
    try:
        yield meter
    except Exception as exc:
        from inqtrix.observability.metrics_defs import (
            outcome_from_exception,
        )
        from inqtrix.usage.recorder import record_provider_call

        error_outcome = outcome_from_exception(exc)
        error_duration = time.monotonic() - started
        if metrics is not None:
            from inqtrix.observability.context import current_feature

            metrics.observe_llm(
                provider=provider_name,
                model=metric_model_label(meter.model),
                operation=operation,
                outcome=error_outcome,
                duration_seconds=error_duration,
                feature=current_feature(),
                prompt_tokens=meter.prompt_tokens,
                completion_tokens=meter.completion_tokens,
            )
        record_provider_call(
            operation=operation,
            model=meter.model or "unknown",
            outcome=error_outcome,
            duration_seconds=error_duration,
            input_tokens=meter.prompt_tokens,
            output_tokens=meter.completion_tokens,
        )
        raise
    from inqtrix.usage.recorder import record_provider_call

    duration = time.monotonic() - started
    # Billed, but stopped before it was finished (content filter, output
    # budget): the metric and the trace mark it, never as a plain success.
    # The usage ledger books it as the billed call it is; its outcome
    # CHECK admits only success, timeout, cancelled and error.
    outcome = (
        "incomplete"
        if meter.finish_reason.strip().lower() in INCOMPLETE_FINISH_REASONS
        else "success"
    )
    if metrics is not None:
        from inqtrix.observability.context import current_feature

        metrics.observe_llm(
            provider=provider_name,
            model=metric_model_label(meter.model),
            operation=operation,
            outcome=outcome,
            duration_seconds=duration,
            feature=current_feature(),
            prompt_tokens=meter.prompt_tokens,
            completion_tokens=meter.completion_tokens,
        )
    record_provider_call(
        operation=operation,
        model=meter.model or "unknown",
        outcome="success",
        duration_seconds=duration,
        input_tokens=meter.prompt_tokens,
        output_tokens=meter.completion_tokens,
    )


@contextmanager
def _metered_search(provider_name: str, engine: str) -> Iterator["_CallMeter"]:
    """Prometheus + ledger feed for one web-search call.

    Grounded-search calls carry their OWN provider token usage; the
    caller sets it on the yielded meter so the ledger books the same
    numbers the run folds into its totals (Ledger↔Quota-Konsistenz).
    """
    from inqtrix.observability.metrics_defs import active_metrics
    from inqtrix.usage.recorder import record_provider_call

    meter = _CallMeter(engine)
    metrics = active_metrics()
    started = time.monotonic()
    try:
        yield meter
    except Exception as exc:
        from inqtrix.observability.metrics_defs import (
            outcome_from_exception,
        )

        error_outcome = outcome_from_exception(exc)
        error_duration = time.monotonic() - started
        if metrics is not None:
            metrics.observe_search(
                provider=provider_name,
                engine=engine,
                outcome=error_outcome,
                duration_seconds=error_duration,
            )
        record_provider_call(
            operation="web_search",
            model=engine,
            outcome=error_outcome,
            duration_seconds=error_duration,
        )
        raise
    duration = time.monotonic() - started
    if metrics is not None:
        metrics.observe_search(
            provider=provider_name,
            engine=engine,
            outcome="success",
            duration_seconds=duration,
        )
    record_provider_call(
        operation="web_search",
        model=meter.model or engine,
        outcome="success",
        duration_seconds=duration,
        input_tokens=meter.prompt_tokens,
        output_tokens=meter.completion_tokens,
    )


def _record_token_usage(
    span: Any, prompt_tokens: int, completion_tokens: int, *,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    reasoning_tokens: int | None = None,
) -> None:
    """One inclusive-total and disjoint-detail contract for model/search calls."""
    span.set_attribute(
        semconv.GEN_AI_USAGE_INPUT_TOKENS, int(prompt_tokens)
    )
    span.set_attribute(
        semconv.GEN_AI_USAGE_OUTPUT_TOKENS, int(completion_tokens)
    )
    details = {
        "input_cached_tokens": cache_read_tokens,
        "input_cache_creation": cache_creation_tokens,
        "output_reasoning_tokens": reasoning_tokens,
    }
    for key, value in tuple(details.items()):
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            log.warning("Provider usage detail %s is invalid; omitted.", key)
            details[key] = None
    cached = (details["input_cached_tokens"] or 0) + (details["input_cache_creation"] or 0)
    if cached > prompt_tokens:
        log.warning("Provider cache counts exceed input tokens; breakdown omitted.")
        details["input_cached_tokens"] = details["input_cache_creation"] = None
        cached = 0
    reasoning = details["output_reasoning_tokens"] or 0
    if reasoning > completion_tokens:
        log.warning("Provider reasoning count exceeds output tokens; breakdown omitted.")
        details["output_reasoning_tokens"] = None
        reasoning = 0
    attribute_names = {
        "input_cached_tokens": semconv.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS,
        "input_cache_creation": semconv.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS,
        "output_reasoning_tokens": semconv.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS,
    }
    measured = {key: value for key, value in details.items() if value is not None}
    if measured:
        for key, value in measured.items():
            span.set_attribute(attribute_names[key], value)
        # Langfuse's generic OTel parser subtracts cache but not reasoning.
        # Explicit usage_details takes precedence and is already disjoint.
        span.set_attribute(
            semconv.LANGFUSE_OBSERVATION_USAGE_DETAILS,
            json.dumps({
                "input": int(prompt_tokens) - cached,
                "output": int(completion_tokens) - reasoning,
                **measured,
                "total": int(prompt_tokens) + int(completion_tokens),
            }),
        )


class TracingLLMProvider(_TracingBase, LLMProvider):
    """Instrument every LLM call of the wrapped provider."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        provider_name: str,
        policy: ContentCapturePolicy,
        tracer_provider: Any | None = None,
    ) -> None:
        _TracingBase.__init__(
            self, provider, policy=policy, tracer_provider=tracer_provider
        )
        self._gen_ai_provider = provider_name

    # -- delegated capability surface (base-class defaults must never
    # -- shadow the wrapped provider's answers) -------------------------
    @property
    def complete_uses_metadata(self) -> bool:
        """Forward the wrapped provider's text/metadata equivalence declaration."""
        return getattr(self._provider, "complete_uses_metadata", False) is True

    @property
    def selectable_models(self) -> list[str]:
        return self._provider.selectable_models

    @property
    def context_window_tokens(self) -> int | None:
        return self._provider.context_window_tokens

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        return reported_capability(
            self._provider, "supports_structured_output", model=model
        )

    def supports_tool_calls(self, *, model: str | None = None) -> bool:
        return reported_capability(
            self._provider, "supports_tool_calls", model=model
        )

    def is_available(self) -> bool:
        return self._provider.is_available()

    # -- instrumentation helpers ----------------------------------------
    def _base_attributes(
        self,
        operation: str,
        *,
        model: str | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            semconv.GEN_AI_OPERATION_NAME: operation,
            semconv.GEN_AI_PROVIDER_NAME: self._gen_ai_provider,
        }
        if model:
            attributes[semconv.GEN_AI_REQUEST_MODEL] = model
        if reasoning_effort:
            attributes[semconv.INQTRIX_REASONING_EFFORT] = reasoning_effort
        attributes.update(self._context_attributes())
        return attributes

    def _record_response(
        self,
        span: Any | None,
        *,
        response_model: str,
        prompt_tokens: int,
        completion_tokens: int,
        finish_reason: str,
        request_max_tokens: int,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> None:
        if span is None:
            return
        if response_model:
            span.set_attribute(
                semconv.GEN_AI_RESPONSE_MODEL, response_model
            )
        _record_token_usage(
            span, prompt_tokens, completion_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            reasoning_tokens=reasoning_tokens,
        )
        if finish_reason:
            span.set_attribute(
                semconv.GEN_AI_RESPONSE_FINISH_REASONS, [finish_reason]
            )
            if finish_reason.strip().lower() in INCOMPLETE_FINISH_REASONS:
                # The provider stopped before the response was finished: a
                # WARNING generation, never a normal one.
                span.set_attribute(
                    semconv.LANGFUSE_OBSERVATION_LEVEL,
                    semconv.OBSERVATION_LEVEL_WARNING,
                )
                span.set_attribute(
                    semconv.LANGFUSE_OBSERVATION_STATUS_MESSAGE,
                    "provider stopped before completing the response "
                    f"(finish_reason={finish_reason})",
                )
        if request_max_tokens:
            span.set_attribute(
                semconv.GEN_AI_REQUEST_MAX_TOKENS, int(request_max_tokens)
            )

    def _record_prompt_content(
        self,
        span: Any | None,
        *,
        prompt: str,
        system: str | None,
        output_text: str,
        raw: dict[str, Any] | None,
    ) -> None:
        if span is None or not self._policy.capture_content:
            return
        if system:
            _set_content(
                span,
                semconv.GEN_AI_SYSTEM_INSTRUCTIONS,
                system,
                self._policy,
            )
        _set_content(
            span,
            semconv.GEN_AI_INPUT_MESSAGES,
            [{"role": "user", "content": prompt}],
            self._policy,
        )
        _set_content(
            span,
            semconv.GEN_AI_OUTPUT_MESSAGES,
            [{"role": "assistant", "content": output_text}],
            self._policy,
        )
        if raw:
            _set_content(
                span, semconv.INQTRIX_RESPONSE_RAW, raw, self._policy
            )
        else:
            span.set_attribute(semconv.INQTRIX_RAW_UNAVAILABLE, True)

    # -- instrumented calls ----------------------------------------------
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
        # The metadata method is the native providers' canonical call. Keep
        # that receipt before projecting to text; no second model invocation.
        # Custom text-only providers retain their accumulator-based path.
        if self.complete_uses_metadata:
            return self.complete_with_metadata(
                prompt, system=system, model=model,
                max_output_tokens=max_output_tokens, timeout=timeout,
                state=state, deadline=deadline, reasoning_effort=reasoning_effort,
            ).content
        attributes = self._base_attributes(
            semconv.OPERATION_TEXT_COMPLETION,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        with self._span("text_completion", attributes) as span:
            with _metered_llm(
                self._gen_ai_provider,
                semconv.OPERATION_TEXT_COMPLETION,
                model,
            ) as meter:
                # complete() returns bare text; its token usage only ever
                # reaches the caller through the state accumulator (the
                # SAME numbers quota books). Read the delta so the ledger
                # cannot systematically undercount this path.
                before_prompt = int((state or {}).get("total_prompt_tokens", 0) or 0)
                before_completion = int(
                    (state or {}).get("total_completion_tokens", 0) or 0
                )
                text = self._provider.complete(
                    prompt,
                    system=system,
                    model=model,
                    max_output_tokens=max_output_tokens,
                    timeout=timeout,
                    state=state,
                    deadline=deadline,
                    reasoning_effort=reasoning_effort,
                )
                if state is not None:
                    meter.prompt_tokens = max(
                        0,
                        int(state.get("total_prompt_tokens", 0) or 0)
                        - before_prompt,
                    )
                    meter.completion_tokens = max(
                        0,
                        int(state.get("total_completion_tokens", 0) or 0)
                        - before_completion,
                    )
            # Export the SAME numbers the ledger and Prometheus book, from
            # the same meter. Without this the span reaches the trace
            # backend carrying no usage at all, and Langfuse then infers
            # token counts from the message text — an invented number that
            # disagrees with the ledger for exactly this call path.
            if state is not None:
                self._record_response(
                    span,
                    response_model="",
                    prompt_tokens=meter.prompt_tokens,
                    completion_tokens=meter.completion_tokens,
                    finish_reason="",
                    request_max_tokens=int(max_output_tokens or 0),
                )
            elif span is not None:
                # No accumulator, so no honest number exists here. Exporting
                # zero would read as a free call; staying silent would let the
                # trace backend infer counts from the message text, as above.
                span.set_attribute(semconv.INQTRIX_USAGE_UNAVAILABLE, True)
            self._record_prompt_content(
                span, prompt=prompt, system=system, output_text=text, raw=None
            )
            return text

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
    ) -> "LLMResponse":
        attributes = self._base_attributes(
            semconv.OPERATION_TEXT_COMPLETION,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        started = time.monotonic()
        with self._span("text_completion", attributes) as span:
            with _metered_llm(
                self._gen_ai_provider,
                semconv.OPERATION_TEXT_COMPLETION,
                model,
            ) as meter:
                response = self._provider.complete_with_metadata(
                    prompt,
                    system=system,
                    model=model,
                    max_output_tokens=max_output_tokens,
                    timeout=timeout,
                    state=state,
                    deadline=deadline,
                    reasoning_effort=reasoning_effort,
                )
                meter.model = response.model or model
                meter.prompt_tokens = int(response.prompt_tokens or 0)
                meter.completion_tokens = int(
                    response.completion_tokens or 0
                )
                meter.finish_reason = str(response.finish_reason or "")
            response = replace(
                response,
                duration_ms=round((time.monotonic() - started) * 1000.0, 3),
            )
            self._record_response(
                span,
                response_model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_creation_tokens=response.cache_creation_tokens,
                reasoning_tokens=response.reasoning_tokens,
                finish_reason=response.finish_reason,
                request_max_tokens=response.request_max_tokens,
            )
            self._record_prompt_content(
                span,
                prompt=prompt,
                system=system,
                output_text=response.content,
                raw=response.raw,
            )
            return response

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
    ) -> "StructuredLLMResponse":
        attributes = self._base_attributes(
            semconv.OPERATION_TEXT_COMPLETION,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        attributes[semconv.INQTRIX_SCHEMA_NAME] = schema_name
        started = time.monotonic()
        with self._span("text_completion", attributes) as span:
            with _metered_llm(
                self._gen_ai_provider,
                semconv.OPERATION_TEXT_COMPLETION,
                model,
            ) as meter:
                response = self._provider.complete_structured(
                    prompt,
                    schema=schema,
                    schema_name=schema_name,
                    schema_description=schema_description,
                    system=system,
                    model=model,
                    max_output_tokens=max_output_tokens,
                    timeout=timeout,
                    state=state,
                    deadline=deadline,
                    reasoning_effort=reasoning_effort,
                )
                meter.model = response.model or model
                meter.prompt_tokens = int(response.prompt_tokens or 0)
                meter.completion_tokens = int(
                    response.completion_tokens or 0
                )
                meter.finish_reason = str(response.finish_reason or "")
            response = replace(
                response,
                duration_ms=round((time.monotonic() - started) * 1000.0, 3),
            )
            self._record_response(
                span,
                response_model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cache_read_tokens=response.cache_read_tokens,
                cache_creation_tokens=response.cache_creation_tokens,
                reasoning_tokens=response.reasoning_tokens,
                finish_reason=response.finish_reason,
                request_max_tokens=response.request_max_tokens,
            )
            self._record_prompt_content(
                span,
                prompt=prompt,
                system=system,
                output_text=response.content,
                raw=response.raw,
            )
            return response

    def _record_chat_turn(self, span, turn, *, raw_tool_calls=None):
        self._record_response(
            span,
            response_model=turn.model,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            cache_read_tokens=turn.cache_read_tokens,
            cache_creation_tokens=turn.cache_creation_tokens,
            reasoning_tokens=turn.reasoning_tokens,
            finish_reason=turn.finish_reason,
            request_max_tokens=0,
        )
        if span is None:
            return
        calls = (
            [
                {"id": call["id"], "name": call["function"]["name"],
                 "arguments": call["function"]["arguments"]}
                for call in raw_tool_calls
            ]
            if raw_tool_calls is not None
            else [
                {"id": call.id, "name": call.name, "arguments": call.arguments}
                for call in turn.tool_calls
            ]
        )
        span.set_attribute(semconv.INQTRIX_TOOL_CALL_COUNT, len(calls))
        if self._policy.capture_content:
            output_message: dict[str, Any] = {"role": "assistant", "content": turn.text}
            if calls:
                output_message["tool_calls"] = calls
            _set_content(span, semconv.GEN_AI_OUTPUT_MESSAGES, [output_message], self._policy)
            if turn.raw is not None:
                _set_content(span, semconv.INQTRIX_RESPONSE_RAW, turn.raw, self._policy)
            else:
                span.set_attribute(semconv.INQTRIX_RAW_UNAVAILABLE, True)

    def chat(
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
    ) -> "ChatTurn":
        attributes = self._base_attributes(
            semconv.OPERATION_CHAT,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if tool_choice is not None:
            attributes[semconv.INQTRIX_TOOL_CHOICE] = tool_choice
        started = time.monotonic()
        with self._span("chat", attributes) as span:
            if span is not None and self._policy.capture_content:
                _set_content(span, semconv.GEN_AI_INPUT_MESSAGES, messages, self._policy)
            with _metered_llm(
                self._gen_ai_provider, semconv.OPERATION_CHAT, model
            ) as meter:
                try:
                    turn = self._provider.chat(
                        messages,
                        tools=tools,
                        model=model,
                        max_output_tokens=max_output_tokens,
                        timeout=timeout,
                        deadline=deadline,
                        reasoning_effort=reasoning_effort,
                        # Only when set: a wrapped provider that never
                        # learned the parameter still serves plain turns.
                        **({"tool_choice": tool_choice} if tool_choice is not None else {}),
                    )
                except Exception as exc:
                    from inqtrix.execution_failures import classify_execution_failure

                    if span is not None:
                        span.set_attribute(
                            semconv.INQTRIX_MODEL_FAILURE_REASON,
                            classify_execution_failure(exc),
                        )
                    if isinstance(exc, AgentToolArgumentsError):
                        turn = exc.turn
                        meter.model = turn.model or model
                        meter.prompt_tokens = int(turn.prompt_tokens or 0)
                        meter.completion_tokens = int(turn.completion_tokens or 0)
                        self._record_chat_turn(span, turn, raw_tool_calls=exc.raw_tool_calls)
                    elif span is not None and self._policy.capture_content:
                        original = getattr(exc, "original", None) or exc
                        body = getattr(original, "body", None)
                        if isinstance(body, dict):
                            _set_content(span, semconv.INQTRIX_RESPONSE_RAW, body, self._policy)
                        else:
                            span.set_attribute(semconv.INQTRIX_RAW_UNAVAILABLE, True)
                    raise
                meter.model = turn.model or model
                meter.prompt_tokens = int(turn.prompt_tokens or 0)
                meter.completion_tokens = int(turn.completion_tokens or 0)
                meter.finish_reason = str(turn.finish_reason or "")
            turn = replace(
                turn,
                duration_ms=round((time.monotonic() - started) * 1000.0, 3),
            )
            self._record_chat_turn(span, turn)
            return turn


class TracingSearchProvider(_TracingBase, SearchProvider):
    """Instrument every web-search call of the wrapped provider."""

    def __init__(
        self,
        provider: SearchProvider,
        *,
        policy: ContentCapturePolicy,
        tracer_provider: Any | None = None,
    ) -> None:
        _TracingBase.__init__(
            self, provider, policy=policy, tracer_provider=tracer_provider
        )

    @property
    def search_model(self) -> str:
        return self._provider.search_model

    def is_available(self) -> bool:
        return self._provider.is_available()

    def search(
        self,
        query: str,
        *,
        search_context_size: str = "high",
        recency_filter: str | None = None,
        language_filter: list[str] | None = None,
        domain_filter: list[str] | None = None,
        search_mode: str | None = None,
        return_related: bool = False,
        deadline: float | None = None,
    ) -> "GroundedSearchResult":
        # The query text is user content and can carry PII, so it is
        # attached ONLY under content capture (below), like every other
        # content attribute — never as always-on metadata. Provider,
        # engine, source count and latency keep the span useful without
        # it.
        #
        # A grounded web search IS a model generation on the provider
        # side (the search agent reads pages and writes the answer), and
        # its tokens are billed and booked like any other call. The
        # span is therefore exported as a Langfuse generation with the
        # engine as its model and the provider's usage in the standard
        # keys — otherwise the trace total silently misses the search
        # share (44 % of a Tief run, measured).
        engine = str(self._provider.search_model or "unknown")
        attributes: dict[str, Any] = {
            semconv.LANGFUSE_OBSERVATION_TYPE: (
                semconv.OBSERVATION_TYPE_GENERATION
            ),
            semconv.GEN_AI_PROVIDER_NAME: type(self._provider).__name__,
            semconv.GEN_AI_REQUEST_MODEL: engine,
            semconv.INQTRIX_SEARCH_PROVIDER: type(self._provider).__name__,
            semconv.INQTRIX_SEARCH_ENGINE: self._provider.search_model,
        }
        attributes.update(self._context_attributes())
        if search_mode:
            attributes[semconv.INQTRIX_SEARCH_MODE] = search_mode
        if recency_filter:
            attributes[semconv.INQTRIX_SEARCH_RECENCY] = recency_filter
        if domain_filter:
            attributes[semconv.INQTRIX_SEARCH_DOMAIN_FILTER_COUNT] = len(
                domain_filter
            )
        with self._span("web_search", attributes) as span:
            with _metered_search(
                type(self._provider).__name__,
                str(self._provider.search_model or "unknown"),
            ) as search_meter:
                result = self._provider.search(
                    query,
                    search_context_size=search_context_size,
                    recency_filter=recency_filter,
                    language_filter=language_filter,
                    domain_filter=domain_filter,
                    search_mode=search_mode,
                    return_related=return_related,
                    deadline=deadline,
                )
                search_meter.prompt_tokens = int(result.prompt_tokens or 0)
                search_meter.model = result.model or engine
                search_meter.completion_tokens = int(
                    result.completion_tokens or 0
                )
            if span is not None:
                span.set_attribute(
                    semconv.INQTRIX_SEARCH_SOURCE_COUNT, len(result.sources)
                )
                span.set_attribute(
                    semconv.INQTRIX_SEARCH_ANSWER_LENGTH,
                    len(result.answer or ""),
                )
                _record_token_usage(
                    span, result.prompt_tokens, result.completion_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_creation_tokens=result.cache_creation_tokens,
                    reasoning_tokens=result.reasoning_tokens,
                )
                if result.model:
                    span.set_attribute(semconv.GEN_AI_RESPONSE_MODEL, result.model)
                if result.tool_call_count is not None:
                    span.set_attribute(semconv.INQTRIX_SEARCH_TOOL_CALL_COUNT, result.tool_call_count)
                if self._policy.capture_content:
                    if result.raw is not None:
                        _set_content(span, semconv.INQTRIX_RESPONSE_RAW, result.raw, self._policy)
                    else:
                        span.set_attribute(semconv.INQTRIX_RAW_UNAVAILABLE, True)
                    _set_content(
                        span,
                        semconv.INQTRIX_SEARCH_QUERY,
                        query,
                        self._policy,
                        json_text_key=semconv.LANGFUSE_OBSERVATION_INPUT,
                    )
                    _set_content(
                        span,
                        semconv.INQTRIX_SEARCH_ANSWER,
                        result.answer,
                        self._policy,
                        json_text_key=semconv.LANGFUSE_OBSERVATION_OUTPUT,
                    )
                if self._policy.capture_content and result.sources:
                    # The per-source records ARE the evidence a bad
                    # report has to be explained from — without them the
                    # trace shows a provider answer whose provenance
                    # cannot be reconstructed. Content, so the same
                    # redaction + cap + truncation-event path applies.
                    _set_content(
                        span,
                        semconv.INQTRIX_SEARCH_SOURCES,
                        [
                            {
                                "url": source.url,
                                "title": source.title,
                                "snippet": source.snippet,
                                "date": source.date,
                                "last_updated": source.last_updated,
                                "rank": source.rank,
                                "origin": source.origin,
                                "spans": [
                                    {"start": span.start, "end": span.end}
                                    for span in source.spans
                                ],
                            }
                            for source in result.sources
                        ],
                        self._policy,
                    )
            return result


class TracingEmbeddingProvider(_TracingBase, EmbeddingProvider):
    """Instrument embedding calls (ingestion batches and queries)."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        *,
        policy: ContentCapturePolicy,
        tracer_provider: Any | None = None,
    ) -> None:
        _TracingBase.__init__(
            self, provider, policy=policy, tracer_provider=tracer_provider
        )

    @property
    def selectable_embedding_models(self) -> list[str]:
        return self._provider.selectable_embedding_models

    @property
    def default_model(self) -> str:
        return self._provider.default_model

    def _attributes(self, model: str | None, count: int) -> dict[str, Any]:
        attributes = {
            semconv.GEN_AI_OPERATION_NAME: semconv.OPERATION_EMBEDDINGS,
            semconv.GEN_AI_PROVIDER_NAME: type(self._provider).__name__,
            semconv.GEN_AI_REQUEST_MODEL: model or self.default_model,
            semconv.INQTRIX_EMBED_TEXT_COUNT: count,
        }
        attributes.update(self._context_attributes())
        return attributes

    def embed_documents(
        self, texts: list[str], *, model: str | None = None
    ) -> list[list[float]]:
        with self._span(
            "embeddings", self._attributes(model, len(texts))
        ):
            with _metered_llm(
                "embeddings",
                semconv.OPERATION_EMBEDDINGS,
                model or self.default_model,
            ) as meter:
                from inqtrix.quota.models import estimate_tokens

                vectors = self._provider.embed_documents(texts, model=model)
                # Same estimator the quota path books against the same texts,
                # so ledger and quota agree by construction. A failed call
                # keeps the zero row, exactly like the search wrapper.
                meter.prompt_tokens = sum(estimate_tokens(text) for text in texts)
                return vectors

    def embed_query(
        self, text: str, *, model: str | None = None
    ) -> list[float]:
        with self._span("embeddings", self._attributes(model, 1)):
            with _metered_llm(
                "embeddings",
                semconv.OPERATION_EMBEDDINGS,
                model or self.default_model,
            ) as meter:
                from inqtrix.quota.models import estimate_tokens

                vector = self._provider.embed_query(text, model=model)
                meter.prompt_tokens = estimate_tokens(text)
                return vector

def instrument_llm(
    provider: LLMProvider,
    *,
    provider_name: str,
    policy: ContentCapturePolicy,
    tracer_provider: Any | None = None,
) -> LLMProvider:
    """Wrap one LLM provider (idempotent — never double-wraps).

    Looks along the whole ``_provider`` chain, not at the outermost
    object only: the untrusted-content rule decorator sits outside the
    tracing layer, and a second tracing layer under it would record every
    call twice (two spans, two metric observations, two ledger rows).
    """
    link: Any = provider
    seen: set[int] = set()
    while link is not None and id(link) not in seen:
        if isinstance(link, TracingLLMProvider):
            return provider
        seen.add(id(link))
        link = vars(link).get("_provider") if hasattr(link, "__dict__") else None
    return TracingLLMProvider(
        provider,
        provider_name=provider_name,
        policy=policy,
        tracer_provider=tracer_provider,
    )


def instrument_search(
    provider: SearchProvider,
    *,
    policy: ContentCapturePolicy,
    tracer_provider: Any | None = None,
) -> SearchProvider:
    """Wrap one search provider (idempotent)."""
    if isinstance(provider, TracingSearchProvider):
        return provider
    return TracingSearchProvider(
        provider, policy=policy, tracer_provider=tracer_provider
    )


def instrument_embeddings(
    provider: EmbeddingProvider,
    *,
    policy: ContentCapturePolicy,
    tracer_provider: Any | None = None,
) -> EmbeddingProvider:
    """Wrap one embedding provider (idempotent)."""
    if isinstance(provider, TracingEmbeddingProvider):
        return provider
    return TracingEmbeddingProvider(
        provider, policy=policy, tracer_provider=tracer_provider
    )
