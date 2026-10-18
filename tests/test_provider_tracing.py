"""Provider tracing wrappers: spans, duration_ms, content policy.

Tests run against their OWN TracerProvider (passed into the wrappers) —
no process-global tracing state is touched.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from inqtrix.observability.content import ContentCapturePolicy
from inqtrix.observability.provider_tracing import (
    TracingLLMProvider,
    instrument_embeddings,
    instrument_llm,
    instrument_search,
)
from inqtrix.providers.base import (
    ChatTurn,
    LLMProvider,
    LLMResponse,
    SearchProvider,
    StructuredLLMResponse,
    ToolCallRequest,
)
from inqtrix.providers.embeddings import EmbeddingProvider
from inqtrix.runtime_logging import describe_llm_provider
from inqtrix.search_result import CitationSpan, GroundedSearchResult, GroundedSource
from inqtrix.services.health_service import provider_label

SUMMARY = ContentCapturePolicy(configured_capture=False, max_attr_bytes=32_768)
FORENSIC = ContentCapturePolicy(configured_capture=True, max_attr_bytes=32_768)


class FakeLLM(LLMProvider):
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def complete(self, prompt, **kwargs) -> str:
        # Real providers report a bare completion's usage only by adding it
        # to the state accumulator (`track_tokens`); there is no return
        # value carrying it. Mirror that here, otherwise the meter this
        # path reads stays empty and the test proves nothing.
        state = kwargs.get("state")
        if state is not None:
            state["total_prompt_tokens"] = (
                int(state.get("total_prompt_tokens", 0) or 0) + 11
            )
            state["total_completion_tokens"] = (
                int(state.get("total_completion_tokens", 0) or 0) + 7
            )
        return "text-answer"

    def complete_with_metadata(self, prompt, **kwargs) -> LLMResponse:
        if self.fail:
            raise RuntimeError("backend down")
        return LLMResponse(
            content="answer https://api.example.com/x?api_key=SECRET123",
            prompt_tokens=11,
            completion_tokens=7,
            model="fake-1",
            finish_reason="stop",
            raw={"api_key": "SECRET123", "usage": {"total": 18}},
            request_max_tokens=256,
        )

    def complete_structured(self, prompt, **kwargs) -> StructuredLLMResponse:
        return StructuredLLMResponse(
            parsed={"ok": True},
            content='{"ok": true}',
            prompt_tokens=3,
            completion_tokens=2,
            model="fake-1",
            finish_reason="stop",
            schema_name=kwargs.get("schema_name", ""),
        )

    def supports_tool_calls(self, *, model=None) -> bool:
        return True

    def supports_structured_output(self, *, model=None) -> bool:
        return True

    def chat(self, messages, **kwargs) -> ChatTurn:
        return ChatTurn(
            text="",
            tool_calls=(
                ToolCallRequest(id="c1", name="web_search", arguments={"q": "x"}),
            ),
            finish_reason="tool_calls",
            model="fake-1",
            prompt_tokens=5,
            completion_tokens=4,
        )

    def is_available(self) -> bool:
        return True


class FakeSearch(SearchProvider):
    def search(self, query, **kwargs) -> GroundedSearchResult:
        return GroundedSearchResult(
            answer="the answer",
            sources=[GroundedSource(url="https://example.com", rank=1)],
            prompt_tokens=9,
            completion_tokens=6,
        )

    def is_available(self) -> bool:
        return True

    @property
    def search_model(self) -> str:
        return "fake-engine"


class TextOnlyLLM(FakeLLM):
    """A custom provider that exposes no native metadata method."""

    complete_with_metadata = LLMProvider.complete_with_metadata


class MetadataLLM(FakeLLM):
    """A native provider whose text method projects the metadata response."""

    complete_uses_metadata = True

    def complete(self, prompt, **kwargs):
        return self.complete_with_metadata(prompt, **kwargs).content


class SuppliedSearch(FakeSearch):
    def __init__(self, result: GroundedSearchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def search(self, query, **kwargs) -> GroundedSearchResult:
        self.calls.append((query, kwargs))
        return self.result


class FakeEmbeddings(EmbeddingProvider):
    @property
    def default_model(self) -> str:
        return "fake-embed"

    def embed_documents(self, texts, *, model=None):
        return [[0.0] for _ in texts]

    def embed_query(self, text, *, model=None):
        return [0.0]


@pytest.fixture()
def exporter_and_provider():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter, provider
    provider.shutdown()


def _wrap_llm(policy, provider, fail: bool = False) -> TracingLLMProvider:
    return TracingLLMProvider(
        FakeLLM(fail=fail),
        provider_name="fake",
        policy=policy,
        tracer_provider=provider,
    )


def test_rejected_tool_json_retains_content_and_billed_usage(exporter_and_provider, monkeypatch):
    from inqtrix.exceptions import AgentToolArgumentsError
    from inqtrix.observability import semconv
    from tests.agents.test_kernel_model_recovery import provider, requested, wire

    exporter, tracer = exporter_and_provider
    receipts = []
    monkeypatch.setattr("inqtrix.usage.recorder.record_provider_call", lambda **data: receipts.append(data))
    raw = '{"text":"MARKE1"'
    inner = provider(wire(requested("call_bad", "write_todos", raw)))
    wrapped = TracingLLMProvider(inner, provider_name="openai", policy=FORENSIC, tracer_provider=tracer)
    messages = [{"role": "user", "content": "Record the task."}]
    with pytest.raises(AgentToolArgumentsError):
        wrapped.chat(messages)
    span = exporter.get_finished_spans()[-1]
    assert span.status.is_ok is False
    assert span.attributes[semconv.INQTRIX_MODEL_FAILURE_REASON] == "model_tool_arguments"
    assert json.loads(span.attributes[semconv.GEN_AI_INPUT_MESSAGES]) == messages
    output = json.loads(span.attributes[semconv.GEN_AI_OUTPUT_MESSAGES])
    assert output[0]["tool_calls"][0]["arguments"] == raw
    assert span.attributes[semconv.GEN_AI_USAGE_INPUT_TOKENS] == 11
    assert span.attributes[semconv.GEN_AI_USAGE_OUTPUT_TOKENS] == 7
    assert len(receipts) == 1
    assert receipts[0]["input_tokens"] == 11
    assert receipts[0]["output_tokens"] == 7


def test_failed_chat_captures_input_before_the_provider_raises(exporter_and_provider):
    from inqtrix.observability import semconv
    from tests.agents.test_kernel_model_recovery import provider

    exporter, tracer = exporter_and_provider
    inner = provider(RuntimeError("Injected internal provider failure."))
    wrapped = TracingLLMProvider(inner, provider_name="openai", policy=FORENSIC, tracer_provider=tracer)
    messages = [{"role": "user", "content": "MARKE2"}]
    with pytest.raises(RuntimeError, match="Injected internal"):
        wrapped.chat(messages)
    span = exporter.get_finished_spans()[-1]
    assert json.loads(span.attributes[semconv.GEN_AI_INPUT_MESSAGES]) == messages
    assert span.attributes[semconv.INQTRIX_MODEL_FAILURE_REASON] == "server_error"


@pytest.mark.parametrize("operation", ["complete_with_metadata", "complete_structured", "chat"])
def test_usage_breakdown_is_disjoint_without_changing_totals(
    exporter_and_provider, monkeypatch, operation,
):
    exporter, provider = exporter_and_provider
    values = dict(prompt_tokens=11, completion_tokens=7, cache_read_tokens=5,
                  cache_creation_tokens=2, reasoning_tokens=3)
    response = {
        "complete_with_metadata": LLMResponse(content="ok", **values),
        "complete_structured": StructuredLLMResponse(parsed={}, **values),
        "chat": ChatTurn(text="ok", **values),
    }[operation]
    fake = FakeLLM()
    monkeypatch.setattr(fake, operation, lambda *_, **__: response)
    wrapped = TracingLLMProvider(fake, provider_name="fake", policy=SUMMARY, tracer_provider=provider)
    kwargs = {"schema": {}, "schema_name": "test"} if operation == "complete_structured" else {}
    result = getattr(wrapped, operation)([] if operation == "chat" else "test", **kwargs)
    assert (result.cache_read_tokens, result.cache_creation_tokens, result.reasoning_tokens) == (5, 2, 3)
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 5
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 2
    assert attrs["gen_ai.usage.reasoning.output_tokens"] == 3
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 4, "output": 4, "input_cached_tokens": 5,
        "input_cache_creation": 2, "output_reasoning_tokens": 3, "total": 18,
    }


@pytest.mark.parametrize("details,expected,warning", [
    ({}, None, False),
    ({"cache_read_tokens": 0, "reasoning_tokens": 0},
     {"input": 11, "output": 7, "input_cached_tokens": 0, "output_reasoning_tokens": 0, "total": 18}, False),
    ({"cache_read_tokens": 12, "reasoning_tokens": 8}, None, True),
    ({"cache_read_tokens": -1, "reasoning_tokens": True}, None, True),
])
def test_missing_zero_and_invalid_breakdowns_stay_distinct(
    exporter_and_provider, monkeypatch, caplog, details, expected, warning,
):
    exporter, provider = exporter_and_provider
    fake = FakeLLM()
    monkeypatch.setattr(fake, "complete_with_metadata", lambda *_, **__: LLMResponse(
        content="ok", prompt_tokens=11, completion_tokens=7, **details,
    ))
    wrapped = TracingLLMProvider(fake, provider_name="fake", policy=SUMMARY, tracer_provider=provider)
    wrapped.complete_with_metadata("test")
    attrs = dict(exporter.get_finished_spans()[0].attributes)
    actual = attrs.get("langfuse.observation.usage_details")
    assert (json.loads(actual) if actual else None) == expected
    assert (attrs["gen_ai.usage.input_tokens"], attrs["gen_ai.usage.output_tokens"]) == (11, 7)
    assert bool(caplog.records) == warning


def test_metadata_span_and_duration(exporter_and_provider):
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    response = llm.complete_with_metadata("why?")
    assert response.duration_ms > 0
    (span,) = exporter.get_finished_spans()
    assert span.name == "text_completion"
    attrs = dict(span.attributes)
    assert attrs["gen_ai.operation.name"] == "text_completion"
    assert attrs["gen_ai.provider.name"] == "fake"
    assert attrs["gen_ai.response.model"] == "fake-1"
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert attrs["gen_ai.request.max_tokens"] == 256
    assert tuple(attrs["gen_ai.response.finish_reasons"]) == ("stop",)
    # Summary policy: no content attributes at all.
    assert "gen_ai.input.messages" not in attrs
    assert "gen_ai.output.messages" not in attrs
    assert "inqtrix.response.raw" not in attrs


def test_forensic_content_is_redacted(exporter_and_provider):
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(FORENSIC, provider)
    llm.complete_with_metadata(
        "check https://api.example.com/x?api_key=SECRET123"
    )
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert "SECRET123" not in attrs["gen_ai.input.messages"]
    assert "SECRET123" not in attrs["gen_ai.output.messages"]
    raw = attrs["inqtrix.response.raw"]
    assert "SECRET123" not in raw
    # Non-sensitive structure survives the sanitizer.
    assert "usage" in raw


def test_truncation_emits_visible_event(exporter_and_provider):
    exporter, provider = exporter_and_provider
    tiny = ContentCapturePolicy(configured_capture=True, max_attr_bytes=64)
    llm = TracingLLMProvider(
        FakeLLM(),
        provider_name="fake",
        policy=tiny,
        tracer_provider=provider,
    )
    llm.complete_with_metadata("x" * 500)
    (span,) = exporter.get_finished_spans()
    events = [e for e in span.events if e.name == "inqtrix.truncation"]
    assert events
    attrs = dict(events[0].attributes)
    assert attrs["original_size"] > attrs["capped_size"]
    assert attrs["limit_name"]


def test_error_is_recorded_on_span(exporter_and_provider):
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider, fail=True)
    with pytest.raises(RuntimeError, match="backend down"):
        llm.complete_with_metadata("boom")
    (span,) = exporter.get_finished_spans()
    assert not span.status.is_ok
    assert any(e.name == "exception" for e in span.events)


@pytest.mark.parametrize("policy", [SUMMARY, FORENSIC])
def test_a_failed_provider_call_is_recorded_without_the_error_text(
    exporter_and_provider, policy
):
    """A provider's error text can repeat the prompt or the answer; the span
    records the exception by type and code only, with content capture on or
    off."""
    from tests._tracing_helpers import everything_a_span_exports

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(policy, provider, fail=True)
    with pytest.raises(RuntimeError, match="backend down"):
        llm.complete_with_metadata("boom")
    (span,) = exporter.get_finished_spans()
    assert "backend down" not in everything_a_span_exports(span)
    (event,) = [event for event in span.events if event.name == "exception"]
    assert event.attributes["exception.type"] == "RuntimeError"


@pytest.mark.parametrize("incognito", [True, False])
def test_an_incognito_chat_keeps_its_content_out_of_every_span_of_the_turn(
    exporter_and_provider, incognito
):
    """With content capture on, an incognito chat records no prompt and no
    answer, also for a call handed to another thread inside the turn; the
    chat span says the content was withheld on purpose, and token usage is
    still recorded. The normal chat is the control that the probe finds
    content where there is some."""
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from inqtrix.observability.context import bound_thread_call
    from inqtrix.observability.otel import chat_thread_call
    from tests._tracing_helpers import everything_a_span_exports

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(FORENSIC, provider)

    def turn() -> SimpleNamespace:
        llm.complete_with_metadata("Frage SENTINEL-INCOGNITO-PROMPT")
        hop = bound_thread_call(
            lambda: llm.complete_with_metadata("Nachfrage SENTINEL-INCOGNITO-PROMPT")
        )
        with ThreadPoolExecutor(max_workers=1) as inner:
            inner.submit(hop).result()
        return SimpleNamespace(raw={})

    runner = chat_thread_call(
        turn,
        mode="direct_llm",
        principal=None,
        incognito=incognito,
        tracer_provider=provider,
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(runner).result()

    spans = exporter.get_finished_spans()
    (chat,) = [span for span in spans if span.name == "inqtrix.chat"]
    generations = [span for span in spans if span is not chat]
    assert len(generations) == 2
    assert {span.get_span_context().trace_id for span in generations} == {
        chat.get_span_context().trace_id
    }
    assert all(
        span.attributes["gen_ai.usage.input_tokens"] == 11 for span in generations
    )
    exported = [everything_a_span_exports(span) for span in generations]
    marker = dict(chat.attributes)
    if incognito:
        everything = "\n".join([*exported, everything_a_span_exports(chat)])
        assert "SENTINEL-INCOGNITO-PROMPT" not in everything
        assert "api.example.com" not in everything
        assert marker["inqtrix.incognito"] is True
        assert marker["langfuse.trace.metadata.incognito"] is True
    else:
        assert all("SENTINEL-INCOGNITO-PROMPT" in text for text in exported)
        assert all("api.example.com" in text for text in exported)
        assert "inqtrix.incognito" not in marker


@pytest.mark.parametrize("incognito", [True, False])
def test_an_incognito_run_keeps_its_content_out_of_the_run_trace(
    exporter_and_provider, monkeypatch, incognito
):
    """A worker executes a run from its persisted request alone; an
    incognito ask marked there records no prompt and no answer under the
    run span, with content capture on, and the run span says why. The
    ordinary run is the control that the probe finds content."""
    from inqtrix.observability import otel as otel_module
    from tests._tracing_helpers import ProxyTrace, everything_a_span_exports

    exporter, provider = exporter_and_provider
    monkeypatch.setattr(otel_module, "_otel_trace", ProxyTrace(provider))
    llm = _wrap_llm(FORENSIC, provider)
    body = {"mode": "knowledge", **({"incognito": True} if incognito else {})}

    with otel_module.run_execute_span(
        run_id="run_1", tenant_id="default", attempt=1, payload={"body": body}
    ):
        llm.complete_with_metadata("Frage SENTINEL-INCOGNITO-RUN")

    spans = exporter.get_finished_spans()
    (run,) = [span for span in spans if span.name == "inqtrix.run"]
    (generation,) = [span for span in spans if span is not run]
    assert generation.parent.span_id == run.get_span_context().span_id
    assert generation.attributes["gen_ai.usage.input_tokens"] == 11
    exported = everything_a_span_exports(generation)
    marker = dict(run.attributes)
    if incognito:
        assert "SENTINEL-INCOGNITO-RUN" not in exported
        assert "api.example.com" not in exported
        assert marker["inqtrix.incognito"] is True
        assert marker["langfuse.trace.metadata.incognito"] is True
    else:
        assert "SENTINEL-INCOGNITO-RUN" in exported
        assert "inqtrix.incognito" not in marker


def test_chat_span_counts_tool_calls_and_sets_duration(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(FORENSIC, provider)
    turn = llm.chat([{"role": "user", "content": "hi"}])
    assert turn.duration_ms > 0
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert span.name == "chat"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["inqtrix.response.tool_call_count"] == 1
    output = json.loads(attrs["gen_ai.output.messages"])
    assert output[0]["tool_calls"][0]["name"] == "web_search"


def test_chat_trace_keeps_complete_tool_arguments_with_nested_redaction(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider
    query = "context " * 60 + "https://a.example?api_key=SECRET789"
    title = "source title " * 30
    text = "details " * 3000
    arguments = {
        "query": query,
        "nested": [{"title": title, "password": "nested-secret"}],
        "items": ({"text": text},),
    }

    class ArgumentLLM(FakeLLM):
        def chat(self, messages, **kwargs):
            return ChatTurn(text="", tool_calls=(
                ToolCallRequest(id="call-1", name="lookup", arguments=arguments),
            ), finish_reason="tool_calls", model="fake-1")

    llm = TracingLLMProvider(
        ArgumentLLM(), provider_name="fake", policy=FORENSIC,
        tracer_provider=provider,
    )
    llm.chat([{"role": "user", "content": "question"}])
    (span,) = exporter.get_finished_spans()
    output = json.loads(span.attributes["gen_ai.output.messages"])
    captured = output[0]["tool_calls"][0]["arguments"]
    assert captured["query"] == "context " * 60 + "https://a.example?api_key=[REDACTED]"
    assert captured["nested"] == [{"title": title}]
    assert captured["items"] == [{"text": text}]
    assert arguments["nested"][0]["password"] == "nested-secret"
    assert not any(event.name == "inqtrix.truncation" for event in span.events)


def test_structured_span_carries_schema_name(exporter_and_provider):
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    response = llm.complete_structured(
        "extract", schema={"type": "object"}, schema_name="claims_v1"
    )
    assert response.duration_ms > 0
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes)["inqtrix.request.schema_name"] == "claims_v1"


def test_chat_span_carries_the_obligation_to_call_a_tool(exporter_and_provider):
    """The obligation a role's turn is sent with stays readable in the
    trace, and reaches the wrapped provider; a plain turn carries none."""
    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    llm.chat([{"role": "user", "content": "hi"}], tool_choice="required")
    llm.chat([{"role": "user", "content": "hi"}])
    first, second = exporter.get_finished_spans()
    assert dict(first.attributes)["inqtrix.request.tool_choice"] == "required"
    assert "inqtrix.request.tool_choice" not in dict(second.attributes)


def test_search_span_metadata_and_content_policy(exporter_and_provider):
    exporter, provider = exporter_and_provider
    search = instrument_search(
        FakeSearch(), policy=SUMMARY, tracer_provider=provider
    )
    result = search.search("find https://x.example?token=SECRET987 things")
    assert result.answer == "the answer"
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert span.name == "web_search"
    assert attrs["inqtrix.search.provider"] == "FakeSearch"
    assert attrs["inqtrix.search.engine"] == "fake-engine"
    assert attrs["inqtrix.search.source_count"] == 1
    # A grounded search is a model generation on the provider side: it is
    # exported as a Langfuse generation with the engine as its model and
    # its usage in the standard keys — the only keys Langfuse sums — and
    # never as inqtrix.search.*_tokens duplicates.
    assert attrs["langfuse.observation.type"] == "generation"
    assert attrs["gen_ai.request.model"] == "fake-engine"
    assert attrs["gen_ai.usage.input_tokens"] == 9
    assert attrs["gen_ai.usage.output_tokens"] == 6
    assert "inqtrix.search.input_tokens" not in attrs
    assert "inqtrix.search.output_tokens" not in attrs
    # Summary policy: query and answer are user content and stay out.
    assert "inqtrix.search.query" not in attrs
    assert "inqtrix.search.answer" not in attrs


def test_search_query_is_content_gated_and_redacted(exporter_and_provider):
    exporter, provider = exporter_and_provider
    search = instrument_search(
        FakeSearch(), policy=FORENSIC, tracer_provider=provider
    )
    search.search("find https://x.example?token=SECRET987 things")
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert "inqtrix.search.query" in attrs
    assert "SECRET987" not in attrs["inqtrix.search.query"]
    assert "inqtrix.search.answer" in attrs


@pytest.mark.parametrize("text", ["", "null", "true", '{"items": [1, 2]}', '"quoted"', '日本語 🌍\nnext \\ line'])
def test_search_standard_io_preserves_text_and_provider_usage(
    exporter_and_provider, text,
):
    exporter, provider = exporter_and_provider
    supplied = GroundedSearchResult(answer=text, prompt_tokens=23, completion_tokens=7)
    backend = SuppliedSearch(supplied)
    search = instrument_search(backend, policy=FORENSIC, tracer_provider=provider)

    assert search.search(text, language_filter=["en"]) is supplied
    assert len(backend.calls) == 1
    assert backend.calls[0][0] == text
    assert backend.calls[0][1]["language_filter"] == ["en"]
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert "langfuse.observation.input" in attrs
    assert "langfuse.observation.output" in attrs
    assert json.loads(attrs["langfuse.observation.input"]) == text
    assert json.loads(attrs["langfuse.observation.output"]) == text
    assert attrs["inqtrix.search.query"] == text
    assert attrs["inqtrix.search.answer"] == text
    assert attrs["gen_ai.usage.input_tokens"] == 23
    assert attrs["gen_ai.usage.output_tokens"] == 7


def test_search_standard_io_is_content_gated(exporter_and_provider):
    exporter, provider = exporter_and_provider
    search = instrument_search(FakeSearch(), policy=SUMMARY, tracer_provider=provider)
    search.search("private question")
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    for key in (
        "langfuse.observation.input", "langfuse.observation.output",
        "inqtrix.search.query", "inqtrix.search.answer", "inqtrix.search.sources",
    ):
        assert key not in attrs


def test_search_standard_io_shares_redacted_content(exporter_and_provider):
    exporter, provider = exporter_and_provider
    text = "See https://api.example.com/report?api_key=SECRET987"
    search = instrument_search(
        SuppliedSearch(GroundedSearchResult(answer=text)),
        policy=FORENSIC, tracer_provider=provider,
    )
    search.search(text)
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    for native, existing in (
        ("langfuse.observation.input", "inqtrix.search.query"),
        ("langfuse.observation.output", "inqtrix.search.answer"),
    ):
        assert native in attrs
        assert "SECRET987" not in attrs[native]
        assert json.loads(attrs[native]) == attrs[existing]
        assert "https://api.example.com/report" in attrs[existing]


def test_search_standard_io_truncation_preserves_json_and_shared_text(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider
    policy = ContentCapturePolicy(configured_capture=True, max_attr_bytes=64)
    text = '日本語 🌍 "quote"\n' * 30
    supplied = GroundedSearchResult(answer=text)
    search = instrument_search(SuppliedSearch(supplied), policy=policy, tracer_provider=provider)
    assert search.search(text) is supplied
    assert supplied.answer == text
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    for native, existing in (
        ("langfuse.observation.input", "inqtrix.search.query"),
        ("langfuse.observation.output", "inqtrix.search.answer"),
    ):
        assert native in attrs
        assert len(attrs[native].encode("utf-8")) <= 64
        assert json.loads(attrs[native]) == attrs[existing]
        assert attrs[existing].endswith("…[truncated]")
        assert text.startswith(attrs[existing].removesuffix("…[truncated]"))
        events = [e for e in span.events if e.name == "inqtrix.truncation" and e.attributes["limit_name"] == native]
        assert len(events) == 1
        event = events[0]
        assert event.attributes["original_size"] > event.attributes["capped_size"]
        assert event.attributes["capped_size"] == len(attrs[native].encode("utf-8"))


def test_search_trace_preserves_source_provenance_and_missing_values(
    exporter_and_provider,
):
    exporter, provider = exporter_and_provider
    supplied = GroundedSearchResult(answer="source statement", sources=[
        GroundedSource(url="https://alpha.example/report", title="Alpha", snippet="passage",
                       date="2026-08-01", last_updated="2026-08-02", rank=1,
                       origin="url_citation", spans=(CitationSpan(0, 8),)),
        GroundedSource(url="https://beta.example/report", rank=2, origin="markdown_link"),
    ])
    search = instrument_search(SuppliedSearch(supplied), policy=FORENSIC, tracer_provider=provider)
    assert search.search("question") is supplied
    (span,) = exporter.get_finished_spans()
    sources = json.loads(span.attributes["inqtrix.search.sources"])
    assert sources == [
        {"url": "https://alpha.example/report", "title": "Alpha", "snippet": "passage",
         "date": "2026-08-01", "last_updated": "2026-08-02", "rank": 1,
         "origin": "url_citation", "spans": [{"start": 0, "end": 8}]},
        {"url": "https://beta.example/report", "title": "", "snippet": "",
         "date": "", "last_updated": "", "rank": 2,
         "origin": "markdown_link", "spans": []},
    ]


def test_embeddings_span(exporter_and_provider):
    exporter, provider = exporter_and_provider
    embeddings = instrument_embeddings(
        FakeEmbeddings(), policy=SUMMARY, tracer_provider=provider
    )
    embeddings.embed_documents(["a", "b", "c"])
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["gen_ai.operation.name"] == "embeddings"
    assert attrs["inqtrix.embeddings.text_count"] == 3
    assert attrs["gen_ai.request.model"] == "fake-embed"


def test_wrapper_is_transparent_for_labels_and_capabilities():
    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    assert provider_label(llm) == "FakeLLM"
    assert describe_llm_provider(llm)["provider"] == "FakeLLM"
    assert llm.supports_tool_calls() is True
    assert llm.supports_structured_output() is True
    assert llm.is_available() is True
    # Idempotent: instrumenting twice never stacks wrappers.
    assert instrument_llm(llm, provider_name="fake", policy=SUMMARY) is llm


@pytest.mark.parametrize("configured", [False, True])
def test_bare_complete_exports_the_same_usage_the_ledger_books(
    exporter_and_provider,
    configured,
):
    """A bare `complete()` span carries usage, like every other call path.

    Without it the span reaches Langfuse with no usage attribute at all and
    Langfuse infers token counts from the message text — producing numbers
    that silently disagree with the ledger and the quota, because a count
    inferred from text does not reproduce the provider's own count that
    the ledger books for the same call.
    """
    exporter, provider = exporter_and_provider
    backend = TextOnlyLLM()
    if configured:
        from inqtrix.providers.base import ConfiguredLLMProvider
        from inqtrix.settings import ModelSettings
        backend = ConfiguredLLMProvider(backend, ModelSettings())
    llm = TracingLLMProvider(backend, provider_name="fake", policy=SUMMARY, tracer_provider=provider)
    state: dict = {}

    assert llm.complete("why?", state=state) == "text-answer"

    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    # The exported numbers are the accumulator delta — the same source the
    # ledger and Prometheus read, so the three can never diverge.
    assert attrs["gen_ai.usage.input_tokens"] == 11
    assert attrs["gen_ai.usage.output_tokens"] == 7
    assert "inqtrix.usage_unavailable" not in attrs


@pytest.mark.parametrize("configured", [False, True])
def test_bare_complete_without_state_marks_usage_unavailable(
    exporter_and_provider,
    configured,
):
    """No accumulator means no usage — say so instead of exporting zero.

    Two call sites pass `state=None`. Exporting `0` there would look like a
    free call; staying silent would let Langfuse invent a number again.
    A visible marker keeps the gap honest, matching `inqtrix.raw_unavailable`.
    """
    exporter, provider = exporter_and_provider
    backend = TextOnlyLLM()
    if configured:
        from inqtrix.providers.base import ConfiguredLLMProvider
        from inqtrix.settings import ModelSettings
        backend = ConfiguredLLMProvider(backend, ModelSettings())
    llm = TracingLLMProvider(backend, provider_name="fake", policy=SUMMARY, tracer_provider=provider)

    assert llm.complete("why?") == "text-answer"

    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.usage_unavailable"] is True
    assert "gen_ai.usage.input_tokens" not in attrs


def test_wrappers_work_without_any_tracer_provider():
    """No global tracing installed: pure duration measurement, no error."""
    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    response = llm.complete_with_metadata("plain")
    assert response.duration_ms > 0
    assert llm.complete("plain") == "text-answer"


@pytest.mark.parametrize("operation", ["complete", "complete_structured", "chat"])
@pytest.mark.parametrize("configured", [False, True])
def test_native_calls_keep_one_full_redacted_receipt(exporter_and_provider, monkeypatch, operation, configured):
    exporter, provider = exporter_and_provider
    calls = []
    raw = {"provider_extension": {"rows": list(range(200)), "api_key": "NEVER_TRACE_THIS"}}
    fake = MetadataLLM()

    def metadata(*_, **__):
        calls.append(True)
        common = dict(prompt_tokens=9, completion_tokens=4, reasoning_tokens=2, raw=raw)
        if operation == "chat":
            return ChatTurn(text="visible", **common)
        if operation == "complete_structured":
            return StructuredLLMResponse(parsed={}, content="visible", **common)
        return LLMResponse(content="visible", **common)

    monkeypatch.setattr(fake, "complete_with_metadata" if operation == "complete" else operation, metadata)
    if configured:
        from inqtrix.providers.base import ConfiguredLLMProvider
        from inqtrix.settings import ModelSettings
        fake = ConfiguredLLMProvider(fake, ModelSettings())
    wrapped = TracingLLMProvider(fake, provider_name="fake", policy=FORENSIC, tracer_provider=provider)
    kwargs = {"schema": {}, "schema_name": "test"} if operation == "complete_structured" else {}
    result = getattr(wrapped, operation)([] if operation == "chat" else "question", **kwargs)
    text = result if isinstance(result, str) else result.text if operation == "chat" else result.content
    assert text == "visible"
    assert calls == [True]
    (span,) = exporter.get_finished_spans()
    captured = json.loads(span.attributes["inqtrix.response.raw"])
    assert captured["provider_extension"]["rows"] == list(range(200))
    assert "NEVER_TRACE_THIS" not in span.attributes["inqtrix.response.raw"]
    assert "inqtrix.raw_unavailable" not in span.attributes


def test_missing_chat_and_structured_raw_response_is_explicit(exporter_and_provider):
    exporter, provider = exporter_and_provider
    wrapped = _wrap_llm(FORENSIC, provider)
    wrapped.chat([])
    wrapped.complete_structured("test", schema={}, schema_name="test")
    assert len(exporter.get_finished_spans()) == 2
    assert all(span.attributes["inqtrix.raw_unavailable"] is True for span in exporter.get_finished_spans())


def test_metadata_backed_text_call_preserves_controls_and_counts_once(exporter_and_provider, monkeypatch):
    exporter, provider = exporter_and_provider
    fake = MetadataLLM()
    state = {"total_prompt_tokens": 100, "total_completion_tokens": 50}
    calls = []

    def metadata(prompt, **kwargs):
        calls.append((prompt, kwargs))
        kwargs["state"]["total_prompt_tokens"] += 9
        kwargs["state"]["total_completion_tokens"] += 4
        return LLMResponse(content="visible", prompt_tokens=9, completion_tokens=4)

    monkeypatch.setattr(fake, "complete_with_metadata", metadata)
    wrapped = TracingLLMProvider(fake, provider_name="fake", policy=SUMMARY, tracer_provider=provider)
    assert wrapped.complete("question", system="rules", model="chosen", max_output_tokens=32,
                            timeout=12, state=state, deadline=12345, reasoning_effort="low") == "visible"
    assert calls == [("question", {"system": "rules", "model": "chosen", "max_output_tokens": 32,
                                  "timeout": 12, "state": state, "deadline": 12345, "reasoning_effort": "low"})]
    assert state == {"total_prompt_tokens": 109, "total_completion_tokens": 54}
    (span,) = exporter.get_finished_spans()
    assert (span.attributes["gen_ai.usage.input_tokens"], span.attributes["gen_ai.usage.output_tokens"]) == (9, 4)


# ------------------------------------------------------------------ #
# Purpose / run / task attribution
# ------------------------------------------------------------------ #


def test_bound_purpose_lands_on_llm_search_and_embedding_spans(
    exporter_and_provider,
):
    """Every provider span says which call site made the call.

    The wrappers are the one wrap point and cannot know their caller, so
    the caller binds its purpose; without it a mission trace is a list
    of anonymous text_completion rows (32 unattributed calls, 514 k
    tokens in the reviewed Tief run).
    """
    from inqtrix.observability.context import bind_purpose

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    search = instrument_search(
        FakeSearch(), policy=SUMMARY, tracer_provider=provider
    )
    embeddings = instrument_embeddings(
        FakeEmbeddings(), policy=SUMMARY, tracer_provider=provider
    )
    with bind_purpose("agent_plan", task_id="t1", task_kind="web_instant"):
        llm.complete_with_metadata("why?")
        search.search("find things")
        embeddings.embed_query("q")
    spans = exporter.get_finished_spans()
    assert len(spans) == 3
    for span in spans:
        attrs = dict(span.attributes)
        assert attrs["inqtrix.purpose"] == "agent_plan", span.name
        assert attrs["inqtrix.task_id"] == "t1", span.name
        assert attrs["inqtrix.task_kind"] == "web_instant", span.name
        # Mirrored into the filterable Langfuse namespace.
        assert attrs["langfuse.observation.metadata.purpose"] == "agent_plan"


def test_purpose_is_absent_when_unbound_and_never_leaks(
    exporter_and_provider,
):
    from inqtrix.observability.context import bind_purpose

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    with bind_purpose("agent_plan"):
        pass
    llm.complete_with_metadata("after the block")
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert "inqtrix.purpose" not in attrs
    assert "langfuse.observation.metadata.purpose" not in attrs


def test_nested_bind_narrows_the_purpose_but_keeps_the_task(
    exporter_and_provider,
):
    """A task span binds the task; the structured-call helper narrows the
    purpose to its node without losing the task attribution."""
    from inqtrix.observability.context import bind_purpose

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    with bind_purpose("web_instant", task_id="t7", task_kind="web_instant"):
        with bind_purpose("agent_citation_repair"):
            llm.complete_with_metadata("repair")
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.purpose"] == "agent_citation_repair"
    assert attrs["inqtrix.task_id"] == "t7"


def test_run_id_from_the_log_context_lands_on_provider_spans(
    exporter_and_provider,
):
    from inqtrix.observability.context import (
        bind_log_context,
        reset_log_context,
    )

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    tokens = bind_log_context(run_id="run_abc")
    try:
        llm.complete_with_metadata("why?")
    finally:
        reset_log_context(tokens)
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes)["inqtrix.run_id"] == "run_abc"


def test_purpose_survives_copy_context_fan_out(exporter_and_provider):
    """The mission fan-out submits pool work through copy_context(); the
    bound purpose must travel with it or every parallel call is
    anonymous again."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from inqtrix.observability.context import bind_purpose

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    with bind_purpose("claim_extract", task_id="t2"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(
                contextvars.copy_context().run,
                llm.complete_with_metadata,
                "in the pool",
            ).result()
    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes)
    assert attrs["inqtrix.purpose"] == "claim_extract"
    assert attrs["inqtrix.task_id"] == "t2"


def test_structured_call_binds_its_node_as_the_purpose(exporter_and_provider):
    """Sixteen agent call sites cross structured_call: binding there
    attributes all of them without a single call-site edit."""
    from pydantic import BaseModel

    from inqtrix.agents.patterns._structured import structured_call

    class _Verdict(BaseModel):
        ok: bool

    exporter, provider = exporter_and_provider
    llm = _wrap_llm(SUMMARY, provider)
    outcome = structured_call(
        llm, prompt="judge", model_cls=_Verdict, node="agent_deep_review"
    )
    assert outcome.value is not None and outcome.value.ok is True
    (span,) = exporter.get_finished_spans()
    assert dict(span.attributes)["inqtrix.purpose"] == "agent_deep_review"


class IncompleteLLM(FakeLLM):
    """Every response stops with the given finish reason."""

    def __init__(self, finish_reason: str) -> None:
        super().__init__()
        self.finish_reason = finish_reason

    def complete_with_metadata(self, prompt, **kwargs) -> LLMResponse:
        return replace(
            super().complete_with_metadata(prompt, **kwargs),
            finish_reason=self.finish_reason,
        )

    def complete_structured(self, prompt, **kwargs) -> StructuredLLMResponse:
        return replace(
            super().complete_structured(prompt, **kwargs),
            finish_reason=self.finish_reason,
        )

    def chat(self, messages, **kwargs) -> ChatTurn:
        return replace(super().chat(messages, **kwargs), finish_reason=self.finish_reason)


def _call(llm: TracingLLMProvider, call: str) -> None:
    if call == "chat":
        llm.chat([{"role": "user", "content": "x"}])
    elif call == "complete_structured":
        llm.complete_structured("x", schema={"type": "object"}, schema_name="S")
    else:
        llm.complete_with_metadata("x")


def _metered_call(exporter_and_provider, monkeypatch, finish_reason: str, call: str):
    """Run one call; return its span attributes, metric outcomes and ledger outcomes."""
    from prometheus_client import CollectorRegistry

    from inqtrix.observability import metrics_defs

    exporter, tracer = exporter_and_provider
    registry = CollectorRegistry()
    monkeypatch.setattr(metrics_defs, "_active", metrics_defs.build_call_metrics(registry))
    receipts = []
    monkeypatch.setattr(
        "inqtrix.usage.recorder.record_provider_call",
        lambda **data: receipts.append(data),
    )
    llm = TracingLLMProvider(
        IncompleteLLM(finish_reason),
        provider_name="fake",
        policy=SUMMARY,
        tracer_provider=tracer,
    )

    _call(llm, call)

    metric_outcomes = [
        sample.labels["outcome"]
        for metric in registry.collect()
        for sample in metric.samples
        if sample.name == "inqtrix_llm_requests_total" and sample.value
    ]
    return (
        exporter.get_finished_spans()[-1].attributes,
        metric_outcomes,
        [receipt["outcome"] for receipt in receipts],
    )


@pytest.mark.parametrize("call", ["complete_with_metadata", "complete_structured", "chat"])
@pytest.mark.parametrize("finish_reason", ["content_filter", "length"])
def test_a_response_the_provider_stopped_early_is_a_warning_not_a_success(
    exporter_and_provider, monkeypatch, call, finish_reason
):
    # The trace marks the generation WARNING and the metric counts it
    # incomplete. The ledger books the billed call as success: its outcome
    # CHECK has no other value for it, and one rejected row would take the
    # tenant's whole flush with it.
    from inqtrix.observability import semconv

    attrs, metric_outcomes, ledger_outcomes = _metered_call(
        exporter_and_provider, monkeypatch, finish_reason, call
    )

    assert attrs[semconv.LANGFUSE_OBSERVATION_LEVEL] == semconv.OBSERVATION_LEVEL_WARNING
    assert finish_reason in attrs[semconv.LANGFUSE_OBSERVATION_STATUS_MESSAGE]
    assert metric_outcomes == ["incomplete"]
    assert ledger_outcomes == ["success"]


@pytest.mark.parametrize("call", ["complete_with_metadata", "complete_structured", "chat"])
def test_a_finished_response_stays_a_plain_success(exporter_and_provider, monkeypatch, call):
    from inqtrix.observability import semconv

    attrs, metric_outcomes, ledger_outcomes = _metered_call(
        exporter_and_provider, monkeypatch, "stop", call
    )

    assert semconv.LANGFUSE_OBSERVATION_LEVEL not in attrs
    assert metric_outcomes == ["success"]
    assert ledger_outcomes == ["success"]
