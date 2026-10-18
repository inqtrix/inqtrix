"""Agent instant-evidence enrichment and synthesis digest contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from inqtrix.agents.evidence import (
    enrich_instant_evidence,
    evidence_digest,
    merge_evidence,
    overlapping_claim_pairs,
    run_contradiction_analysis,
)
from inqtrix.agents.scheduler import TaskOutcome
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


@pytest.mark.parametrize("other", ["The limit is 5 MW.", "  The limit\tis 5 MW.\n"])
def test_identical_claims_do_not_invoke_the_contradiction_model(other):
    claims = [{"text": "The limit is 5 MW.", "task_id": "a"}, {"text": other, "task_id": "b"}]
    before = [dict(claim) for claim in claims]
    pairs = overlapping_claim_pairs(claims, signature=lambda text: "limit")
    assert pairs == []
    assert run_contradiction_analysis(
        None, pairs=pairs, model=None, reasoning_effort=None, timeout=1,
    ) is None
    assert claims == before


@pytest.mark.parametrize("other", [
    "The limit is not 5 MW.", "The limit is 6 MW.", "The limit is 5 mW.",
])
def test_distinct_claims_keep_negation_numbers_and_case(other):
    first, second = {"text": "The limit is 5 MW."}, {"text": other}
    assert overlapping_claim_pairs([first, second], signature=lambda text: "limit") == [(first, second)]


def test_identical_pairs_do_not_consume_the_limit_or_remove_claims():
    claims = [{"text": text} for text in ("Alpha is true.", "Alpha is true.", "Alpha is false.", "Gamma is true.")]
    assert overlapping_claim_pairs(claims, signature=lambda text: "shared", limit=1) == [(claims[1], claims[2])]
    assert len(claims) == 4
    assert overlapping_claim_pairs([{"text": "  \n\t"}, claims[0]], signature=lambda text: "shared") == []


def test_grounded_answer_associates_each_inline_url_with_its_statement() -> None:
    answer = (
        "Gartner beziffert die weltweiten KI-Ausgaben 2025 auf US-$1.5T "
        "([Gartner](https://example.com/gartner)).\n\n"
        "Statista grenzt den GenAI-Umsatz auf $244 Mrd. ein "
        "([Statista](https://example.com/statista))."
    )
    refs = enrich_instant_evidence(
        answer,
        [
            {
                "url": "https://example.com/gartner",
                "title": "Gartner",
                "excerpt": "",
            },
            {
                "url": "https://example.com/statista",
                "title": "Statista",
                "excerpt": "",
            },
        ],
    )

    assert "KI-Ausgaben 2025" in refs[0]["grounded_support"]
    assert "GenAI-Umsatz" not in refs[0]["grounded_support"]
    assert "GenAI-Umsatz" in refs[1]["grounded_support"]
    assert "KI-Ausgaben 2025" not in refs[1]["grounded_support"]
    assert refs[0]["grounded_support"].count("https://") == 0
    assert "US-$1.5T" in refs[0]["grounded_support"]


def test_empty_provider_snippet_still_yields_a_factual_digest() -> None:
    answer = (
        "Der Umsatz stieg 2025 um 29 Prozent "
        "([Marktbericht](https://example.com/report))."
    )
    enriched = enrich_instant_evidence(
        answer,
        [
            {
                "label": "W1",
                "url": "https://example.com/report",
                "title": "Marktbericht",
                "excerpt": "",
            }
        ],
    )

    digest = _register_lines(evidence_digest(enriched))
    assert digest[0].startswith("[W1]")
    assert "Umsatz stieg 2025 um 29 Prozent" in digest[0]
    assert digest != ["[W1] Marktbericht"]


def test_support_is_bounded_and_never_invented_for_an_uncited_url() -> None:
    cited_url = "https://example.com/cited"
    refs = enrich_instant_evidence(
        "A" * 500 + f" ([Quelle]({cited_url})).",
        [
            {"url": cited_url, "title": "Cited"},
            {"url": "https://example.com/uncited", "title": "Uncited"},
        ],
        support_chars=80,
    )

    assert 0 < len(refs[0]["grounded_support"]) <= 80
    assert "grounded_support" not in refs[1]


def test_a_support_window_never_opens_inside_a_number() -> None:
    """The window start falls inside ``1 500 000``; the support opens on
    the next whole word instead of on a partial number a writer could
    quote."""
    url = "https://example.com/q"
    answer = (
        "Nach Angaben des Amtes wurden bis Ende Juni insgesamt 1 500 000 "
        "EUR an Hilfen ausgezahlt, so der aktuelle Bericht "
        f"([Quelle]({url})), und weitere Mittel sind für das kommende Jahr "
        "fest zugesagt worden."
    )
    (ref,) = enrich_instant_evidence(answer, [{"url": url}], support_chars=80)
    assert ref["grounded_support"] == (
        "…EUR an Hilfen ausgezahlt, so der aktuelle Bericht (Quelle), und "
        "weitere Mittel…"
    )


def test_a_support_window_never_ends_inside_a_number() -> None:
    """The window's end would fall inside ``1 500 000``; the support keeps
    the whole number and is cut at a later word, visibly."""
    url = "https://example.com/q"
    answer = (
        f"Laut Bericht ([Quelle]({url})) stiegen die Ausgaben auf 1 500 000 "
        "EUR, und weitere Mittel sind für das kommende Jahr fest zugesagt."
    )
    (ref,) = enrich_instant_evidence(answer, [{"url": url}], support_chars=80)
    assert ref["grounded_support"] == (
        "Laut Bericht (Quelle) stiegen die Ausgaben auf 1 500 000 EUR, und "
        "weitere…"
    )


def test_first_paragraph_keeps_its_first_character_and_urls_match_exactly() -> None:
    answer = (
        "Gartner nennt einen Wert "
        "([Lang](https://example.com/report-extended))."
    )
    refs = enrich_instant_evidence(
        answer,
        [
            {"url": "https://example.com/report", "title": "Short"},
            {
                "url": "https://example.com/report-extended",
                "title": "Long",
            },
        ],
    )

    assert "grounded_support" not in refs[0]
    assert refs[1]["grounded_support"].startswith("Gartner")


def test_web_identity_uses_the_platform_url_normalizer() -> None:
    references, _claims = merge_evidence(
        {
            "a": TaskOutcome(
                status="completed",
                evidence=[
                    {"url": "https://example.com/report?utm_source=one"}
                ],
            ),
            "b": TaskOutcome(
                status="completed",
                evidence=[
                    {"url": "https://example.com/report?utm_source=two"}
                ],
            ),
        }
    )

    assert len(references) == 1
    assert references[0]["tasks"] == ["a", "b"]


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

    scoped = evidence_digest(
        [
            {"label": "W1", "excerpt": "one"},
            {"label": "W2", "excerpt": "two"},
        ],
        labels=["W2"],
    )
    assert _register_lines(scoped) == ["[W2] two"]


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


def test_a_passage_of_two_hundred_characters_stays_whole() -> None:
    passage = "Frist " * 33 + "14"
    assert len(passage) == 200
    assert _register_lines(
        evidence_digest([{"label": "K1", "excerpt": passage}])
    ) == [f"[K1] {passage}"]


def test_duplicate_url_fills_missing_stronger_evidence_without_overwrite() -> None:
    references, _claims = merge_evidence(
        {
            "a": TaskOutcome(
                status="completed",
                evidence=[
                    {
                        "url": "https://example.com/report",
                        "title": "First title",
                        "grounded_support": "Provider answer support",
                    }
                ],
            ),
            "b": TaskOutcome(
                status="completed",
                evidence=[
                    {
                        "url": "https://example.com/report/",
                        "title": "Later title",
                        "excerpt": "Verbatim provider excerpt",
                    }
                ],
            ),
            "c": TaskOutcome(
                status="completed",
                evidence=[
                    {
                        "url": "https://example.com/report",
                        "excerpt": "Must not replace the first excerpt",
                    }
                ],
            ),
        }
    )

    assert len(references) == 1
    assert references[0]["label"] == "W1"
    assert references[0]["tasks"] == ["a", "b", "c"]
    assert references[0]["title"] == "First title"
    assert references[0]["grounded_support"] == "Provider answer support"
    assert references[0]["excerpt"] == "Verbatim provider excerpt"


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
