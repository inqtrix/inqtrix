"""The writer's working vocabulary stays out of the report.

A model quotes the short labels it reads in its prompt into the report
('provider-attributed Angaben'). The standings therefore reach the writer and
the evaluator as plain words, while the tokens stay the internal vocabulary
of ranking, audit, API and forensics. A passage lands under the same heading
in the full and the compact view, and the German system texts of the report
carry no English pipeline labels.
"""

import pytest

from inqtrix.evidence import STANDING_TEXT, _normalized_text_key, _render_source_record_block
from inqtrix.i18n import MESSAGES
from inqtrix.prompts import build_answer_system_prompt
from inqtrix.report_profiles import ReportProfile

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
        ("source_snippet", "Text the search provider returned from this source:"),
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
            "Text the search provider returned from this source:",
        } - {heading}
        assert not any(line in block for line in other), (compact, block)


def _prompt(**state_extra) -> str:
    state = {
        "today_str": "2026-10-17",
        "answer_lang": "Deutsch",
        "evidence_overview": "RESEARCH RESULT R1\n[E1] A News\n  Standing: "
        + STANDING_TEXT["provider-attributed"],
        "allowed_citations": ["https://www.a-news.de/x"],
        "source_tier_counts": {},
        "claim_status_counts": {"verified": 0, "attributed": 5, "contested": 0, "unverified": 1},
        "report_profile": ReportProfile.COMPACT,
    }
    state.update(state_extra)
    return build_answer_system_prompt(state)


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


def test_attribution_is_once_per_paragraph_and_dates_keep_their_label():
    prompt = _prompt()

    assert "once per paragraph, table row or list item" in prompt
    assert "Dates and figures always carry the label" in prompt
    assert "Attribute every one inline" not in prompt


def test_the_depth_block_names_no_label():
    prompt = _prompt(
        evidence_depth_gap={
            "active": True,
            "supported_count": 5,
            "cross_checked_count": 0,
            "single_source_count": 5,
            "single_source_ratio": 1.0,
        },
    )

    assert "EVIDENCE DEPTH" in prompt
    assert not any(label in prompt for label in LABEL_TOKENS)


GERMAN_REPORT_TEXTS = (
    "search_quality_summary",
    "answer_examined_intro",
    "answer_evidence_notice_source_context",
    "answer_evidence_notice_single_source",
    "answer_evidence_notice_unbound",
    "answer_stats_evidence_prompt_one",
    "answer_stats_evidence_prompt_other",
    "answer_stats_cross_checked",
)
ENGLISH_PIPELINE_TERMS = (
    "cross-checked",
    "single-source",
    "primary-source",
    "source-context",
    "Evidence-",
    "Hard-Fact",
    "Claims-",
    "Ledger",
)


@pytest.mark.parametrize("key", GERMAN_REPORT_TEXTS)
def test_german_report_texts_use_german_terms(key):
    german = MESSAGES[key]["de"]

    assert not any(term in german for term in ENGLISH_PIPELINE_TERMS), german
