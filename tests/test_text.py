"""Tests for inqtrix.text — text utility functions."""

import pytest

from inqtrix.text import (
    is_none_value,
    iter_bounded_chunks,
    iter_word_chunks,
    number_spans,
    shorten_visibly,
    start_at_word,
)


class TestIsNoneValue:

    def test_keine(self):
        assert is_none_value("Keine") is True

    def test_none_word(self):
        assert is_none_value("None") is True

    def test_keine_with_dot(self):
        assert is_none_value("Keine.") is True

    def test_none_with_dot(self):
        assert is_none_value("None.") is True

    def test_empty_string(self):
        assert is_none_value("") is True

    def test_keine_prefix_with_text(self):
        assert is_none_value("Keine relevanten Luecken") is True

    def test_case_insensitive(self):
        assert is_none_value("KEINE") is True
        assert is_none_value("NONE") is True
        assert is_none_value("keine") is True

    def test_whitespace(self):
        assert is_none_value("  Keine  ") is True

    def test_actual_value(self):
        assert is_none_value("Es fehlen Daten zu X") is False

    def test_partial_match_not_triggered(self):
        assert is_none_value("Dieses Ergebnis ist keines") is False


class TestIterWordChunks:

    def test_preserves_spaces_between_words(self):
        assert list(iter_word_chunks("Hallo Welt")) == ["Hallo ", "Welt"]

    def test_preserves_newlines(self):
        assert list(iter_word_chunks("A\n\nB")) == ["A\n\n", "B"]


class TestIterBoundedChunks:
    """A finished answer is published in a few byte-bounded chunks."""

    def test_joining_the_chunks_gives_back_the_text(self):
        text = "Größe und Maß\n\n" + " ".join(f"Wort{i}" for i in range(400))
        assert "".join(iter_bounded_chunks(text, 64)) == text

    def test_chunks_stay_within_the_bound_and_never_split_a_word(self):
        text = " ".join(["äöü"] * 50)  # six bytes per word plus a space
        chunks = list(iter_bounded_chunks(text, 20))
        assert all(len(chunk.encode("utf-8")) <= 20 for chunk in chunks)
        assert all(not chunk.startswith(("ö", "ü")) for chunk in chunks)
        assert len(chunks) == 25

    def test_a_word_longer_than_the_bound_is_a_chunk_of_its_own(self):
        long_word = "x" * 30
        assert list(iter_bounded_chunks(f"a {long_word} b", 10)) == ["a ", f"{long_word} ", "b"]

    def test_empty_text_yields_nothing(self):
        assert list(iter_bounded_chunks("", 10)) == []


class TestShortenVisibly:
    """One shortening rule for every excerpt a reader or a model reads.

    The expected strings are spelled out here, never derived from the
    helper: a cut that moves by one word must be caught.
    """

    def test_a_text_within_the_limit_returns_whole(self):
        text = "Die Frist beträgt 14 Tage."
        assert shorten_visibly(text, len(text), marker=" ...") == text
        assert shorten_visibly(text, 80, marker=" ...") == text

    def test_whitespace_collapses_so_the_text_stays_one_line(self):
        assert shorten_visibly("Die Frist\n[K2] beträgt\t 14 Tage.", 80, marker=" ...") == (
            "Die Frist [K2] beträgt 14 Tage."
        )

    def test_a_long_text_is_cut_at_a_word_boundary_and_marked(self):
        cut = shorten_visibly("alpha beta gamma delta epsilon", 20, marker=" ...")
        assert cut == "alpha beta gamma ..."
        assert len(cut) <= 20

    def test_the_marker_counts_against_the_limit(self):
        marker = " [shortened here; the passage continues]"
        cut = shorten_visibly("Wort " * 100, 440, marker=marker)
        assert cut == ("Wort " * 80).rstrip() + marker
        assert len(cut) <= 440

    def test_a_cut_never_splits_a_space_grouped_number(self):
        """The room ends on the space inside ``1 500 000``: cutting there
        would leave "1 500", a different number."""
        cut = shorten_visibly("Die Summe beträgt 1 500 000 Euro im Jahr", 27, marker=" ...")
        assert cut == "Die Summe beträgt ..."

    def test_a_number_on_the_last_character_of_the_room_is_not_split(self):
        """``5 000`` starts on the last character the room keeps, and the
        room ends on its group space: cutting there would leave "5", a
        different number."""
        cut = shorten_visibly("Wort Wort Wort 5 000 EUR bleiben offen", 20, marker=" ...")
        assert cut == "Wort Wort Wort ..."

    def test_a_year_after_a_number_is_a_word_boundary(self):
        """Only a three-digit group joins a number; ``5 2019`` are two."""
        cut = shorten_visibly("Artikel 5 2019 regelt die Frist genau", 13, marker=" ...")
        assert cut == "Artikel 5 ..."

    def test_a_hard_cut_inside_an_overlong_word_backs_out_of_a_number(self):
        text = "x" * 30 + "1.500.000" + "y" * 30
        assert shorten_visibly(text, 40, marker=" ...") == "x" * 30 + " ..."

    def test_an_overlong_word_is_cut_inside_rather_than_losing_the_room(self):
        """A boundary in the first half of the room would drop most of it;
        the cut then falls inside the overlong word, still marked."""
        cut = shorten_visibly("Siehe " + "u" * 100, 40, marker=" ...")
        assert cut == "Siehe " + "u" * 30 + " ..."

    def test_a_limit_without_room_for_the_marker_is_refused(self):
        with pytest.raises(ValueError, match="no room for the 4-character cut marker"):
            shorten_visibly("abc", 4, marker=" ...")

    def test_a_table_row_is_cut_after_its_last_whole_cell(self):
        """A row of three-digit cells is no number: every space in it stays
        a word boundary, so the passage keeps the cells that fit instead of
        falling back to before the row."""
        row = "Fallzahlen 2023 nach Monat: " + " ".join(
            str(100 + 7 * month) for month in range(60)
        )
        marker = " [shortened here; the passage continues]"
        assert shorten_visibly(row, 240, marker=marker) == (
            "Fallzahlen 2023 nach Monat: 100 107 114 121 128 135 142 149 156 "
            "163 170 177 184 191 198 205 212 219 226 233 240 247 254 261 268 "
            "275 282 289 296 303 310 317 324 331 338 345 352 359 366 373 380 "
            "387 394 [shortened here; the passage continues]"
        )

    def test_a_cut_never_splits_an_amount_with_thin_spaces(self):
        cut = shorten_visibly(
            "Die Summe beträgt 1\u2009500\u2009000 Euro im Jahr", 27, marker=" ..."
        )
        assert cut == "Die Summe beträgt ..."


class TestNumberSpans:
    """What a cut treats as one number, spelled out case by case."""

    @pytest.mark.parametrize(
        ("text", "numbers"),
        [
            ("1 500 000 Euro", ["1 500 000"]),
            ("1\u2009500\u2009000 Euro", ["1\u2009500\u2009000"]),
            ("1\u202f500\u202f000,50 €", ["1\u202f500\u202f000,50"]),
            ("1\u00a0500 Euro", ["1\u00a0500"]),
            ("1.500.000,50 €", ["1.500.000,50"]),
            ("1'500 und 1’500", ["1'500", "1’500"]),
            ("am 26.09.2026 um 12:30", ["26.09.2026", "12:30"]),
            ("2026-09-26 und 2023/24", ["2026-09-26", "2023/24"]),
            ("10–20 Prozent", ["10–20"]),
            ("1 500 000 000 000,50", ["1 500 000 000 000,50"]),
        ],
    )
    def test_one_value_is_one_number(self, text, numbers):
        assert [text[start:end] for start, end in number_spans(text)] == numbers

    @pytest.mark.parametrize(
        ("text", "numbers"),
        [
            # A group after a space has exactly three digits.
            ("Artikel 5 2019", ["5", "2019"]),
            ("1 5000", ["1", "5000"]),
            # A lead group has at most three digits: the year stays apart.
            ("im Jahr 2023 100 000 Menschen", ["2023", "100 000"]),
            # A decimal is no lead group of a following amount.
            ("3,5 100 000", ["3,5", "100 000"]),
            # More than six groups is a table row or a list.
            (
                "Monat: 100 107 114 121 128 135 142",
                ["100", "107", "114", "121", "128", "135", "142"],
            ),
            ("1,2,3,4,5,6,7", ["1", "2", "3", "4", "5", "6", "7"]),
            # A tab or a line break separates table cells.
            ("100\t107\n114", ["100", "107", "114"]),
        ],
    )
    def test_separate_values_stay_apart(self, text, numbers):
        assert [text[start:end] for start, end in number_spans(text)] == numbers


class TestStartAtWord:
    """Where a passage taken from the middle of a text opens."""

    def test_a_start_inside_a_word_moves_to_the_next_word(self):
        text = "alpha beta gamma"
        assert text[start_at_word(text, 7, reach=10):] == "gamma"

    def test_a_start_on_a_word_stays(self):
        text = "alpha beta gamma"
        assert text[start_at_word(text, 6, reach=10):] == "beta gamma"

    def test_a_start_never_opens_inside_a_number(self):
        text = "Summe 1 500 000 EUR offen"
        assert text[start_at_word(text, 8, reach=20):] == "EUR offen"
        thin = "Summe 1\u2009500\u2009000 EUR offen"
        assert thin[start_at_word(thin, 8, reach=20):] == "EUR offen"

    def test_an_overlong_word_is_entered_but_a_number_is_not(self):
        """No boundary within reach: the start stays inside the word but
        moves past the number it would split, and the far boundary beyond
        the reach does not swallow the word."""
        text = "x" * 30 + "1.500.000" + "y" * 30 + " Ende"
        assert text[start_at_word(text, 33, reach=10):] == "y" * 30 + " Ende"

    def test_the_start_of_the_text_stays(self):
        assert start_at_word("alpha beta", 0, reach=5) == 0
