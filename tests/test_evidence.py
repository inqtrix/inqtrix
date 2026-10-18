"""Tests for EvidenceLedger assembly and the record-driven evidence overview."""

from __future__ import annotations

import pytest

from inqtrix.evidence import (
    evidence_cut_words,
    COMPACT_CLAIM_LINE_CHARS,
    STANDING_TEXT,
    assemble_evidence_records,
    audit_answer_evidence_bindings,
    build_web_search_ledger,
    derive_claim_ledger_from_evidence,
    evidence_id_for_citation,
    project_claim_verification_to_evidence,
    render_evidence_ledger_overview,
    select_section_evidence_records,
)
from inqtrix.runtime_logging import make_record_id


def test_web_search_ledger_uses_provider_annotation_span_without_page_read():
    answer = "Global input costs 5 USD. Data Zone costs more."
    ledger = build_web_search_ledger(
        run_id="run_1",
        query_records=[
            {
                "query_id": "query_1",
                "query": "model prices",
                "provider": "AzureFoundryWebSearch",
                "status": "completed",
            }
        ],
        query_synthesis={
            "query_1": {
                "query": "model prices",
                "provider_answer": answer,
            }
        },
        citation_records=[
            {
                "annotation_end": 25,
                "annotation_start": 0,
                "canonical_url": "https://example.com/prices",
                "citation_id": "citation_1",
                "query_id": "query_1",
                "source_id": "source_1",
                "title": "Prices",
            }
        ],
    )

    search = ledger["searches"]["query_1"]
    assert search["provider_answer"] == answer
    assert search["citations"][0]["grounded_support"] == (
        "Global input costs 5 USD."
    )
    assert search["citations"][0]["mapping_status"] == (
        "provider_answer_context"
    )


def test_web_search_ledger_labels_link_only_annotation_as_marker_context():
    answer = (
        "Global input costs 5 USD. "
        "([azure.microsoft.com](https://azure.microsoft.com/prices))"
    )
    marker_start = answer.index("([")
    ledger = build_web_search_ledger(
        run_id="run_1",
        query_records=[
            {
                "query_id": "query_1",
                "query": "model prices",
                "provider": "AzureFoundryWebSearch",
                "status": "completed",
            }
        ],
        query_synthesis={
            "query_1": {
                "query": "model prices",
                "provider_answer": answer,
            }
        },
        citation_records=[
            {
                "annotation_end": len(answer),
                "annotation_start": marker_start,
                "canonical_url": "https://azure.microsoft.com/prices",
                "citation_id": "citation_1",
                "query_id": "query_1",
                "source_id": "source_1",
                "title": "Prices",
            }
        ],
    )

    citation = ledger["searches"]["query_1"]["citations"][0]
    assert citation["mapping_status"] == "provider_citation_marker"
    assert citation["grounded_support"] == "Global input costs 5 USD."


LONG_ANSWER_LINE = "Wort " * 119 + "1 500 000 EUR bleiben offen."
"""An answer line of 623 characters whose ``1 500 000`` straddles the
600-character bound of a grounded support."""


def _annotated_ledger(answer: str, start: int, end: int) -> dict:
    return build_web_search_ledger(
        run_id="run_1",
        query_records=[
            {
                "query_id": "query_1",
                "query": "open funds",
                "provider": "AzureFoundryWebSearch",
                "status": "completed",
            }
        ],
        query_synthesis={
            "query_1": {"query": "open funds", "provider_answer": answer}
        },
        citation_records=[
            {
                "annotation_end": end,
                "annotation_start": start,
                "canonical_url": "https://example.com/funds",
                "citation_id": "citation_1",
                "query_id": "query_1",
                "source_id": "source_1",
                "title": "Funds",
            }
        ],
    )["searches"]["query_1"]["citations"][0]


def test_a_long_annotation_span_is_cut_visibly_before_a_number() -> None:
    citation = _annotated_ledger(LONG_ANSWER_LINE, 0, len(LONG_ANSWER_LINE))
    assert citation["mapping_status"] == "provider_answer_context"
    assert citation["grounded_support"] == ("Wort " * 119).rstrip() + "…"


def test_a_long_marker_context_is_cut_visibly_before_a_number() -> None:
    answer = LONG_ANSWER_LINE + " ([example.com](https://example.com/funds))"
    citation = _annotated_ledger(answer, answer.index(" (["), len(answer))
    assert citation["mapping_status"] == "provider_citation_marker"
    assert citation["grounded_support"] == ("Wort " * 119).rstrip() + "…"


def _source(source_id: str, url: str, tier: str = "primary") -> dict:
    return {
        "source_id": source_id,
        "url": url,
        "canonical_url": url,
        "domain": "example.com",
        "provider": "StubSearch",
        "first_seen_query_id": "qry_1",
        "first_seen_rank": 1,
        "origin": "search_results",
        "tier": tier,
        "tier_reason": "matched_primary_domain",
        "access_status": "answer",
    }


def _citation(query_id: str, source_id: str, url: str, rank: int = 1) -> dict:
    return {
        "citation_id": make_record_id("cit", query_id, rank, url, "search_results"),
        "query_id": query_id,
        "source_id": source_id,
        "url": url,
        "canonical_url": url,
        "rank": rank,
        "origin": "search_results",
        "provider": "StubSearch",
        "title": "Example report",
        "snippet": "Meta expects capital expenditures of 115-135 USD billion.",
        "source_date": "2026-05-01",
        "last_updated": "2026-05-02",
    }


def _synthesis(query_id: str, query: str, summary: str, provider_answer: str = "") -> dict:
    """Build a one-query ``query_synthesis`` map entry for the renderer."""
    return {
        query_id: {
            "query": query,
            "round": 0,
            "provider_answer": provider_answer or summary,
            "related_questions": [],
        }
    }


def test_assemble_evidence_records_preserves_claims_and_passages_without_data_points():
    query_id = "qry_1"
    url = "https://example.com/report"
    source = _source("src_1", url)
    citation = _citation(query_id, "src_1", url)
    evidence_id = evidence_id_for_citation(query_id, citation)
    claim = {
        "raw_claim_id": "raw_1",
        "claim_text": "Meta expects 115-135 USD billion of capital expenditures.",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": True,
        "source_urls": [url],
        "source_ids": ["src_1"],
        "citation_ids": [citation["citation_id"]],
        "evidence_ids": [evidence_id],
        "published_date": "2026-05-01",
        "signature": "meta capex 115 135 usd billion",
        "evidence_snippet": "The guidance range is 115-135 USD billion.",
    }

    records = assemble_evidence_records(
        query_id=query_id,
        query="meta capex",
        provider="StubSearch",
        source_records=[source],
        citation_records=[citation],
        claim_entries=[claim],
    )

    assert len(records) == 1
    record = records[0]
    assert record["evidence_id"] == evidence_id
    assert "provider_answer_excerpt" not in record
    assert record["claims"][0]["raw_claim_id"] == "raw_1"
    assert record["source_passages"]
    assert "data_points" not in record


def test_multi_source_claim_attaches_to_each_source_record():
    """A claim citing two sources yields one per-source record each, both citable."""
    query_id = "qry_multi_src"
    urls = ["https://example.com/a", "https://example.com/b"]
    citations = [
        _citation(query_id, f"src_{index}", url, rank=index)
        for index, url in enumerate(urls, start=1)
    ]
    evidence_ids = [evidence_id_for_citation(query_id, citation) for citation in citations]
    claim = {
        "raw_claim_id": "raw_multi",
        "claim_text": "Example reported 12 percent growth.",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "source_urls": urls,
        "source_ids": ["src_1", "src_2"],
        "citation_ids": [citation["citation_id"] for citation in citations],
        "evidence_ids": evidence_ids,
        "published_date": "2026-05-01",
        "signature": "example growth 12 percent",
        "evidence_snippet": "Two sources cite 12 percent growth.",
    }

    records = assemble_evidence_records(
        query_id=query_id,
        query="multi source",
        provider="StubSearch",
        source_records=[_source("src_1", urls[0]), _source("src_2", urls[1])],
        citation_records=citations,
        claim_entries=[claim],
    )

    assert len(records) == 2
    assert all(record["record_type"] == "source" for record in records)
    assert all(record["report_eligible"] for record in records)
    assert all(
        record["claims"] and record["claims"][0]["raw_claim_id"] == "raw_multi"
        for record in records
    )


def test_url_only_source_is_citeable_without_snippet():
    """Azure-style URL-only sources (no per-source body) stay citable anchors."""
    query_id = "qry_urlonly"
    url = "https://www.bloomberg.com/latest/the-ai-race"
    citation = _citation(query_id, "src_1", url)
    citation["snippet"] = ""
    evidence_id = evidence_id_for_citation(query_id, citation)
    claim = {
        "raw_claim_id": "raw_1",
        "claim_text": "Cloudflare announced 1,100 layoffs.",
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": True,
        "source_urls": [url],
        "source_ids": ["src_1"],
        "citation_ids": [citation["citation_id"]],
        "evidence_ids": [evidence_id],
        "signature": "cloudflare layoffs 1100",
    }

    records = assemble_evidence_records(
        query_id=query_id,
        query="cloudflare layoffs",
        provider="AzureFoundryWebSearch",
        source_records=[_source("src_1", url, tier="mainstream")],
        citation_records=[citation],
        claim_entries=[claim],
    )

    assert len(records) == 1
    assert records[0]["report_eligible"] is True
    assert records[0]["source_snippet"] == ""
    assert records[0]["claims"][0]["citation_set"][0]["url"] == url


def _records_for_query(query_id: str, query: str, count: int) -> list[dict]:
    records: list[dict] = []
    for index in range(count):
        url = f"https://example.com/{query_id}-{index}"
        citation = _citation(query_id, f"src_{query_id}_{index}", url, rank=index + 1)
        records += assemble_evidence_records(
            query_id=query_id,
            query=query,
            provider="StubSearch",
            source_records=[_source(f"src_{query_id}_{index}", url)],
            citation_records=[citation],
            claim_entries=[],
        )
    return records


def test_evidence_ledger_overview_renders_every_eligible_record():
    records = _records_for_query("qrymulti", "multi source query", 3)
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=8000,
        max_record_chars=1500,
        query_synthesis=_synthesis("qrymulti", "multi source query", "Summary: three sources."),
    )
    assert overview.rendered_record_count == 3
    assert overview.omitted_record_count == 0
    for index in range(1, 4):
        assert f"[E{index}]" in overview.markdown
    assert set(overview.label_urls) == {"E1", "E2", "E3"}
    assert len(overview.allowed_urls) == 3
    # URLs stay out of the prompt body (token saving); they live in the
    # label map and are re-attached after synthesis.
    assert overview.label_urls["E1"] not in overview.markdown
    assert "  URL:" not in overview.markdown


def test_evidence_ledger_overview_shows_provider_answer_once_per_query():
    records = _records_for_query("qryone", "query number one", 2)
    records += _records_for_query("qrytwo", "query number two", 2)
    query_synthesis = {
        **_synthesis("qryone", "query number one", "SUMMARY-Q1 digest text."),
        **_synthesis("qrytwo", "query number two", "SUMMARY-Q2 digest text."),
    }
    overview = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=2000, query_synthesis=query_synthesis
    )
    assert overview.rendered_record_count == 4
    assert overview.markdown.count("SUMMARY-Q1 digest text.") == 1
    assert overview.markdown.count("SUMMARY-Q2 digest text.") == 1


def test_evidence_ledger_overview_renders_claimless_record_as_context():
    records = _records_for_query("qrycl", "claimless query", 1)
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=8000,
        max_record_chars=2000,
        query_synthesis=_synthesis(
            "qrycl", "claimless query", "Summary: claimless source still useful."
        ),
    )
    assert overview.rendered_record_count == 1
    assert f"Standing: {STANDING_TEXT['source-context']}" in overview.markdown
    assert "Meta expects capital expenditures" in overview.markdown


def test_evidence_ledger_overview_dedups_repeated_text():
    query_id = "qrydup"
    url = "https://example.com/dup"
    repeated = "The launch event is scheduled for the third quarter."
    citation = _citation(query_id, "src_dup", url)
    citation["snippet"] = repeated
    evidence_id = evidence_id_for_citation(query_id, citation)
    claim = {
        "raw_claim_id": "raw_dup",
        "claim_text": repeated,
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "source_urls": [url],
        "source_ids": ["src_dup"],
        "citation_ids": [citation["citation_id"]],
        "evidence_ids": [evidence_id],
        "published_date": "2026-05-01",
        "signature": "launch event third quarter",
        "evidence_snippet": repeated,
    }
    records = assemble_evidence_records(
        query_id=query_id,
        query="dedup query",
        provider="StubSearch",
        source_records=[_source("src_dup", url)],
        citation_records=[citation],
        claim_entries=[claim],
    )
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=8000,
        max_record_chars=2000,
        query_synthesis=_synthesis(query_id, "dedup query", "Summary: dedup check."),
    )
    assert overview.markdown.count(repeated) == 1


def test_evidence_ledger_overview_omits_over_budget_records_visibly():
    records = _records_for_query("qrybudget", "budget query", 6)
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=900,
        max_record_chars=400,
        query_synthesis=_synthesis(
            "qrybudget", "budget query", "Summary: many sources, tiny budget."
        ),
    )
    assert 0 < overview.rendered_record_count < 6
    assert overview.omitted_record_count > 0
    assert overview.rendered_record_count + overview.omitted_record_count == 6
    assert "NOTE:" in overview.markdown
    assert len(overview.allowed_urls) == overview.rendered_record_count
    # The cut names what it dropped (trace + report need labels, not a count).
    assert len(overview.omitted_records) == overview.omitted_record_count
    assert overview.omitted_query_ids == ["qrybudget"]
    rendered_ids = set(overview.rendered_evidence_ids)
    for dropped in overview.omitted_records:
        assert dropped["reason"] == "budget_tail"
        assert dropped["query_id"] == "qrybudget"
        assert dropped["url"].startswith("https://example.com/qrybudget-")
        assert dropped["label"].startswith("E")
        assert dropped["evidence_id"] not in rendered_ids


def test_evidence_ledger_overview_with_zero_rendered_records_is_empty():
    records = _records_for_query("qrynone", "zero budget query", 2)
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=1,
        max_record_chars=400,
        query_synthesis=_synthesis(
            "qrynone", "zero budget query", "Summary too large for the budget."
        ),
    )

    assert overview.rendered_record_count == 0
    assert overview.omitted_record_count == 2
    assert overview.markdown == ""
    assert overview.allowed_urls == []
    assert [record["reason"] for record in overview.omitted_records] == [
        "whole_group",
        "whole_group",
    ]
    assert overview.omitted_query_ids == ["qrynone"]


def test_render_allowed_urls_only_include_rendered_source_blocks():
    records = _records_for_query("qryvisible", "visible budget query", 5)
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=900,
        max_record_chars=400,
        query_synthesis=_synthesis("qryvisible", "visible budget query", "Summary."),
    )

    url_by_id = {record["evidence_id"]: record["canonical_url"] for record in records}
    rendered_urls = [
        url_by_id[evidence_id]
        for evidence_id in overview.rendered_evidence_ids
    ]

    assert 0 < overview.rendered_record_count < len(records)
    assert overview.allowed_urls == rendered_urls
    assert set(overview.label_urls.values()) == set(rendered_urls)


def test_render_assigns_one_label_per_canonical_url_across_queries():
    url = "https://example.com/shared-source"
    records: list[dict] = []
    for index, query_id in enumerate(("qry_shared_a", "qry_shared_b"), start=1):
        citation = _citation(query_id, f"src_shared_{index}", url, rank=index)
        records += assemble_evidence_records(
            query_id=query_id,
            query=f"shared query {index}",
            provider="StubSearch",
            source_records=[_source(f"src_shared_{index}", url)],
            citation_records=[citation],
            claim_entries=[],
        )

    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=8000,
        max_record_chars=1200,
        query_synthesis={
            **_synthesis("qry_shared_a", "shared query 1", "Summary one."),
            **_synthesis("qry_shared_b", "shared query 2", "Summary two."),
        },
    )
    labels = {
        overview.label_by_evidence_id[record["evidence_id"]]
        for record in records
    }

    assert labels == {"E1"}
    assert overview.label_urls == {"E1": url}
    assert overview.allowed_urls == [url]


def test_render_remaps_provider_answer_citations_to_labels():
    """Provider inline citations are rewritten to [E#] labels; no raw URLs leak."""
    query_id = "qry_remap"
    url1 = "https://primary.example/a"
    url2 = "https://media.example/b"
    records: list[dict] = []
    for sid, url, rank in (("src_1", url1, 1), ("src_2", url2, 2)):
        citation = _citation(query_id, sid, url, rank=rank)
        records += assemble_evidence_records(
            query_id=query_id,
            query="remap query",
            provider="StubSearch",
            source_records=[_source(sid, url)],
            citation_records=[citation],
            claim_entries=[],
        )
    query_synthesis = {
        query_id: {
            "query": "remap query",
            "round": 0,
            # Perplexity numeric [1] + Azure Markdown-link citation styles:
            "provider_answer": (
                f"Fakt eins [1]. Fakt zwei ([media.example]({url2})). "
                "Beide zusammen [1][2]."
            ),
            "related_questions": [],
            "citation_urls_by_rank": {"1": url1, "2": url2},
        }
    }
    overview = render_evidence_ledger_overview(
        records, max_total_chars=8000, max_record_chars=2000, query_synthesis=query_synthesis
    )
    md = overview.markdown
    label1 = next(label for label, url in overview.label_urls.items() if "primary.example" in url)
    label2 = next(label for label, url in overview.label_urls.items() if "media.example" in url)
    assert f"Fakt eins [{label1}]." in md
    assert f"Fakt zwei [{label2}]." in md
    assert f"Beide zusammen [{label1}] [{label2}]." in md
    assert "[1]" not in md
    assert url1 not in md and url2 not in md
    assert "Search answer (context; not independently verified):" in md


def test_render_keeps_unmapped_provider_citations_visible():
    query_id = "qry_unmapped"
    url = "https://primary.example/a"
    citation = _citation(query_id, "src_1", url, rank=1)
    records = assemble_evidence_records(
        query_id=query_id,
        query="unmapped query",
        provider="StubSearch",
        source_records=[_source("src_1", url)],
        citation_records=[citation],
        claim_entries=[],
    )
    query_synthesis = {
        query_id: {
            "query": "unmapped query",
            "round": 0,
            "provider_answer": "Fakt eins [99].",
            "related_questions": [],
            "citation_urls_by_rank": {"1": url},
        }
    }

    overview = render_evidence_ledger_overview(
        records, max_total_chars=8000, max_record_chars=2000, query_synthesis=query_synthesis
    )

    assert "[unmapped:99]" in overview.markdown


def test_provider_synthesis_marks_citations_without_visible_source_block():
    query_id = "qry_not_rendered"
    records: list[dict] = []
    for rank in range(1, 4):
        url = f"https://example.com/not-rendered-{rank}"
        citation = _citation(query_id, f"src_{rank}", url, rank=rank)
        citation["snippet"] = f"Long source {rank}. " * 50
        records += assemble_evidence_records(
            query_id=query_id,
            query="budget remap query",
            provider="StubSearch",
            source_records=[_source(f"src_{rank}", url)],
            citation_records=[citation],
            claim_entries=[],
        )
    # The budget fits two of the three source blocks: the third citation
    # has no visible block and is marked in the search answer.
    overview = render_evidence_ledger_overview(
        records,
        max_total_chars=660,
        max_record_chars=500,
        query_synthesis={
            query_id: {
                "query": "budget remap query",
                "round": 0,
                "provider_answer": "Combined statement [1][2][3].",
                "related_questions": [],
                "citation_urls_by_rank": {
                    str(rank): f"https://example.com/not-rendered-{rank}"
                    for rank in range(1, 4)
                },
            }
        },
    )

    assert 0 < overview.rendered_record_count < 3
    assert "[unrendered:" in overview.markdown
    assert "[E2] [unrendered:3]" in overview.markdown
    for label in overview.label_urls:
        assert f"[{label}] " in overview.markdown


def test_derive_claim_ledger_merges_one_claim_across_evidence_records():
    query_id = "qry_1"
    urls = ["https://primary.example/report", "https://media.example/story"]
    citations = [
        _citation(query_id, f"src_{idx}", url, rank=idx)
        for idx, url in enumerate(urls, start=1)
    ]
    records = []
    for idx, (url, citation) in enumerate(zip(urls, citations, strict=True), start=1):
        evidence_id = evidence_id_for_citation(query_id, citation)
        records.extend(
            assemble_evidence_records(
                query_id=query_id,
                query="meta capex",
                provider="StubSearch",
                source_records=[_source(f"src_{idx}", url)],
                citation_records=[citation],
                claim_entries=[
                    {
                        "raw_claim_id": "raw_shared",
                        "claim_text": "Meta expects 115-135 USD billion of capital expenditures.",
                        "claim_type": "fact",
                        "polarity": "affirmed",
                        "needs_primary": True,
                        "source_urls": [url],
                        "source_ids": [f"src_{idx}"],
                        "citation_ids": [citation["citation_id"]],
                        "evidence_ids": [evidence_id],
                        "published_date": "2026-05-01",
                        "signature": "meta capex 115 135 usd billion",
                    }
                ],
            )
        )

    ledger = derive_claim_ledger_from_evidence(records)

    assert len(ledger) == 1
    assert len(ledger[0]["evidence_ids"]) == 2
    assert len(ledger[0]["source_urls"]) == 2


def _claim_record(query_id: str, url: str, claim_text: str, *, status: str) -> dict:
    """Build one evidence record carrying a single projected-verification claim."""
    citation = _citation(query_id, "src_x", url)
    evidence_id = evidence_id_for_citation(query_id, citation)
    records = assemble_evidence_records(
        query_id=query_id,
        query="claim record query",
        provider="StubSearch",
        source_records=[_source("src_x", url)],
        citation_records=[citation],
        claim_entries=[
            {
                "raw_claim_id": f"raw_{query_id}",
                "claim_text": claim_text,
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": False,
                "source_urls": [url],
                "source_ids": ["src_x"],
                "citation_ids": [citation["citation_id"]],
                "evidence_ids": [evidence_id],
                "published_date": "2026-05-01",
                "signature": claim_text.lower(),
                "evidence_snippet": claim_text,
            }
        ],
    )
    record = records[0]
    for claim in record["claims"]:
        claim["verification_status"] = status
        claim["verification_basis"] = (
            "primary_source" if status in {"verified", "attributed"} else status
        )
        # A verified claim was checked against this record's own text.
        claim["checked_here"] = status == "verified"
    return record


def test_project_claim_verification_makes_ledger_self_describing():
    query_id = "qry_proj"
    url = "https://example.com/proj"
    citation = _citation(query_id, "src_x", url)
    evidence_id = evidence_id_for_citation(query_id, citation)
    records = assemble_evidence_records(
        query_id=query_id,
        query="projection query",
        provider="StubSearch",
        source_records=[_source("src_x", url)],
        citation_records=[citation],
        claim_entries=[
            {
                "raw_claim_id": "raw_proj",
                "claim_text": "Meta expects 115-135 USD billion of capital expenditures.",
                "claim_type": "fact",
                "polarity": "affirmed",
                "needs_primary": True,
                "source_urls": [url],
                "source_ids": ["src_x"],
                "citation_ids": [citation["citation_id"]],
                "evidence_ids": [evidence_id],
                "published_date": "2026-05-01",
                "signature": "meta capex",
                "evidence_snippet": "Guidance range is 115-135 USD billion.",
            }
        ],
    )
    consolidated = [
        {
            "claim_id": "claim_proj",
            "status": "verified",
            "verification_basis": "primary_source",
            "checked_urls": [url],
            "member_claim_ids": ["raw_proj"],
            "supporting_evidence_ids": [evidence_id],
            "supporting_domain_count": 1,
        }
    ]
    projected = project_claim_verification_to_evidence(records, consolidated)
    overview = render_evidence_ledger_overview(
        projected,
        max_total_chars=8000,
        max_record_chars=2000,
        query_synthesis=_synthesis(query_id, "projection query", "Summary."),
    )
    assert projected[0]["claims"][0]["verification_status"] == "verified"
    assert projected[0]["claims"][0]["checked_here"] is True
    assert f"Standing: {STANDING_TEXT['primary-source']}" in overview.markdown


def test_audit_answer_evidence_bindings_classifies_cited_sources():
    verified = _claim_record(
        "qry_v", "https://example.com/verified", "Verified fact stated.", status="verified"
    )
    context = _claim_record(
        "qry_c", "https://example.com/context", "Unverified note stated.", status="unverified"
    )
    answer = (
        "Erstens [E1](https://example.com/verified) belegt den Fakt. "
        "Zweitens [E2](https://example.com/context) liefert Kontext. "
        "Drittens [E9](https://example.com/not-in-ledger) ist unbekannt."
    )
    bindings = audit_answer_evidence_bindings(answer, [verified, context])
    by_url = {b["citation_url"]: b for b in bindings}
    assert by_url["https://example.com/verified"]["binding_status"] == "matched"
    assert by_url["https://example.com/context"]["binding_status"] == "source_context"
    assert by_url["https://example.com/not-in-ledger"]["binding_status"] == "unknown_citation"


def test_select_section_evidence_records_prefers_uncertainty_for_risks_section():
    verified = _claim_record(
        "qry_v", "https://example.com/verified", "Verified fact.", status="verified"
    )
    contested = _claim_record(
        "qry_x", "https://example.com/contested", "Contested claim.", status="contested"
    )
    label_by_id = {
        str(verified["evidence_id"]): "E1",
        str(contested["evidence_id"]): "E2",
    }
    selected = select_section_evidence_records(
        [verified, contested],
        section_key="risks",
        required_aspects=[],
        used_labels=set(),
        label_by_evidence_id=label_by_id,
        max_records=1,
    )
    assert selected and selected[0]["evidence_id"] == contested["evidence_id"]


def _group_block_chars(records: list[dict], query_id: str, query: str, count: int) -> int:
    """Length of one query group rendered with exactly ``count`` records."""
    overview = render_evidence_ledger_overview(
        records[:count],
        max_total_chars=10**9,
        max_record_chars=400,
        query_synthesis=_synthesis(query_id, query, "Summary."),
    )
    assert overview.rendered_record_count == count
    return len(overview.markdown)


def test_budget_thins_every_search_round_robin_instead_of_dropping_the_weakest_whole():
    """Two searches, the second lower-ranked (unknown tier). A budget that
    holds the first search whole plus one more source must render at least
    the best source of the second search — before, the first search took
    the largest prefix that fit and the second fell away whole (the answer
    then read as 'nothing found' for its half of the question)."""
    records_a = _records_for_query("qrya", "query a", 4)  # primary tier (fixture default)
    records_b: list[dict] = []
    for index in range(3):
        url = f"https://example.com/qryb-{index}"
        records_b += assemble_evidence_records(
            query_id="qryb",
            query="query b",
            provider="StubSearch",
            source_records=[_source(f"src_qryb_{index}", url, tier="unknown")],
            citation_records=[_citation("qryb", f"src_qryb_{index}", url, rank=index + 1)],
            claim_entries=[],
        )
    len_a_full = _group_block_chars(records_a, "qrya", "query a", 4)
    len_b_one = _group_block_chars(records_b, "qryb", "query b", 1)
    budget = len_a_full + 2 + len_b_one - 1  # the greedy cut: A whole, B nothing
    overview = render_evidence_ledger_overview(
        records_a + records_b,
        max_total_chars=budget,
        max_record_chars=400,
        query_synthesis={
            **_synthesis("qrya", "query a", "Summary."),
            **_synthesis("qryb", "query b", "Summary."),
        },
    )
    rendered_b = sum(
        1 for evidence_id in overview.rendered_evidence_ids
        if any(r["evidence_id"] == evidence_id for r in records_b)
    )
    rendered_a = overview.rendered_record_count - rendered_b
    assert rendered_b >= 1, "the second search must keep its best source"
    assert rendered_a - rendered_b <= 1, "no search gets its (N+1)-th before every open search has its N-th"
    assert overview.rendered_record_count + overview.omitted_record_count == 7
    assert len(overview.markdown) <= budget + 200  # footer may follow the accounting
    # The cut is named per search, with the query text the report can print.
    assert overview.omitted_by_query
    for entry in overview.omitted_by_query:
        assert entry["query"] in {"query a", "query b"}
        assert entry["omitted"] > 0
    assert sum(entry["omitted"] for entry in overview.omitted_by_query) == overview.omitted_record_count
    assert all(dropped["query"] for dropped in overview.omitted_records)
    assert "from" in overview.markdown.rsplit("NOTE:", 1)[1]


def test_budget_gives_every_search_its_best_source_before_any_second_one():
    """Three searches with two sources each and room for exactly three
    one-source blocks: every search renders exactly one source."""
    groups = {qid: _records_for_query(qid, f"query {qid}", 2) for qid in ("qryx", "qryy", "qryz")}
    one_each = sum(_group_block_chars(groups[qid], qid, f"query {qid}", 1) for qid in groups)
    budget = one_each + 2 * 2  # three blocks plus two separators, no room for a fourth source
    overview = render_evidence_ledger_overview(
        [record for records in groups.values() for record in records],
        max_total_chars=budget,
        max_record_chars=400,
        query_synthesis={
            k: v for qid in groups for k, v in _synthesis(qid, f"query {qid}", "Summary.").items()
        },
    )
    assert overview.rendered_record_count == 3
    assert overview.omitted_record_count == 3
    per_group = {
        qid: sum(1 for r in records if r["evidence_id"] in set(overview.rendered_evidence_ids))
        for qid, records in groups.items()
    }
    assert per_group == {"qryx": 1, "qryy": 1, "qryz": 1}
    assert [entry["rendered"] for entry in overview.omitted_by_query] == [1, 1, 1]
    assert all(dropped["reason"] == "budget_tail" for dropped in overview.omitted_records)


def test_compact_record_view_keeps_claims_and_drops_passages():
    """The evaluator's view: header and claim lines for every source, no
    snippets or passages — breadth where the full blocks would not fit."""
    url = "https://example.com/compact"
    records = assemble_evidence_records(
        query_id="qryc",
        query="compact query",
        provider="StubSearch",
        source_records=[_source("src_c", url)],
        citation_records=[_citation("qryc", "src_c", url)],
        claim_entries=[],
    )
    records[0]["claims"] = [
        {
            "claim_text": "Die Foerderung betraegt 30 Prozent der Kosten. " * 8,
            "evidence_snippet": "Belegausschnitt mit der Zahl 30 Prozent.",
            "citation_set": [{"url": url}],
            "status": "verified",
        }
    ]
    records[0]["source_passages"] = [
        {"passage_id": "p1", "origin": "source_snippet", "text": "Eine lange Passage aus der Quelle."}
    ]
    full = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=2200,
        query_synthesis=_synthesis("qryc", "compact query", "Summary."),
    )
    compact = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=900,
        query_synthesis=_synthesis("qryc", "compact query", "Summary."),
        record_view="compact",
    )
    # Page text and the provider text a statement was taken from stay
    # apart, and neither is called evidence.
    assert "Text the search provider returned from this source:" in full.markdown
    assert "Eine lange Passage" in full.markdown
    assert "Search-provider text these statements were taken from:" in full.markdown
    assert "Evidence excerpts" not in full.markdown
    assert "Text the search provider returned" not in compact.markdown
    assert "Eine lange Passage" not in compact.markdown
    assert "Statements the search provider attributes to this source:" in compact.markdown
    assert "Die Foerderung betraegt 30 Prozent" in compact.markdown
    # Claim lines are bounded in the compact view (breadth, not depth).
    claim_line = next(l for l in compact.markdown.splitlines() if "Die Foerderung betraegt" in l)
    assert len(claim_line) <= COMPACT_CLAIM_LINE_CHARS + 8
    assert "Tier: primary" in compact.markdown
    assert compact.rendered_record_count == 1 and compact.omitted_record_count == 0
    with pytest.raises(ValueError):
        render_evidence_ledger_overview(records, max_total_chars=1000, max_record_chars=400, record_view="tiny")


def test_compact_view_gives_a_claimless_source_one_bounded_context_line():
    """A source without extracted claims is not a bare header in the
    evaluator's view: one bounded line of its snippet (or first passage)
    says what it contains, so the evaluator can judge it."""
    url = "https://example.com/claimless"
    records = assemble_evidence_records(
        query_id="qryd",
        query="claimless query",
        provider="StubSearch",
        source_records=[_source("src_d", url)],
        citation_records=[_citation("qryd", "src_d", url)],
        claim_entries=[],
    )
    records[0]["claims"] = []
    records[0]["source_snippet"] = " ".join(
        f"Satz {index} der Behoerde ueber die Frist bis Ende 2026." for index in range(20)
    )
    compact = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=900,
        query_synthesis=_synthesis("qryd", "claimless query", "Summary."),
        record_view="compact",
    )
    lines = [l for l in compact.markdown.splitlines() if "Satz 0 der Behoerde" in l]
    assert len(lines) == 1, compact.markdown
    assert len(lines[0]) <= COMPACT_CLAIM_LINE_CHARS + 8
    assert "Satz 19 der Behoerde" not in compact.markdown, "bounded, not the whole snippet"


BASE_RATE = "Die Grundförderung beträgt 30 Prozent der Kosten."


def _statement(url: str, text: str, *, snippet: str = "") -> dict:
    """A statement the search node extracted for ``url``, not yet bound."""
    return {
        "raw_claim_id": make_record_id("raw_claim", url, text),
        "claim_text": text,
        "claim_type": "fact",
        "polarity": "affirmed",
        "needs_primary": False,
        "source_urls": [url],
        "signature": text.lower(),
        "evidence_snippet": snippet,
        "published_date": "2026-05-01",
    }


def _source_cited_at(
    query_id: str, url: str, places: int, statements: dict[int, list[dict]] | None = None
) -> list[dict]:
    """One source a search answer cites at ``places`` places, as the Azure
    adapter records it: one citation per place, each with the source's
    title, date and snippet. ``statements`` binds statements to places (the
    search node binds all of a URL's statements to one place)."""
    source_id = "src_" + url.rsplit("/", 1)[-1]
    first = _citation(query_id, source_id, url)
    citations = [
        {
            **first,
            "citation_id": make_record_id(
                "cit", query_id, 1, url, "search_results", 20 * place, 20 * place + 12
            ),
        }
        for place in range(places)
    ]
    evidence_ids = [evidence_id_for_citation(query_id, citation) for citation in citations]
    return assemble_evidence_records(
        query_id=query_id,
        query=f"query {query_id}",
        provider="StubSearch",
        source_records=[_source(source_id, url)],
        citation_records=citations,
        claim_entries=[
            {**statement, "source_ids": [source_id], "evidence_ids": [evidence_ids[place]]}
            for place, bound in (statements or {}).items()
            for statement in bound
        ],
    )


def _headers(markdown: str, label: str) -> list[str]:
    return [line for line in markdown.splitlines() if line.startswith(f"[{label}] ")]


def test_a_source_cited_at_two_places_is_one_block_with_the_statements_of_both():
    """A search answer that cites one source at two places yields one
    EvidenceRecord per place; the writer reads the source once: one
    header, the statements of both places under it."""
    url = "https://example.com/kfw"
    records = _source_cited_at(
        "qrytwo", url, 2,
        {0: [_statement(url, BASE_RATE)], 1: [_statement(url, "Die Kostengrenze sinkt auf 28.000 Euro.")]},
    )
    overview = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=2000,
        query_synthesis=_synthesis("qrytwo", "query qrytwo", "Summary."),
    )
    assert len(_headers(overview.markdown, "E1")) == 1, overview.markdown
    assert overview.markdown.count("Standing:") == 1
    assert f"  - {BASE_RATE}" in overview.markdown
    assert "  - Die Kostengrenze sinkt auf 28.000 Euro." in overview.markdown
    assert "Content: attributed to this source" not in overview.markdown
    assert overview.rendered_record_count == 2 and overview.omitted_record_count == 0
    assert sorted(overview.rendered_evidence_ids) == sorted(r["evidence_id"] for r in records)
    assert overview.label_urls == {"E1": url}


def test_the_places_a_source_is_cited_at_cost_the_budget_one_block():
    """The budget pays for a source once, not per place it is cited at: a
    budget that holds the source's block holds all its places. Before,
    each further place was a block of its own (label, title, standing and
    the generic line again) and fell off as budget tail."""
    url = "https://example.com/kfw"
    records = _source_cited_at("qryfit", url, 3, {2: [_statement(url, BASE_RATE)]})
    synthesis = _synthesis("qryfit", "query qryfit", "Summary.")
    alone = render_evidence_ledger_overview(
        [record for record in records if record["claims"]],
        max_total_chars=10**9, max_record_chars=2000, query_synthesis=synthesis,
    )
    overview = render_evidence_ledger_overview(
        records, max_total_chars=len(alone.markdown), max_record_chars=2000,
        query_synthesis=synthesis,
    )
    assert overview.omitted_record_count == 0, overview.omitted_records
    assert overview.rendered_record_count == 3
    assert overview.markdown == alone.markdown
    assert "NOTE:" not in overview.markdown


PINNED_FULL = (
    "RESEARCH RESULT R1\n"
    "Search query: query qryone\n"
    "Search answer (context; not independently verified):\n"
    "Summary.\n"
    "\n"
    "Sources from this search:\n"
    "\n"
    "[E1] Example report\n"
    "  Date: 2026-05-01 | Tier: primary | Standing: insufficiently supported\n"
    "  Statements the search provider attributes to this source:\n"
    "  - Die Grundförderung beträgt 30 Prozent der Kosten.\n"
    "  Text the search provider returned from this source:\n"
    "  - Meta expects capital expenditures of 115-135 USD billion.\n"
    "  Search-provider text these statements were taken from:\n"
    "  - Grundförderung: 30 Prozent.\n"
    "\n"
    "[E2] Example report\n"
    "  Date: 2026-05-01 | Tier: primary | Standing: background only; no extracted statement\n"
    "  Content: attributed to this source in the search answer above (no separate per-source excerpt available)."
)
PINNED_COMPACT = (
    "RESEARCH RESULT R1\n"
    "Search query: query qryone\n"
    "Search answer (context; not independently verified):\n"
    "Summary.\n"
    "\n"
    "Sources from this search:\n"
    "\n"
    "[E1] Example report\n"
    "  Date: 2026-05-01 | Tier: primary | Standing: insufficiently supported\n"
    "  Statements the search provider attributes to this source:\n"
    "  - Die Grundförderung beträgt 30 Prozent der Kosten.\n"
    "\n"
    "[E2] Example report\n"
    "  Date: 2026-05-01 | Tier: primary | Standing: background only; no extracted statement\n"
    "  Text the search provider returned from this source:\n"
    "  - Meta expects capital expenditures of 115-135 USD billion."
)


def test_sources_cited_at_one_place_render_as_before():
    """A source cited at one place renders exactly as before the places of
    a source were bundled (pinned, full and compact view)."""
    stated = "https://example.com/stated"
    bare = "https://example.com/bare"
    records = [
        *_source_cited_at(
            "qryone", stated, 1, {0: [_statement(stated, BASE_RATE, snippet="Grundförderung: 30 Prozent.")]}
        ),
        *_source_cited_at("qryone", bare, 1),
    ]
    synthesis = _synthesis("qryone", "query qryone", "Summary.")
    full = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=2000, query_synthesis=synthesis,
    )
    compact = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=900, query_synthesis=synthesis,
        record_view="compact",
    )
    assert full.markdown == PINNED_FULL
    assert compact.markdown == PINNED_COMPACT


def test_an_omitted_source_names_every_place_it_was_cited_at():
    """A source that does not fit falls whole, and the cut names every
    place it was cited at: the trace per record stays complete."""
    stated = "https://example.com/stated"
    cited = "https://example.com/cited"
    stated_records = _source_cited_at("qrycut", stated, 1, {0: [_statement(stated, BASE_RATE)]})
    cited_records = _source_cited_at("qrycut", cited, 3)
    synthesis = _synthesis("qrycut", "query qrycut", "Summary.")
    alone = render_evidence_ledger_overview(
        stated_records, max_total_chars=10**9, max_record_chars=2000, query_synthesis=synthesis,
    )
    overview = render_evidence_ledger_overview(
        stated_records + cited_records, max_total_chars=len(alone.markdown),
        max_record_chars=2000, query_synthesis=synthesis,
    )
    assert overview.rendered_evidence_ids == [stated_records[0]["evidence_id"]]
    assert sorted(dropped["evidence_id"] for dropped in overview.omitted_records) == sorted(
        record["evidence_id"] for record in cited_records
    )
    assert {(dropped["url"], dropped["reason"]) for dropped in overview.omitted_records} == {
        (cited, "budget_tail")
    }
    assert overview.omitted_by_query == [
        {"query_id": "qrycut", "query": "query qrycut", "omitted": 3, "rendered": 1}
    ]


def test_the_compact_view_shows_a_source_cited_at_two_places_once():
    url = "https://example.com/kfw"
    records = _source_cited_at("qrycmp", url, 2, {1: [_statement(url, BASE_RATE)]})
    compact = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=900,
        query_synthesis=_synthesis("qrycmp", "query qrycmp", "Summary."),
        record_view="compact",
    )
    assert len(_headers(compact.markdown, "E1")) == 1, compact.markdown
    assert f"  - {BASE_RATE}" in compact.markdown
    assert "Content: attributed to this source" not in compact.markdown
    assert compact.rendered_record_count == 2


def test_a_sources_standing_reads_the_statements_of_every_place_it_is_cited_at():
    """The header's standing is the source's standing in this search, read
    from the statements of all its places -- as when a search had one
    record per source -- not from the best place alone."""
    url = "https://example.com/kfw"
    records = _source_cited_at(
        "qrystd", url, 2,
        {0: [_statement(url, BASE_RATE)], 1: [_statement(url, "Der Bonus beträgt 16 Prozent.")]},
    )
    records[0]["claims"][0].update(
        verification_status="verified", verification_basis="primary_source", checked_here=True
    )
    records[1]["claims"][0].update(
        verification_status="contested", verification_basis="contested", checked_here=False
    )
    overview = render_evidence_ledger_overview(
        records, max_total_chars=20000, max_record_chars=2000,
        query_synthesis=_synthesis("qrystd", "query qrystd", "Summary."),
    )
    standings = [line for line in overview.markdown.splitlines() if "Standing:" in line]
    assert len(standings) == 1, overview.markdown
    assert standings[0].endswith(f"Standing: {STANDING_TEXT['contested']}")
    assert f"Statements checked against this source's text:\n  - {BASE_RATE}" in overview.markdown


def test_the_budget_counts_sources_not_the_places_they_are_cited_at():
    """Round robin hands out sources: a search whose first source is cited
    at three places gets its second source in pass two like any other
    search, in rank order -- not after two passes spent on places."""
    first, second = "https://example.com/a-first", "https://example.com/a-second"
    a_records = [
        *_source_cited_at("qrya", first, 3, {2: [_statement(first, BASE_RATE)]}),
        *_source_cited_at("qrya", second, 1),
    ]
    b_records = [
        *_source_cited_at("qryb", "https://example.com/b-first", 1),
        *_source_cited_at("qryb", "https://example.com/b-second", 1),
    ]
    synthesis = {
        **_synthesis("qrya", "query qrya", "Summary."),
        **_synthesis("qryb", "query qryb", "Summary."),
    }

    def chars(records: list[dict], query_id: str) -> int:
        return len(
            render_evidence_ledger_overview(
                records, max_total_chars=10**9, max_record_chars=2000,
                query_synthesis={query_id: synthesis[query_id]},
            ).markdown
        )

    budget = chars(a_records, "qrya") + 2 + chars(b_records[:1], "qryb")
    overview = render_evidence_ledger_overview(
        a_records + b_records, max_total_chars=budget, max_record_chars=2000,
        query_synthesis=synthesis,
    )
    assert a_records[-1]["evidence_id"] in overview.rendered_evidence_ids
    assert [dropped["evidence_id"] for dropped in overview.omitted_records] == [
        b_records[1]["evidence_id"]
    ]


def test_evidence_cut_words_is_the_one_reader_phrase():
    assert evidence_cut_words(1, 1, language="de") == "1 belegfähige Quelle aus einer Suche"
    assert evidence_cut_words(21, 3, language="de") == "21 belegfähige Quellen aus 3 Suchen"
    assert evidence_cut_words(4, 0, language="de") == "4 belegfähige Quellen"
    assert evidence_cut_words(1, 1, language="en") == "1 citable source from one search"
    assert evidence_cut_words(21, 3, language="en") == "21 citable sources from 3 searches"
    assert evidence_cut_words(4, 0, language=None) == "4 citable sources"


def test_derived_claim_ledger_carries_the_records_pinned_tier():
    """The derived ledger entry names the pinned tier of every URL a claim
    binds to — with a citation set (the citation's URL) and without one
    (the record's canonical URL) — so the consolidator judges a primary
    basis from the ledger, never from a site list."""
    from inqtrix.evidence import derive_claim_ledger_from_evidence

    cited = "https://www.kfw.de/cited"
    bare = "https://www.bafa.de/bare"
    records = [
        {
            "evidence_id": "ev_1", "record_type": "source", "report_eligible": True,
            "query_id": "q1", "source_id": "src_1", "citation_id": "cit_1",
            "canonical_url": cited, "tier": "primary", "tier_reason": "model:official_primary",
            "claims": [{"raw_claim_id": "raw_1", "claim_text": "Die KfW foerdert Waermepumpen mit 30 Prozent.", "signature": "s1",
                        "citation_set": [{"url": cited, "source_id": "src_1", "citation_id": "cit_1"}]}],
        },
        {
            "evidence_id": "ev_2", "record_type": "source", "report_eligible": True,
            "query_id": "q1", "source_id": "src_2", "citation_id": "cit_2",
            "canonical_url": bare, "tier": "mainstream", "tier_reason": "model:reporting",
            "claims": [{"raw_claim_id": "raw_2", "claim_text": "Das BAFA nennt eine Frist bis Dezember.", "signature": "s2"}],
        },
    ]
    by_id = {entry["raw_claim_id"]: entry for entry in derive_claim_ledger_from_evidence(records)}
    assert by_id["raw_1"]["source_tiers"] == {cited: "primary"}
    assert by_id["raw_2"]["source_tiers"] == {bare: "mainstream"}
    assert by_id["raw_1"]["source_tier_reasons"] == {cited: "model:official_primary"}
    assert by_id["raw_2"]["source_tier_reasons"] == {bare: "model:reporting"}


@pytest.mark.parametrize("section_key", ["risks", "risiken", "Open questions"])
def test_select_section_evidence_records_keys_the_risk_heuristic_on_the_section_identity(section_key):
    """The heuristic reads the section key (language-independent) or a
    custom plan's literal heading; German heading words do not decide,
    so an English run ranks contested records into its risks
    section too."""
    verified = _claim_record("qry_v", "https://example.com/verified", "Verified fact.", status="verified")
    contested = _claim_record("qry_x", "https://example.com/contested", "Contested claim.", status="contested")
    label_by_id = {str(verified["evidence_id"]): "E1", str(contested["evidence_id"]): "E2"}
    selected = select_section_evidence_records(
        [verified, contested], section_key=section_key, required_aspects=[],
        used_labels=set(), label_by_evidence_id=label_by_id, max_records=1,
    )
    assert selected and selected[0]["evidence_id"] == contested["evidence_id"]


def test_select_section_evidence_records_treats_closing_and_background_sections_by_key():
    verified = _claim_record("qry_v", "https://example.com/verified", "Verified fact.", status="verified")
    contested = _claim_record("qry_x", "https://example.com/contested", "Contested claim.", status="contested")
    label_by_id = {str(verified["evidence_id"]): "E1", str(contested["evidence_id"]): "E2"}
    closing = select_section_evidence_records(
        [contested, verified], section_key="context_outlook", required_aspects=[],
        used_labels=set(), label_by_evidence_id=label_by_id, max_records=1,
    )
    assert closing[0]["evidence_id"] == verified["evidence_id"]

    sparse = _claim_record("qry_a", "https://example.com/sparse", "Fact A.", status="verified")
    dense = _claim_record("qry_b", "https://example.com/dense", "Fact B.", status="verified")
    dense["source_passages"] = [{"text": f"Passage {index}."} for index in range(6)]
    labels = {str(sparse["evidence_id"]): "E1", str(dense["evidence_id"]): "E2"}
    background = select_section_evidence_records(
        [sparse, dense], section_key="background", required_aspects=[],
        used_labels=set(), label_by_evidence_id=labels, max_records=1,
    )
    assert background[0]["evidence_id"] == dense["evidence_id"]
