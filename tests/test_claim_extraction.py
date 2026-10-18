"""Direct regression tests for claim extraction strategy behavior."""

import logging
from types import SimpleNamespace

from inqtrix.exceptions import AgentStructuredOutputError, BedrockAPIError
from inqtrix.strategies import ClaimExtractionStrategy, LLMClaimExtractor, ProviderCitationRef
from inqtrix.strategies._claim_consolidation import DefaultClaimConsolidator
from inqtrix.strategies._source_tiering import DefaultSourceTiering


class _BedrockFailingLLM:
    def complete_with_metadata(self, *args, **kwargs):
        raise BedrockAPIError(model="bedrock-sonnet", message="temporary failure")


class _RecordingLLM:
    def __init__(self) -> None:
        self.last_prompt = ""
        self.last_kwargs = {}

    def complete_with_metadata(self, prompt, *args, **kwargs):
        self.last_prompt = prompt
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(
            content=(
                '{"claims": ['
                '{"claim_text": "Erster Claim mit Zahl 10 Prozent.", "claim_type": "fact", '
                '"polarity": "affirmed", "needs_primary": true, '
                '"evidence_snippet": "Im Bericht steht: Die Kennzahl stieg um 10 Prozent.", '
                '"provider_refs": ["1", "2"], '
                '"published_date": "2026-04-13"}, '
                '{"claim_text": "Zweiter Claim mit Verbandssicht.", "claim_type": "actor_claim", '
                '"polarity": "affirmed", "needs_primary": false, '
                '"provider_refs": ["1"], '
                '"published_date": "2026-04-13"}, '
                '{"claim_text": "Dritter Claim fuer den Overflow.", "claim_type": "fact", '
                '"polarity": "affirmed", "needs_primary": false, '
                '"provider_refs": ["1"], '
                '"published_date": "2026-04-13"}'
                ']} '
            ),
            prompt_tokens=11,
            completion_tokens=7,
        )


class _InvalidJsonLLM:
    def complete_with_metadata(self, *args, **kwargs):
        return SimpleNamespace(
            content="not json",
            prompt_tokens=13,
            completion_tokens=5,
        )


class _EmptyClaimsLLM:
    def complete_with_metadata(self, *args, **kwargs):
        return SimpleNamespace(
            content='{"claims": []}',
            prompt_tokens=17,
            completion_tokens=3,
        )


class _ProviderRefsLLM:
    def __init__(self) -> None:
        self.last_prompt = ""

    def complete_with_metadata(self, prompt, *args, **kwargs):
        del args, kwargs
        self.last_prompt = prompt
        return SimpleNamespace(
            content=(
                '{"claims": ['
                '{"claim_text": "NVIDIA meldete Umsatzwachstum laut zwei Quellen.", '
                '"claim_type": "fact", "polarity": "affirmed", '
                '"needs_primary": true, "evidence_snippet": "NVIDIA meldete Wachstum [2][3].", '
                '"provider_refs": ["2", "web:3"], '
                '"published_date": "2026-05-23"}'
                ']}'
            ),
            prompt_tokens=23,
            completion_tokens=9,
        )


class _UnknownProviderRefsLLM:
    def complete_with_metadata(self, prompt, *args, **kwargs):
        del prompt, args, kwargs
        return SimpleNamespace(
            content=(
                '{"claims": ['
                '{"claim_text": "Der Text nennt eine belegte Aussage.", '
                '"claim_type": "fact", "polarity": "affirmed", '
                '"needs_primary": false, "evidence_snippet": "Aussage im Text [99].", '
                '"provider_refs": ["99"], '
                '"source_urls": ["https://example.com/known"], '
                '"published_date": "2026-05-23"}'
                ']}'
            ),
            prompt_tokens=11,
            completion_tokens=5,
        )


class _StructuredLLM:
    def __init__(self) -> None:
        self.last_schema = {}
        self.last_schema_name = ""
        self.last_kwargs = {}

    def supports_structured_output(self, *, model: str | None = None) -> bool:
        del model
        return True

    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict,
        schema_name: str,
        **kwargs: object,
    ) -> SimpleNamespace:
        self.last_prompt = prompt
        self.last_schema = schema
        self.last_schema_name = schema_name
        self.last_kwargs = dict(kwargs)
        return SimpleNamespace(
            parsed={
                "claims": [
                    {
                        "claim_text": "Structured Claim mit Zahl 42 Prozent.",
                        "claim_type": "fact",
                        "polarity": "affirmed",
                        "needs_primary": True,
                        "evidence_snippet": "Die Kennzahl lag bei 42 Prozent.",
                        "provider_refs": ["1"],
                        "published_date": "2026-05-15",
                    }
                ]
            },
            content='{"claims":[]}',
            prompt_tokens=19,
            completion_tokens=6,
            finish_reason="stop",
            request_max_tokens=64000,
        )

    def complete_with_metadata(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("legacy JSON path should not be used")

    def last_prompt_card(self) -> list[dict]:
        import json as _json
        block = self.last_prompt.split("Source map:\n", 1)[1].split("\n\nText:", 1)[0]
        return _json.loads(block)


class _StructuredFailingLLM:
    def supports_structured_output(self, *, model: str | None = None) -> bool:
        del model
        return True

    def complete_structured(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AgentStructuredOutputError(
            "structured-model",
            "inqtrix_claim_extraction_v2",
            "invalid object",
        )


def test_claim_extraction_bedrock_error_falls_back_nonfatally():
    extractor = LLMClaimExtractor(
        _BedrockFailingLLM(),
        claim_extract_model="bedrock-sonnet",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "Kurzer Testtext",
        ["https://example.com/report"],
        "Was ist passiert?",
    )

    assert claims == []
    assert prompt_tokens == 0
    assert completion_tokens == 0
    notice = extractor.consume_nonfatal_notice()
    # Visibility: the notice must include the model AND the underlying
    # exception class + message, not just a generic "failed".
    assert notice is not None
    assert "ALGO-FAIL claim_extraction" in notice
    assert "bedrock-sonnet failed" in notice
    assert "BedrockAPIError" in notice
    assert "no structured claims emitted" in notice


def test_claim_extraction_invalid_json_is_visible_nonfatal_fallback():
    extractor = LLMClaimExtractor(
        _InvalidJsonLLM(),
        claim_extract_model="json-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "Suchantwort mit Quelleninhalt",
        ["https://example.com/report"],
        "Was ist passiert?",
    )

    assert claims == []
    assert prompt_tokens == 13
    assert completion_tokens == 5
    notice = extractor.consume_nonfatal_notice()
    assert notice is not None
    assert "ALGO-FAIL claim_extraction" in notice
    assert "invalid or incomplete JSON" in notice
    assert "no structured claims emitted" in notice


def test_claim_extraction_valid_empty_claims_is_not_a_parse_fallback():
    extractor = LLMClaimExtractor(
        _EmptyClaimsLLM(),
        claim_extract_model="empty-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "Suchantwort mit Quelleninhalt",
        ["https://example.com/report"],
        "Was ist passiert?",
    )

    assert claims == []
    assert prompt_tokens == 17
    assert completion_tokens == 3
    assert extractor.consume_nonfatal_notice() is None
    metadata = extractor.consume_extraction_metadata()
    assert metadata["claim_extraction_raw_claim_count"] == 0
    assert metadata["claim_extraction_normalized_claim_count"] == 0
    assert metadata["claim_extraction_filtered_claim_count"] == 0


def test_claim_extraction_respects_custom_limits():
    llm = _RecordingLLM()
    extractor = LLMClaimExtractor(
        llm,
        claim_extract_model="stub-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "ABCDE-truncated-text",
        ["https://example.com/a", "https://example.com/b"],
        "Was ist passiert?",
        text_char_limit=5,
        citation_cap=1,
        max_claims=2,
        source_url_limit=1,
        provider_refs=[
            ProviderCitationRef(ref="1", url="https://example.com/a"),
            ProviderCitationRef(ref="2", url="https://example.com/b"),
        ],
    )

    assert len(claims) == 2
    assert all(len(claim["source_urls"]) <= 1 for claim in claims)
    assert claims[0]["evidence_snippet"] == "Im Bericht steht: Die Kennzahl stieg um 10 Prozent."
    assert "evidence_snippet" not in claims[1]
    assert 'Source map:\n[{"ref": "1", "url": "https://example.com/a"}]' in llm.last_prompt
    assert "Quellenliste:" not in llm.last_prompt
    assert llm.last_prompt.count("https://example.com/a") == 1
    assert "https://example.com/b" not in llm.last_prompt
    assert '"evidence_snippet": "Short supporting excerpt from the text"' in llm.last_prompt
    assert "Text:\nABCDE" in llm.last_prompt
    assert "max_output_tokens" not in llm.last_kwargs
    assert prompt_tokens == 11
    assert completion_tokens == 7
    metadata = extractor.consume_extraction_metadata()
    assert metadata["claim_extraction_mode"] == "legacy_text_json"
    assert metadata["claim_extraction_structured_supported"] is False
    assert metadata["claim_extraction_raw_claim_count"] == 3
    assert metadata["claim_extraction_normalized_claim_count"] == 2
    assert metadata["claim_extraction_filtered_claim_count"] == 1
    assert metadata["citation_prompt_count"] == 1
    assert metadata["citation_omitted_count"] == 1


def test_citation_prompt_counts_follow_the_card_when_provider_refs_are_absent():
    llm = _RecordingLLM()
    extractor = LLMClaimExtractor(llm, claim_extract_model="stub-model")
    extractor.extract("A source describes the result.", ["https://example.com/a"], "Question")
    metadata = extractor.consume_extraction_metadata()
    assert "Source map:\n[]" in llm.last_prompt
    assert "https://example.com/a" not in llm.last_prompt
    assert metadata["citation_count"] == 1
    assert metadata["citation_prompt_count"] == 0
    assert metadata["citation_omitted_count"] == 1


def test_claim_extraction_resolves_provider_refs_to_source_urls() -> None:
    llm = _ProviderRefsLLM()
    extractor = LLMClaimExtractor(
        llm,
        claim_extract_model="stub-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "NVIDIA meldete Wachstum [2][3].",
        [
            "https://investor.nvidia.com/results",
            "https://www.reuters.com/markets/nvidia-results",
        ],
        "Welche Quartalszahlen hat NVIDIA erreicht?",
        provider_refs=[
            ProviderCitationRef(
                ref="2",
                url="https://investor.nvidia.com/results",
                title="NVIDIA results",
            ),
            ProviderCitationRef(
                ref="3",
                url="https://www.reuters.com/markets/nvidia-results",
                title="Reuters NVIDIA results",
            ),
        ],
    )

    assert claims[0]["source_urls"] == [
        "https://investor.nvidia.com/results",
        "https://www.reuters.com/markets/nvidia-results",
    ]
    assert claims[0]["provider_refs"] == ["2", "3"]
    assert '"ref": "2"' in llm.last_prompt
    assert '"url": "https://investor.nvidia.com/results"' in llm.last_prompt
    assert prompt_tokens == 23
    assert completion_tokens == 9


def test_claim_extraction_ignores_model_source_urls_and_marks_unknown_refs(caplog) -> None:
    extractor = LLMClaimExtractor(
        _UnknownProviderRefsLLM(),
        claim_extract_model="stub-model",
    )
    caplog.set_level(logging.WARNING, logger="inqtrix")

    claims, _, _ = extractor.extract(
        "Aussage im Text [99].",
        ["https://example.com/known"],
        "Was ist passiert?",
        provider_refs=[
            ProviderCitationRef(ref="1", url="https://example.com/known"),
        ],
    )

    assert claims[0]["provider_refs"] == []
    assert claims[0]["source_urls"] == []
    assert claims[0]["binding_status"] == "unbound"
    metadata = extractor.consume_extraction_metadata()
    assert metadata["unknown_provider_ref_count"] == 1
    assert metadata["unbound_claim_count"] == 1
    assert "Claim cited unknown provider refs" in caplog.text


def test_claim_extraction_uses_structured_output_when_available() -> None:
    llm = _StructuredLLM()
    extractor = LLMClaimExtractor(
        llm,
        claim_extract_model="structured-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "Suchantwort mit Quelleninhalt",
        ["https://example.com/report"],
        "Was ist passiert?",
        provider_refs=[ProviderCitationRef(ref="1", url="https://example.com/report")],
    )

    assert len(claims) == 1
    assert claims[0]["claim_text"] == "Structured Claim mit Zahl 42 Prozent."
    assert claims[0]["source_urls"] == ["https://example.com/report"]
    assert llm.last_schema_name == "inqtrix_claim_extraction_v2"
    assert llm.last_schema["required"] == ["claims", "source_roles"]
    claim_schema = llm.last_schema["properties"]["claims"]["items"]
    assert "source_urls" not in claim_schema["properties"]
    assert set(claim_schema["required"]) == set(claim_schema["properties"])
    assert llm.last_kwargs["model"] == "structured-model"
    assert prompt_tokens == 19
    assert completion_tokens == 6
    assert extractor.consume_nonfatal_notice() is None
    metadata = extractor.consume_extraction_metadata()
    assert metadata["claim_extraction_mode"] == "structured_output"
    assert metadata["claim_extraction_schema"] == "inqtrix_claim_extraction_v2"
    assert metadata["claim_extraction_structured_supported"] is True
    assert metadata["claim_extraction_raw_claim_count"] == 1
    assert metadata["claim_extraction_normalized_claim_count"] == 1
    assert metadata["claim_extraction_filtered_claim_count"] == 0


def test_claim_extraction_structured_output_failure_is_algo_fail() -> None:
    extractor = LLMClaimExtractor(
        _StructuredFailingLLM(),
        claim_extract_model="structured-model",
    )

    claims, prompt_tokens, completion_tokens = extractor.extract(
        "Suchantwort mit Quelleninhalt",
        ["https://example.com/report"],
        "Was ist passiert?",
    )

    assert claims == []
    assert prompt_tokens == 0
    assert completion_tokens == 0
    notice = extractor.consume_nonfatal_notice()
    assert notice is not None
    assert "ALGO-FAIL claim_extraction" in notice
    assert "invalid structured output" in notice
    metadata = extractor.consume_extraction_metadata()
    assert metadata["claim_extraction_mode"] == "structured_output"


def test_claim_consolidation_uses_independent_evidence_ids_for_cross_checking():
    consolidator = DefaultClaimConsolidator(DefaultSourceTiering())
    claim_ledger = [
        {
            "raw_claim_id": "raw_1",
            "claim_text": "Meta expects 115-135 USD billion of capital expenditures.",
            "claim_type": "fact",
            "polarity": "affirmed",
            "needs_primary": True,
            "source_urls": ["https://investor.atmeta.com/report"],
            "source_tiers": {"https://investor.atmeta.com/report": "primary"},
            "source_ids": ["src_1"],
            "citation_ids": ["cit_1"],
            "evidence_ids": ["ev_1"],
            "signature": "meta expects capex 115 135 usd billion",
        },
        {
            "raw_claim_id": "raw_2",
            "claim_text": "Meta expects 115-135 USD billion of capital expenditures.",
            "claim_type": "fact",
            "polarity": "affirmed",
            "needs_primary": True,
            "source_urls": ["https://www.bloomberg.com/news/articles/meta-capex"],
            "source_ids": ["src_2"],
            "citation_ids": ["cit_2"],
            "evidence_ids": ["ev_2"],
            "signature": "meta expects capex 115 135 usd billion",
        },
    ]

    consolidated = consolidator.consolidate(claim_ledger)

    assert len(consolidated) == 1
    claim = consolidated[0]
    assert claim["status"] == "attributed"
    assert claim["verification_basis"] == "primary_source"
    assert claim["primary_basis"] == "role"
    assert claim["supporting_evidence_ids"] == ["ev_1", "ev_2"]
    assert claim["supporting_domain_count"] == 2


def test_news_question_focus_stems_do_not_drop_ai_release_claims():
    consolidator = DefaultClaimConsolidator(DefaultSourceTiering())
    focus_stems = consolidator.focus_stems_from_question(
        "Was waren die wichtigsten KI-Entwicklungen der letzten 7 Tage?"
    )
    claims = [
        "OpenAI GPT-5.5 Instant wurde am 5. Mai 2026 als Standardmodell ausgerollt.",
        "Gemini 3.2 Flash appeared in Google AI Studio on May 5, 2026.",
        "Anthropic announced Claude Mythos Preview for selected partners.",
    ]

    assert all(
        consolidator.claim_matches_focus_stems(claim, focus_stems)
        for claim in claims
    )


class _LowMarkingTiering(DefaultSourceTiering):
    """A strategy that judges two aggregator hosts low (the default floor
    judges no site; the exclusion logic is what this pins)."""

    def tier_for_url(self, url: str) -> str:
        if "aitoolsrecap.com" in url or "toolscompare.ai" in url:
            return "low"
        return super().tier_for_url(url)


def test_fallback_citation_selection_excludes_low_quality_domains():
    consolidator = DefaultClaimConsolidator(_LowMarkingTiering())

    selected = consolidator.select_answer_citations(
        [],
        [
            "https://aitoolsrecap.com/Blog/ai-news-may-2026",
            "https://openai.com/index/introducing-gpt-5-5",
            "https://techcrunch.com/2026/05/05/openai-releases-gpt-5-5-instant-a-new-default-model-for-chatgpt",
            "https://toolscompare.ai/news/may-2026",
        ],
        max_items=4,
        source_tiering=_LowMarkingTiering(),
    )

    assert "https://openai.com/index/introducing-gpt-5-5" in selected
    assert "https://techcrunch.com/2026/05/05/openai-releases-gpt-5-5-instant-a-new-default-model-for-chatgpt" in selected
    assert all("aitoolsrecap.com" not in url for url in selected)
    assert all("toolscompare.ai" not in url for url in selected)


class _RolesLLM(_StructuredLLM):
    """A structured reply that judges the card: one valid role, one unknown
    ref, one role outside the vocabulary, one duplicate of a judged ref."""

    def complete_structured(self, prompt: str, *, schema: dict, schema_name: str, **kwargs: object) -> SimpleNamespace:
        response = super().complete_structured(prompt, schema=schema, schema_name=schema_name, **kwargs)
        response.parsed["source_roles"] = [
            {"ref": "1", "role": "OFFICIAL_PRIMARY", "reason": "  Foerderbank des   Bundes  "},
            {"ref": "9", "role": "reporting", "reason": "unknown ref"},
            {"ref": "2", "role": "influencer", "reason": "no such role"},
            {"ref": "1", "role": "low", "reason": "a second judgement of the same source"},
        ]
        return response


def test_source_roles_are_validated_against_the_card_and_first_judgement_wins():
    """The extractor judges every source of the card once; unknown refs and
    roles outside the vocabulary are dropped and counted, a second judgement
    of the same source does not overwrite the first."""
    llm = _RolesLLM()
    extractor = LLMClaimExtractor(llm, claim_extract_model="structured-model")
    extractor.extract(
        "Die Kennzahl lag bei 42 Prozent.",
        ["https://example.com/report", "https://example.com/second"],
        "Was ist passiert?",
        provider_refs=[
            ProviderCitationRef(ref="1", url="https://example.com/report"),
            ProviderCitationRef(ref="2", url="https://example.com/second"),
        ],
    )
    roles = extractor.consume_source_roles()
    assert roles == [{"url": "https://example.com/report", "role": "official_primary", "reason": "Foerderbank des Bundes"}]
    assert extractor.consume_source_roles() == [], "consumed once"
    metadata = extractor.consume_extraction_metadata()
    assert metadata["source_role_count"] == 1
    assert metadata["source_role_invalid_count"] == 2
    assert metadata["source_role_missing_count"] == 1
    schema = llm.last_schema["properties"]["source_roles"]["items"]
    assert schema["properties"]["role"]["enum"][0] == "official_primary"
    from inqtrix.prompts import build_claim_extraction_prompt
    assert "source_roles: judge EVERY source" in build_claim_extraction_prompt(max_claims=8)


class _CappedCardLLM(_StructuredLLM):
    """Judges exactly the refs on a two-entry card and writes a paragraph as reason."""

    def complete_structured(self, prompt: str, *, schema: dict, schema_name: str, **kwargs: object) -> SimpleNamespace:
        response = super().complete_structured(prompt, schema=schema, schema_name=schema_name, **kwargs)
        response.parsed["source_roles"] = [
            {"ref": "1", "role": "official_primary", "reason": "Behoerde " * 40},
            {"ref": "2", "role": "reporting", "reason": "Redaktion"},
        ]
        return response


def test_missing_roles_count_only_the_card_and_fixed_rows_are_not_judged(caplog):
    """Three provider refs, a card of two: the third ref was never shown, so
    it is not 'missing'; a row fixed by the run is neither judged nor
    missing; an over-long reason is cut with a counter and a warning."""
    llm = _CappedCardLLM()
    extractor = LLMClaimExtractor(llm, claim_extract_model="structured-model")
    with caplog.at_level("WARNING", logger="inqtrix"):
        extractor.extract(
            "Die Kennzahl lag bei 42 Prozent.",
            ["https://example.com/report", "https://example.com/second", "https://example.com/third"],
            "Was ist passiert?",
            provider_refs=[
                ProviderCitationRef(ref="1", url="https://example.com/report"),
                ProviderCitationRef(ref="2", url="https://example.com/second"),
                ProviderCitationRef(ref="3", url="https://example.com/third"),
            ],
            citation_cap=2,
            fixed_roles={"https://example.com/second": "reporting"},
        )
    roles = extractor.consume_source_roles()
    assert [r["url"] for r in roles] == ["https://example.com/report"], "the fixed row is not judged again"
    assert len(roles[0]["reason"]) == 160
    metadata = extractor.consume_extraction_metadata()
    assert metadata["source_role_missing_count"] == 0, "the third ref was never on the card"
    assert metadata["source_role_reason_truncated_count"] == 1
    assert "reason(s) truncated" in caplog.text
    card = llm.last_prompt_card()
    assert card[1].get("role_fixed") == "reporting" and "role_fixed" not in card[0]


def test_a_custom_extractor_without_roles_leaves_every_source_unjudged():
    class _Legacy(ClaimExtractionStrategy):
        def extract(self, *args, **kwargs):
            return [], 0, 0

    assert _Legacy().consume_source_roles() == []
