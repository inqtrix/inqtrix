"""Bilingual (DE/EN) progress messages and UI-language detection.

The agent's user-facing progress strings (streamed as SSE chunks during a
research run) need to follow the language of the question. Detection runs
in two stages:

1. A fast local heuristic (:func:`detect_ui_language`) used by
   :func:`inqtrix.state.initial_state` so the very first progress event
   ("Analysiere Frage..." / "Analyzing question...") is already in the
   right language.
2. The LLM-based classification in :func:`inqtrix.nodes.classify` later
   overwrites ``state["language"]`` with a more precise reading.

Translations are looked up via :func:`t`. The fallback chain is:
``state["language"] == "de"`` → DE, anything else (including ``""``,
``"en"``, ``"fr"``, ...) → EN.
"""

from __future__ import annotations

import re
from typing import Any

# Umlauts and ß are an unambiguous DE signal.
_DE_CHARS = re.compile(r"[äöüÄÖÜß]")

# Frequent DE stopwords (short, lowercased tokens).
# Bewusst nur Wörter, die im EN selten/nie vorkommen.
_DE_STOPWORDS = frozenset({
    "der", "die", "das", "den", "dem", "des",
    "und", "oder", "aber", "denn", "weil", "wenn", "dass", "ob",
    "ist", "sind", "war", "waren", "wird", "werden", "wurde", "wurden",
    "sein", "hat", "haben", "hatte", "hatten",
    "ein", "eine", "einen", "einer", "eines", "einem",
    "kein", "keine", "keinen", "keiner", "keines", "keinem",
    "wie", "warum", "wieso", "weshalb", "wann", "wer", "wen", "wem",
    "wo", "was", "welche", "welcher", "welches", "wieviel", "wieviele",
    "ich", "du", "er", "es", "wir", "ihr", "sie",
    "mich", "dich", "ihn", "uns", "euch",
    "mein", "meine", "meiner", "meines", "meinem", "meinen",
    "dein", "deine", "deiner", "deines", "deinem", "deinen",
    "sein", "seine", "seiner", "seines", "seinem", "seinen",
    "ihre", "ihrer", "ihres", "ihrem", "ihren",
    "unser", "unsere", "unserer", "unseres", "unserem", "unseren",
    "euer", "eure", "eurer", "eures", "eurem", "euren",
    "nicht", "noch", "schon", "auch", "nur", "mehr", "viel", "viele",
    "mit", "ohne", "bei", "von", "vom", "zum", "zur", "zu",
    "für", "für", "über", "ueber", "unter", "vor", "nach", "auf", "an",
    "im", "ins", "am", "ans", "beim",
})

# English stopword tie-breakers when no DE marker fires.
_EN_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "because",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had",
    "do", "does", "did", "doing",
    "how", "why", "when", "where", "what", "which", "who", "whom", "whose",
    "this", "that", "these", "those",
    "with", "without", "of", "for", "to", "from", "by", "at", "in", "on",
    "i", "you", "he", "she", "we", "they", "it",
    "my", "your", "his", "her", "our", "their", "its",
    "should", "would", "could", "can", "may", "might", "must",
    "not", "no", "any", "some", "all", "many", "much",
})

_TOKEN_RE = re.compile(r"\b\w+\b", re.UNICODE)

# How many function words one language must lead by before a text counts as
# written in it. A lead of one is what the function words of a quoted title
# produce ("Was verlangt Measure 7.6 im Code of Practice ...": two German
# words against three English ones), so it is no signal.
_CONFIDENT_LEAD = 2


def detect_ui_language(text: str) -> str:
    """Return ``"de"`` or ``"en"`` based on a fast local heuristic.

    1. Empty input → ``"en"`` (fallback per spec).
    2. Any DE-specific character (umlaut or ß) → ``"de"``.
    3. Tokenize lowercased text on word boundaries and count
       stopword hits in either language; ``de_hits > en_hits`` → ``"de"``,
       otherwise → ``"en"``.
    """
    if not text:
        return "en"
    if _DE_CHARS.search(text):
        return "de"
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return "en"
    de_hits = sum(1 for tok in tokens if tok in _DE_STOPWORDS)
    en_hits = sum(1 for tok in tokens if tok in _EN_STOPWORDS)
    if de_hits > en_hits:
        return "de"
    return "en"


def detect_ui_language_confident(text: str, *, lead: int = _CONFIDENT_LEAD) -> str | None:
    """Return ``"de"``/``"en"`` only on a positive signal, else ``None``.

    Unlike :func:`detect_ui_language` (which DEFAULTS to ``"en"`` for empty or
    ambiguous input), this reports a language only when a clear signal is
    present: a DE-specific character, or a lead of at least two stopwords for
    exactly one language. Ambiguous input (a short term query with no
    stopwords like ``"DSGVO Pflichten"``, a German question around English
    titles, a short question one word leads) returns ``None``. *lead* is how
    many stopwords one language must lead by; a caller with nothing better
    to fall back on may accept a lead of one.

    Why a separate, stricter primitive: callers that act on a *mismatch* must
    not fire on a guess. The tokenizer-mismatch visibility for BM25 retrieval
    flags only confident cross-language cases; the ``"en"`` fallback of
    :func:`detect_ui_language` would otherwise misread untagged DE queries as
    English and raise false alarms. Shares the same stopword tables (defined
    once).
    """
    if not text:
        return None
    if _DE_CHARS.search(text):
        return "de"
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return None
    de_hits = sum(1 for tok in tokens if tok in _DE_STOPWORDS)
    en_hits = sum(1 for tok in tokens if tok in _EN_STOPWORDS)
    if de_hits - en_hits >= lead:
        return "de"
    if en_hits - de_hits >= lead:
        return "en"
    return None


# ---------------------------------------------------------------------------
# Translation table
# ---------------------------------------------------------------------------

MESSAGES: dict[str, dict[str, str]] = {
    # ---- agent run texts written in the reader's language ----
    "memo_title_answer": {"de": "Antwort", "en": "Answer"},
    "task_evidence_partial_coverage": {
        "de": "teilweise ausgewertet ({cut} nicht ausgewertet)",
        "en": "partially evaluated ({cut} not evaluated)",
    },
    # ---- Dokumentanalyse (serverseitiger Fensterdurchlauf) ----
    "document_analysis_untitled_document": {
        "de": "Dokument ohne Titel (Nr. {number})",
        "en": "Document without a title (no. {number})",
    },
    # ---- Sprache / Recency / Type-Labels (eingebettet in classify-Hints) ----
    "lang_name_de": {"de": "Deutsch", "en": "German"},
    "lang_name_en": {"de": "Englisch", "en": "English"},
    "lang_name_fr": {"de": "Französisch", "en": "French"},
    "lang_name_es": {"de": "Spanisch", "en": "Spanish"},
    "lang_name_it": {"de": "Italienisch", "en": "Italian"},
    "lang_name_pt": {"de": "Portugiesisch", "en": "Portuguese"},
    # A window counts back from now; the approval card of the agent names
    # the same windows in the same words.
    "recency_hour": {"de": "letzte Stunde", "en": "past hour"},
    "recency_day": {"de": "letzte 24 Stunden", "en": "past 24 hours"},
    "recency_week": {"de": "letzte 7 Tage", "en": "past 7 days"},
    "recency_month": {"de": "letzte 30 Tage", "en": "past 30 days"},
    "recency_year": {"de": "letzte 12 Monate", "en": "past 12 months"},
    "recency_label": {
        "de": "Aktualität: {label}",
        "en": "Recency: {label}",
    },
    "type_academic": {"de": "Akademisch", "en": "Academic"},
    "type_news": {"de": "Nachrichten", "en": "News"},
    "search_in_lang": {
        "de": "Suche auf {lang_name}",
        "en": "Search in {lang_name}",
    },

    # ---- compose_answer_sections (nodes.py) ----
    "compose_section_token_limit": {
        "de": (
            "Warnung: Abschnitt '{heading}' hat die Ausgabegrenze des Modells "
            "erreicht (Stoppgrund: {finish_reason}; Tokens erlaubt: {request_max}, "
            "erzeugt: {completion})"
        ),
        "en": (
            "Warning: section '{heading}' reached the model's output limit "
            "(stop reason: {finish_reason}; tokens allowed: {request_max}, "
            "written: {completion})"
        ),
    },
    "compose_section_truncation": {
        "de": "Warnung: Abschnitt '{heading}' wirkt abgeschnitten: {reasons}",
        "en": "Warning: section '{heading}' looks cut off: {reasons}",
    },
    "compose_aborted": {
        "de": (
            "Schreiben des Berichts abgebrochen: {n} Abschnitte nacheinander "
            "leer (zuletzt '{heading}')"
        ),
        "en": (
            "Report writing aborted: {n} sections in a row came back empty "
            "(last '{heading}')"
        ),
    },
    "context_window_unknown_warning": {
        "de": (
            "Warnung: Kontextfenster für Modell {model} unbekannt "
            "(geschätzter Bedarf {estimated} Tokens, Mindestwert {required})"
        ),
        "en": (
            "Warning: context window for model {model} is unknown "
            "(estimated need {estimated} tokens, required minimum {required})"
        ),
    },
    "context_window_too_small": {
        "de": (
            "Kontextfenster zu klein: Modell {model} fasst {window} Tokens, "
            "benötigt werden {required}"
        ),
        "en": (
            "Context window too small: model {model} holds {window} tokens, "
            "{required} are required"
        ),
    },
    # A model resolution that found no model; the server log names the
    # step and the provider.
    "node_model_unresolved": {
        "de": (
            "Warnung: Für diesen Schritt ließ sich kein Modell bestimmen; es "
            "gilt die Voreinstellung des Anbieters"
        ),
        "en": (
            "Warning: no model could be determined for this step; the "
            "provider's default applies"
        ),
    },

    # ---- classify (nodes.py) ----
    "classify_start": {
        "de": "Analysiere Frage...",
        "en": "Analyzing question...",
    },
    "classify_warning_hint": {
        "de": "Hinweis: {warning}",
        "en": "Note: {warning}",
    },
    "classify_direct_answer": {
        "de": "Direktantwort ohne Websuche",
        "en": "Direct answer without web search",
    },
    "classify_search_required": {
        "de": "Websuche erforderlich{hints}",
        "en": "Web search required{hints}",
    },
    "classify_subquestions": {
        "de": "Analyseziele erkannt: {n} Teilfragen: {subs}",
        "en": "Analysis targets identified: {n} sub-questions: {subs}",
    },
    "classify_failed": {
        "de": "Klassifikation fehlgeschlagen ({label}); die Recherche läuft mit vorsichtigen Standardannahmen weiter",
        "en": "Classification failed ({label}); the research continues with cautious standard assumptions",
    },
    "classify_contract_fallback": {
        "de": "Antwortform nicht erkannt ({reason}) — nutze 'allgemein'",
        "en": "Answer form not recognised ({reason}) — using 'general'",
    },
    "search_source_roles": {
        "de": "Quellen-Rollen: {judged} beurteilt, {missing} nicht beurteilt, {invalid} ungültig",
        "en": "Source roles: {judged} judged, {missing} not judged, {invalid} invalid",
    },
    "classify_subquestions_fallback": {
        "de": "Teilfragen nicht erkannt — die Frage selbst ist die einzige Teilfrage",
        "en": "Sub-questions not recognised — the question itself is the only sub-question",
    },

    # ---- plan (nodes.py) ----
    "plan_start": {
        "de": "Plane Suchanfragen (Runde {round}/{max_rounds})...",
        "en": "Planning search queries (round {round}/{max_rounds})...",
    },
    "plan_failed": {
        "de": "Suchplanung fehlgeschlagen ({label}); die Suche stützt sich auf die Frage selbst",
        "en": "Search planning failed ({label}); the search rests on the question itself",
    },
    "plan_query_generation_failed": {
        "de": "Suchplanung: {reason}; die Suche stützt sich auf die Frage selbst",
        "en": "Search planning: {reason}; the search rests on the question itself",
    },
    "plan_new_queries": {
        "de": "{added} neue Suchanfragen generiert{strategy_hint}",
        "en": "{added} new search queries generated{strategy_hint}",
    },
    "plan_no_more_queries": {
        "de": "Keine neuen Suchanfragen möglich — beende Recherche",
        "en": "No new search queries possible — ending research",
    },

    # ---- search (nodes.py) ----
    "search_start": {
        "de": "Durchsuche {n} Suchanfragen (Runde {round}/{max_rounds})...",
        "en": "Searching {n} queries (round {round}/{max_rounds})...",
    },
    "search_failed_n_of_m": {
        "de": "{failed} von {total} Suchanfragen fehlgeschlagen (leere Ergebnisse zurückgegeben)",
        "en": "{failed} of {total} searches failed (returned empty results)",
    },
    "search_empty_n_of_m": {
        "de": "{empty} von {total} Suchanfragen lieferten keine Ergebnisse",
        "en": "{empty} of {total} searches returned no results",
    },
    "provider_retry_observed": {
        "de": "Verbindung zu {provider} instabil: {count} Wiederholungen bei {operation} ({model})",
        "en": "{provider} connection unstable — {count} retries during {operation} ({model})",
    },
    "provider_retry_attempt": {
        "de": (
            "{provider}: Wiederholung {attempt}/{max_attempts} bei {operation} "
            "({model}); nächster Versuch in {delay} s wegen {reason}"
        ),
        "en": (
            "{provider} retry {attempt}/{max_attempts} during {operation} "
            "({model}); next attempt in {delay}s after {reason}"
        ),
    },
    "retry_operation_classify": {
        "de": "Klassifikation",
        "en": "classification",
    },
    "retry_operation_plan": {
        "de": "Suchplanung",
        "en": "search planning",
    },
    "retry_operation_search": {
        "de": "Websuche",
        "en": "web search",
    },
    "retry_operation_claim_extraction": {
        "de": "Aussagenerfassung",
        "en": "statement extraction",
    },
    "retry_operation_evaluate": {
        "de": "Bewertung",
        "en": "evaluation",
    },
    "retry_operation_answer_synthesis": {
        "de": "Berichtsabschnitt",
        "en": "report section",
    },
    "cancel_fanout_abandoned": {
        "de": (
            "Abbruch angefordert: {abandoned} von {total} {operation}-Aufrufen "
            "verworfen, {in_flight} laufende Aufrufe werden beendet."
        ),
        "en": (
            "Cancel requested: abandoned {abandoned} of {total} {operation} "
            "calls, finishing {in_flight} in-flight calls."
        ),
    },
    "claim_extract_failed": {
        "de": "{failed} von {total} Aussagenerfassungen fehlgeschlagen",
        "en": "{failed} of {total} statement extractions failed",
    },
    "claim_extract_empty": {
        "de": "{empty} von {total} Aussagenerfassungen ergaben trotz Suchkontext keine Aussage",
        "en": "{empty} of {total} statement extractions found no statement despite search context",
    },
    "search_claim_binding_issues": {
        "de": "{unbound} Aussagen ohne belegte Quelle, {unknown} unbekannte Quellen-Referenzen — Gegenprüfung eingeschränkt",
        "en": "{unbound} statements without a bound source, {unknown} unknown source references — cross-check limited",
    },
    "search_sources_processed": {
        "de": (
            "{n} Suchantworten verarbeitet, {citations} Referenzen gesammelt, "
            "{evidence} Belege angelegt"
        ),
        "en": (
            "{n} search answers processed, {citations} references collected, "
            "{evidence} evidence records created"
        ),
    },
    # The cross-check count of this step is every statement whose basis is
    # cross-checked, whatever its status; the footer counts verified ones
    # only, and each says so.
    "search_quality_summary": {
        "de": (
            "Aussagenlage: verifiziert {verified_claims}, "
            "vom Anbieter zugeschrieben {attributed_claims}, "
            "nicht belegt {unverified_claims}; "
            "gegengeprüft, unabhängig vom Stand: {cross_checked_claims}"
        ),
        "en": (
            "Statement status: {verified_claims} verified, "
            "{attributed_claims} attributed by the search provider, "
            "{unverified_claims} unverified; "
            "cross-checked regardless of status: {cross_checked_claims}"
        ),
    },
    "search_source_quality": {
        "de": (
            "Quellenmix: {primary} primär, {mainstream} etabliert, "
            "{stakeholder} interessengeleitet, {unknown} nicht eingeordnet, "
            "{low} minderwertig; Quellenqualität {quality}"
        ),
        "en": (
            "Source mix: {primary} primary, {mainstream} established media, "
            "{stakeholder} interest groups, {unknown} unclassified, "
            "{low} low quality; source quality {quality}"
        ),
    },

    # ---- evaluate (nodes.py) ----
    "evaluate_start": {
        "de": "Bewerte Informationsqualität (nach Runde {round}/{max_rounds})...",
        "en": "Evaluating information quality (after round {round}/{max_rounds})...",
    },
    "evaluate_confidence_missing": {
        "de": "Bewertung unvollständig (keine Konfidenz angegeben) — es gilt der Standardwert 5",
        "en": "Evaluation incomplete (no confidence given) — using the default 5",
    },
    "evaluate_confidence_out_of_range": {
        "de": "Konfidenz der Bewertung außerhalb des Wertebereichs ({raw}) — auf {clamped} begrenzt",
        "en": "Evaluation confidence out of range ({raw}) — clamped to {clamped}",
    },
    "evaluate_coverage_missing": {
        "de": "Bewertung ohne Teilfragen-Abdeckung — offene Teilfragen bleiben offen",
        "en": "Evaluation without sub-question coverage — open sub-questions stay open",
    },
    "evaluate_evidence_scores_missing": {
        "de": (
            "Bewertung der Beleglage unvollständig (Widerspruchsfreiheit oder "
            "Tragfähigkeit nicht bewertet) — es gilt der Standardwert 5"
        ),
        "en": (
            "Evidence assessment incomplete (consistency or sufficiency not "
            "assessed) — using the default 5"
        ),
    },
    "evaluate_failed": {
        "de": "Qualitätsbewertung fehlgeschlagen ({label}) — Konfidenz vorsichtig begrenzt",
        "en": "Quality evaluation failed ({label}) — using conservative confidence cap",
    },
    "research_finished": {
        "de": "Recherche abgeschlossen (Konfidenz: {conf}/10, Runden: {round})",
        "en": "Research finished (Confidence: {conf}/10, Rounds: {round})",
    },
    "confidence_continue": {
        "de": "Konfidenz {conf}/10 — weitere Recherche nötig",
        "en": "Confidence {conf}/10 — further research needed",
    },
    "evaluate_quality_summary": {
        "de": (
            "Bewertungssignal: Quellenqualität {source_quality}, "
            "Aussagenqualität {claim_quality}, {evidence_records} Belege, "
            "{open_aspects} offene Teilfragen"
        ),
        "en": (
            "Evaluation signal: source quality {source_quality}, "
            "statement quality {claim_quality}, {evidence_records} evidence records, "
            "{open_aspects} open sub-questions"
        ),
    },
    "min_rounds_continue": {
        "de": "Mindestzahl der Runden ({min_rounds}) noch nicht erreicht (aktuell {round}); Recherche geht weiter",
        "en": "Minimum number of rounds ({min_rounds}) not yet reached (currently {round}); continuing research",
    },
    "evaluate_evidence_too_thin": {
        "de": (
            "Beleglage noch zu dünn (berichtsfähige Belege: {count}, nötig sind "
            "mindestens {minimum}); weitere Recherche."
        ),
        "en": (
            "Evidence still too thin (report-ready evidence records: {count}, "
            "at least {minimum} needed); continuing research."
        ),
    },

    # ---- answer (nodes.py) ----
    "answer_start": {
        "de": "Formuliere Antwort (nach {n} {round_label})...",
        "en": "Formulating answer (after {n} {round_label})...",
    },
    # --- Research answer appendix and footer (user-visible answer text) ---
    "answer_references_heading": {"de": "## Referenzen", "en": "## References"},
    "answer_references_none": {
        "de": "Keine zitatgebundenen Markdown-Links im Antworttext gefunden.",
        "en": "No citation-bound Markdown links found in the answer text.",
    },
    "answer_examined_heading": {"de": "## Weitere geprüfte Quellen", "en": "## Further examined sources"},
    "answer_examined_intro": {
        "de": "Im Text nicht zitiert; vom Recherchelauf gesichtet und unter den Belegen des Laufs geführt.",
        "en": "Not cited in the text; examined by the research run and listed among its evidence.",
    },
    "answer_reference_label": {"de": "Quelle", "en": "Source"},
    "answer_evidence_label_unassigned": {
        "de": "[{label}: nicht zugeordnet]",
        "en": "[{label}: not assigned]",
    },
    "answer_completeness_heading": {"de": "## Hinweis zur Vollständigkeit", "en": "## Note on completeness"},
    "answer_completeness_status": {"de": "- Status: unvollständig", "en": "- Status: incomplete"},
    "answer_completeness_provider_stop": {
        "de": "- Stoppgrund des Modells: `{finish_reason}`",
        "en": "- Model stop reason: `{finish_reason}`",
    },
    "answer_completeness_diagnosis": {"de": "- Diagnose: {reasons}", "en": "- Diagnosis: {reasons}"},
    # Why a written report or section looks cut off, one text per signal:
    # in the run's language for the reader, in English for the logs.
    "answer_incomplete_reason_empty": {"de": "der Text ist leer", "en": "the text is empty"},
    "answer_incomplete_reason_open_bold": {
        "de": "Fettschrift am Ende nicht geschlossen",
        "en": "bold text not closed at the end",
    },
    "answer_incomplete_reason_open_code": {
        "de": "Codeblock am Ende nicht geschlossen",
        "en": "code block not closed at the end",
    },
    "answer_incomplete_reason_fragment": {
        "de": "endet mit einem kurzen Bruchstück ohne Satzende",
        "en": "ends in a short fragment without a sentence ending",
    },
    "answer_incomplete_reason_stop": {
        "de": "Stoppgrund des Modells: {detail}",
        "en": "model stop reason: {detail}",
    },
    "answer_incomplete_reason_missing_sections": {
        "de": "fehlende Abschlussabschnitte: {detail}",
        "en": "missing closing sections: {detail}",
    },
    "answer_completeness_hint": {
        "de": "- Hinweis: Bereits sauber abgeschlossene Abschnitte bleiben sichtbar; es wurde keine verdeckende Regeneration ausgeführt.",
        "en": "- Note: sections that finished cleanly stay visible; no masking regeneration was run.",
    },
    "answer_omission_note": {
        "de": "- Nicht ausgewertete Quellen: {cut} passten nicht in das Evidenz-Budget und wurden im Bericht nicht ausgewertet (Suchanfragen: {queries}).",
        "en": "- Sources not evaluated: {cut} did not fit into the evidence budget and were not evaluated in the report (search queries: {queries}).",
    },
    "answer_evidence_notice_heading": {"de": "## Evidenzhinweis", "en": "## Evidence notice"},
    # What source_context_only counts: no cited sentence restates a statement
    # recorded for the source it cites, whatever that statement's status.
    "answer_evidence_notice_source_context": {
        "de": "Kein Satz der Antwort, der eine Quelle zitiert, gibt eine zu dieser Quelle erfasste Aussage wieder. Ob ein Satz von seiner Quelle gestützt wird, ist daher nicht festgestellt.",
        "en": "No sentence of the answer that cites a source restates a statement recorded for that source. Whether a sentence is supported by its source is therefore not established.",
    },
    "answer_evidence_notice_unbound": {
        "de": "Mindestens ein zitierter Link ließ sich keinem Beleg zuordnen. Die betroffenen Punkte sind als nicht ausreichend belegt zu behandeln.",
        "en": "At least one cited link could not be traced back to an evidence record. Treat the affected points as insufficiently supported.",
    },
    "answer_evidence_notice_citation_without_claim": {
        "de": "Zu mindestens einer zitierten Quelle ist keine Aussage erfasst. Die Punkte, die sich auf sie stützen, sind als nicht ausreichend belegt zu behandeln.",
        "en": "At least one cited source carries no recorded statement. Treat the points that rest on it as insufficiently supported.",
    },
    "answer_evidence_notice_algorithm_failure": {
        "de": "Mindestens ein Schritt der Recherche ist fehlgeschlagen. Der Bericht entstand trotzdem, kann aber auf weniger Belegen beruhen als vorgesehen.",
        "en": "At least one step of the research failed. The report was still written but may rest on less evidence than intended.",
    },
    # The basis line under every report that cites a source; its words mean
    # what the research note to a parent run says (agents/kernel/evidence_read.py).
    "answer_evidence_basis_provider_context": {
        "de": "Grundlage: die Angaben des Suchanbieters zu den zitierten Quellen; die Seiten selbst wurden nicht gelesen.",
        "en": "Basis: what the search provider returned for the cited sources; the pages themselves were not read.",
    },
    "answer_evidence_basis_evidence": {
        "de": "Grundlage: das gespeicherte Material der zitierten Quellen, nicht allein die Angaben des Suchanbieters.",
        "en": "Basis: the stored material of the cited sources, not the search provider's context alone.",
    },
    "answer_evidence_basis_provider_only": {
        "de": "Zitierte Quellen nur mit den Angaben des Suchanbieters: {provider_only} von {cited}.",
        "en": "Cited sources with only what the search provider returned: {provider_only} of {cited}.",
    },
    # The numbers of the evidence-depth gap: every supported statement the
    # research collected, not only those the report cites.
    "answer_evidence_depth_numbers": {
        "de": "Gestützte Aussagen der Recherche mit mehr als einer Quelle: {multi} von {supported}.",
        "en": "Supported statements of the research with more than one source: {multi} of {supported}.",
    },
    # The stats footer, one piece per count, joined with " · ".
    "answer_stats_sources_one": {"de": "{count} Quelle recherchiert", "en": "{count} source researched"},
    "answer_stats_sources_other": {"de": "{count} Quellen recherchiert", "en": "{count} sources researched"},
    "answer_stats_evidence_prompt_one": {"de": "{count} Beleg für den Bericht bereitgestellt", "en": "{count} evidence record provided for the report"},
    "answer_stats_evidence_prompt_other": {"de": "{count} Belege für den Bericht bereitgestellt", "en": "{count} evidence records provided for the report"},
    "answer_stats_linked_one": {"de": "{count} Quelle im Bericht verlinkt", "en": "{count} source linked in the report"},
    "answer_stats_linked_other": {"de": "{count} Quellen im Bericht verlinkt", "en": "{count} sources linked in the report"},
    "answer_stats_verified_one": {"de": "{count} verifizierte Aussage", "en": "{count} verified statement"},
    "answer_stats_verified_other": {"de": "{count} verifizierte Aussagen", "en": "{count} verified statements"},
    "answer_stats_attributed_one": {
        "de": "{count} vom Anbieter zugeschriebene Aussage",
        "en": "{count} statement attributed by the search provider",
    },
    "answer_stats_attributed_other": {
        "de": "{count} vom Anbieter zugeschriebene Aussagen",
        "en": "{count} statements attributed by the search provider",
    },
    # Verified statements whose basis is cross-checked; the search step
    # counts that basis whatever the status, and says so.
    "answer_stats_cross_checked_one": {
        "de": "{count} verifizierte Aussage gegengeprüft",
        "en": "{count} verified statement cross-checked",
    },
    "answer_stats_cross_checked_other": {
        "de": "{count} verifizierte Aussagen gegengeprüft",
        "en": "{count} verified statements cross-checked",
    },
    "answer_stats_omitted_one": {"de": "{count} Evidenzbeleg wegen Budget ausgelassen", "en": "{count} evidence record omitted for budget"},
    "answer_stats_omitted_other": {"de": "{count} Evidenzbelege wegen Budget ausgelassen", "en": "{count} evidence records omitted for budget"},
    "answer_stats_failed_steps_one": {"de": "{count} fehlgeschlagener Schritt", "en": "{count} failed step"},
    "answer_stats_failed_steps_other": {"de": "{count} fehlgeschlagene Schritte", "en": "{count} failed steps"},
    "answer_stats_searches_one": {"de": "{count} Suche", "en": "{count} search"},
    "answer_stats_searches_other": {"de": "{count} Suchen", "en": "{count} searches"},
    "answer_stats_round_one": {"de": "{count} Runde", "en": "{count} round"},
    "answer_stats_round_other": {"de": "{count} Runden", "en": "{count} rounds"},
    "answer_stats_confidence": {"de": "Konfidenz {conf}/10", "en": "Confidence {conf}/10"},
    "answer_section_start": {
        "de": "Schreibe Berichtsabschnitt {index}/{total}: {heading}",
        "en": "Writing report section {index}/{total}: {heading}",
    },
    "answer_round_singular": {"de": "Runde", "en": "round"},
    "answer_round_plural": {"de": "Runden", "en": "rounds"},
    "answer_fallback_model": {
        "de": "Der Bericht ließ sich nicht schreiben; neuer Versuch mit dem Ersatzmodell {model}",
        "en": "The report could not be written; trying again with the backup model {model}",
    },
    # The steps of an answer without a written report end in {outcome}: one of
    # the two texts below, depending on whether the research gathered material.
    "answer_timeout_fallback": {
        "de": "Das Schreiben des Berichts hat die Zeitgrenze überschritten; {outcome}",
        "en": "Writing the report exceeded the time limit; {outcome}",
    },
    "answer_provider_failed_no_fallback": {
        "de": (
            "Der Bericht ließ sich nicht schreiben ({error}); es ist kein "
            "Ersatzmodell eingerichtet; {outcome}"
        ),
        "en": "The report could not be written ({error}); no backup model is set up; {outcome}",
    },
    "answer_fallback_model_failed": {
        "de": "Auch das Ersatzmodell konnte den Bericht nicht schreiben ({error}); {outcome}",
        "en": "The backup model could not write the report either ({error}); {outcome}",
    },
    "answer_fallback_outcome_material": {
        "de": "die Antwort zeigt das bisher gesammelte Material mit einem Warnhinweis",
        "en": "the answer shows the material gathered so far with a warning",
    },
    "answer_fallback_outcome_no_material": {
        "de": "es liegt noch kein gesammeltes Material vor, die Antwort zeigt nur einen Warnhinweis",
        "en": "no material has been gathered yet, so the answer shows only a warning",
    },
    "answer_incomplete_detected": {
        "de": "Finale Antwort als unvollständig erkannt",
        "en": "Final answer detected as incomplete",
    },
    "answer_links_removed": {
        "de": "{n} nicht-zugelassene Links entfernt",
        "en": "{n} non-allowed links removed",
    },
    "evidence_contract_needs_review": {
        "de": "Belege des Berichts mit Einschränkung — der Evidenzhinweis unter dem Bericht nennt den Grund",
        "en": "The report's evidence has a limitation — the evidence notice under the report names the reason",
    },
    "evidence_contract_source_context_only": {
        "de": "Kein zitierter Satz gibt eine erfasste Aussage wieder — der Evidenzhinweis unter dem Bericht erklärt es",
        "en": "No cited sentence restates a recorded statement — the evidence notice under the report explains it",
    },
    "algorithm_failure_report_blocked": {
        "de": "Der normale Bericht entfällt wegen blockierender Fehler (Anzahl: {count})",
        "en": "The normal report is withheld because of blocking failures (count: {count})",
    },

    # ---- graph.py ----
    "run_aborted": {
        "de": "Recherche abgebrochen: {exc}",
        "en": "Research aborted: {exc}",
    },
    "run_rate_limited": {
        "de": "Recherche abgebrochen: Der Modelldienst ist gerade ausgelastet (Anfragelimit erreicht).",
        "en": "Research aborted: the model service is busy right now (request limit reached).",
    },
    "run_failed": {
        "de": "Recherche fehlgeschlagen: {exc}",
        "en": "Research failed: {exc}",
    },
    "chat_mode_failed": {
        "de": "Chat-Modus fehlgeschlagen: {exc}",
        "en": "Chat mode failed: {exc}",
    },

    # ---- stop_criteria.py ----
    "contradictions_severe": {
        "de": "Schwere Widersprüche erkannt — Konfidenz stark begrenzt",
        "en": "Severe contradictions detected — confidence strongly capped",
    },
    "contradictions_light": {
        "de": "Leichte Widersprüche erkannt (z.B. Datumsabweichungen)",
        "en": "Minor contradictions detected (e.g. date mismatches)",
    },
    "multiple_explanations": {
        "de": "Mehrere mögliche Erklärungen erkannt",
        "en": "Multiple possible explanations detected",
    },
    "falsification_released": {
        "de": "Konfidenz wieder hoch — Falsifikations-Modus deaktiviert",
        "en": "Confidence recovered — falsification mode deactivated",
    },
    "falsification_started": {
        "de": "Niedrige Evidenz — starte Falsifikations-Recherche",
        "en": "Low evidence — starting falsification research",
    },
    "stagnation_low_evidence_stop": {
        "de": (
            "Recherche ausgeschöpft, aber Evidenz reicht nicht für "
            "die geforderte Bestätigung"
        ),
        "en": (
            "Research exhausted, but evidence is insufficient for the "
            "requested confirmation"
        ),
    },
    "utility_stop": {
        "de": "Informationsgewinn stagniert — beende Recherche",
        "en": "Information gain stagnating — ending research",
    },
    "plateau_stop": {
        "de": "Konfidenz {conf}/10 stabil — Recherche abgeschlossen",
        "en": "Confidence {conf}/10 stable — research complete",
    },

    # ---- streaming.py SSE error chunks (user-facing) ----
    "sse_request_timeout": {
        "de": "\n---\n\n⚠️ **Fehler bei der Recherche:** Zeitgrenze der Anfrage erreicht",
        "en": "\n---\n\n⚠️ **Research error:** request timeout reached",
    },
    "sse_agent_error": {
        "de": "\n---\n\n⚠️ **Fehler bei der Recherche:** {err}",
        "en": "\n---\n\n⚠️ **Research error:** {err}",
    },

    # ---- agents/narration.py transcript lines (Agent Desk) ----
    # What stands in a transcript line in place of an internal identifier.
    # A narration line is assembled from finished artifacts, so there is
    # no writer left to ask for a repair - and dropping the line would
    # take a step of the run away from the reader without saying so.
    "agent_reader_identifier_removed": {
        "de": "[interne Kennung entfernt]",
        "en": "[internal identifier removed]",
    },

    # ---- agent transcript channel (narration + activity) ----
    "agent_limit_tokens_narration": {
        "de": (
            "Das feste serverseitige Tokenlimit ist erreicht. Der Lauf wurde "
            "ohne automatische Teilantwort beendet; eine Erweiterung ist für "
            "diesen Lauf nicht möglich."
        ),
        "en": (
            "The fixed server-side token limit is reached. The run ended "
            "without an automatic partial answer; it cannot be extended."
        ),
    },
    "question_too_long": {
        "de": (
            "Die Frage ist zu lang (max. {limit} Zeichen). Bitte kürzer "
            "formulieren."
        ),
        "en": (
            "The question is too long (at most {limit} characters). Please "
            "shorten it."
        ),
    },
    # The reasons of the step plan_query_generation_failed.
    "research_query_plan_not_json": {
        "de": "keine lesbare Liste von Suchanfragen erhalten",
        "en": "no readable list of search queries returned",
    },
    "research_answer_failed_fallback_failed_body": {
        "de": (
            "Der Bericht ließ sich nicht schreiben ({error}); auch das "
            "Ersatzmodell `{model}` ist gescheitert ({fallback_error})."
        ),
        "en": (
            "The report could not be written ({error}); the backup model "
            "`{model}` failed as well ({fallback_error})."
        ),
    },
    "research_answer_failed_no_fallback_body": {
        "de": (
            "Der Bericht ließ sich nicht schreiben ({error}); ein Ersatzmodell "
            "ist nicht eingerichtet."
        ),
        "en": (
            "The report could not be written ({error}); no backup model is set up."
        ),
    },
    "kernel_recovery_context_window": {
        "de": "Der Modellkontext ist zu groß. Ich nehme das größte Werkzeugergebnis aus der Ansicht; es bleibt vollständig gesichert und nachlesbar.",
        "en": "The model context is too large. I am taking the largest tool result out of the view; it stays fully preserved and readable.",
    },
    "kernel_recovery_tool_arguments": {
        "de": "Das Modell hat ungültige Werkzeugargumente geliefert. Aus dieser Antwort wurde kein Werkzeug ausgeführt; ich lasse den Aufruf korrigieren.",
        "en": "The model returned invalid tool arguments. No tool from that response was executed; I am requesting a corrected call.",
    },
    "kernel_recovery_required_tool": {
        "de": "Das Modell hat in Text geantwortet, wo ein Werkzeugaufruf nötig war. Ich lasse den Aufruf nachholen.",
        "en": "The model answered in text where a tool call was required. I am asking it for the call.",
    },
    "kernel_recovery_context_budget": {
        "de": "Die Ergebnisse sind zu groß für den verfügbaren Modellkontext. Ich nehme die größten aus der Ansicht; die Originale bleiben nachlesbar.",
        "en": "The results do not fit the available model context. I am taking the largest out of the view; the originals remain readable.",
    },
    "kernel_context_source_read_preview": {
        "de": "Original aus dem Gespräch",
        "en": "Conversation original",
    },
    "kernel_context_source_list_preview": {
        "de": "Verfügbarer Gesprächskontext",
        "en": "Available conversation sources",
    },
    "kernel_archive_read_preview": {
        "de": "Archivierter Text",
        "en": "Archived text",
    },
    "kernel_archive_range_preview": {
        "de": "Archivierter Text ab Zeichen {position}",
        "en": "Archived text from character {position}",
    },
    "kernel_archive_catalogue_preview": {
        "de": "Archivübersicht",
        "en": "Archive catalogue",
    },
    "kernel_archive_catalogue_next_preview": {
        "de": "Archivübersicht fortsetzen",
        "en": "Continue archive catalogue",
    },
    "kernel_document_read_preview": {
        "de": "Dokumenttext von Anfang",
        "en": "Document text from the start",
    },
    "kernel_document_range_preview": {
        "de": "Dokumenttext ab Zeichen {position}",
        "en": "Document text from character {position}",
    },
    "kernel_document_comments_next_preview": {
        "de": "Weitere Kommentare",
        "en": "Further comments",
    },
    "kernel_cite_passages_preview_one": {
        "de": "{window}, {count} Stelle",
        "en": "{window}, {count} passage",
    },
    "kernel_cite_passages_preview_other": {
        "de": "{window}, {count} Stellen",
        "en": "{window}, {count} passages",
    },
    "kernel_context_compacted": {
        "de": (
            "Kontext komprimiert: ältere Schritte zusammengefasst und im "
            "Lauf-Archiv gesichert."
        ),
        "en": (
            "Context compacted: older steps summarized and kept in the run "
            "archive."
        ),
    },
    "agent_memory_label_unavailable": {
        "de": "Memory nicht verfügbar", "en": "Memory unavailable",
    },
    "agent_memory_detail_unavailable": {
        "de": "Langzeit-Memory ist für diesen Lauf nicht nutzbar.",
        "en": "Long-term memory is unavailable for this run.",
    },
    "agent_memory_detail_candidates_failed": {
        "de": "Memory-Kandidaten konnten nicht erzeugt werden.",
        "en": "Memory candidates could not be generated.",
    },
    "agent_critic_revision_discarded_label": {
        "de": "Prüfung: Überarbeitung verworfen",
        "en": "Review: revision discarded",
    },
    "agent_critic_revision_partially_discarded_label": {
        "de": "Prüfung: Überarbeitung teilweise verworfen",
        "en": "Review: revision partly discarded",
    },
    # ---- server/editor_suggestions.py — suggestion validation warnings ----
    "editor_warning_unchanged": {
        "de": "Der Vorschlag entspricht unverändert dem markierten Text.",
        "en": "The suggestion is unchanged from the selected text.",
    },
    "editor_excerpt_only_note": {
        "de": (
            "Für diesen Vorschlag wurde nur der relevante Ausschnitt des Berichts "
            "berücksichtigt."
        ),
        "en": (
            "Only the relevant excerpt of the report was considered for this "
            "suggestion."
        ),
    },
    # ---- server/editor_instructions.py — an edit that wrote a request pointer ----
    "editor_pointer_edit_discarded": {
        "de": (
            "Ein Änderungsvorschlag wurde verworfen: Er hätte den internen "
            "Verweis „{pointer}“ in den Text geschrieben."
        ),
        "en": (
            "A proposed edit was discarded: it would have written the internal "
            "reference “{pointer}” into the text."
        ),
    },
    "knowledge_no_relevant_content": {
        "de": (
            "In den durchsuchten Dokumenten wurden keine relevanten Inhalte zu "
            "dieser Frage gefunden."
        ),
        "en": (
            "No relevant content for this question was found in the searched "
            "documents."
        ),
    },

    # ---- nodes.py — query-planning strategy hint ----
    "research_strategy_deep_perspectives": {"de": "Pflichtperspektiven", "en": "Required perspectives"},
    "research_strategy_storm_perspectives": {"de": "Perspektivenvielfalt", "en": "Perspective diversity"},
    "research_strategy_falsification": {"de": "Falsifikation", "en": "Falsification"},
    "research_strategy_alternative": {"de": "Alternative Hypothesen", "en": "Alternative hypotheses"},
    "research_strategy_competing": {"de": "Konkurrierende Erklärungen", "en": "Competing explanations"},
    "research_strategy_reformulation": {"de": "Reformulierung", "en": "Reformulation"},
    "research_strategy_crosscheck": {"de": "Gegenprüfung", "en": "Cross-checks"},
    "research_query_basis_one": {"de": "aus {count} Analyseziel", "en": "from {count} analysis target"},
    "research_query_basis_other": {"de": "aus {count} Analysezielen", "en": "from {count} analysis targets"},
    "research_query_basis_open": {"de": "{count} noch offen", "en": "{count} still open"},

    # ---- nodes.py — research engine, reader-facing ----
    "research_gaps_evaluation_incomplete": {
        "de": "Automatische Qualitätsbewertung unvollständig; Antwort vorsichtig formulieren.",
        "en": "Automatic quality assessment incomplete; phrase the answer cautiously.",
    },
    "research_gap_no_structured_claims": {
        "de": "Evidence-Pipeline konnte keine strukturierten Claims aus den Suchergebnissen ableiten.",
        "en": "The evidence pipeline could not derive structured claims from the search results.",
    },
    "research_gap_no_report_bundles": {
        "de": "Claims vorhanden, aber keiner ist gestützt oder umstritten.",
        "en": "Claims are present, but none is supported or contested.",
    },
    "research_gap_primary_missing": {
        "de": "Zentrale Zahlen/Regelungen nicht mit Primärquelle belegt.",
        "en": "Key figures and rules are not backed by a primary source.",
    },
    "research_gap_aspects_open": {
        "de": "Teilfragen offen: {items}",
        "en": "Sub-questions still open: {items}",
    },
    "research_plan_query_fallback_reason": {
        "de": "keine eigene Suchanfrage erhalten",
        "en": "no search query of its own returned",
    },
    # The report that stands in for a blocked one. Reader text up to the
    # section for operators; the failure messages and the settings to look
    # at stand there, verbatim and as code.
    "research_algofail_heading": {
        "de": "## Kein Bericht erstellt",
        "en": "## No report written",
    },
    "research_algofail_intro": {
        "de": "Ein Schritt, den der Bericht braucht, ist fehlgeschlagen. Der Bericht wurde deshalb nicht geschrieben, damit ein Fehler nicht wie ein vollständiges Ergebnis aussieht.",
        "en": "A step the report needs failed. The report was therefore not written, so that a failure cannot pass for a complete result.",
    },
    "research_algofail_diagnosis": {
        "de": "## Diagnose",
        "en": "## Diagnosis",
    },
    "research_algofail_run_id": {
        "de": "- Laufkennung: `{value}`",
        "en": "- Run id: `{value}`",
    },
    "research_algofail_rounds": {
        "de": "- Rechercherunden: `{value}`",
        "en": "- Research rounds: `{value}`",
    },
    "research_algofail_references": {
        "de": "- Referenzen gesammelt: `{value}`",
        "en": "- References collected: `{value}`",
    },
    "research_algofail_evidence_records": {
        "de": "- Belege der Recherche: `{value}`",
        "en": "- Evidence records of the research: `{value}`",
    },
    "research_algofail_claim_calls": {
        "de": "- Aufrufe der Aussagenerfassung: `{value}`",
        "en": "- Statement extraction calls: `{value}`",
    },
    "research_algofail_claim_failures": {
        "de": "- Fehlgeschlagene Aussagenerfassungen: `{value}`",
        "en": "- Failed statement extractions: `{value}`",
    },
    "research_algofail_blocking": {
        "de": "## Für den Betrieb: blockierende Fehler",
        "en": "## For operators: blocking failures",
    },
    "research_algofail_failure_line": {
        "de": "- `{phase}` in Runde `{round}`: `{reason}`",
        "en": "- `{phase}` in round `{round}`: `{reason}`",
    },
    "research_algofail_next_step": {
        "de": "### Nächster Schritt",
        "en": "### Next step",
    },
    "research_algofail_next_step_text": {
        "de": "Das Serverprotokoll nennt jeden dieser Fehler mit Phase und Grund; mit `OBSERVABILITY_PROFILE=forensic` und einer Trace-Senke (`INQTRIX_TRACING=file` oder `otlp`) trägt auch der Trace des Laufs die Ereignisse `algorithm_failure` mit ihren Einzelheiten.",
        "en": "The server log names each of these failures with its phase and reason; with `OBSERVABILITY_PROFILE=forensic` and a trace sink (`INQTRIX_TRACING=file` or `otlp`), the run's trace also carries the `algorithm_failure` events with their details.",
    },
    # The header of an answer without a written report, in two versions: with
    # the material the research gathered beneath it, and without any.
    "research_answer_fallback_warning": {
        "de": "> [!WARNING] Kein geschriebener Bericht\n> {reason}\n> Darunter steht das bisher gesammelte Material der Recherche in Rohform, kein vom Modell geschriebener Bericht. Für den Betrieb: Das Iterationsprotokoll führt diese Antwort mit `_answer_fallback=true`.",
        "en": "> [!WARNING] No written report\n> {reason}\n> Below is the material the research has gathered so far, in raw form, not a report written by the model. For operators: the iteration log marks this answer with `_answer_fallback=true`.",
    },
    "research_answer_fallback_warning_no_material": {
        "de": "> [!WARNING] Kein geschriebener Bericht\n> {reason}\n> Es liegt noch kein gesammeltes Material vor. Für den Betrieb: Das Iterationsprotokoll führt diese Antwort mit `_answer_fallback=true`.",
        "en": "> [!WARNING] No written report\n> {reason}\n> No material has been gathered yet. For operators: the iteration log marks this answer with `_answer_fallback=true`.",
    },
    "research_answer_timeout_reason": {
        "de": "Das Schreiben des Berichts hat die Zeitgrenze des Modells überschritten.",
        "en": "Writing the report exceeded the model's time limit.",
    },
    # What follows the header without material.
    "research_no_context_timeout": {
        "de": "Bitte erneut versuchen; für den Betrieb: gegebenenfalls `REASONING_TIMEOUT` erhöhen oder `MAX_TOTAL_SECONDS` anpassen.",
        "en": "Please try again; for operators: raise `REASONING_TIMEOUT` or adjust `MAX_TOTAL_SECONDS` if needed.",
    },
    "research_no_context_provider": {
        "de": "Bitte erneut versuchen und die Verbindung zum Modellanbieter prüfen.",
        "en": "Please try again and check the connection to the model provider.",
    },

    "research_rate_limited_answer": {
        "de": (
            "Die Recherche wurde abgebrochen: {error}\n\nBitte später erneut "
            "versuchen (Tages-Token-Limit erreicht)."
        ),
        "en": (
            "The research run was aborted: {error}\n\nPlease try again later "
            "(daily token limit reached)."
        ),
    },
    "chat_rate_limited_answer": {
        "de": (
            "Die Anfrage wurde abgebrochen: {error}\n\nBitte später erneut "
            "versuchen (Tages-Token-Limit erreicht)."
        ),
        "en": (
            "The request was aborted: {error}\n\nPlease try again later "
            "(daily token limit reached)."
        ),
    },

    # ---- agents/telemetry.py — retry activity, shown on a disruption ----
    "telemetry_retry_text": {
        "de": "Beim Anbieter ist Versuch {failed}/{max} fehlgeschlagen ({code}); Versuch {next}/{max} startet nach {delay} s.",
        "en": "Provider attempt {failed}/{max} failed ({code}); attempt {next}/{max} starts after {delay} s.",
    },
    "telemetry_skill_point_check": {
        "de": "Skill-Eingaben werden geprüft",
        "en": "Skill inputs are being checked",
    },
    "telemetry_sufficiency": {
        "de": "Beleglage wird bewertet",
        "en": "The evidence base is being assessed",
    },
    "telemetry_citation_repair": {
        "de": "Quellenverweise werden geprüft",
        "en": "Source references are being checked",
    },
    "telemetry_memory_reflection": {
        "de": "Sitzungskontext wird aktualisiert",
        "en": "The session context is being updated",
    },
    "telemetry_file_analysis": {
        "de": "Dateiinhalte werden ausgewertet",
        "en": "File contents are being evaluated",
    },
    "telemetry_kernel": {
        "de": "Agent-Antwort wird fortgesetzt",
        "en": "The agent answer is being continued",
    },
    "telemetry_context_compaction": {
        "de": "Ältere Schritte werden zusammengefasst",
        "en": "Earlier steps are being summarized",
    },
    "telemetry_deep_review": {
        # One review for every Stufe that reviews: the wording names the
        # step, not the depth the user chose.
        "de": "Ergebnis wird geprüft",
        "en": "The result is being reviewed",
    },
    "telemetry_model": {
        "de": "Modellschritt wird fortgesetzt",
        "en": "The model step is being continued",
    },

    "kernel_batchguard_ask_first": {
        "de": "Rückfrage zuerst — der Unterauftrag startet nach der Antwort.",
        "en": "Clarification first — the subtask starts after your answer.",
    },
    "kernel_batchguard_bundled": {
        "de": (
            "Mehrere Unteraufträge in einem Zug abgefangen — der Agent bündelt "
            "sie nun über delegate_batch."
        ),
        "en": (
            "Several subtasks in one turn were caught — the agent now bundles "
            "them through delegate_batch."
        ),
    },
    # Transport-neutral on purpose: the review runs as one call or as a
    # bounded role, and a failure text that names the role would report a
    # child run that never existed.
    "kernel_review_unavailable": {
        "de": "Prüfung nicht gestartet — bestehende Outputs bleiben unverändert.",
        "en": "Review not started — the existing outputs stay unchanged.",
    },
    "kernel_review_failed": {
        "de": "Prüfung abgebrochen — bestehende Outputs bleiben unverändert.",
        "en": "Review broke off — the existing outputs stay unchanged.",
    },
    "kernel_review_result_missing": {
        "de": "Prüfung lieferte kein typisiertes Ergebnis — bestehende Outputs bleiben unverändert.",
        "en": "The review delivered no typed result — the existing outputs stay unchanged.",
    },
    "kernel_review_schema_invalid": {
        "de": "Prüfergebnis ungültig — bestehende Outputs bleiben unverändert.",
        "en": "The review result was invalid — the existing outputs stay unchanged.",
    },
    "kernel_review_budget_exhausted": {
        "de": "Prüfbudget erschöpft, Prüfung unvollständig — bestehende Outputs bleiben unverändert.",
        "en": "Review budget exhausted, review incomplete — the existing outputs stay unchanged.",
    },
    "kernel_review_context": {
        "de": "Prüfmaterial passt in kein Modellfenster — bestehende Outputs bleiben unverändert.",
        "en": "The review material fits no model window — the existing outputs stay unchanged.",
    },
    "review_diagnostic_document_analysis": {
        "de": "Lesedaten der Teilaufgabe ‚{title}‘",
        "en": "Reading record of the subtask ‘{title}’",
    },
    "review_diagnostic_research_run": {
        "de": "Belegbilanz des Ursprungslaufs",
        "en": "Evidence record of the origin run",
    },
    "review_diagnostic_mission_task": {
        "de": "Belegbilanz der Teilaufgabe ‚{title}‘",
        "en": "Evidence record of the subtask ‘{title}’",
    },
    "review_diagnostic_run_analysis": {
        "de": "Dokumentanalyse dieses Laufs ‚{title}‘",
        "en": "Document analysis of this run ‘{title}’",
    },
    "review_diagnostic_run_reading": {
        "de": "Lesestand dieses Laufs für ‚{title}‘",
        "en": "Reading record of this run for ‘{title}’",
    },
    # What a reader sees where the review would otherwise print an id. Every
    # name is quoted, so it fits whatever case the sentence puts it in.
    "review_name_quoted": {
        "de": "„{name}“", "en": "“{name}”",
    },
    "review_name_source_untitled": {
        "de": "Quelle ohne Titel", "en": "source without a title",
    },
    "review_name_diagnostic_untitled": {
        "de": "Laufdiagnose ohne Titel", "en": "run diagnostic without a title",
    },
    "review_name_canvas_untitled": {
        "de": "Canvas ohne Titel", "en": "canvas without a title",
    },
    # The finding's target line. An untitled canvas is named ONCE: the
    # prefix carries the noun, the untitled variant does not repeat it.
    "review_findings_chat": {
        "de": "In der Antwort: ", "en": "In the answer: ",
    },
    "review_findings_canvas": {
        "de": "Canvas „{title}“: ", "en": "Canvas “{title}”: ",
    },
    "review_findings_canvas_untitled": {
        "de": "Canvas ohne Titel: ", "en": "Canvas without a title: ",
    },
    "review_name_this_run": {
        "de": "dieser Lauf", "en": "this run",
    },
    "review_name_origin_run": {
        "de": "Ursprungslauf", "en": "origin run",
    },
    "review_name_search": {
        "de": "Suche ‚{query}‘", "en": "search ‘{query}’",
    },
    "review_name_search_plain": {
        "de": "Suche ohne Fragetext", "en": "search without a question",
    },
    "review_name_unassignable": {
        "de": "nicht zuordenbare Grundlage", "en": "unassignable basis",
    },
    "review_unavailable_generic": {
        "de": "Die Prüfung lieferte kein Urteil; der Grund steht im "
              "Serverprotokoll.",
        "en": "The review delivered no verdict; the reason is in the "
              "server log.",
    },
    "review_unavailable_failed": {
        "de": "Die Prüfung brach ab, bevor sie ein Urteil abgeben konnte; "
              "der Grund steht im Serverprotokoll.",
        "en": "The review broke off before it could deliver a verdict; the "
              "reason is in the server log.",
    },
    "review_unavailable_result_missing": {
        "de": "Die Prüfung lief zu Ende, gab aber kein typisiertes Urteil "
              "ab; der Grund steht im Serverprotokoll.",
        "en": "The review ran to the end but delivered no typed verdict; "
              "the reason is in the server log.",
    },
    "review_unavailable_context": {
        "de": "Das Prüfmaterial passt in kein Modellfenster; der "
              "Grund steht im Serverprotokoll.",
        "en": "The review material fits no model window; the reason is in "
              "the server log.",
    },
    "review_unavailable_unchanged_store_error": {
        "de": "Das Prüfergebnis ließ sich nicht speichern; der Grund "
              "steht im Serverprotokoll.",
        "en": "The review result could not be stored; the reason is in "
              "the server log.",
    },
    "review_unavailable_budget_exhausted": {
        "de": "Die Prüfung erreichte ihr Budget, bevor sie ein Urteil "
              "abgeben konnte; der Grund steht im Serverprotokoll.",
        "en": "The review reached its budget before it could deliver a "
              "verdict; the reason is in the server log.",
    },
    "review_unavailable_schema_invalid": {
        "de": "Die Prüfung lieferte kein auswertbares Ergebnis; der Grund "
              "steht im Serverprotokoll.",
        "en": "The review returned no readable result; the reason is in the "
              "server log.",
    },
    "review_basis_unconfirmed": {
        "de": "Befund ohne bestätigte Grundlage, nicht angewendet: {finding}",
        "en": "Finding without a confirmed basis, not applied: {finding}",
    },
    # One finding the outputs did not take over, and why: the reason is one
    # code of review_edits.FINDING_REASONS, each with its sentence below.
    "review_finding_named": {
        "de": "Befund nicht angewendet ({reason}): {finding}",
        "en": "Finding not applied ({reason}): {finding}",
    },
    "review_finding_reason_finding_invalid": {
        "de": "Ziel oder Wortlaut des Befunds ungültig",
        "en": "the finding's target or wording is invalid",
    },
    "review_finding_reason_basis_invalid": {
        "de": "Grundlage passt nicht zur Ausgabe oder zur Art des Befunds",
        "en": "the basis does not fit the output or the kind of finding",
    },
    "review_finding_reason_basis_outside_output": {
        "de": "Grundlage gehört nicht zu den Belegen dieses Dokuments",
        "en": "the basis is not among this document's references",
    },
    "review_finding_reason_remove_labels_invalid": {
        "de": "zu entfernender Beleg steht nicht in der Stelle",
        "en": "a citation to remove is not in the passage",
    },
    "review_finding_reason_anchor_missing": {
        "de": "zitierte Stelle steht nicht im Text",
        "en": "the quoted passage is not in the text",
    },
    "review_finding_reason_anchor_ambiguous": {
        "de": "zitierte Stelle steht mehrfach im Text",
        "en": "the quoted passage occurs more than once",
    },
    "review_finding_reason_anchor_overlap": {
        "de": "Stelle überschneidet sich mit einem früheren Befund",
        "en": "the passage overlaps an earlier finding",
    },
    "review_finding_reason_provider_context_only": {
        "de": "nur am Text des Suchanbieters messbar; Stelle markiert",
        "en": "measurable only against the search provider's text; passage marked",
    },
    # The line of a finding that marked its passage: it asked for no
    # revision, so it reads as what the mark says, not as a finding the
    # revision failed to apply.
    "review_finding_marked": {
        "de": "Nicht ausreichend belegt, nur am Text des Suchanbieters messbar: {finding}",
        "en": "Insufficiently supported, measurable only against the search provider's text: {finding}",
    },
    # The note of a statement marked as insufficiently supported, one per
    # reason of claim_states.MARK_REASONS; shown where the mark sits.
    "claim_mark_finding": {
        "de": "Nicht ausreichend belegt: {finding}",
        "en": "Insufficiently supported: {finding}",
    },
    "claim_mark_metadata_only": {
        "de": (
            "Nicht ausreichend belegt: Von der zitierten Quelle liegen nur Angaben wie "
            "Titel und Adresse vor, kein Text."
        ),
        "en": (
            "Insufficiently supported: of the cited source only details such as its "
            "title and address are held, none of its text."
        ),
    },
    "claim_mark_restates": {
        "de": (
            "Nicht ausreichend belegt: Die Aussage wiederholt einen Wert, den die "
            "Prüfung an anderer Stelle als nicht ausreichend belegt markiert hat."
        ),
        "en": (
            "Insufficiently supported: the statement repeats a value the review "
            "marked as insufficiently supported elsewhere."
        ),
    },
    "review_finding_reason_edit_not_single": {
        "de": "Überarbeitung lieferte keine eindeutige Ersetzung",
        "en": "the revision returned no single replacement",
    },
    "review_finding_reason_edit_unchanged": {
        "de": "Überarbeitung ließ die Stelle unverändert",
        "en": "the revision left the passage unchanged",
    },
    "review_finding_reason_edit_deletes": {
        "de": "Ersetzung hätte Inhalt ersatzlos gestrichen",
        "en": "the replacement would have deleted content without a replacement",
    },
    "review_finding_reason_edit_shrinks": {
        "de": "Ersetzung hätte die Stelle auf einen Rest gekürzt",
        "en": "the replacement would have cut the passage to a remnant",
    },
    "review_finding_reason_edit_rewrites_unchecked": {
        "de": "Ersetzung hätte einen Satz ohne gespeicherten Quelltext umformuliert",
        "en": "the replacement would have reworded a sentence without stored source text",
    },
    "review_finding_reason_edit_drops_figure": {
        "de": "Ersetzung hätte eine Zahl der Stelle gestrichen",
        "en": "the replacement would have dropped a number of the passage",
    },
    "review_finding_reason_identifier_added": {
        "de": "Ersetzung hätte eine interne Kennung gezeigt",
        "en": "the replacement would have shown an internal identifier",
    },
    "review_finding_reason_citation_malformed": {
        "de": "Ersetzung enthält eine unlesbare Belegklammer",
        "en": "the replacement holds an unreadable citation bracket",
    },
    "review_finding_reason_label_not_citable": {
        "de": "Ersetzung zitiert einen Beleg außerhalb dieses Befunds",
        "en": "the replacement cites a source outside this finding",
    },
    "review_finding_reason_quote_label_dropped": {
        "de": "Ersetzung entfernt einen Beleg der Stelle",
        "en": "the replacement drops a citation of the passage",
    },
    "review_finding_reason_label_moved": {
        "de": "Ersetzung setzt einen Beleg an einen anderen Satz",
        "en": "the replacement moves a citation to another sentence",
    },
    # A sentence counts as new when the passage did not hold its text: an
    # added sentence, or a kept one the edit reworded.
    "review_finding_reason_segment_without_basis_label": {
        "de": "neuer oder umformulierter Satz ohne Beleg aus der Grundlage des Befunds",
        "en": "a new or reworded sentence without a citation of the finding's basis",
    },
    "review_finding_reason_labels_whole_text": {
        "de": "Einsetzen machte eine Belegklammer im Text unlesbar",
        "en": "splicing it in broke a citation in the text",
    },
    "review_finding_reason_output_emptied": {
        "de": "Einsetzen hätte die Ausgabe geleert",
        "en": "splicing it in would have emptied the output",
    },
    "review_finding_reason_output_shrunk": {
        "de": "Einsetzen hätte die Ausgabe zu stark gekürzt",
        "en": "splicing it in would have cut too much of the output",
    },
    "review_finding_reason_revision_context": {
        "de": "Überarbeitung passte in kein Modellfenster",
        "en": "the revision fitted no model window",
    },
    "review_finding_reason_revision_failed": {
        "de": "Überarbeitung fehlgeschlagen",
        "en": "the revision failed",
    },
    "review_finding_reason_revision_empty": {
        "de": "Überarbeitung ohne Ergebnis",
        "en": "the revision returned nothing",
    },
    "review_finding_reason_write_conflict": {
        "de": "Dokument wurde zwischenzeitlich geändert",
        "en": "the document changed meanwhile",
    },
    "review_finding_reason_store_error": {
        "de": "Speicherfehler",
        "en": "store error",
    },
    "review_finding_reason_cancelled": {
        "de": "Lauf abgebrochen",
        "en": "run cancelled",
    },
    "review_finding_reason_revision_error": {
        "de": "Überarbeitung brach mit einem Fehler ab",
        "en": "the revision broke off with an error",
    },
    # An attached rule as the review's reader lines name it: the title it was
    # attached under and the revision the run is frozen at.
    "review_rule_name": {
        "de": "Regel „{title}“ (Rev. {revision})",
        "en": "Rule “{title}” (rev. {revision})",
    },
    # A rule marker two attached rules share names their common handle.
    "review_rule_handle": {
        "de": "Regel „{label}“",
        "en": "Rule “{label}”",
    },
    "review_criterion_open": {
        "de": "Kriterium offen: {criterion} — {gap}",
        "en": "Criterion open: {criterion} — {gap}",
    },
    "review_criterion_open_plain": {
        "de": "Kriterium offen: {criterion}",
        "en": "Criterion open: {criterion}",
    },
    "review_criterion_unsettled": {
        "de": "Kriterium von der Prüfung nicht abgerechnet: {criterion}",
        "en": "Criterion not settled by the review: {criterion}",
    },
    # A point the review could not check, when an edit of that review was
    # applied afterwards: it describes the reviewed draft, and the delivered
    # text may no longer hold what it names.
    "review_point_before_revision": {
        "de": "Vor der Überarbeitung festgestellt: {point}",
        "en": "Noted before the revision: {point}",
    },
    # An unmet criterion whose findings' edits were all applied: worked on in the
    # reviewed draft, never shown met, and without the draft's gap, which the
    # delivered text may no longer hold.
    "review_criterion_revised_one": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die Änderung dazu ist angewendet, "
            "nicht nachgeprüft: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its edit is applied, not "
            "re-checked: {criterion}"
        ),
    },
    "review_criterion_revised_other": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die {count} Änderungen dazu sind "
            "angewendet, nicht nachgeprüft: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its {count} edits are "
            "applied, not re-checked: {criterion}"
        ),
    },
    # The same, and the re-check held every one of those edits: still no
    # claim that the criterion is met.
    "review_criterion_verified_one": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die Änderung dazu ist angewendet "
            "und nachgeprüft: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its edit is applied and "
            "re-checked: {criterion}"
        ),
    },
    "review_criterion_verified_other": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die {count} Änderungen dazu sind "
            "angewendet und nachgeprüft: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its {count} edits are "
            "applied and re-checked: {criterion}"
        ),
    },
    # The same in a run without stored evidence: nothing re-checks an edit,
    # so the line says where it came from.
    "review_criterion_knowledge_one": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die Änderung dazu ist angewendet, "
            "aus Modellwissen ohne Quellen: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its edit is applied, from "
            "general knowledge without sources: {criterion}"
        ),
    },
    "review_criterion_knowledge_other": {
        "de": (
            "Kriterium im geprüften Entwurf offen; die {count} Änderungen dazu sind "
            "angewendet, aus Modellwissen ohne Quellen: {criterion}"
        ),
        "en": (
            "Criterion open in the reviewed draft; its {count} edits are "
            "applied, from general knowledge without sources: {criterion}"
        ),
    },
    # The outcome record's summary (run_outcome.outcome_words): closed
    # vocabulary joined by " · ", built from the record's counts only. Only
    # an applied edit the re-check held is "behoben".
    "outcome_reviewed": {"de": "Geprüft", "en": "Reviewed"},
    # A review of a run without stored evidence, named once in its place.
    "outcome_reviewed_knowledge": {
        "de": "Geprüft aus Modellwissen, ohne Quellen",
        "en": "Reviewed from general knowledge, without sources",
    },
    # Every cited reference carries the search provider's context alone.
    "outcome_reviewed_provider": {
        "de": "Gegen Anbieterauswertungen geprüft, Originalseiten nicht gelesen",
        "en": "Reviewed against the search provider's summaries, original pages not read",
    },
    "outcome_no_findings": {"de": "ohne Befund", "en": "no findings"},
    "outcome_verified_one": {
        "de": "{count} Befund behoben und nachgeprüft",
        "en": "{count} finding fixed and re-checked",
    },
    "outcome_verified_other": {
        "de": "{count} Befunde behoben und nachgeprüft",
        "en": "{count} findings fixed and re-checked",
    },
    "outcome_applied_one": {
        "de": "{count} Befund überarbeitet, nicht nachgeprüft",
        "en": "{count} finding revised, not re-checked",
    },
    "outcome_applied_other": {
        "de": "{count} Befunde überarbeitet, nicht nachgeprüft",
        "en": "{count} findings revised, not re-checked",
    },
    # Applied edits of a review from general knowledge: no source exists to
    # re-check them against, so neither word about a re-check applies.
    "outcome_revised_one": {
        "de": "{count} Befund überarbeitet", "en": "{count} finding revised",
    },
    "outcome_revised_other": {
        "de": "{count} Befunde überarbeitet", "en": "{count} findings revised",
    },
    "outcome_applied_reasons": {"de": "{applied} ({reasons})", "en": "{applied} ({reasons})"},
    # Why the re-check left revised findings unverified, one entry per
    # reason of review_edits.RECHECK_REASONS, counted in the summary.
    "outcome_reason_no_source_text": {
        "de": "{count} mit Belegen ohne gespeicherten Quelltext",
        "en": "{count} citing sources without stored text",
    },
    "outcome_reason_format_only": {
        "de": "{count} nur Formatierung", "en": "{count} formatting only",
    },
    "outcome_reason_too_large": {
        "de": "{count} zu umfangreich", "en": "{count} too large to re-check",
    },
    "outcome_reason_check_unavailable": {
        "de": "{count} ohne mögliche Nachprüfung", "en": "{count} with no re-check possible",
    },
    "outcome_reason_inconclusive": {
        "de": "{count} ohne eindeutiges Prüfergebnis", "en": "{count} with an inconclusive re-check",
    },
    "outcome_reason_knowledge_only": {
        "de": "{count} aus Modellwissen", "en": "{count} from general knowledge",
    },
    "outcome_named_one": {
        "de": "{count} Befund nicht angewendet", "en": "{count} finding not applied",
    },
    "outcome_named_other": {
        "de": "{count} Befunde nicht angewendet", "en": "{count} findings not applied",
    },
    "outcome_not_checked_one": {
        "de": "{count} Punkt nicht geprüft", "en": "{count} point not checked",
    },
    "outcome_not_checked_other": {
        "de": "{count} Punkte nicht geprüft", "en": "{count} points not checked",
    },
    "outcome_criteria_open_one": {
        "de": "{count} Kriterium offen", "en": "{count} criterion open",
    },
    "outcome_criteria_open_other": {
        "de": "{count} Kriterien offen", "en": "{count} criteria open",
    },
    "outcome_reach": {
        "de": "{read} von {cited} nachgelesen", "en": "{read} of {cited} re-read",
    },
    "outcome_provider_only_one": {
        "de": "{count} Beleg nur mit Anbieter-Kontext, ohne gespeicherten Quelltext",
        "en": "{count} reference with search-provider context only, no stored source text",
    },
    "outcome_provider_only_other": {
        "de": "{count} Belege nur mit Anbieter-Kontext, ohne gespeicherten Quelltext",
        "en": "{count} references with search-provider context only, no stored source text",
    },
    # The plaque: the delivered statements counted by their state
    # (claim_states.STATES), in that order, each state only when it counts.
    "outcome_claims_one": {"de": "{count} Aussage", "en": "{count} statement"},
    "outcome_claims_other": {"de": "{count} Aussagen", "en": "{count} statements"},
    "outcome_claims_states": {"de": "{claims}: {states}", "en": "{claims}: {states}"},
    "outcome_claim_verified_one": {"de": "{count} verifiziert", "en": "{count} verified"},
    "outcome_claim_verified_other": {"de": "{count} verifiziert", "en": "{count} verified"},
    "outcome_claim_source_text_one": {
        "de": "{count} mit Quelltext", "en": "{count} with source text",
    },
    "outcome_claim_source_text_other": {
        "de": "{count} mit Quelltext", "en": "{count} with source text",
    },
    "outcome_claim_attributed_one": {
        "de": "{count} vom Anbieter zugeschrieben", "en": "{count} attributed by the search provider",
    },
    "outcome_claim_attributed_other": {
        "de": "{count} vom Anbieter zugeschrieben", "en": "{count} attributed by the search provider",
    },
    "outcome_claim_insufficient_one": {
        "de": "{count} nicht ausreichend belegt", "en": "{count} insufficiently supported",
    },
    "outcome_claim_insufficient_other": {
        "de": "{count} nicht ausreichend belegt", "en": "{count} insufficiently supported",
    },
    "outcome_waived": {"de": "Ohne Prüfung (Stufe)", "en": "No review (tier)"},
    "outcome_unavailable": {
        "de": "Prüfung nicht auswertbar, Ergebnis ungeprüft",
        "en": "Review unavailable, result unchecked",
    },
    "outcome_review_failed": {
        "de": "Prüfung gescheitert, Ergebnis ungeprüft",
        "en": "Review failed, result unchecked",
    },
    "outcome_review_budget": {
        "de": "Prüfung am Budget beendet, Ergebnis ungeprüft",
        "en": "Review stopped at its budget, result unchecked",
    },
    # An inconsistent verdict: a negative flag without any reason, named.
    "outcome_review_inconsistent": {
        "de": "Prüfurteil widersprüchlich: {flags} ohne Begründung",
        "en": "Review verdict contradictory: {flags} without a reason",
    },
    "review_flag_complete": {"de": "Vollständigkeit", "en": "completeness"},
    "review_flag_grounded": {"de": "Belegtreue", "en": "grounding"},
    "review_flag_contradictions_named": {"de": "Gegenpositionen", "en": "opposing views"},
    "outcome_not_run": {"de": "Ohne Prüfung beendet", "en": "Ended without a review"},
    "outcome_ended_before_review": {
        "de": "Vor der Prüfung beendet", "en": "Ended before the review",
    },
    "review_waived_label": {
        "de": "Verifikation: in der Stufe „{tier}“ nicht vorgesehen",
        "en": "Verification: not part of the '{tier}' tier",
    },
    "review_waived_label_legacy": {
        "de": "Verifikation: nur im Tiefmodus vorgesehen",
        "en": "Verification: only part of the deep mode",
    },
    "review_unavailable_label": {
        "de": "Verifikation geschuldet, ohne verwertbares Urteil beendet",
        "en": "Verification was owed and ended without a usable verdict",
    },
    "review_coverage_cited_one": {
        "de": "{count} zitierten Beleg", "en": "{count} cited reference",
    },
    "review_coverage_cited_other": {
        "de": "{count} zitierten Belegen", "en": "{count} cited references",
    },
    "review_findings_label_one": {
        "de": "Verifikation: {count} Befund", "en": "Verification: {count} finding",
    },
    "review_findings_label_other": {
        "de": "Verifikation: {count} Befunde", "en": "Verification: {count} findings",
    },
    # Previews of a role's tool row: counts only, in the run's language.
    # The review counts what it REPORTED, confirmed or not.
    "agent_review_role_result_one": {
        "de": "{count} Befund gemeldet", "en": "{count} finding reported",
    },
    "agent_review_role_result_other": {
        "de": "{count} Befunde gemeldet", "en": "{count} findings reported",
    },
    "kernel_review_prefix": {
        "de": "Verifikations-Durchlauf: {text}",
        "en": "Verification pass: {text}",
    },
    "kernel_review_running": {
        "de": "Prüfung läuft.", "en": "Review in progress.",
    },
    "kernel_review_findings_revising_one": {
        "de": "{count} Befund — Überarbeitung läuft.",
        "en": "{count} finding — revision in progress.",
    },
    "kernel_review_findings_revising_other": {
        "de": "{count} Befunde — Überarbeitung läuft.",
        "en": "{count} findings — revision in progress.",
    },
    "kernel_partial_limit_name_tool_calls": {
        "de": "Werkzeuglimit", "en": "tool limit",
    },
    "kernel_partial_limit_name_steps": {
        "de": "Schrittlimit", "en": "step limit",
    },
    "kernel_partial_heading": {
        "de": "## Teilstand – ausdrücklich übernommen",
        "en": "## Partial result – explicitly accepted",
    },
    "kernel_partial_intro": {
        "de": (
            "Auf deine Entscheidung endet der Lauf hier als Teilstand, weil das "
            "{limit_name} erreicht wurde. Eine abschließende Antwort wurde nicht "
            "erzeugt."
        ),
        "en": (
            "On your decision the run ends here as a partial result, because the "
            "{limit_name} was reached. No final answer was produced."
        ),
    },
    "kernel_partial_documents_heading": {
        "de": "### Bereits im Canvas", "en": "### Already in the canvas",
    },
    "kernel_partial_evidence_heading": {
        "de": "### Gesicherte Belegbasis", "en": "### Secured evidence base",
    },
    "kernel_partial_no_source": {
        "de": "Bis zu diesem Punkt wurde noch keine zitierfähige Quelle gespeichert.",
        "en": "No citable source had been stored up to this point.",
    },
    "kernel_partial_source_fallback_label": {"de": "Quelle", "en": "Source"},
    "kernel_partial_source_untitled": {
        "de": "Quelle ohne Titel", "en": "Source without a title",
    },
    "kernel_partial_more_sources_one": {
        "de": "- {count} weitere Quelle bleibt im Belegprotokoll erhalten.",
        "en": "- {count} further source remains in the evidence log.",
    },
    "kernel_partial_more_sources_other": {
        "de": "- {count} weitere Quellen bleiben im Belegprotokoll erhalten.",
        "en": "- {count} further sources remain in the evidence log.",
    },
    "kernel_partial_gap_heading": {
        "de": "### Offene Lücke", "en": "### Open gap",
    },
    "kernel_partial_gap_text": {
        "de": (
            "Bewertung, Widerspruchsprüfung und abschließende Antwort fehlen. Für "
            "eine vollständige Antwort stelle den Auftrag mit einer höheren Stufe "
            "erneut oder grenze ihn ein."
        ),
        "en": (
            "Assessment, contradiction check and the final answer are missing. For "
            "a complete answer, ask again with a higher tier or narrow the "
            "assignment."
        ),
    },
    "kernel_citation_marker_unsupported": {
        "de": "unbelegt", "en": "unsupported",
    },
    "kernel_citation_removed_notice": {
        "de": "> Belegstatus: Nicht auflösbare Zitationslabels wurden entfernt: {markers}.",
        "en": "> Evidence status: Unresolvable citation labels were removed: {markers}.",
    },

    # ---- agents/limit_contract.py — the limit gate card ----
    "agent_limit_noun_tool_calls": {
        "de": "Werkzeugaufrufe", "en": "tool calls",
    },
    "agent_limit_noun_steps": {
        "de": "Arbeitsschritten", "en": "work steps",
    },
    "agent_limit_option_extend_label": {
        "de": "Auf {proposed} erweitern", "en": "Extend to {proposed}",
    },
    "agent_limit_option_extend_description": {
        "de": (
            "Der Lauf setzt am gespeicherten Stand fort; bereits verbrauchte "
            "Aufrufe und Tokens bleiben angerechnet."
        ),
        "en": (
            "The run continues from its saved state; calls and tokens already "
            "spent stay counted."
        ),
    },
    "agent_limit_option_partial_label": {
        "de": "Teilstand übernehmen", "en": "Take the partial result",
    },
    "agent_limit_option_partial_description": {
        "de": (
            "Der Lauf schließt mit den bisher gesicherten Belegen und einer "
            "klar gekennzeichneten Teilantwort ab."
        ),
        "en": (
            "The run finishes with the evidence secured so far and a clearly "
            "marked partial answer."
        ),
    },
    "agent_limit_option_cancel_label": {
        "de": "Lauf abbrechen", "en": "Cancel the run",
    },
    "agent_limit_option_cancel_description": {
        "de": "Der gespeicherte Lauf wird beendet.",
        "en": "The saved run is ended.",
    },
    "agent_limit_ceiling_extendable": {
        "de": (
            " Eine Erweiterung bis {proposed} ist innerhalb der Betreibergrenze "
            "{ceiling} möglich."
        ),
        "en": (
            " An extension up to {proposed} is possible within the operator "
            "limit {ceiling}."
        ),
    },
    "agent_limit_ceiling_fixed": {
        "de": " Eine Erweiterung ist hier nicht möglich.",
        "en": " It cannot be extended here.",
    },
    "agent_limit_reached_tool_calls": {
        "de": (
            "Der Agent hat bisher {used} {noun} verwendet. Der nächste atomare "
            "Werkzeug-Batch würde das aktuelle Limit {current} überschreiten. "
            "Kein Aufruf dieses Batches wurde ausgeführt."
        ),
        "en": (
            "The agent has used {used} {noun} so far. The next atomic tool "
            "batch would exceed the current limit {current}. No call in that "
            "batch was executed."
        ),
    },
    "agent_limit_reached_steps": {
        "de": "Der Agent hat seine Grenze von {current} {noun} erreicht.",
        "en": "The agent has reached its limit of {current} {noun}.",
    },
    "agent_limit_question_suffix": {
        "de": " Wie soll der Lauf fortgesetzt werden?",
        "en": " How should the run continue?",
    },
    "agent_limit_default_assumption": {
        "de": (
            "Ohne Entscheidung bleibt der Lauf pausiert; es gibt keinen "
            "automatischen Teilabschluss."
        ),
        "en": (
            "Without a decision the run stays paused; there is no automatic "
            "partial completion."
        ),
    },
    "search_notice_searches_one": {"de": "in einer Suche", "en": "in one search"},
    "search_notice_searches_other": {"de": "in {count} Suchen", "en": "in {count} searches"},
    "search_notice_reindex_one": {
        "de": (
            "Ein Treffer, dessen Originaltext sich nicht prüfen ließ, wurde "
            "ausgeschlossen ({searches}); solcher Inhalt muss neu indiziert werden."
        ),
        "en": (
            "One hit whose original text could not be verified was excluded "
            "({searches}); such content must be reindexed."
        ),
    },
    "search_notice_reindex_other": {
        "de": (
            "Treffer, deren Originaltext sich nicht prüfen ließ, wurden "
            "ausgeschlossen ({count} Ausschlüsse {searches}; derselbe Treffer "
            "kann mehrfach zählen); solche Inhalte müssen neu indiziert werden."
        ),
        "en": (
            "Hits whose original text could not be verified were excluded "
            "({count} exclusions {searches}; the same hit can count more than "
            "once); such content must be reindexed."
        ),
    },
    "search_notice_reconcile_one": {
        "de": (
            "Ein Treffer ohne abgeglichenen Datenbankeintrag wurde ausgeschlossen "
            "({searches}); der Indexabgleich muss erst abgeschlossen werden."
        ),
        "en": (
            "One hit without a reconciled database record was excluded "
            "({searches}); the index reconciliation must complete first."
        ),
    },
    "search_notice_reconcile_other": {
        "de": (
            "Treffer ohne abgeglichenen Datenbankeintrag wurden ausgeschlossen "
            "({count} Ausschlüsse {searches}; derselbe Treffer kann mehrfach "
            "zählen); der Indexabgleich muss erst abgeschlossen werden."
        ),
        "en": (
            "Hits without a reconciled database record were excluded ({count} "
            "exclusions {searches}; the same hit can count more than once); the "
            "index reconciliation must complete first."
        ),
    },
    "search_notice_excluded_one": {
        "de": (
            "Ein Treffer wurde durch eine Prüfung der Wissensquelle "
            "ausgeschlossen ({searches}); die Trefferliste kann unvollständig sein."
        ),
        "en": (
            "One hit was excluded by a check of the knowledge source "
            "({searches}); the hit list may be incomplete."
        ),
    },
    "search_notice_excluded_other": {
        "de": (
            "Treffer wurden durch eine Prüfung der Wissensquelle ausgeschlossen "
            "({count} Ausschlüsse {searches}; derselbe Treffer kann mehrfach "
            "zählen); die Trefferliste kann unvollständig sein."
        ),
        "en": (
            "Hits were excluded by a check of the knowledge source ({count} "
            "exclusions {searches}; the same hit can count more than once); "
            "the hit list may be incomplete."
        ),
    },
    "search_notice_duplicate_one": {
        "de": (
            "1 Treffer wurde übergangen ({searches}), weil ein "
            "inhaltsgleiches Dokument schon unter den gefundenen Quellen war."
        ),
        "en": (
            "1 hit was skipped ({searches}) because a content-identical "
            "document was already among the found sources."
        ),
    },
    "search_notice_duplicate_other": {
        "de": (
            "{count} Treffer wurden übergangen ({searches}; derselbe Treffer "
            "kann mehrfach zählen), weil inhaltsgleiche Dokumente schon unter "
            "den gefundenen Quellen waren."
        ),
        "en": (
            "{count} hits were skipped ({searches}; the same hit can count "
            "more than once) because content-identical documents were already "
            "among the found sources."
        ),
    },
    "search_notice_degraded_one": {
        "de": (
            "Die Wissenssuche erreichte eine technische Kandidatengrenze "
            "({searches}) und lieferte im knappsten Fall {returned} von 1 "
            "angeforderten Treffer; das Ergebnis ist kein Vollständigkeitsnachweis."
        ),
        "en": (
            "The knowledge search hit a technical candidate limit ({searches}) "
            "and returned {returned} of 1 requested hit in the tightest case; "
            "the result is not a proof of completeness."
        ),
    },
    "search_notice_degraded_other": {
        "de": (
            "Die Wissenssuche erreichte eine technische Kandidatengrenze "
            "({searches}) und lieferte im knappsten Fall {returned} von {count} "
            "angeforderten Treffern; das Ergebnis ist kein Vollständigkeitsnachweis."
        ),
        "en": (
            "The knowledge search hit a technical candidate limit ({searches}) "
            "and returned {returned} of {count} requested hits in the tightest "
            "case; the result is not a proof of completeness."
        ),
    },
    "search_notice_degraded_complete": {
        "de": (
            "Die Wissenssuche erreichte eine technische Kandidatengrenze "
            "({searches}); die angeforderte Trefferzahl kam dennoch zustande, "
            "die Auswahl kann aber von einer ungebremsten Suche abweichen."
        ),
        "en": (
            "The knowledge search hit a technical candidate limit ({searches}); "
            "the requested number of hits still came back, but the selection "
            "may differ from an unbounded search."
        ),
    },
    "search_notice_no_collections": {
        "de": "Diesem Lauf ist keine Wissenssammlung zugänglich; es gab nichts zu durchsuchen.",
        "en": "No knowledge collection is accessible to this run; there was nothing to search.",
    },
    "search_notice_unknown": {
        "de": (
            "Die Wissenssuche meldete einen Hinweis, den dieser Lauf nicht "
            "einordnen kann; die Trefferliste kann unvollständig sein."
        ),
        "en": (
            "The knowledge search reported a notice this run cannot classify; "
            "the hit list may be incomplete."
        ),
    },
    "search_notice_unreadable": {
        "de": (
            "Die Wissenssuche meldete einen unlesbaren Hinweis; die "
            "Trefferliste kann unvollständig sein."
        ),
        "en": (
            "The knowledge search reported an unreadable notice; the hit list "
            "may be incomplete."
        ),
    },
}


def t(state: dict[str, Any], key: str, **kwargs: Any) -> str:
    """Translate ``key`` to the language stored in ``state["language"]``.

    Returns the localized string, with ``**kwargs`` substituted via
    :meth:`str.format`. Fallback chain:
    ``state["language"] == "de"`` → DE; anything else → EN.

    If ``key`` is not registered in :data:`MESSAGES`, the key itself is
    returned (defensive — surfaces typos as visible bugs in the stream).
    If ``kwargs`` are missing for placeholders in the template, the raw
    template is returned untransformed (no exception).
    """
    raw_lang = (state.get("language") or "").lower() if state else ""
    lang = "de" if raw_lang == "de" else "en"
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    template = entry.get(lang) or entry.get("en") or entry.get("de") or key
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            return template
    return template


def plural_form(count: int) -> str:
    """The plural category of *count* for German and English: ``one`` or ``other``."""
    return "one" if count == 1 else "other"


def t_count(state: dict[str, Any], key: str, count: int, **kwargs: Any) -> str:
    """Translate the ``{key}_one``/``{key}_other`` pair for *count*.

    The one place a count chooses its grammatical number; ``count`` is also
    passed to the template.
    """
    return t(state, f"{key}_{plural_form(count)}", count=count, **kwargs)
