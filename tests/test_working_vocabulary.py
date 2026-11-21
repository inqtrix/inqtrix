"""The writer's working vocabulary stays out of the report.

A model quotes the short labels it reads in its prompt into the report
('provider-attributed Angaben'). The standings therefore reach the writer and
the evaluator as plain words, while the tokens stay the internal vocabulary
of ranking, audit, API and forensics. A passage lands under the same heading
in the full and the compact view, and the German system texts of the report
and of the research steps carry no English pipeline labels or internal codes.
"""

import re

import pytest

from inqtrix.evidence import STANDING_TEXT, _normalized_text_key, _render_source_record_block
from inqtrix.i18n import MESSAGES
from inqtrix.prompts import build_answer_section_system_prompt

LABEL_TOKENS = (
    "cross-checked",
    "primary-source",
    "single-source verified",
    "provider-attributed",
    "source-context",
)


def _claim(text: str, *, basis: str, status: str, checked: bool) -> dict:
    return {
        "raw_claim_id": f"raw_{basis}_{status}",
        "claim_text": text,
        "evidence_snippet": "",
        "verification_basis": basis,
        "verification_status": status,
        "checked_here": checked,
    }


def _record(claims: list[dict], *, passages: tuple[dict, ...] = ()) -> dict:
    return {
        "evidence_id": "ev_1",
        "query_id": "q1",
        "canonical_url": "https://www.a-news.de/x",
        "tier": "mainstream",
        "tier_reason": "",
        "source_title": "A News",
        "source_snippet": "",
        "source_passages": list(passages),
        "claims": claims,
        "report_eligible": True,
    }


CLAIM = "Die Frist endet am 2. August 2026."

STANDING_CASES = {
    "cross-checked": [_claim(CLAIM, basis="cross_checked", status="verified", checked=True)],
    "primary-source": [_claim(CLAIM, basis="primary_source", status="verified", checked=True)],
    "single-source verified": [_claim(CLAIM, basis="quality_source", status="verified", checked=True)],
    "provider-attributed": [_claim(CLAIM, basis="primary_source", status="attributed", checked=False)],
    "contested": [_claim(CLAIM, basis="contested", status="contested", checked=False)],
    "unverified": [_claim(CLAIM, basis="weak_evidence", status="unverified", checked=False)],
    "source-context": [],
}


def _block(record: dict, *, compact: bool = False, seen: set[str] | None = None) -> str:
    return _render_source_record_block(
        [record], "E1", max_chars=4000, seen_texts=set() if seen is None else seen, compact=compact,
    )


@pytest.mark.parametrize("token", sorted(STANDING_CASES))
def test_every_standing_reaches_the_model_as_plain_words(token):
    block = _block(_record(STANDING_CASES[token]))

    assert f"Standing: {STANDING_TEXT[token]}" in block
    assert f"Standing: {token}" not in block
    assert not any(label in block for label in LABEL_TOKENS)


def test_the_plain_words_cover_every_standing_and_quote_no_label():
    assert set(STANDING_TEXT) == set(STANDING_CASES)
    assert len(set(STANDING_TEXT.values())) == len(STANDING_TEXT)
    for text in STANDING_TEXT.values():
        assert not any(label in text for label in LABEL_TOKENS)


@pytest.mark.parametrize(
    ("origin", "heading"),
    [
        ("claim_evidence_snippet", "Search-provider text these statements were taken from:"),
        ("source_snippet", "The search provider's excerpt of this source:"),
    ],
)
def test_the_compact_view_files_a_passage_where_the_full_view_does(origin, heading):
    # The record's statement was already shown under an earlier source of the
    # same search, so the compact view falls back to one passage of text.
    prose = "Nach Angaben des Anbieters gilt die Pflicht ab dem 31. Dezember 2027."
    record = _record(
        [_claim(CLAIM, basis="primary_source", status="attributed", checked=False)],
        passages=({"origin": origin, "text": prose},),
    )

    for compact in (False, True):
        block = _block(record, compact=compact, seen={_normalized_text_key(CLAIM)})
        assert f"{heading}\n  - {prose}" in block, (compact, block)
        other = {
            "Search-provider text these statements were taken from:",
            "The search provider's excerpt of this source:",
        } - {heading}
        assert not any(line in block for line in other), (compact, block)


DEPTH_GAP = {
    "active": True,
    "supported_count": 5,
    "cross_checked_count": 0,
    "single_source_count": 5,
    "single_source_ratio": 1.0,
}


def _prompt(**state_extra) -> str:
    """The system prompt of the key-points section of a compact report: the
    section that writes lists, where an attribution per item would repeat."""
    state = {
        "today_str": "2026-10-17",
        "answer_lang": "Deutsch",
        "evidence_overview": "RESEARCH RESULT R1\n[E1] A News\n  Standing: "
        + STANDING_TEXT["provider-attributed"],
        "allowed_citations": ["https://www.a-news.de/x"],
        "source_tier_counts": {},
        "claim_status_counts": {"verified": 0, "attributed": 5, "contested": 0, "unverified": 1},
    }
    state.update(state_extra)
    return build_answer_section_system_prompt(
        state,
        heading="Kernaussagen",
        instruction="Write substantial bullet points with the most important facts.",
        length_guidance="as many bullet points as the evidence carries",
        section_position=2,
        section_total=4,
    )


def _bullet(prompt: str, start: str) -> str:
    """The one prompt line that starts with *start*."""
    lines = [line for line in prompt.splitlines() if line.startswith(start)]
    assert len(lines) == 1, (start, lines)
    return lines[0]


def test_the_legend_explains_every_standing_in_plain_words():
    prompt = _prompt()

    for text in STANDING_TEXT.values():
        assert f"`{text}`" in prompt, text
    assert not any(label in prompt for label in LABEL_TOKENS)
    assert "provider synthesis" not in prompt


def test_the_writer_is_told_to_keep_the_working_terms_out_of_the_answer():
    prompt = _prompt()

    assert "never quote them as terms" in prompt
    assert "in plain words of the answer language what supports a statement" in prompt


# One attribution rule for the writer. The expected sentences are the rule's
# wording; a reword must be caught here, not by a looser substring.


def test_the_attribution_rule_is_stated_once():
    prompt = _prompt(evidence_depth_gap=DEPTH_GAP)

    assert prompt.count("ATTRIBUTION RULE (applies to every statement you attribute to a source):\n") == 1
    assert prompt.count("once per paragraph") == 1


def _prompt_without_overview(**state_extra) -> str:
    """A section prompt whose evidence overview rendered no source."""
    state = {"today_str": "2026-10-17", "answer_lang": "Deutsch", "evidence_overview": "", "allowed_citations": []}
    state.update(state_extra)
    return build_answer_section_system_prompt(
        state,
        heading="Kernaussagen",
        instruction="Write substantial bullet points with the most important facts.",
        length_guidance="as many bullet points as the evidence carries",
        section_position=2,
        section_total=4,
    )


@pytest.mark.parametrize(
    ("state_extra", "referring_block"),
    [
        ({"claim_status_counts": {"verified": 0, "attributed": 3, "contested": 0, "unverified": 0}},
         "CLAIM CALIBRATION (IMPORTANT):\n"),
        ({"evidence_depth_gap": DEPTH_GAP}, "EVIDENCE DEPTH -- IMPORTANT FOR THE TONE OF THE REPORT:\n"),
        ({"evaluate_status": "insufficient"}, "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE:\n"),
    ],
    ids=["claim_calibration", "evidence_depth", "transparency"],
)
def test_the_attribution_rule_stands_wherever_a_block_refers_to_it(state_extra, referring_block):
    """The evidence overview carries the rule; when it renders no source, a
    block that attributes by the rule still finds it in the prompt."""
    prompt = _prompt_without_overview(**state_extra)

    assert referring_block in prompt
    assert "EVIDENCE OVERVIEW" not in prompt
    assert prompt.count(
        "ATTRIBUTION RULE (applies to every statement you attribute to a source):\n"
        "- Name a source in plain words once per paragraph.\n"
    ) == 1


def test_a_prompt_without_a_block_that_refers_to_it_carries_no_attribution_rule():
    assert "ATTRIBUTION RULE" not in _prompt_without_overview()


def test_a_paragraph_names_its_source_once():
    prompt = _prompt()

    assert "- Name a source in plain words once per paragraph.\n" in prompt
    assert (
        "- Further statements of the same source in that paragraph, list or "
        "table carry only its label.\n"
    ) in prompt
    # The unit is the paragraph, never the single row or item.
    assert "table row or list item" not in prompt


def test_a_list_with_one_source_names_it_in_the_sentence_that_introduces_it():
    prompt = _prompt()

    assert "- In a list, name it once in the sentence that introduces the list;" in prompt


def test_a_list_with_two_sources_names_the_one_its_introduction_omits_at_its_first_item():
    """Two sources in one list: the introduction may name both; a source it
    does not name is named where the list first uses it, then only labelled."""
    prompt = _prompt()

    assert (
        "- When that introduction does not name a source, name it at the first "
        "item or row that uses it.\n"
    ) in prompt


def test_a_table_names_its_source_once_in_its_introduction():
    prompt = _prompt()

    assert "in a table, once in the sentence that introduces the table.\n" in prompt


def test_dates_and_figures_always_carry_the_label():
    prompt = _prompt()

    assert "- Dates and figures always carry the label.\n" in prompt


@pytest.mark.parametrize("token", ["single-source verified", "provider-attributed", "source-context"])
def test_every_single_source_standing_follows_the_one_rule(token):
    line = _bullet(_prompt(), f"- `{STANDING_TEXT[token]}`:")

    assert "ATTRIBUTION RULE" in line
    assert "inline" not in line
    assert "once per paragraph" not in line


def test_claim_calibration_and_transparency_attribute_by_the_one_rule():
    prompt = _prompt(
        claim_status_counts={"verified": 0, "attributed": 1, "contested": 0, "unverified": 5},
        claim_needs_primary_total=2,
        claim_needs_primary_supported=0,
    )

    assert "TRANSPARENCY OBLIGATION WITH UNCERTAIN EVIDENCE" in prompt
    assert "Attribute them by the ATTRIBUTION RULE, never as the source's own wording" in _bullet(
        prompt, "- `attributed` statements are"
    )
    assert "Write them with inline attribution" not in prompt
    assert "their attribution follows the ATTRIBUTION RULE" in _bullet(
        prompt, "- When claims requiring a primary source are not supported"
    )
    # Only contested and secondarily supported figures get a mark of their own.
    assert _bullet(prompt, "- Mark contested") == (
        "- Mark contested and only secondarily supported figures as such "
        "('contested', 'not conclusively supported by a primary source'); every "
        "other statement follows the ATTRIBUTION RULE without such a mark."
    )
    assert "'according to source X'" not in prompt


def test_the_depth_block_refers_to_the_one_rule_instead_of_a_cautious_verb():
    prompt = _prompt(evidence_depth_gap=DEPTH_GAP)

    assert "EVIDENCE DEPTH -- IMPORTANT FOR THE TONE OF THE REPORT:\n" in prompt
    assert "- Attribute statements from a single source by the ATTRIBUTION RULE.\n" in prompt
    assert "MUST be attributed inline" not in prompt
    assert "'reports', 'states', 'is said to'" not in prompt
    # The single-source caution stays.
    assert (
        "- Do NOT present figures, actor attributions and causal statements "
        "that stem from a single source as established fact.\n"
    ) in prompt


def test_the_depth_block_names_no_label():
    prompt = _prompt(evidence_depth_gap=DEPTH_GAP)

    assert "EVIDENCE DEPTH" in prompt
    assert not any(label in prompt for label in LABEL_TOKENS)


# The search provider's excerpt has one name in the writer's and the
# evaluator's input, in the result and in the Research Desk, and no text says
# that a page was read.
EXCERPT = "the search provider's excerpt"
PAGE_WORDS = ("own text", "source's text", "text from the page", "Quelltext", "page snippet")


def test_the_excerpt_has_one_name_in_the_writers_input():
    prompt = _prompt(
        claim_status_counts={"verified": 2, "attributed": 1, "contested": 0, "unverified": 0},
        evidence_depth_gap=DEPTH_GAP,
    )
    excerpt_lines = [
        _bullet(prompt, "- Treat `verified` statements"),
        _bullet(prompt, "- `attributed` statements are"),
        _bullet(prompt, "- Treat only independently confirmed statements"),
        _bullet(prompt, "- Statements under 'checked against the search provider's excerpt'"),
    ]

    for line in excerpt_lines:
        assert EXCERPT in line, line
    for token in ("cross-checked", "primary-source", "single-source verified"):
        assert STANDING_TEXT[token].startswith(f"checked against {EXCERPT};"), token
    assert not [word for word in PAGE_WORDS if word in prompt]


def test_the_excerpt_has_one_name_in_the_source_block():
    record = _record(
        [_claim(CLAIM, basis="quality_source", status="verified", checked=True)],
        passages=({"origin": "source_snippet", "text": "Die Frist endet am 2. August 2026 laut Amt."},),
    )

    block = _block(record)

    assert f"Statements checked against {EXCERPT} of this source:\n  - {CLAIM}" in block
    assert "The search provider's excerpt of this source:\n  - Die Frist" in block
    assert not [word for word in PAGE_WORDS if word in block]


def test_the_excerpt_has_one_name_in_the_result():
    from inqtrix.result import Claim, ReportReference, ReportReferenceStatement, ResearchMetrics

    descriptions = {
        "Claim.status": Claim.model_fields["status"].description,
        "ReportReference.statements": ReportReference.model_fields["statements"].description,
        "ReportReference.provider_snippet": ReportReference.model_fields["provider_snippet"].description,
        "ResearchMetrics.verified_claims_used_count": (
            ResearchMetrics.model_fields["verified_claims_used_count"].description
        ),
        "ResearchMetrics.evidence_basis": ResearchMetrics.model_fields["evidence_basis"].description,
        "ReportReferenceStatement.checked": _attribute_docstring(ReportReferenceStatement, "checked"),
    }

    for name, text in descriptions.items():
        assert EXCERPT in text.lower(), name
        assert not [word for word in PAGE_WORDS if word in text], name


def _attribute_docstring(model: type, attribute: str) -> str:
    """The docstring written under *attribute* in *model*'s source."""
    import ast
    import inspect
    import textwrap

    body = ast.parse(textwrap.dedent(inspect.getsource(model))).body[0].body
    for node, following in zip(body, body[1:]):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attribute
            and isinstance(following, ast.Expr)
            and isinstance(following.value, ast.Constant)
        ):
            return str(following.value.value)
    raise AssertionError(f"{model.__name__}.{attribute} has no docstring")


GERMAN_REPORT_TEXTS = (
    "answer_examined_intro",
    "answer_evidence_notice_source_context",
    "answer_evidence_notice_unbound",
    "answer_evidence_notice_citation_without_claim",
    "answer_evidence_notice_algorithm_failure",
    "answer_evidence_basis_provider_context",
    "answer_evidence_basis_evidence",
    "answer_evidence_basis_provider_only",
    "answer_evidence_depth_numbers",
    "answer_completeness_provider_stop",
    "answer_incomplete_reason_empty",
    "answer_incomplete_reason_open_bold",
    "answer_incomplete_reason_open_code",
    "answer_incomplete_reason_fragment",
    "answer_incomplete_reason_stop",
    "answer_incomplete_reason_missing_sections",
    "answer_stats_sources_one",
    "answer_stats_sources_other",
    "answer_stats_evidence_prompt_one",
    "answer_stats_evidence_prompt_other",
    "answer_stats_linked_one",
    "answer_stats_linked_other",
    "answer_stats_verified_one",
    "answer_stats_verified_other",
    "answer_stats_cross_checked_one",
    "answer_stats_cross_checked_other",
    "answer_stats_failed_steps_one",
    "answer_stats_failed_steps_other",
    "answer_stats_searches_one",
    "answer_stats_searches_other",
    "answer_stats_confidence",
    "research_algofail_heading",
    "research_algofail_intro",
    "research_algofail_run_id",
    "research_algofail_evidence_records",
    "research_algofail_claim_calls",
    "research_algofail_claim_failures",
    "research_algofail_blocking",
    "research_algofail_next_step",
    "research_algofail_next_step_text",
    "research_answer_fallback_warning",
    "research_answer_failed_fallback_failed_body",
    "research_answer_failed_no_fallback_body",
    "research_answer_timeout_reason",
    "research_answer_fallback_warning_no_material",
    "research_no_context_timeout",
    "research_no_context_provider",
    "sse_request_timeout",
)
# The research steps a reader follows in the live protocol.
GERMAN_STEP_TEXTS = (
    "search_quality_summary",
    "search_source_quality",
    "search_sources_processed",
    "search_claim_binding_issues",
    "claim_extract_failed",
    "claim_extract_empty",
    "retry_operation_claim_extraction",
    "plan_failed",
    "plan_query_generation_failed",
    "research_query_plan_not_json",
    "research_plan_query_fallback_reason",
    "research_strategy_deep_perspectives",
    "research_strategy_storm_perspectives",
    "research_strategy_crosscheck",
    "evaluate_quality_summary",
    "evaluate_confidence_missing",
    "evaluate_confidence_out_of_range",
    "evaluate_coverage_missing",
    "evaluate_evidence_scores_missing",
    "evaluate_failed",
    "evaluate_evidence_too_thin",
    "research_finished",
    "confidence_continue",
    "plateau_stop",
    "contradictions_severe",
    "falsification_released",
    "min_rounds_continue",
    "context_window_too_small",
    "compose_section_token_limit",
    "compose_section_truncation",
    "compose_aborted",
    "answer_section_start",
    "evidence_contract_needs_review",
    "evidence_contract_source_context_only",
    "algorithm_failure_report_blocked",
    "classify_failed",
    "provider_retry_observed",
    "provider_retry_attempt",
    "retry_operation_answer_synthesis",
    "node_model_unresolved",
    "answer_fallback_model",
    "answer_timeout_fallback",
    "answer_provider_failed_no_fallback",
    "answer_fallback_model_failed",
    "answer_fallback_outcome_material",
    "answer_fallback_outcome_no_material",
    "telemetry_retry_text",
)
ENGLISH_PIPELINE_TERMS = (
    "cross-checked",
    "single-source",
    "primary-source",
    "source-context",
    "Evidence-",
    "Hard-Fact",
    "Claim",
    "Ledger",
    "ALGO-FAIL",
    "Confidence",
    "Record",
    "Cross-Check",
    "STORM",
    "DEEP",
    "Report",
    "Fallback",
    "finish_reason",
    "_tokens",
    "min_rounds",
    "LLM",
    "Retry",
    "Defaults",
    "Timeout",
    "Provider",
)


def test_no_reader_text_says_mehrfach_belegt():
    """The word is kept for a later count of independent provider
    attributions. The footer and the search step name the cross-checked
    basis 'gegengeprüft', the line under the report counts statements with
    more than one source; neither borrows the word, nor does any other text."""
    found = [
        (key, lang)
        for key, entry in MESSAGES.items()
        for lang in ("de", "en")
        if "mehrfach beleg" in entry[lang].lower()
    ]

    assert found == []


def test_the_basis_line_and_the_notices_never_say_cross_checked():
    """The line under the report and the notices count something other than
    the cross-checked basis, so they must not borrow its name."""
    texts = [
        MESSAGES[key][lang]
        for key in GERMAN_REPORT_TEXTS
        if key.startswith(("answer_evidence_notice_", "answer_evidence_basis_", "answer_evidence_depth_"))
        for lang in ("de", "en")
    ]

    assert len(texts) == 2 * 8
    assert not [
        text
        for text in texts
        if "gegengeprüft" in text.lower() or "cross-checked" in text.lower()
    ]


FALLBACK_AND_RETRY_TEXTS = (
    "research_answer_failed_fallback_failed_body",
    "research_answer_failed_no_fallback_body",
    "research_answer_timeout_reason",
    "research_answer_fallback_warning",
    "research_answer_fallback_warning_no_material",
    "research_no_context_timeout",
    "research_no_context_provider",
    "answer_fallback_model",
    "answer_timeout_fallback",
    "answer_provider_failed_no_fallback",
    "answer_fallback_model_failed",
    "answer_fallback_outcome_material",
    "answer_fallback_outcome_no_material",
    "classify_failed",
    "provider_retry_observed",
    "provider_retry_attempt",
    "retry_operation_answer_synthesis",
    "node_model_unresolved",
)
# How the pipeline names its own steps; a reader is told what happened in
# plain words of the run's language instead.
INTERNAL_STEP_WORDS = (
    "synthes", "fallback", "llm", "kontextdaten", "context data", "provider-verbindung",
    "provider connection", "defaults", "retrys", "node '", "attribute",
)


@pytest.mark.parametrize("language", ["de", "en"])
@pytest.mark.parametrize("key", FALLBACK_AND_RETRY_TEXTS)
def test_the_fallback_answer_and_its_steps_name_no_internal_step(key, language):
    # Code spans are the names an operator looks up (a setting, the log
    # flag), not words for the reader.
    text = re.sub(r"\{\w+\}|`[^`]*`", "", MESSAGES[key][language]).lower()

    assert not [word for word in INTERNAL_STEP_WORDS if word in text], text


@pytest.mark.parametrize("key", GERMAN_REPORT_TEXTS + GERMAN_STEP_TEXTS)
def test_german_reader_texts_use_german_terms(key):
    # A placeholder name is no reader text; what fills it is checked where
    # it is written.
    german = re.sub(r"\{\w+\}", "", MESSAGES[key]["de"])

    assert not any(term in german for term in ENGLISH_PIPELINE_TERMS), german
