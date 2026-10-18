"""Automatic resumption of indexing work a dependency outage paused.

A ``paused_dependency`` job carries a bounded resume plan in its checkpoint
(``auto_resume``): how many automatic resumes its current streak used, the
limit, when the next one is due, and why the streak stopped. The plan is made
when the job pauses (``plan_pause``) and consumed by a periodic sweep
(``sweep_due_resumes``) in the API process, which resumes a job the way a
person's click does. Waits grow from ``base_seconds`` to
``max_delay_seconds``; ``max_attempts`` resumes in a row without progress end
the streak. Validation pauses never get a plan: they need a person, not
another attempt.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from inqtrix.contextualization_circuit import (
    CircuitAdmission,
    ContextualizationCircuitView,
)

log = logging.getLogger("inqtrix")

# A pause at an open circuit made no provider call: the resume that led to it
# waited for the circuit and does not count against the job.
CIRCUIT_OPEN_ERROR_TYPE = "contextualization_provider_circuit_open"
# Pauses the shared contextualization circuit governs: at most one of them is
# resumed while the circuit is not closed (its probe).
_CIRCUIT_ERROR_PREFIX = "contextualization_"
# How often the API process sweeps for due resumes (``PeriodicSweep``).
SWEEP_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True)
class AutoResumePolicy:
    """How often and how soon dependency pauses resume on their own."""

    max_attempts: int = 5
    base_seconds: float = 60.0
    max_delay_seconds: float = 900.0
    jitter: float = 0.2
    per_sweep: int = 5

    @property
    def enabled(self) -> bool:
        return self.max_attempts > 0


@dataclass(frozen=True)
class DueResume:
    """A paused job whose next automatic resume is due."""

    job_id: str
    error_type: str
    next_at: float


@dataclass(frozen=True)
class SweepReport:
    """What one sweep did."""

    resumed: int = 0
    stopped: int = 0
    deferred: int = 0


def plan_pause(
    previous: Mapping[str, Any] | None,
    *,
    error_type: str,
    progress: Sequence[int],
    now: float,
    policy: AutoResumePolicy,
    random: Callable[[], float],
) -> dict[str, Any] | None:
    """The resume plan a dependency pause stores, or ``None`` when off.

    ``previous`` is the plan of the job's last pause, updated by the sweep
    when it resumed the job. Progress since that pause starts a new streak.
    """
    if not policy.enabled:
        return None
    attempts = int(previous.get("attempts", 0)) if previous else 0
    if (
        previous
        and previous.get("resumed_automatically")
        and error_type == CIRCUIT_OPEN_ERROR_TYPE
    ):
        attempts = max(0, attempts - 1)
    if previous and list(progress) > list(previous.get("progress") or [0, 0]):
        attempts = 0
    plan: dict[str, Any] = {
        "attempts": attempts,
        "max": policy.max_attempts,
        "progress": list(progress),
        "next_at": None,
        "stopped": None,
    }
    if attempts >= policy.max_attempts:
        plan["stopped"] = "exhausted"
        return plan
    delay = min(policy.max_delay_seconds, policy.base_seconds * 2**attempts)
    delay *= 1 + policy.jitter * (2 * random() - 1)
    plan["next_at"] = now + delay
    return plan


def public_plan(plan: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The part of a plan clients see: attempts, limit, due time, stop."""
    if not plan:
        return None
    return {
        "attempts": int(plan.get("attempts", 0)),
        "max": int(plan.get("max", 0)),
        "next_at": plan.get("next_at"),
        "stopped": plan.get("stopped"),
    }


def is_due(plan: Mapping[str, Any] | None, now: float) -> bool:
    """Whether a plan asks for an automatic resume at ``now``.

    A stopped streak and a running job have no due time.
    """
    next_at = plan.get("next_at") if plan else None
    return next_at is not None and float(next_at) <= now


def sweep_due_resumes(
    due: Sequence[DueResume],
    *,
    now: float,
    policy: AutoResumePolicy,
    resume: Callable[[str], Any],
    stop: Callable[[str, str], None],
    circuit: Callable[[], ContextualizationCircuitView | None],
) -> SweepReport:
    """Resume the due jobs, oldest first, within the policy's bounds.

    The contextualization circuit admits one probe while it is not closed;
    jobs it governs wait for the probe's outcome instead of bouncing. A job
    the sweep can never resume (its creator lost access, its execution
    context is gone) stops its streak visibly; anything else waits for the
    next sweep.
    """
    from inqtrix.server.indexing import (
        IndexingJobConflict,
        IndexingJobNotFound,
        IndexingResumeUnavailable,
    )

    resumed = stopped = deferred = 0
    circuit_budget: int | None = None
    for item in sorted(due, key=lambda entry: entry.next_at):
        if resumed >= policy.per_sweep:
            break
        if item.error_type.startswith(_CIRCUIT_ERROR_PREFIX):
            if circuit_budget is None:
                view = circuit()
                admission = CircuitAdmission.ALL if view is None else view.admission(now)
                circuit_budget = {
                    CircuitAdmission.ALL: policy.per_sweep,
                    CircuitAdmission.ONE: 1,
                    CircuitAdmission.NONE: 0,
                }[admission]
            if circuit_budget <= 0:
                deferred += 1
                continue
        try:
            resume(item.job_id)
        except IndexingJobNotFound:
            stop(item.job_id, "access")
            stopped += 1
            continue
        except IndexingResumeUnavailable:
            stop(item.job_id, "unavailable")
            stopped += 1
            continue
        except IndexingJobConflict:
            deferred += 1
            continue
        except Exception as exc:  # noqa: BLE001 - one job never stops the sweep
            log.warning(
                "Automatic resume of indexing job %s failed; it waits for the next "
                "sweep (error_type=%s)",
                item.job_id,
                type(exc).__name__,
            )
            deferred += 1
            continue
        resumed += 1
        if item.error_type.startswith(_CIRCUIT_ERROR_PREFIX) and circuit_budget is not None:
            circuit_budget -= 1
    return SweepReport(resumed=resumed, stopped=stopped, deferred=deferred)
