"""One grammar of inline evidence labels: grouped brackets are single ones."""

from __future__ import annotations

import time

import pytest

from inqtrix.citation_labels import (
    AGENT_FAMILIES,
    CITATION_FORMAT,
    DELEGATED_FAMILIES,
    canonicalize_citations,
    citation_segments,
    citation_units,
    cited_labels,
    malformed_citations,
    segment_is_new,
    unescape_citations,
    without_citations,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Marktteilnehmer wäre.[K2, W7]", "Marktteilnehmer wäre.[K2] [W7]"),
        ("Satz [K2; W7].", "Satz [K2] [W7]."),
        ("Satz [K2,W7].", "Satz [K2] [W7]."),
        ("Satz [ K2 , W7 ].", "Satz [K2] [W7]."),
        ("Satz [K2, K2].", "Satz [K2]."),
        ("Zwei Wege sind vorgesehen [K2, W7]: erstens", "Zwei Wege sind vorgesehen [K2] [W7]: erstens"),
    ],
)
def test_a_group_becomes_single_brackets(text, expected):
    assert canonicalize_citations(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Die Zellen [A1, B2] tragen die Summe.",
        "Link [K2](https://example.com) bleibt.",
        "Gruppe als Linktext [K2, W7](https://example.com) bleibt.",
        "Code `[K2, W7]` bleibt.",
        "```\n[K2, W7]\n```\n",
        "Formel $x \\in [K1, K2]$ bleibt.",
        "$$\n[K1, K2]\n$$\n",
        "Angrenzend [K2][W7] bleibt.",
        "Eine Spanne [K3–K5] bleibt.",
        "Ein Marker [unbelegt: K2] bleibt.",
        "Andere Familie [E1, E2] bleibt.",
        "Ziffern jenseits von ASCII [K٢, W7] bleiben.",
        "Ein Trenner jenseits von ASCII [K2, W7] bleibt.",
        "Ohne Klammer.",
    ],
)
def test_nothing_but_a_group_of_labels_in_prose_changes(text):
    assert canonicalize_citations(text) == text


def test_the_families_decide_which_labels_group():
    text = "Aus dem Kind [E2, E6]."
    assert canonicalize_citations(text, DELEGATED_FAMILIES) == "Aus dem Kind [E2] [E6]."
    assert canonicalize_citations(text, AGENT_FAMILIES) == text


def test_a_fence_closes_only_on_its_own_marker():
    text = "````\n```\n[K1, K2]\n````\n[K1, K2]\n"
    assert canonicalize_citations(text) == "````\n```\n[K1, K2]\n````\n[K1] [K2]\n"


def test_canonicalizing_twice_changes_nothing():
    once = canonicalize_citations("A [K2, W7]. B [K1; K3]. `[K9, K8]` $[K1, K2]$")
    assert canonicalize_citations(once) == once


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Satz \\[K1\\].", "Satz [K1]."),
        ("Satz \\[K2, W7\\].", "Satz [K2, W7]."),
        ("Angrenzend \\[K1\\]\\[W2\\] bleibt.", "Angrenzend [K1] [W2] bleibt."),
        ("Quelle \\[K1\\]: siehe oben", "Quelle [K1]: siehe oben"),
        ("| a \\[K1\\] | \\[W7\\]: y |", "| a [K1] | [W7]: y |"),
        ("## Titel \\[K1\\]", "## Titel [K1]"),
        ("- Punkt **fett \\[K1\\]**", "- Punkt **fett [K1]**"),
        ("> Zitat \\[W7\\]", "> Zitat [W7]"),
        ("```\n\\[K1\\]\n```\nnach \\[K1\\]", "```\n\\[K1\\]\n```\nnach [K1]"),
        ("Siehe [Quelle](https://x.org)\\[K1\\] hier.", "Siehe [Quelle](https://x.org)[K1] hier."),
        ("Das ist falsch!\\[K1\\] Weiter.", "Das ist falsch![K1] Weiter."),
    ],
)
def test_an_escaped_citation_bracket_is_written_raw(text, expected):
    assert unescape_citations(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Studie \\[K1\\](2020).",
        "\\[K1\\]: foo",
        "> \\[K1\\]: foo",
        "- \\[K1\\]: foo",
        "1. \\[K2, W7\\]: foo",
        "- [ ] \\[K1\\]: Aufgabe",
        "Zeile\n  \\[K1\\]: foo",
        "```\n\\[K1\\]\n```\n",
        "Code `\\[K1\\]` bleibt.",
        "Formel $x_1 = \\[K1\\]$ bleibt.",
        "$$\n\\[K1\\]\n$$\n",
        "Quelle https://example.org/\\[K1\\] bleibt.",
        "Pfad \\\\[K1\\] bleibt.",
        "Ein Platzhalter \\[Marke\\], \\[K3–K5\\], \\[k1\\] und \\[unbelegt: K2\\].",
        "Andere Familie \\[E1\\].",
        "Mehr [siehe \\[K1\\]](https://example.org) dazu.",
        "[\\[W2\\]](https://x.org)",
        "![Bild \\[K1\\]](a.png)",
    ],
)
def test_an_escaped_bracket_stays_where_a_raw_one_would_change_the_text(text):
    """Before ``(`` a raw bracket opens a link; at the start of a line, after
    container markers only and before ``:``, a link reference definition.
    Code, formulas, URLs, a bracket whose backslash is escaped itself and
    brackets that are no citation of the families keep their bytes. So does
    the text of a link or an image, where the editor keeps labels escaped:
    raw, a reader would link the label inside the link."""
    assert unescape_citations(text) == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Preis 5 $ laut \\[K1\\] und 7 $ sonst.", "Preis 5 $ laut [K1] und 7 $ sonst."),
        ("Kosten $5 laut \\[K1\\] und $x$ gilt.", "Kosten $5 laut [K1] und $x$ gilt."),
        (
            "Preis 5$$ und \\[K1\\] belegt.\n\nWeiter \\[W2\\] hier.",
            "Preis 5$$ und [K1] belegt.\n\nWeiter [W2] hier.",
        ),
    ],
)
def test_dollar_signs_hide_an_escaped_label_only_where_they_hide_a_raw_one(text, expected):
    """Unescaping reads prose as the grammar does: the backslashes of an
    escape are no formula sign, and a ``$$`` that never closes is no formula.
    The raw result is what the readers count."""
    assert cited_labels(text) == []
    assert unescape_citations(text) == expected
    assert cited_labels(expected) == [label for label in ("K1", "W2") if label in expected]


def test_unescaping_stays_linear_in_the_text():
    """A long document with many labels stays fast. A label's line start is
    looked up only before a ``:`` and never further back than its line:
    looking it up for every label, or back to the start of the text, made
    unescaping quadratic."""
    text = (
        "```\n" + "Zeile ohne Beleg.\n" * 100_000 + "```\n"
        + " ".join(f"Satz \\[K{index}\\]." for index in range(40_000))
        + "\n"
        + "\n".join(f"\\[K{index}\\]: Eintrag" for index in range(10_000))
    )
    started = time.perf_counter()
    unescaped = unescape_citations(text)
    elapsed = time.perf_counter() - started
    assert "Satz [K39999].\n\\[K0\\]: Eintrag" in unescaped
    # Linear: well under half a second here; either quadratic lookup takes
    # several seconds.
    assert elapsed < 2.0


def test_the_families_decide_which_escaped_labels_are_written_raw():
    text = "Aus dem Kind \\[E2\\] und \\[K1\\]."
    assert unescape_citations(text) == "Aus dem Kind \\[E2\\] und [K1]."
    assert unescape_citations(text, DELEGATED_FAMILIES) == "Aus dem Kind [E2] und [K1]."


def test_unescaping_twice_changes_nothing():
    once = unescape_citations(
        "A \\[K1\\]\\[W2\\].\n\\[K1\\]: x\nB \\[K3\\](y) `\\[K4\\]` \\[W5, K6\\]"
    )
    assert once == "A [K1] [W2].\n\\[K1\\]: x\nB \\[K3\\](y) `\\[K4\\]` [W5, K6]"
    assert unescape_citations(once) == once


def test_a_citation_counts_bare_grouped_and_as_link_text():
    assert cited_labels("A.[K2, W7] B [K1] C [W7]") == ["K2", "W7", "K1"]
    assert cited_labels("A [K2][W7]") == ["K2", "W7"]
    assert cited_labels("Link [W9](https://x) und Definition [K4]: text") == ["W9", "K4"]
    assert cited_labels("Code `[K3]` und Formel $x \\in [K5]$") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Spanne [K3–K5].", ["[K3–K5]"]),
        ("Schräg [K2/W7].", ["[K2/W7]"]),
        ("Verbunden [K2 und W7].", ["[K2 und W7]"]),
        ("Gemischt [K2, siehe oben].", ["[K2, siehe oben]"]),
        ("Gruppe als Linktext [K2, W7](https://x).", ["[K2, W7]"]),
        ("Erst [K3–K5], dann [K2 und W7].", ["[K3–K5]", "[K2 und W7]"]),
        ("Gruppe [K2, W7] und einzeln [K1].", []),
        ("Marker [unbelegt: K2], [unbelegt: K3–K5], [E9: nicht zugeordnet].", []),
        ("Zellen [A1, B2].", []),
        ("Linktext mit Wort [Quelle K2](https://x).", []),
        ("Code `[K3–K5]`.", []),
    ],
)
def test_what_no_reader_can_parse_is_named(text, expected):
    assert malformed_citations(text) == expected


def test_the_format_sentence_names_every_rejected_form():
    for form in ("[K1] [W2]", "[K1, W2]", "[K1; W2]", "[K1][W2]", "[K1–K3]"):
        assert form in CITATION_FORMAT


def _parity_cases() -> list[dict]:
    import json
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "citation_label_parity.json"
    return json.loads(fixture.read_text(encoding="utf-8"))["cases"]


def test_the_parity_fixture_covers_a_meaningful_case_count():
    assert len(_parity_cases()) >= 25


@pytest.mark.parametrize("case", _parity_cases(), ids=lambda case: case["name"])
def test_the_grammar_reproduces_the_shared_parity_fixture(case):
    assert canonicalize_citations(case["text"], case["families"]) == case["canonical"]
    assert malformed_citations(case["text"], case["families"]) == case["malformed"]


# -- how a revision binds labels to sentences --------------------------------- #


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("A gilt. [K12] Ergänzend gilt E.",
                     [("A gilt. [K12]", ("K12",)), ("Ergänzend gilt E.", ())], id="house-style"),
        pytest.param("wäre.[K2] [W7] Weiter.",
                     [("wäre.[K2] [W7]", ("K2", "W7")), ("Weiter.", ())], id="adjacent-to-the-period"),
        pytest.param("verzögert. [K4][K9][K10]",
                     [("verzögert. [K4][K9][K10]", ("K4", "K9", "K10"))], id="a-run-at-the-end"),
        pytest.param("Im Jahr 2025. [K3] Danach gilt X.",
                     [("Im Jahr 2025. [K3]", ("K3",)), ("Danach gilt X.", ())], id="after-a-number"),
    ],
)
def test_a_citation_run_after_the_period_closes_the_sentence_on_its_left(text, expected):
    """The house style puts the label after the period; it still belongs to
    the sentence it follows, never to the next one."""
    assert [(s.text, s.labels) for s in citation_segments(text)] == expected


@pytest.mark.parametrize(
    ("text", "count"),
    [
        pytest.param("Nach § 5 Abs. 2 gilt die Frist [K3]. Z. B. gilt Nr. 4 [K4].", 2,
                     id="abbreviations-and-single-letters"),
        pytest.param("Am 3. Mai gilt X.", 1, id="an-ordinal"),
    ],
)
def test_abbreviations_single_letters_and_ordinals_do_not_end_a_sentence(text, count):
    assert len(citation_segments(text)) == count


def test_headings_list_lines_and_code_are_their_own_units():
    """A run of fenced lines is ONE code segment, read by the grammar's own
    Markdown reading."""
    text = "## Neu\n\n- Alpha gilt. Beta gilt [K1].\n- Gamma.\n\n```\nx. Y\n```"
    assert [s.kind for s in citation_segments(text)] == [
        "heading", "sentence", "sentence", "sentence", "code",
    ]


def test_the_units_keep_each_paragraph_list_item_and_table_row_apart():
    """The sentences of one paragraph, list item or table row form one unit,
    and the segments are those units read as one list."""
    text = (
        "## Neu\n\n"
        "Alpha gilt. Beta gilt [K1].\nGamma folgt.\n\n"
        "- Delta gilt [K2].\n- Epsilon.\n\n"
        "| A | B |\n|---|---|\n| Zeta | 30 % [K3] |\n\n"
        "```\nx. Y\n```"
    )
    units = citation_units(text)
    assert [(unit.kind, [segment.text for segment in unit.segments]) for unit in units] == [
        ("heading", ["## Neu"]),
        ("prose", ["Alpha gilt.", "Beta gilt [K1].", "Gamma folgt."]),
        ("prose", ["- Delta gilt [K2]."]),
        ("prose", ["- Epsilon."]),
        ("prose", ["| A | B |"]),
        ("prose", ["|---|---|"]),
        ("prose", ["| Zeta | 30 % [K3] |"]),
        ("code", ["```\nx. Y\n```"]),
    ]
    assert [segment for unit in units for segment in unit.segments] == citation_segments(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("Die Frist gilt [K3].", "Die Frist gilt .", id="bare"),
        pytest.param("Die Frist gilt [K3, W7].", "Die Frist gilt .", id="grouped"),
        pytest.param("Die Frist gilt [W7](https://example.org/a?b=1).", "Die Frist gilt .",
                     id="link-text-with-its-destination"),
        pytest.param("Siehe `[K3]` und $x = [K3]$.", "Siehe `[K3]` und $x = [K3]$.",
                     id="code-and-formula"),
        pytest.param("Wörtlich \\[K3\\] bleibt.", "Wörtlich \\[K3\\] bleibt.", id="escaped"),
        pytest.param("Ein [Link](https://example.org) bleibt [W1].",
                     "Ein [Link](https://example.org) bleibt .", id="an-ordinary-link"),
    ],
)
def test_without_citations_leaves_everything_but_the_citations(text, expected):
    assert " ".join(without_citations(text).split()) == expected


QUOTE = "Die Frist beträgt 30 Tage, wie das Gesetz sagt [K3]. Danach gilt **X** [K4]."


@pytest.mark.parametrize(
    ("quote", "text", "new"),
    [
        pytest.param(QUOTE, "Die Frist beträgt 30 Tage [K3].", False, id="truncated"),
        pytest.param(QUOTE, "Danach gilt X [K4].", False, id="emphasis-removed"),
        pytest.param(QUOTE, "Danach gilt **X** [K4]. Die Frist beträgt 30 Tage, wie das "
                     "Gesetz sagt [K3].", False, id="reordered"),
        pytest.param(QUOTE, "Die Frist beträgt nur ausnahmsweise 30 Tage [K3].", True,
                     id="hedged"),
        pytest.param("Die Rechtsgrundlage gilt im Jahr 2025 [K3].", "Recht.", True,
                     id="part-of-a-longer-word"),
        pytest.param("Die Rechtsgrundlage gilt im Jahr 2025 [K3].", "Ja.", True,
                     id="the-head-of-a-word"),
        pytest.param("Die Frist läuft ab Kenntnis; erfasst ist auch der Verdacht. Die "
                     "Fristen gelten nur ausnahmsweise nicht [K15].",
                     "Die Frist läuft ab Kenntnis; erfasst ist auch der Verdacht; die "
                     "Fristen gelten nur ausnahmsweise nicht [K15].", False,
                     id="two-sentences-joined"),
    ],
)
def test_what_counts_as_a_new_segment(quote, text, new):
    assert any(segment_is_new(segment, quote) for segment in citation_segments(text)) is new
