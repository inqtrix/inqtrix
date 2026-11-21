"""The text a web source carries: the line of the provider answer that cites it.

The answers below are written the way the search provider writes them: a
Markdown link in parentheses follows its statement on the same line, and
the provider's annotation marks exactly that link.
"""

from __future__ import annotations

import re

import pytest

from inqtrix.evidence import attach_web_search_lineage, build_web_search_ledger

EURLEX = "https://eur-lex.europa.eu/eli/reg/2023/1542/oj"
COMMISSION = "https://environment.ec.europa.eu/news/battery-passport_en"
VDI = "https://www.vdi.example/faktencheck"
ENERGY = "https://www.energiewechsel.example/waermepumpe"
REPORT = "https://www.sap.example/investors/q1-2025.pdf"
STATISTICS = "https://www.destatis.example/pressemitteilung"

PASSPORT = "Der Batteriepass ist ab dem 18. Februar 2027 vorgeschrieben."
UNCHANGED = "Eine spätere Verordnung hat den Stichtag nicht verschoben."
PROSE = (
    f"{PASSPORT} ([eur-lex.europa.eu]({EURLEX}))\n\n"
    f"{UNCHANGED} ([environment.ec.europa.eu]({COMMISSION}))"
)
"""Two paragraphs, each one statement and the link after its full stop."""

COMPARISON = (
    "| Kriterium | Luft-Wasser | Sole-Wasser |\n"
    "|---|---|---|\n"
    f"| **Effizienz** | Schwankt mit der Außentemperatur. ([vdi.example]({VDI})) "
    f"| Meist effizienter, weil das Erdreich konstant bleibt. ([energiewechsel.example]({ENERGY})) |"
)
"""A table row whose cells cite two different sources."""


def _places(answer: str, url: str) -> list[tuple[int, int]]:
    """Where *answer* cites *url*: each link with the parentheses around it."""
    marker = r"\(\[[^\]]+\]\(" + re.escape(url) + r"\)\)"
    return [(match.start(), match.end()) for match in re.finditer(marker, answer)]


def _citations(answer: str, *cited: tuple[str, int | None, int | None], snippet: str = "") -> list[dict]:
    """The ledger's citations of one search over *answer*.

    Each *cited* entry is an address with the span the provider reports for
    it, or with ``None`` twice where the provider reports none.
    """
    ledger = build_web_search_ledger(
        run_id="run_support",
        query_records=[{"query_id": "query_support", "query": "Batteriepass", "status": "completed"}],
        query_synthesis={"query_support": {"query": "Batteriepass", "provider_answer": answer}},
        citation_records=[
            {
                "annotation_start": start,
                "annotation_end": end,
                "canonical_url": url,
                "citation_id": f"citation_{index}",
                "query_id": "query_support",
                "snippet": snippet,
                "source_id": f"source_{index}",
            }
            for index, (url, start, end) in enumerate(cited, start=1)
        ],
    )
    return ledger["searches"]["query_support"]["citations"]


def _marked(answer: str, *urls: str) -> list[dict]:
    """The citations of *urls*, each annotated at every place *answer* cites it."""
    return _citations(answer, *[(url, *place) for url in urls for place in _places(answer, url)])


def _supports(citations: list[dict]) -> list[tuple[str | None, str]]:
    return [(item.get("grounded_support"), item["mapping_status"]) for item in citations]


def test_a_statement_with_its_link_after_the_full_stop_is_the_text_of_its_source() -> None:
    assert _supports(_marked(PROSE, EURLEX, COMMISSION)) == [
        (PASSPORT, "provider_citation_marker"),
        (UNCHANGED, "provider_citation_marker"),
    ]


def test_a_link_before_the_full_stop_leaves_the_sentence_whole() -> None:
    answer = f"Der Markt wächst auf 1,5 Bio. US-Dollar ([gartner.example]({REPORT}))."
    assert _supports(_marked(answer, REPORT)) == [
        ("Der Markt wächst auf 1,5 Bio. US-Dollar.", "provider_citation_marker"),
    ]


def test_a_parenthesis_of_the_statement_stays_when_the_link_leaves() -> None:
    answer = f"Die Pflicht gilt für Industriebatterien (industrial batteries) ([eur-lex.europa.eu]({EURLEX}))"
    assert _supports(_marked(answer, EURLEX)) == [
        ("Die Pflicht gilt für Industriebatterien (industrial batteries)", "provider_citation_marker"),
    ]


def test_a_table_row_with_its_link_in_a_cell_of_its_own_is_the_row() -> None:
    answer = (
        "| Quartal | Umsatz | Quelle |\n"
        "|---|---:|---|\n"
        f"| Q1 2025 | 9.013 Mio. € | ([sap.example]({REPORT})) |\n"
        "| Q2 2025 | 9.027 Mio. € | – |"
    )
    assert _supports(_marked(answer, REPORT)) == [
        ("Q1 2025 | 9.013 Mio. €", "provider_citation_marker"),
    ]


def test_each_source_of_a_table_row_takes_its_own_cell_behind_the_row_label() -> None:
    assert _supports(_marked(COMPARISON, VDI, ENERGY)) == [
        ("**Effizienz** | Schwankt mit der Außentemperatur.", "provider_citation_marker"),
        ("**Effizienz** | Meist effizienter, weil das Erdreich konstant bleibt.", "provider_citation_marker"),
    ]


def test_each_source_of_a_line_takes_the_sentence_before_its_link() -> None:
    answer = (
        f"Die Quote stieg auf 12 % ([vdi.example]({VDI})). "
        f"Der Bestand sank auf 3,1 Mio. ([energiewechsel.example]({ENERGY}))."
    )
    assert _supports(_marked(answer, VDI, ENERGY)) == [
        ("Die Quote stieg auf 12 %", "provider_citation_marker"),
        ("Der Bestand sank auf 3,1 Mio.", "provider_citation_marker"),
    ]


def test_links_with_no_text_between_them_share_the_statement_before_them() -> None:
    answer = (
        f"Die Quote stieg auf 12 %. ([vdi.example]({VDI})) "
        f"Der Bestand sank. ([energiewechsel.example]({ENERGY})) ([destatis.example]({STATISTICS}))"
    )
    assert _supports(_marked(answer, VDI, ENERGY, STATISTICS)) == [
        ("Die Quote stieg auf 12 %.", "provider_citation_marker"),
        ("Der Bestand sank.", "provider_citation_marker"),
        ("Der Bestand sank.", "provider_citation_marker"),
    ]


def test_links_inside_one_pair_of_parentheses_leave_the_statement_together() -> None:
    answer = f"Die Quote stieg auf 12 %. ([vdi.example]({VDI}); [energiewechsel.example]({ENERGY}))"
    start = answer.index("([")
    assert _supports(_citations(answer, (VDI, start, len(answer)), (ENERGY, start, len(answer)))) == [
        ("Die Quote stieg auf 12 %.", "provider_citation_marker"),
        ("Die Quote stieg auf 12 %.", "provider_citation_marker"),
    ]


def test_a_numbered_statement_inside_a_line_keeps_its_number() -> None:
    """Only the opening of a line is a list marker; ``2. Quartal`` after an
    earlier link is part of the statement."""
    answer = (
        f"Im 1. Quartal: 9.013 Mio. € ([sap.example]({REPORT})) "
        f"2. Quartal: 9.027 Mio. € ([destatis.example]({STATISTICS}))"
    )
    assert _supports(_marked(answer, REPORT, STATISTICS)) == [
        ("Im 1. Quartal: 9.013 Mio. €", "provider_citation_marker"),
        ("2. Quartal: 9.027 Mio. €", "provider_citation_marker"),
    ]


def test_a_minus_before_a_figure_stays() -> None:
    """A dash falls away where it stands alone; before a figure it is the
    sign of that figure, at the start of a row, of a cell and of a line."""
    row = (
        f"| -0,5 | Prozentpunkte | September bis Oktober ([destatis.example]({STATISTICS})) "
        f"| -20 °C ([vdi.example]({VDI})) |"
    )
    line = f"-5 °C ist die untere Grenze. ([energiewechsel.example]({ENERGY}))"
    assert _supports(_marked(f"{row}\n\n{line}", STATISTICS, VDI, ENERGY)) == [
        ("-0,5 | Prozentpunkte | September bis Oktober", "provider_citation_marker"),
        ("-0,5 | -20 °C", "provider_citation_marker"),
        ("-5 °C ist die untere Grenze.", "provider_citation_marker"),
    ]


def test_a_dash_that_stands_alone_at_an_end_falls_away() -> None:
    answer = f"– Die Quote lag bei 3,1 % – ([destatis.example]({STATISTICS}))"
    assert _supports(_marked(answer, STATISTICS)) == [
        ("Die Quote lag bei 3,1 %", "provider_citation_marker"),
    ]


def test_a_short_cell_of_a_table_row_is_a_statement_of_its_own() -> None:
    answer = f"| 1. Quartal | hoch ([vdi.example]({VDI})) | niedrig ([energiewechsel.example]({ENERGY})) |"
    assert _supports(_marked(answer, VDI, ENERGY)) == [
        ("1. Quartal | hoch", "provider_citation_marker"),
        ("1. Quartal | niedrig", "provider_citation_marker"),
    ]


def test_two_links_in_one_cell_share_the_cell() -> None:
    answer = f"| Effizienz | Meist höher ([vdi.example]({VDI})) und ([energiewechsel.example]({ENERGY})) |"
    assert _supports(_marked(answer, VDI, ENERGY)) == [
        ("Effizienz | Meist höher", "provider_citation_marker"),
        ("Effizienz | Meist höher und", "provider_citation_marker"),
    ]


@pytest.mark.parametrize(
    ("between", "second"),
    [
        ("und", "Die Quote stieg auf 12 % und"),
        ("und 14 %", "Die Quote stieg auf 12 % und 14 %"),
        ("der Bestand sank", "der Bestand sank"),
    ],
)
def test_fewer_than_three_words_between_two_links_are_no_statement_of_their_own(between: str, second: str) -> None:
    """A link behind a connective shares the statement before the first
    link and adds its own few words; three words are a statement."""
    answer = f"Die Quote stieg auf 12 % ([vdi.example]({VDI})) {between} ([energiewechsel.example]({ENERGY}))."
    assert _supports(_marked(answer, VDI, ENERGY)) == [
        ("Die Quote stieg auf 12 %", "provider_citation_marker"),
        (second, "provider_citation_marker"),
    ]


def test_a_link_inside_a_sentence_keeps_its_label_and_splits_nothing() -> None:
    answer = (
        "Das Formular steht [hier](https://formulare.example/antrag) bereit und abrufbar. "
        f"([vdi.example]({VDI}))"
    )
    assert _supports(_marked(answer, VDI)) == [
        ("Das Formular steht hier bereit und abrufbar.", "provider_citation_marker"),
    ]


def test_a_link_inside_a_sentence_keeps_the_blank_before_its_label() -> None:
    """Only what is cut out takes the blank before it along; a label stays
    a word of its sentence, also right before the full stop."""
    answer = f"Das Formular steht [hier](https://formulare.example/antrag). ([vdi.example]({VDI}))"
    assert _supports(_marked(answer, VDI)) == [
        ("Das Formular steht hier.", "provider_citation_marker"),
    ]


def test_a_span_on_a_link_with_another_spelling_takes_its_part_of_a_line_with_several_links() -> None:
    answer = (
        f"{PASSPORT} ([eur-lex.europa.eu](https://kurz.example/b7)) "
        f"{UNCHANGED} ([environment.ec.europa.eu]({COMMISSION}))"
    )
    start = answer.index("([")
    assert _supports(_citations(answer, (EURLEX, start, answer.index("))") + 2))) == [
        (PASSPORT, "provider_citation_marker"),
    ]


@pytest.mark.parametrize(
    "opening",
    ["## ", "> ", "3) ", "12. ", "* ", "- ", "+ "],
)
def test_the_opening_of_a_line_is_no_part_of_its_statement(opening: str) -> None:
    answer = f"{opening}Die Quote lag bei 3,1 %. ([destatis.example]({STATISTICS}))"
    assert _supports(_marked(answer, STATISTICS)) == [
        ("Die Quote lag bei 3,1 %.", "provider_citation_marker"),
    ]


def test_a_dash_attached_to_a_word_stays() -> None:
    answer = f"Betrifft die Bereiche Luft- ([vdi.example]({VDI}))"
    assert _supports(_marked(answer, VDI)) == [
        ("Betrifft die Bereiche Luft-", "provider_citation_marker"),
    ]


def test_a_long_text_is_cut_at_every_place_it_arises() -> None:
    """The part of a row behind another link and the line of a mark that is
    no link end at the bound like the line of a single link: at a word,
    with the mark of the cut."""
    long_cell = "Wort " * 130
    row = f"| Wert | kurz ([vdi.example]({VDI})) | {long_cell}([energiewechsel.example]({ENERGY})) |"
    marked_line = f"{long_cell}[1]."
    answer = f"{row}\n\n{marked_line}"
    start = answer.index("[1]")
    supports = _supports(_citations(
        answer, (ENERGY, *_places(answer, ENERGY)[0]), (STATISTICS, start, start + 3),
    ))
    assert [(len(text) <= 600, text.endswith("Wort…"), status) for text, status in supports] == [
        (True, True, "provider_citation_marker"),
        (True, True, "provider_citation_marker"),
    ]
    assert supports[0][0].startswith("Wert | Wort Wort")


def test_a_source_linked_inside_a_sentence_takes_that_sentence() -> None:
    answer = f"Laut [Destatis]({STATISTICS}) stieg die Quote auf 5 % ([vdi.example]({VDI}))."
    assert _supports(_citations(answer, (STATISTICS, None, None), (VDI, None, None))) == [
        ("Laut Destatis stieg die Quote auf 5 %.", "provider_citation_marker"),
        ("Laut Destatis stieg die Quote auf 5 %.", "provider_citation_marker"),
    ]


def test_a_source_linked_inside_a_sentence_takes_the_part_between_the_links_around_it() -> None:
    answer = (
        f"Der Bestand sank deutlich ([vdi.example]({VDI})). Laut [Destatis]({STATISTICS}) stieg die Quote "
        f"([energiewechsel.example]({ENERGY})). Mehr ist nicht bekannt."
    )
    assert _supports(_citations(answer, (STATISTICS, None, None))) == [
        ("Laut Destatis stieg die Quote", "provider_citation_marker"),
    ]


def test_a_cited_bare_address_leaves_the_line_without_itself() -> None:
    answer = f"Die Quote lag bei 3,1 %. Quelle: {STATISTICS}"
    assert _supports(_citations(answer, (STATISTICS, None, None))) == [
        ("Die Quote lag bei 3,1 %. Quelle:", "provider_citation_marker"),
    ]


def test_a_link_with_a_tracking_parameter_names_the_address_of_its_citation() -> None:
    """The address of a citation has been normalized more often than the
    address in the answer; one more pass still shortens the latter."""
    answer = "Die Quote lag bei 3,1 %. ([a.example](https://a.example/p/?utm_source=x))"
    assert _supports(_citations(answer, ("https://a.example/p", None, None))) == [
        ("Die Quote lag bei 3,1 %.", "provider_citation_marker"),
    ]


@pytest.mark.parametrize(
    ("snippet", "status"),
    [("", "source_only"), ("Pressemitteilung Nr. 199", "provider_snippet")],
)
def test_a_link_alone_in_its_line_gives_its_source_no_text(snippet: str, status: str) -> None:
    """A link under a table names the source of the table, not a statement:
    neither the domain label nor the link itself is a text."""
    answer = f"| Jahr | Wert |\n|---|---|\n| 2025 | 3,1 % |\n\n([destatis.example]({STATISTICS}))"
    (citation,) = _citations(answer, (STATISTICS, *_places(answer, STATISTICS)[0]), snippet=snippet)
    assert ("grounded_support" in citation, citation["mapping_status"]) == (False, status)


@pytest.mark.parametrize("shift", [-3, 1, 2, 7])
def test_a_span_that_slipped_off_its_link_still_gives_the_line_of_that_link(shift: int) -> None:
    """Offsets counted against a text that lost characters in front: the
    span covers a piece of the link and what follows it."""
    start, end = _places(PROSE, EURLEX)[0]
    assert _supports(_citations(PROSE, (EURLEX, start + shift, end + shift))) == [
        (PASSPORT, "provider_citation_marker"),
    ]


def test_a_span_far_from_every_link_of_its_source_gives_the_line_of_the_first_such_link() -> None:
    """One span of the answer lies on its link, so the provider marks links:
    the other span, on some sentence, has slipped, and the link of its
    source decides, never the window the span happens to cover."""
    assert _supports(_citations(PROSE, (EURLEX, *_places(PROSE, EURLEX)[0]), (COMMISSION, 0, 31))) == [
        (PASSPORT, "provider_citation_marker"),
        (UNCHANGED, "provider_citation_marker"),
    ]


def test_a_span_on_a_statement_stays_the_text_beside_a_list_of_links() -> None:
    """No span of the answer lies on a link, so the provider annotates
    statements: the list of sources below does not replace the statement."""
    statement = "Global input costs 5 USD."
    answer = f"{statement}\n\n- [Prices]({REPORT})"
    assert _supports(_citations(answer, (REPORT, 0, len(statement)))) == [
        (statement, "provider_answer_context"),
    ]


def test_a_span_on_a_link_that_stands_alone_gives_no_text_though_the_source_has_a_statement_elsewhere() -> None:
    """Each citation stands for its own place; the reference of the source
    passes the one without a text (see the hand-over tests below)."""
    answer = f"([destatis.example]({STATISTICS}))\n\nDie Quote lag bei 3,1 %. ([destatis.example]({STATISTICS}))"
    assert _supports(_marked(answer, STATISTICS)) == [
        (None, "source_only"),
        ("Die Quote lag bei 3,1 %.", "provider_citation_marker"),
    ]


def test_a_source_without_a_span_takes_the_line_of_its_link() -> None:
    assert _supports(_citations(PROSE, (EURLEX, None, None))) == [
        (PASSPORT, "provider_citation_marker"),
    ]


def test_a_source_the_answer_links_nowhere_gets_no_text() -> None:
    """A text is never made up for a source: the answer has to cite it."""
    assert _supports(_citations(PROSE, (STATISTICS, None, None))) == [(None, "source_only")]


def test_an_address_that_only_begins_like_the_cited_one_is_another_source() -> None:
    answer = "Gartner nennt einen Wert ([Lang](https://example.com/report-extended))."
    assert _supports(_citations(
        answer,
        ("https://example.com/report", None, None),
        ("https://example.com/report-extended", None, None),
    )) == [
        (None, "source_only"),
        ("Gartner nennt einen Wert.", "provider_citation_marker"),
    ]


def test_a_source_without_a_span_passes_a_link_that_stands_alone() -> None:
    answer = f"([destatis.example]({STATISTICS}))\n\nDie Quote lag bei 3,1 %. ([destatis.example]({STATISTICS}))"
    assert _supports(_citations(answer, (STATISTICS, None, None))) == [
        ("Die Quote lag bei 3,1 %.", "provider_citation_marker"),
    ]


def test_a_bare_address_in_parentheses_is_a_link_like_any_other() -> None:
    answer = f"Die Quote lag bei 3,1 %. ({STATISTICS})"
    assert _supports(_citations(answer, (STATISTICS, None, None))) == [
        ("Die Quote lag bei 3,1 %.", "provider_citation_marker"),
    ]


def test_an_address_the_search_does_not_cite_stays_in_the_statement() -> None:
    answer = f"Das Formular steht unter https://formulare.example/antrag bereit. ([vdi.example]({VDI}))"
    assert _supports(_marked(answer, VDI)) == [
        ("Das Formular steht unter https://formulare.example/antrag bereit.", "provider_citation_marker"),
    ]


def test_an_address_with_a_parenthesis_is_read_whole() -> None:
    address = "https://de.wikipedia.example/wiki/Merkur_(Planet)"
    answer = f"Merkur ist der innerste Planet. ([de.wikipedia.example]({address}))"
    start = answer.index("([")
    assert _supports(_citations(answer, (address, start, len(answer)))) == [
        ("Merkur ist der innerste Planet.", "provider_citation_marker"),
    ]


def test_a_span_on_a_link_keeps_its_place_when_the_link_spells_the_address_differently() -> None:
    """The provider reports the address a short link resolves to: no link of
    the answer names it, so the provider's own place stands."""
    answer = f"{PASSPORT} ([eur-lex.europa.eu](https://kurz.example/b7))\n\n{UNCHANGED}"
    start = answer.index("([")
    assert _supports(_citations(answer, (EURLEX, start, answer.index("\n")))) == [
        (PASSPORT, "provider_citation_marker"),
    ]


@pytest.mark.parametrize("shift", [-2, 2])
def test_a_slipped_span_on_a_link_with_another_spelling_still_gives_the_line_of_that_link(shift: int) -> None:
    answer = f"{PASSPORT} ([eur-lex.europa.eu](https://kurz.example/b7)) Stand: 2026.\n\n{UNCHANGED}"
    start, end = answer.index("(["), answer.index("))") + 2
    assert _supports(_citations(answer, (EURLEX, start + shift, end + shift))) == [
        (f"{PASSPORT} Stand: 2026.", "provider_citation_marker"),
    ]


def test_a_slipped_span_on_the_second_link_of_a_source_gives_the_line_of_that_link() -> None:
    answer = (
        f"{PASSPORT} ([eur-lex.europa.eu]({EURLEX}))\n\n"
        f"{UNCHANGED} ([eur-lex.europa.eu]({EURLEX}))\n\nStand: 2026."
    )
    start, end = _places(answer, EURLEX)[1]
    assert _supports(_citations(answer, (EURLEX, start + 2, end + 2))) == [
        (UNCHANGED, "provider_citation_marker"),
    ]


@pytest.mark.parametrize(
    ("answer", "marked"),
    [
        ("Die Quote lag bei 3,1 % [1]. Der Bestand sank.", "[1]"),
        ("Die Hauptstadt ist Paris.", "Paris"),
    ],
)
def test_a_span_on_a_mark_or_a_word_gives_its_whole_line(answer: str, marked: str) -> None:
    """A span that holds no statement and lies on no link: the provider's
    place stands, and nothing of the line is cut out."""
    start = answer.index(marked)
    assert _supports(_citations(answer, (STATISTICS, start, start + len(marked)))) == [
        (answer, "provider_citation_marker"),
    ]


def test_a_line_with_nothing_but_a_mark_beside_its_link_gives_no_text() -> None:
    answer = f"Die Quote lag bei 3,1 %.\n\n→ ([destatis.example]({STATISTICS}))"
    (citation,) = _citations(answer, (STATISTICS, *_places(answer, STATISTICS)[0]))
    assert ("grounded_support" in citation, citation["mapping_status"]) == (False, "source_only")


@pytest.mark.parametrize("span", [(len(PROSE) + 5, len(PROSE) + 60), (40, 12), (12, 12)])
def test_a_span_that_lies_nowhere_in_the_answer_gives_no_text(span: tuple[int, int]) -> None:
    assert _supports(_citations(PROSE, (STATISTICS, *span))) == [(None, "source_only")]


def test_a_span_on_a_statement_that_links_another_source_stays_that_statement() -> None:
    """The span holds a whole link and a sentence around it: the provider
    annotated the sentence, and the sentence is the text."""
    sentence = f"Laut [Destatis]({STATISTICS}) stieg die Quote auf 5 %."
    answer = f"{sentence} Weitere Angaben folgen."
    assert _supports(_citations(answer, (VDI, 0, len(sentence)))) == [
        (sentence, "provider_answer_context"),
    ]


def test_a_span_on_a_statement_is_the_text_when_the_answer_links_the_source_nowhere() -> None:
    answer = f"{PASSPORT} {UNCHANGED}"
    assert _supports(_citations(answer, (EURLEX, 0, len(PASSPORT)))) == [
        (PASSPORT, "provider_answer_context"),
    ]


def _ledger(*searches: tuple[str, list[dict]]) -> dict:
    return {
        "searches": {
            query_id: {"query_id": query_id, "source_run_id": "run_support", "citations": citations}
            for query_id, citations in searches
        }
    }


def _cited(url: str, text: str, *, start: int | None, citation_id: str) -> dict:
    return {
        "annotation_start": start,
        "citation_id": citation_id,
        "grounded_support": text,
        "source_id": "source_1",
        "url": url,
    }


def test_a_reference_takes_the_first_citation_that_carries_a_text() -> None:
    ledger = _ledger(("query_a", [
        _cited(STATISTICS, "", start=10, citation_id="citation_alone"),
        _cited(STATISTICS, "Die Quote lag bei 3,1 %.", start=80, citation_id="citation_statement"),
    ]))
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger)
    assert (reference["grounded_support"], reference["citation_ids"]) == (
        "Die Quote lag bei 3,1 %.", ["citation_alone", "citation_statement"],
    )


def test_a_reference_takes_the_citation_that_stands_first_in_the_answer() -> None:
    ledger = _ledger(("query_a", [
        _cited(STATISTICS, "Später im Text.", start=400, citation_id="citation_late"),
        _cited(STATISTICS, "Ohne Position.", start=None, citation_id="citation_unplaced"),
        _cited(STATISTICS, "Früher im Text.", start=40, citation_id="citation_early"),
    ]))
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger)
    assert reference["grounded_support"] == "Früher im Text."


def test_a_reference_without_a_registering_search_reads_the_searches_in_ledger_order() -> None:
    ledger = _ledger(
        ("query_old", [_cited(STATISTICS, "", start=5, citation_id="citation_old")]),
        ("query_new", [_cited(STATISTICS, "Die Quote lag bei 3,1 %.", start=900, citation_id="citation_new")]),
    )
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger)
    assert (reference["grounded_support"], reference["query_ids"]) == (
        "Die Quote lag bei 3,1 %.", ["query_old", "query_new"],
    )


def test_a_reference_reads_the_first_search_with_a_text_whatever_the_positions() -> None:
    ledger = _ledger(
        ("query_first", [_cited(STATISTICS, "Aus der ersten Suche.", start=900, citation_id="citation_first")]),
        ("query_second", [_cited(STATISTICS, "Aus der zweiten Suche.", start=5, citation_id="citation_second")]),
    )
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger)
    assert reference["grounded_support"] == "Aus der ersten Suche."


def test_the_search_that_registers_a_reference_leads_its_pointers_and_gives_its_text() -> None:
    ledger = _ledger(
        ("query_old", [_cited(STATISTICS, "Aussage zu einer anderen Frage.", start=5, citation_id="citation_old")]),
        ("query_new", [_cited(STATISTICS, "Die Quote lag bei 3,1 %.", start=900, citation_id="citation_new")]),
    )
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger, registering=["query_new"])
    assert (
        reference["grounded_support"], reference["query_id"], reference["query_ids"], reference["citation_id"],
    ) == ("Die Quote lag bei 3,1 %.", "query_new", ["query_new", "query_old"], "citation_new")


def test_a_registering_search_without_a_statement_leaves_the_text_of_another_search_alone() -> None:
    ledger = _ledger(
        ("query_old", [_cited(STATISTICS, "Aussage zu einer anderen Frage.", start=5, citation_id="citation_old")]),
        ("query_new", [_cited(STATISTICS, "", start=900, citation_id="citation_new")]),
    )
    (reference,) = attach_web_search_lineage([{"url": STATISTICS}], ledger, registering=["query_new"])
    assert ("grounded_support" in reference, reference["query_ids"]) == (False, ["query_new", "query_old"])


def test_a_reference_with_a_text_of_its_own_keeps_it() -> None:
    ledger = _ledger(("query_a", [_cited(STATISTICS, "Die Quote lag bei 3,1 %.", start=5, citation_id="citation_a")]))
    (reference,) = attach_web_search_lineage(
        [{"url": STATISTICS, "grounded_support": "Gespeicherter Text."}], ledger, registering=["query_a"],
    )
    assert reference["grounded_support"] == "Gespeicherter Text."


def test_a_reference_that_arrives_without_a_text_carries_its_cell_of_a_table_row() -> None:
    """Rule and hand-over together, without a registering search."""
    ledger = {"searches": {"query_support": {
        "query_id": "query_support", "source_run_id": "run_support", "citations": _marked(COMPARISON, VDI, ENERGY),
    }}}
    references = attach_web_search_lineage([{"url": VDI, "cited": True}, {"url": ENERGY, "cited": True}], ledger)
    assert [reference["grounded_support"] for reference in references] == [
        "**Effizienz** | Schwankt mit der Außentemperatur.",
        "**Effizienz** | Meist effizienter, weil das Erdreich konstant bleibt.",
    ]
