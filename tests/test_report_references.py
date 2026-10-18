"""Report-reference de-duplication for the knowledge citation feature.

The default knowledge deployment cites chunks via
``inqtrix://documents/{id}#chunk-{n}``; ``normalize_url`` strips the
fragment, so de-duping by normalized URL would silently collapse every
cited chunk of one document into a single reference (dead ``[K#]`` links,
missing excerpts). These tests pin the (document_id, chunk_index) identity
for knowledge references while keeping URL-dedup for web references.
"""

from __future__ import annotations

from types import SimpleNamespace

from inqtrix.nodes import (
    _format_report_reference_records,
    _reference_facts_from_evidence_ledger,
)
from inqtrix.result import _report_references_from_state


class _Tiering:
    def tier_for_url(self, url: str) -> str:  # noqa: D401 - test stub
        return "unknown"


def test_same_document_chunks_survive_url_dedup() -> None:
    references = [
        {
            "label": "K1",
            "url": "inqtrix://documents/doc1#chunk-0",
            "tier": "primary",
            "document_id": "doc1",
            "chunk_index": 0,
            "excerpt": "Chunk null.",
        },
        {
            "label": "K2",
            "url": "inqtrix://documents/doc1#chunk-5",
            "tier": "primary",
            "document_id": "doc1",
            "chunk_index": 5,
            "excerpt": "Chunk fuenf.",
        },
    ]

    out = _report_references_from_state(
        references, tier_by_url={}, tiering=_Tiering()
    )

    assert [reference.label for reference in out] == ["K1", "K2"]
    assert out[0].excerpt == "Chunk null."
    assert out[1].chunk_index == 5


def test_same_document_same_chunk_is_still_deduped() -> None:
    references = [
        {"label": "K1", "url": "inqtrix://documents/doc1#chunk-0",
         "document_id": "doc1", "chunk_index": 0},
        {"label": "K2", "url": "inqtrix://documents/doc1#chunk-0",
         "document_id": "doc1", "chunk_index": 0},
    ]

    out = _report_references_from_state(
        references, tier_by_url={}, tiering=_Tiering()
    )

    assert [reference.label for reference in out] == ["K1"]


def test_web_references_still_dedup_by_url() -> None:
    references = [
        {"label": "E1", "url": "https://example.com/a", "tier": "unknown"},
        {"label": "E2", "url": "https://example.com/a", "tier": "unknown"},
    ]

    out = _report_references_from_state(
        references, tier_by_url={}, tiering=_Tiering()
    )

    assert [reference.label for reference in out] == ["E1"]
    assert out[0].document_id is None


def test_internal_reference_without_url_keeps_identity_and_support_fields() -> None:
    out = _report_references_from_state(
        [
            {
                "label": "K1",
                "document_id": "doc1",
                "chunk_index": 4,
                "source_text": "Exact internal passage",
            }
        ],
        tier_by_url={},
        tiering=_Tiering(),
    )

    assert len(out) == 1
    assert out[0].url == "inqtrix://documents/doc1#chunk-4"
    assert out[0].document_id == "doc1"
    assert out[0].chunk_index == 4
    assert out[0].source_text == "Exact internal passage"


def test_web_search_lineage_survives_report_reference_projection() -> None:
    out = _report_references_from_state(
        [
            {
                "label": "W1",
                "url": "https://prices.example.test/api",
                "reference_id": "ref_price",
                "source_id": "source_price",
                "query_id": "query_price",
                "query_ids": ["query_price", "query_price_followup"],
                "citation_id": "citation_price",
                "citation_ids": ["citation_price"],
                "source_run_id": "run_price",
                "source_run_ids": ["run_price"],
                "provider_snippet": "Azure provider snippet",
            }
        ],
        tier_by_url={},
        tiering=_Tiering(),
    )

    assert len(out) == 1
    payload = out[0].model_dump(mode="json")
    assert payload["reference_id"] == "ref_price"
    assert payload["source_id"] == "source_price"
    assert payload["query_id"] == "query_price"
    assert payload["query_ids"] == ["query_price", "query_price_followup"]
    assert payload["citation_id"] == "citation_price"
    assert payload["source_run_id"] == "run_price"
    assert payload["provider_snippet"] == "Azure provider snippet"


def test_knowledge_reference_keeps_its_verified_quotes() -> None:
    # The verified quotes reach the exported reference; an entry without
    # quotable text proves nothing and is not carried.
    out = _report_references_from_state(
        [
            {
                "label": "K1",
                "document_id": "doc1",
                "chunk_index": 0,
                "source_text": "Die Haftung ist begrenzt. Sonst nichts.",
                "quotes": [
                    {
                        "text": "Die Haftung ist begrenzt.",
                        "verified": True,
                        "artifact_tolerated": True,
                    },
                    {"text": "", "verified": True, "artifact_tolerated": False},
                ],
            }
        ],
        tier_by_url={},
        tiering=_Tiering(),
    )

    assert out[0].model_dump(mode="json")["quotes"] == [
        {
            "text": "Die Haftung ist begrenzt.",
            "verified": True,
            "artifact_tolerated": True,
        }
    ]


# --- Page titles travel with the structured reference records -------------
#
# The evidence ledger records the provider's page title with every
# citation. Records carrying label, url and tier only would make a run that
# hands its references on (the Agent Desk's child report) show bare URLs for
# them.

_KFW = "https://www.kfw.de/inlandsfoerderung/Heizungsfoerderung"
_BMLUK = "https://www.bmluk.gv.at/service/presse/2026/sanierungsoffensive"


def _strategies() -> SimpleNamespace:
    return SimpleNamespace(source_tiering=_Tiering())


def test_ledger_facts_carry_tier_and_title_per_url() -> None:
    ledger = [
        {
            "tier": "primary",
            "title": "",
            "canonical_url": _KFW,
            "citation_set": [
                {"url": _KFW, "title": "Heizungsförderung | KfW"},
                {"url": _BMLUK, "title": "Sanierungsoffensive 2026 – BMLUK"},
            ],
        },
        {
            "tier": "secondary",
            "title": "Zweiter Datensatz",
            "canonical_url": _BMLUK,
            "citation_set": [{"url": _BMLUK, "title": "Spaeterer Titel"}],
        },
    ]
    facts = _reference_facts_from_evidence_ledger(ledger)
    assert facts.tier_by_url == {_KFW: "primary", _BMLUK: "primary"}
    # First occurrence wins, exactly like the tiers.
    assert facts.title_by_url == {
        _KFW: "Heizungsförderung | KfW",
        _BMLUK: "Sanierungsoffensive 2026 – BMLUK",
    }


def test_reference_records_carry_the_ledger_title_when_known() -> None:
    records = _format_report_reference_records(
        [("E1", _KFW), ("E2", _BMLUK)],
        _strategies(),
        tier_by_url={_KFW: "primary"},
        title_by_url={_KFW: "Heizungsförderung | KfW"},
    )
    assert records == [
        {
            "label": "E1",
            "url": _KFW,
            "tier": "primary",
            "cited": True,
            "title": "Heizungsförderung | KfW",
        },
        # No title known -> no key (the record shape stays as before).
        {"label": "E2", "url": _BMLUK, "tier": "unknown", "cited": True},
    ]
