"""Canonicalization of plan collection references (name -> id).

THE one collection-reference rule for every plan that wants to execute
(a single definition shared by every caller): the planner output and a user's edit decision both
run through :func:`resolve_plan_collections` BEFORE the shared validator
(:mod:`inqtrix.agents.plan_validation`) checks membership against the
caller-visible catalog. The duties are deliberately non-overlapping:

* this module rewrites resolvable NAME references to canonical ids —
  planners and humans naturally quote "EU-AI-Act", never ``kc_...`` —
  and reports ambiguity (two collections sharing a name cannot be
  resolved deterministically);
* unknown references stay untouched so the validator's
  ``allowed_collection_ids`` check reports them alongside every other
  violation in one repair round.

Retrieval itself keeps the strict visibility gate
(``assert_collections_visible``) as defense in depth; after this pass a
saved plan carries only canonical ids, so that gate passes instead of
surfacing a raw ``CollectionNotFound`` mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from inqtrix.agents.plan_models import ExecutionPlanModel

__all__ = [
    "CollectionCatalogEntry",
    "resolve_plan_collections",
]


@dataclass(frozen=True)
class CollectionCatalogEntry:
    """One caller-visible knowledge collection, as planning surfaces see it.

    Attributes:
        collection_id: Canonical store id (``kc_...``) retrieval expects
            in ``params.collection_ids``.
        name: Operator/user-facing label the planner naturally quotes;
            resolved to :attr:`collection_id` when unambiguous.
        embedding_model: Embedding model id of the collection. Shown in
            planner-facing listings so scope decisions can weigh corpus
            comparability; not used for resolution.
        document_count: Live document count. Lets the planner (and the
            user reviewing a plan) judge whether a collection is worth a
            task at all; not used for resolution.
    """

    collection_id: str
    name: str
    embedding_model: str = ""
    document_count: int = 0


def _normalize(name: str) -> str:
    """Match key for name lookups: whitespace-collapsed, casefolded."""
    return " ".join(name.split()).casefold()


def resolve_plan_collections(
    plan: "ExecutionPlanModel",
    catalog: Sequence[CollectionCatalogEntry],
) -> list[str]:
    """Rewrite name-based ``collection_ids`` entries to canonical ids.

    Mutates *plan* in place — the caller exclusively owns the instance in
    both call sites (planner repair loop, approval-edit endpoint) and
    persists it only after the combined error list is empty.

    Per entry: an exact id match stays; a unique normalized name match
    becomes that id; an ambiguous name is reported; anything else is left
    untouched for the validator to flag as unknown. Duplicates collapsing
    onto the same id (name and id of one collection listed together) are
    deduplicated, order preserved.

    Args:
        plan: Parsed plan whose task params are canonicalized in place.
        catalog: Caller-visible collections (empty means the caller sees
            none — every explicit reference will then fail validation).

    Returns:
        Ambiguity errors (German, user-facing); empty means every
        reference is either canonical or left for the validator.
    """
    known_ids = {entry.collection_id for entry in catalog}
    by_name: dict[str, list[str]] = {}
    for entry in catalog:
        by_name.setdefault(_normalize(entry.name), []).append(
            entry.collection_id
        )
    errors: list[str] = []
    for task in plan.tasks:
        raw_ids = task.params.collection_ids
        if not raw_ids:
            continue
        resolved: list[str] = []
        for raw in raw_ids:
            value = str(raw).strip()
            if not value:
                continue
            if value in known_ids:
                candidate = value
            else:
                matches = by_name.get(_normalize(value), [])
                if len(matches) == 1:
                    candidate = matches[0]
                elif matches:
                    errors.append(
                        f"Task {task.id}: collection name {value!r} is "
                        "ambiguous; use the collection id."
                    )
                    continue
                else:
                    candidate = value
            if candidate not in resolved:
                resolved.append(candidate)
        task.params.collection_ids = resolved or None
    return errors
