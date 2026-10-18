"""Tests for the bilingual UI helper :mod:`inqtrix.i18n`."""

from __future__ import annotations

import re
import string

import pytest

from inqtrix.i18n import (
    MESSAGES,
    detect_ui_language,
    detect_ui_language_confident,
    t,
)


_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?::[^}]*)?\}")

_GERMAN_LETTERS = re.compile(r"[äöüÄÖÜß]")

# Umlauts alone are not enough: "Kontrolle nicht auswertbar" is pure ASCII
# and would slip through. These tokens are German and never English words,
# so one hit in the EN column is proof the entry was copied from DE.
# Deliberately excludes German/English homographs ("die", "war", "an",
# "in", "so", "man", "bei", "als", "ist") — a gate with false positives
# gets disabled, and a disabled gate protects nothing.
_GERMAN_ONLY_WORDS = re.compile(
    r"\b(nicht|wurde|wurden|werden|wird|sind|kein|keine|keinen|keiner"
    r"|und|auch|noch|schon|vom|zur|zum|ohne|eine|einen|einem|einer"
    r"|diese|dieser|dieses|wegen|damit|weil|dass|sich|beim|ihre|seine"
    r"|zwischen|bleibt|bleiben|steht|stehen|nennt|erreicht|verbraucht"
    r"|abgelehnt|abgebrochen|auswertbar|verwendet|geprüft|gefunden"
    r"|erstellt|erzeugt|beendet|fortgesetzt|übernommen)\b",
    re.IGNORECASE,
)

# ASCII substitutes are caught by ALLOWLIST, not by a word list of known
# offenders. A list of offenders only catches what someone thought to write
# down: it passed "enthaelt", "Saetze", "kuerzer", "Primaerquelle" and ten
# more that were sitting in the table the whole time. Inverting it makes a
# new substitute fail by default, and the cost is one line per legitimate
# German word that really contains ae/oe/ue/ss.
_UMLAUT_CANDIDATE = re.compile(r"\b[A-Za-zÄÖÜäöüß]*(?:ae|oe|ue|ss)[A-Za-zÄÖÜäöüß]*\b")

_LEGITIMATE_SPELLINGS = frozenset({
    # German words that genuinely carry these letter pairs
    "Abschlussbericht", "Ausführungsschritte", "Aussage", "Aussagen", "Aussagenlage",
    "Ausschnitt", "Bewertungssignal", "Einzelquellen", "Einzelquellenangaben", "Ergebnisse",
    "Klassifikation", "Kontextquellen", "Mission", "Nachbesserung", "Passe",
    "Primärquelle", "Prämisse", "Quelle", "Quellen", "Quellenkontext",
    "Quellenmix", "Quellenqualität", "Quellenverweise", "Suchergebnissen",
    "Suchquery", "Teilabschluss", "Zusammenfassung", "Zusammenfassungs",
    "zusammengefasst", "abgeschlossen",
    "abgeschlossene", "aktuell", "aktuelle", "anpassen", "ausgelassen",
    "Aktuelle", "klassifizierte", "lässt", "manuell", "muss", "neue", "neuen", "neuer",
    "passt", "passten", "verfasst", "erfasst", "lasse", "dass",
    "Projektwissen", "Modellwissen", "Wissenssammlung", "Wissenssammlungen",
    "quellenbasierte", "quellenbasierten", "quellenbasierter", "schloss",
    "zuerst", "zugelassene", "Ausschlüsse", "Passage", "Passagen",
    "Wissensquelle", "Wissenssuche", "ausgeschlossen", "dessen", "müssen", "Quelltext",
    "Aussicht", "zugelassen", "passte", "Rechercheaussicht", "messbar",
    "Adresse",
    # English terms and placeholders inside German templates
    "Cross", "cross", "Query", "query", "queries", "Request", "requested",
    "missing", "true", "value", "assumption",
})


def _placeholders(template: str) -> set[str]:
    """Return the set of named placeholders used in ``template``."""
    return set(_PLACEHOLDER_RE.findall(template))


class TestDetectUILanguage:

    def test_empty_input_falls_back_to_english(self):
        assert detect_ui_language("") == "en"
        assert detect_ui_language("   ") == "en"

    def test_umlaut_is_strong_de_signal(self):
        assert detect_ui_language("Hallo Welt mit ä") == "de"
        assert detect_ui_language("Größenordnung") == "de"
        assert detect_ui_language("Übersicht") == "de"

    def test_eszett_is_strong_de_signal(self):
        assert detect_ui_language("Straße der Fragen") == "de"

    def test_de_stopwords_without_umlauts_detected(self):
        assert detect_ui_language("Wie ist der Stand der Forschung zu Quanten?") == "de"
        assert detect_ui_language("Was ist passiert?") == "de"

    def test_pure_english_returns_en(self):
        assert detect_ui_language("How does the system work?") == "en"
        assert detect_ui_language("What is the current state of research?") == "en"

    def test_unknown_language_falls_back_to_english(self):
        # French — no DE markers, no DE stopwords, so EN fallback.
        assert detect_ui_language("Quel est l'état actuel?") == "en"

    def test_proper_noun_only_returns_en(self):
        # No stopwords either way — fall back to EN.
        assert detect_ui_language("Bitcoin price 2026") == "en"


class TestDetectUILanguageConfident:
    """The stricter primitive: a language only on a positive signal, else None.

    Unlike :func:`detect_ui_language`, ambiguous input must NOT be guessed as
    English — otherwise the BM25 tokenizer-mismatch visibility would raise false
    alarms on untagged German term queries.
    """

    def test_empty_or_whitespace_is_none(self):
        assert detect_ui_language_confident("") is None
        assert detect_ui_language_confident("   ") is None

    def test_umlaut_or_eszett_is_confident_de(self):
        assert detect_ui_language_confident("Größe der Datei") == "de"
        assert detect_ui_language_confident("Straße") == "de"

    def test_de_stopword_majority_is_confident_de(self):
        assert detect_ui_language_confident("Wie ist die Haftung geregelt?") == "de"

    def test_en_stopword_majority_is_confident_en(self):
        assert detect_ui_language_confident("How is the liability defined?") == "en"

    def test_ambiguous_term_query_is_none_not_english(self):
        # The load-bearing case: no stopwords either way -> None (NOT "en"),
        # so a German term query never falsely reads as English.
        assert detect_ui_language_confident("DSGVO Pflichten") is None
        assert detect_ui_language_confident("Bitcoin price 2026") is None

    def test_diverges_from_detect_ui_language_on_ambiguous(self):
        # detect_ui_language defaults ambiguous -> "en"; the confident variant
        # stays None. This difference is exactly why the new primitive exists.
        assert detect_ui_language("DSGVO Pflichten") == "en"
        assert detect_ui_language_confident("DSGVO Pflichten") is None

    @pytest.mark.parametrize("question", [
        "Was verlangt Measure 7.6 im Code of Practice, Kapitel Safety and Security?",
        "was verlangt measure 7.6 im code of practice, kapitel safety and security?",
        "Was ist AI?",
    ])
    def test_a_lead_of_one_function_word_is_no_signal(self, question):
        """The function words of a quoted title must not tip a German frame:
        two German words against three English ones is no signal, nor is the
        one word a short question leads by. The interface language decides."""
        assert detect_ui_language_confident(question) is None


class TestTranslator:

    def test_de_state_returns_de_string(self):
        assert t({"language": "de"}, "classify_start") == "Analysiere Frage..."

    def test_en_state_returns_en_string(self):
        assert t({"language": "en"}, "classify_start") == "Analyzing question..."

    def test_unknown_language_falls_back_to_en(self):
        assert t({"language": "fr"}, "classify_start") == "Analyzing question..."

    def test_missing_language_falls_back_to_en(self):
        assert t({}, "classify_start") == "Analyzing question..."

    def test_empty_language_falls_back_to_en(self):
        assert t({"language": ""}, "classify_start") == "Analyzing question..."

    def test_none_state_falls_back_to_en(self):
        assert t(None, "classify_start") == "Analyzing question..."  # type: ignore[arg-type]

    def test_format_kwargs_substituted(self):
        msg = t({"language": "de"}, "plan_start", round=2, max_rounds=4)
        assert "Runde 2/4" in msg

        msg_en = t({"language": "en"}, "plan_start", round=2, max_rounds=4)
        assert "round 2/4" in msg_en

    def test_missing_kwargs_returns_template_unchanged(self):
        # Defensive: missing placeholder must not crash the run.
        msg = t({"language": "de"}, "plan_start")
        assert "{round}" in msg or "Runde" in msg

    def test_unknown_key_returns_key_itself(self):
        assert t({"language": "de"}, "this_key_does_not_exist") == "this_key_does_not_exist"

    def test_de_only_when_lowercased_matches(self):
        # Defensive: case-insensitive de detection.
        assert t({"language": "DE"}, "classify_start") == "Analysiere Frage..."
        assert t({"language": "De"}, "classify_start") == "Analysiere Frage..."

    def test_search_quality_summary_uses_format_specs(self):
        # Confirms the progress message reports the consolidated-claim
        # status the research loop uses, not bundles.
        msg = t(
            {"language": "en"},
            "search_quality_summary",
            verified_claims=1,
            attributed_claims=2,
            unverified_claims=5,
            cross_checked_claims=1,
        )
        assert "1 verified" in msg
        assert "2 attributed by the search provider" in msg
        assert "5 unverified" in msg
        assert "1 cross-checked" in msg
        # Coverage is the evaluator's judgement now; the search summary does not guess it.
        assert "%" not in msg


class TestMessagesIntegrity:

    @pytest.mark.parametrize("key", list(MESSAGES.keys()))
    def test_each_key_has_de_and_en(self, key):
        entry = MESSAGES[key]
        assert "de" in entry, f"Key '{key}' missing DE translation"
        assert "en" in entry, f"Key '{key}' missing EN translation"
        assert isinstance(entry["de"], str) and entry["de"], f"Key '{key}' DE empty"
        assert isinstance(entry["en"], str) and entry["en"], f"Key '{key}' EN empty"

    @pytest.mark.parametrize("key", list(MESSAGES.keys()))
    def test_de_and_en_have_matching_placeholders(self, key):
        # Drift-Schutz: if DE has {n} and {round}, EN must have the same set.
        de_phs = _placeholders(MESSAGES[key]["de"])
        en_phs = _placeholders(MESSAGES[key]["en"])
        assert de_phs == en_phs, (
            f"Key '{key}' has different placeholders between DE/EN: "
            f"DE={de_phs}, EN={en_phs}"
        )

    @pytest.mark.parametrize("key", list(MESSAGES.keys()))
    def test_the_english_column_is_not_german(self, key):
        """An English reader must never get a German sentence because the
        EN column was filled from the DE one."""
        english = MESSAGES[key]["en"]
        assert not _GERMAN_LETTERS.search(english), (
            f"Key '{key}' has German letters in its EN column: {english!r}"
        )
        german_words = set(_GERMAN_ONLY_WORDS.findall(english))
        assert not german_words, (
            f"Key '{key}' has German words {sorted(german_words)} in its EN "
            f"column: {english!r}"
        )

    @pytest.mark.parametrize("key", list(MESSAGES.keys()))
    def test_the_german_column_uses_real_umlauts(self, key):
        """The server tells the model to write real umlauts; its own German
        text follows the same rule. ASCII substitutes ("geprueft") are a
        defect, not a style choice."""
        german = MESSAGES[key]["de"]
        found = [
            word
            for word in _UMLAUT_CANDIDATE.findall(german)
            if word not in _LEGITIMATE_SPELLINGS
        ]
        assert not found, (
            f"Key '{key}' uses ASCII umlaut substitutes {found} in its DE "
            f"column: {german!r}. If a word is legitimate German, add it to "
            f"_LEGITIMATE_SPELLINGS instead of widening the pattern."
        )

    def test_one_name_per_language_for_the_reviewing_step(self):
        """The UI phase heading says "Prüfung" / "Review". Server text used
        to add "Kontrolle", "Critic" and "Kritik-Bericht" for the same actor,
        so a reader saw up to five names for one thing in one transcript."""
        for key, entry in MESSAGES.items():
            for lang in ("de", "en"):
                text = entry[lang]
                for word in ("Kontrolle", "Critic", "Kritik"):
                    assert word not in text, (
                        f"Key '{key}' ({lang}) names the reviewing step "
                        f"'{word}'; the one name is Prüfung / Review: {text!r}"
                    )

    def test_no_format_spec_collisions(self):
        # Sanity: ``Formatter().parse`` accepts every template (no
        # malformed ``{...}`` literals).
        formatter = string.Formatter()
        for key, entry in MESSAGES.items():
            for lang, tmpl in entry.items():
                try:
                    list(formatter.parse(tmpl))
                except ValueError as exc:  # pragma: no cover — fail the test
                    pytest.fail(f"Key '{key}' lang '{lang}' template malformed: {exc}")


# A placeholder followed by a German noun (capitalised; up to three
# lower-case attributes may stand between) counts that noun.
_COUNTED_NOUN_DE = re.compile(
    r"\{([a-z_]+)(?::[^}]*)?\}[ \u00a0](?:[a-zäöüß-]+[ \u00a0]){0,3}[A-ZÄÖÜ][\wäöüßÄÖÜ-]*"
)

# Counted German nouns that need no singular, each with the reason. Reviewed
# by hand: an entry is a claim about the value or the grammar, never a way
# to silence a line that can read "1 Quellen".
_NOUN_NEUTRAL = {
    **{
        (f"outcome_reason_{reason}", "count"): (
            "no counted noun: the count is of the revised findings named before it; "
            "the noun is what they cite, change or lack"
        )
        for reason in (
            "no_source_text", "format_only", "check_unavailable", "inconclusive", "knowledge_only",
        )
    },
    ("document_analysis_read_completely", "chars"): "'Zeichen' is the same word for one",
    ("document_analysis_text_read_to_end", "chars"): "'Zeichen' is the same word for one",
    ("document_analysis_pages_without_text_one", "pages"): "no counted noun: 'Textschicht' is the object",
    ("document_analysis_pages_without_text_other", "pages"): "the form states at least two pages without text",
    ("document_analysis_scope_found_one", "scope"): "{scope} is itself a counted pair phrase",
    ("document_analysis_scope_found_other", "scope"): "{scope} is itself a counted pair phrase",
    ("document_analysis_unchecked_long_one", "limit"): "filled with CITE_QUOTE_CHARS_MAX = 1200",
    ("document_analysis_unchecked_long_other", "limit"): "filled with CITE_QUOTE_CHARS_MAX = 1200",
    ("document_analysis_unchecked_short_one", "min_words"): "filled with CITE_QUOTE_MIN_WORDS = 3",
    ("document_analysis_unchecked_short_other", "min_words"): "filled with CITE_QUOTE_MIN_WORDS = 3",
    ("document_analysis_more_one", "limit"): "no counted noun: 'je Abschnitt' distributes the limit",
    ("document_analysis_more_other", "limit"): "no counted noun: 'je Abschnitt' distributes the limit",
    ("compose_aborted", "n"): "filled with _MAX_CONSECUTIVE_EMPTY = 2",
    ("context_window_unknown_warning", "estimated"): "a prompt's token estimate, never one",
    ("context_window_too_small", "window"): "a model's context window in tokens, never one",
    ("question_too_long", "limit"): "'Zeichen' is the same word for one",
    ("agent_mission_time_budget_detail_one", "minutes"): "the pair is chosen by the minutes themselves",
    ("agent_mission_time_budget_detail_other", "minutes"): "the pair is chosen by the minutes themselves",
    ("agent_limit_ceiling_extendable", "proposed"): "no counted noun: a clause follows the number",
    ("agent_limit_ceiling_fixed", "ceiling"): "no counted noun: a clause follows the number",
    ("review_quote_unverified_draft", "round"): "no counted noun: an ordinal, a clause follows",
}

# Counted German nouns that DO read wrong for a count of one, named as debt
# with the cut that owns them. The list only shrinks: a fixed line fails the
# currency test below until its entry is removed.
_NOUN_DEBT = {
    **{
        (key, name): "Research pipeline progress line (nodes.py); not in the agent cut"
        for key, name in (
            ("classify_subquestions", "n"), ("plan_new_queries", "added"),
            ("search_start", "n"), ("search_failed_n_of_m", "total"),
            ("search_empty_n_of_m", "total"), ("provider_retry_observed", "count"),
            ("cancel_fanout_abandoned", "in_flight"), ("claim_extract_failed", "total"),
            ("claim_extract_empty", "total"), ("search_claim_binding_issues", "unbound"),
            ("search_claim_binding_issues", "unknown"), ("search_sources_processed", "n"),
            ("search_sources_processed", "citations"), ("search_sources_processed", "evidence"),
            ("evaluate_quality_summary", "evidence_records"),
            ("evaluate_quality_summary", "open_aspects"),
            ("answer_evidence_notice_single_source", "supported"),
            ("answer_stats_sources", "n"), ("answer_stats_linked", "n"),
            ("answer_stats_verified", "n"), ("answer_stats_cross_checked", "n"),
            ("answer_stats_searches", "n"), ("answer_links_removed", "n"),
            ("algorithm_failure_report_blocked", "count"),
        )
    },
}


def _counted_nouns_de() -> set[tuple[str, str]]:
    """Every (key, placeholder) whose German text counts a noun outside the
    ``{count}`` of a one/other pair."""
    found: set[tuple[str, str]] = set()
    for key, entry in MESSAGES.items():
        pair = key.endswith(("_one", "_other"))
        for match in _COUNTED_NOUN_DE.finditer(entry["de"]):
            if not (pair and match.group(1) == "count"):
                found.add((key, match.group(1)))
    return found


class TestPluralForms:
    """One place chooses a grammatical number; no template guesses it."""

    def test_no_template_uses_a_parenthetical_plural(self):
        """ "Dokument(e)", "Stelle(n)", "document(s)" read as a form, not as
        language; every counted text is a ``_one``/``_other`` pair instead."""
        import re

        pattern = re.compile(r"\w\((e|n|en|s|er|es)\)")
        found = [
            (key, lang)
            for key, entry in MESSAGES.items()
            for lang, template in entry.items()
            if pattern.search(template)
        ]
        assert found == []

    def test_every_plural_pair_is_complete_in_both_directions(self):
        missing_one = [key for key in MESSAGES if key.endswith("_other") and key[:-6] + "_one" not in MESSAGES]
        missing_other = [key for key in MESSAGES if key.endswith("_one") and key[:-4] + "_other" not in MESSAGES]
        assert missing_one == [] and missing_other == []

    def test_every_counted_german_noun_has_a_singular_or_a_named_reason(self):
        """A number before a noun either sits in a one/other pair or is on a
        reviewed list: neutral with its reason, or named debt with its owner.
        A new "{sources} Quellen" outside a pair fails here."""
        unexplained = _counted_nouns_de() - set(_NOUN_NEUTRAL) - set(_NOUN_DEBT)
        assert sorted(unexplained) == []

    def test_the_noun_lists_name_only_lines_that_still_count(self):
        """A fixed or reworded line leaves its list, so no stale entry can
        excuse the next template that reuses its key."""
        found = _counted_nouns_de()
        assert sorted(set(_NOUN_NEUTRAL) - found) == []
        assert sorted(set(_NOUN_DEBT) - found) == []
        assert set(_NOUN_NEUTRAL).isdisjoint(_NOUN_DEBT)

    def test_t_count_selects_one_only_for_exactly_one(self):
        from inqtrix.i18n import t_count

        state = {"language": "de"}
        assert t_count(state, "document_analysis_summary_cited", 1) == "1 Stelle wörtlich belegt."
        assert t_count(state, "document_analysis_summary_cited", 0) == "0 Stellen wörtlich belegt."
        assert t_count(state, "document_analysis_summary_cited", 2) == "2 Stellen wörtlich belegt."
