"""The shared deterministic-retry predicate of the failure policy.

``should_retry`` is the ONE decision both retry loops call — the in-process
wave (``execute_wave``) and the parked child-run node (``_node_children_wait``)
— so the two cannot drift. Table-driven so a change to the policy (first
attempt only, transient only, not-cancelled) goes red.
"""

from __future__ import annotations

import contextvars
import threading
import time
from types import SimpleNamespace

import pytest

from inqtrix.agents.scheduler import TaskOutcome, execute_wave, should_retry
from inqtrix.exceptions import AgentCancelled


@pytest.mark.parametrize(
    "status, transient, attempt, cancelled, expected",
    [
        # The one retryable shape: first transient failure, no cancel.
        ("failed", True, 1, False, True),
        # Only the FIRST attempt retries — the retry (attempt 2) does not.
        ("failed", True, 2, False, False),
        # A pending cancel suppresses the retry.
        ("failed", True, 1, True, False),
        # A non-transient failure is terminal — no retry.
        ("failed", False, 1, False, False),
        # A non-failed outcome never retries.
        ("completed", True, 1, False, False),
        ("insufficient_evidence", True, 1, False, False),
    ],
)
def test_should_retry(status, transient, attempt, cancelled, expected):
    outcome = TaskOutcome(status=status, transient=transient)
    assert (
        should_retry(outcome, attempt=attempt, cancelled=cancelled) is expected
    )


def _wave_task(task_id: str = "t1"):
    # execute_wave reads only task_id off the task; the executor is injected.
    return SimpleNamespace(task_id=task_id)


def test_execute_wave_retries_a_transient_failure_and_accumulates_usage():
    """The in-process substrate: a transient attempt-1 failure earns exactly
    ONE retry (attempt 2), and the wave returns the retry outcome with
    first+retry token usage folded (spent in the parent run)."""
    attempts: list[int] = []

    def executor(task, attempt):
        attempts.append(attempt)
        if attempt == 1:
            return TaskOutcome(
                status="failed",
                transient=True,
                failure_reason="rate_limited",
                usage={"prompt_tokens": 10, "completion_tokens": 2},
            )
        return TaskOutcome(
            status="completed",
            summary="ok",
            usage={"prompt_tokens": 5, "completion_tokens": 1},
        )

    outcomes = execute_wave(
        [_wave_task()],
        executor=executor,
        max_parallel=1,
        cancelled=lambda: False,
        language="de",
    )

    assert attempts == [1, 2]  # exactly one retry
    result = outcomes["t1"]
    assert result.status == "completed"
    assert result.usage == {"prompt_tokens": 15, "completion_tokens": 3}


def test_execute_wave_does_not_retry_a_nontransient_failure():
    """A non-transient failure is terminal — no second attempt."""
    attempts: list[int] = []

    def executor(task, attempt):
        attempts.append(attempt)
        return TaskOutcome(status="failed", transient=False)

    outcomes = execute_wave(
        [_wave_task()],
        executor=executor,
        max_parallel=1,
        cancelled=lambda: False,
        language="de",
    )

    assert attempts == [1]  # no retry
    assert outcomes["t1"].status == "failed"


def test_retry_admission_runs_on_scheduler_thread_and_can_observe_task_cancel():
    """A durable task cancel between attempts prevents provider attempt two."""
    scheduler_thread = threading.get_ident()
    attempts: list[int] = []

    def executor(_task, attempt):
        attempts.append(attempt)
        return TaskOutcome(
            status="failed",
            transient=True,
            failure_reason="temporary_transport_error",
        )

    def retry_allowed(_task):
        assert threading.get_ident() == scheduler_thread
        return False

    outcomes = execute_wave(
        [_wave_task("cancelled"), _wave_task("sibling")],
        executor=executor,
        max_parallel=2,
        cancelled=lambda: False,
        language="de",
        retry_allowed=retry_allowed,
    )

    assert attempts == [1, 1]
    assert set(outcomes) == {"cancelled", "sibling"}


@pytest.mark.parametrize("max_parallel", [1, 2])
def test_retry_stopped_before_attempt_two_preserves_first_outcome(max_parallel):
    """An admitted but unstarted retry cannot erase executed attempt one."""
    cancel = threading.Event()
    attempts: list[tuple[str, int]] = []

    def executor(task, attempt):
        attempts.append((task.task_id, attempt))
        return TaskOutcome(
            status="failed",
            transient=True,
            failure_reason="temporary_transport_error",
            usage={"prompt_tokens": 9, "completion_tokens": 2},
        )

    def retry_allowed(task):
        if task.task_id == "target":
            cancel.set()
        return True

    wave = [_wave_task("target")]
    if max_parallel > 1:
        wave.append(_wave_task("sibling"))
    outcomes = execute_wave(
        wave,
        executor=executor,
        max_parallel=max_parallel,
        cancelled=cancel.is_set,
        language="de",
        retry_allowed=retry_allowed,
    )

    assert ("target", 2) not in attempts
    assert outcomes["target"].status == "failed"
    assert outcomes["target"].failure_reason == "temporary_transport_error"
    assert outcomes["target"].usage == {
        "prompt_tokens": 9,
        "completion_tokens": 2,
    }


def test_parallel_attempts_receive_an_independent_copied_context():
    """Agent retry telemetry ContextVars cross the provider-worker boundary."""
    marker = contextvars.ContextVar("scheduler-test-marker", default="missing")
    marker.set("bound")
    observed: list[str] = []

    def executor(_task, _attempt):
        observed.append(marker.get())
        return TaskOutcome(status="completed")

    execute_wave(
        [_wave_task("a"), _wave_task("b")],
        executor=executor,
        max_parallel=2,
        cancelled=lambda: False,
        language="de",
    )

    assert observed == ["bound", "bound"]


def test_parallel_cancel_preserves_completed_and_skips_not_started() -> None:
    """Cancellation returns every observable partial outcome to the caller."""
    cancelled = threading.Event()
    both_started = threading.Barrier(2)
    executed: list[str] = []

    def executor(task, _attempt):
        executed.append(task.task_id)
        if task.task_id == "done":
            both_started.wait()
            assert cancelled.wait(2)
            return TaskOutcome(status="completed", summary="partial result")
        if task.task_id == "abort":
            both_started.wait()
            cancelled.set()
            raise AgentCancelled("cancelled")
        raise AssertionError("a queued task executed after cancellation")

    outcomes = execute_wave(
        [_wave_task("done"), _wave_task("abort"), _wave_task("not-started")],
        executor=executor,
        max_parallel=2,
        cancelled=cancelled.is_set,
        language="de",
    )

    assert set(executed) == {"done", "abort"}
    assert outcomes["done"].status == "completed"
    assert outcomes["abort"].status == "failed"
    assert outcomes["not-started"].status == "skipped"


def test_parallel_outcomes_are_observed_before_the_slowest_sibling_finishes() -> None:
    """Control rows/events follow real completion, not whole-wave completion."""
    release_slow = threading.Event()
    observed: list[str] = []

    def executor(task, _attempt):
        if task.task_id == "slow":
            assert release_slow.wait(2)
        return TaskOutcome(status="completed", summary=task.task_id)

    def observe(task, _outcome):
        observed.append(task.task_id)
        if task.task_id == "fast":
            release_slow.set()

    started = time.monotonic()
    outcomes = execute_wave(
        [_wave_task("slow"), _wave_task("fast")],
        executor=executor,
        max_parallel=2,
        cancelled=lambda: False,
        language="de",
        on_outcome=observe,
    )

    assert time.monotonic() - started < 2
    assert observed == ["fast", "slow"]
    assert set(outcomes) == {"fast", "slow"}


def test_wave_summaries_follow_the_run_language():
    """The scheduler holds no prose: its summaries come from the table."""
    cancelled = threading.Event()
    cancelled.set()

    def executor(task, attempt):  # pragma: no cover - never reached
        raise AssertionError("a cancelled wave must not execute")

    for language, expected in (
        ("de", "Wegen Abbruch nicht ausgeführt."),
        ("en", "Not executed because the run was cancelled."),
        ("", "Not executed because the run was cancelled."),
    ):
        outcomes = execute_wave(
            [_wave_task()],
            executor=executor,
            max_parallel=1,
            cancelled=cancelled.is_set,
            language=language,
        )
        assert outcomes[_wave_task().task_id].summary == expected
