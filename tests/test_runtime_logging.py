"""Tests for structured runtime logging helpers and integration points."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from queue import Queue
from types import SimpleNamespace
from typing import Any

import pytest

from inqtrix.graph import run
from inqtrix.logging_config import configure_logging
from inqtrix.nodes import search
from inqtrix.providers.base import ProviderContext
from inqtrix.runtime_logging import (
    describe_search_provider,
    emit_runtime_event,
    log_run_end,
    normalize_source_provenance,
    sanitize_grounded_search_result,
    sanitize_event_payload,
)
from inqtrix.search_result import CitationSpan, GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.state import append_iteration_log, emit_run_event, initial_state
from inqtrix.strategies import StrategyContext, create_default_strategies


@pytest.fixture(autouse=True)
def reset_inqtrix_logger():
    logger = logging.getLogger("inqtrix")
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate

    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    yield

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(previous_level)
    logger.propagate = previous_propagate
    for handler in previous_handlers:
        logger.addHandler(handler)


class AzureOpenAILLM:
    def __init__(self) -> None:
        self.models = SimpleNamespace(
            reasoning_model="gpt-4o",
            effective_classify_model="gpt-4o-mini",
            effective_claim_extract_model="gpt-4o-mini",
            effective_evaluate_model="gpt-4o",
        )
        self._default_max_tokens = 4096

    def complete(self, *args, **kwargs):
        return ""


    def is_available(self):
        return True


class AzureFoundryWebSearch:
    def __init__(self) -> None:
        self._agent_name = "web-search-agent"
        self._agent_version = "2026-04-01"

    def search(self, *args, **kwargs):
        return GroundedSearchResult()

    def is_available(self):
        return True


def _flush_inqtrix_handlers() -> None:
    logger = logging.getLogger("inqtrix")
    for handler in logger.handlers:
        if hasattr(handler, "flush"):
            handler.flush()


def test_run_event_sink_failure_logs_type_without_exception_message(tmp_path):
    log_path = configure_logging(
        enabled=True,
        level="DEBUG",
        log_dir=str(tmp_path / "logs"),
    )

    def _broken_sink(_event_type, _payload):
        raise RuntimeError("PRIVATE_SINK_EXCEPTION_MESSAGE")

    emit_run_event(
        {"_run_event_sink": _broken_sink},
        "inqtrix.run.test",
        {"status": "completed"},
    )

    _flush_inqtrix_handlers()
    content = Path(log_path).read_text(encoding="utf-8")

    assert "Native run event sink failed" in content
    assert "error_code=RuntimeError" in content
    assert "PRIVATE_SINK_EXCEPTION_MESSAGE" not in content


def test_run_end_banner_rejects_free_form_reason_text(tmp_path):
    log_path = configure_logging(
        enabled=True,
        level="DEBUG",
        log_dir=str(tmp_path / "logs"),
    )

    log_run_end(
        run_id="run_safe_123",
        run_mode="run",
        status="failed",
        elapsed_s=1.25,
        state={"round": 2, "done": False},
        reason="PRIVATE FREE FORM FAILURE DETAIL",
    )

    _flush_inqtrix_handlers()
    content = Path(log_path).read_text(encoding="utf-8")

    assert "id=run_safe_123" in content
    assert "status=failed" in content
    assert "PRIVATE FREE FORM FAILURE DETAIL" not in content


def test_run_logs_start_metadata_banner(tmp_path, monkeypatch):
    log_path = configure_logging(enabled=True, level="DEBUG", log_dir=str(tmp_path / "logs"))

    class _StubAgent:
        def invoke(self, state):
            return {"answer": "ok", "total_prompt_tokens": 0, "total_completion_tokens": 0}

    monkeypatch.setattr("inqtrix.graph.get_agent", lambda *args, **kwargs: _StubAgent())

    settings = AgentSettings()
    providers = ProviderContext(llm=AzureOpenAILLM(), search=AzureFoundryWebSearch())
    strategies = create_default_strategies(settings)

    run(
        "Was ist der aktuelle Stand?",
        providers=providers,
        strategies=strategies,
        settings=settings,
    )

    _flush_inqtrix_handlers()
    content = Path(log_path).read_text(encoding="utf-8")

    assert "RUN start:" in content
    assert "profile=compact" in content
    assert "llm=AzureOpenAILLM" in content
    assert "reasoning=gpt-4o" in content
    assert "search=AzureFoundryWebSearch" in content
    assert "engine=web-search-agent@2026-04-01" in content
    assert "default_max_tokens=4096" in content


def test_describe_search_provider_handles_common_engine_labels():
    class PerplexitySearch:
        def __init__(self) -> None:
            self._model = "perplexity-sonar-pro-agent"

    class AgentSearchProvider:
        def __init__(self) -> None:
            self._agent_id = "agent-123"

    assert describe_search_provider(PerplexitySearch())[
        "engine"] == "perplexity-sonar-pro-agent"
    assert describe_search_provider(AgentSearchProvider())["engine"] == "agent-123"


def test_describe_search_provider_prefers_standard_search_model_property():
    class CustomSearchProvider:
        _agent_name = "legacy-agent"

        @property
        def search_model(self):
            return "custom-search-engine"

    metadata = describe_search_provider(CustomSearchProvider())

    assert metadata["engine"] == "custom-search-engine"


def test_runtime_event_drops_content_and_credentials(recording):
    """Credentials and content never reach a runtime event.

    The projection is fail-closed: only operational identifiers, codes,
    statuses, counters and timings are admitted. A caller that hands in
    an api key, a bearer header, a raw provider payload or a URL must
    not be able to leak it — the event carries the fields it declared
    safe and nothing else.
    """
    exporter = recording
    from inqtrix.observability.otel import operation_span

    with operation_span("probe"):
        emit_runtime_event(
            "forensic_probe",
            {
                "event": "forensic_probe",
                "request_kwargs": {"api_key": "sk-secretsecretsecretsecret"},
                "headers": {"authorization": "Bearer abc.def.ghi"},
                "raw_response": {"token": "pplx-secretsecretsecretsecret"},
                "safe_url": (
                    "https://api.example.com/v1"
                    "?api_key=sk-realLookingApiKey1234567890&page=2"
                ),
                "nested": [
                    {"url": "https://example.com/report?sig=abc123&page=1"},
                ],
                "status": "completed",
                "source_count": 2,
            },
        )

    (span,) = exporter.get_finished_spans()
    (event,) = [e for e in span.events if e.name == "forensic_probe"]
    rendered = json.dumps(dict(event.attributes))

    assert "request_kwargs" not in rendered
    assert "headers" not in rendered
    assert "raw_response" not in rendered
    assert "sk-secretsecretsecretsecret" not in rendered
    assert "Bearer abc.def.ghi" not in rendered
    assert "page=2" not in rendered
    assert "https://" not in rendered
    assert event.attributes["status"] == "completed"
    assert event.attributes["source_count"] == 2


def test_normalize_source_provenance_builds_records_from_grounded_result():
    result = GroundedSearchResult(
        answer="ok",
        sources=[
            GroundedSource(
                url="https://example.com/report?api_key=sk-realLookingApiKey1234567890",
                rank=3,
                origin="api_citations",
                title="Source title",
                date="2026-05-01",
                last_updated="2026-05-02",
            )
        ],
    )

    sources, citations = normalize_source_provenance(
        result,
        query_id="qry_1",
        provider="CustomSearch",
        tier_explanations={
            "https://example.com/report?api_key=sk-realLookingApiKey1234567890": {
                "tier": "primary",
                "tier_reason": "matched_primary_domain",
            }
        },
    )

    assert sources[0]["provider"] == "CustomSearch"
    assert sources[0]["tier"] == "primary"
    assert citations[0]["origin"] == "api_citations"
    assert citations[0]["rank"] == 3
    assert citations[0]["title"] == "Source title"
    assert citations[0]["source_date"] == "2026-05-01"
    assert citations[0]["last_updated"] == "2026-05-02"


def test_source_provenance_records_one_citation_per_place_the_answer_cites_it():
    result = GroundedSearchResult(answer="a" * 40, sources=[GroundedSource(
        url="https://example.com/a", rank=1, origin="url_citation",
        spans=(CitationSpan(0, 10), CitationSpan(20, 30)),
    )])
    sources, citations = normalize_source_provenance(result, query_id="query_a", provider="azure")
    _, again = normalize_source_provenance(result, query_id="query_a", provider="azure")
    assert (len(sources), [(item["annotation_start"], item["annotation_end"]) for item in citations]) == (
        1, [(0, 10), (20, 30)],
    )
    assert len({item["citation_id"] for item in citations}) == 2
    assert [item["citation_id"] for item in again] == [item["citation_id"] for item in citations]


def test_a_scrubbed_answer_drops_the_places_its_sources_were_cited():
    """Offsets point into the answer as the provider wrote it; once a
    credential is scrubbed from it, they no longer point anywhere true."""
    result = sanitize_grounded_search_result(GroundedSearchResult(
        answer="Use https://api.example/report?X-Amz-Signature=secret-value for it.",
        sources=[GroundedSource(url="https://example.com/a", spans=(CitationSpan(0, 3),))],
    ))
    assert ("secret-value" in result.answer, result.sources[0].spans) == (False, ())


def test_search_provenance_redacts_credential_urls_before_state_persistence():
    secret = "search-provider-secret"
    result = GroundedSearchResult(
        answer=(
            "Use https://api.example/report?X-Amz-Signature=" + secret
        ),
        sources=[
            GroundedSource(
                url=f"https://api.example/report?x-api-key={secret}&page=2",
                title=(
                    "Download https://api.example/report?client_secret=" + secret
                ),
                snippet=(
                    "Raw https://api.example/report?access_token=" + secret
                ),
            )
        ],
    )

    sources, citations = normalize_source_provenance(
        result,
        query_id="qry_safe_url",
        provider="CustomSearch",
    )
    serialized = __import__("json").dumps(
        {"sources": sources, "citations": citations}
    )

    assert secret not in serialized
    assert sources[0]["access_status"] == "blocked_credentials"
    assert "x-api-key=[REDACTED]" in sources[0]["url"]
    assert "client_secret=[REDACTED]" in citations[0]["title"]
    assert "access_token=[REDACTED]" in citations[0]["snippet"]
    assert "source" not in citations[0]
    assert "headers" not in citations[0]


def test_product_evidence_preserves_late_facts_offsets_and_usage_metadata() -> None:
    original = "ä" * 70_000 + " Late fact: 93."
    result = sanitize_grounded_search_result(
        GroundedSearchResult(
            answer=original, model="search-model", prompt_tokens=17, completion_tokens=9,
            cache_read_tokens=4, cache_creation_tokens=3, reasoning_tokens=2, tool_call_count=1,
            raw={"private_envelope": "not product evidence"},
            sources=[GroundedSource(url="https://example.com", snippet=original,
                                    spans=(CitationSpan(70_000, len(original)),))],
        )
    )
    assert result.answer == original
    assert result.sources[0].snippet == original
    assert result.sources[0].spans == (CitationSpan(70_000, len(original)),)
    assert (result.model, result.prompt_tokens, result.completion_tokens,
            result.cache_read_tokens, result.cache_creation_tokens,
            result.reasoning_tokens, result.tool_call_count) == ("search-model", 17, 9, 4, 3, 2, 1)
    assert result.raw is None



def test_append_iteration_log_keeps_audit_content_out_of_events(
    recording,
):
    """The protected audit copy keeps the exact text; the event does not.

    Queries, provider prose, claim/evidence text, prompt views and URLs
    stay inside the run's own audit representation. What reaches the
    trace event is the operational projection — counters, codes, model
    ids, stop reasons.
    """
    from inqtrix.observability.otel import operation_span

    exporter = recording
    state = {"iteration_logs": []}

    with operation_span("probe"):
        append_iteration_log(
        state,
        {
            "node": "answer",
            "query": "PRIVATE_QUERY_EXACT_TEXT",
            "provider_answer": "PRIVATE_PROVIDER_EXACT_TEXT",
            "claim_text": "PRIVATE_CLAIM_EXACT_TEXT",
            "evidence_snippet": "PRIVATE_EVIDENCE_EXACT_TEXT",
            "report_evidence_prompt": "PRIVATE_PROMPT_EXACT_TEXT",
            "prompt_citations": ["https://private.example.test/report"],
            "fallback_attempted": True,
            "_claim_extraction_fallback": True,
            "_stop_reason": "confidence_stop",
            "model": "openai/gpt-5.6-sol",
            "evidence_record_count": 3,
        },
        testing_mode=True,
    )

    (span,) = exporter.get_finished_spans()
    (event,) = span.events
    content = json.dumps(dict(event.attributes))

    audit_entry = state["iteration_logs"][0]
    assert audit_entry["query"] == "PRIVATE_QUERY_EXACT_TEXT"
    assert audit_entry["provider_answer"] == "PRIVATE_PROVIDER_EXACT_TEXT"
    assert audit_entry["claim_text"] == "PRIVATE_CLAIM_EXACT_TEXT"
    assert audit_entry["evidence_snippet"] == "PRIVATE_EVIDENCE_EXACT_TEXT"
    assert audit_entry["report_evidence_prompt"] == "PRIVATE_PROMPT_EXACT_TEXT"
    assert audit_entry["prompt_citations"] == [
        "https://private.example.test/report"
    ]
    assert event.attributes["fallback_attempted"] is True
    assert event.attributes["_claim_extraction_fallback"] is True
    assert event.attributes["_stop_reason"] == "confidence_stop"
    assert event.attributes["model"] == "openai/gpt-5.6-sol"
    assert event.attributes["evidence_record_count"] == 3
    for private_value in (
        "PRIVATE_QUERY_EXACT_TEXT",
        "PRIVATE_PROVIDER_EXACT_TEXT",
        "PRIVATE_CLAIM_EXACT_TEXT",
        "PRIVATE_EVIDENCE_EXACT_TEXT",
        "PRIVATE_PROMPT_EXACT_TEXT",
        "https://private.example.test/report",
    ):
        assert private_value not in content


def test_event_name_projection_fails_closed(recording):
    """An unrecognised event or node name must not pass through.

    Names are attacker-influenceable free text; the projection admits
    only known operational tokens and falls back to a neutral one. The
    protected audit copy keeps the original.
    """
    from inqtrix.observability.otel import operation_span

    exporter = recording
    state = {"iteration_logs": []}

    with operation_span("probe"):
        append_iteration_log(
            state,
            {
                "event": "PRIVATE EVENT TEXT",
                "node": "PRIVATE NODE TEXT",
                "status": "completed",
            },
            testing_mode=True,
        )

    assert state["iteration_logs"][0]["event"] == "PRIVATE EVENT TEXT"
    assert state["iteration_logs"][0]["node"] == "PRIVATE NODE TEXT"
    (span,) = exporter.get_finished_spans()
    (event,) = span.events
    assert event.name == "iteration_summary"
    rendered = json.dumps(dict(event.attributes))
    assert "PRIVATE EVENT TEXT" not in rendered
    assert "PRIVATE NODE TEXT" not in rendered
    assert event.attributes["status"] == "completed"


def test_search_emits_per_query_runtime_artifacts(recording):
    """Per-query lineage reaches the trace with content held back.

    Fallback counters, extraction mode/schema and claim counts are
    operational and must be visible; provider prose, claim text and
    URLs stay in the run's protected representation.
    """
    exporter = recording

    class _SearchWithNotice:
        def __init__(self) -> None:
            self._notice = None

        def search(self, *args, **kwargs):
            self._notice = "search fallback"
            return GroundedSearchResult(
                answer="Gefundener Text",
                sources=[GroundedSource(url="https://example.com/report", rank=1)],
                related_questions=["Was ist neu?"],
                prompt_tokens=11,
                completion_tokens=7,
            )

        def consume_nonfatal_notice(self):
            notice = self._notice
            self._notice = None
            return notice

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self._notice = None
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *args, **kwargs):
            return ""

        def consume_nonfatal_notice(self):
            notice = self._notice
            self._notice = None
            return notice

        def is_available(self):
            return True

    class _ClaimExtractionWithNotice:
        def __init__(self) -> None:
            self._notice = None
            self._metadata: dict[str, object] | None = None

        def extract(
            self,
            *args: object,
            **kwargs: object,
        ) -> tuple[list[dict[str, object]], int, int]:
            del args, kwargs
            self._notice = "claim fallback"
            self._metadata = {
                "claim_extraction_mode": "structured_output",
                "claim_extraction_schema": "inqtrix_claim_extraction_v2",
                "claim_extraction_structured_supported": True,
                "claim_extraction_raw_claim_count": 1,
                "claim_extraction_normalized_claim_count": 1,
                "claim_extraction_filtered_claim_count": 0,
                "unknown_provider_ref_count": 0,
                "unbound_claim_count": 0,
            }
            return (
                [
                    {
                        "claim_text": "Passiert ist ein wichtiger Fakt aus der Quelle",
                        "evidence_snippet": "Die Quelle nennt diesen wichtigen Fakt direkt.",
                        "source_urls": ["https://example.com/report"],
                    }
                ],
                0,
                0,
            )

        def consume_nonfatal_notice(self) -> str | None:
            notice = self._notice
            self._notice = None
            return notice

        def consume_extraction_metadata(self) -> dict[str, object] | None:
            metadata = self._metadata
            self._metadata = None
            return metadata

    settings = AgentSettings(first_round_queries=1, max_rounds=4, observability_profile="forensic")
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_ClaimExtractionWithNotice(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Was ist passiert?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["q1"]

    from inqtrix.observability.otel import operation_span

    with operation_span("probe"):
        search(
            state,
            providers=ProviderContext(llm=llm, search=_SearchWithNotice()),
            strategies=strategies,
            settings=settings,
        )

    events = [e for span in exporter.get_finished_spans() for e in span.events]
    content = json.dumps(
        [{"name": e.name, **dict(e.attributes)} for e in events]
    )

    assert any(e.name == "query_summary" for e in events)
    assert '"search_fallbacks": 1' in content
    assert '"claim_fallbacks": 1' in content
    assert '"claim_extraction_mode": "structured_output"' in content
    assert '"claim_extraction_schema": "inqtrix_claim_extraction_v2"' in content
    assert '"claim_extraction_structured_supported": true' in content
    assert '"claim_extraction_raw_claim_count": 1' in content
    assert '"claim_extraction_normalized_claim_count": 1' in content
    assert '"claim_extraction_filtered_claim_count": 0' in content
    assert '"provider_notice": "search fallback"' not in content
    assert '"claim_notice": "claim fallback"' not in content
    assert "Gefundener Text" not in content
    assert "Passiert ist ein wichtiger Fakt aus der Quelle" not in content
    assert "Die Quelle nennt diesen wichtigen Fakt direkt." not in content
    assert "https://example.com/report" not in content

    assert state["query_synthesis"]
    assert next(iter(state["query_synthesis"].values()))["provider_answer"] == "Gefundener Text"
    assert state["evidence_ledger"][0]["claims"][0]["claim_text"] == (
        "Passiert ist ein wichtiger Fakt aus der Quelle"
    )


def test_search_forensic_lineage_works_with_legacy_custom_provider():
    class _LegacySearch:
        def search(self, *args, **kwargs):
            return GroundedSearchResult(
                answer="Der Energiepreis steigt laut Quelle.",
                sources=[GroundedSource(url="https://example.com/energy-report", rank=1)],
                prompt_tokens=1,
                completion_tokens=2,
            )

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    class _ClaimExtraction:
        def extract(self, *args, **kwargs):
            return (
                [
                    {
                        "claim_text": "Der Energiepreis steigt laut Quelle.",
                        "evidence_snippet": "Der Bericht sagt: Der Energiepreis steigt.",
                        "claim_type": "fact",
                        "polarity": "affirmed",
                        "source_urls": ["https://example.com/energy-report"],
                    }
                ],
                0,
                0,
            )

    settings = AgentSettings(
        first_round_queries=1,
        max_rounds=4,
        testing_mode=True,
        observability_profile="forensic",
    )
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_ClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Warum steigt der Energiepreis?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["Energiepreis steigt Bericht"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_LegacySearch()),
        strategies=strategies,
        settings=settings,
    )

    events = [entry.get("event") for entry in state["iteration_logs"]]
    assert "query_record" in events
    assert "source_record" in events
    assert "provider_citation_record" in events
    assert "query_summary" in events
    assert "claim_record" in events
    assert "evidence_record" in events
    assert "evidence_verification_projection" in events
    assert "evidence_selection" in events
    assert "claim_merge" in events

    summary = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "iteration_summary" and entry.get("node") == "search"
    ][-1]
    assert summary["query_record_ids"]
    assert summary["source_record_ids"]
    assert summary["provider_citation_record_ids"]
    assert summary["claim_record_ids"]
    assert summary["evidence_record_count"] >= 1
    assert "verified_claim_count" in summary

    from inqtrix.evidence import derive_claim_ledger_from_evidence

    raw_claim = derive_claim_ledger_from_evidence(state["evidence_ledger"])[0]
    assert raw_claim["query_id"]
    assert raw_claim["evidence_snippet"] == "Der Bericht sagt: Der Energiepreis steigt."
    assert raw_claim["source_ids"]
    assert raw_claim["citation_ids"]
    assert raw_claim["evidence_ids"]
    assert state["evidence_ledger"][0]["claims"]
    assert state["consolidated_claims"][0]["member_claim_ids"] == [raw_claim["raw_claim_id"]]
    assert state["consolidated_claims"][0]["supporting_evidence_ids"]
    assert state["consolidated_claims"][0]["evidence_snippets"] == [
        "Der Bericht sagt: Der Energiepreis steigt."
    ]


def test_search_empty_claims_keep_source_backed_context_from_evidence_ledger():
    class _PerplexityShapedSearch:
        def search(self, *args, **kwargs):
            return GroundedSearchResult(
                answer="Perplexity answer describes the source but structured claims are empty.",
                sources=[
                    GroundedSource(
                        url="https://example.com/perplexity-report",
                        title="Perplexity-shaped source report",
                        snippet="The source snippet carries rich context for the final answer.",
                        date="2026-05-10",
                        origin="search_results",
                        rank=1,
                    )
                ],
                prompt_tokens=1,
                completion_tokens=2,
            )

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    class _EmptyClaimExtraction:
        def extract(self, *args, **kwargs):
            return ([], 4, 2)

        def consume_nonfatal_notice(self):
            return None

    settings = AgentSettings(
        first_round_queries=1,
        max_rounds=4,
        testing_mode=True,
        observability_profile="forensic",
    )
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_EmptyClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Was ist passiert?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["perplexity shaped query"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_PerplexityShapedSearch()),
        strategies=strategies,
        settings=settings,
    )

    from inqtrix.evidence import derive_claim_ledger_from_evidence

    assert derive_claim_ledger_from_evidence(state["evidence_ledger"]) == []
    assert state["all_citations"] == ["https://example.com/perplexity-report"]
    # The claimless source survives as a report-eligible EvidenceRecord that
    # still carries its own snippet; provider synthesis lives once in
    # state["query_synthesis"], not duplicated onto the record.
    record = state["evidence_ledger"][0]
    assert record["report_eligible"] is True
    assert record["claims"] == []
    assert state["query_synthesis"][record["query_id"]]["provider_answer"].startswith(
        "Perplexity answer describes"
    )
    assert "rich context for the final answer" in record["source_snippet"]
    assert record["canonical_url"] == "https://example.com/perplexity-report"

    query_summary = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "query_summary"
    ][-1]
    assert query_summary["claim_extraction_valid_empty"] is True
    assert query_summary["evidence_record_count"] >= 1
    assert query_summary["evidence_context_source_count"] == 1

    summary = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "iteration_summary" and entry.get("node") == "search"
    ][-1]
    assert summary["claim_valid_empty"] == 1
    assert summary["_claim_extraction_empty"] is True
    assert summary["claim_fallbacks"] == 0
    assert summary["evidence_record_count"] == 1
    assert summary["report_eligible_evidence_count"] == 1
    assert summary["verified_claim_count"] == 0


def test_sanitize_event_payload_drops_unknown_keys_for_known_event():
    payload = {
        "event": "source_record",
        "source_id": "src_123",
        "url": "https://example.com/report",
        "canonical_url": "https://example.com/report",
        "domain": "example.com",
        "provider": "Custom",
        "first_seen_query_id": "qry_1",
        "first_seen_rank": 1,
        "origin": "search_result",
        "tier": "primary",
        "tier_reason": "matched_primary_domain",
        "access_status": "ok",
        "request_kwargs": {"api_key": "sk-secretsecretsecretsecret"},
        "headers": {"authorization": "Bearer abc"},
        "raw_response": {"token": "pplx-realtoken"},
        "extra_unknown_field": "should-not-appear",
    }

    sanitized = sanitize_event_payload("source_record", payload)

    assert sanitized["source_id"] == "src_123"
    assert sanitized["tier"] == "primary"
    assert "request_kwargs" not in sanitized
    assert "headers" not in sanitized
    assert "raw_response" not in sanitized
    assert "extra_unknown_field" not in sanitized


def test_sanitize_event_payload_keeps_envelope_for_known_event():
    payload = {
        "event": "query_record",
        "event_seq": 7,
        "node": "search",
        "run_id": "run_abc",
        "timestamp": 12345.67,
        "query_id": "qry_1",
        "round": 0,
        "query_index": 0,
        "query": "energiepreis",
        "domain_filter": [],
        "provider": "Custom",
        "source_ids": ["src_1"],
        "citation_ids": ["cit_1"],
    }

    sanitized = sanitize_event_payload("query_record", payload)

    for envelope_key in ("event", "event_seq", "node", "run_id", "timestamp"):
        assert envelope_key in sanitized
    assert sanitized["query_id"] == "qry_1"


def test_sanitize_event_payload_keeps_audit_fields_for_forensic_events():
    claim_payload = {
        "event": "claim_record",
        "raw_claim_id": "raw_1",
        "query_id": "qry_1",
        "signature": "energiepreis steigt",
        "claim_text": "Der Energiepreis steigt laut Quelle.",
        "evidence_snippet": "Die Quelle sagt: Der Energiepreis steigt.",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "source_ids": ["src_1"],
        "citation_ids": ["cit_1"],
        "source_urls": ["https://example.com/report"],
        "published_date": "2026-05-09",
        "round": 0,
        "raw_response": {"token": "secret"},
    }
    query_summary_payload = {
        "event": "query_summary",
        "query_id": "qry_1",
        "round": 0,
        "query_index": 0,
        "query": "energiepreis",
        "answer_length": 42,
        "claims_extracted": 1,
        "claims_kept": 1,
        "claim_extraction_valid_empty": False,
        "claim_extraction_raw_claim_count": 2,
        "claim_extraction_normalized_claim_count": 1,
        "claim_extraction_filtered_claim_count": 1,
        "evidence_record_count": 1,
        "evidence_context_source_count": 1,
        "source_ids": ["src_1"],
        "citation_ids": ["cit_1"],
        "urls": ["https://example.com/report?token=abc123"],
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "provider_notice": "",
        "headers": {"authorization": "Bearer leak"},
    }
    answer_inputs_payload = {
        "event": "answer_prompt_inputs",
        "report_profile": "compact",
        "model": "gpt-test",
        "evidence_record_count": 4,
        "rendered_evidence_record_count": 3,
        "omitted_evidence_record_count": 1,
        "evidence_overview_chars": 1200,
        "visible_evidence_label_count": 2,
        "allowed_citation_count": 3,
        "consolidated_claim_count": 1,
        "algorithm_failure_count": 0,
        "blocking_algorithm_failure_count": 0,
        "evidence_overview": "RESEARCH RESULT R1\n[E1] Source",
        "section_plan": [{"key": "key_points", "heading": "Kernaussagen"}],
        "raw_response": {"token": "secret"},
    }

    claim = sanitize_event_payload("claim_record", claim_payload)
    query_summary = sanitize_event_payload("query_summary", query_summary_payload)
    answer_inputs = sanitize_event_payload("answer_prompt_inputs", answer_inputs_payload)

    assert claim["evidence_snippet"] == "Die Quelle sagt: Der Energiepreis steigt."
    assert claim["published_date"] == "2026-05-09"
    assert "raw_response" not in claim
    assert query_summary["answer_length"] == 42
    assert query_summary["evidence_record_count"] == 1
    assert query_summary["claim_extraction_raw_claim_count"] == 2
    assert query_summary["claim_extraction_normalized_claim_count"] == 1
    assert query_summary["claim_extraction_filtered_claim_count"] == 1
    assert "token=[REDACTED]" in query_summary["urls"][0]
    assert "abc123" not in query_summary["urls"][0]
    assert "headers" not in query_summary
    assert answer_inputs["evidence_overview"].startswith("RESEARCH RESULT")
    assert answer_inputs["rendered_evidence_record_count"] == 3
    assert answer_inputs["visible_evidence_label_count"] == 2
    assert "raw_response" not in answer_inputs


def test_search_rejects_dict_search_provider_results():
    class _DictSearch:
        def search(self, *args, **kwargs):
            return {"answer": "", "citations": []}

        def is_available(self):
            return True

    class _LLM:
        models = SimpleNamespace(reasoning_model="reasoning-model")

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    settings = AgentSettings(first_round_queries=1, testing_mode=True)
    defaults = create_default_strategies(settings, llm=_LLM(), claim_extract_model="claim-extract-model")
    state = initial_state("Was ist passiert?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["q1"]

    with pytest.raises(TypeError, match="GroundedSearchResult"):
        search(
            state,
            providers=ProviderContext(llm=_LLM(), search=_DictSearch()),
            strategies=defaults,
            settings=settings,
        )


def test_search_respects_provider_max_concurrency_cap():
    class _CappedSearch:
        max_search_concurrency = 1

        def search(self, query, **kwargs):
            return GroundedSearchResult(
                answer=f"Antwort fuer {query}",
                sources=[GroundedSource(url=f"https://example.com/{query}", rank=1)],
            )

        def is_available(self):
            return True

    class _LLM:
        models = SimpleNamespace(reasoning_model="reasoning-model")

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    class _EmptyClaimExtraction:
        def extract(self, *args, **kwargs):
            return ([], 0, 0)

        def consume_nonfatal_notice(self):
            return None

    settings = AgentSettings(first_round_queries=3, testing_mode=True)
    llm = _LLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_EmptyClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Was ist passiert?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["q1", "q2", "q3"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_CappedSearch()),
        strategies=strategies,
        settings=settings,
    )

    summary = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "iteration_summary" and entry.get("node") == "search"
    ][-1]
    assert summary["worker_count"] == 1


def test_search_retry_progress_labels_parallel_query_position() -> None:
    class _RetryingSearch:
        def observe_retries(self, callback: Any) -> Any:
            class _RetryContext:
                def __enter__(self_inner: Any) -> Any:
                    callback({
                        "provider": "RetryingSearch",
                        "model": "test-search",
                        "attempt": 1,
                        "max_attempts": 5,
                        "delay_seconds": 0.0,
                        "error_code": "APITimeoutError",
                    })
                    return self_inner

                def __exit__(
                    self_inner: Any,
                    exc_type: type[BaseException] | None,
                    exc: BaseException | None,
                    tb: object | None,
                ) -> bool:
                    return False

            return _RetryContext()

        def search(self, query: str, **kwargs: Any) -> GroundedSearchResult:
            return GroundedSearchResult(answer=f"Antwort fuer {query}")

        def is_available(self) -> bool:
            return True

    class _LLM:
        models = SimpleNamespace(reasoning_model="reasoning-model")

        def complete(self, *args: Any, **kwargs: Any) -> str:
            return ""

        def is_available(self) -> bool:
            return True

    class _EmptyClaimExtraction:
        def extract(self, *args: Any, **kwargs: Any) -> tuple[list[object], int, int]:
            return ([], 0, 0)

        def consume_nonfatal_notice(self) -> None:
            return None

    settings = AgentSettings(first_round_queries=2, testing_mode=True)
    llm = _LLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_EmptyClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,
        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    progress_queue = Queue()
    state = initial_state("Was ist passiert?", progress_queue=progress_queue, max_total_seconds=30)
    state["queries"] = ["q1", "q2"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_RetryingSearch()),
        strategies=strategies,
        settings=settings,
    )

    messages = []
    while not progress_queue.empty():
        messages.append(progress_queue.get()[1])

    assert any("RetryingSearch-Retry 1/5 bei Websuche 1/2" in message for message in messages)
    assert any("RetryingSearch-Retry 1/5 bei Websuche 2/2" in message for message in messages)
    retry_operations = [
        entry.get("operation")
        for entry in state["iteration_logs"]
        if entry.get("event") == "provider_retry"
    ]
    assert sorted(retry_operations) == ["Websuche 1/2", "Websuche 2/2"]


def test_search_records_unknown_provider_ref_markers():
    class _Search:
        def search(self, query, **kwargs):
            return GroundedSearchResult(
                answer=f"Antwort fuer {query} [1]",
                sources=[GroundedSource(url="https://example.com/report", rank=1)],
            )

        def is_available(self):
            return True

    class _LLM:
        models = SimpleNamespace(reasoning_model="reasoning-model")

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    class _ClaimExtraction:
        def extract(self, *args, **kwargs):
            return (
                [
                    {
                        "claim_text": "Ungebundener Claim",
                        "claim_type": "fact",
                        "polarity": "affirmed",
                        "needs_primary": False,
                        "provider_refs": [],
                        "source_urls": [],
                        "binding_status": "unbound",
                        "published_date": "unknown",
                    }
                ],
                0,
                0,
            )

        def consume_extraction_metadata(self):
            return {
                "claim_extraction_mode": "legacy_text_json",
                "unknown_provider_ref_count": 2,
                "unbound_claim_count": 1,
            }

        def consume_nonfatal_notice(self):
            return None

    settings = AgentSettings(first_round_queries=1, testing_mode=True)
    llm = _LLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_ClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Was ist passiert?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["q1"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_Search()),
        strategies=strategies,
        settings=settings,
    )

    summary = [
        entry
        for entry in state["iteration_logs"]
        if entry.get("event") == "iteration_summary" and entry.get("node") == "search"
    ][-1]
    assert summary["unknown_provider_ref_count"] == 2
    assert summary["unbound_claim_count"] == 1
    assert summary["_claim_unknown_provider_refs"] is True
    assert summary["_claim_unbound_claims"] is True


def test_sanitize_event_payload_redacts_url_query_secrets():
    payload = {
        "event": "provider_citation_record",
        "citation_id": "cit_1",
        "query_id": "qry_1",
        "source_id": "src_1",
        "url": "https://example.com/report?api_key=sk-realLookingApiKey1234567890&page=1",
        "canonical_url": "https://example.com/report",
        "rank": 1,
        "origin": "search_result",
        "provider": "Custom",
        "title": "Title",
        "snippet": "Snippet",
    }

    sanitized = sanitize_event_payload("provider_citation_record", payload)

    assert "api_key=[REDACTED]" in sanitized["url"]
    assert "sk-realLookingApiKey1234567890" not in sanitized["url"]
    assert "page=1" in sanitized["url"]


def test_sanitize_event_payload_unknown_event_uses_drop_list_only():
    payload = {
        "event": "iteration_summary",
        "node": "search",
        "prompt_citations": ["https://example.com/report"],
        "fallback_attempted": True,
        "request_kwargs": {"api_key": "sk-secretsecretsecretsecret"},
    }

    sanitized = sanitize_event_payload("iteration_summary", payload)

    assert sanitized["fallback_attempted"] is True
    assert sanitized["prompt_citations"] == ["https://example.com/report"]
    assert "request_kwargs" not in sanitized


def test_audit_previews_remain_bounded_and_redacted():
    payload = {"query": "q" * 400, "password": "hidden",
               "nested": [{"title": "t" * 200}]}
    result = sanitize_event_payload("iteration_summary", payload)
    assert result["query"] == "q" * 240 + "..."
    assert result["nested"] == [{"title": "t" * 180 + "..."}]
    assert "password" not in result


def test_append_iteration_log_sanitizes_iteration_logs_export_for_testing_mode(tmp_path):
    log_path = configure_logging(enabled=True, level="DEBUG", log_dir=str(tmp_path / "logs"))
    state = {"_run_id": "run_abc", "iteration_logs": []}

    append_iteration_log(
        state,
        {
            "node": "answer",
            "prompt_citations": [
                "https://example.com/report?token=abc123",
                "https://example.com/safe?page=2",
            ],
            "headers": {"authorization": "Bearer leak"},
            "request_kwargs": {"api_key": "sk-secretsecretsecretsecret"},
        },
        testing_mode=True,
    )

    _flush_inqtrix_handlers()
    file_content = Path(log_path).read_text(encoding="utf-8")

    assert len(state["iteration_logs"]) == 1
    entry = state["iteration_logs"][0]

    assert "headers" not in entry
    assert "request_kwargs" not in entry
    assert "Bearer leak" not in str(entry)
    assert "sk-secretsecretsecretsecret" not in str(entry)

    assert "token=[REDACTED]" in entry["prompt_citations"][0]
    assert "abc123" not in entry["prompt_citations"][0]
    assert "page=2" in entry["prompt_citations"][1]

    assert "prompt_citations" not in file_content
    assert "token=[REDACTED]" not in file_content
    assert "https://example.com" not in file_content
    assert "abc123" not in file_content
    assert "Bearer leak" not in file_content


def test_search_forensic_dedupes_source_record_per_canonical_url():
    class _LegacySearch:
        def __init__(self) -> None:
            self.calls = 0

        def search(self, *args, **kwargs):
            self.calls += 1
            return GroundedSearchResult(
                answer=f"Antwort {self.calls}",
                sources=[GroundedSource(url="https://example.com/shared-report", rank=1)],
                prompt_tokens=1,
                completion_tokens=2,
            )

        def is_available(self):
            return True

    class _ClaimExtractLLM:
        def __init__(self) -> None:
            self.models = SimpleNamespace(
                reasoning_model="reasoning-model",
                effective_claim_extract_model="claim-extract-model",
            )

        def complete(self, *args, **kwargs):
            return ""

        def is_available(self):
            return True

    class _ClaimExtraction:
        def extract(self, *args, **kwargs):
            return ([], 0, 0)

    settings = AgentSettings(
        first_round_queries=2,
        max_rounds=4,
        testing_mode=True,
        observability_profile="forensic",
    )
    llm = _ClaimExtractLLM()
    defaults = create_default_strategies(settings, llm=llm, claim_extract_model="claim-extract-model")
    strategies = StrategyContext(
        source_tiering=defaults.source_tiering,
        claim_extraction=_ClaimExtraction(),
        claim_consolidation=defaults.claim_consolidation,        risk_scoring=defaults.risk_scoring,
        stop_criteria=defaults.stop_criteria,
    )
    state = initial_state("Warum?", progress_queue=Queue(), max_total_seconds=30)
    state["queries"] = ["q1", "q2"]

    search(
        state,
        providers=ProviderContext(llm=llm, search=_LegacySearch()),
        strategies=strategies,
        settings=settings,
    )

    events = [entry.get("event") for entry in state["iteration_logs"]]
    assert events.count("source_record") == 1
    assert events.count("provider_citation_record") == 2
    assert events.count("query_record") == 2
