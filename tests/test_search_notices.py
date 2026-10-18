"""Search notices: one registry from search codes to their two wordings.

Every code a producer can emit has one English API message (route,
capability, kernel tool, discovery) and reader words in both languages
(mission answer, memo, review). Notices fold by code over a task's
searches, so counts add up; only a duplicate collapse is informative.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib
from dataclasses import replace

import pytest

from inqtrix.i18n import MESSAGES
from inqtrix.knowledge import retrieval_warnings
from inqtrix.knowledge.retrieval_warnings import (
    SearchNotice,
    collect_search_notices,
    notice_family,
    search_notice_lines,
    stored_search_notices,
    warning_message,
)

RUNTIME = pathlib.Path("src/inqtrix")


def _literals(callees: set[str], keyword: str, *, positional: bool = False) -> set[str]:
    """String constants passed as *keyword* (or as the first positional
    argument) to a call named in *callees*, anywhere in the runtime."""
    found: set[str] = set()
    for path in sorted(RUNTIME.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in callees:
                continue
            values = [item.value for item in node.keywords if item.arg == keyword]
            if positional and node.args:
                values.append(node.args[0])
            found.update(
                value.value
                for value in values
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            )
    return found


def test_every_emitted_code_has_reader_words():
    """A new exclusion reason, degradation reason or warning code without a
    registry entry fails here, before a reader meets a raw code."""
    exclusions = _literals({"RetrievalExclusion"}, "reason")
    degradations = _literals(
        {"RetrievalDegradation", "degraded_candidates", "_log_candidate_degradation"}, "reason"
    )
    codes = _literals({"KnowledgeSearchWarning", "warning_message"}, "code", positional=True)
    # The scan reads what it claims to read.
    assert {"source_unverified", "duplicate_document"} <= exclusions
    assert {"vector_overfetch_cap", "vector_candidate_stalled"} <= degradations
    assert {"knowledge.no_collections", "collections_filtered"} <= codes

    assert sorted(exclusions - set(retrieval_warnings._WARNING_CONTRACT)) == []
    assert sorted(code for code in degradations if notice_family(code) != "degradation") == []
    emitted = {
        *retrieval_warnings._WARNING_CONTRACT.values(),
        retrieval_warnings._UNKNOWN_EXCLUSION_CODE,
        *degradations,
        *codes,
    }
    assert sorted(code for code in emitted if notice_family(code) is None) == []

    suffixes = {"exclusion": ("_one", "_other"), "degradation": ("_one", "_other", "_complete"), "scope": ("",)}
    unworded = []
    for code, notice in retrieval_warnings._NOTICES.items():
        assert notice.message
        if code == "collections_filtered":
            # Route-only: the Agent capability refuses instead of narrowing.
            assert notice.words == ""
            continue
        for suffix in suffixes[notice.family]:
            entry = MESSAGES.get(notice.words + suffix) if notice.words else None
            if not entry or not entry.get("de") or not entry.get("en"):
                unworded.append(f"{code}: {notice.words}{suffix}")
    assert unworded == []


def test_counted_notices_use_the_plural_pair():
    """A count chooses its sentence: one exclusion in one search is not
    "1 Ausschlüsse in 1 Suchen"."""
    single, several = search_notice_lines(
        [
            SearchNotice(code="chunks_require_reindex", count=1, searches=1),
            SearchNotice(code="chunks_require_reindex", count=3, searches=2),
        ],
        "de",
    )
    assert single == (
        "Ein Treffer, dessen Originaltext sich nicht prüfen ließ, wurde "
        "ausgeschlossen (in einer Suche); solcher Inhalt muss neu indiziert werden."
    )
    assert several == (
        "Treffer, deren Originaltext sich nicht prüfen ließ, wurden ausgeschlossen "
        "(3 Ausschlüsse in 2 Suchen; derselbe Treffer kann mehrfach zählen); "
        "solche Inhalte müssen neu indiziert werden."
    )
    [limit] = search_notice_lines(
        [SearchNotice(code="vector_overfetch_cap", searches=1, incomplete=1, returned=0, requested=1)],
        "de",
    )
    assert "im knappsten Fall 0 von 1 angeforderten Treffer;" in limit


def _degradation(returned: int, requested: int, complete: bool) -> dict:
    return {
        "code": "vector_overfetch_cap", "count": 0, "final_evidence_complete": complete,
        "returned_hits": returned, "final_top_k": requested,
    }


def test_notice_aggregation_shapes():
    """Searches count per code, not per entry; the family comes from the
    registry, not from the fields a warning happens to carry."""
    notices = collect_search_notices([
        # Two entries of one code in one search: one search.
        [{"code": "chunks_require_reindex", "count": 2}, {"code": "chunks_require_reindex", "count": 1}],
        # The capability gives every warning final_evidence_complete=False;
        # for an exclusion it means nothing.
        [{"code": "chunks_require_reindex", "count": 3, "final_evidence_complete": False}],
        # A degradation counts nothing and still renders.
        [_degradation(4, 4, True)],
    ])
    assert [notice.as_dict() for notice in notices] == [
        {"code": "chunks_require_reindex", "count": 6, "searches": 2, "incomplete": 0, "returned": 0, "requested": 0},
        {"code": "vector_overfetch_cap", "count": 0, "searches": 1, "incomplete": 0, "returned": 0, "requested": 0},
    ]
    assert search_notice_lines(notices[1:], "en") == [
        "The knowledge search hit a technical candidate limit (in one search); the requested "
        "number of hits still came back, but the selection may differ from an unbounded search."
    ]
    # A complete final evidence count is still a limit: the selection may differ.
    assert notices[1].severity == "limit"

    [tight] = collect_search_notices([
        [_degradation(3, 8, False)], [_degradation(1, 4, False)], [_degradation(1, 8, False)],
        [_degradation(4, 4, True)],
    ])
    assert (tight.searches, tight.incomplete, tight.returned, tight.requested) == (4, 3, 1, 8)
    assert "(in 4 Suchen) und lieferte im knappsten Fall 1 von 8 angeforderten Treffern;" in (
        search_notice_lines([tight], "de")[0]
    )


def test_only_a_duplicate_collapse_is_informative():
    notices = collect_search_notices([[
        {"code": "duplicate_documents_collapsed", "count": 2},
        {"code": "chunks_pending_reconciliation", "count": 1},
        {"code": "future_code"},
    ]])
    assert [notice.severity for notice in notices] == ["info", "limit", "limit"]
    assert search_notice_lines(notices, "de", severity="info") == [
        "2 Treffer wurden übergangen (in einer Suche; derselbe Treffer kann "
        "mehrfach zählen), weil inhaltsgleiche Dokumente schon unter den "
        "gefundenen Quellen waren."
    ]
    # One skipped hit takes the singular, and it names the DOCUMENT the store
    # collapsed — not "the same passage", which the reader may never have got.
    single = collect_search_notices([[{"code": "duplicate_documents_collapsed", "count": 1}]])
    assert search_notice_lines(single, "de") == [
        "1 Treffer wurde übergangen (in einer Suche), weil ein inhaltsgleiches "
        "Dokument schon unter den gefundenen Quellen war."
    ]
    assert search_notice_lines(single, "en") == [
        "1 hit was skipped (in one search) because a content-identical document "
        "was already among the found sources."
    ]


def test_an_unknown_or_malformed_notice_stays_visible_and_logs_once(caplog):
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        notices = collect_search_notices([
            [{"code": "future_code", "count": 2}, "not a mapping", {"code": "chunks_require_reindex", "count": 0}],
            [{"code": "future_code", "count": 1}],
        ])
    assert [(notice.code, notice.searches) for notice in notices] == [("future_code", 2), ("", 1)]
    logged = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert len(logged) == 2
    assert "'future_code' has no reader words (entries=2, searches=2)" in logged[0]
    # The log names what the unreadable entries claimed to be; the notice
    # itself stays text-free.
    assert "unreadable (entries=2, searches=1, codes=['chunks_require_reindex'])" in logged[1]

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="inqtrix"):
        lines = search_notice_lines(notices, "de")
    assert lines == [MESSAGES["search_notice_unknown"]["de"], MESSAGES["search_notice_unreadable"]["de"]]
    # The code stays in the log; the renderer neither shows nor logs it.
    assert not any("future_code" in line for line in lines)
    assert caplog.records == []

    # A record written before the notices were typed holds free text: every
    # such entry folds into ONE general sentence, logged once on read-back.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        legacy = stored_search_notices(["an older free-text note", "and another"])
        stored = search_notice_lines(legacy, "en")
    assert [notice.code for notice in legacy] == [""]
    assert stored == [MESSAGES["search_notice_unreadable"]["en"]]
    assert [record.getMessage() for record in caplog.records] == [
        "Stored search notices unreadable (entries=2, readable=0); the reader "
        "sees one general sentence."
    ]


def test_an_unregistered_code_does_not_fail_the_search_it_came_from(caplog):
    """A code outside the registry is a contract break a test catches before
    it ships. At runtime the search that reported it must still answer: the
    API message is the general one, and the server logs the code."""
    from inqtrix.knowledge.retrieval_warnings import warning_message

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        message = warning_message("vector_future_limit", final_evidence_complete=False)
    assert "cannot classify" in message
    assert [record.getMessage() for record in caplog.records] == [
        "Knowledge search code 'vector_future_limit' is not registered; the "
        "reader gets the general message."
    ]
    # A registered degradation still needs to say which case it is.
    with pytest.raises(ValueError):
        warning_message("vector_overfetch_cap")


@pytest.mark.parametrize("complete", [True, False])
def test_degradation_message_is_one_text_for_capability_and_route(complete: bool):
    """The capability and the synchronous route read the one registry text;
    a second hand-written copy drifts (it did: "reranking" vs "final
    ranking")."""
    from inqtrix.capabilities import build_capability_registry
    from inqtrix.knowledge.stores.memory import MemoryKnowledgeStore
    from inqtrix.knowledge.stores.ports import RetrievalCandidateBatch, RetrievalDegradation
    from tests.test_capabilities import ANON, make_knowledge_service
    from tests.test_knowledge_routes import _create_collection_with_document, make_knowledge_client

    def degradation(returned: int) -> RetrievalDegradation:
        return RetrievalDegradation(
            reason="vector_overfetch_cap", retrieval_mode="dense", requested_top_k=1,
            returned_hits=returned, candidate_cap=64, requested_candidate_pool=8,
            returned_candidate_pool=returned, final_top_k=1,
        )

    service = make_knowledge_service()
    collection = asyncio.run(service.create_collection(name="Recht"))
    asyncio.run(service.add_document(collection_id=collection.id, title="Haftung", text="Die Haftung ist begrenzt."))
    original = service.search_reported

    async def degraded(**kwargs):
        outcome = await original(**kwargs)
        return replace(outcome, retrieval_degradations=[degradation(1 if complete else 0)])

    service.search_reported = degraded  # type: ignore[method-assign]
    capability = asyncio.run(build_capability_registry(knowledge_service=service).invoke(
        "knowledge.search", {"query": "Haftung", "top_k": 1}, ANON,
    )).warnings[0].message

    store = MemoryKnowledgeStore()
    client, _ = make_knowledge_client(store=store)
    with client:
        _create_collection_with_document(client)
        stored_search = store.search

        async def bounded(**kwargs):
            candidates = await stored_search(**kwargs)
            return RetrievalCandidateBatch(candidates, degradations=(degradation(1 if complete else 0),))

        store.search = bounded  # type: ignore[method-assign]
        # The service measures the final count itself: one stored document
        # fills a width of one and misses a width of four.
        response = client.post("/v1/knowledge/search", json={"query": "Haftung", "top_k": 1 if complete else 4})
        [warning] = response.json()["warnings"]
        assert warning["final_evidence_complete"] is complete
        route = warning["message"]

    expected = warning_message("vector_overfetch_cap", final_evidence_complete=complete)
    assert capability == route == expected
    assert ("nevertheless reached" in expected) is complete
