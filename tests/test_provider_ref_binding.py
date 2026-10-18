"""A statement's source is the citation that closes its passage in the provider's text.

The extractor model names the provider refs of every statement. Where the
statement's excerpt stands once in the search answer and a citation that
the source map resolves closes its paragraph or list item, that citation is
the provider's attribution: a model choice foreign to it is rebound and
counted. Everything the text does not settle stays as the model chose.
"""

import json
import logging
from types import SimpleNamespace

import pytest

from inqtrix.evidence_claim_processing import claim_extraction_text
from inqtrix.prompts import build_claim_extraction_prompt
from inqtrix.strategies import LLMClaimExtractor, ProviderCitationRef

PROPOSAL = "https://eur-lex.europa.eu/legal-content/EN/TXT/PDF/?uri=COM%3A2025%3A836%3AFIN"
CONSOLIDATED = "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX%3A02024R1689-20240712"
REFS = [
    ProviderCitationRef(ref="1", url=PROPOSAL, title="COM(2025) 836 final"),
    ProviderCitationRef(ref="2", url=CONSOLIDATED, title="EUR-Lex 02024R1689"),
]
AZURE_TEXT = (
    "Coherent, web-grounded provider answer:\n"
    "Die Verordnung ist am **1. August 2024 in Kraft getreten**. Für **Artikel 50** ist wichtig: "
    "Artikel 50 Absätze 1 bis 4 gelten ab dem 2. August 2026, während **Artikel 50 Absatz 5 "
    "bereits ab dem 2. Februar 2025** gilt. ([eur-lex.europa.eu](" + PROPOSAL + "))\n\n"
    "Die konsolidierte Fassung nennt den 12. Juli 2024 als Veröffentlichung im Amtsblatt. "
    "([eur-lex.europa.eu](" + CONSOLIDATED + "))"
)
ABS5 = "Artikel 50 Absatz 5 bereits ab dem 2. Februar 2025 gilt"


class _ClaimsLLM:
    """Answers the extraction with the given claims."""

    def __init__(self, claims: list[dict]) -> None:
        self._claims = claims

    def complete_with_metadata(self, prompt, *args, **kwargs):
        del prompt, args, kwargs
        return SimpleNamespace(
            content=json.dumps({"claims": self._claims}, ensure_ascii=False),
            prompt_tokens=10,
            completion_tokens=5,
        )


def _claim(snippet: str, refs: list[str]) -> dict:
    return {
        "claim_text": "Artikel 50 Absatz 5 gilt ab dem 2. Februar 2025.",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": True,
        "evidence_snippet": snippet,
        "provider_refs": refs,
    }


def _extract(text: str, claim: dict, refs=REFS):
    extractor = LLMClaimExtractor(_ClaimsLLM([claim]), claim_extract_model="stub-model")
    claims, _, _ = extractor.extract(
        text, [ref.url for ref in refs], "Ab wann gelten die Pflichten?", provider_refs=refs,
    )
    return claims[0], extractor.consume_extraction_metadata()


def test_a_foreign_ref_is_rebound_to_the_citation_closing_its_paragraph(caplog):
    caplog.set_level(logging.WARNING, logger="inqtrix")

    claim, metadata = _extract(AZURE_TEXT, _claim(ABS5, ["2"]))

    assert claim["provider_refs"] == ["1"]
    assert claim["source_urls"] == [PROPOSAL]
    assert metadata["provider_refs_rebound"] == 1
    assert "rebound 1 of 1" in caplog.text


def test_a_choice_that_shares_the_citation_stays():
    claim, metadata = _extract(AZURE_TEXT, _claim(ABS5, ["1", "2"]))

    assert claim["provider_refs"] == ["1", "2"]
    assert metadata["provider_refs_rebound"] == 0


@pytest.mark.parametrize(
    "snippet",
    [
        "Artikel 50 Absatz 5 gilt seit Februar 2025",  # not in the text
        "in Kraft",  # stands once, but too short to place
    ],
)
def test_an_excerpt_the_text_does_not_place_stays_unchecked(snippet):
    claim, metadata = _extract(AZURE_TEXT, _claim(snippet, ["2"]))

    assert claim["provider_refs"] == ["2"]
    assert metadata["provider_refs_rebound"] == 0
    assert metadata["provider_refs_unchecked"] == 1


def test_an_excerpt_that_stands_twice_is_not_placed():
    text = AZURE_TEXT + "\n\nNoch einmal: " + ABS5 + ". ([eur-lex.europa.eu](" + CONSOLIDATED + "))"

    claim, metadata = _extract(text, _claim(ABS5, ["2"]))

    assert claim["provider_refs"] == ["2"]
    assert metadata["provider_refs_unchecked"] == 1


def test_a_citation_after_the_next_list_item_does_not_bind():
    text = (
        "Betroffen sind:\n"
        "- Anbieter von KI-Systemen, vor allem bei Hochrisiko-Systemen.\n"
        "- Betreiber, die KI im Personalwesen einsetzen. ([eur-lex.europa.eu](" + CONSOLIDATED + "))"
    )

    claim, metadata = _extract(text, _claim("Anbieter von KI-Systemen, vor allem bei Hochrisiko-Systemen", ["1"]))

    assert claim["provider_refs"] == ["1"]
    assert metadata["provider_refs_unchecked"] == 1


def test_numbered_markers_close_a_sentence_as_well():
    text = "Die Pflicht gilt ab dem 2. August 2026 [2]. Der Vorschlag nennt andere Daten [1]."

    claim, metadata = _extract(text, _claim("Die Pflicht gilt ab dem 2. August 2026", ["1"]))

    assert claim["provider_refs"] == ["2"]
    assert metadata["provider_refs_rebound"] == 1


def test_a_citation_outside_the_source_map_binds_nothing():
    text = (
        "Die Pflicht gilt ab dem 2. August 2026. ([example.org](https://example.org/unknown)) "
        "([eur-lex.europa.eu](" + PROPOSAL + "))"
    )

    claim, metadata = _extract(text, _claim("Die Pflicht gilt ab dem 2. August 2026", ["2"]))

    assert claim["provider_refs"] == ["2"]
    assert metadata["provider_refs_unchecked"] == 1


def test_the_source_list_below_the_answer_binds_nothing():
    # A title from the source list is no statement of the answer, and the
    # list's next number is no citation of it.
    refs = [
        ProviderCitationRef(ref="1", url=CONSOLIDATED, title="EUR-Lex - 02024R1689-20240712"),
        ProviderCitationRef(ref="2", url=PROPOSAL, title="COM(2025) 836 final"),
    ]
    text = claim_extraction_text(
        "Die Verordnung wurde am 12. Juli 2024 veröffentlicht. ([eur-lex.europa.eu](" + CONSOLIDATED + "))",
        [
            {"url": CONSOLIDATED, "rank": 1, "title": "EUR-Lex - 02024R1689-20240712 - DE - EUR-Lex"},
            {"url": PROPOSAL, "rank": 2, "title": "COM(2025) 836 final"},
        ],
    )

    claim, metadata = _extract(text, _claim("EUR-Lex - 02024R1689-20240712", ["1"]), refs=refs)

    assert claim["provider_refs"] == ["1"]
    assert metadata["provider_refs_rebound"] == 0
    assert metadata["provider_refs_unchecked"] == 1


def test_the_prompt_example_names_no_concrete_ref():
    prompt = build_claim_extraction_prompt()

    assert '"provider_refs": ["2"]' not in prompt
    assert '{"ref": "2"' not in prompt
    assert 'e.g. ["2"]' not in prompt
