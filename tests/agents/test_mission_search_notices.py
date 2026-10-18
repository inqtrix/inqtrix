"""A mission's knowledge task (rag_query) and what its searches reported.

The knowledge mode runs as a scripted stand-in, so the task's own folding,
wording and open-point handling are what the tests read: notices fold by
code over the task's searches, read in the run's language without codes,
and only a limit travels as an open point.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from inqtrix.agents import algorithm
from inqtrix.agents.control_ports import PlanTaskRecord
from inqtrix.agents.scheduler import TaskOutcome, task_result_payload
from inqtrix.core.results import AgentResult

_REINDEX = {
    "code": "chunks_require_reindex", "reason": "source_unverified", "stage": "canonical_hydration",
    "count": 2, "recommended_action": "reindex",
}
_DUPLICATE = {
    "code": "duplicate_documents_collapsed", "reason": "duplicate_document",
    "stage": "canonical_hydration", "count": 3, "recommended_action": None,
}
_CAP = {
    "reason": "vector_overfetch_cap", "retrieval_mode": "hybrid", "stage": "vector_candidate_pool",
    "requested_candidate_pool": 40, "returned_candidate_pool": 12, "final_top_k": 8,
    "final_evidence_complete": False, "requested_top_k": 8, "returned_hits": 3, "candidate_cap": 64,
}


class _Knowledge:
    """The knowledge mode: one referenced answer per query, with the
    retrieval receipt a real run persists."""

    def __init__(self, *, warnings: list[dict[str, Any]], degradations: list[dict[str, Any]] = ()) -> None:
        self.warnings = warnings
        self.degradations = list(degradations)

    def run(self, request: Any, *, runtime: Any, context: Any) -> AgentResult:
        answer = "Interne Befunde: Anbieter A führt."
        return AgentResult(
            answer=answer,
            raw={
                "answer": answer,
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                "result_state": {
                    "answer": answer,
                    "knowledge_retrieval": {
                        "warnings": [dict(item) for item in self.warnings],
                        "degradations": [dict(item) for item in self.degradations],
                    },
                    "report_references": [
                        {"title": "Marktbericht", "document_id": "doc-1", "chunk_index": 0,
                         "source_text": "Anbieter A führt."},
                    ],
                },
            },
        )


def _deps(knowledge: _Knowledge) -> Any:
    return SimpleNamespace(
        runtime=SimpleNamespace(registry=SimpleNamespace(get=lambda _mode: knowledge)),
        platform=SimpleNamespace(max_parallel_queries_per_task=1),
        collection_scope=lambda: [],
        emit=lambda *_a, **_k: None,
        context=SimpleNamespace(
            providers=None, strategies=None, agent_settings=None, principal=None,
            workspace_id=None, cancel_token=None, token_budget=0,
        ),
    )


def _task() -> PlanTaskRecord:
    return PlanTaskRecord(
        task_id="t1", plan_id="p", run_id="run_x", ordinal=1, title="Interne Quellen befragen",
        tool_kind="rag_query", queries=("Anbieter", "Marktanteile"),
    )


def _run(knowledge: _Knowledge, language: str = "de") -> TaskOutcome:
    return algorithm._run_rag_query(_deps(knowledge), {"language": language}, _task(), 1)


def test_rag_query_search_note_is_localized_without_codes():
    """Two searches, two exclusions each: one German sentence under the
    search-note heading, four exclusions over two searches, no code."""
    outcome = _run(_Knowledge(warnings=[_REINDEX]))
    assert outcome.status == "completed"
    assert outcome.answer_markdown.endswith(
        "Suchhinweis:\n- Treffer, deren Originaltext sich nicht prüfen ließ, wurden "
        "ausgeschlossen (4 Ausschlüsse in 2 Suchen; derselbe Treffer kann mehrfach zählen); "
        "solche Inhalte müssen neu indiziert werden."
    )
    assert "chunks_require_reindex" not in outcome.answer_markdown
    assert "Retrieval" not in outcome.answer_markdown


def test_rag_query_search_limit_travels_as_open_point():
    """A candidate limit is an open point of the task: flagged, and its
    note is the reader sentence, not the task's missing confidence."""
    outcome = _run(_Knowledge(warnings=[], degradations=[_CAP]))
    assert [notice["code"] for notice in outcome.search_notices] == ["vector_overfetch_cap"]
    assert algorithm._task_evidence_flagged(outcome)
    note = algorithm._task_evidence_note("t1", outcome, "de", title="Interne Quellen befragen")
    assert note == (
        "Teilaufgabe „Interne Quellen befragen“: Die Wissenssuche erreichte eine technische "
        "Kandidatengrenze (in 2 Suchen) und lieferte im knappsten Fall 3 von 8 angeforderten "
        "Treffern; das Ergebnis ist kein Vollständigkeitsnachweis."
    )


def test_a_duplicate_collapse_alone_does_not_flag_the_task_evidence():
    """A skipped duplicate protects the evidence: the answer names it, no
    open point is raised for it."""
    outcome = _run(_Knowledge(warnings=[_DUPLICATE]))
    assert "6 Treffer wurden übergangen" in outcome.answer_markdown
    # The store collapses a content-identical DOCUMENT; the sentence says so.
    assert "inhaltsgleiche Dokumente schon unter den gefundenen Quellen" in outcome.answer_markdown
    assert outcome.search_notices == []
    assert not algorithm._task_evidence_flagged(outcome)


def test_the_search_limit_survives_the_task_row_after_a_resume():
    """A resumed segment rebuilds the outcome from the persisted row; the
    open point must come back with it."""
    outcome = _run(_Knowledge(warnings=[_REINDEX]))
    row = PlanTaskRecord(
        task_id="t1", plan_id="p", run_id="run_x", ordinal=1, title="Interne Quellen befragen",
        tool_kind="rag_query", status="completed", result_summary=outcome.summary,
        result_payload=task_result_payload(outcome, persisted_summary=outcome.summary),
    )
    rebuilt = algorithm._outcome_from_persisted_task(row)
    assert rebuilt.search_notices == outcome.search_notices != []
    assert algorithm._task_evidence_flagged(rebuilt)
