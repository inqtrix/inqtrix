"""Tests for the values a claim states: dates, percentages and amounts."""

import pytest

from inqtrix.strategies._claim_values import claim_values, values_conflict


def _labels(text: str) -> tuple[list[str], list[str]]:
    values = claim_values(text)
    return (
        sorted(value.label() for value in values.values),
        sorted(value.label() for value in values.scope),
    )


def _conflict(first: str, second: str) -> bool:
    return values_conflict(claim_values(first), claim_values(second))


@pytest.mark.parametrize(
    "text",
    [
        "Die Frist endet am 2. August 2026.",
        "Die Frist endet am 2 August 2026.",
        "Die Frist endet am 2. Aug. 2026.",
        "Die Frist endet am 02.08.2026.",
        "Die Frist endet am 2.8.2026.",
        "Die Frist endet am 2026-08-02.",
        "The deadline ends on August 2, 2026.",
    ],
)
def test_every_way_of_writing_a_date_reads_the_same_day(text):
    assert _labels(text) == (["date:2026-08-02"], [])


def test_a_month_or_a_year_alone_reads_coarser():
    assert _labels("Die Regel gilt ab August 2026.") == (["date:2026-08"], [])
    assert _labels("Die Regel gilt ab 2026.") == (["date:2026"], [])
    assert _labels("Die Pflichten gelten zwischen 2025–2027.") == (
        ["date:2025", "date:2027"], [],
    )


def test_a_lower_case_may_is_a_verb_not_a_month():
    assert _labels("The rule may apply from 2026.") == (["date:2026"], [])


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("Die Strafe beträgt 7 Prozent des Umsatzes.", ["percent:7"]),
        ("Die Inflation lag bei 2,1 %.", ["percent:2.1"]),
        ("Die Quote stieg um 3 Prozentpunkte.", ["points:3"]),
        ("Die Strafe beträgt 35 Mio. Euro.", ["amount:35000000 EUR"]),
        ("Die Kosten betragen € 1,5 Mrd.", ["amount:1500000000 EUR"]),
        ("Revenue reached $12.3 billion.", ["amount:12300000000 USD"]),
        ("Die Prämie beträgt 1.500 Euro.", ["amount:1500 EUR"]),
        ("Die Inflation wird bei 2–3 % erwartet.", ["percent:2", "percent:3"]),
    ],
)
def test_percentages_and_amounts_are_normalized(text, labels):
    assert _labels(text) == (labels, [])


def test_bounds_approximations_and_ambiguous_separators_are_not_exact():
    assert _labels("Die Strafe beträgt bis zu 35 Mio. Euro.") == (["amount:35000000 EUR~"], [])
    assert _labels("Rund 30 % der Unternehmen nutzen KI.") == (["percent:30~"], [])
    # A comma before three digits groups thousands in English and starts
    # three decimals in German: the number stays as written.
    assert _labels("Der Betrag liegt bei 1,250 Mio. Euro.") == (["amount:1,250 Mio. EUR~"], [])


def test_a_date_next_to_another_value_is_its_time_scope():
    assert _labels("Der Mindestlohn steigt 2026 auf 13,90 Euro.") == (
        ["amount:13.9 EUR"], ["date:2026"],
    )


def test_numbers_that_name_what_a_claim_is_about_are_no_values():
    assert _labels("Artikel 50 gilt für Anbieter.") == ([], [])
    assert _labels("Die Verordnung (EU) 2024/1689 regelt KI.") == ([], [])
    assert _labels("Die Frist beträgt 3 Monate.") == ([], [])


def test_different_single_values_of_one_kind_conflict():
    assert _conflict(
        "Artikel 50 ist ab dem 2. August 2025 anwendbar.",
        "Artikel 50 ist ab dem 2. August 2026 anwendbar.",
    )
    assert _conflict("Die Strafe beträgt 35 Mio. Euro.", "Die Strafe beträgt 15 Mio. Euro.")
    assert _conflict("Der Anteil liegt bei 7 %.", "Der Anteil liegt bei 3 %.")


def test_equal_compatible_or_incomparable_values_do_not_conflict():
    assert not _conflict("Die Frist endet 2026.", "Die Frist endet am 2. August 2026.")
    assert not _conflict(
        "Die Strafe beträgt bis zu 35 Mio. Euro.", "Die Strafe beträgt bis zu 15 Mio. Euro."
    )
    assert not _conflict("Rund 30 % nutzen KI.", "29,7 % nutzen KI.")
    assert not _conflict("Der Umsatz lag bei 80 Mrd. Dollar.", "Der Umsatz lag bei 74 Mrd. Euro.")
    assert not _conflict("Der Betrag liegt bei 1,250 Mio. Euro.", "Der Betrag liegt bei 1,25 Mio. Euro.")


def test_values_in_different_time_scopes_do_not_conflict():
    assert not _conflict(
        "Der Mindestlohn steigt 2026 auf 13,90 Euro.",
        "Der Mindestlohn steigt 2027 auf 14,60 Euro.",
    )
    assert _conflict(
        "Der Mindestlohn steigt 2026 auf 13,90 Euro.",
        "Der Mindestlohn steigt 2026 auf 14,60 Euro.",
    )


def test_claims_with_several_values_of_a_kind_are_left_to_a_reader():
    # A start and an end date that share one value: no deterministic
    # conflict; a claim pair like this needs a reader of both.
    assert not _conflict(
        "Die Pflichten gelten ab 2. August 2025 und vollständig ab 2. August 2027.",
        "Die Pflichten gelten ab 2. August 2025 und vollständig ab 2. Dezember 2027.",
    )


def test_a_scale_after_the_currency_still_scales():
    assert _labels("Meta plans 2026 AI capital expenditures of 115-135 USD billion.") == (
        ["amount:115000000000 USD", "amount:135000000000 USD"], ["date:2026"],
    )
