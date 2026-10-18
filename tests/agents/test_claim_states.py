"""Every statement an output makes has one state after its review.

Pinned on literal texts: which sentences form a statement, which state each
gets from its cited material and the findings that still stand, and which
statements carry the mark of a statement a finding marked.
"""

from __future__ import annotations

import pytest

from inqtrix.agents.kernel.claim_states import (
    Material,
    Standing,
    statement_states,
    statements,
)

PROVIDER = Material("provider_context")
META = Material("source_metadata_only")


def stored(excerpt: str) -> Material:
    return Material("retained_source_excerpt", excerpt)


def states_of(text: str, **kwargs) -> list[tuple[str, str, str]]:
    return [
        (state.statement.text, state.state, state.reason)
        for state in statement_states(text, **kwargs)
    ]


# -- what a statement is ----------------------------------------------------- #


def test_a_statement_runs_to_the_citation_group_that_closes_it():
    text = (
        "Die Förderung wurde gesenkt. Sie beträgt jetzt 30 Prozent der Kosten [W1]. "
        "Der Klimabonus entfällt ganz [W2] [W3]."
    )
    assert [(item.text, item.labels) for item in statements(text)] == [
        ("Die Förderung wurde gesenkt. Sie beträgt jetzt 30 Prozent der Kosten [W1].", ("W1",)),
        ("Der Klimabonus entfällt ganz [W2] [W3].", ("W2", "W3")),
    ]


def test_each_list_item_and_table_row_is_a_statement_of_its_own():
    text = (
        "- Grundförderung bleibt unverändert [W1].\n"
        "- Klimabonus sinkt deutlich [W2].\n\n"
        "| Punkt | Wert |\n|---|---|\n"
        "| Grundförderung | 30 % [W1] |\n"
        "| Einkommensbonus | 40 % [W2] |"
    )
    assert [item.text for item in statements(text)] == [
        "- Grundförderung bleibt unverändert [W1].",
        "- Klimabonus sinkt deutlich [W2].",
        "| Grundförderung | 30 % [W1] |",
        "| Einkommensbonus | 40 % [W2] |",
    ]


def test_sentences_after_the_last_citation_of_their_unit_belong_to_no_statement():
    """What an output says without a source the review judges; its
    findings mark it, the count does not."""
    text = (
        "Die Grundförderung bleibt unverändert [W1]. Das klingt zunächst beruhigend.\n\n"
        "Der Zuschuss liegt seit Juli bei 28.000 Euro je Wohnung."
    )
    assert [(item.text, item.labels) for item in statements(text)] == [
        ("Die Grundförderung bleibt unverändert [W1].", ("W1",)),
    ]


def test_a_fragment_lends_its_citations_to_the_statement_before_it():
    """An author-year group or a short tag after a statement states nothing
    of its own; its citations belong to the statement it follows."""
    text = (
        "Das Modell erschien diese Woche mit neuen Funktionen. [W1] "
        "(Anbieter, 2026) [W2] (Anbieter, 2026) [W3]\n\n"
        "Stand: 10. Oktober 2026"
    )
    assert [(item.text, item.labels) for item in statements(text)] == [(
        "Das Modell erschien diese Woche mit neuen Funktionen. [W1] "
        "(Anbieter, 2026) [W2] (Anbieter, 2026) [W3]",
        ("W1", "W2", "W3"),
    )]


def test_a_line_naming_a_web_address_is_a_reference_entry_not_a_statement():
    text = (
        "- Anbieter. (2026). *Neue Funktionen im Überblick*. Anbieter. "
        "https://example.org/news [W1]\n"
        "- Die [Übersichtsseite](https://example.org/news) nennt drei neue Funktionen [W1]."
    )
    assert [item.text for item in statements(text)] == [
        "- Die [Übersichtsseite](https://example.org/news) nennt drei neue Funktionen [W1].",
    ]


def test_headings_and_code_blocks_state_nothing():
    text = "## Förderung 2026 [W1]\n\n```\nRate = 30 % [W1]\n```\n\nDie Rate beträgt 30 Prozent [W1]."
    assert [item.text for item in statements(text)] == ["Die Rate beträgt 30 Prozent [W1]."]


def test_an_output_that_cites_nothing_holds_no_statement_to_count():
    """A pointer to the document a run wrote cites nothing of its own."""
    text = (
        "notiz.md ist erstellt. Die Notiz fasst zusammen, was bis August 2026 "
        "zu beachten ist."
    )
    assert statement_states(text, materials={}) == []


def test_a_statement_stands_where_the_text_holds_it():
    text = "Erster Absatz ohne Wert.\n\n- Die Rate beträgt 30 Prozent [W1]."
    [item] = statements(text)
    assert text[item.start:item.end] == item.text


# -- the state of a statement ------------------------------------------------ #


EXCERPT = "Der Zuschuss für die Heizung beträgt seit Juli 30 Prozent der förderfähigen Kosten."


@pytest.mark.parametrize(
    ("text", "materials", "expected"),
    [
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [K1].",
            {"K1": stored(EXCERPT)}, ("verified", ""), id="stored-text-states-it",
        ),
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 40 Prozent der Kosten [K1].",
            {"K1": stored(EXCERPT)}, ("source_text", ""), id="stored-text-without-its-value",
        ),
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [W1] [K1].",
            {"W1": PROVIDER, "K1": stored(EXCERPT)}, ("verified", ""),
            id="stored-text-beside-provider-context",
        ),
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [W1].",
            {"W1": PROVIDER}, ("attributed", ""), id="provider-context-alone",
        ),
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [W1].",
            {"W1": META}, ("insufficient", "metadata_only"), id="metadata-only",
        ),
        pytest.param(
            "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [W9].",
            {}, ("insufficient", "metadata_only"), id="a-label-without-material",
        ),
    ],
)
def test_the_cited_material_decides_the_state(text, materials, expected):
    [state] = statement_states(text, materials=materials)
    assert (state.state, state.reason) == expected




def test_a_standing_evidence_finding_marks_every_statement_its_passage_touches():
    text = (
        "- Klimabonus: 16 Prozent [W1]\n"
        "- Einkommensbonus: 30 Prozent bis 40.000 Euro [W1]\n"
        "- Effizienzbonus entfällt vollständig [W2]"
    )
    standing = [Standing("16 Prozent [W1]\n- Einkommensbonus: 30", "Nicht im Kontext.")]
    assert states_of(text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing) == [
        ("- Klimabonus: 16 Prozent [W1]", "insufficient", "finding"),
        ("- Einkommensbonus: 30 Prozent bis 40.000 Euro [W1]", "insufficient", "finding"),
        ("- Effizienzbonus entfällt vollständig [W2]", "attributed", ""),
    ]


def test_a_finding_marks_its_passage_wherever_it_stands():
    """A passage that stands twice carries the finding twice."""
    text = "Der Bonus beträgt 16 Prozent [W1].\n\nZusammengefasst: Der Bonus beträgt 16 Prozent [W1]."
    standing = [Standing("Der Bonus beträgt 16 Prozent [W1].", "Nicht im Kontext.")]
    states = statement_states(text, materials={"W1": PROVIDER}, standing=standing)
    assert [(state.state, state.finding) for state in states] == [
        ("insufficient", "Nicht im Kontext."), ("insufficient", "Nicht im Kontext."),
    ]


def test_a_passage_the_text_no_longer_holds_marks_nothing():
    text = "Der Bonus beträgt 16 Prozent [W1]."
    standing = [Standing("Der Bonus beträgt 20 Prozent [W1].", "Nicht im Kontext.")]
    assert states_of(text, materials={"W1": PROVIDER}, standing=standing) == [
        ("Der Bonus beträgt 16 Prozent [W1].", "attributed", ""),
    ]


def test_a_statement_a_revision_wrote_is_verified_only_when_the_recheck_held_it():
    """An edit the re-check did not hold reads as stored text at most; a
    statement the reviewed text already held keeps its own state."""
    reviewed = "Die Heizungsförderung ist bekannt [K1]."
    text = (
        "Die Heizungsförderung ist bekannt [K1]. "
        "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [K1]."
    )
    materials = {"K1": stored(EXCERPT + " Die Heizungsförderung ist bekannt.")}
    held = states_of(text, materials=materials)
    unheld = states_of(text, materials=materials, revised_from=reviewed)
    assert ([state for _text, state, _reason in held], [state for _text, state, _reason in unheld]) == (
        ["source_text", "verified"], ["source_text", "source_text"],
    )


# -- repeating a marked statement -------------------------------------------- #


def test_a_restatement_of_a_marked_value_carries_the_mark():
    text = (
        "Kurz gesagt: Der Zuschuss liegt bei höchstens 28.000 Euro [W2].\n\n"
        "| Punkt | Wert |\n|---|---|\n"
        "| Förderfähige Kosten | 28.000 Euro [W1] |\n"
        "| Grundförderung | 30 % [W1] |"
    )
    standing = [Standing("| Förderfähige Kosten | 28.000 Euro [W1] |", "Nicht im Kontext.")]
    assert states_of(text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing) == [
        ("Kurz gesagt: Der Zuschuss liegt bei höchstens 28.000 Euro [W2].", "insufficient", "restates"),
        ("| Förderfähige Kosten | 28.000 Euro [W1] |", "insufficient", "finding"),
        ("| Grundförderung | 30 % [W1] |", "attributed", ""),
    ]


def test_a_bare_year_carries_no_mark():
    """A year stands in too many statements to tell a repetition."""
    text = (
        "Der Bonus sinkt 2026 auf 16 Prozent [W1].\n\n"
        "Für 2026 gilt außerdem eine neue Einkommensgrenze [W2]."
    )
    standing = [Standing("Der Bonus sinkt 2026 auf 16 Prozent [W1].", "Nicht im Kontext.")]
    assert [state for _text, state, _reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == ["insufficient", "attributed"]


def test_the_date_a_marked_value_holds_from_carries_no_mark():
    """The date that says when a marked percentage holds is not what the
    finding disputes: a sentence naming that date repeats nothing marked."""
    text = (
        "Die Förderung ist seit dem 21.07.2026 sozial gestaffelt [W1].\n\n"
        "| Bonus | 2025 | 2026 |\n|---|---|---|\n"
        "| Klimabonus | 20 Prozent [W6] | 16 Prozent seit 21.07.2026 [W1] |"
    )
    standing = [Standing("| Klimabonus | 20 Prozent [W6] | 16 Prozent seit 21.07.2026 [W1] |", "Nicht im Kontext.")]
    states = states_of(text, materials={"W1": PROVIDER, "W6": PROVIDER}, standing=standing)
    assert states[0] == ("Die Förderung ist seit dem 21.07.2026 sozial gestaffelt [W1].", "attributed", "")
    assert {reason for _text, _state, reason in states[1:]} == {"finding"}


def test_a_disputed_date_carries_the_mark_where_it_is_restated():
    """A statement that states a date alone puts that date in dispute."""
    text = (
        "Die Transparenzpflichten gelten ab dem 2. August 2026 [W1].\n\n"
        "Der Anwendungszeitpunkt ist der 2. August 2026 [W2]."
    )
    standing = [Standing("Die Transparenzpflichten gelten ab dem 2. August 2026 [W1].", "Nicht im Kontext.")]
    assert [(state, reason) for _text, state, reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == [("insufficient", "finding"), ("insufficient", "restates")]


def test_a_disputed_date_carries_the_mark_where_it_frames_another_value():
    """A repetition may name the disputed date as the time of another value."""
    text = (
        "Die Transparenzpflichten gelten ab dem 2. August 2026 [W1].\n\n"
        "Ab dem 2. August 2026 drohen Bußgelder bis 15 Millionen Euro [W2]."
    )
    standing = [Standing("Die Transparenzpflichten gelten ab dem 2. August 2026 [W1].", "Nicht im Kontext.")]
    assert [(state, reason) for _text, state, reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == [("insufficient", "finding"), ("insufficient", "restates")]


def test_a_restatement_with_its_own_stored_text_keeps_its_state():
    text = (
        "Der Zuschuss für die Heizung beträgt 30 Prozent der Kosten [K1].\n\n"
        "- Grundförderung: 30 Prozent [W1]"
    )
    standing = [Standing("- Grundförderung: 30 Prozent [W1]", "Nicht im Kontext.")]
    assert [state for _text, state, _reason in states_of(
        text, materials={"K1": stored(EXCERPT), "W1": PROVIDER}, standing=standing,
    )] == ["verified", "insufficient"]


def test_a_finding_on_a_sentence_without_a_value_passes_no_mark_on():
    """What a finding disputes is its passage, not the statement around it:
    a condition quoted from a paragraph that also names a date puts that
    date in no dispute, so a paragraph repeating the date keeps its state."""
    text = (
        "Der Zuschuss wurde nicht gestrichen, sondern zum 21.07.2026 geändert. "
        "Bis 20.07.2026 galten die alten Bedingungen. "
        "Laufende Prüfungen werden nach altem Stand beschieden, wenn die Bestätigung vollständig vorlag [W1].\n\n"
        "Seit dem 21.07.2026 gilt das Programm in geänderter Form weiter [W2]."
    )
    standing = [Standing(
        "Laufende Prüfungen werden nach altem Stand beschieden, wenn die Bestätigung vollständig vorlag [W1].",
        "Die Bedingung ist unvollständig wiedergegeben.",
    )]
    assert [(state, reason) for _text, state, reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == [("insufficient", "finding"), ("attributed", "")]


def test_only_the_value_of_the_quoted_passage_carries_the_mark_on():
    """A statement naming two values, of which the finding quotes one: a
    repetition of the quoted value carries the mark, one of the other does not."""
    text = (
        "Der Klimabonus beträgt 16 Prozent. Anträge sind bis zum 30.09.2026 möglich [W1].\n\n"
        "Die Frist endet am 30.09.2026 [W2].\n\n"
        "Der Klimabonus liegt bei 16 Prozent [W2]."
    )
    standing = [Standing("Anträge sind bis zum 30.09.2026 möglich [W1].", "Die Frist ist nicht belegt.")]
    assert [(state, reason) for _text, state, reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == [("insufficient", "finding"), ("insufficient", "restates"), ("attributed", "")]


def test_a_passage_the_text_no_longer_holds_passes_no_mark_on():
    text = "Der Bonus beträgt 16 Prozent [W1].\n\nZusammengefasst: Der Bonus liegt bei 20 Prozent [W2]."
    standing = [Standing("Der Bonus beträgt 20 Prozent [W1].", "Nicht im Kontext.")]
    assert [(state, reason) for _text, state, reason in states_of(
        text, materials={"W1": PROVIDER, "W2": PROVIDER}, standing=standing,
    )] == [("attributed", ""), ("attributed", "")]
