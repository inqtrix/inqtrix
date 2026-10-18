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
    # ---- mission memo texts written in the reader's language ----
    "memo_open_points_intro": {
        "de": "Die Prüfung am Ende des Laufs sah weiteren Recherchebedarf, der in diesem Lauf nicht mehr gedeckt wurde:",
        "en": "The review at the end of the run saw further research needs that this run could no longer cover:",
    },
    "memo_open_points_intro_no_new_evidence": {
        "de": "Die Prüfung am Ende des Laufs sah diese Punkte als offen; die letzte Nachrecherche fügte keine neuen Belege hinzu:",
        "en": "The review at the end of the run saw these points as open; the last follow-up research added no new evidence:",
    },
    "memo_open_points_item": {"de": "Noch offen: {item}", "en": "Still open: {item}"},
    "memo_title_answer": {"de": "Antwort", "en": "Answer"},
    # The canvas name a memo falls back to before the outline has
    # produced one. It is a document name a person reads, so it
    # follows the run, like every other name in this table.
    "memo_title_default": {"de": "Memo", "en": "Memo"},
    "critic_report_title": {"de": "Prüfbericht", "en": "Review report"},
    "task_title_synthesis": {"de": "Synthese", "en": "Synthesis"},
    "task_evidence_label_quoted": {"de": "Teilaufgabe \u201e{title}\u201c", "en": "Subtask \u201c{title}\u201d"},
    "task_evidence_label_id": {"de": "Teilaufgabe {task_id}", "en": "Subtask {task_id}"},
    "task_evidence_needs_review": {"de": "nur einfach belegt", "en": "supported by a single source only"},
    "task_evidence_source_context_only": {
        "de": "nur Kontextquellen ohne belastbaren Beleg",
        "en": "context sources only, without robust evidence",
    },
    "task_evidence_single_source_all": {
        "de": " (alle Belege aus je einer Quelle)",
        "en": " (every piece of evidence from a single source)",
    },
    "task_evidence_single_source_share": {
        "de": " ({share} % der Belege aus je einer Quelle)",
        "en": " ({share} % of the evidence from a single source each)",
    },
    "task_evidence_partial_coverage": {
        "de": "teilweise ausgewertet ({cut} nicht ausgewertet)",
        "en": "partially evaluated ({cut} not evaluated)",
    },
    "task_evidence_confidence": {"de": "Konfidenz {confidence}/10", "en": "confidence {confidence}/10"},
    # ---- Dokumentanalyse (serverseitiger Fensterdurchlauf) ----
    "document_analysis_summary_read_one": {
        "de": "Dokumentanalyse: {read} von {count} Dokument bis zum Ende seines gespeicherten Textes gelesen.",
        "en": "Document analysis: {read} of {count} document read to the end of its stored text.",
    },
    "document_analysis_summary_read_other": {
        "de": "Dokumentanalyse: {read} von {count} Dokumenten bis zum Ende ihres gespeicherten Textes gelesen.",
        "en": "Document analysis: {read} of {count} documents read to the end of their stored text.",
    },
    "document_analysis_summary_extraction_single": {
        "de": "Es ist nicht belegt, dass der gespeicherte Text den ganzen Inhalt enthält.",
        "en": "It is not shown that the stored text holds the whole content.",
    },
    "document_analysis_summary_extraction_one": {
        "de": "Bei einem davon ist nicht belegt, dass der gespeicherte Text den ganzen Inhalt enthält.",
        "en": "For one of them it is not shown that the stored text holds the whole content.",
    },
    "document_analysis_summary_extraction_other": {
        "de": "Bei {count} davon ist nicht belegt, dass der gespeicherte Text den ganzen Inhalt enthält.",
        "en": "For {count} of them it is not shown that the stored text holds the whole content.",
    },
    "document_analysis_summary_cited_one": {
        "de": "{count} Stelle wörtlich belegt.",
        "en": "{count} passage verified verbatim.",
    },
    "document_analysis_summary_cited_other": {
        "de": "{count} Stellen wörtlich belegt.",
        "en": "{count} passages verified verbatim.",
    },
    # The whole result line of an analysis whose found documents an earlier
    # round had all read; the _part pair follows the line of one that also
    # read documents.
    "document_analysis_summary_already_read_one": {
        "de": (
            "Dokumentanalyse: {count} gefundenes Dokument hat dieser Lauf schon "
            "gelesen; es wurde nicht erneut gelesen."
        ),
        "en": (
            "Document analysis: {count} document found was already read in this "
            "run and was not read again."
        ),
    },
    "document_analysis_summary_already_read_other": {
        "de": (
            "Dokumentanalyse: {count} gefundene Dokumente hat dieser Lauf schon "
            "gelesen; sie wurden nicht erneut gelesen."
        ),
        "en": (
            "Document analysis: {count} documents found were already read in this "
            "run and were not read again."
        ),
    },
    "document_analysis_summary_already_read_part_one": {
        "de": (
            "{count} weiteres gefundenes Dokument hat dieser Lauf schon gelesen; "
            "es wurde nicht erneut gelesen."
        ),
        "en": (
            "{count} further document found was already read in this run and was "
            "not read again."
        ),
    },
    "document_analysis_summary_already_read_part_other": {
        "de": (
            "{count} weitere gefundene Dokumente hat dieser Lauf schon gelesen; "
            "sie wurden nicht erneut gelesen."
        ),
        "en": (
            "{count} further documents found were already read in this run and "
            "were not read again."
        ),
    },
    # The same for found documents no earlier reading could open: never
    # read, so never "already read".
    "document_analysis_summary_already_failed_one": {
        "de": (
            "Dokumentanalyse: {count} gefundenes Dokument ließ sich in einer "
            "früheren Runde dieses Laufs nicht lesen; es wurde nicht erneut versucht."
        ),
        "en": (
            "Document analysis: {count} document found could not be read in an "
            "earlier round of this run and was not tried again."
        ),
    },
    "document_analysis_summary_already_failed_other": {
        "de": (
            "Dokumentanalyse: {count} gefundene Dokumente ließen sich in einer "
            "früheren Runde dieses Laufs nicht lesen; sie wurden nicht erneut versucht."
        ),
        "en": (
            "Document analysis: {count} documents found could not be read in an "
            "earlier round of this run and were not tried again."
        ),
    },
    "document_analysis_summary_already_failed_part_one": {
        "de": (
            "{count} weiteres gefundenes Dokument ließ sich in einer früheren "
            "Runde dieses Laufs nicht lesen; es wurde nicht erneut versucht."
        ),
        "en": (
            "{count} further document found could not be read in an earlier "
            "round of this run and was not tried again."
        ),
    },
    "document_analysis_summary_already_failed_part_other": {
        "de": (
            "{count} weitere gefundene Dokumente ließen sich in einer früheren "
            "Runde dieses Laufs nicht lesen; sie wurden nicht erneut versucht."
        ),
        "en": (
            "{count} further documents found could not be read in an earlier "
            "round of this run and were not tried again."
        ),
    },
    "document_analysis_untitled_document": {
        "de": "Dokument ohne Titel (Nr. {number})",
        "en": "Document without a title (no. {number})",
    },
    "document_analysis_read_completely": {
        "de": "„{title}“ vollständig gelesen ({chars} Zeichen).",
        "en": "“{title}” read completely ({chars} characters).",
    },
    "document_analysis_text_read_to_end": {
        "de": "„{title}“: gespeicherter Text vollständig gelesen ({chars} Zeichen), aber nicht nachweislich der ganze Inhalt.",
        "en": "“{title}”: stored text read to its end ({chars} characters), but not shown to hold the whole content.",
    },
    "document_analysis_no_text": {
        "de": "„{title}“ enthält keinen gespeicherten Text.",
        "en": "“{title}” holds no stored text.",
    },
    "document_analysis_not_analysed": {
        "de": "„{title}“ nicht analysiert: {ranges}.",
        "en": "“{title}” not analysed: {ranges}.",
    },
    "document_analysis_read_partly": {
        "de": "„{title}“ nur teilweise analysiert; nicht analysiert: {ranges}.",
        "en": "“{title}” analysed only in part; not analysed: {ranges}.",
    },
    "document_analysis_range": {
        "de": "Zeichen {start}–{end} ({reason})",
        "en": "characters {start}–{end} ({reason})",
    },
    "document_analysis_reason_window_limit": {"de": "Fenstergrenze erreicht", "en": "window limit reached"},
    "document_analysis_reason_extraction_failed": {"de": "Auswertung gescheitert", "en": "extraction failed"},
    "document_analysis_reason_not_read": {"de": "nicht gelesen", "en": "not read"},
    "document_analysis_reason_cancelled": {"de": "abgebrochen", "en": "cancelled"},
    "document_analysis_reason_provider_error": {
        "de": "Modellaufruf gescheitert",
        "en": "model call failed",
    },
    "document_analysis_reason_not_called_after_failure": {
        "de": "nach einem gescheiterten Modellaufruf nicht mehr aufgerufen",
        "en": "not called after a failed model call",
    },
    "document_analysis_reason_content_refused": {
        "de": "Anfrage vom Inhaltsfilter des Modellanbieters abgelehnt",
        "en": "request refused by the model provider's content filter",
    },
    "document_analysis_reason_knowledge_document_not_found": {
        "de": "Dokument nicht gefunden oder nicht mehr sichtbar",
        "en": "document not found or no longer visible",
    },
    "document_analysis_reason_technical": {
        "de": "technischer Fehler ({code})",
        "en": "technical error ({code})",
    },
    "document_analysis_failed": {
        "de": "„{title}“ konnte nicht analysiert werden ({reason}).",
        "en": "“{title}” could not be analysed ({reason}).",
    },
    "document_analysis_reason_document_changed": {
        "de": "das Dokument änderte sich während der Analyse",
        "en": "the document changed during the analysis",
    },
    "document_analysis_pages_without_text_one": {
        "de": "„{title}“: Seite {list} von {pages} hat keine Textschicht; ihr Inhalt fehlt in der Analyse.",
        "en": "“{title}”: page {list} of {pages} has no text layer; its content is missing from the analysis.",
    },
    "document_analysis_pages_without_text_other": {
        "de": "„{title}“: {count} von {pages} Seiten haben keine Textschicht (Seiten {list}); ihr Inhalt fehlt in der Analyse.",
        "en": "“{title}”: {count} of {pages} pages have no text layer (pages {list}); their content is missing from the analysis.",
    },
    "document_analysis_extraction_unrecorded": {
        "de": "„{title}“: Die Extraktionsabdeckung wurde nicht erfasst; Seiten ohne Textschicht können fehlen und sind nicht benennbar.",
        "en": "“{title}”: extraction coverage was not recorded; pages without a text layer may be missing and cannot be named.",
    },
    "document_analysis_not_selected_document_limit_one": {
        "de": "Gefunden, aber nicht analysiert (höchstens {count} Dokument je Analyse): {titles}.",
        "en": "Found but not analysed (at most {count} document per analysis): {titles}.",
    },
    "document_analysis_not_selected_document_limit_other": {
        "de": "Gefunden, aber nicht analysiert (höchstens {count} Dokumente je Analyse): {titles}.",
        "en": "Found but not analysed (at most {count} documents per analysis): {titles}.",
    },
    "document_analysis_not_selected_unknown_reason": {
        "de": "Gefunden, aber nicht analysiert ({reason}): {titles}.",
        "en": "Found but not analysed ({reason}): {titles}.",
    },
    # A re-read a later round left out because its windows no longer fitted
    # the analysis's window budget; a later round can still read it.
    "document_analysis_reread_not_fitted": {
        "de": (
            "In einer früheren Runde teilweise gelesen; ein erneutes Lesen passte "
            "nicht mehr in das Fensterbudget dieser Analyse: {titles}."
        ),
        "en": (
            "Partly read in an earlier round; reading it again no longer fitted "
            "the window budget of this analysis: {titles}."
        ),
    },
    # Found documents an earlier round of the run already read as far as a
    # re-read reaches; {reason} is a research_code_* phrase.
    "document_analysis_already_read_to_end": {
        "de": (
            "In einer früheren Runde dieses Laufs schon bis zum Ende des "
            "gespeicherten Textes gelesen, hier nicht erneut gelesen: {titles}."
        ),
        "en": (
            "Already read to the end of the stored text in an earlier round of "
            "this run, not read again here: {titles}."
        ),
    },
    "document_analysis_already_read_partly": {
        "de": (
            "In einer früheren Runde dieses Laufs schon gelesen, hier nicht "
            "erneut gelesen ({reason}): {titles}."
        ),
        "en": (
            "Already read in an earlier round of this run, not read again here "
            "({reason}): {titles}."
        ),
    },
    # Found documents no earlier reading could open; {reason} is the
    # document_analysis_reason_* phrase of the stored failure code.
    "document_analysis_already_failed": {
        "de": (
            "In einer früheren Runde dieses Laufs nicht lesbar ({reason}), hier "
            "nicht erneut versucht: {titles}."
        ),
        "en": (
            "Could not be read in an earlier round of this run ({reason}), not "
            "tried again here: {titles}."
        ),
    },
    # {count} is the number of documents left out; {scope} names the whole
    # search scope from the document_analysis_scope_documents pair.
    "document_analysis_scope_found_one": {
        "de": "Die Suchen dieser Teilaufgabe trafen {found} von {scope} ihres Suchbereichs; das übrige wurde nicht betrachtet.",
        "en": "The searches of this subtask found {found} of {scope} in their scope; the other one was not considered.",
    },
    "document_analysis_scope_found_other": {
        "de": "Die Suchen dieser Teilaufgabe trafen {found} von {scope} ihres Suchbereichs; die {count} übrigen wurden nicht betrachtet.",
        "en": "The searches of this subtask found {found} of {scope} in their scope; the {count} others were not considered.",
    },
    "document_analysis_scope_none_one": {
        "de": "Die Suchen dieser Teilaufgabe trafen das eine Dokument ihres Suchbereichs nicht; es wurde nicht betrachtet.",
        "en": "The searches of this subtask did not find the one document in their scope; it was not considered.",
    },
    "document_analysis_scope_none_other": {
        "de": "Die Suchen dieser Teilaufgabe trafen keines der {count} Dokumente ihres Suchbereichs; sie wurden nicht betrachtet.",
        "en": "The searches of this subtask found none of the {count} documents in their scope; they were not considered.",
    },
    "document_analysis_scope_documents_one": {
        "de": "{count} Dokument", "en": "the {count} document",
    },
    "document_analysis_scope_documents_other": {
        "de": "{count} Dokumenten", "en": "the {count} documents",
    },
    "document_analysis_complete_negative": {
        "de": "Vollständig gelesen; keine Stelle beantwortet die Aufgabe.",
        "en": "Read completely; no passage answers the task.",
    },
    "document_analysis_partial_negative_one": {
        "de": "Keine Stelle ließ sich belegen; ob das Dokument die Aufgabe beantwortet, bleibt offen (siehe oben).",
        "en": "No passage could be verified; whether the document answers the task remains open (see above).",
    },
    "document_analysis_partial_negative_other": {
        "de": "Keine Stelle ließ sich belegen; ob die {count} Dokumente die Aufgabe beantworten, bleibt offen (siehe oben).",
        "en": "No passage could be verified; whether the {count} documents answer the task remains open (see above).",
    },
    "document_analysis_not_verbatim_one": {
        "de": "„{title}“: Eine als einschlägig gemeldete Stelle stand nicht wörtlich im gelesenen Abschnitt und wurde verworfen.",
        "en": "“{title}”: one passage named as relevant was not verbatim in the section read and was discarded.",
    },
    "document_analysis_not_verbatim_other": {
        "de": "„{title}“: {count} als einschlägig gemeldete Stellen standen nicht wörtlich im gelesenen Abschnitt und wurden verworfen.",
        "en": "“{title}”: {count} passages named as relevant were not verbatim in the section read and were discarded.",
    },
    "document_analysis_unchecked_long_one": {
        "de": "„{title}“: Eine gemeldete Stelle war länger als {limit} Zeichen und wurde nicht geprüft.",
        "en": "“{title}”: one named passage was longer than {limit} characters and was not checked.",
    },
    "document_analysis_unchecked_long_other": {
        "de": "„{title}“: {count} gemeldete Stellen waren länger als {limit} Zeichen und wurden nicht geprüft.",
        "en": "“{title}”: {count} named passages were longer than {limit} characters and were not checked.",
    },
    "document_analysis_unchecked_short_one": {
        "de": "„{title}“: Eine gemeldete Stelle hatte weniger als {min_words} Wörter und wurde nicht geprüft.",
        "en": "“{title}”: one named passage had fewer than {min_words} words and was not checked.",
    },
    "document_analysis_unchecked_short_other": {
        "de": "„{title}“: {count} gemeldete Stellen hatten weniger als {min_words} Wörter und wurden nicht geprüft.",
        "en": "“{title}”: {count} named passages had fewer than {min_words} words and were not checked.",
    },
    "document_analysis_uncitable_one": {
        "de": "„{title}“: Eine gemeldete Stelle steht wörtlich im gelesenen Text, ließ sich aber nicht als Beleg übernehmen.",
        "en": "“{title}”: one named passage stands verbatim in the text read but could not be taken over as evidence.",
    },
    "document_analysis_uncitable_other": {
        "de": "„{title}“: {count} gemeldete Stellen stehen wörtlich im gelesenen Text, ließen sich aber nicht als Beleg übernehmen.",
        "en": "“{title}”: {count} named passages stand verbatim in the text read but could not be taken over as evidence.",
    },
    "document_analysis_unchecked_other_one": {
        "de": "„{title}“: Eine gemeldete Stelle ließ sich nicht prüfen ({code}).",
        "en": "“{title}”: one named passage could not be checked ({code}).",
    },
    "document_analysis_unchecked_other_other": {
        "de": "„{title}“: {count} gemeldete Stellen ließen sich nicht prüfen ({code}).",
        "en": "“{title}”: {count} named passages could not be checked ({code}).",
    },
    "document_analysis_more_one": {
        "de": "„{title}“: In einem Abschnitt können weitere einschlägige Stellen stehen, als übernommen wurden (höchstens {limit} je Abschnitt).",
        "en": "“{title}”: in one section further relevant passages may stand than were taken over (at most {limit} per section).",
    },
    "document_analysis_more_other": {
        "de": "„{title}“: In {count} Abschnitten können weitere einschlägige Stellen stehen, als übernommen wurden (höchstens {limit} je Abschnitt).",
        "en": "“{title}”: in {count} sections further relevant passages may stand than were taken over (at most {limit} per section).",
    },
    "document_analysis_retrieval_warnings": {
        "de": "Suchhinweis zur Dokumentauswahl: {warnings}",
        "en": "Search note on the document selection: {warnings}",
    },
    "document_analysis_quote": {"de": "„{quote}“", "en": "“{quote}”"},
    "document_analysis_open_points_heading": {"de": "Offene Punkte", "en": "Open points"},
    "document_analysis_passages_heading": {"de": "Wörtlich belegte Stellen", "en": "Passages verified verbatim"},
    "document_analysis_page": {"de": "S. {page}", "en": "p. {page}"},
    "document_analysis_summary_failed": {
        "de": "Die Zusammenfassung scheiterte; die belegten Stellen bleiben erhalten.",
        "en": "The summary failed; the verified passages remain.",
    },
    "document_analysis_criteria_heading": {"de": "Kriterien", "en": "Criteria"},
    "document_analysis_verdict_answered": {"de": "beantwortet", "en": "answered"},
    "document_analysis_verdict_partly_answered": {"de": "teilweise beantwortet", "en": "partly answered"},
    "document_analysis_verdict_not_addressed_one": {"de": "im ganzen Dokument nicht behandelt", "en": "not addressed anywhere in the document"},
    "document_analysis_verdict_not_addressed_other": {
        "de": "in keinem der {count} Dokumente behandelt",
        "en": "not addressed in any of the {count} documents",
    },
    "document_analysis_verdict_not_found_in_verified_passages": {
        "de": "in den belegten Stellen nicht gefunden; was die Aussage offen lässt, steht unten",
        "en": "not found in the verified passages; what leaves this open is named below",
    },
    "document_analysis_verdict_no_verified_passage": {
        "de": "als beantwortet genannt, aber ohne belegte Stelle",
        "en": "named as answered, but without a verified passage",
    },
    "document_analysis_verdict_not_found_in_analysed_part": {
        "de": "in den analysierten Teilen nicht gefunden",
        "en": "not found in the analysed parts",
    },
    "document_analysis_unreadable": {
        "de": "Keines der ausgewählten Dokumente ließ sich lesen.",
        "en": "None of the selected documents could be read.",
    },
    # ---- Sprache / Recency / Type-Labels (eingebettet in classify-Hints) ----
    "lang_name_de": {"de": "Deutsch", "en": "German"},
    "lang_name_en": {"de": "Englisch", "en": "English"},
    "lang_name_fr": {"de": "Französisch", "en": "French"},
    "lang_name_es": {"de": "Spanisch", "en": "Spanish"},
    "lang_name_it": {"de": "Italienisch", "en": "Italian"},
    "lang_name_pt": {"de": "Portugiesisch", "en": "Portuguese"},
    "recency_hour": {"de": "letzte Stunde", "en": "last hour"},
    "recency_day": {"de": "heute", "en": "today"},
    "recency_week": {"de": "diese Woche", "en": "this week"},
    "recency_month": {"de": "diesen Monat", "en": "this month"},
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
            "Warnung: Abschnitt '{heading}' hat Token-Limit erreicht "
            "(finish_reason={finish_reason}, request_max_tokens={request_max}, "
            "completion_tokens={completion})"
        ),
        "en": (
            "Warning: section '{heading}' hit token limit "
            "(finish_reason={finish_reason}, request_max_tokens={request_max}, "
            "completion_tokens={completion})"
        ),
    },
    "compose_section_truncation": {
        "de": "Warnung: Abschnitt '{heading}' zeigt Trunkierungsanzeichen: {reasons}",
        "en": "Warning: section '{heading}' shows truncation signals: {reasons}",
    },
    "compose_aborted": {
        "de": (
            "Antwort-Synthese abgebrochen: {n} aufeinanderfolgende leere "
            "Sections (zuletzt '{heading}')"
        ),
        "en": (
            "Answer composition aborted: {n} consecutive empty sections "
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
            "ALGO-FAIL context_window: Modell {model} hat {window} Tokens "
            "Kontextfenster, benötigt werden {required}"
        ),
        "en": (
            "ALGO-FAIL context_window: model {model} has {window} context "
            "tokens, but {required} are required"
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
        "de": "Klassifikation fehlgeschlagen ({label}) — nutze konservative Defaults",
        "en": "Classification failed ({label}) — using conservative defaults",
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
        "de": "ALGO-FAIL plan_query_generation: Planung fehlgeschlagen ({label}); verwende Fallback-Query",
        "en": "ALGO-FAIL plan_query_generation: planning failed ({label}); using fallback query",
    },
    "plan_query_generation_failed": {
        "de": "ALGO-FAIL plan_query_generation: {reason}; verwende Fallback-Query",
        "en": "ALGO-FAIL plan_query_generation: {reason}; using fallback query",
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
        "de": "{provider}-Verbindung instabil — {count} Retrys bei {operation} ({model})",
        "en": "{provider} connection unstable — {count} retries during {operation} ({model})",
    },
    "provider_retry_attempt": {
        "de": (
            "{provider}-Retry {attempt}/{max_attempts} bei {operation} "
            "({model}); nächster Versuch in {delay}s wegen {reason}"
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
        "de": "Claim-Extraktion",
        "en": "claim extraction",
    },
    "retry_operation_evaluate": {
        "de": "Bewertung",
        "en": "evaluation",
    },
    "retry_operation_answer_synthesis": {
        "de": "Antwort-Synthese",
        "en": "answer synthesis",
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
        "de": "ALGO-FAIL claim_extraction: {failed} von {total} Claim-Extraktionen fehlgeschlagen",
        "en": "ALGO-FAIL claim_extraction: {failed} of {total} claim extractions failed",
    },
    "claim_extract_empty": {
        "de": "{empty} von {total} Claim-Extraktionen lieferten keine strukturierten Claims trotz Suchkontext",
        "en": "{empty} of {total} claim extractions returned no structured claims despite search context",
    },
    "search_claim_binding_issues": {
        "de": "{unbound} Aussagen ohne belegte Quelle, {unknown} unbekannte Quellen-Referenzen — Cross-Check eingeschränkt",
        "en": "{unbound} statements without a bound source, {unknown} unknown source references — cross-check limited",
    },
    "search_sources_processed": {
        "de": (
            "{n} Suchantworten verarbeitet, {citations} Referenzen gesammelt, "
            "{evidence} Evidenz-Records angelegt"
        ),
        "en": (
            "{n} search answers processed, {citations} references collected, "
            "{evidence} evidence records created"
        ),
    },
    "search_quality_summary": {
        "de": (
            "Aussagenlage: verifiziert {verified_claims}, "
            "vom Anbieter zugeschrieben {attributed_claims}, "
            "mehrfach belegt {cross_checked_claims}, "
            "nicht belegt {unverified_claims}"
        ),
        "en": (
            "Claim status: {verified_claims} verified, "
            "{attributed_claims} attributed by the search provider, "
            "{cross_checked_claims} cross-checked, "
            "{unverified_claims} unverified"
        ),
    },
    "search_source_quality": {
        "de": (
            "Quellenmix: {primary} primary, {mainstream} mainstream, "
            "{stakeholder} stakeholder, {unknown} unknown, {low} low; "
            "Qualität {quality}"
        ),
        "en": (
            "Source mix: {primary} primary, {mainstream} mainstream, "
            "{stakeholder} stakeholder, {unknown} unknown, {low} low; "
            "quality {quality}"
        ),
    },

    # ---- evaluate (nodes.py) ----
    "evaluate_start": {
        "de": "Bewerte Informationsqualität (nach Runde {round}/{max_rounds})...",
        "en": "Evaluating information quality (after round {round}/{max_rounds})...",
    },
    "evaluate_confidence_missing": {
        "de": "Bewertung unvollständig (CONFIDENCE-Feld fehlt) — nutze Default 5",
        "en": "Evaluation incomplete (CONFIDENCE field missing) — using default 5",
    },
    "evaluate_confidence_out_of_range": {
        "de": "Bewertung außerhalb des Wertebereichs (CONFIDENCE={raw}) — auf {clamped} geklemmt",
        "en": "Evaluation out of range (CONFIDENCE={raw}) — clamped to {clamped}",
    },
    "evaluate_coverage_missing": {
        "de": "Bewertung ohne Teilfragen-Abdeckung (COVERED_ASPECTS fehlt) — offene Teilfragen bleiben offen",
        "en": "Evaluation without sub-question coverage (COVERED_ASPECTS missing) — open sub-questions stay open",
    },
    "evaluate_evidence_scores_missing": {
        "de": "Evidence-Scores unvollständig (CONSISTENCY/SUFFICIENCY-Feld fehlt) — nutze Default 5",
        "en": "Evidence scores incomplete (CONSISTENCY/SUFFICIENCY field missing) — using default 5",
    },
    "evaluate_failed": {
        "de": "Qualitätsbewertung fehlgeschlagen ({label}) — konservative Confidence-Begrenzung",
        "en": "Quality evaluation failed ({label}) — using conservative confidence cap",
    },
    "research_finished": {
        "de": "Recherche abgeschlossen (Confidence: {conf}/10, Runden: {round})",
        "en": "Research finished (Confidence: {conf}/10, Rounds: {round})",
    },
    "confidence_continue": {
        "de": "Confidence {conf}/10 — weitere Recherche nötig",
        "en": "Confidence {conf}/10 — further research needed",
    },
    "evaluate_quality_summary": {
        "de": (
            "Bewertungssignal: Quellenqualität {source_quality}, "
            "Claim-Qualität {claim_quality}, {evidence_records} Evidenz-Records, "
            "{open_aspects} offene Aspekte"
        ),
        "en": (
            "Evaluation signal: source quality {source_quality}, "
            "claim quality {claim_quality}, {evidence_records} evidence records, "
            "{open_aspects} open aspects"
        ),
    },
    "min_rounds_continue": {
        "de": "min_rounds={min_rounds} noch nicht erreicht (aktuell {round}); setze Recherche fort",
        "en": "min_rounds={min_rounds} not yet reached (currently {round}); continuing research",
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
        "en": "Not cited in the text; examined by the research run and contained in the evidence ledger.",
    },
    "answer_reference_label": {"de": "Quelle", "en": "Source"},
    "answer_completeness_heading": {"de": "## Hinweis zur Vollständigkeit", "en": "## Note on completeness"},
    "answer_completeness_status": {"de": "- Status: unvollständig", "en": "- Status: incomplete"},
    "answer_completeness_provider_stop": {
        "de": "- Provider-Stoppgrund: `{finish_reason}`",
        "en": "- Provider stop reason: `{finish_reason}`",
    },
    "answer_completeness_diagnosis": {"de": "- Diagnose: {reasons}", "en": "- Diagnosis: {reasons}"},
    "answer_completeness_hint": {
        "de": "- Hinweis: Bereits sauber abgeschlossene Abschnitte bleiben sichtbar; es wurde keine verdeckende Regeneration ausgeführt.",
        "en": "- Note: sections that finished cleanly stay visible; no masking regeneration was run.",
    },
    "answer_omission_note": {
        "de": "- Nicht ausgewertete Quellen: {cut} passten nicht in das Evidenz-Budget und wurden im Bericht nicht ausgewertet (Suchanfragen: {queries}).",
        "en": "- Sources not evaluated: {cut} did not fit into the evidence budget and were not evaluated in the report (search queries: {queries}).",
    },
    "answer_evidence_notice_heading": {"de": "## Evidenzhinweis", "en": "## Evidence notice"},
    "answer_evidence_notice_source_context": {
        "de": "Die Antwort stützt sich auf quellenbasierten Kontext aus den Belegen, aber keine der zitierten Quellen trägt eine verifizierte oder strittige Aussage. Sie ist daher eine quellenbasierte Zusammenfassung, kein geprüfter Faktenbericht.",
        "en": "The answer rests on source-based evidence-ledger context, but none of the cited sources carries a verified or contested statement. It is therefore a source-based synthesis, not a verified hard-fact report.",
    },
    "answer_evidence_notice_single_source": {
        "de": "Die Evidenzlage stützt sich überwiegend auf Einzelquellen: {single} von {supported} gestützten Aussagen ({ratio} %) ruhen auf einer einzigen Quelle, nur {cross} sind mehrfach belegt. Aussagen, die weder mehrfach belegt sind noch aus einer Primärquelle stammen, sind als Angaben einer einzelnen Quelle zu lesen; die in einem Satz genannte Quelle trägt die jeweilige Behauptung allein.",
        "en": "The evidence is predominantly single-source: {single} of {supported} supported statements ({ratio} %) rest on a single source, only {cross} are cross-checked. Statements without a cross-checked or primary-source basis are single-source attributions; the source given in a sentence carries that claim alone.",
    },
    "answer_evidence_notice_unbound": {
        "de": "Einige zitierte Links ließen sich keinem Beleg zuordnen. Die betroffenen Punkte sind als nicht ausreichend belegt zu behandeln.",
        "en": "Some cited links could not be traced back to an evidence record. Treat the affected points as insufficiently supported.",
    },
    "answer_stats_sources": {"de": "{n} Quellen recherchiert", "en": "{n} sources researched"},
    "answer_stats_evidence_prompt_one": {"de": "{count} Beleg für den Bericht bereitgestellt", "en": "{count} evidence record in the evidence prompt"},
    "answer_stats_evidence_prompt_other": {"de": "{count} Belege für den Bericht bereitgestellt", "en": "{count} evidence records in the evidence prompt"},
    "answer_stats_linked": {"de": "{n} Quellen im Report verlinkt", "en": "{n} sources linked in the report"},
    "answer_stats_verified": {"de": "{n} verifizierte Aussagen", "en": "{n} verified statements"},
    "answer_stats_attributed_one": {
        "de": "{count} vom Anbieter zugeschriebene Aussage",
        "en": "{count} statement attributed by the search provider",
    },
    "answer_stats_attributed_other": {
        "de": "{count} vom Anbieter zugeschriebene Aussagen",
        "en": "{count} statements attributed by the search provider",
    },
    "answer_stats_cross_checked": {"de": "{n} mehrfach belegte Aussagen", "en": "{n} cross-checked statements"},
    "answer_stats_omitted_one": {"de": "{count} Evidenzbeleg wegen Budget ausgelassen", "en": "{count} evidence record omitted for budget"},
    "answer_stats_omitted_other": {"de": "{count} Evidenzbelege wegen Budget ausgelassen", "en": "{count} evidence records omitted for budget"},
    "answer_stats_searches": {"de": "{n} Suchen", "en": "{n} searches"},
    "answer_stats_round_one": {"de": "{count} Runde", "en": "{count} round"},
    "answer_stats_round_other": {"de": "{count} Runden", "en": "{count} rounds"},
    "answer_section_start": {
        "de": "Schreibe Report-Abschnitt {index}/{total}: {heading}",
        "en": "Writing report section {index}/{total}: {heading}",
    },
    "answer_round_singular": {"de": "Runde", "en": "round"},
    "answer_round_plural": {"de": "Runden", "en": "rounds"},
    "answer_fallback_model": {
        "de": "Finale Antwort fehlgeschlagen — Fallback-Modell {model}",
        "en": "Final answer failed — fallback model {model}",
    },
    "answer_timeout_fallback": {
        "de": (
            "Antwort-Synthese-Timeout — Antwort enthält nur die bisherigen "
            "Recherche-Ergebnisse mit Warnhinweis"
        ),
        "en": (
            "Answer synthesis timeout — answer contains only the existing "
            "research results with a warning header"
        ),
    },
    "answer_provider_failed_no_fallback": {
        "de": (
            "Antwort-Synthese fehlgeschlagen ({error}) — kein Fallback-Modell "
            "konfiguriert; Antwort enthält nur die Recherche-Ergebnisse mit Warnhinweis"
        ),
        "en": (
            "Answer synthesis failed ({error}) — no fallback model configured; "
            "answer contains only the research results with a warning header"
        ),
    },
    "answer_fallback_model_failed": {
        "de": (
            "Antwort-Synthese und Fallback-Modell fehlgeschlagen ({error}) — "
            "Antwort enthält nur die Recherche-Ergebnisse mit Warnhinweis"
        ),
        "en": (
            "Answer synthesis and fallback model failed ({error}) — "
            "answer contains only the research results with a warning header"
        ),
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
        "de": "Antwort-Evidenz-Vertrag verletzt — Bericht wird konservativ markiert",
        "en": "Answer evidence contract violated — marking report conservatively",
    },
    "evidence_contract_source_context_only": {
        "de": "ALGO-FAIL report_evidence: Quellenkontext ohne verifizierte Report-Evidence-Bundles",
        "en": "ALGO-FAIL report_evidence: source context has no verified report evidence bundles",
    },
    "algorithm_failure_report_blocked": {
        "de": "ALGO-FAIL: normaler Report blockiert ({count} blockierende Kernfehler)",
        "en": "ALGO-FAIL: normal report blocked ({count} blocking core failures)",
    },

    # ---- graph.py ----
    "run_aborted": {
        "de": "Recherche abgebrochen: {exc}",
        "en": "Research aborted: {exc}",
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
        "de": "Schwere Widersprüche erkannt — Confidence stark begrenzt",
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
        "de": "Confidence wieder hoch — Falsifikations-Modus deaktiviert",
        "en": "Confidence recovered — falsification mode deactivated",
    },
    "falsification_started": {
        "de": "Niedrige Evidenz — starte Falsifikations-Recherche",
        "en": "Low evidence — starting falsification research",
    },
    "stagnation_premise_false": {
        "de": (
            "Umfangreiche Recherche abgeschlossen — "
            "Prämisse der Frage wahrscheinlich falsch"
        ),
        "en": (
            "Extensive research complete — "
            "question premise likely false"
        ),
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
        "de": "Confidence {conf}/10 stabil — Recherche abgeschlossen",
        "en": "Confidence {conf}/10 stable — research complete",
    },

    # ---- streaming.py SSE error chunks (user-facing) ----
    "sse_request_timeout": {
        "de": "\n---\n\n⚠️ **Fehler bei der Recherche:** Request-Timeout erreicht",
        "en": "\n---\n\n⚠️ **Research error:** request timeout reached",
    },
    "sse_agent_error": {
        "de": "\n---\n\n⚠️ **Fehler bei der Recherche:** {err}",
        "en": "\n---\n\n⚠️ **Research error:** {err}",
    },

    # ---- agents/narration.py transcript lines (Agent Desk) ----
    # A counted phrase is a one/other pair; a sentence that counts two
    # things takes each phrase whole into a fixed slot of its frame (the
    # accusative object below), so the frame owns the word order.
    "agent_narration_discovery_empty": {
        "de": "Die Erkundung hat weder belegte Fakten noch offene Lücken ergeben.",
        "en": "The exploration produced neither supported facts nor open gaps.",
    },
    "agent_narration_discovery_counts": {
        "de": "Ich habe {facts} und {gaps} identifiziert.",
        "en": "I identified {facts} and {gaps}.",
    },
    "agent_narration_discovery_facts_one": {
        "de": "{count} belegten Fakt", "en": "{count} supported fact",
    },
    "agent_narration_discovery_facts_other": {
        "de": "{count} belegte Fakten", "en": "{count} supported facts",
    },
    "agent_narration_discovery_gaps_one": {
        "de": "{count} offene Lücke", "en": "{count} open gap",
    },
    "agent_narration_discovery_gaps_other": {
        "de": "{count} offene Lücken", "en": "{count} open gaps",
    },
    "agent_narration_discovery_top_gap": {
        "de": "Wichtigste Lücke: {gap}",
        "en": "Most important gap: {gap}",
    },
    "agent_narration_plan_proposal_one": {
        "de": "Ich schlage einen Plan mit {count} Aufgabe vor.",
        "en": "I propose a plan with {count} task.",
    },
    "agent_narration_plan_proposal_other": {
        "de": "Ich schlage einen Plan mit {count} Aufgaben vor.",
        "en": "I propose a plan with {count} tasks.",
    },
    "agent_narration_plan_summary_one": {
        "de": "Mein Plan ({count} Aufgabe): {summary}",
        "en": "My plan ({count} task): {summary}",
    },
    "agent_narration_plan_summary_other": {
        "de": "Mein Plan ({count} Aufgaben): {summary}",
        "en": "My plan ({count} tasks): {summary}",
    },
    "agent_narration_synthesis_one": {
        "de": "Ich schreibe jetzt das Memo '{title}' mit {count} Abschnitt.",
        "en": "I am now writing the memo '{title}' with {count} section.",
    },
    "agent_narration_synthesis_other": {
        "de": "Ich schreibe jetzt das Memo '{title}' mit {count} Abschnitten.",
        "en": "I am now writing the memo '{title}' with {count} sections.",
    },
    "agent_narration_section_written": {
        "de": "Abschnitt '{title}' geschrieben.",
        "en": "Section '{title}' written.",
    },
    # What stands in a transcript line in place of an internal identifier.
    # A narration line is assembled from finished artifacts, so there is
    # no writer left to ask for a repair - and dropping the line would
    # take a step of the run away from the reader without saying so.
    "agent_reader_identifier_removed": {
        "de": "[interne Kennung entfernt]",
        "en": "[internal identifier removed]",
    },

    # ---- agents/algorithm.py transcript channel (narration + activity) ----
    # Only the TRANSCRIPT lives here. A task row's persisted
    # ``result_summary`` is state, not speech, and carries a reason code
    # instead (rendered at the display boundary).
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
    "agent_limit_tokens_detail": {
        "de": "Serverseitiges Tokenbudget erreicht",
        "en": "Server-side token budget reached",
    },
    # Task and synthesis outcome summaries. These are PERSISTED with the
    # plan row and read back long after the run, so they follow the run's
    # answer language like every other text the run produces.
    "agent_task_summary_cancelled_by_user": {
        "de": "Aufgabe auf Nutzerwunsch abgebrochen.",
        "en": "Task cancelled at the user's request.",
    },
    "agent_task_summary_skipped_cancelled": {
        "de": "Wegen Abbruch nicht ausgeführt.",
        "en": "Not executed because the run was cancelled.",
    },
    "agent_task_summary_token_budget": {
        "de": "Serverseitiges Tokenbudget erreicht.",
        "en": "Server-side token budget reached.",
    },
    "agent_task_summary_no_document_content": {
        "de": "Keine Dokumentinhalte gefunden.",
        "en": "No document content found.",
    },
    "agent_synthesis_summary_not_restarted": {
        "de": "Synthese nach einer Unterbrechung nicht erneut gestartet.",
        "en": "Synthesis was not restarted after an interruption.",
    },
    "agent_synthesis_summary_invalid_labels": {
        "de": "Synthese wegen ungültiger Belege-Labels verworfen.",
        "en": "Synthesis discarded because of invalid evidence labels.",
    },
    "agent_synthesis_summary_failed": {
        "de": "Synthese fehlgeschlagen.",
        "en": "Synthesis failed.",
    },
    "agent_synthesis_summary_done": {
        "de": "Synthese abgeschlossen.",
        "en": "Synthesis completed.",
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
    "research_query_plan_not_json": {
        "de": "Suchquery-Planung lieferte keine valide JSON-Liste",
        "en": "Query planning did not return a valid JSON list",
    },
    "research_answer_failed_no_fallback_body": {
        "de": (
            "Antwort-Synthese fehlgeschlagen ({error}); kein Fallback-Modell "
            "konfiguriert."
        ),
        "en": (
            "Answer synthesis failed ({error}); no fallback model configured."
        ),
    },
    "kernel_recovery_context_window": {
        "de": "Der Modellkontext ist zu groß. Die vollständigen Werkzeugdaten sind gesichert; ich bereite eine kleinere, aufgabenbezogene Ansicht vor.",
        "en": "The model context is too large. Full tool data is preserved; I am preparing a smaller, task-specific view.",
    },
    "kernel_recovery_tool_arguments": {
        "de": "Das Modell hat ungültige Werkzeugargumente geliefert. Aus dieser Antwort wurde kein Werkzeug ausgeführt; ich lasse den Aufruf korrigieren.",
        "en": "The model returned invalid tool arguments. No tool from that response was executed; I am requesting a corrected call.",
    },
    "kernel_recovery_context_budget": {
        "de": "Ich verdichte die vollständigen Ergebnisse für den verfügbaren Modellkontext. Die Originale bleiben nachlesbar.",
        "en": "I am condensing the complete results for the available model context. The originals remain readable.",
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
    "kernel_context_compacted_without_summary": {
        "de": (
            "Kontext-Komprimierung ohne Zusammenfassung (Fehler im "
            "Zusammenfassungs-Aufruf) — ältere Schritte unverdichtet im "
            "Lauf-Archiv gesichert."
        ),
        "en": (
            "Context compacted without a summary (the summarization call "
            "failed) — older steps kept uncondensed in the run archive."
        ),
    },
    "agent_synthesis_summary_cancelled": {
        "de": (
            "Synthese abgebrochen; ein begonnenes Ergebnis wurde nicht "
            "übernommen."
        ),
        "en": (
            "Synthesis cancelled; work already begun was not adopted."
        ),
    },
    "agent_task_summary_cancelled_midway": {
        "de": (
            "Ausführung abgebrochen; ein begonnenes Ergebnis wurde nicht "
            "übernommen."
        ),
        "en": (
            "Execution cancelled; work already begun was not adopted."
        ),
    },
    "agent_task_summary_not_restarted": {
        "de": "Ausführung nach einer Unterbrechung nicht erneut gestartet.",
        "en": "Execution was not restarted after an interruption.",
    },
    "agent_task_summary_upstream_failed": {
        "de": (
            "Nicht ausgeführt, weil alle vorgelagerten Aufgaben "
            "fehlgeschlagen sind."
        ),
        "en": (
            "Not executed because every upstream task failed."
        ),
    },
    # Settlement of a terminal run's unfinished plan rows. The graph knows
    # the run language and passes it; the HTTP recovery boundary does not
    # and falls back to English like every other unresolved language.
    "agent_task_settled_skipped_cancelled": {
        "de": "Wegen Abbruch nicht ausgeführt.",
        "en": "Not executed because the run was cancelled.",
    },
    "agent_task_settled_skipped_failed": {
        "de": "Nicht ausgeführt, weil der Lauf fehlgeschlagen ist.",
        "en": "Not executed because the run failed.",
    },
    "agent_task_settled_running_cancelled": {
        "de": (
            "Ausführung abgebrochen; ein begonnenes Ergebnis wurde nicht "
            "übernommen."
        ),
        "en": "Execution cancelled; work already begun was not adopted.",
    },
    "agent_task_settled_running_failed": {
        "de": "Ausführung durch einen Lauf-Fehler unvollständig beendet.",
        "en": "Execution ended incompletely because the run failed.",
    },
    "agent_task_settled_cancel_requested": {
        "de": (
            "Aufgabe auf Nutzerwunsch abgebrochen; ein begonnenes Ergebnis "
            "wurde nicht übernommen."
        ),
        "en": (
            "Task cancelled at the user's request; work already begun was "
            "not adopted."
        ),
    },
    "agent_memory_label_used": {"de": "Memory verwendet", "en": "Memory used"},
    "agent_memory_label_empty": {"de": "Memory geprüft", "en": "Memory checked"},
    "agent_memory_label_unavailable": {
        "de": "Memory nicht verfügbar", "en": "Memory unavailable",
    },
    "agent_memory_detail_used": {
        "de": "Langzeit-Memory liefert Kontext, aber keine Evidenz.",
        "en": "Long-term memory provides context, but no evidence.",
    },
    "agent_memory_detail_unavailable": {
        "de": "Langzeit-Memory ist für diesen Lauf nicht nutzbar.",
        "en": "Long-term memory is unavailable for this run.",
    },
    "agent_memory_detail_candidates_failed": {
        "de": "Memory-Kandidaten konnten nicht erzeugt werden.",
        "en": "Memory candidates could not be generated.",
    },
    "agent_clarify_settled_one": {
        "de": (
            "Eine Rückfrage entfällt, weil die Einstellungen des Auftrags sie "
            "beantworten: {items}."
        ),
        "en": (
            "One clarifying question is dropped because the assignment's "
            "settings answer it: {items}."
        ),
    },
    "agent_clarify_settled_other": {
        "de": (
            "{count} Rückfragen entfallen, weil die Einstellungen des Auftrags "
            "sie beantworten: {items}."
        ),
        "en": (
            "{count} clarifying questions are dropped because the assignment's "
            "settings answer them: {items}."
        ),
    },
    "agent_clarify_settled_item": {
        "de": "„{prompt}“ ({setting})",
        "en": "“{prompt}” ({setting})",
    },
    "agent_clarify_not_asked_limit_one": {
        "de": "Das Rückfrage-Limit dieses Laufs ist erreicht; nicht gestellt: {items}.",
        "en": "This run's clarification limit is reached; not asked: {items}.",
    },
    "agent_clarify_not_asked_limit_other": {
        "de": (
            "Das Rückfrage-Limit dieses Laufs ist erreicht; {count} Rückfragen "
            "nicht gestellt: {items}."
        ),
        "en": (
            "This run's clarification limit is reached; {count} clarifying "
            "questions not asked: {items}."
        ),
    },
    "agent_clarify_not_asked_tier_one": {
        "de": "In dieser Stufe stellt der Lauf keine Rückfragen. Offen war: {items}.",
        "en": "At this tier the run asks no clarifying questions. Left open: {items}.",
    },
    "agent_clarify_not_asked_tier_other": {
        "de": (
            "In dieser Stufe stellt der Lauf keine Rückfragen. Offen waren "
            "{count} Rückfragen: {items}."
        ),
        "en": (
            "At this tier the run asks no clarifying questions. {count} "
            "questions were left open: {items}."
        ),
    },
    "agent_clarify_not_asked_overflow_one": {
        "de": (
            "Die Runde ist voll (Obergrenze: {limit}); eine weitere Rückfrage "
            "nicht gestellt: {items}."
        ),
        "en": (
            "The round is full (cap: {limit}); one more clarifying question "
            "not asked: {items}."
        ),
    },
    "agent_clarify_not_asked_overflow_other": {
        "de": (
            "Die Runde ist voll (Obergrenze: {limit}); {count} weitere "
            "Rückfragen nicht gestellt: {items}."
        ),
        "en": (
            "The round is full (cap: {limit}); {count} more clarifying "
            "questions not asked: {items}."
        ),
    },
    "agent_clarify_assumed_item": {
        "de": "„{prompt}“ – angenommen: {assumption}",
        "en": "“{prompt}” – assumed: {assumption}",
    },
    "agent_clarify_no_assumption": {
        "de": "„{prompt}“ – ohne vorgeschlagene Annahme",
        "en": "“{prompt}” – no assumption proposed",
    },
    "agent_clarify_skipped_one": {
        "de": (
            "{count} Rückfrage übersprungen, die dieser Lauf schon gestellt "
            "oder mit einer Annahme belegt hat."
        ),
        "en": (
            "{count} clarifying question skipped that this run already asked "
            "or settled with an assumption."
        ),
    },
    "agent_clarify_skipped_other": {
        "de": (
            "{count} Rückfragen übersprungen, die dieser Lauf schon gestellt "
            "oder mit einer Annahme belegt hat."
        ),
        "en": (
            "{count} clarifying questions skipped that this run already asked "
            "or settled with an assumption."
        ),
    },
    "agent_setting_scope_pinned_one": {
        "de": "festgelegte Sammlung {names}",
        "en": "selected collection {names}",
    },
    "agent_setting_scope_pinned_other": {
        "de": "festgelegte Sammlungen {names}",
        "en": "selected collections {names}",
    },
    "agent_setting_collection_name": {
        "de": "„{name}“",
        "en": "“{name}”",
    },
    "agent_setting_scope_count_one": {
        "de": "{count} festgelegte Sammlung",
        "en": "{count} selected collection",
    },
    "agent_setting_scope_count_other": {
        "de": "{count} festgelegte Sammlungen",
        "en": "{count} selected collections",
    },
    "agent_setting_scope_only": {
        "de": "einzige für diesen Lauf freigegebene Sammlung {names}",
        "en": "the only collection released for this run, {names}",
    },
    "agent_setting_scope_only_unnamed": {
        "de": "eine einzige für diesen Lauf freigegebene Sammlung",
        "en": "a single collection released for this run",
    },
    "agent_setting_scope_none": {
        "de": "für diesen Lauf ist keine Wissenssammlung freigegeben",
        "en": "no knowledge collection is released for this run",
    },
    "agent_setting_scope_all": {
        "de": "alle für Sie sichtbaren Wissenssammlungen",
        "en": "all knowledge collections visible to you",
    },
    "agent_setting_knowledge_off": {
        "de": "Projektwissen ist ausgeschaltet",
        "en": "project knowledge is off",
    },
    "agent_setting_web_off": {
        "de": "Websuche ist ausgeschaltet",
        "en": "web search is off",
    },
    "agent_setting_form_chat": {
        "de": "festgelegte Ausgabeform Chat-Antwort",
        "en": "fixed output form: chat answer",
    },
    "agent_setting_form_canvas": {
        "de": "festgelegte Ausgabeform Canvas-Dokument",
        "en": "fixed output form: canvas document",
    },
    "agent_plan_assumption_unproposed": {
        "de": "Nicht gestellte Rückfrage ohne vorgeschlagene Annahme: „{prompt}“",
        "en": "Question not asked, no assumption proposed: “{prompt}”",
    },
    "agent_plan_assumption_web_not_needed": {
        "de": (
            "Die Websuche ist nicht eingeplant: Der Auftrag braucht nach "
            "seiner Einordnung keine öffentlichen Quellen. Soll das Web "
            "mitgeprüft werden, verlange es ausdrücklich im Auftrag."
        ),
        "en": (
            "Web search is not planned: as the assignment was read, it needs "
            "no public sources. To include the web, ask for it explicitly in "
            "the assignment."
        ),
    },
    "agent_discovery_limit_narration_one": {
        "de": (
            "Die Erkundung hat ihr Abrufbudget ausgeschöpft (Obergrenze: "
            "{limit}); {count} geplanter Abruf wurde ausgelassen."
        ),
        "en": (
            "The exploration used up its lookup budget (cap: {limit}); "
            "{count} planned lookup was left out."
        ),
    },
    "agent_discovery_limit_narration_other": {
        "de": (
            "Die Erkundung hat ihr Abrufbudget ausgeschöpft (Obergrenze: "
            "{limit}); {count} geplante Abrufe wurden ausgelassen."
        ),
        "en": (
            "The exploration used up its lookup budget (cap: {limit}); "
            "{count} planned lookups were left out."
        ),
    },
    "agent_answer_written": {
        "de": "Antwort verfasst.", "en": "Answer written.",
    },
    "agent_memory_candidate_label": {
        "de": "Memory-Kandidat erzeugt", "en": "Memory candidate created",
    },
    "agent_memory_candidate_detail": {
        "de": "Der Kandidat wartet auf Nutzerfreigabe.",
        "en": "The candidate is waiting for your approval.",
    },
    "agent_critic_revision_discarded_label": {
        "de": "Prüfung: Überarbeitung verworfen",
        "en": "Review: revision discarded",
    },
    "agent_critic_revision_partially_discarded_label": {
        "de": "Prüfung: Überarbeitung teilweise verworfen",
        "en": "Review: revision partly discarded",
    },
    "agent_replan_invalid_label": {
        "de": "Replan verworfen, weiter mit dem letzten Plan",
        "en": "Replan discarded, continuing with the last plan",
    },
    "agent_critic_no_gap_label": {
        "de": "Keine belegte Rechercheaufgabe",
        "en": "No evidenced research task",
    },
    "agent_critic_no_gap_detail": {
        "de": (
            "Die Prüfung benennt keine klassifizierte Sachlücke; keine "
            "weitere Recherche."
        ),
        "en": (
            "The review names no classified factual gap; no further research."
        ),
    },
    "agent_critic_cap_label": {
        "de": "Recherche-Limit erreicht", "en": "Research limit reached",
    },
    "agent_critic_no_new_evidence_label": {
        "de": "Nachrecherche beendet", "en": "Follow-up research ended",
    },
    "agent_critic_no_new_evidence_detail": {
        "de": "Die letzte Nachrecherche fügte keine neuen Belege hinzu; der Lauf recherchiert deshalb nicht weiter.",
        "en": "The last follow-up research added no new evidence, so the run researches no further.",
    },
    "agent_critic_cap_replan_empty_detail": {
        "de": (
            "Die Prüfung sieht weiteren Recherchebedarf, aber der Replan ergab "
            "keine neuen Aufgaben; das Memo bleibt mit der benannten Lücke."
        ),
        "en": (
            "The review sees further research needed, but the replan produced "
            "no new tasks; the memo keeps the named gap."
        ),
    },
    "agent_critic_replan_all_dropped_detail": {
        "de": (
            "Die Prüfung sieht weiteren Recherchebedarf, aber jede neue "
            "Dokumentanalyse des Replans fände kein Dokument, das dieser Lauf "
            "noch lesen kann; das Memo bleibt mit der benannten Lücke."
        ),
        "en": (
            "The review sees further research need, but every new document "
            "analysis of the replan would find no document this run can still "
            "read; the memo stands with the named gap."
        ),
    },
    # The plan-time check of a replan's new document analysis: searched and
    # selected as execution would, before the plan version is saved.
    "agent_replan_analysis_rejected_label": {
        "de": "Dokumentanalyse nicht eingeplant", "en": "Document analysis not planned",
    },
    "agent_replan_analysis_rejected_detail": {
        "de": (
            "Die neue Dokumentanalyse „{title}“ fände kein Dokument, das dieser "
            "Lauf noch lesen kann; sie entfällt."
        ),
        "en": (
            "The new document analysis “{title}” would find no document this run "
            "can still read; it is dropped."
        ),
    },
    "agent_replan_analysis_found_nothing_detail": {
        "de": (
            "Die Suchen der neuen Dokumentanalyse „{title}“ fanden kein Dokument; "
            "sie entfällt."
        ),
        "en": (
            "The searches of the new document analysis “{title}” found no "
            "document; it is dropped."
        ),
    },
    "agent_replan_analysis_unchecked_label": {
        "de": "Dokumentanalyse ungeprüft eingeplant",
        "en": "Document analysis planned unchecked",
    },
    "agent_replan_analysis_unchecked_detail": {
        "de": (
            "Vor dem Planen ließ sich nicht prüfen, ob die Dokumentanalyse "
            "„{title}“ noch Ungelesenes findet; sie bleibt im Plan."
        ),
        "en": (
            "Before planning it could not be checked whether the document "
            "analysis “{title}” still finds anything unread; it stays in the plan."
        ),
    },
    # Strict autonomy: the check would search before the replan approval.
    "agent_replan_analysis_consent_detail": {
        "de": (
            "Im Modus „Strikt“ sucht die Neuplanung vor der Freigabe nichts; ob "
            "die Dokumentanalyse „{title}“ noch Ungelesenes findet, zeigt erst "
            "ihre Ausführung."
        ),
        "en": (
            "In Strict mode the replan searches nothing before the approval; "
            "whether the document analysis “{title}” still finds anything unread "
            "shows only when it runs."
        ),
    },
    "agent_document_version_unchecked_label": {
        "de": "Lesestand nicht geprüft", "en": "Reading state not checked",
    },
    "agent_document_version_unchecked_detail_one": {
        "de": (
            "{count} gefundenes Dokument ließ sich nicht mit dem abgleichen, was "
            "dieser Lauf schon gelesen hat; es zählt als ungelesen."
        ),
        "en": (
            "{count} document found could not be compared with what this run has "
            "already read; it counts as unread."
        ),
    },
    "agent_document_version_unchecked_detail_other": {
        "de": (
            "{count} gefundene Dokumente ließen sich nicht mit dem abgleichen, was "
            "dieser Lauf schon gelesen hat; sie zählen als ungelesen."
        ),
        "en": (
            "{count} documents found could not be compared with what this run has "
            "already read; they count as unread."
        ),
    },
    # A part of the replan's context the server had to leave out.
    "agent_replan_context_incomplete_label": {
        "de": "Kontext der Neuplanung unvollständig", "en": "Replan context incomplete",
    },
    "agent_replan_context_incomplete_tasks_before_round_missing_detail": {
        "de": (
            "Der Lauf verzeichnet nicht, welche Aufgaben vor dieser Runde liefen; "
            "die Dokumentanalysen dieser Runde überspringen deshalb nichts, was "
            "frühere Runden schon gelesen haben."
        ),
        "en": (
            "The run does not record which tasks ran before this round; this "
            "round's document analyses therefore skip nothing earlier rounds "
            "already read."
        ),
    },
    "agent_replan_context_incomplete_criteria_changed_detail": {
        "de": (
            "Die letzte Prüfung beurteilte andere Erfolgskriterien, als der Plan "
            "jetzt enthält; ihre offenen Kriterien gehen nicht in die Neuplanung ein."
        ),
        "en": (
            "The last review judged other success criteria than the plan now "
            "holds; its open criteria are left out of the replan."
        ),
    },
    "agent_replan_context_incomplete_criterion_unclassified_detail": {
        "de": (
            "Die letzte Prüfung ließ offene Kriterien ohne gültige Einordnung "
            "zurück; sie gehen nicht in die Neuplanung ein."
        ),
        "en": (
            "The last review left open criteria without a valid classification; "
            "they are left out of the replan."
        ),
    },
    "agent_replan_context_incomplete_prospect_missing_detail": {
        "de": (
            "Für diese Runde liegt keine Rechercheaussicht vor; die Neuplanung "
            "erfährt nicht, welches ungelesene Material erreichbar ist."
        ),
        "en": (
            "No research prospect is recorded for this round; the replan is not "
            "told which unread material is reachable."
        ),
    },
    "agent_gate_subject_discovery": {"de": "Erkundung", "en": "Exploration"},
    "agent_gate_subject_plan": {"de": "Plan", "en": "Plan"},
    "agent_gate_rejected_draft_kept": {
        "de": "{subject} abgelehnt; der bisherige Entwurf bleibt das Ergebnis.",
        "en": "{subject} rejected; the existing draft remains the result.",
    },
    "agent_gate_rejected_receipt": {
        "de": "{subject} abgelehnt. Es wurde keine Recherche ausgeführt.",
        "en": "{subject} rejected. No research was carried out.",
    },
    "agent_gate_rejected_note_prefix": {"de": "> Notiz: ", "en": "> Note: "},
    "agent_gate_rejected_resubmit": {
        "de": (
            "\n\nPasse den Auftrag an und sende ihn erneut, um einen neuen "
            "Plan zu erhalten."
        ),
        "en": (
            "\n\nAdjust the assignment and send it again to get a new plan."
        ),
    },
    "agent_gate_rejected_title": {
        "de": "{subject} abgelehnt", "en": "{subject} rejected",
    },
    "agent_gate_rejected_no_research": {
        "de": "{subject} abgelehnt; der Lauf endet ohne Recherche.",
        "en": "{subject} rejected; the run ends without research.",
    },
    "agent_sufficiency_skipped_label": {
        "de": "Zusätzliche Abdeckungsbewertung nicht erforderlich",
        "en": "Additional coverage assessment not required",
    },
    "agent_sufficiency_skipped_detail": {
        "de": (
            "Keine fehlgeschlagene oder unzureichende Teilaufgabe. Die "
            "Inhaltsprüfung folgt am fertigen Ergebnis."
        ),
        "en": (
            "No failed or insufficient subtask. The content review follows on "
            "the finished result."
        ),
    },
    "agent_critic_unavailable_label": {
        "de": "Prüfung nicht auswertbar", "en": "Review not evaluable",
    },
    "agent_critic_unavailable_detail": {
        "de": (
            "Es liegt kein gültiges, widerspruchsfreies Prüfurteil vor. Das "
            "vorhandene Ergebnis bleibt erhalten, ist dadurch aber nicht als "
            "geprüft bestätigt."
        ),
        "en": (
            "No valid, consistent review verdict is available. The existing "
            "result is kept, but is not confirmed as reviewed."
        ),
    },
    "agent_critic_quote_escalation_label": {
        "de": "Web-Zitate nicht verifiziert", "en": "Web quotes not verified",
    },
    "agent_critic_quote_escalation_detail": {
        "de": (
            "Wörtliche Zitate mit Web-Beleg wurden nicht in den gespeicherten "
            "Auszügen gefunden; sie gehen als Befunde in die Prüfung ein."
        ),
        "en": (
            "Verbatim quotes with web evidence were not found in the stored "
            "excerpts; they join the review's findings."
        ),
    },
    "agent_critic_revise_label": {
        "de": "Prüfung fordert Nachbesserung", "en": "Review requests a revision",
    },
    "agent_critic_revise_detail": {
        "de": "Die benannten Stellen werden überarbeitet.",
        "en": "The named passages are revised.",
    },
    "agent_critic_quote_unanchored": {
        "de": (
            "Wörtliches Zitat mit Web-Beleg ohne Treffer im gespeicherten "
            "Auszug; es steht mehrfach im selben Wortlaut im Text und lässt "
            "sich nicht gezielt ersetzen: „{quote}“"
        ),
        "en": (
            "Verbatim quote with web evidence that matches no stored excerpt; "
            "it occurs several times in identical wording and cannot be "
            "replaced selectively: “{quote}”"
        ),
    },
    "agent_critic_quote_finding": {
        "de": (
            "Dieses wörtliche Zitat steht in einem Absatz mit Web-Beleg, findet "
            "sich aber in keinem gespeicherten Auszug. Durch eine gedeckte "
            "Formulierung aus den Auszügen ersetzen oder ohne Anführungszeichen "
            "als Paraphrase kennzeichnen."
        ),
        "en": (
            "This verbatim quote stands in a paragraph with web evidence but "
            "matches no stored excerpt. Replace it with supported wording from "
            "the excerpts or mark it as a paraphrase without quotation marks."
        ),
    },
    "agent_critic_research_label": {
        "de": "Prüfung fordert Nachrecherche", "en": "Review requested more research",
    },
    "agent_critic_research_detail_items": {
        "de": (
            "Das Memo braucht weitere Evidenz zu: {items} — der Plan wird "
            "additiv erweitert."
        ),
        "en": (
            "The memo needs further evidence on: {items} — the plan is "
            "extended additively."
        ),
    },
    "agent_mission_time_budget_label": {
        "de": "Zeitbudget erreicht", "en": "Time budget reached",
    },
    # {minutes} is the budget in the run's number format ("12", "1,5" /
    # "1.5"); only a whole single minute takes the singular.
    "agent_mission_time_budget_detail_one": {
        "de": (
            "Die Mission hat ihr Zeitbudget von {minutes} Minute Rechenzeit "
            "ausgeschöpft; die Prüfung sieht weiteren Recherchebedarf, das Memo "
            "nennt die Lücken als offene Punkte."
        ),
        "en": (
            "The mission spent its time budget of {minutes} minute of compute; "
            "the review sees further research needed, and the memo names the "
            "gaps as open points."
        ),
    },
    "agent_mission_time_budget_detail_other": {
        "de": (
            "Die Mission hat ihr Zeitbudget von {minutes} Minuten Rechenzeit "
            "ausgeschöpft; die Prüfung sieht weiteren Recherchebedarf, das Memo "
            "nennt die Lücken als offene Punkte."
        ),
        "en": (
            "The mission spent its time budget of {minutes} minutes of compute; "
            "the review sees further research needed, and the memo names the "
            "gaps as open points."
        ),
    },
    "agent_critic_cap_delegated_detail": {
        "de": (
            "Ein Unterauftrag recherchiert höchstens einmal nach; die zweite "
            "Prüfung übernimmt der Elternlauf, das Memo nennt die Lücken als "
            "offene Punkte."
        ),
        "en": (
            "A subtask researches again at most once; the parent run takes the "
            "second review, and the memo names the gaps as open points."
        ),
    },
    "agent_critic_cap_replan_detail": {
        "de": "Die Prüfung sieht weiteren Recherchebedarf, aber das Replan-Limit ist erreicht.",
        "en": "The review sees further research needed, but the replan limit is reached.",
    },
    "agent_critic_plan_task_limit_label": {
        "de": "Keine Aufgabe mehr planbar", "en": "No further task can be planned",
    },
    "agent_critic_plan_task_limit_detail": {
        "de": (
            "Der Plan hat die Höchstzahl an Aufgaben erreicht; eine Neuplanung "
            "könnte keine Aufgabe mehr hinzufügen."
        ),
        "en": (
            "The plan holds the maximum number of tasks; a replan could add no "
            "further task."
        ),
    },
    "agent_critic_replan_invalid_detail": {
        "de": (
            "Der Plan für die Nachrecherche ließ sich nicht gültig aufstellen; "
            "das Memo bleibt mit den benannten Lücken."
        ),
        "en": (
            "No valid plan for the follow-up research could be made; the memo "
            "keeps the named gaps."
        ),
    },
    "agent_critic_replan_rejected_label": {
        "de": "Nachrecherche abgelehnt", "en": "Follow-up research declined",
    },
    "agent_critic_replan_rejected_detail": {
        "de": (
            "Die Freigabe der Nachrecherche wurde abgelehnt; das Memo bleibt mit "
            "den benannten Lücken."
        ),
        "en": (
            "The follow-up research was declined at its approval; the memo keeps "
            "the named gaps."
        ),
    },
    # A research prospect: whether another research round of the run can
    # still reach unread material. {branches} and {codes} are "; "-joined
    # research_branch_* and research_code_* phrases.
    "agent_research_prospect_open_label": {
        "de": "Nachrecherche möglich", "en": "Follow-up research possible",
    },
    "agent_research_prospect_open_detail": {
        "de": "Recherche kann noch ungelesenes Material erreichen: {branches}.",
        "en": "Research can still reach unread material: {branches}.",
    },
    "agent_research_prospect_closed_label": {
        "de": "Nachrecherche ohne Aussicht", "en": "No prospect for follow-up research",
    },
    "agent_research_prospect_closed_detail": {
        "de": (
            "Recherche kann in diesem Lauf kein ungelesenes Material mehr "
            "erreichen: {codes}."
        ),
        "en": "Research can reach no further unread material in this run: {codes}.",
    },
    "agent_research_prospect_gate_detail": {
        "de": "Die Belege dieser Runde gehen ohne Neuplanung in die Synthese.",
        "en": "The evidence of this round goes into the synthesis without a replan.",
    },
    "research_branch_coverage": {
        "de": "ungelesene Teile schon gelesener Dokumente",
        "en": "unread parts of documents already read",
    },
    "research_branch_not_selected": {
        "de": "gefundene, noch nicht gelesene Dokumente",
        "en": "documents found but not yet read",
    },
    "research_branch_collection": {
        "de": "freigegebene Sammlungen, die noch keine Aufgabe durchsucht hat",
        "en": "released collections no task has searched yet",
    },
    "research_branch_web": {"de": "das Web", "en": "the web"},
    "research_code_read_budget_exhausted": {
        "de": "das Lesebudget des Laufs ist aufgebraucht",
        "en": "the run's reading budget is used up",
    },
    "research_code_source_bound_limit": {
        "de": "was ungelesen blieb, erreicht ein erneutes Lesen nicht",
        "en": "reading again cannot reach what stayed unread",
    },
    "research_code_collection_unavailable": {
        "de": "eine noch nicht durchsuchte Sammlung ist nicht mehr verfügbar",
        "en": "a collection not yet searched is no longer available",
    },
    "research_code_web_not_permitted": {
        "de": "das Web ist in diesem Lauf nicht zugelassen",
        "en": "the web is not permitted in this run",
    },
    "research_code_web_not_needed": {
        "de": "der Auftrag braucht nach seiner Einordnung kein Web",
        "en": "as the assignment was read, it needs no web",
    },
    # extraction_limit closes no branch: its own sentence, never one of the
    # {codes} that say nothing unread is reachable.
    "agent_research_prospect_extraction_detail": {
        "de": (
            "In zu Ende gelesenen Dokumenten sah die Auswertung mehr Stellen, als "
            "sie übernehmen konnte."
        ),
        "en": (
            "In documents read to the end, the analysis saw more passages than it "
            "could take over."
        ),
    },
    "agent_memo_manual_edit_hint": {
        "de": (
            "\n\n---\n\n> Hinweis: Das Memo wurde zwischenzeitlich manuell "
            "bearbeitet. Die folgende Aktualisierung stammt aus diesem "
            "Agent-Lauf.\n\n"
        ),
        "en": (
            "\n\n---\n\n> Note: the memo was edited manually in the meantime. "
            "The update below comes from this agent run.\n\n"
        ),
    },
    "agent_collection_not_visible": {
        "de": "Eine ausgewählte Wissenssammlung ist für diese Aufgabe nicht sichtbar oder unbekannt.",
        "en": "A selected knowledge collection is not visible to this task or unknown.",
    },
    # ---- agents/kernel/algorithm.py — deep review, partial receipt ----
    # ---- server/editor_suggestions.py — suggestion validation warnings ----
    "editor_warning_sentence_limit": {
        "de": "Der Vorschlag enthält mehr Sätze als die Anweisung erlaubt.",
        "en": "The suggestion has more sentences than the instruction allows.",
    },
    "editor_warning_not_shortened": {
        "de": (
            "Der Vorschlag ist nicht kürzer als der markierte Text, obwohl die "
            "Anweisung eine Kürzung nahelegt."
        ),
        "en": (
            "The suggestion is not shorter than the selected text, although the "
            "instruction asks to shorten it."
        ),
    },
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
    "research_strategy_deep_perspectives": {"de": "DEEP-Pflichtperspektiven", "en": "DEEP mandatory perspectives"},
    "research_strategy_storm_perspectives": {"de": "STORM-Perspektiven", "en": "STORM perspectives"},
    "research_strategy_falsification": {"de": "Falsifikation", "en": "Falsification"},
    "research_strategy_alternative": {"de": "Alternative Hypothesen", "en": "Alternative hypotheses"},
    "research_strategy_competing": {"de": "Konkurrierende Erklärungen", "en": "Competing explanations"},
    "research_strategy_reformulation": {"de": "Reformulierung", "en": "Reformulation"},
    "research_strategy_crosscheck": {"de": "Cross-Check", "en": "Cross-check"},
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
        "de": "Suchquery-Planung fiel auf die Originalfrage zurück",
        "en": "Search-query planning fell back to the original question",
    },
    "research_algofail_heading": {
        "de": "## ALGO-FAIL: Kein auditierbarer Report erzeugt",
        "en": "## ALGO-FAIL: no auditable report produced",
    },
    "research_algofail_intro": {
        "de": "Der normale Abschlussbericht wurde blockiert, weil ein Kernschritt des Evidence-Pfads fehlgeschlagen ist. Ein quellenbasierter Hard-Fact-Report wäre in diesem Zustand nicht belastbar.",
        "en": "The normal final report was blocked because a core step of the evidence path failed. A source-based hard-fact report would not be dependable in this state.",
    },
    "research_algofail_diagnosis": {
        "de": "## Diagnose",
        "en": "## Diagnosis",
    },
    "research_algofail_run_id": {
        "de": "- Run-ID: `{value}`",
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
        "de": "- Evidenz-Records im Ledger: `{value}`",
        "en": "- Evidence records in the ledger: `{value}`",
    },
    "research_algofail_claim_calls": {
        "de": "- Claim-Extraction-Aufrufe: `{value}`",
        "en": "- Claim-extraction calls: `{value}`",
    },
    "research_algofail_claim_failures": {
        "de": "- Claim-Extraction-Fehler: `{value}`",
        "en": "- Claim-extraction failures: `{value}`",
    },
    "research_algofail_blocking": {
        "de": "## Blockierende Kernfehler",
        "en": "## Blocking core failures",
    },
    "research_algofail_failure_line": {
        "de": "- `{phase}` in Runde `{round}`: `{reason}`",
        "en": "- `{phase}` in round `{round}`: `{reason}`",
    },
    "research_algofail_next_step": {
        "de": "## Nächster Debug-Schritt",
        "en": "## Next debugging step",
    },
    "research_algofail_next_step_text": {
        "de": "Bitte das Forensic-Log mit `uv run python scripts/debug_research_log.py <logfile>` auswerten. Der finale Bericht wurde absichtlich nicht formuliert, damit ein Algorithmusfehler nicht wie ein erfolgreicher degradierter Normalpfad wirkt.",
        "en": "Evaluate the forensic log with `uv run python scripts/debug_research_log.py <logfile>`. The final report was deliberately not written, so that an algorithm failure cannot look like a successful degraded normal path.",
    },
    "research_answer_fallback_warning": {
        "de": "> [!WARNING] Antwort-Synthese-Fallback aktiv\n> {reason}\n> Die folgende Antwort enthält **keine LLM-synthetisierte** Zusammenfassung, sondern den bisherigen Recherche-Kontext in Rohform. Iteration-Marker `_answer_fallback=true`.",
        "en": "> [!WARNING] answer-synthesis fallback active\n> {reason}\n> The answer below contains **no LLM-synthesised** summary, only the research context gathered so far, raw. Iteration marker `_answer_fallback=true`.",
    },
    "research_answer_timeout_reason": {
        "de": "Die Antwort-Synthese hat das LLM-Timeout überschritten.",
        "en": "The answer synthesis exceeded the LLM timeout.",
    },
    "research_no_context_timeout": {
        "de": "Es liegen noch keine Kontextdaten vor. Bitte erneut versuchen, ggf. `REASONING_TIMEOUT` erhöhen oder `MAX_TOTAL_SECONDS` anpassen.",
        "en": "No context data is available yet. Please try again, raising `REASONING_TIMEOUT` or adjusting `MAX_TOTAL_SECONDS` if needed.",
    },
    "research_no_context_provider": {
        "de": "Es liegen noch keine Kontextdaten vor. Bitte erneut versuchen und Provider-Verbindung prüfen.",
        "en": "No context data is available yet. Please try again and check the provider connection.",
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
    "agent_discovery_web_preview_purpose": {
        "de": "Webvorschau in der Erkundung",
        "en": "Web preview during exploration",
    },

    # ---- agents/telemetry.py — retry activity, shown on a disruption ----
    "telemetry_retry_text": {
        "de": "Provider-Versuch {failed}/{max} fehlgeschlagen ({code}); Versuch {next}/{max} startet nach {delay} s.",
        "en": "Provider attempt {failed}/{max} failed ({code}); attempt {next}/{max} starts after {delay} s.",
    },
    "telemetry_intake": {
        "de": "Auftrag wird eingeordnet",
        "en": "The assignment is being classified",
    },
    "telemetry_discovery_analyst": {
        "de": "Erkundung wird ausgewertet",
        "en": "The exploration is being evaluated",
    },
    "telemetry_skill_point_check": {
        "de": "Skill-Eingaben werden geprüft",
        "en": "Skill inputs are being checked",
    },
    "telemetry_plan": {
        "de": "Ausführungsplan wird erstellt",
        "en": "The execution plan is being created",
    },
    "telemetry_contradiction": {
        "de": "Gegenbelege werden geprüft",
        "en": "Counter-evidence is being checked",
    },
    "telemetry_sufficiency": {
        "de": "Beleglage wird bewertet",
        "en": "The evidence base is being assessed",
    },
    "telemetry_synthesis": {
        "de": "Ergebnisse werden zusammengeführt",
        "en": "Results are being merged",
    },
    "telemetry_citation_repair": {
        "de": "Quellenverweise werden geprüft",
        "en": "Source references are being checked",
    },
    "telemetry_answer": {
        "de": "Antwort wird ausgearbeitet",
        "en": "The answer is being drafted",
    },
    "telemetry_memory_reflection": {
        "de": "Sitzungskontext wird aktualisiert",
        "en": "The session context is being updated",
    },
    "telemetry_file_analysis": {
        "de": "Dateiinhalte werden ausgewertet",
        "en": "File contents are being evaluated",
    },
    "telemetry_patch": {
        "de": "Dokumentänderung wird vorbereitet",
        "en": "The document change is being prepared",
    },
    "telemetry_kernel": {
        "de": "Agent-Antwort wird fortgesetzt",
        "en": "The agent answer is being continued",
    },
    "telemetry_context_projection": {
        "de": "Eine kleinere Kontextansicht wird vorbereitet",
        "en": "A smaller context view is being prepared",
    },
    "telemetry_deep_review": {
        # One review for both agent engines: the wording names the step,
        # not the depth the user chose.
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
    # Reader titles of the run diagnostics. The review judges from these
    # records and addresses them by basis_id; a person reads the title.
    "review_diagnostic_text_measurements": {
        "de": "Textmaße der Ausgabe",
        "en": "Text measurements of the output",
    },
    "review_diagnostic_quote_checks": {
        "de": "Zitatprüfung der Ausgabe",
        "en": "Quote check of the output",
    },
    "review_diagnostic_contradictions": {
        "de": "Bekannte Widersprüche des Laufs",
        "en": "Known contradictions of the run",
    },
    "review_diagnostic_execution": {
        "de": "Plan- und Aufgabenstand des Laufs",
        "en": "Plan and task state of the run",
    },
    "review_diagnostic_task_evidence_notes": {
        "de": "Belegnotizen der Teilaufgaben",
        "en": "Evidence notes of the subtasks",
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
        "de": "Die Prüfung lieferte kein Urteil; die Einzelheiten stehen im "
              "Serverprotokoll.",
        "en": "The review delivered no verdict; the details are in the "
              "server log.",
    },
    "review_unavailable_failed": {
        "de": "Die Prüfung brach ab, bevor sie ein Urteil abgeben konnte; "
              "die Einzelheiten stehen im Serverprotokoll.",
        "en": "The review broke off before it could deliver a verdict; the "
              "details are in the server log.",
    },
    "review_unavailable_result_missing": {
        "de": "Die Prüfung lief zu Ende, gab aber kein typisiertes Urteil "
              "ab; die Einzelheiten stehen im Serverprotokoll.",
        "en": "The review ran to the end but delivered no typed verdict; "
              "the details are in the server log.",
    },
    "review_unavailable_context": {
        "de": "Das Prüfmaterial passt in kein Modellfenster; die "
              "Einzelheiten stehen im Serverprotokoll.",
        "en": "The review material fits no model window; the details are in "
              "the server log.",
    },
    "review_unavailable_unchanged_store_error": {
        "de": "Das Prüfergebnis ließ sich nicht speichern; die Einzelheiten "
              "stehen im Serverprotokoll.",
        "en": "The review result could not be stored; the details are in "
              "the server log.",
    },
    "review_unavailable_budget_exhausted": {
        "de": "Die Prüfung erreichte ihr Budget, bevor sie ein Urteil "
              "abgeben konnte; die Einzelheiten stehen im Serverprotokoll.",
        "en": "The review reached its budget before it could deliver a "
              "verdict; the details are in the server log.",
    },
    "review_unavailable_schema_invalid": {
        "de": "Die Prüfung lieferte kein auswertbares Ergebnis; der Rohtext "
              "steht im Serverprotokoll.",
        "en": "The review returned no readable result; the raw text is in the "
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
    "review_quote_unverified": {
        "de": "Wörtliches Zitat mit Web-Beleg ohne Treffer im gespeicherten Auszug: „{quote}“",
        "en": "Verbatim quote with web evidence that matches no stored excerpt: “{quote}”",
    },
    # The research route rewrites the draft next: the line names the round
    # instead of quoting a passage the next text may no longer hold.
    "review_quote_unverified_draft": {
        "de": (
            "Ein wörtliches Zitat mit Web-Beleg im Entwurf der Runde {round} "
            "hatte keinen Treffer im gespeicherten Auszug."
        ),
        "en": (
            "A verbatim quote with web evidence in the round {round} draft "
            "matched no stored excerpt."
        ),
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
    "review_finding_reason_research_route": {
        "de": "Prüfung schickte den Lauf in die Nachrecherche",
        "en": "the review sent the run back to research",
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
    "outcome_extraction_limit": {"de": "Lesegrenze", "en": "reading limit"},
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
        "de": "Verifikation geschuldet, aber nicht gelaufen",
        "en": "Verification was owed and did not run",
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
    "agent_section_role_result_one": {
        "de": "{count} Beleg nachgelesen", "en": "{count} reference re-read",
    },
    "agent_section_role_result_other": {
        "de": "{count} Belege nachgelesen", "en": "{count} references re-read",
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
            "Der Lauf wurde auf deine Entscheidung als Teilstand beendet, nachdem "
            "das konfigurierte {limit_name} erreicht wurde. Es wurde keine "
            "vollständige Synthese vorgetäuscht."
        ),
        "en": (
            "The run ended as a partial result on your decision, after the "
            "configured {limit_name} was reached. No complete synthesis was "
            "simulated."
        ),
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
            "Bewertung, Widerspruchsprüfung und finale Antwort wurden nicht "
            "vollständig abgeschlossen. Für eine belastbare Endantwort muss der "
            "Auftrag mit einem ausreichend hohen, vom Betreiber erlaubten Limit "
            "fortgesetzt oder neu eingegrenzt werden."
        ),
        "en": (
            "Assessment, contradiction check and final answer were not completed "
            "in full. For a dependable final answer the assignment has to continue "
            "under a sufficiently high operator-permitted limit, or be narrowed."
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
        "de": "Ausführungsschritte", "en": "execution steps",
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
        "de": " Die Betreibergrenze {ceiling} lässt keine Erweiterung zu.",
        "en": " The operator limit {ceiling} allows no extension.",
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
        "de": (
            "Der Agent hat {used} {noun} verwendet und damit das aktuelle "
            "Limit {current} erreicht."
        ),
        "en": (
            "The agent has used {used} {noun} and thereby reached the current "
            "limit {current}."
        ),
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
    "agent_subquestion_no_result": {
        "de": "Die Teilfrage „{query}“ lieferte kein verwertbares Ergebnis: {reason}",
        "en": 'The sub-question "{query}" yielded no usable result: {reason}',
    },
    # Search notices (knowledge.retrieval_warnings): the reader wording of
    # every search code. Counts are hits or exclusions over searches, never
    # documents; a code or an English API message never appears here.
    "search_notice_heading": {"de": "Suchhinweis:", "en": "Search note:"},
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
