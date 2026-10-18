"""Answer-language resolution and the language directives — one source of truth.

Every prompt that produces user-visible text asks this module which language
the answer must be written in and how to say so to the model. The directive
is phrased in the target language itself: empirically a single "answer in X"
line inside a prompt written in another language is too weak, large models
drift back to the prompt's dominant language. The German directive carries
the orthography rule (real umlauts, never ae/oe/ue/ss); no other language
gets a German sentence appended.

Resolution order (``resolve_answer_language``): an explicit classification
(the research classifier reports the question language) wins, then a
confident detection of the question text, then the UI language the client
reported, then, without one, a detection that accepts a lead of one function
word. The UI language only breaks a tie: its directive asks for the
language of the question first and names the UI language for the case the
question leaves open. When nothing is known the directive is
language-neutral — "answer in the language of the question" — and the caller
sees that in ``source`` so the fallback is never silent.
"""

from __future__ import annotations

from dataclasses import dataclass

from inqtrix.i18n import detect_ui_language_confident

LANGUAGE_NAMES_EN: dict[str, str] = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
}
"""English display names for the languages the directives are written in."""

# The research state still names languages in German (``answer_lang``); the
# directives are keyed by ISO code, so the legacy names map onto codes here
# until every caller passes codes.
_LEGACY_NAME_TO_CODE: dict[str, str] = {
    "Deutsch": "de",
    "Englisch": "en",
    "Franzoesisch": "fr",
    "Französisch": "fr",
    "Spanisch": "es",
    "Italienisch": "it",
    "Portugiesisch": "pt",
}

GERMAN_ORTHOGRAPHY = (
    "in korrekter deutscher Rechtschreibung mit Umlauten (ä, ö, ü, ß), "
    "nie als ae/oe/ue/ss"
)
"""The orthography rule that rides only with the German directive."""

_DIRECTIVES: dict[str, str] = {
    "de": (
        "Antworte IMMER auf Deutsch, unabhängig von der Sprache der "
        "Recherche-Ergebnisse oder der Anweisungen in diesem Prompt — "
        f"{GERMAN_ORTHOGRAPHY}."
    ),
    "en": (
        "ALWAYS respond in English, regardless of the language of any research "
        "results or instructions in this prompt. Do not switch back to German "
        "even when the instructions, section headings, or sources are in German."
    ),
    "fr": (
        "Réponds TOUJOURS en français, quelle que soit la langue des résultats "
        "de recherche ou des instructions dans ce prompt."
    ),
    "es": (
        "Responde SIEMPRE en español, independientemente del idioma de los "
        "resultados de búsqueda o de las instrucciones de este prompt."
    ),
    "it": (
        "Rispondi SEMPRE in italiano, indipendentemente dalla lingua dei "
        "risultati di ricerca o dalle istruzioni in questo prompt."
    ),
    "pt": (
        "Responde SEMPRE em português, independentemente do idioma dos "
        "resultados de pesquisa ou das instruções deste prompt."
    ),
}

QUESTION_LANGUAGE_DIRECTIVE = (
    "ALWAYS respond in the language of the user's question, regardless of the "
    "language of any research results or of the instructions in this prompt. "
    "German answers use correct German spelling with umlauts (ä, ö, ü, ß), "
    "never ae/oe/ue/ss."
)
"""The neutral directive used when the answer language could not be resolved."""

_UI_FALLBACK_DIRECTIVES: dict[str, str] = {
    "de": (
        "Antworte in der Sprache der Frage, unabhängig von der Sprache der "
        "Recherche-Ergebnisse oder der Anweisungen in diesem Prompt; ist sie "
        "nicht eindeutig, antworte auf Deutsch. Deutsche Antworten schreibst "
        f"du {GERMAN_ORTHOGRAPHY}."
    ),
    "en": (
        "Respond in the language of the user's question, regardless of the "
        "language of any research results or of the instructions in this "
        "prompt; if the question's language is unclear, respond in English. "
        "German answers use correct German spelling with umlauts (ä, ö, ü, ß), "
        "never ae/oe/ue/ss."
    ),
}
"""The directive when only the UI language is known, in the UI language: the
question's own language comes first, the UI language breaks the tie."""


HEADINGS: dict[str, dict[str, str]] = {
    "de": {
        "summary": "Kurzfazit",
        "key_points": "Kernaussagen",
        "detail_analysis": "Detailanalyse",
        "context_outlook": "Einordnung / Ausblick",
        "executive_summary": "Executive Summary",
        "background": "Hintergrund / Kontext",
        "analysis": "Analyse",
        "perspectives": "Perspektiven / Positionen",
        "risks": "Risiken / Unsicherheiten",
        "conclusion": "Fazit / Ausblick",
        "uncertainties": "Unsicherheiten / Offene Punkte",
        "evidence_base": "Quellenlage",
        "open_points": "Offene Punkte",
    },
    "en": {
        "summary": "Summary",
        "key_points": "Key points",
        "detail_analysis": "Detailed analysis",
        "context_outlook": "Context and outlook",
        "executive_summary": "Executive summary",
        "background": "Background and context",
        "analysis": "Analysis",
        "perspectives": "Perspectives and positions",
        "risks": "Risks and uncertainties",
        "conclusion": "Conclusion and outlook",
        "uncertainties": "Uncertainties and open points",
        "evidence_base": "Evidence base",
        "open_points": "Open points",
    },
}
"""User-visible section headings by answer language.

Prompts are written in English, but a heading is part of the answer and
must read in the answer's language; the section plans and every prompt
that names a section resolve the text through :func:`heading_for`.
Languages without a table fall back to English headings."""

MARKERS: dict[str, dict[str, str]] = {
    "de": {"unconfirmed": "(unbestätigt)"},
    "en": {"unconfirmed": "(unconfirmed)"},
}
"""Inline markers the model writes into the answer, by answer language."""


def _language_table(table: dict[str, dict[str, str]], language: str | None) -> dict[str, str]:
    code = normalize_language_code(language)
    return table.get(code or "", table["en"])


def heading_for(key: str, language: str | None) -> str:
    """The heading text for a section ``key`` in ``language``.

    An unknown key is returned verbatim so extension hooks may define
    custom section plans with literal headings.
    """
    return _language_table(HEADINGS, language).get(key, key)


def marker_for(key: str, language: str | None) -> str:
    """The inline marker for ``key`` in ``language``."""
    return _language_table(MARKERS, language)[key]


def date_line(today: str) -> str:
    """The date line every dated prompt opens with."""
    return f"Today's date: {today}."


OUTPUT_FORMAT_BLOCK = (
    "OUTPUT FORMAT (the renderer supports exactly this):\n"
    "- GitHub-flavoured Markdown: headings with #, lists, blockquotes. "
    "No horizontal rules (headings carry the structure), no HTML, no emojis.\n"
    "- Tables: when two or more entities are compared on two or more "
    "attributes, use a GFM table (at most 7 columns; split wider "
    "comparisons). Currency and units are text: write \"USD 5\", never \"$5\".\n"
    "- Math: inline $...$ for symbols and short expressions inside a "
    "sentence; display $$...$$ on its own lines for equations. A dollar "
    "sign is a math delimiter only.\n"
    "- Code: fenced blocks with a language tag; inline code for identifiers.\n"
    "- Diagrams: a ```mermaid block only when a structure, sequence or "
    "process is clearer as a picture. Supported: flowchart (TD; LR only "
    "for chains of at most five short nodes), sequenceDiagram, "
    "classDiagram, stateDiagram-v2, erDiagram, gantt, pie, xychart-beta. "
    "Every diagram, a plot too, is a ```mermaid block: its first line is "
    "the diagram keyword, its second line `accTitle: <short title>`; "
    "nothing comes before the keyword. No %%{init}%% directives, no "
    "inline styling. In a flowchart a node label that contains a bracket "
    "of any kind or a pipe must be in double quotes: write A[\"v(t)\"], "
    "not A[v(t)].\n"
    "- Plots: an xychart-beta diagram, bars or lines only, from numbers "
    "present in the evidence or the task; at most two series and 12 "
    "points per series; label both axes with units; name the series in "
    "the title (there is no legend). Every title, axis label and x-axis "
    "category is in double quotes, even a single word: unquoted text with "
    "a letter outside A-Z, such as an umlaut, breaks the plot. Two plot "
    "blocks written directly after each other are shown side by side. "
    "Prefer a table when exact values matter more than the trend. A plot "
    "looks like this:\n"
    "```mermaid\n"
    "xychart-beta\n"
    "  accTitle: Output per month\n"
    "  title \"Output per month in t\"\n"
    "  x-axis \"Month\" [\"Jan\", \"Feb\", \"Mar\"]\n"
    "  y-axis \"Output in t\" 0 --> 100\n"
    "  bar [71, 68, 78]\n"
    "```\n"
    "- Nothing decorative: every table cell and diagram node must trace "
    "back to evidence or the task.\n"
    "- Explicit format instructions from the user, an attached template "
    "or a skill override these defaults."
)
"""What the frontend renderer supports — one text for every answer path.

Cross-reference: apps/research-desk/src/components/markdown/
MarkdownRenderer.tsx (remark-gfm, rehype-katex, bounded Shiki
highlighting, MermaidFigure with the promised grammars). The drift test
in tests/agents/test_prompts_rendering.py asserts each feature is named
here — removing one from the renderer must update BOTH places."""


WRITING_STYLE_BLOCK = (
    "WRITING STYLE:\n"
    "- Answer the question that was asked, directly and in the language of "
    "the question.\n"
    "- Write in full sentences and paragraphs. Use a list only when the content "
    "is genuinely enumerable (steps, options, a comparison) or the user asks for "
    "one; never turn an explanation into bullet points.\n"
    "- Explain complex topics like a well-written academic lecture script: define "
    "terms on first use, build from the fundamentals, give each formula with the "
    "meaning of its symbols and a short worked example.\n"
    "- Match length to the question: a few sentences for a simple question, depth "
    "for a complex one. No preamble, no filler, no closing offers, no praise of "
    "the question.\n"
    "- Be honest: say plainly when you do not know something or cannot verify a "
    "figure, a name or a source; separate established facts from your own "
    "assessment; never invent sources, numbers or quotes.\n"
    "- When the question is ambiguous, answer the most plausible reading, state "
    "the assumption, and ask only when the readings lead to different answers.\n"
    "- When asked for a recommendation, give one, with the decisive trade-offs. "
    "Push back constructively when a premise is wrong."
)
"""The house writing style for conversational answers — the direct chat and
the Agent Desk (kernel answers and the mission's chat answer). Research
reports keep the form their report profile prescribes per section, so the
research writer does not carry this block. Wording follows the published
practice of the large assistants: prose by default and lists only for
enumerable content, honesty about the unknown, no filler."""


def normalize_language_code(value: str | None) -> str | None:
    """Return a lower-case ISO-639-1 code, or ``None`` when nothing usable.

    Accepts codes (``"de"``, ``"DE"``, ``"de-DE"``) and the legacy German
    language names the research state carries (``"Deutsch"``).
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text in _LEGACY_NAME_TO_CODE:
        return _LEGACY_NAME_TO_CODE[text]
    code = text.lower().replace("_", "-").split("-", 1)[0]
    return code if code.isalpha() and 2 <= len(code) <= 3 else None


def directive_for(language: str | None) -> str:
    """The language directive for ``language`` (code or legacy name).

    Known languages get their native-language directive; an unknown but
    named language gets an English directive naming it; ``None`` gets the
    neutral "language of the question" directive.
    """
    if not language or not language.strip():
        return QUESTION_LANGUAGE_DIRECTIVE
    code = normalize_language_code(language)
    if code in _DIRECTIVES:
        return _DIRECTIVES[code]
    # A code without a native directive keeps its English name when known;
    # anything else (a spelled-out language name) is named as given.
    name = (LANGUAGE_NAMES_EN.get(code) if code else None) or (
        code if code else language.strip()
    )
    return (
        f"ALWAYS respond in {name}, regardless of the language of any research "
        "results or of the instructions in this prompt."
    )


def ui_fallback_directive_for(language: str) -> str:
    """The directive for a question without a clear language, answered in the
    UI language ``language`` only where the question leaves it open."""
    code = normalize_language_code(language)
    if code in _UI_FALLBACK_DIRECTIVES:
        return _UI_FALLBACK_DIRECTIVES[code]
    name = (LANGUAGE_NAMES_EN.get(code) if code else None) or (
        code if code else language.strip()
    )
    return (
        "Respond in the language of the user's question, regardless of the "
        "language of any research results or of the instructions in this "
        f"prompt; if the question's language is unclear, respond in {name}."
    )


@dataclass(frozen=True)
class AnswerLanguage:
    """The resolved answer language and where it came from."""

    code: str | None
    source: str
    """``classified`` (question classifier), ``detected`` (confident text
    heuristic), ``ui`` (client-reported UI language) or ``unresolved``."""

    @property
    def directive(self) -> str:
        if self.source == "ui" and self.code:
            return ui_fallback_directive_for(self.code)
        return directive_for(self.code)


def resolve_answer_language(
    question: str,
    *,
    classified: str | None = None,
    ui_language: str | None = None,
) -> AnswerLanguage:
    """Resolve the answer language for ``question``.

    Never defaults silently to one language: when neither the classifier,
    the text heuristic nor the UI language gives a signal, the result is
    ``unresolved`` and the directive becomes language-neutral.
    """
    code = normalize_language_code(classified)
    if code:
        return AnswerLanguage(code=code, source="classified")
    detected = detect_ui_language_confident(question)
    if detected:
        return AnswerLanguage(code=detected, source="detected")
    code = normalize_language_code(ui_language)
    if code:
        return AnswerLanguage(code=code, source="ui")
    # Without an interface language a lead of one function word still
    # decides, a weak signal beating none: a short German question stays
    # German. Where the client sends the interface language, it outranks
    # that signal, which a German question around English titles tips.
    detected = detect_ui_language_confident(question, lead=1)
    if detected:
        return AnswerLanguage(code=detected, source="detected")
    return AnswerLanguage(code=None, source="unresolved")
