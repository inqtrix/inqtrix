"""Tests for the Azure AI Foundry Web Search provider (azure-ai-projects)."""

from __future__ import annotations

import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import APIStatusError, APITimeoutError, OpenAIError, RateLimitError

from inqtrix.exceptions import (
    AgentProviderTimeout,
    AgentRateLimited,
    AgentTimeout,
    AzureFoundryWebSearchAPIError,
)
from inqtrix.agents.evidence_context import project_evidence_context
from inqtrix.evidence import build_web_search_ledger
from inqtrix.providers.azure_web_search import AzureFoundryWebSearch
from inqtrix.runtime_logging import normalize_source_provenance
from inqtrix.search_result import CitationSpan


# ---------------------------------------------------------------------------
# Helpers — mock Responses API objects
# ---------------------------------------------------------------------------


def _url_citation(
    url: str,
    title: str = "",
    *,
    start_index: int | None = None,
    end_index: int | None = None,
):
    return SimpleNamespace(
        type="url_citation",
        url=url,
        title=title,
        start_index=start_index,
        end_index=end_index,
    )


def _output_text(text: str, annotations: list | None = None):
    return SimpleNamespace(type="output_text", text=text, annotations=annotations or [])


def _output_message(text: str, annotations: list | None = None):
    return SimpleNamespace(type="message", content=[_output_text(text, annotations)])


def _response(text="", annotations=None, input_tokens=10, output_tokens=20):
    output = [_output_message(text, annotations)] if text else []
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(output=output, usage=usage, output_text=text)


def _provider(client, **kwargs):
    params = {
        "project_endpoint": "https://test.ai.azure.com/api/projects/p",
        "agent_name": "web-search-agent",
        "_client": client,
    }
    params.update(kwargs)
    return AzureFoundryWebSearch(**params)


@pytest.mark.parametrize("capture", [True, False])
def test_foundry_receipt_reaches_trace_without_losing_model_or_tool_usage(capture, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from inqtrix.observability.content import ContentCapturePolicy
    from inqtrix.observability.provider_tracing import instrument_search
    import json

    response = _response("A sourced answer", [_url_citation("https://example.com/source")], 20, 7)
    recorded_usage = []
    monkeypatch.setattr("inqtrix.usage.recorder.record_provider_call", lambda **row: recorded_usage.append(row))
    response.model = "gpt-5.4"
    response.usage.input_tokens_details = SimpleNamespace(cached_tokens=5, cache_write_tokens=2)
    response.usage.output_tokens_details = SimpleNamespace(reasoning_tokens=3)
    response.output.extend([SimpleNamespace(type="web_search_call", status="completed"),
                            SimpleNamespace(type="web_search_call", status="failed")])
    raw = {"model": "gpt-5.4", "output": [{"type": "web_search_call", "action": {"query": "a real query"}}],
           "provider_extra": {"complete_rows": list(range(100)), "api_key": "DO_NOT_RECORD"}}
    response.model_dump = lambda: raw
    client = MagicMock()
    client.responses.create.return_value = response
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    wrapped = instrument_search(_provider(client), policy=ContentCapturePolicy(configured_capture=capture, max_attr_bytes=32768), tracer_provider=tracer)
    result = wrapped.search("question")
    tracer.shutdown()
    assert client.responses.create.call_count == 1
    assert result.raw == raw
    assert result.tool_call_count == 2  # Reported calls, including failure; not invoices or sources.
    assert len(recorded_usage) == 1
    assert recorded_usage[0]["model"] == "gpt-5.4"
    assert (recorded_usage[0]["input_tokens"], recorded_usage[0]["output_tokens"]) == (20, 7)
    (span,) = exporter.get_finished_spans()
    attrs = span.attributes
    assert attrs["gen_ai.response.model"] == "gpt-5.4"
    assert attrs["inqtrix.search.engine"] == "foundry-web:web-search-agent@latest"
    assert attrs["inqtrix.search.tool_call_count"] == 2
    assert json.loads(attrs["langfuse.observation.usage_details"]) == {
        "input": 13, "output": 4, "input_cached_tokens": 5,
        "input_cache_creation": 2, "output_reasoning_tokens": 3, "total": 27,
    }
    if capture:
        captured = json.loads(attrs["inqtrix.response.raw"])
        assert captured["provider_extra"]["complete_rows"] == list(range(100))
        assert captured["output"][0]["action"]["query"] == "a real query"
        assert "DO_NOT_RECORD" not in attrs["inqtrix.response.raw"]
    else:
        assert "inqtrix.response.raw" not in attrs


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_requires_project_endpoint():
    with pytest.raises(ValueError, match="project_endpoint"):
        AzureFoundryWebSearch(project_endpoint="", agent_name="a", _client=MagicMock())


def test_requires_agent_name():
    with pytest.raises(ValueError, match="agent_name"):
        AzureFoundryWebSearch(
            project_endpoint="https://x.ai.azure.com/api", agent_name="", _client=MagicMock()
        )


def test_is_available_when_configured():
    assert _provider(MagicMock()).is_available()


def test_shared_provider_gate_caps_all_callers() -> None:
    """The instance gate, not each caller's thread pool, owns concurrency."""
    lock = threading.Lock()
    release = threading.Event()
    two_started = threading.Event()
    active = 0
    peak = 0

    def _create(**_kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return _response("ok")

    client = MagicMock()
    client.responses.create.side_effect = _create
    provider = _provider(client, max_concurrency=2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(provider.search, f"q{index}") for index in range(4)]
        assert two_started.wait(timeout=1)
        assert peak == 2
        release.set()
        assert [future.result(timeout=2).answer for future in futures] == [
            "ok",
            "ok",
            "ok",
            "ok",
        ]
    assert peak == 2


def test_construction_entra_uses_project_client_without_agent_name():
    """Entra ID auth builds via AIProjectClient.get_openai_client() (no agent_name).

    The agent is referenced per call via ``agent_reference``, not bound at the
    client, so the documented version-pinning path stays available.
    """
    configured_openai_client = MagicMock()
    openai_client = MagicMock()
    openai_client.with_options.return_value = configured_openai_client
    project_client = MagicMock()
    project_client.get_openai_client.return_value = openai_client
    custom_cred = MagicMock()
    with patch(
        "azure.ai.projects.AIProjectClient", return_value=project_client
    ) as mock_proj:
        provider = AzureFoundryWebSearch(
            project_endpoint="https://test.ai.azure.com/api/projects/p/",
            agent_name="web-search-agent",
            credential=custom_cred,
        )
    assert mock_proj.call_args.kwargs["endpoint"] == "https://test.ai.azure.com/api/projects/p"
    assert mock_proj.call_args.kwargs["credential"] is custom_cred
    assert mock_proj.call_args.kwargs["allow_preview"] is True
    project_client.get_openai_client.assert_called_once_with()
    openai_client.with_options.assert_called_once_with(max_retries=0, timeout=600)
    assert provider._client is configured_openai_client


def test_construction_with_project_api_key_uses_data_plane_client():
    """A static project key builds the OpenAI client directly on the data-plane.

    AIProjectClient is the control-plane SDK (Entra only); key auth talks to the
    ``/openai/v1`` endpoint with the ``api-key`` header instead.
    """
    openai_client = MagicMock()
    with patch(
        "inqtrix.providers.azure_web_search.OpenAI", return_value=openai_client
    ) as mock_openai:
        provider = AzureFoundryWebSearch(
            project_endpoint="https://test.ai.azure.com/api/projects/p",
            agent_name="web-search-agent",
            api_key="proj-key-123",
        )
    call = mock_openai.call_args
    assert call.kwargs["base_url"] == "https://test.ai.azure.com/api/projects/p/openai/v1/"
    assert call.kwargs["api_key"] == "proj-key-123"
    assert call.kwargs["default_headers"] == {"api-key": "proj-key-123"}
    assert call.kwargs["max_retries"] == 0
    assert provider._client is openai_client


def test_search_model_includes_agent_and_version():
    assert _provider(MagicMock(), agent_version="2").search_model == (
        "foundry-web:web-search-agent@2"
    )
    assert _provider(MagicMock()).search_model == "foundry-web:web-search-agent@latest"


def test_search_pins_agent_version_via_agent_reference():
    """When agent_version is set, the call pins it through agent_reference."""
    client = MagicMock()
    client.responses.create.return_value = _response(text="ok")
    _provider(client, agent_version="3").search("frage")
    assert client.responses.create.call_args.kwargs["extra_body"] == {
        "agent_reference": {"type": "agent_reference", "name": "web-search-agent", "version": "3"}
    }


def test_search_agent_reference_omits_version_when_unset():
    """Without a version the agent_reference carries name + type only (latest)."""
    client = MagicMock()
    client.responses.create.return_value = _response(text="ok")
    _provider(client).search("frage")
    assert client.responses.create.call_args.kwargs["extra_body"] == {
        "agent_reference": {"type": "agent_reference", "name": "web-search-agent"}
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_search_returns_answer_and_sources():
    client = MagicMock()
    client.responses.create.return_value = _response(
        "Die GKV-Reform bringt folgende Aenderungen...",
        annotations=[
            _url_citation("https://example.com/reform", "Reform"),
            _url_citation("https://example.com/details", "Details"),
        ],
    )
    result = _provider(client).search("GKV Reform")

    assert "GKV-Reform" in result.answer
    assert result.citation_urls == [
        "https://example.com/reform",
        "https://example.com/details",
    ]
    assert result.sources[0].title == "Reform"
    assert result.sources[0].snippet == ""  # Azure exposes no per-source body
    assert result.related_questions == []
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 20


def test_search_preserves_provider_native_citation_answer_span():
    client = MagicMock()
    answer = "Global input costs 5 USD. Data Zone costs more."
    client.responses.create.return_value = _response(
        answer,
        annotations=[
            _url_citation(
                "https://example.com/prices",
                "Prices",
                start_index=0,
                end_index=25,
            ),
        ],
    )

    result = _provider(client).search("model prices")

    span, = result.sources[0].spans
    assert (span.start, span.end) == (0, 25)
    assert answer[span.start:span.end] == "Global input costs 5 USD."


def test_search_merges_citations_and_additional_answer_urls():
    client = MagicMock()
    client.responses.create.return_value = _response(
        "Annotated source https://example.com/annotated and "
        "<https://prices.example/api?$filter="
        "contains(meterName, 'model family')>",
        annotations=[
            _url_citation(
                "https://example.com/annotated",
                "Annotated",
            )
        ],
    )

    result = _provider(client).search("Prices")

    assert result.citation_urls == [
        "https://example.com/annotated",
        (
            "https://prices.example/api?$filter="
            "contains(meterName,%20'model%20family')"
        ),
    ]
    assert result.sources[0].origin == "url_citation"
    assert result.sources[1].origin == "answer_url_fallback"


def test_search_rejects_unsafe_sources_and_recovers_encoded_target():
    credential_url = "https://example.com/private?client_secret=hidden"
    compound_url = (
        "https://wrapper.example/redirect?"
        "next=https%3A%2F%2Ftarget.example%2Fapi"
    )
    client = MagicMock()
    client.responses.create.return_value = _response(
        f"Credential {credential_url}; redirect {compound_url}",
        annotations=[
            _url_citation(credential_url, "Unsafe"),
            _url_citation(compound_url, "Compound"),
        ],
    )

    result = _provider(client).search("Sources")

    assert result.citation_urls == ["https://target.example/api"]
    assert "hidden" not in repr(result.sources)
    assert "wrapper.example" not in repr(result.sources)


def test_search_preserves_every_provider_citation():
    urls = [f"https://source-{index}.example/data" for index in range(60)]
    client = MagicMock()
    client.responses.create.return_value = _response(
        " ".join(urls),
        annotations=[_url_citation(url) for url in urls],
    )

    result = _provider(client).search("Many sources")

    assert len(result.sources) == 60
    assert result.sources[0].rank == 1
    assert result.sources[-1].rank == 60


def test_search_calls_responses_create_with_plain_input():
    client = MagicMock()
    client.responses.create.return_value = _response("ok", [_url_citation("https://a.com")])
    _provider(client).search("Test query")

    client.responses.create.assert_called_once()
    call_kwargs = client.responses.create.call_args.kwargs
    assert call_kwargs["input"] == [{"role": "user", "content": "Test query"}]


def test_search_empty_response_sets_notice():
    client = MagicMock()
    client.responses.create.return_value = _response("")
    provider = _provider(client)
    result = provider.search("Empty")
    assert result.answer == ""
    assert result.sources == []
    assert provider.consume_nonfatal_notice() is not None


def test_failure_does_not_copy_query_or_provider_error_to_logs(caplog):
    query_secret = "private-azure-query-sentinel-92741"
    provider_secret = "azure-provider-error-sentinel-63820"
    client = MagicMock()
    client.responses.create.side_effect = OpenAIError(provider_secret)
    provider = _provider(client)
    logger = logging.getLogger("inqtrix")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.ERROR, logger="inqtrix"):
            provider.search(query_secret)
    finally:
        logger.removeHandler(caplog.handler)

    notice = provider.consume_nonfatal_notice()
    assert query_secret not in caplog.text
    assert provider_secret not in caplog.text
    assert notice is not None
    assert query_secret not in notice
    assert provider_secret not in notice


def test_search_markdown_link_fallback_when_no_annotations():
    client = MagicMock()
    client.responses.create.return_value = _response(
        "Die Zahlen stehen hier ([investor.nvidia.com](https://investor.nvidia.com/q1)).",
        annotations=[],
    )
    result = _provider(client).search("NVIDIA")
    assert result.citation_urls == ["https://investor.nvidia.com/q1"]
    assert result.sources[0].title == "investor.nvidia.com"
    assert result.sources[0].origin == "markdown_link"


def test_search_bare_url_fallback():
    client = MagicMock()
    client.responses.create.return_value = _response(
        "Laut https://www.bmi.bund.de/reform ist die Reform in Kraft.", annotations=[]
    )
    result = _provider(client).search("Reform")
    assert "https://www.bmi.bund.de/reform" in result.citation_urls


def test_search_deduplicates_citations():
    client = MagicMock()
    client.responses.create.return_value = _response(
        "Doppelte Quelle...",
        annotations=[
            _url_citation("https://example.com/same"),
            _url_citation("https://example.com/same"),
        ],
    )
    result = _provider(client).search("Test")
    assert result.citation_urls == ["https://example.com/same"]


KFW = "https://www.kfw.de/foerderung"
# One paragraph per statement, as the provider writes its answer: the review
# reads the line around each citation marker.
KFW_ANSWER = (
    f"Die Grundförderung beträgt 30 %. ([kfw.de]({KFW}))\n\n"
    f"Der Höchstbetrag liegt bei 28.000 €. ([kfw.de]({KFW}))"
)


def _kfw_citations() -> list[SimpleNamespace]:
    """Both markers of the answer, annotated the way Azure annotates them:
    the rendered link only, not the sentence before it."""
    first = KFW_ANSWER.index("([kfw.de]")
    second = KFW_ANSWER.index("([kfw.de]", first + 1)
    return [
        _url_citation(KFW, "KfW", start_index=start, end_index=KFW_ANSWER.index("))", start) + 2)
        for start in (first, second)
    ]


def test_search_keeps_every_place_the_answer_cites_a_source():
    """The source stays one, but each of its citations is a place of the
    answer that cites it; none of them is dropped."""
    client = MagicMock()
    client.responses.create.return_value = _response(KFW_ANSWER, annotations=_kfw_citations())
    result = _provider(client).search("Förderung")
    sources, citations = normalize_source_provenance(result, query_id="query_kfw", provider="azure")
    assert (result.citation_urls, len(sources), [citation["annotation_start"] for citation in citations]) == (
        [KFW], 1, [KFW_ANSWER.index("([kfw.de]"), KFW_ANSWER.rindex("([kfw.de]")],
    )


def test_search_keeps_a_repeated_citation_place_once_and_none_without_offsets():
    client = MagicMock()
    client.responses.create.return_value = _response("Quelle A gilt. Quelle B gilt.", annotations=[
        _url_citation("https://example.com/a", start_index=0, end_index=14),
        _url_citation("https://example.com/a", start_index=0, end_index=14),
        _url_citation("https://example.com/b"),
    ])
    result = _provider(client).search("Quellen")
    assert [(source.url, source.spans) for source in result.sources] == [
        ("https://example.com/a", (CitationSpan(0, 14),)),
        ("https://example.com/b", ()),
    ]


def test_review_reads_every_sentence_the_provider_cites_a_source_for():
    """End to end without network: provider answer, ledger and the review's
    evidence projection hold the sentence before each citation of the source."""
    client = MagicMock()
    client.responses.create.return_value = _response(KFW_ANSWER, annotations=_kfw_citations())
    result = _provider(client).search("Förderung")
    _sources, citations = normalize_source_provenance(result, query_id="query_kfw", provider="azure")
    ledger = build_web_search_ledger(
        run_id="run_kfw",
        query_records=[{"query_id": "query_kfw", "query": "Förderung"}],
        query_synthesis={"query_kfw": {"provider_answer": result.answer}},
        citation_records=citations,
    )
    context = project_evidence_context(
        [{"label": "W1", "url": KFW, "source_run_id": "run_kfw", "query_id": "query_kfw"}], ledger,
    )
    supports = [item["grounded_support"] for item in context["sources"][0]["citation_contexts"]]
    assert [any("30 %" in text for text in supports), any("28.000" in text for text in supports)] == [True, True]


def test_search_token_counts():
    client = MagicMock()
    client.responses.create.return_value = _response("Test", input_tokens=42, output_tokens=99)
    result = _provider(client).search("Test")
    assert result.prompt_tokens == 42
    assert result.completion_tokens == 99


# ---------------------------------------------------------------------------
# Parameter handling
# ---------------------------------------------------------------------------


def test_domain_filter_appended_to_query():
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    _provider(client).search("KI Regulierung", domain_filter=["bmi.bund.de"])
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert "site:bmi.bund.de" in content


def test_domain_filter_exclusion():
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    _provider(client).search("KI", domain_filter=["-pinterest.com", "-reddit.com"])
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert "-site:pinterest.com" in content
    assert "-site:reddit.com" in content


def test_recency_hint_in_user_input():
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    _provider(client).search("Test", recency_filter="day")
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content == "Focus on results from the past 24 hours.\n\nTest"


@pytest.mark.parametrize("language", ["de", "en"])
def test_search_hints_preserve_language_and_domain_selection(language):
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    _provider(client).search(
        "A test question", recency_filter="week", language_filter=[language],
        domain_filter=["example.com", "-example.org"],
    )
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content == (
        "Focus on results from the past week. "
        f"Answer in {language} and prefer sources in that language.\n\n"
        "A test question site:example.com -site:example.org"
    )


def test_no_hint_when_no_filters():
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    _provider(client).search("Test")
    content = client.responses.create.call_args.kwargs["input"][0]["content"]
    assert content == "Test"


def test_unsupported_params_ignored_gracefully():
    client = MagicMock()
    client.responses.create.return_value = _response("x", [_url_citation("https://a.com")])
    result = _provider(client).search(
        "Test", search_context_size="low", search_mode="academic", return_related=True
    )
    assert result.related_questions == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _http_error(cls, status):
    request = httpx.Request("POST", "https://test.ai.azure.com/responses")
    response = httpx.Response(status_code=status, request=request, json={"error": {"message": "x"}})
    return cls("err", response=response, body={"error": {"message": "x"}})


def test_generic_exception_returns_empty_with_notice():
    client = MagicMock()
    client.responses.create.side_effect = RuntimeError("connection refused")
    provider = _provider(client)
    result = provider.search("Test")
    assert result.answer == ""
    notice = provider.consume_nonfatal_notice()
    assert notice == (
        "Azure-Foundry-WebSearch failed; the empty result remains "
        "a visible evidence gap."
    )


def test_rate_limit_escalated():
    client = MagicMock()
    client.responses.create.side_effect = _http_error(RateLimitError, 429)
    with pytest.raises(AgentRateLimited):
        _provider(client).search("Test")


def test_status_429_escalated_to_rate_limited():
    client = MagicMock()
    client.responses.create.side_effect = _http_error(APIStatusError, 429)
    with pytest.raises(AgentRateLimited):
        _provider(client).search("Test")


def test_non_429_status_error_degrades_to_empty(monkeypatch):
    monkeypatch.setattr("inqtrix.providers.base._retry_delay_seconds", lambda attempt: 0.0)
    client = MagicMock()
    client.responses.create.side_effect = _http_error(APIStatusError, 503)
    provider = _provider(client)
    result = provider.search("Test")
    assert result.answer == ""
    assert provider.consume_nonfatal_notice() is not None


def test_status_408_keeps_provider_timeout_notice(monkeypatch):
    monkeypatch.setattr("inqtrix.providers.base._retry_delay_seconds", lambda attempt: 0.0)
    client = MagicMock()
    client.responses.create.side_effect = _http_error(APIStatusError, 408)
    provider = _provider(client)

    result = provider.search("Test")
    notice = provider.consume_nonfatal_notice_detail()

    assert result.sources == []
    assert notice is not None
    assert notice["code"] == "provider_timeout"
    assert notice["http_status"] == 408


def test_transient_timeout_retry_emits_notice(monkeypatch):
    monkeypatch.setattr("inqtrix.providers.base._retry_delay_seconds", lambda attempt: 0.0)
    request = httpx.Request("POST", "https://test.ai.azure.com/responses")
    client = MagicMock()
    client.responses.create.side_effect = [
        APITimeoutError(request=request),
        _response("ok"),
    ]
    provider = _provider(client)
    notices = []

    with provider.observe_retries(lambda notice: notices.append(notice)):
        result = provider.search("Test")

    assert result.answer == "ok"
    assert client.responses.create.call_count == 2
    assert len(notices) == 1
    assert notices[0]["provider"] == "AzureFoundryWebSearch"
    assert notices[0]["model"] == "foundry-web:web-search-agent@latest"
    assert notices[0]["operation"] == "web_search"
    assert notices[0]["attempt"] == 1
    assert notices[0]["max_attempts"] == 3
    assert notices[0]["configured_timeout_seconds"] == 600
    assert notices[0]["error_code"] == "APITimeoutError"


def test_timeout_exception_re_raised():
    client = MagicMock()
    client.responses.create.side_effect = OpenAIError("Request timed out")
    with pytest.raises(AgentProviderTimeout):
        _provider(client).search("Test")


def test_deadline_exceeded_raises_agent_timeout():
    with pytest.raises(AgentTimeout):
        _provider(MagicMock()).search("Test", deadline=time.monotonic() - 10)


# ---------------------------------------------------------------------------
# _parse_response edge cases
# ---------------------------------------------------------------------------


def test_parse_response_no_output():
    resp = SimpleNamespace(output_text="", output=[], usage=None)
    result = AzureFoundryWebSearch._parse_response(resp)
    assert result.answer == ""
    assert result.sources == []
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0


def test_parse_response_non_message_items_skipped():
    resp = SimpleNamespace(
        output_text="answer",
        output=[
            SimpleNamespace(type="tool_call", content=[]),
            _output_message("answer", [_url_citation("https://a.com")]),
        ],
        usage=SimpleNamespace(input_tokens=1, output_tokens=2),
    )
    result = AzureFoundryWebSearch._parse_response(resp)
    assert result.citation_urls == ["https://a.com"]


# ---------------------------------------------------------------------------
# Exception class
# ---------------------------------------------------------------------------


def test_api_error_formatting():
    err = AzureFoundryWebSearchAPIError(
        agent_name="my-agent",
        status_code=429,
        error_code="RateLimitExceeded",
        message="Too many requests",
    )
    s = str(err)
    assert "my-agent" in s
    assert "429" in s
    assert "RateLimitExceeded" in s
    assert "Too many requests" in s


def test_api_error_minimal():
    err = AzureFoundryWebSearchAPIError(agent_name="a", message="boom")
    assert "boom" in str(err)
