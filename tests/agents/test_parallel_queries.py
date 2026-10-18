"""A task's sub-queries run concurrently and fold in QUERY order.

In a plain `for` loop, the queries of one retrieval task never overlap,
so one slow task holds up the whole execution phase while its sibling
tasks have long finished.

They are independent by construction: each builds its prompt from
``(task, query)`` alone and reads nothing a sibling produced. Task
DEPENDENCIES live on ``depends_on`` and stay sequenced by the wave
scheduler; nothing here touches that.

What must NOT change is the observable order: the answer text, which
duplicate reference wins, and which failure code carries.
"""

import threading

from inqtrix.agents.algorithm import _QueryOutcome, _execute_queries


def _outcome(index: int, query: str, **kwargs: object) -> _QueryOutcome:
    return _QueryOutcome(index=index, query=query, **kwargs)  # type: ignore[arg-type]


def test_results_fold_in_query_order_not_completion_order() -> None:
    """The regression guard: later queries may finish FIRST."""
    started = threading.Barrier(4)

    def run_one(index: int, query: str) -> _QueryOutcome:
        # Every worker waits for all four, then the LAST index returns
        # first — completion order is deliberately reversed.
        started.wait(timeout=5)
        return _outcome(index, query, answer=f"A{index}")

    outcomes = _execute_queries(["q1", "q2", "q3", "q4"], run_one, 4)
    assert [outcome.answer for outcome in outcomes] == ["A1", "A2", "A3", "A4"]


def test_a_single_query_takes_the_sequential_path() -> None:
    """No pool for the trivial case (scheduler precedent)."""
    threads: set[int] = set()

    def run_one(index: int, query: str) -> _QueryOutcome:
        threads.add(threading.get_ident())
        return _outcome(index, query)

    _execute_queries(["nur eine"], run_one, 4)
    assert threads == {threading.get_ident()}


def test_cap_of_one_restores_the_sequential_loop() -> None:
    """The documented escape hatch must really be sequential."""
    threads: set[int] = set()

    def run_one(index: int, query: str) -> _QueryOutcome:
        threads.add(threading.get_ident())
        return _outcome(index, query)

    _execute_queries(["a", "b", "c"], run_one, 1)
    assert threads == {threading.get_ident()}


def test_the_cap_bounds_how_many_run_at_once() -> None:
    """Without a bound one run could out-demand the whole model lane."""
    lock = threading.Lock()
    live = 0
    peak = 0

    def run_one(index: int, query: str) -> _QueryOutcome:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            threading.Event().wait(0.02)
            return _outcome(index, query)
        finally:
            with lock:
                live -= 1

    _execute_queries([f"q{n}" for n in range(8)], run_one, 3)
    assert peak <= 3


def test_every_query_is_run_exactly_once_with_its_own_index() -> None:
    seen: list[tuple[int, str]] = []
    lock = threading.Lock()

    def run_one(index: int, query: str) -> _QueryOutcome:
        with lock:
            seen.append((index, query))
        return _outcome(index, query)

    _execute_queries(["a", "b", "c"], run_one, 3)
    assert sorted(seen) == [(1, "a"), (2, "b"), (3, "c")]


def test_a_sibling_that_raises_stops_queries_that_have_not_started() -> None:
    """The sequential loop stopped at the first exception; the pool must too.

    Without this, a capability that is simply down would be paid for once
    per query instead of once per task.
    """
    started: list[int] = []
    barrier = threading.Event()

    def run_one(index: int, query: str) -> _QueryOutcome:
        started.append(index)
        if index == 1:
            raise RuntimeError("capability down")
        # Hold the second worker until the first has raised, so the two
        # remaining queries decide AFTER the failure is known.
        barrier.wait(timeout=5.0)
        return _QueryOutcome(index=index, query=query)

    queries = ["a", "b", "c", "d"]
    try:
        _execute_queries(queries, run_one, max_parallel=2)
    except RuntimeError:
        pass
    finally:
        barrier.set()

    # 1 raised and 2 was already in flight; 3 and 4 never started.
    assert 1 in started
    assert 3 not in started and 4 not in started
