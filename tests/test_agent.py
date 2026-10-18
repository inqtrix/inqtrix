"""Tests for the public API: ResearchAgent, AgentConfig, ResearchResult."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from inqtrix.agent import AgentConfig, ResearchAgent
from inqtrix.providers.base import LLMProvider, SearchProvider
from inqtrix.providers.anthropic import AnthropicLLM
from inqtrix.report_profiles import ReportProfile
from inqtrix.result import (
    Claim,
    ClaimMetrics,
    KnowledgeResultState,
    ReportReference,
    ResearchMetrics,
    ResearchResult,
    ResearchResultExportOptions,
    Source,
    SourceMetrics,
    merge_knowledge_result_payload,
)
from inqtrix.search_result import GroundedSearchResult
from inqtrix.settings import AgentSettings
from inqtrix.strategies import (
    create_default_strategies,
)


# ------------------------------------------------------------------ #
# AgentConfig tests
# ------------------------------------------------------------------ #


class TestAgentConfig:
    def test_defaults(self):
        cfg = AgentConfig()
        assert cfg.report_profile == ReportProfile.COMPACT
        assert cfg.max_rounds == 2
        assert cfg.confidence_stop == 7
        assert cfg.answer_prompt_citations_max == 60
        assert cfg.max_total_seconds == 3600
        assert cfg.reasoning_timeout == 600
        assert cfg.search_timeout == 600
        assert cfg.llm is None
        assert cfg.search is None

    def test_override_behaviour(self):
        cfg = AgentConfig(max_rounds=2, confidence_stop=6)
        assert cfg.max_rounds == 2
        assert cfg.confidence_stop == 6

    def test_serialisation_roundtrip(self):
        cfg = AgentConfig(max_rounds=3, report_profile=ReportProfile.DEEP)
        data = cfg.model_dump(exclude={"llm", "search"})
        restored = AgentConfig(**data)
        assert restored.max_rounds == 3
        assert restored.report_profile == ReportProfile.DEEP

    def test_forbids_unknown_and_removed_fields(self):
        """extra='forbid' rejects typos and since-removed fields loudly.

        A removed field (``context_pruning``, deleted with the context-pruning
        strategy) and a plain typo (``does_not_exist``) must both raise instead
        of being silently dropped -- the Python building-block layer is as
        strict as the HTTP override layer (No Silent Fallbacks).
        """
        with pytest.raises(ValidationError):
            AgentConfig(context_pruning=object())
        with pytest.raises(ValidationError):
            AgentConfig(does_not_exist=1)

    def test_accepts_custom_provider(self):
        class DummyLLM(LLMProvider):
            def complete(self, *a, **kw):
                return "test"


            def is_available(self):
                return True

        cfg = AgentConfig(llm=DummyLLM())
        assert cfg.llm is not None
        assert cfg.llm.is_available()


# ------------------------------------------------------------------ #
# ResearchResult tests
# ------------------------------------------------------------------ #


class TestResearchResult:
    def test_empty_result(self):
        r = ResearchResult(answer="test")
        assert r.answer == "test"
        assert r.metrics.rounds == 0
        assert r.top_sources == []
        assert r.top_claims == []

    def test_from_raw_minimal(self):
        raw = {
            "answer": "Die Antwort",
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            "result_state": {
                "round": 2,
                "final_confidence": 7,
                "all_citations": ["https://example.com"],
                "report_references": [
                    {"label": "E1", "url": "https://example.com", "tier": "unknown"},
                ],
                "queries": ["q1", "q2"],
                "consolidated_claims": [
                    {
                        "claim_text": "Ein Fakt",
                        "status": "verified",
                        "claim_type": "fact",
                        "needs_primary": True,
                        "status_reason": "mehrfach belegt",
                        "support_count": 2,
                        "contradict_count": 0,
                        "used_in_answer": True,
                        "source_tier_counts": {
                            "primary": 1,
                            "mainstream": 0,
                            "stakeholder": 0,
                            "unknown": 0,
                            "low": 0,
                        },
                        "source_urls": ["https://example.com"],
                    }
                ],
                "source_tier_counts": {"primary": 0, "mainstream": 0, "stakeholder": 0, "unknown": 1, "low": 0},
                "source_quality_score": 0.35,
                "claim_status_counts": {"verified": 1, "contested": 0, "unverified": 0},
                "claim_quality_score": 1.0,
                "aspect_coverage": 0.5,
                "evidence_consistency": 8,
                "evidence_sufficiency": 6,
                "answer_claim_bindings": [
                    {"binding_status": "matched", "claim_id": "claim_1"},
                ],
                "answer_evidence_bindings": [
                    {"binding_status": "unknown_citation", "claim_id": "claim_1"},
                ],
                # Canonical contract is set by the answer node and read verbatim
                # by ResearchResult.from_raw (single source of truth).
                "evidence_contract_status": "needs_review",
            },
        }
        result = ResearchResult.from_raw(raw)

        assert result.answer == "Die Antwort"
        assert result.metrics.rounds == 2
        assert result.metrics.confidence == 7
        assert result.metrics.total_queries == 2
        assert result.metrics.total_citations == 1
        assert result.metrics.prompt_tokens == 100
        assert result.metrics.completion_tokens == 50
        assert result.metrics.sources.quality_score == 0.35
        assert result.metrics.claims.quality_score == 1.0
        assert result.metrics.answer_bound_claims_count == 1
        assert result.metrics.unbound_answer_citations_count == 1
        assert result.metrics.verified_claims_used_count == 1
        assert result.metrics.evidence_contract_status == "needs_review"
        assert len(result.top_sources) == 1
        assert result.top_sources[0].url == "https://example.com"
        assert len(result.references) == 1
        assert result.references[0].label == "E1"
        assert result.references[0].url == "https://example.com"
        assert len(result.top_claims) == 1
        assert result.top_claims[0].text == "Ein Fakt"
        assert result.top_claims[0].status == "verified"
        assert result.top_claims[0].needs_primary is True
        assert result.top_claims[0].status_reason == "mehrfach belegt"
        assert result.top_claims[0].support_count == 2
        assert result.top_claims[0].source_tier_counts["primary"] == 1

    def test_from_raw_preserves_report_reference_title(self):
        """A reference title (e.g. a knowledge document's original filename)
        must survive from state into the model AND the export payload — the
        knowledge source list renders it instead of the raw citation URL."""
        raw = {
            "answer": "Antwort [K1]",
            "usage": {},
            "result_state": {
                "report_references": [
                    {
                        "label": "K1",
                        "url": "inqtrix://documents/kd_abc#chunk-1",
                        "tier": "primary",
                        "title": "AI ACT Regulation (EU) 2024:1689.pdf",
                    },
                    # No title supplied -> stays None, never invented.
                    {"label": "K2", "url": "inqtrix://documents/kd_def#chunk-2", "tier": "primary"},
                ],
            },
        }
        result = ResearchResult.from_raw(raw)
        assert result.references[0].title == "AI ACT Regulation (EU) 2024:1689.pdf"
        assert result.references[1].title is None
        payload = result.to_export_payload()
        assert payload["references"][0]["title"] == "AI ACT Regulation (EU) 2024:1689.pdf"
        assert payload["references"][1]["title"] is None

    def test_from_raw_accepts_agent_reference_alias_and_grounded_support(self):
        raw = {
            "answer": "Antwort [W1]",
            "usage": {},
            "result_state": {
                "references": [
                    {
                        "label": "W1",
                        "url": "https://example.com/report",
                        "title": "Market report",
                        "grounded_support": "Provider-grounded context",
                    }
                ]
            },
        }

        result = ResearchResult.from_raw(raw)
        payload = result.to_export_payload()

        assert len(result.references) == 1
        assert (
            result.references[0].grounded_support
            == "Provider-grounded context"
        )
        assert (
            payload["references"][0]["grounded_support"]
            == "Provider-grounded context"
        )

    def test_from_raw_empty_state(self):
        raw = {"answer": "Keine Ergebnisse", "usage": {}, "result_state": {}}
        result = ResearchResult.from_raw(raw)
        assert result.answer == "Keine Ergebnisse"
        assert result.metrics.rounds == 0
        assert result.top_sources == []

    def test_from_raw_exports_text_free_knowledge_receipt(self):
        degradation = {
            "reason": "vector_overfetch_cap",
            "retrieval_mode": "hybrid",
            "stage": "vector_candidate_pool",
            "requested_candidate_pool": 40,
            "returned_candidate_pool": 12,
            "final_top_k": 8,
            "final_evidence_complete": False,
            "requested_top_k": 8,
            "returned_hits": 3,
            "candidate_cap": 64,
            # Internal state may carry diagnostic input, but the safe result
            # receipt must never duplicate it.
            "query": "private customer question",
            "source_ids": ["private-source"],
        }
        retrieval_warning = {
            "code": "chunks_require_reindex",
            "message": "Verifizierbare Quelltexte fehlen.",
            "reason": "source_unverified",
            "stage": "canonical_hydration",
            "count": 2,
            "recommended_action": "reindex",
            "source_ids": ["private-source"],
        }
        raw = {
            "answer": "Belegte Antwort [K1].",
            "usage": {},
            "result_state": {
                "queries": ["private customer question"],
                "knowledge_profile": {
                    "id": "gruendlich",
                    "requested": "auto",
                    "auto_selected": True,
                    "auto_reason": "strong_enumeration_marker",
                    "degraded_stages": ["rerank", "rerank"],
                },
                "knowledge_gate": {
                    "enabled": True,
                    "marker": "_knowledge_gate_parsed",
                    "sufficient": False,
                    "coverage": "partial",
                    "reason": "derived from private evidence prose",
                    "rounds_used": 1,
                    "max_rounds": 2,
                    "second_pass": True,
                },
                "knowledge_grounding": {
                    "enabled": True,
                    "marker": "_knowledge_grounding_parsed",
                    "status": "verified",
                    "failure_code": None,
                    "format_repaired": False,
                    "quotes_total": 1,
                    "quotes_verified": 1,
                    "quotes": [
                        {
                            "label": "K1",
                            "text": "private document passage",
                            "verified": True,
                        }
                    ],
                },
                "knowledge_retrieval": {
                    "degradations": [degradation, dict(degradation)],
                    "warnings": [
                        retrieval_warning,
                        {**retrieval_warning, "count": 1},
                    ],
                },
                "knowledge_candidates": 12,
                "knowledge_evidence_used": 3,
            },
        }

        result = ResearchResult.from_raw(raw)
        payload = result.to_export_payload()

        assert isinstance(result.knowledge, KnowledgeResultState)
        assert payload["knowledge_profile"]["degraded_stages"] == ["rerank"]
        assert payload["knowledge_gate"] == {
            "enabled": True,
            "marker": "_knowledge_gate_parsed",
            "sufficient": False,
            "coverage": "partial",
            "rounds_used": 1,
            "max_rounds": 2,
            "second_pass": True,
        }
        assert "reason" not in payload["knowledge_gate"]
        assert payload["knowledge_grounding"]["quotes_total"] == 1
        assert "quotes" not in payload["knowledge_grounding"]
        assert payload["knowledge_retrieval"]["degradations"] == [
            {
                key: value
                for key, value in degradation.items()
                if key not in {"query", "source_ids"}
            }
        ]
        assert payload["knowledge_retrieval"]["warnings"] == [
            {
                key: value
                for key, value in retrieval_warning.items()
                if key not in {"message", "source_ids"}
            }
        ]
        assert payload["knowledge_candidates"] == 12
        assert payload["knowledge_evidence_used"] == 3
        assert "queries" not in payload

        restored = ResearchResult.model_validate_json(result.model_dump_json())
        assert restored.to_export_payload()["knowledge_retrieval"] == (
            payload["knowledge_retrieval"]
        )

    def test_knowledge_result_merge_fails_closed_on_conflicting_receipts(self):
        with pytest.raises(ValueError, match="conflicting knowledge_gate"):
            merge_knowledge_result_payload(
                {
                    "answer": "answer",
                    "knowledge_gate": {
                        "enabled": True,
                        "sufficient": True,
                    },
                },
                {
                    "knowledge_gate": {
                        "enabled": True,
                        "sufficient": False,
                    }
                },
            )

    def test_knowledge_result_merge_enriches_legacy_retrieval_receipt(self):
        legacy = {
            "reason": "vector_overfetch_cap",
            "retrieval_mode": "hybrid",
            "requested_top_k": 8,
            "returned_hits": 3,
            "candidate_cap": 64,
        }
        current = {
            **legacy,
            "stage": "vector_candidate_pool",
            "requested_candidate_pool": 40,
            "returned_candidate_pool": 10,
            "final_top_k": 8,
            "final_evidence_complete": False,
        }

        payload = merge_knowledge_result_payload(
            {
                "knowledge_retrieval": {"degradations": [current]},
            },
            {
                "knowledge_retrieval": {"degradations": [legacy]},
            },
        )

        assert payload["knowledge_retrieval"] == {
            "degradations": [current]
        }

    def test_legacy_partial_knowledge_receipt_remains_readable(self):
        result = ResearchResult.from_raw(
            {
                "answer": "legacy",
                "result_state": {
                    "knowledge_profile": {
                        "id": "standard",
                        "auto_selected": True,
                        "degraded_stages": [],
                    },
                    "knowledge_gate": {
                        "enabled": False,
                        "rounds_used": 0,
                        "max_rounds": 0,
                    },
                    "knowledge_grounding": {
                        "enabled": False,
                        "format_repaired": False,
                        "quotes_total": 0,
                        "quotes_verified": 0,
                    },
                },
            }
        )

        payload = result.to_export_payload()
        assert "requested" not in payload["knowledge_profile"]
        assert payload["knowledge_gate"]["rounds_used"] == 0
        assert payload["knowledge_grounding"]["quotes_total"] == 0

    def test_from_raw_top_sources_prioritize_answer_then_visible_allowlist(self):
        raw = {
            "answer": (
                "Used source [x](https://used.example/report) and "
                "another source [y](https://second.example/report)."
            ),
            "usage": {},
            "result_state": {
                "all_citations": [
                    "https://discovered.example/report",
                    "https://used.example/report",
                    "https://visible.example/report",
                    "https://ledger.example/report",
                ],
                "allowed_citations": ["https://visible.example/report"],
                "evidence_ledger": [
                    {
                        "evidence_id": "ev_1",
                        "report_eligible": True,
                        "canonical_url": "https://ledger.example/report",
                        "citation_set": [{"url": "https://ledger.example/report"}],
                    }
                ],
                "source_records": {
                    "src_1": {
                        "canonical_url": "https://used.example/report",
                        "tier": "primary",
                    },
                    "src_2": {
                        "canonical_url": "https://ledger.example/report",
                        "tier": "mainstream",
                    },
                },
            },
        }

        result = ResearchResult.from_raw(raw)

        assert [source.url for source in result.top_sources[:4]] == [
            "https://used.example/report",
            "https://second.example/report",
            "https://visible.example/report",
            "https://ledger.example/report",
        ]
        assert result.top_sources[0].tier == "primary"
        assert result.top_sources[3].tier == "mainstream"

    def test_json_serialisation(self):
        result = ResearchResult(
            answer="Test",
            metrics=ResearchMetrics(rounds=1, confidence=8),
            top_sources=[Source(url="https://a.com", tier="primary")],
            references=[ReportReference(label="E1", url="https://a.com", tier="primary")],
            top_claims=[Claim(text="Fakt", status="verified")],
        )
        data = result.model_dump()
        assert data["answer"] == "Test"
        assert data["metrics"]["rounds"] == 1
        assert data["top_sources"][0]["tier"] == "primary"
        assert data["references"][0]["label"] == "E1"
        assert data["top_claims"][0]["needs_primary"] is False

        # JSON roundtrip
        json_str = result.model_dump_json()
        restored = ResearchResult.model_validate_json(json_str)
        assert restored.answer == "Test"
        assert restored.metrics.confidence == 8

    def test_claim_defaults_include_full_public_shape(self):
        c = Claim(text="Fakt")
        assert c.needs_primary is False
        assert c.status_reason == ""
        assert c.support_count == 0
        assert c.contradict_count == 0
        assert c.source_tier_counts == {
            "primary": 0,
            "mainstream": 0,
            "stakeholder": 0,
            "unknown": 0,
            "low": 0,
        }

    def test_export_payload_supports_optional_projection(self):
        result = ResearchResult(
            answer="Test",
            metrics=ResearchMetrics(rounds=2, confidence=8),
            top_sources=[
                Source(url="https://www.bundestag.de/dokumente/x", tier="primary"),
                Source(url="https://example.com/x", tier="unknown"),
            ],
            references=[
                ReportReference(label="E1", url="https://www.bundestag.de/dokumente/x", tier="primary"),
                ReportReference(label="E2", url="https://example.com/x", tier="unknown"),
            ],
            top_claims=[
                Claim(text="Fakt 1", status="verified", support_count=2),
                Claim(text="Fakt 2", status="contested", contradict_count=1),
            ],
        )

        payload = result.to_export_payload(
            ResearchResultExportOptions(
                include_sources=False,
                max_claims=1,
            )
        )

        assert payload["answer"] == "Test"
        assert payload["metrics"]["confidence"] == 8
        assert "top_sources" not in payload
        assert len(payload["references"]) == 2
        assert len(payload["top_claims"]) == 1
        assert payload["top_claims"][0]["text"] == "Fakt 1"

    def test_from_raw_exports_all_references_while_top_sources_stays_capped(self):
        reference_urls = [f"https://example.com/reference/{index:02d}" for index in range(65)]
        raw = {
            "answer": "Report",
            "usage": {},
            "result_state": {
                "all_citations": reference_urls,
                "report_references": [
                    {"label": f"E{index + 1}", "url": url, "tier": "unknown"}
                    for index, url in enumerate(reference_urls)
                ],
            },
        }

        result = ResearchResult.from_raw(raw)
        payload = result.to_export_payload()

        assert len(result.top_sources) == 60
        assert len(result.references) == 65
        assert len(payload["top_sources"]) == 60
        assert len(payload["references"]) == 65
        assert payload["references"][64]["label"] == "E65"


# ------------------------------------------------------------------ #
# ResearchAgent construction tests (no LLM calls)
# ------------------------------------------------------------------ #


class TestResearchAgentConstruction:
    def test_default_config(self):
        agent = ResearchAgent()
        assert agent.config.max_rounds == 2

    def test_direct_agent_settings_apply_report_profile_defaults(self):
        settings = AgentSettings(report_profile=ReportProfile.DEEP)

        assert settings.max_rounds == 3
        assert settings.min_rounds == 2
        assert settings.confidence_stop == 7
        assert settings.first_round_queries == 6
        assert settings.answer_prompt_citations_max == 500

    def test_build_settings_keeps_explicit_overrides_over_report_profile_defaults(self):
        settings = ResearchAgent._build_settings(
            AgentConfig(
                report_profile=ReportProfile.DEEP,
                first_round_queries=14,
                max_rounds=7,
            )
        )

        assert settings.report_profile == ReportProfile.DEEP
        assert settings.first_round_queries == 14
        assert settings.max_rounds == 7
        assert settings.confidence_stop == 7

    def test_default_initialisation_reads_dotenv(self, tmp_path, monkeypatch):
        import inqtrix.providers as providers_module

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("MAX_ROUNDS", raising=False)
        monkeypatch.delenv("REASONING_MODEL", raising=False)
        monkeypatch.delenv("SEARCH_MODEL", raising=False)
        monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
        monkeypatch.delenv("LITELLM_API_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "\n".join([
                "MAX_ROUNDS=7",
                "REASONING_MODEL=dotenv-reasoning",
                "SEARCH_MODEL=dotenv-search",
                "LITELLM_BASE_URL=http://dotenv/v1",
                "LITELLM_API_KEY=dotenv-key",
            ])
            + "\n",
            encoding="utf-8",
        )

        class DummyOpenAI:
            def __init__(self, *, base_url, api_key, **_kw):
                self.base_url = base_url
                self.api_key = api_key
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))

        monkeypatch.setattr(providers_module, "OpenAI", DummyOpenAI)

        agent = ResearchAgent()

        providers, _, settings = agent._ensure_initialised()

        assert settings.max_rounds == 7
        assert providers.llm.models.reasoning_model == "dotenv-reasoning"
        assert providers.search._model == "dotenv-search"
        assert providers.llm._client.base_url == "http://dotenv/v1"
        assert providers.llm._client.api_key == "dotenv-key"

    def test_custom_config(self):
        cfg = AgentConfig(max_rounds=2, confidence_stop=6)
        agent = ResearchAgent(cfg)
        assert agent.config.max_rounds == 2
        assert agent.config.confidence_stop == 6

    def test_config_property(self):
        cfg = AgentConfig(max_rounds=3)
        agent = ResearchAgent(cfg)
        assert agent.config is cfg

    def test_ensure_initialised_respects_explicit_config_over_env(self, monkeypatch):
        import inqtrix.providers as providers_module
        from inqtrix.providers.litellm import LiteLLM
        from inqtrix.providers.perplexity import PerplexitySearch

        monkeypatch.setenv("MAX_ROUNDS", "99")
        monkeypatch.setenv("REASONING_MODEL", "env-model")
        monkeypatch.setenv("LITELLM_BASE_URL", "http://env/v1")
        monkeypatch.setenv("LITELLM_API_KEY", "env-key")

        class DummyOpenAI:
            def __init__(self, *, base_url, api_key, **_kw):
                self.base_url = base_url
                self.api_key = api_key
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=None))

        monkeypatch.setattr(providers_module, "OpenAI", DummyOpenAI)

        llm = LiteLLM(
            api_key="custom-key",
            base_url="http://custom/v1",
            default_model="custom-model",
        )
        search = PerplexitySearch(
            api_key="custom-key",
            base_url="http://custom/v1",
            model="sonar-pro",
            _client=llm._client,
        )
        cfg = AgentConfig(
            llm=llm,
            search=search,
            max_rounds=2,
            answer_prompt_citations_max=11,
        )
        agent = ResearchAgent(cfg)

        providers, _, settings = agent._ensure_initialised()

        assert settings.max_rounds == 2
        assert settings.answer_prompt_citations_max == 11
        assert providers.llm.models.reasoning_model == "custom-model"
        assert str(providers.llm._client.base_url).rstrip("/") == "http://custom/v1"
        assert providers.llm._client.api_key == "custom-key"

    def test_default_claim_extraction_uses_provider_interface(self):
        class DummyLLM(LLMProvider):
            def complete(self, *a, **kw):
                return (
                    '{"claims":[{"claim_text":"Der Beitrag steigt um 5 Prozent.",'
                    '"claim_type":"fact","polarity":"affirmed","needs_primary":true,'
                    '"source_urls":["https://example.com/report"],'
                    '"published_date":"2026-03-29"}]}'
                )


            def is_available(self):
                return True

        strategies = create_default_strategies(
            AgentSettings(),
            llm=DummyLLM(),
            claim_extract_model="summary-model",
        )
        claims, prompt_tokens, completion_tokens = strategies.claim_extraction.extract(
            "Kurztext",
            ["https://example.com/report"],
            "Was passiert?",
        )

        assert len(claims) == 1
        assert claims[0]["claim_text"] == "Der Beitrag steigt um 5 Prozent."
        assert prompt_tokens == 0
        assert completion_tokens == 0

    def test_custom_llm_without_models_gets_configured_model_metadata(self):
        class DummyLLM(LLMProvider):
            def __init__(self):
                self.models_seen: list[str | None] = []

            def complete(self, *a, **kw):
                self.models_seen.append(kw.get("model"))
                return "ok"


            def is_available(self):
                return True

        class DummySearch(SearchProvider):
            def search(self, *a, **kw):
                return GroundedSearchResult()

            def is_available(self):
                return True

        llm = DummyLLM()
        cfg = AgentConfig(
            llm=llm,
            search=DummySearch(),
        )
        agent = ResearchAgent(cfg)

        providers, _, _ = agent._ensure_initialised()

        assert providers.llm.complete("test") == "ok"
        assert llm.models_seen == [None]
        # Custom LLM without .models keeps provider-native model defaults
        assert hasattr(providers.llm, "models")
        assert providers.llm.models.reasoning_model == ""

    def test_anthropic_llm_keeps_own_model_metadata(self):
        class DummySearch(SearchProvider):
            def search(self, *a, **kw):
                return GroundedSearchResult()

            def is_available(self):
                return True

        llm = AnthropicLLM(
            api_key="anthropic-key",
            default_model="claude-opus-4-6",
            classify_model="claude-sonnet-4-6",
            claim_extract_model="claude-sonnet-4-6",
            evaluate_model="claude-haiku-4-5",
        )
        agent = ResearchAgent(AgentConfig(llm=llm, search=DummySearch()))

        providers, _, _ = agent._ensure_initialised()

        assert providers.llm.models.reasoning_model == "claude-opus-4-6"
        assert providers.llm.models.effective_classify_model == "claude-sonnet-4-6"
        assert providers.llm.models.effective_claim_extract_model == "claude-sonnet-4-6"
        assert providers.llm.models.effective_evaluate_model == "claude-haiku-4-5"

    def test_stream_yields_answer_in_word_chunks(self, monkeypatch):
        import inqtrix.graph as graph_module

        def fake_run(
            question,
            *,
            history,
            progress_queue,
            providers,
            strategies,
            settings,
        ):
            progress_queue.put(("progress", "Schritt 1"))
            return {"answer": "Hallo Welt"}

        monkeypatch.setattr(graph_module, "run", fake_run)

        agent = ResearchAgent(AgentConfig())
        monkeypatch.setattr(agent, "_ensure_initialised", lambda: (None, None, None))

        chunks = list(agent.stream("Meine Frage"))

        assert chunks == ["> Schritt 1\n", "---\n", "Hallo ", "Welt"]

    def test_stream_can_skip_progress_updates(self, monkeypatch):
        import inqtrix.graph as graph_module

        def fake_run(
            question,
            *,
            history,
            progress_queue,
            providers,
            strategies,
            settings,
        ):
            assert progress_queue is None
            return {"answer": "Hallo Welt"}

        monkeypatch.setattr(graph_module, "run", fake_run)

        agent = ResearchAgent(AgentConfig())
        monkeypatch.setattr(agent, "_ensure_initialised", lambda: (None, None, None))

        chunks = list(agent.stream("Meine Frage", include_progress=False))

        assert chunks == ["Hallo ", "Welt"]


# ------------------------------------------------------------------ #
# Pydantic model tests
# ------------------------------------------------------------------ #


class TestPydanticModels:
    def test_source_defaults(self):
        s = Source(url="https://example.com")
        assert s.tier == "unknown"

    def test_claim_defaults(self):
        c = Claim(text="test")
        assert c.status == "unverified"
        assert c.claim_type == "fact"
        assert c.sources == []

    def test_source_metrics_defaults(self):
        m = SourceMetrics()
        assert m.quality_score == 0.0
        assert m.tier_counts["primary"] == 0

    def test_claim_metrics_defaults(self):
        m = ClaimMetrics()
        assert m.quality_score == 0.0
        assert m.status_counts["verified"] == 0

    def test_research_metrics_nesting(self):
        m = ResearchMetrics(
            rounds=3,
            confidence=8,
            sources=SourceMetrics(quality_score=0.7),
            claims=ClaimMetrics(quality_score=0.9),
        )
        assert m.sources.quality_score == 0.7
        assert m.claims.quality_score == 0.9


def test_metrics_carry_the_single_source_ratio_of_an_active_depth_gap() -> None:
    """The share the research child prints as 'NN % Einzelquellen' is also
    a structured metric, so a parent run can carry it forward."""
    from inqtrix.result import ResearchResult

    def raw(gap: dict) -> dict:
        return {
            "answer": "Die Antwort",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            "result_state": {
                "answer": "Die Antwort",
                "round": 1,
                "final_confidence": 6,
                "evidence_contract_status": "needs_review",
                "evidence_depth_gap": gap,
            },
        }

    active = ResearchResult.from_raw(raw({"active": True, "single_source_ratio": 0.93}))
    assert active.metrics.single_source_ratio == 0.93
    inactive = ResearchResult.from_raw(raw({"active": False, "single_source_ratio": 0.93}))
    assert inactive.metrics.single_source_ratio == 0.0


def test_metrics_carry_the_evidence_budget_cut_of_the_answer() -> None:
    """A report that could not evaluate every source says so in its metrics,
    so a parent run reads 'partial' instead of a confident-looking report
    that silently dropped sources it never evaluated."""
    from inqtrix.result import ResearchResult

    raw = {
        "answer": "Die Antwort",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "result_state": {
            "answer": "Die Antwort",
            "round": 1,
            "final_confidence": 3,
            "rendered_evidence_record_count": 16,
            "omitted_evidence_record_count": 21,
            "omitted_evidence_by_query": [
                {"query_id": "q1", "query": "Foerderung Oesterreich 2026", "omitted": 12, "rendered": 0},
                {"query_id": "q2", "query": "Fristen", "omitted": 9, "rendered": 4},
            ],
        },
    }
    metrics = ResearchResult.from_raw(raw).metrics
    assert metrics.rendered_evidence_record_count == 16
    assert metrics.omitted_evidence_record_count == 21
    assert metrics.omitted_evidence_query_count == 2
    clean = dict(raw)
    clean["result_state"] = {"answer": "Die Antwort", "round": 1, "final_confidence": 8}
    untouched = ResearchResult.from_raw(clean).metrics
    assert (untouched.omitted_evidence_record_count, untouched.omitted_evidence_query_count) == (0, 0)
