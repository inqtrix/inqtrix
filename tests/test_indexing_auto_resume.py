"""Automatic resumption of dependency pauses: plan and sweep, no storage."""

from __future__ import annotations

import pytest

from inqtrix.contextualization_circuit import (
    ContextualizationCircuitState,
    ContextualizationCircuitView,
)
from inqtrix.indexing_auto_resume import (
    AutoResumePolicy,
    DueResume,
    plan_pause,
    sweep_due_resumes,
)
from inqtrix.server.indexing import (
    IndexingJobConflict,
    IndexingJobNotFound,
    IndexingResumeUnavailable,
)

POLICY = AutoResumePolicy(max_attempts=5, base_seconds=60.0, max_delay_seconds=900.0)
NO_JITTER = lambda: 0.5  # noqa: E731 - the jitter term is zero at the midpoint
NOW = 1_000.0


def _plan_chain(pauses: int, *, progress: list[int] | None = None) -> list[dict]:
    """Pause, resume automatically, pause again: the plans one job collects."""
    plans: list[dict] = []
    previous = None
    for _ in range(pauses):
        plan = plan_pause(
            previous,
            error_type="contextualization_provider_timeout",
            progress=progress or [0, 3],
            now=NOW,
            policy=POLICY,
            random=NO_JITTER,
        )
        plans.append(plan)
        if plan["stopped"] is not None:
            break
        # What the sweep records when it resumes the job.
        previous = {**plan, "attempts": plan["attempts"] + 1, "resumed_automatically": True}
    return plans


def test_waits_grow_from_one_minute_to_the_cap_and_stop_after_five_resumes():
    plans = _plan_chain(7)

    assert [plan["next_at"] - NOW for plan in plans[:5]] == [60.0, 120.0, 240.0, 480.0, 900.0]
    assert [plan["attempts"] for plan in plans] == [0, 1, 2, 3, 4, 5]
    assert plans[-1]["stopped"] == "exhausted"
    assert plans[-1]["next_at"] is None
    assert all(plan["max"] == 5 for plan in plans)


def test_jitter_spreads_a_wait_by_at_most_a_fifth():
    low = plan_pause(None, error_type="embedding_provider_timeout", progress=[0, 0],
                     now=NOW, policy=POLICY, random=lambda: 0.0)
    high = plan_pause(None, error_type="embedding_provider_timeout", progress=[0, 0],
                      now=NOW, policy=POLICY, random=lambda: 1.0)

    assert low["next_at"] - NOW == pytest.approx(48.0)
    assert high["next_at"] - NOW == pytest.approx(72.0)


def test_progress_since_the_last_pause_starts_a_new_streak():
    tired = {"attempts": 4, "max": 5, "progress": [0, 3], "next_at": None,
             "stopped": None, "resumed_automatically": True}

    plan = plan_pause(tired, error_type="contextualization_provider_timeout",
                      progress=[0, 7], now=NOW, policy=POLICY, random=NO_JITTER)

    assert plan["attempts"] == 0
    assert plan["next_at"] - NOW == 60.0
    assert plan["progress"] == [0, 7]


def test_a_bounce_at_the_open_circuit_gives_the_resume_back():
    # The resumed job never reached the provider: the circuit held it. That
    # resume does not count against the job.
    bounced = {"attempts": 2, "max": 5, "progress": [0, 3], "next_at": None,
               "stopped": None, "resumed_automatically": True}

    plan = plan_pause(bounced, error_type="contextualization_provider_circuit_open",
                      progress=[0, 3], now=NOW, policy=POLICY, random=NO_JITTER)

    assert plan["attempts"] == 1


def test_no_plan_when_automatic_resumption_is_off():
    off = AutoResumePolicy(max_attempts=0)

    assert plan_pause(None, error_type="vector_store_timeout", progress=[0, 0],
                      now=NOW, policy=off, random=NO_JITTER) is None


def _due(job_id: str, error_type: str = "contextualization_provider_timeout") -> DueResume:
    return DueResume(job_id=job_id, error_type=error_type, next_at=NOW - 1)


class Sweep:
    """Records what one sweep resumed and stopped."""

    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.resumed: list[str] = []
        self.stopped: list[tuple[str, str]] = []
        self._failures = failures or {}

    def resume(self, job_id: str) -> None:
        if job_id in self._failures:
            raise self._failures[job_id]
        self.resumed.append(job_id)

    def stop(self, job_id: str, reason: str) -> None:
        self.stopped.append((job_id, reason))


def _view(state: ContextualizationCircuitState, *, cooldown_until: float = 0.0,
          probe_lease_until: float | None = None) -> ContextualizationCircuitView:
    return ContextualizationCircuitView(
        state=state, cooldown_until=cooldown_until, probe_lease_until=probe_lease_until
    )


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (None, ["a", "b", "c"]),
        (_view(ContextualizationCircuitState.CLOSED), ["a", "b", "c"]),
        (_view(ContextualizationCircuitState.OPEN, cooldown_until=NOW + 30), []),
        (_view(ContextualizationCircuitState.OPEN, cooldown_until=NOW - 1), ["a"]),
        (_view(ContextualizationCircuitState.HALF_OPEN, probe_lease_until=NOW + 30), []),
        (_view(ContextualizationCircuitState.HALF_OPEN, probe_lease_until=NOW - 1), ["a"]),
    ],
    ids=["no-circuit", "closed", "cooling", "cooled", "probe-held", "probe-lapsed"],
)
def test_one_circuit_lets_one_probe_through_until_it_closes(view, expected):
    sweep = Sweep()

    sweep_due_resumes(
        [_due("a"), _due("b"), _due("c")],
        now=NOW, policy=POLICY, resume=sweep.resume, stop=sweep.stop,
        circuit=lambda: view,
    )

    assert sweep.resumed == expected


def test_the_circuit_never_holds_back_other_dependencies():
    sweep = Sweep()

    sweep_due_resumes(
        [_due("a"), _due("b", "embedding_provider_timeout"), _due("c", "vector_store_unavailable")],
        now=NOW, policy=POLICY, resume=sweep.resume, stop=sweep.stop,
        circuit=lambda: _view(ContextualizationCircuitState.OPEN, cooldown_until=NOW + 30),
    )

    assert sweep.resumed == ["b", "c"]


def test_one_sweep_resumes_a_bounded_number():
    sweep = Sweep()

    sweep_due_resumes(
        [_due(str(index), "embedding_provider_timeout") for index in range(9)],
        now=NOW, policy=AutoResumePolicy(max_attempts=5, per_sweep=3),
        resume=sweep.resume, stop=sweep.stop, circuit=lambda: None,
    )

    assert sweep.resumed == ["0", "1", "2"]


def test_a_resume_the_job_can_never_take_stops_visibly_and_the_rest_goes_on():
    sweep = Sweep(failures={
        "gone": IndexingJobNotFound("gone"),
        "lost": IndexingResumeUnavailable("no execution context"),
        "busy": IndexingJobConflict("kc_1"),
    })

    report = sweep_due_resumes(
        [_due("gone", "vector_store_timeout"), _due("lost", "vector_store_timeout"),
         _due("busy", "vector_store_timeout"), _due("ok", "vector_store_timeout")],
        now=NOW, policy=POLICY, resume=sweep.resume, stop=sweep.stop, circuit=lambda: None,
    )

    assert sweep.stopped == [("gone", "access"), ("lost", "unavailable")]
    assert sweep.resumed == ["ok"]
    assert report.resumed == 1
    assert report.stopped == 2
    assert report.deferred == 1
