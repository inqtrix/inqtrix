"""Tests for prompt variants used by the final answer node."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.evidence import STANDING_TEXT
from inqtrix.nodes import (
    _domain_filter_for_query_text,
    _select_additional_links,
)
from inqtrix.prompt_language import OUTPUT_FORMAT_BLOCK, QUESTION_LANGUAGE_DIRECTIVE
from inqtrix.prompts import (
    build_answer_section_system_prompt,
    build_answer_section_user_prompt,
    build_answer_system_prompt,
    build_claim_extraction_prompt,
    build_knowledge_answer_prompt,
)
from inqtrix.report_profiles import (
    AnswerSectionSpec,
    ReportProfile,
    evidence_limits_section_index,
    tuning_for_report_profile,
)
from inqtrix.strategies._source_tiering import DefaultSourceTiering


def _base_state(report_profile: ReportProfile) -> dict[str, object]:
    return {
        "today_str": "2026-04-13",
        "answer_lang": "Deutsch",
        "evidence_overview": "",
        "allowed_citations": [],
        "source_tier_counts": {},
        "claim_status_counts": {},
        "report_profile": report_profile,
    }


def test_build_answer_system_prompt_compact_profile():
    prompt = build_answer_system_prompt(_base_state(ReportProfile.COMPACT))

    assert "**Kurzfazit**" in prompt
    assert "as extensive as question and evidence require" in prompt
    assert "600-1200 Woerter" not in prompt
    assert "Prioritise complete, cleanly closed sections" in prompt
    assert "**Executive Summary**" not in prompt


def test_domain_filter_for_query_text_extracts_multiple_site_domains():
    domain_filter = _domain_filter_for_query_text(
        "site:reuters.com OR site:bloomberg.com Tesla CapEx",
        base_domain_filter=["-reddit.com"],
    )

    assert domain_filter == ["reuters.com", "bloomberg.com"]


def test_domain_filter_for_query_text_uses_base_filter_without_site_operator():
    domain_filter = _domain_filter_for_query_text(
        "Tesla CapEx news",
        base_domain_filter=["-reddit.com"],
    )

    assert domain_filter == ["-reddit.com"]


def test_domain_filter_for_query_text_has_no_default_blocklist():
    domain_filter = _domain_filter_for_query_text("Tesla CapEx news")

    assert domain_filter == []


def test_build_answer_system_prompt_deep_profile():
    prompt = build_answer_system_prompt(_base_state(ReportProfile.DEEP))

    assert "**Executive Summary**" in prompt
    assert "as extensive as question and evidence require" in prompt
    assert "1800-2400 Woerter" not in prompt
    assert "Prioritise complete, cleanly closed sections" in prompt
    assert "**Risiken / Unsicherheiten**" in prompt
    assert "no reliable evidence exists" in prompt


def test_build_answer_system_prompt_uses_runtime_managed_reference_sections():
    state = _base_state(ReportProfile.DEEP)
    state["evidence_overview"] = "RESEARCH RESULT R1\n[E1] Source"
    state["allowed_citations"] = ["https://example.com/report"]

    prompt = build_answer_system_prompt(state)

    assert "Create NO own reference, source or link section" in prompt
    assert "[E1] [E2]" in prompt
    assert "Am Ende der Antwort fuege eine Quellenleiste ein" not in prompt


def test_build_answer_system_prompt_includes_evidence_overview_contract():
    state = _base_state(ReportProfile.COMPACT)
    state["allowed_citations"] = ["https://example.com/report", "https://example.com/check"]
    state["evidence_overview"] = (
        "RESEARCH RESULT R1\n"
        "[E1] Meta Investor Relations\n"
        "  URL: https://example.com/report\n"
        f"  Date: 2026-05-01 | Tier: primary | Standing: {STANDING_TEXT['cross-checked']}\n"
        "  Statements from this source:\n"
        "  - Meta expects 115-135 USD billion capex."
    )

    prompt = build_answer_system_prompt(state)

    assert "EVIDENCE OVERVIEW" in prompt
    for text in STANDING_TEXT.values():
        assert f"`{text}`" in prompt
    # Citation rule: bare [E#] label, no URL (URLs are attached post-synthesis).
    assert "[E12]" in prompt
    assert "[E12](URL)" not in prompt
    assert "RESEARCH RESULT R1" in prompt


def test_build_answer_system_prompt_hint_when_no_evidence_overview():
    state = _base_state(ReportProfile.COMPACT)
    state["evidence_overview"] = ""
    state["allowed_citations"] = ["https://example.com/report"]
    state["claim_status_counts"] = {"verified": 0, "contested": 0, "unverified": 0}
    state["source_tier_counts"] = {"primary": 1, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0}

    prompt = build_answer_system_prompt(state)

    assert "NOTE ON THE EVIDENCE" in prompt
    assert "source-attributed" in prompt


def test_additional_links_skip_low_quality_domains():
    class _LowMarkingTiering(DefaultSourceTiering):
        def tier_for_url(self, url: str) -> str:
            if "aitoolsrecap.com" in url or "toolscompare.ai" in url:
                return "low"
            return super().tier_for_url(url)

    strategies = SimpleNamespace(source_tiering=_LowMarkingTiering())

    links = _select_additional_links(
        [
            "https://aitoolsrecap.com/Blog/ai-news-may-2026",
            "https://openai.com/index/introducing-gpt-5-5",
            "https://toolscompare.ai/news/may-2026",
            "https://techcrunch.com/2026/05/05/openai-releases-gpt-5-5-instant-a-new-default-model-for-chatgpt",
        ],
        excluded_urls=set(),
        prompt_citation_urls=set(),
        strategies=strategies,
    )

    assert "https://openai.com/index/introducing-gpt-5-5" in links
    assert "https://techcrunch.com/2026/05/05/openai-releases-gpt-5-5-instant-a-new-default-model-for-chatgpt" in links
    assert all("aitoolsrecap.com" not in url for url in links)
    assert all("toolscompare.ai" not in url for url in links)


def test_answer_prompt_explains_evidence_overview_beleglage_levels():
    state = _base_state(ReportProfile.COMPACT)
    state["allowed_citations"] = ["https://example.com/report"]
    state["evidence_overview"] = (
        "RESEARCH RESULT R1\n"
        "[E1] Source\n"
        "  URL: https://example.com/report\n"
        f"  Date: 2026-05-01 | Tier: primary | Standing: {STANDING_TEXT['source-context']}"
    )

    prompt = build_answer_system_prompt(state)

    assert f"`{STANDING_TEXT['cross-checked']}` and `{STANDING_TEXT['primary-source']}`" in prompt
    assert f"`{STANDING_TEXT['single-source verified']}`" in prompt
    assert f"`{STANDING_TEXT['contested']}`" in prompt
    assert f"`{STANDING_TEXT['source-context']}`" in prompt
    assert "attribute inline" in prompt


def test_answer_prompt_tells_model_to_use_per_source_substance():
    state = _base_state(ReportProfile.COMPACT)
    state["allowed_citations"] = ["https://example.com/report"]
    state["evidence_overview"] = (
        "RESEARCH RESULT R1\n"
        "[E1] Source\n"
        "  Statements from this source:\n"
        "  - 115-135 USD billion capex expected."
    )

    prompt = build_answer_system_prompt(state)

    assert "concrete statements and excerpts" in prompt
    assert "not only titles" in prompt
    assert "search answer of a research result is the search provider's coherent answer" in prompt
    assert "do not count it as another independent piece of evidence" in prompt


@pytest.mark.parametrize("profile", list(ReportProfile))
@pytest.mark.parametrize("section", [False, True])
def test_answer_prompt_keeps_source_labels_separate_from_semantic_support(profile, section):
    state = _base_state(profile)
    state["allowed_citations"] = ["https://example.com/report"]
    state["evidence_overview"] = f"[E1] Standing: {STANDING_TEXT['primary-source']}"
    prompt = build_answer_section_system_prompt(
        state, heading="Empfehlung", instruction="Ordne die Befunde ein.",
        length_guidance="Kurz", section_position=1, section_total=1,
    ) if section else build_answer_system_prompt(state)

    assert "als gesicherte Fakten verwendbar" not in prompt
    assert "do not automatically confirm the statement in the finished sentence" in prompt
    assert "expressly carries that attribution" in prompt
    assert "alternatives that change a recommendation belong directly to that recommendation" in prompt


def test_section_prompt_contains_report_so_far_and_used_evidence_labels():
    prompt = build_answer_section_user_prompt(
        "Was ist passiert?",
        heading="Analyse",
        instruction="Vertiefe die Fakten.",
        completed_headings=["Executive Summary"],
        report_so_far_summary="Executive Summary: Kurzbefund.",
        used_evidence_labels=["E1", "E2"],
    )

    assert "Report summary so far:" in prompt
    assert "Executive Summary: Kurzbefund." in prompt
    assert "Evidence labels already used:" in prompt
    assert "E1, E2" in prompt


def test_build_answer_section_system_prompt_reuses_common_rules():
    state = _base_state(ReportProfile.DEEP)
    state["evidence_overview"] = "RESEARCH RESULT R1\n[E1] Source"
    state["allowed_citations"] = ["https://example.com/report"]

    prompt = build_answer_section_system_prompt(
        state,
        heading="Analyse",
        instruction="Schreibe sinnvolle Unterabschnitte mit Zahlen und Zusammenhaengen.",
        length_guidance="Tiefe nach Evidenzlage mit `###`-Unterabschnitten",
        section_position=3,
        section_total=6,
    )

    assert "You write ONLY ONE section" in prompt
    assert "Do NOT add the main heading `## Analyse` yourself" in prompt
    assert "Create NO own reference, source or link section" in prompt
    assert "CLAIM CALIBRATION" not in prompt


def _weak_evidence_state(report_profile: ReportProfile) -> dict[str, object]:
    """State where TRANSPARENCY OBLIGATION and citation rules should fire."""
    state = _base_state(report_profile)
    state["claim_status_counts"] = {"verified": 1, "contested": 0, "unverified": 5}
    state["claim_needs_primary_total"] = 3
    state["claim_needs_primary_supported"] = 1
    state["source_tier_counts"] = {"primary": 1, "mainstream": 1, "stakeholder": 0, "unknown": 2, "low": 0}
    state["evidence_overview"] = "RESEARCH RESULT R1\n[E1] Source"
    state["allowed_citations"] = ["https://example.com/a", "https://example.com/b"]
    state["required_aspects"] = ["Status quo", "Position der Akteure"]
    state["uncovered_aspects"] = ["Position der Akteure"]
    return state


def test_section_executive_summary_omits_unsicherheiten_subsection_directive():
    """Executive Summary must NOT be told to add a '## Unsicherheiten' block.

    Regression: a global TRANSPARENCY OBLIGATION block injecting this
    directive into every section would make the Executive Summary section
    add an unsolicited uncertainty bullet list where it does not belong.
    """
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Executive Summary",
        instruction="Beantworte die Frage direkt.",
        length_guidance="knapp und dicht, keine Bulletpoints",
        section_position=1,
        section_total=6,
    )

    # The transparency block itself still appears (status info is useful)
    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" in prompt
    # but the structural directive is suppressed for this section
    assert "Add a section '" not in prompt
    # and the suppression note is present
    assert "Add NO own uncertainty block in THIS section" in prompt


def test_section_risiken_keeps_unsicherheiten_subsection_directive():
    """The Risiken / Unsicherheiten section is the legitimate host."""
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Risiken / Unsicherheiten",
        instruction="Liste praezise Bulletpoints zu Evidenzgrenzen.",
        length_guidance="nur echte Risiken und Unsicherheiten",
        section_position=5,
        section_total=6,
        owns_evidence_limits=True,
    )

    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" in prompt
    assert "Add a section '### Unsicherheiten / Offene Punkte'" in prompt
    assert "Add NO own uncertainty block in THIS section" not in prompt


def test_section_fazit_does_not_host_unsicherheiten_block():
    """The Fazit section in DEEP must NOT host the transparency sub-block.

    Regression: the section after 'Risiken / Unsicherheiten' would otherwise
    duplicate the block, producing two '## Unsicherheiten / Offene Punkte'
    sub-headings in the rendered answer.
    """
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Fazit / Ausblick",
        instruction="Ziehe eine belastbare Gesamteinordnung.",
        length_guidance="kurz, klar und abgeschlossen",
        section_position=6,
        section_total=6,
    )

    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" in prompt
    assert "Add a section '" not in prompt
    assert "Add NO own uncertainty block in THIS section" in prompt


def test_section_einordnung_compact_hosts_unsicherheiten_block():
    """In COMPACT (no Risiken section) the closing 'Einordnung / Ausblick' hosts it."""
    state = _weak_evidence_state(ReportProfile.COMPACT)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Einordnung / Ausblick",
        instruction="Ordne die Befunde knapp ein.",
        length_guidance="kurz und abgeschlossen",
        section_position=4,
        section_total=4,
        owns_evidence_limits=True,
    )

    assert "Add a section '### Unsicherheiten / Offene Punkte'" in prompt


def test_monolithic_mode_keeps_legacy_unsicherheiten_directive():
    """Backwards compatibility: the monolithic prompt path is unchanged."""
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_system_prompt(state)

    assert "Add a section '## Unsicherheiten / Offene Punkte'" in prompt


def test_section_citation_rules_use_per_section_wording():
    """In section mode the citation rules use generic per-section wording."""
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Hintergrund / Kontext",
        instruction="Beschreibe Ausgangspunkt und zeitlichen Rahmen.",
        length_guidance="kompakt, aber mit den noetigen Kontextankern",
        section_position=2,
        section_total=6,
    )

    assert "CITATION RULES" in prompt
    assert "In this section" in prompt


def test_monolithic_citation_rules_use_global_wording():
    """Monolithic mode uses the report-wide citation wording."""
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_system_prompt(state)

    assert "CITATION RULES" in prompt
    assert "In the key points and the detailed sections" in prompt


def test_knowledge_report_headings_do_not_expose_prompt_instructions():
    prompt = build_knowledge_answer_prompt(
        "Welche Regel gilt?",
        "[K1] Regeltext",
        language="de",
        report=True,
    )

    assert prompt.startswith("Antworte IMMER auf Deutsch")
    assert "## Kurzfazit\n" in prompt
    assert "## Kernaussagen\n" in prompt
    assert "## Detailanalyse\n" in prompt
    assert "## Quellenlage\n" in prompt
    assert "## Kurzfazit (" not in prompt
    assert "## Kernaussagen (" not in prompt
    english = build_knowledge_answer_prompt(
        "Which rule applies?", "[K1] Rule text", language="en", report=True,
    )
    assert english.startswith("ALWAYS respond in English")
    for heading in ("## Summary\n", "## Key points\n", "## Detailed analysis\n", "## Evidence base\n"):
        assert heading in english
    assert "Kurzfazit" not in english
    assert OUTPUT_FORMAT_BLOCK in english


def test_knowledge_prompts_are_english_with_language_rules():
    from inqtrix.prompts import (
        build_chunk_context_prompt,
        build_knowledge_decompose_prompt,
        build_knowledge_followup_context_prompt,
        build_knowledge_gate_prompt,
        build_knowledge_rerank_prompt,
    )

    context = build_chunk_context_prompt("Titel", "Text", ["chunk"], is_excerpt=True)
    assert "in the language of the document" in context
    assert "DOCUMENT EXCERPT (title: Titel)" in context and "Note: the excerpt comes from a longer document" in context
    gate = build_knowledge_gate_prompt("Frage?", "[K1] text", vocabulary_bridge=True)
    assert "in the language of the question" in gate and "technical language of the documents" in gate
    assert "Reply EXCLUSIVELY with a JSON object" in gate
    decompose = build_knowledge_decompose_prompt("Frage?", max_sub_queries=3)
    assert "JSON array of sub-questions in the language of the question" in decompose and "2 to 3" in decompose
    followup = build_knowledge_followup_context_prompt("Und davon?", "User: A")
    assert "Keep the language of the current question" in followup
    rerank = build_knowledge_rerank_prompt("q", ["a", "b"])
    assert "List ALL 2 numbers exactly once" in rerank
    grounded = build_knowledge_answer_prompt(
        "Frage?", "[K1] text", language="de", grounding=True, unverified_quotes=("falsch",),
    )
    assert "QUOTES:" in grounded and "ANSWER:" in grounded and "CORRECTION:" in grounded
    for prompt in (context, gate, decompose, followup, rerank):
        assert "Antworte" not in prompt and "Teilfragen" not in prompt and "ZITATE" not in prompt
    # The grounded answer prompt opens with the German directive; its body is English.
    body = grounded.split("\n", 1)[1]
    assert "Antworte" not in body and "ZITATE" not in body and "Teilfragen" not in body


CHUNK_CONTEXT_WORDING = {
    "3": "4e3942d71d70472fcd70b7102119f385f358e4defdcab9f57f037e69e3fa0f6c",
}
"""The chunk-context prompt's wording per version. Ingestion stamps the
version on every document so an operator can tell which prompt situated a
collection's chunks; a wording change under an old number makes that stamp
lie. Changing the prompt means a new version and a new line here."""


def test_the_chunk_context_prompt_changes_only_with_its_version():
    import hashlib

    from inqtrix.prompts import CHUNK_CONTEXT_PROMPT_VERSION, build_chunk_context_prompt

    wording = (
        build_chunk_context_prompt("Titel", "Text", ["eins", "zwei"])
        + "\x00"
        + build_chunk_context_prompt("Titel", "Text", ["eins"], is_excerpt=True)
    )
    assert hashlib.sha256(wording.encode()).hexdigest() == CHUNK_CONTEXT_WORDING.get(
        CHUNK_CONTEXT_PROMPT_VERSION
    ), "the chunk-context prompt changed: bump CHUNK_CONTEXT_PROMPT_VERSION"


def test_section_abdeckungsregel_has_inline_marker_for_non_risiken_section():
    """The coverage rule must not push the LLM to spawn a risks sub-block in
    a section that does not own the evidence limits."""
    state = _weak_evidence_state(ReportProfile.DEEP)

    prompt = build_answer_section_system_prompt(
        state,
        heading="Analyse",
        instruction="Vertiefe Kernaspekte.",
        length_guidance="Tiefe nach Evidenzlage statt Wortlimit",
        section_position=3,
        section_total=6,
    )

    assert "SUB-QUESTION COVERAGE" in prompt
    assert "mark them briefly inline with '(unbestätigt)'" in prompt
    # The "name them under the risks heading" directive must not appear in
    # this section because it does not own the evidence limits.
    assert "name them transparently under" not in prompt


def test_language_directive_is_in_target_language_for_english():
    """Regression: 'Antworte auf Englisch' alone gets ignored by the LLM
    because all other instructions in the system prompt are in German.
    The directive must therefore be repeated in the target language.
    """
    state = _base_state(ReportProfile.COMPACT)
    state["answer_lang"] = "Englisch"

    prompt = build_answer_system_prompt(state)

    assert "ALWAYS respond in English" in prompt
    # The legacy German-only directive must NOT appear when the answer
    # language is English — otherwise the LLM gets a mixed signal.
    assert "Antworte IMMER auf Englisch" not in prompt


def test_language_directive_keeps_german_when_answer_lang_is_deutsch():
    """Default German answer keeps the German wording."""
    state = _base_state(ReportProfile.COMPACT)
    state["answer_lang"] = "Deutsch"

    prompt = build_answer_system_prompt(state)

    assert "Antworte IMMER auf Deutsch" in prompt
    # Without it, research reports can come back in ASCII German ("fuer",
    # "Koenigreich"); the directive names the umlauts.
    assert "mit Umlauten (ä, ö, ü, ß), nie als ae/oe/ue/ss" in prompt
    assert "ALWAYS respond in English" not in prompt


def test_language_directive_for_french():
    state = _base_state(ReportProfile.COMPACT)
    state["answer_lang"] = "Franzoesisch"

    prompt = build_answer_system_prompt(state)

    assert "Réponds TOUJOURS en français" in prompt


def test_language_directive_falls_back_for_unknown_language():
    """Unknown language: an English directive names the supplied language."""
    state = _base_state(ReportProfile.COMPACT)
    state["answer_lang"] = "Esperanto"

    prompt = build_answer_system_prompt(state)

    assert "ALWAYS respond in Esperanto" in prompt
    assert "Antworte IMMER auf Esperanto" not in prompt


def test_language_directive_applies_to_section_prompt_too():
    """The section composer must also enforce the target language."""
    state = _base_state(ReportProfile.DEEP)
    state["answer_lang"] = "Englisch"

    prompt = build_answer_section_system_prompt(
        state,
        heading="Executive Summary",
        instruction="Beantworte die Frage direkt.",
        length_guidance="knapp und dicht",
        section_position=1,
        section_total=6,
    )

    assert "ALWAYS respond in English" in prompt


def test_deep_profile_first_round_queries_are_bounded_to_six():
    """Deep starts with the same six-query wave as COMPACT; depth is rounds."""
    overrides = tuning_for_report_profile(ReportProfile.DEEP).settings_overrides
    assert overrides.get("first_round_queries") == 6


def test_report_profiles_do_not_carry_provider_token_budgets():
    """Provider output budgets belong to provider constructors, not profiles."""
    compact = tuning_for_report_profile(ReportProfile.COMPACT)
    deep = tuning_for_report_profile(ReportProfile.DEEP)

    assert not hasattr(compact, "default_max_tokens")
    assert not hasattr(deep, "default_max_tokens")
    assert all(not hasattr(s, "max_output_tokens") for s in compact.answer_sections)
    assert all(not hasattr(s, "max_output_tokens") for s in deep.answer_sections)


def test_report_profiles_do_not_apply_hidden_answer_citation_caps():
    compact = tuning_for_report_profile(ReportProfile.COMPACT)
    deep = tuning_for_report_profile(ReportProfile.DEEP)

    assert not hasattr(compact, "answer_body_citation_cap")
    assert not hasattr(deep, "answer_body_citation_cap")
    assert not hasattr(compact, "answer_citation_block_char_budget")
    assert not hasattr(deep, "answer_citation_block_char_budget")


def test_deep_profile_keeps_broader_pre_report_evidence_view():
    deep = tuning_for_report_profile(ReportProfile.DEEP)

    assert deep.claim_max_items == 24
    assert deep.materialize_max_unverified == deep.materialize_max_total


def test_custom_section_plan_assigns_disclosure_without_heading_inference():
    sections = (
        AnswerSectionSpec("Summary", "Summarise", "short", write_last=True),
        AnswerSectionSpec("Findings", "Explain", "as needed"),
        AnswerSectionSpec("Context", "Explain", "as needed"),
    )
    assert evidence_limits_section_index(sections) == 3
    assert evidence_limits_section_index(sections[:1]) == 1
    assert evidence_limits_section_index(()) is None


def test_ambiguous_disclosure_ownership_is_rejected():
    with pytest.raises(ValueError, match="Only one answer section"):
        evidence_limits_section_index((
            AnswerSectionSpec("A", "Explain", "short", owns_evidence_limits=True),
            AnswerSectionSpec("B", "Explain", "short", owns_evidence_limits=True),
        ))


@pytest.mark.parametrize("evaluation", [
    {"coverage_source": "llm", "uncovered_aspects": ["Applicability"]},
    {"evaluate_status": "insufficient"},
])
def test_verified_claims_do_not_hide_unresolved_questions(evaluation):
    state = _base_state(ReportProfile.SCHNELL)
    state["claim_status_counts"] = {"verified": 12, "contested": 0, "unverified": 0}
    state.update(evaluation)
    prompt = build_answer_system_prompt(state)
    assert "Add a section '## Unsicherheiten / Offene Punkte'" in prompt


def test_attributed_support_is_support_not_weak_evidence():
    """A provider without source text attributes every supported claim;
    that must not read as more unsupported than supported claims (the
    transparency block), while the writer still gets the attribution rule."""
    state = _base_state(ReportProfile.SCHNELL)
    state.update(
        claim_status_counts={"verified": 0, "attributed": 12, "contested": 0, "unverified": 3},
        coverage_source="llm", uncovered_aspects=[], evaluate_status="sufficient",
    )
    prompt = build_answer_system_prompt(state)
    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" not in prompt
    assert "Claim status (verified/attributed/contested/unverified): 0/12/0/3" in prompt
    assert "`attributed` statements are what the search provider's answer" in prompt
    assert "never as the source's own wording or as 'the regulation text says'" in prompt


def test_complete_evidence_does_not_require_an_uncertainty_block():
    state = _base_state(ReportProfile.SCHNELL)
    state.update(
        claim_status_counts={"verified": 12, "contested": 0, "unverified": 0},
        coverage_source="llm", uncovered_aspects=[], evaluate_status="sufficient",
    )
    prompt = build_answer_system_prompt(state)
    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" not in prompt


@pytest.mark.parametrize("prompt", [
    build_claim_extraction_prompt(),
    build_answer_system_prompt(_base_state(ReportProfile.SCHNELL)),
], ids=["extraction", "answer"])
def test_research_prompts_preserve_conditions_scope_and_current_status(prompt):
    # Literal requirements pin the consumer's prompt; an empty shared helper
    # must not turn this check green by construction.
    assert "Preserve the conditions, scope and time reference" in prompt
    assert "A conditional statement is not a finding that its conditions are met." in prompt
    assert "A rule or a deadline is no proof of a current state." in prompt
    assert "Source roles, assessment labels and valid citations do not replace substantive support." in prompt
    assert "Keep the trigger of a deadline and the concrete scope of a rule." in prompt
    assert "Distinguish the actual speaker, the publisher and the document status." in prompt
    assert "A search provider's answer is a synthesis, not original page text." in prompt
    assert "Run-internal evidence notes rest on run data, not on external sources." in prompt


@pytest.mark.parametrize("section_mode", [False, True])
def test_answers_without_evidence_explicitly_forbid_invented_citations(section_mode):
    def build(state):
        if not section_mode:
            return build_answer_system_prompt(state)
        return build_answer_section_system_prompt(
            state, heading="Summary", instruction="Summarise with evidence labels.",
            length_guidance="short", section_position=1, section_total=2,
        )

    empty = _base_state(ReportProfile.SCHNELL)
    assert "There are no citable sources." in build(empty)
    assert "Create no source labels, source URLs or apparent evidence." in build(empty)
    sourced = {**empty, "evidence_overview": "[E1] A supported statement.",
               "allowed_citations": ["https://source.example/report"]}
    assert "There are no citable sources." not in build(sourced)
    assert "Invent NO URLs and NO labels" in build(sourced)


# --- Language-resolved headings and markers ---------------------------------


def test_full_answer_style_lists_the_profile_sections_in_the_answer_language():
    """The monolithic style block is derived from the profile's section plan
    and names the headings in the answer's language — no second copy of
    the plan lives in the prompt text."""
    german = build_answer_system_prompt({**_base_state(ReportProfile.DEEP), "answer_lang": "de"})
    english = build_answer_system_prompt({**_base_state(ReportProfile.DEEP), "answer_lang": "en"})
    for heading in ("Executive Summary", "Hintergrund / Kontext", "Risiken / Unsicherheiten", "Fazit / Ausblick"):
        assert f"**{heading}** (## heading)" in german
    for heading in ("Executive summary", "Background and context", "Risks and uncertainties", "Conclusion and outlook"):
        assert f"**{heading}** (## heading)" in english
    assert "Hintergrund" not in english
    compact = build_answer_system_prompt({**_base_state(ReportProfile.COMPACT), "answer_lang": "en"})
    assert "**Summary** (## heading)" in compact and "**Key points** (## heading)" in compact
    assert "Executive summary" not in compact


def test_uncertainty_heading_and_marker_follow_the_answer_language():
    state = _weak_evidence_state(ReportProfile.DEEP)
    state["answer_lang"] = "en"
    owner = build_answer_section_system_prompt(
        state, heading="Risks and uncertainties", instruction="List the limits.",
        length_guidance="short", section_position=5, section_total=6, owns_evidence_limits=True,
    )
    assert "Add a section '### Uncertainties and open points'" in owner
    assert "'Risks and uncertainties' or 'Uncertainties and open points'" in owner
    assert "(unconfirmed)" in owner and "(unbestätigt)" not in owner
    other = build_answer_section_system_prompt(
        state, heading="Analysis", instruction="Deepen.", length_guidance="as needed",
        section_position=3, section_total=6,
    )
    assert "mark them briefly inline with '(unconfirmed)'" in other
    german = build_answer_system_prompt({**state, "answer_lang": "de"})
    assert "Add a section '## Unsicherheiten / Offene Punkte'" in german
    assert "'(unbestätigt)'" in german and "unbestaetigt" not in german


def test_answer_prompt_opens_with_the_directive_and_carries_the_format_block():
    prompt = build_answer_system_prompt({**_base_state(ReportProfile.COMPACT), "answer_lang": "en"})
    assert prompt.startswith("ALWAYS respond in English")
    assert "Today's date: 2026-04-13." in prompt
    assert OUTPUT_FORMAT_BLOCK in prompt
    assert "Trennlinien" not in prompt and "horizontal rules" in OUTPUT_FORMAT_BLOCK
    section = build_answer_section_system_prompt(
        {**_base_state(ReportProfile.COMPACT), "answer_lang": "de"},
        heading="Kernaussagen", instruction="Fakten.", length_guidance="kurz",
        section_position=2, section_total=4,
    )
    assert section.startswith("Antworte IMMER auf Deutsch")
    assert OUTPUT_FORMAT_BLOCK in section


def test_unresolved_answer_language_gets_the_question_language_directive():
    """No silent default: without a resolved code the directive asks for the
    question's language and the headings fall back to English."""
    state = _base_state(ReportProfile.COMPACT)
    state["answer_lang"] = None
    prompt = build_answer_system_prompt(state)
    assert prompt.startswith(QUESTION_LANGUAGE_DIRECTIVE)
    assert "Antworte IMMER auf Deutsch" not in prompt
    assert "**Summary** (## heading)" in prompt


@pytest.mark.parametrize("section_mode", [False, True])
def test_requested_elements_are_produced_not_described(section_mode):
    """A report asked for a flowchart and a role table must produce them,
    not write what the diagram and the table 'should' show. The style
    block ranks an explicit request above the section's form guidance."""
    state = _base_state(ReportProfile.SCHNELL)
    prompt = (
        build_answer_section_system_prompt(
            state, heading="Kernaussagen", instruction="Bullet points.",
            length_guidance="short", section_position=2, section_total=2,
        )
        if section_mode
        else build_answer_system_prompt(state)
    )
    assert "produce that element in the section where it belongs instead of describing" in prompt
    assert "does not override such a request" in prompt


@pytest.mark.parametrize("profile", list(ReportProfile))
def test_summary_sections_write_about_the_subject_not_the_report(profile):
    """Write-last summaries state the findings instead of describing "the
    completed comparison table" or "the added flow diagram"."""
    sections = tuning_for_report_profile(profile).answer_sections
    summaries = [spec for spec in sections if spec.write_last]
    assert summaries
    for spec in summaries:
        assert "never describe what its sections, tables or diagrams contain" in spec.prompt_instruction
