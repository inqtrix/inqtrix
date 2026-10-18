"""Answer-language resolution and directives (``inqtrix.prompt_language``)."""

from __future__ import annotations

import re

from inqtrix.prompt_language import (
    GERMAN_ORTHOGRAPHY,
    HEADINGS,
    OUTPUT_FORMAT_BLOCK,
    QUESTION_LANGUAGE_DIRECTIVE,
    WRITING_STYLE_BLOCK,
    date_line,
    directive_for,
    heading_for,
    marker_for,
    normalize_language_code,
    resolve_answer_language,
)


def test_german_directive_carries_the_orthography_rule_only_for_german() -> None:
    german = directive_for("de")
    assert german.startswith("Antworte IMMER auf Deutsch")
    assert GERMAN_ORTHOGRAPHY in german
    assert "unabhängig" in german and "unabhaengig" not in german
    english = directive_for("en")
    assert english.startswith("ALWAYS respond in English")
    assert "Umlaut" not in english and "ae/oe/ue" not in english


def test_directives_are_phrased_in_the_target_language() -> None:
    assert directive_for("fr").startswith("Réponds TOUJOURS en français")
    assert directive_for("es").startswith("Responde SIEMPRE en español")
    assert directive_for("it").startswith("Rispondi SEMPRE in italiano")
    assert directive_for("pt").startswith("Responde SEMPRE em português")


def test_legacy_german_names_and_region_codes_resolve() -> None:
    assert normalize_language_code("Deutsch") == "de"
    assert normalize_language_code("Englisch") == "en"
    assert normalize_language_code("de-DE") == "de"
    assert normalize_language_code("EN") == "en"
    assert normalize_language_code("") is None
    assert normalize_language_code(None) is None
    assert directive_for("Franzoesisch") == directive_for("fr")


def test_unknown_language_gets_an_english_directive_naming_it() -> None:
    assert directive_for("Esperanto") == (
        "ALWAYS respond in Esperanto, regardless of the language of any "
        "research results or of the instructions in this prompt."
    )
    assert directive_for("nl").startswith("ALWAYS respond in nl")


def test_unresolved_language_gets_the_question_language_directive() -> None:
    assert directive_for(None) == QUESTION_LANGUAGE_DIRECTIVE
    assert "language of the user's question" in QUESTION_LANGUAGE_DIRECTIVE


def test_resolution_order_classifier_then_text_then_ui_then_unresolved() -> None:
    classified = resolve_answer_language("What is this?", classified="de", ui_language="en")
    assert (classified.code, classified.source) == ("de", "classified")
    detected = resolve_answer_language(
        "Wie funktioniert die Energiebilanz eines Speichers und was ist wichtig?",
        ui_language="en",
    )
    assert (detected.code, detected.source) == ("de", "detected")
    english = resolve_answer_language(
        "How does the energy balance of a storage system work and what matters?",
    )
    assert (english.code, english.source) == ("en", "detected")
    ui = resolve_answer_language("DSGVO Pflichten", ui_language="fr")
    assert (ui.code, ui.source) == ("fr", "ui")
    unresolved = resolve_answer_language("DSGVO Pflichten")
    assert (unresolved.code, unresolved.source) == (None, "unresolved")
    assert unresolved.directive == QUESTION_LANGUAGE_DIRECTIVE


UNCLEAR_QUESTION = "Was verlangt Measure 7.6 im Code of Practice, Kapitel Safety and Security?"
"""A German frame around English titles: no clear language."""


def test_an_unclear_question_falls_back_to_the_interface_language_softly() -> None:
    """The interface language decides only in doubt: the directive asks for
    the question's language first and names the interface language for the
    case the question leaves open, never ALWAYS."""
    german = resolve_answer_language(UNCLEAR_QUESTION, ui_language="de")
    english = resolve_answer_language(UNCLEAR_QUESTION, ui_language="en")
    assert (german.code, german.source, english.code, english.source) == ("de", "ui", "en", "ui")
    assert german.directive.startswith("Antworte in der Sprache der Frage")
    assert GERMAN_ORTHOGRAPHY in german.directive
    assert english.directive.startswith("Respond in the language of the user's question")
    assert "IMMER" not in german.directive and "ALWAYS" not in english.directive


def test_without_an_interface_language_a_lead_of_one_still_decides() -> None:
    """A desk that sends no interface language keeps the weak signal, so a
    short German question stays German; where the interface language is
    known, it outranks a lead of one."""
    weak = resolve_answer_language("Lies bitte den archivierten Text.")
    outranked = resolve_answer_language("Lies bitte den archivierten Text.", ui_language="en")
    assert ((weak.code, weak.source), (outranked.code, outranked.source)) == (
        ("de", "detected"), ("en", "ui"),
    )


def test_a_clear_question_keeps_its_language_whatever_the_interface_shows() -> None:
    resolved = resolve_answer_language(
        "Wie ist die Haftung im Rahmenvertrag geregelt?", ui_language="en",
    )
    assert (resolved.source, resolved.directive) == ("detected", directive_for("de"))


def test_headings_and_markers_resolve_per_language_and_fall_back_to_english() -> None:
    assert heading_for("summary", "de") == "Kurzfazit"
    assert heading_for("summary", "en") == "Summary"
    assert heading_for("summary", "Deutsch") == "Kurzfazit"
    assert heading_for("summary", "fr") == "Summary"
    assert heading_for("summary", None) == "Summary"
    # A custom section plan may name a section literally.
    assert heading_for("Open questions", "de") == "Open questions"
    assert set(HEADINGS["de"]) == set(HEADINGS["en"])
    assert marker_for("unconfirmed", "de") == "(unbestätigt)"
    assert marker_for("unconfirmed", "en") == "(unconfirmed)"
    assert marker_for("unconfirmed", None) == "(unconfirmed)"
    assert date_line("2026-09-10") == "Today's date: 2026-09-10."


def test_output_format_block_states_the_renderer_contract() -> None:
    block = OUTPUT_FORMAT_BLOCK
    assert block.startswith("OUTPUT FORMAT")
    for phrase in (
        "No horizontal rules",
        "no HTML, no emojis",
        "at most 7 columns",
        'write "USD 5", never "$5"',
        "inline $...$",
        "display $$...$$",
        "fenced blocks with a language tag",
        "flowchart (TD; LR only",
        "accTitle: <short title>",
        "No %%{init}%%",
        "xychart-beta",
        "bars or lines only",
        "at most two series and 12 points",
        "side by side",
        "Nothing decorative",
        "override these defaults",
    ):
        assert phrase in block, phrase
    assert "---" not in block
    assert block.isascii()


def test_every_diagram_is_a_mermaid_block_that_opens_with_its_keyword() -> None:
    """Mermaid reads the diagram type from the first line, and the renderer
    draws only ```mermaid blocks: an accTitle before the keyword is "No
    diagram type detected", a ```xychart-beta fence stays plain code, and an
    unquoted umlaut in a plot is a lexical error. The block teaches the one
    form that renders, and its example shows that form."""
    block = OUTPUT_FORMAT_BLOCK
    assert "Every diagram, a plot too, is a ```mermaid block" in block
    assert (
        "its first line is the diagram keyword, its second line "
        "`accTitle: <short title>`" in block
    )
    assert "nothing comes before the keyword" in block
    assert "Every title, axis label and x-axis category is in double quotes" in block
    assert "Start every diagram with" not in block
    assert "Plots (xychart-beta)" not in block
    example = block[block.index("```mermaid\n"):].split("\n")
    body = example[1:example.index("```", 1)]
    assert body[0] == "xychart-beta"
    assert body[1].strip().startswith("accTitle: ")
    for line in body[2:]:
        keyword, _, rest = line.strip().partition(" ")
        if keyword in {"title", "x-axis", "y-axis"}:
            assert not re.search(r"[A-Za-z]", re.sub(r'"[^"]*"', "", rest)), line


def test_writing_style_block_states_the_house_style() -> None:
    block = WRITING_STYLE_BLOCK
    assert block.startswith("WRITING STYLE:")
    for phrase in (
        "in the language of the question",
        "Write in full sentences and paragraphs",
        "Use a list only when the content is genuinely enumerable",
        "never turn an explanation into bullet points",
        "academic lecture script",
        "meaning of its symbols and a short worked example",
        "Match length to the question",
        "no closing offers, no praise of the question",
        "say plainly when you do not know something",
        "never invent sources, numbers or quotes",
        "answer the most plausible reading, state the assumption",
        "give one, with the decisive trade-offs",
        "Push back constructively",
    ):
        assert phrase in block, phrase
    assert block.isascii()
