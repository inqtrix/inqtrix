"""Fail-closed restart guard shared by both agent engines.

LangGraph checkpoints are a resumability cache; durable control rows and
child runs are the evidence that execution already started.  A missing
checkpoint is safe to treat as a fresh run only when every evidence source
was read successfully and all of them are empty.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from inqtrix.agents.control_ports import PlanNotFound

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.runs.ports import RunStorePort


class CheckpointRestartUnsafe(RuntimeError):
    """A checkpoint-less agent run cannot be proven to be fresh."""


class _AsyncRunner(Protocol):
    def __call__(self, awaitable: "Awaitable[Any]") -> Any: ...


def ensure_checkpoint_restart_safe(
    run_id: str,
    *,
    control: "AgentControlStore",
    run_store: "RunStorePort",
    run_async: _AsyncRunner,
) -> None:
    """Allow a checkpoint-less start only after a complete empty probe.

    The function deliberately evaluates every source instead of returning
    after the first hit.  This makes the decision one conservative unit: a
    later store failure wins over an earlier successful read and prevents a
    potentially destructive restart.

    Args:
        run_id: Root agent run whose checkpoint is absent.
        control: Canonical plan, gate, and artifact store.
        run_store: Canonical child-run store.
        run_async: Sync bridge used by the calling engine's worker thread.

    Raises:
        CheckpointRestartUnsafe: Negotiated/executed state exists, or any
            source could not be read with certainty.
    """
    try:
        try:
            run_async(control.get_plan(run_id))
            has_plan = True
        except PlanNotFound:
            has_plan = False

        approvals = run_async(control.list_approvals(run_id))
        clarifications = run_async(control.list_clarifications(run_id))
        artifacts, _cursor = run_async(
            control.list_artifacts(run_id, limit=1)
        )
        children = run_store.children(run_id)
    except Exception as exc:
        raise CheckpointRestartUnsafe(
            ("Checkpoint status for this agent run could not be fully verified; the run "
            "will not be restarted because its prior state is uncertain.")
        ) from exc

    if has_plan or approvals or clarifications or artifacts or children:
        raise CheckpointRestartUnsafe(
            ("The checkpoint for this agent run was lost; the run cannot resume. Start "
            "the assignment as a new run; control and artifact data are retained.")
        )
