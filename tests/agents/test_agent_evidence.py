"""Agent evidence merge, register digest and kernel registration contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.agents.evidence import (
    dedup_key,
    evidence_digest,
    merge_missing_evidence_fields,
)
from inqtrix.untrusted_content import UNTRUSTED_FENCE_CLOSE


def _register_lines(digest: str) -> list[str]:
    """The register entries of *digest*, which a writer reads as data.

    The register is fenced as a whole: the first line opens the fence,
    the last closes it, and every entry stands between them.
    """
    lines = digest.splitlines()
    assert lines[0] == '<untrusted_content source="evidence-register">'
    assert lines[-1] == UNTRUSTED_FENCE_CLOSE
    return lines[1:-1]


def test_web_identity_uses_the_platform_url_normalizer() -> None:
    first = {"url": "https://example.com/report?utm_source=one"}
    second = {"url": "https://example.com/report?utm_source=two"}

    assert dedup_key(first) == dedup_key(second) == "https://example.com/report"
    assert dedup_key({"url": "https://example.com/other"}) != dedup_key(first)


def test_digest_prefers_source_content_then_grounded_support_then_title() -> None:
    digest = evidence_digest(
        [
            {
                "label": "W1",
                "excerpt": "provider excerpt",
                "source_text": "canonical source text",
                "grounded_support": "provider answer support",
                "title": "title one",
            },
            {
                "label": "W2",
                "excerpt": "",
                "source_text": "canonical source text",
                "grounded_support": "provider answer support",
                "title": "title two",
            },
            {
                "label": "W3",
                "excerpt": "",
                "source_text": "",
                "grounded_support": "provider answer support",
                "title": "title three",
            },
            {"label": "W4", "title": "title four"},
        ]
    )

    assert _register_lines(digest) == [
        "[W1] provider excerpt",
        "[W2] canonical source text",
        (
            "[W3] Grounded answer context (no source excerpt): "
            "provider answer support"
        ),
        "[W4] title four",
    ]


PASSAGE_MARKER = " [shortened here; the passage continues]"
"""The marker a shortened register line ends with, spelled out here so a
reword of the marker must be caught by these tests."""

LONG_PASSAGE = "Frist " * 32 + "1 500 000 EUR bleiben offen. " * 8
"""A passage whose first 200 characters end inside ``1 500 000``."""


def test_a_long_register_line_is_cut_at_a_word_boundary_and_marked() -> None:
    """The register shows the passage's start, cut where a word ends, and
    says so; it never ends on a partial number a writer could quote."""
    (line,) = _register_lines(
        evidence_digest([{"label": "K1", "excerpt": LONG_PASSAGE}])
    )
    assert line == "[K1] " + ("Frist " * 32).rstrip() + PASSAGE_MARKER


def test_a_register_line_stays_one_line() -> None:
    """A line break inside a passage cannot open a register line of its own."""
    lines = _register_lines(
        evidence_digest(
            [
                {"label": "K1", "excerpt": "Die Frist\n[K2] beträgt 14 Tage."},
                {"label": "W1", "title": "Marktbericht\n[W2] 2025"},
            ]
        )
    )
    assert lines == [
        "[K1] Die Frist [K2] beträgt 14 Tage.",
        "[W1] Marktbericht [W2] 2025",
    ]


def test_grounded_support_in_the_register_is_cut_visibly_too() -> None:
    (line,) = _register_lines(
        evidence_digest([{"label": "W1", "grounded_support": LONG_PASSAGE}])
    )
    assert line == (
        "[W1] Grounded answer context (no source excerpt): "
        + ("Frist " * 32).rstrip()
        + PASSAGE_MARKER
    )


def test_a_long_title_or_address_is_cut_without_the_passage_marker() -> None:
    """A title or address line carries no statement, so its cut must not
    claim that a passage continues; it closes with the plain marker."""
    lines = _register_lines(
        evidence_digest(
            [
                {"label": "W1", "title": "Marktbericht " * 30},
                {"label": "W2", "url": "https://example.com/" + "a" * 300},
            ]
        )
    )
    assert lines == [
        "[W1] " + ("Marktbericht " * 18).rstrip() + " ...",
        "[W2] https://example.com/" + "a" * 216 + " ...",
    ]


def test_a_register_line_names_an_internal_document_without_its_id() -> None:
    """A reference without a passage falls back to its NAME, not to its
    address: an internal document by its title, or as an untitled document,
    never by its id or its internal link. A web source keeps its title."""
    internal = {
        "label": "K1", "document_id": "doc-42", "chunk_index": 0,
        "url": "inqtrix://documents/doc-42#chunk-0",
        "title": "Interner Marktbericht", "excerpt": "Die Lage ist stabil.",
    }
    untitled = {
        "label": "K2", "document_id": "doc-99", "chunk_index": 0,
        "url": "inqtrix://documents/doc-99#chunk-0",
    }
    web = {"label": "W1", "url": "https://example.org/a", "title": "Marktbericht"}

    register = evidence_digest([internal, untitled, web])
    assert "[K2] untitled document" in register
    assert "doc-99" not in register and "inqtrix://" not in register
    assert "[W1] Marktbericht" in register


def test_a_passage_of_two_hundred_characters_stays_whole() -> None:
    passage = "Frist " * 33 + "14"
    assert len(passage) == 200
    assert _register_lines(
        evidence_digest([{"label": "K1", "excerpt": passage}])
    ) == [f"[K1] {passage}"]


def test_duplicate_url_fills_missing_stronger_evidence_without_overwrite() -> None:
    canonical = {
        "label": "W1",
        "url": "https://example.com/report",
        "title": "First title",
        "grounded_support": "Provider answer support",
        "excerpt": "",
    }
    later = {
        "label": "W9",
        "url": "https://example.com/report/",
        "title": "Later title",
        "excerpt": "Verbatim provider excerpt",
    }
    last = {"url": "https://example.com/report", "excerpt": "Must not replace the first excerpt"}

    assert dedup_key(later) == dedup_key(canonical)
    assert merge_missing_evidence_fields(canonical, later) is True
    assert merge_missing_evidence_fields(canonical, last) is False

    assert canonical["label"] == "W1"
    assert canonical["title"] == "First title"
    assert canonical["grounded_support"] == "Provider answer support"
    assert canonical["excerpt"] == "Verbatim provider excerpt"


def test_kernel_ledger_assigns_stable_labels_across_resume() -> None:
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps

    store = MemoryAgentControlStore()
    first = KernelDeps(
        run_id="run_kernel_evidence_resume",
        control=store,
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )

    registered = first.register_references(
        [
            {
                "url": "https://example.com/report?utm_source=first",
                "title": "Web report",
            },
            {
                "document_id": "doc-1",
                "chunk_index": 2,
                "title": "Internal report",
            },
        ]
    )

    assert [item["label"] for item in registered] == ["W1", "K1"]

    resumed = KernelDeps(
        run_id=first.run_id,
        control=store,
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    resumed.hydrate_evidence()
    after_resume = resumed.register_references(
        [
            {
                "url": "https://example.com/report?utm_source=second",
                "excerpt": "Later source excerpt",
            },
            {"url": "https://example.com/second", "title": "Second web"},
        ]
    )

    assert [item["label"] for item in after_resume] == ["W1", "W2"]
    assert after_resume[0]["excerpt"] == "Later source excerpt"
    assert sorted(
        item["label"] for item in resumed.evidence_refs.values()
    ) == ["K1", "W1", "W2"]


def test_instant_search_without_urls_keeps_provider_answer_as_evidence() -> None:
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps, run_coro

    store = MemoryAgentControlStore()
    deps = KernelDeps(
        run_id="run_kernel_provider_answer_only",
        control=store,
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    provider_answer = (
        "Frankreich gewann die Fußball-Weltmeisterschaft 2018 gegen "
        "Kroatien. " + ("Zusätzlicher Kontext. " * 60)
    )

    registered = deps.register_instant_web_search(
        SimpleNamespace(
            answer=provider_answer,
            completion_tokens=32,
            duration_ms=418,
            finished_at="2026-07-23T12:00:01Z",
            parameters={"max_sources": 8},
            prompt_tokens=64,
            provider="AzureFoundryWebSearch",
            query="Fußball-Weltmeisterschaft 2018 Sieger Finalgegner",
            query_id="query-answer-only",
            sources=[],
            started_at="2026-07-23T12:00:00Z",
        )
    )

    assert len(registered) == 1
    reference = registered[0]
    assert reference["label"] == "W1"
    assert reference["query_id"] == "query-answer-only"
    assert reference["evidence_kind"] == "provider_search_answer"
    assert reference.get("url") is None
    assert reference["grounded_support"].endswith("…")

    artifact, _ = run_coro(
        store.get_artifact(
            deps.run_id,
            f"art_{deps.run_id[-12:]}_evidence",
        )
    )
    search = artifact.payload["web_search_ledger"]["searches"][
        "query-answer-only"
    ]
    assert search["provider_answer"] == provider_answer
    assert search["citations"] == []
    assert artifact.refs[0]["reference_id"] == reference["reference_id"]


def test_a_provider_answer_without_urls_is_cut_like_every_grounded_support() -> None:
    """The kernel stores an answer without URL rows through the same cut
    as the mission: ``1 500 000`` straddles character 599, and the stored
    support ends before it instead of on ``1 50``."""
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps

    deps = KernelDeps(
        run_id="run_kernel_provider_answer_cut",
        control=MemoryAgentControlStore(),
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )
    (reference,) = deps.register_instant_web_search(
        SimpleNamespace(
            answer="Wort " * 119 + "1 500 000 EUR bleiben offen.",
            completion_tokens=1,
            duration_ms=1,
            finished_at="2026-07-23T12:00:01Z",
            parameters={},
            prompt_tokens=1,
            provider="AzureFoundryWebSearch",
            query="offene Mittel",
            query_id="query-answer-cut",
            sources=[],
            started_at="2026-07-23T12:00:00Z",
        )
    )
    assert reference["grounded_support"] == ("Wort " * 119).rstrip() + "…"


EURLEX = "https://eur-lex.europa.eu/eli/reg/2023/1542/oj"
COMMISSION = "https://environment.ec.europa.eu/news/battery-passport_en"
AGENCY = "https://www.bundesnetzagentur.example/ladeinfrastruktur"
FIRST_QUARTER = "https://www.sap.example/investors/q1-2025.pdf"
SECOND_QUARTER = "https://www.sap.example/investors/q2-2025.pdf"
PASSPORT = "Der Batteriepass ist ab dem 18. Februar 2027 vorgeschrieben."
UNCHANGED = "Eine spätere Verordnung hat den Stichtag nicht verschoben."

# Answers the way the search provider writes them: the link follows its
# statement on the same line, and the provider's span marks exactly the link.
CITED_PROSE = (
    f"{PASSPORT} ([eur-lex.europa.eu]({EURLEX}))\n\n"
    f"{UNCHANGED} ([environment.ec.europa.eu]({COMMISSION}))"
)
CITED_DATED_ITEM = (
    "- **1. September 2026:** 214.334 öffentliche Ladepunkte in Deutschland. "
    f"([bundesnetzagentur.example]({AGENCY}))"
)
CITED_TABLE = (
    "| Quartal | Umsatz | Quelle |\n"
    "|---|---:|---|\n"
    f"| Q1 2025 | 9.013 Mio. € | ([sap.example]({FIRST_QUARTER})) |\n"
    f"| Q2 2025 | 9.027 Mio. € | ([sap.example]({SECOND_QUARTER})) |"
)


def _cited_sources(answer: str, urls: list[str]) -> list[dict]:
    """The sources of *answer* with the span of every link that cites them."""
    import re

    return [
        {
            "url": url,
            "title": "",
            "rank": rank,
            "origin": "url_citation",
            "spans": [
                {"start": match.start(), "end": match.end()}
                for match in re.finditer(r"\(\[[^\]]+\]\(" + re.escape(url) + r"\)\)", answer)
            ],
        }
        for rank, url in enumerate(urls, start=1)
    ]


def _instant_search(answer: str, urls: list[str], *, query_id: str = "query-cited-line") -> SimpleNamespace:
    return SimpleNamespace(
        answer=answer,
        completion_tokens=1,
        duration_ms=1,
        finished_at="2026-07-23T12:00:01Z",
        parameters={},
        prompt_tokens=1,
        provider="AzureFoundryWebSearch",
        query="Ab wann gilt der Batteriepass?",
        query_id=query_id,
        sources=_cited_sources(answer, urls),
        started_at="2026-07-23T12:00:00Z",
    )


def _kernel_deps(run_id: str):
    from inqtrix.agents.control_memory import MemoryAgentControlStore
    from inqtrix.agents.kernel.deps import KernelDeps

    return KernelDeps(
        run_id=run_id,
        control=MemoryAgentControlStore(),
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (CITED_PROSE, {EURLEX: PASSPORT, COMMISSION: UNCHANGED}),
        (CITED_DATED_ITEM, {AGENCY: "**1. September 2026:** 214.334 öffentliche Ladepunkte in Deutschland."}),
        (CITED_TABLE, {FIRST_QUARTER: "Q1 2025 | 9.013 Mio. €", SECOND_QUARTER: "Q2 2025 | 9.027 Mio. €"}),
    ],
    ids=["statement", "dated list item", "table row"],
)
def test_the_kernel_stores_the_line_that_cites_each_source_of_an_instant_search(
    answer: str, expected: dict[str, str],
) -> None:
    """The reference of a source says what the search said where it cites
    the source, the same text the ledger keeps at that citation, and the
    register a sufficiency judgement reads names it."""
    deps = _kernel_deps("run_kernel_cited_line")
    registered = deps.register_instant_web_search(_instant_search(answer, list(expected)))

    citations = deps.web_search_ledger["searches"]["query-cited-line"]["citations"]
    register = evidence_digest(registered)
    assert {item["url"]: item["grounded_support"] for item in registered} == expected
    assert {item["url"]: item["grounded_support"] for item in citations} == expected
    assert all(f"Grounded answer context (no source excerpt): {text}" in register for text in expected.values())


def test_a_source_an_earlier_search_cited_takes_the_line_of_the_search_that_registers_it() -> None:
    """The ledger of the run also holds the searches of a child, whose
    sources are not all references of the run: a later search that cites
    one of them speaks for itself, and it leads the pointers."""
    from inqtrix.evidence import build_instant_web_search_ledger

    deps = _kernel_deps("run_kernel_two_searches")
    earlier = f"Die Verordnung regelt das Recycling von Altbatterien. ([eur-lex.europa.eu]({EURLEX}))"
    deps.register_web_search_ledger(build_instant_web_search_ledger(
        run_id="run_child",
        query_id="query-earlier",
        query="Was regelt die Batterieverordnung?",
        provider="AzureFoundryWebSearch",
        answer=earlier,
        sources=_cited_sources(earlier, [EURLEX]),
    ))
    (reference,) = deps.register_instant_web_search(
        _instant_search(f"{PASSPORT} ([eur-lex.europa.eu]({EURLEX}))", [EURLEX], query_id="query-later")
    )
    assert (reference["grounded_support"], reference["query_id"], reference["query_ids"]) == (
        PASSPORT, "query-later", ["query-later", "query-earlier"],
    )


def test_a_source_cited_only_by_a_link_that_stands_alone_stays_provider_context_without_a_text() -> None:
    """No statement stands in the line of the link: the reference carries no
    text, and it is still what the provider wrote, not a stronger material."""
    from inqtrix.agents.evidence_context import project_evidence_context

    deps = _kernel_deps("run_kernel_link_alone")
    answer = f"| Jahr | Wert |\n|---|---|\n| 2025 | 3,1 % |\n\n([bundesnetzagentur.example]({AGENCY}))"
    (reference,) = deps.register_instant_web_search(_instant_search(answer, [AGENCY]))
    (source,) = project_evidence_context([reference], deps.web_search_ledger)["sources"]
    assert ("grounded_support" in reference, source["material_kind"]) == (False, "provider_context")


def test_a_canvas_update_adds_sources_instead_of_replacing_them():
    """A model that fact-checks one paragraph passes only the newly
    found source; replacing the list would DELETE every source the
    document already carried — silently, because the write itself
    succeeds. An update merges."""
    from inqtrix.agents.kernel.tools import _merged_refs

    existing = [
        {"reference_id": "ref_a", "label": "W1", "url": "https://a.example"},
        {"reference_id": "ref_b", "label": "W2", "url": "https://b.example"},
    ]
    added = [
        {"reference_id": "ref_c", "label": "W3", "url": "https://c.example"}
    ]
    merged = _merged_refs(existing, added)
    assert [ref["reference_id"] for ref in merged] == [
        "ref_a",
        "ref_b",
        "ref_c",
    ]


def test_the_same_source_twice_stays_one_entry_with_its_first_row():
    """Identity is the reference_id — the content hash of the citation.
    A re-read that produced a THINNER row must not overwrite the richer
    one the document already has, or a source silently loses its
    excerpt while keeping its label."""
    from inqtrix.agents.kernel.tools import _merged_refs

    rich = {
        "reference_id": "ref_a",
        "label": "W1",
        "url": "https://a.example",
        "excerpt": "Der belegende Satz.",
    }
    thin = {"reference_id": "ref_a", "label": "W1", "url": "https://a.example"}
    merged = _merged_refs([rich], [thin])
    assert len(merged) == 1
    assert merged[0]["excerpt"] == "Der belegende Satz."


def test_a_reference_without_an_id_is_never_silently_swallowed():
    """A row the ledger could not key must still reach the document:
    dropping it would remove a source the model explicitly attached."""
    from inqtrix.agents.kernel.tools import _merged_refs

    merged = _merged_refs([{"label": "W1"}], [{"label": "W2"}])
    assert len(merged) == 2


def _ledger_deps(store, run_id: str = "run_kernel_gathered"):
    from inqtrix.agents.kernel.deps import KernelDeps

    return KernelDeps(
        run_id=run_id,
        control=store,
        platform=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        model=None,
        reasoning_effort=None,
        timeout=1.0,
    )


def test_the_run_keeps_which_references_it_gathered_itself_across_a_park() -> None:
    """What a run searched or cited is its own; what it was handed is not.

    The ledger also holds the references of earlier turns and of documents
    the run read, so that their labels stay citable. Only what the run's own
    source tools registered counts as gathered, a reference an earlier turn
    left included once a tool finds it again, and the set comes back with
    the ledger after a park.
    """
    from inqtrix.agents.control_memory import MemoryAgentControlStore

    store = MemoryAgentControlStore()
    first = _ledger_deps(store)
    handed, = first.register_references(
        [{"url": "https://example.com/earlier", "title": "Earlier turn"}],
        gathered=False,
    )
    own, = first.register_references(
        [{"url": "https://example.com/own", "title": "This run"}]
    )
    assert first.gathered_reference_ids == {own["reference_id"]}

    resumed = _ledger_deps(store)
    resumed.hydrate_evidence()
    assert sorted(resumed.evidence_refs) == sorted(
        [handed["reference_id"], own["reference_id"]]
    )
    assert resumed.gathered_reference_ids == {own["reference_id"]}

    # A search of this run finds the earlier reference again: nothing about
    # the reference changes, and it is this run's source from now on.
    again, = resumed.register_references([{"url": "https://example.com/earlier"}])
    assert again["reference_id"] == handed["reference_id"]
    both = {own["reference_id"], handed["reference_id"]}
    assert resumed.gathered_reference_ids == both

    third = _ledger_deps(store)
    third.hydrate_evidence()
    assert third.gathered_reference_ids == both


def test_a_ledger_stored_before_the_marker_counts_all_of_it_as_gathered() -> None:
    """A run in flight across the change keeps the list it had before."""
    import asyncio

    from inqtrix.agents.control_memory import MemoryAgentControlStore

    store = MemoryAgentControlStore()
    run_id = "run_kernel_gathered_old"
    asyncio.run(
        store.upsert_artifact(
            run_id=run_id,
            kind="evidence_bundle",
            session_id=None,
            title="Kernel evidence",
            status="ready",
            content_markdown="",
            payload={"schema_version": 1, "web_search_ledger": {}},
            refs=[
                {"label": "W1", "url": "https://example.com/one"},
                {"label": "W2", "url": "https://example.com/two"},
            ],
            updated_by="agent",
            artifact_id=f"art_{run_id[-12:]}_evidence",
        )
    )
    deps = _ledger_deps(store, run_id)
    deps.hydrate_evidence()

    assert len(deps.evidence_refs) == 2
    assert deps.gathered_reference_ids == set(deps.evidence_refs)
