"""The deterministic replan gate.

Decides WHETHER a replan is needed and whether autonomy auto-approves
it; the replan itself reuses the planner. Deltas are ADDITIVE — results
never get discarded (append-only plan versions).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from inqtrix.agents.control_ports import PlanTaskRecord
from inqtrix.agents.scheduler import TaskOutcome

AUTO_APPROVABLE_REPLAN_TOOLS = ("rag_query",)
"""Task kinds a balanced replan may add without showing its plan: they touch
ONLY the user's own holdings — no web contact — at the cost of one search.

In Standard mode (``balanced``), the plan
gate is the web-search consent (tasks carry their verbatim queries), so
a replan may only auto-approve when it introduces NO new web contact —
otherwise new queries would run that the user never saw. A file analysis
is internal too, but it reads whole documents (one model call per window,
up to a task's window budget), so a replan that adds one shows its plan."""


def evidence_identity(references: Sequence[Mapping[str, Any]]) -> str:
    """A fingerprint of a run's evidence: the sorted identities of its references.

    An identity is what a reference cites: a document chunk
    (``doc:{document_id}#{chunk_index}``) or a normalized URL. Two review
    rounds with the same fingerprint cited nothing new. A round that only
    cites chunks and URLs the run already cited counts as no progress; a
    re-read of a known document version that cites a chunk not cited before
    is progress.
    """
    from inqtrix.agents.evidence import dedup_key

    keys = sorted({dedup_key(dict(ref)) for ref in references} - {""})
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def has_unresolved_outcome(outcomes: dict[str, TaskOutcome]) -> bool:
    """Whether task execution leaves a failure or an evidence shortfall."""
    return any(
        outcome.status in {"failed", "insufficient_evidence"}
        for outcome in outcomes.values()
    )


def evaluate_replan(
    *,
    outcomes: dict[str, TaskOutcome],
    blocking_gap_uncovered: bool,
    sufficiency_coverage: str,
    replan_rounds_used: int,
    max_replan_rounds: int,
) -> bool:
    """Whether a replan should run (the deterministic failure policy).

    A replan is possible only when execution produced a real unresolved
    task outcome (``failed`` or ``insufficient_evidence``). Blocking-gap and
    sufficiency signals then decide whether more work is justified. A clean
    set of successful tasks proceeds directly to synthesis even when the
    advisory sufficiency model is pessimistic. Returns a bare bool — the gate consumes only
    whether to loop; the ``reason``/``auto_approved`` fields the old
    ``ReplanDecision`` carried were never read (the plan-version reason is
    computed separately in ``_node_plan`` from the route), so they are
    dropped rather than kept as dead scoring.
    """
    if replan_rounds_used >= max_replan_rounds:
        return False
    if not has_unresolved_outcome(outcomes):
        return False
    if blocking_gap_uncovered:
        return True
    if sufficiency_coverage == "uncovered":
        return True
    return sufficiency_coverage == "partial"


def autonomy_auto_approves(
    *,
    autonomy: str,
    new_tasks: list[PlanTaskRecord],
) -> bool:
    """Replan policy: strict never,
    autonomous always, balanced only for <= 2 NEW INTERNAL read-only
    tasks — a replan that adds web queries always re-gates, because the
    approved plan is the user's web-search consent and new queries must
    be seen before they run."""
    if autonomy == "autonomous":
        return True
    if autonomy == "strict":
        return False
    return len(new_tasks) <= 2 and all(
        task.tool_kind in AUTO_APPROVABLE_REPLAN_TOOLS for task in new_tasks
    )
