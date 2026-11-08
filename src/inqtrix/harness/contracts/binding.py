"""The binding of a run to the workflow contract it was accepted under.

Standard library and pydantic only: the run store reads bindings in its claim
transaction, and the contracts are the one part of the kernel it may import.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RunBinding(BaseModel):
    """The mode a run belongs to and the contract version it was stamped with."""

    model_config = ConfigDict(frozen=True)

    mode: str
    workflow_contract: int = Field(ge=1)


class BindingVerdict(StrEnum):
    """What a claim does with a run whose binding it has read."""

    MATCH = "match"
    """Same contract as the worker: the run continues."""
    RESTAMP = "restamp"
    """Older contract, but nothing stored to resume from: the run takes the
    worker's contract and continues."""
    RETIRE_OLDER = "retire_older"
    """Older contract with stored state the worker can no longer read: the run
    ends visibly and its checkpoint is deleted."""
    DEFER_NEWER = "defer_newer"
    """Newer contract than the worker carries: the claim rolls back and the
    worker claims no further runs (its other jobs go on) until the run is
    cancelled, ends or parks."""
    RETIRED_MODE = "retired_mode"
    """A mode taken out on purpose: the run ends visibly and its checkpoint is
    deleted."""
    UNAVAILABLE = "unavailable"
    """A mode this worker has not registered: a configuration fault, so the
    claim rolls back, the run and its checkpoint stay, and the worker stops
    claiming."""


class RunBindingNewer(RuntimeError):
    """A run bound to a newer contract than this worker carries.

    Raised inside the claim transaction like a schema fence: the claim rolls
    back, the entry stays unacknowledged for a newer worker, and this worker
    stops claiming. The fields are identifiers a log line may name; the log
    filter shows the exception itself only by its type.
    """

    def __init__(
        self, *, run_id: str, mode: str, bound_contract: int, worker_contract: int
    ) -> None:
        super().__init__(
            f"Run {run_id} is bound to {mode} contract {bound_contract}; "
            f"this worker carries {worker_contract}."
        )
        self.run_id = run_id
        self.mode = mode
        self.bound_contract = bound_contract
        self.worker_contract = worker_contract


class WorkflowUnavailable(RuntimeError):
    """A run of a mode this worker has not registered.

    Handled like :class:`RunBindingNewer`: the claim rolls back, the run and
    its checkpoint stay, and this worker claims no further runs until the run is
    cancelled, ends or parks. ``registered`` names the modes the worker does carry.
    """

    def __init__(self, *, run_id: str, mode: str, registered: tuple[str, ...]) -> None:
        super().__init__(
            f"Run {run_id} belongs to mode {mode}, which this worker has not "
            f"registered (registered: {', '.join(registered) or 'none'})."
        )
        self.run_id = run_id
        self.mode = mode
        self.registered = registered


RETIRED_WORKFLOWS: frozenset[str] = frozenset()
"""Modes taken out of the product on purpose. Their runs end visibly; a mode
that is only missing on a worker is a configuration fault and never lands
here."""


def check_run_binding(
    binding: RunBinding,
    current: Mapping[str, int],
    *,
    has_checkpoint_state: bool,
) -> BindingVerdict:
    """Compare a run's binding with the contract this worker carries for its mode.

    Args:
        binding: The run's binding.
        current: The contract version this worker carries, per registered mode.
        has_checkpoint_state: Whether the run has stored state to resume from,
            today ``attempt > 0``. Without it an older binding is stamped anew
            instead of ending the run.

    Returns:
        The verdict the claim acts on.
    """
    if binding.mode in RETIRED_WORKFLOWS:
        return BindingVerdict.RETIRED_MODE
    if binding.mode not in current:
        return BindingVerdict.UNAVAILABLE
    worker = current[binding.mode]
    if binding.workflow_contract == worker:
        return BindingVerdict.MATCH
    if binding.workflow_contract > worker:
        return BindingVerdict.DEFER_NEWER
    if has_checkpoint_state:
        return BindingVerdict.RETIRE_OLDER
    return BindingVerdict.RESTAMP
