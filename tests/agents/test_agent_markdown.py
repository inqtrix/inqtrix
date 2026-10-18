"""Markdown hygiene at agent-generation boundaries."""

from __future__ import annotations

import pytest

from inqtrix.agents.markdown import normalize_agent_markdown
from inqtrix.agents.scheduler import task_result_summary


def test_currency_dollars_are_escaped_without_creating_cross_prose_math() -> None:
    markdown = (
        "Gartner meldet $1.5T, waehrend Statista US-$244 Mrd. ausweist."
    )

    assert normalize_agent_markdown(markdown) == (
        r"Gartner meldet \$1.5T, waehrend Statista US-\$244 Mrd. ausweist."
    )
    assert normalize_agent_markdown(
        "Szenario $1.5T + Forecast, Vergleich US-$244B."
    ) == r"Szenario \$1.5T + Forecast, Vergleich US-\$244B."


def test_genuine_inline_and_block_math_are_preserved() -> None:
    markdown = (
        "Die Variable $x$, $O(n)$, $x, y$, $1.5$ und die Formel "
        "$x + y = z$ bleiben.\n\n"
        "$$\nE = mc^2\n$$\n"
    )

    assert normalize_agent_markdown(markdown) == markdown


def test_code_spans_and_fences_are_preserved_while_prose_is_normalized() -> None:
    markdown = (
        "`price = '$1.5T'` und ausserhalb $2T.\n\n"
        "```python\nprice = '$3T'\n```\n"
    )
    assert normalize_agent_markdown("Offen ` und Preis $4T") == (
        r"Offen ` und Preis \$4T"
    )

    assert normalize_agent_markdown(markdown) == (
        "`price = '$1.5T'` und ausserhalb \\$2T.\n\n"
        "```python\nprice = '$3T'\n```\n"
    )


def test_normalization_is_idempotent_and_keeps_existing_escapes() -> None:
    markdown = r"Umsatz: \$1.5T; Formel: $x-y$."
    normalized = normalize_agent_markdown(markdown)

    assert normalized == markdown
    assert normalize_agent_markdown(normalized) == normalized


def test_url_query_dollars_are_byte_preserved() -> None:
    markdown = (
        "Quelle: https://example.com/report?q=$20&formula=$x$ "
        "und Marktwert $20B."
    )

    assert normalize_agent_markdown(markdown) == (
        "Quelle: https://example.com/report?q=$20&formula=$x$ "
        r"und Marktwert \$20B."
    )


@pytest.mark.parametrize(
    ("words", "expected"),
    [pytest.param(301, ("[…]", 300), id="cut"), pytest.param(300, ("w300", 300), id="at-the-bound")],
)
def test_a_long_task_summary_ends_with_a_visible_cut_mark(words, expected) -> None:
    """The control row holds at most 300 words; a longer summary keeps the
    first 299 and ends in the mark, so the row shows that it was cut. Turns
    red when the summary is sliced without a mark, or when a summary at the
    bound is cut."""
    summary = task_result_summary(" ".join(f"w{index}" for index in range(1, words + 1)))
    assert (summary.split()[-1], len(summary.split())) == expected


def test_task_result_summary_uses_the_same_currency_boundary() -> None:
    assert task_result_summary("Marktwert US-$1.5T; Formel $x$.") == (
        r"Marktwert US-\$1.5T; Formel $x$."
    )


def test_amount_pairs_in_a_calculation_line_are_currency_not_math() -> None:
    """A calculation line like "5,120 × $0.023 = **$117.76**" must not render
    as KaTeX because the span between the two amounts contains "=". A
    closing dollar that is followed by digits opens the next amount;
    emphasis markers never occur inside a formula."""
    line = "- Speicher: 5,120 × $0.023 = **$117.76**\n- Egress: 1,024 × $0.09 = **$92.16**"
    assert normalize_agent_markdown(line) == (
        "- Speicher: 5,120 × \\$0.023 = **\\$117.76**\n- Egress: 1,024 × \\$0.09 = **\\$92.16**"
    )
    # Without emphasis the same pairing still holds: the closing dollar
    # opens the next amount.
    assert normalize_agent_markdown("Preis: $0.023 = $0.12 gesamt") == "Preis: \\$0.023 = \\$0.12 gesamt"
    # An amount followed by emphasis and, later, a genuine formula: the
    # span "0.12** pro Monat; Formel " carries "*" (a math signal) but
    # emphasis never occurs inside a formula, so the amount is currency and
    # the formula stays intact.
    assert normalize_agent_markdown("Kosten **$0.12** pro Monat; Formel $E = P t$.") == (
        "Kosten **\\$0.12** pro Monat; Formel $E = P t$."
    )
    # A formula that legitimately contains "=" and no amounts stays math.
    assert normalize_agent_markdown("Formel $E = P t$ gilt.") == "Formel $E = P t$ gilt."
    # Table cells with single amounts are currency.
    assert normalize_agent_markdown("| S3 | $0.023 | $0.09 |") == "| S3 | \\$0.023 | \\$0.09 |"


def test_escaped_amounts_inside_code_spans_lose_the_backslash() -> None:
    """A model can write ``\\$0,023`` inside a code span, where the
    backslash would render. A code span needs no escaping, so only the
    escape before a digit is dropped; other backslashes stay."""
    text = (
        "Preis `\\$0,023`/GB-Monat und `\\$0,09/GB` Egress; Shell `echo \\$HOME` "
        "bleibt, Prosa $5 wird escaped."
    )
    normalized = normalize_agent_markdown(text)
    assert "`$0,023`" in normalized and "`$0,09/GB`" in normalized
    assert "`echo \\$HOME`" in normalized
    assert "Prosa \\$5" in normalized
    assert normalize_agent_markdown(normalized) == normalized
