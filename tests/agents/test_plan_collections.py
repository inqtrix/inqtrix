"""Name -> id canonicalization of plan collection references.

The regression behind these tests: the planner naturally quoted the
collection NAME ("EU-AI-Act") in ``params.collection_ids``; execution
looked it up as a store id and every rag task failed with a raw
``CollectionNotFound`` KeyError. The resolver rewrites resolvable names
to canonical ids BEFORE validation, so a saved plan can never carry a
name-shaped reference again.
"""

from __future__ import annotations

from inqtrix.agents.plan_collections import (
    CollectionCatalogEntry,
    resolve_plan_collections,
)
from inqtrix.agents.plan_models import ExecutionPlanModel

_CATALOG = [
    CollectionCatalogEntry(
        collection_id="kc_18d4",
        name="EU-AI-Act",
        embedding_model="text-embedding-3-large",
        document_count=12,
    ),
    CollectionCatalogEntry(collection_id="kc_beta", name="Marktdaten"),
    CollectionCatalogEntry(collection_id="kc_dup1", name="Recht"),
    CollectionCatalogEntry(collection_id="kc_dup2", name="recht"),
]


def _plan(collection_ids: list[str] | None, tool_kind: str = "rag_query"):
    return ExecutionPlanModel.model_validate(
        {
            "tasks": [
                {
                    "id": "t1",
                    "title": "Interne Sammlung sichten",
                    "tool_kind": tool_kind,
                    "queries": ["Welche Dokumente sind relevant?"],
                    "params": {"collection_ids": collection_ids},
                },
                {
                    "id": "s",
                    "title": "Synthese",
                    "tool_kind": "synthesis",
                    "depends_on": ["t1"],
                },
            ]
        }
    )


def test_exact_id_reference_stays() -> None:
    plan = _plan(["kc_18d4"])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["kc_18d4"]


def test_unique_name_reference_becomes_canonical_id() -> None:
    plan = _plan(["EU-AI-Act"])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["kc_18d4"]


def test_name_match_normalizes_case_and_whitespace() -> None:
    plan = _plan(["  eu-ai-act "])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["kc_18d4"]


def test_ambiguous_name_is_reported_not_guessed() -> None:
    plan = _plan(["Recht"])
    errors = resolve_plan_collections(plan, _CATALOG)
    assert errors == [
        "Task t1: collection name 'Recht' is ambiguous; "
        "use the collection id."
    ]
    # The ambiguous entry is dropped from params (the error blocks the
    # plan anyway); nothing silently resolves to either candidate.
    assert plan.tasks[0].params.collection_ids is None


def test_unknown_reference_left_for_the_validator() -> None:
    plan = _plan(["Unbekannte Sammlung"])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["Unbekannte Sammlung"]


def test_name_and_id_of_same_collection_deduplicate() -> None:
    plan = _plan(["EU-AI-Act", "kc_18d4", "Marktdaten"])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["kc_18d4", "kc_beta"]


def test_blank_entries_collapse_to_inherited_scope() -> None:
    plan = _plan(["", "   "])
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids is None


def test_tasks_without_references_untouched() -> None:
    plan = _plan(None)
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids is None


def test_file_analysis_references_resolved_too() -> None:
    """The executor reads ``collection_ids`` for file_analysis as well
    (``_task_collection_scope``) — the canonicalization must not be
    rag-only."""
    plan = _plan(["EU-AI-Act"], tool_kind="file_analysis")
    assert resolve_plan_collections(plan, _CATALOG) == []
    assert plan.tasks[0].params.collection_ids == ["kc_18d4"]
