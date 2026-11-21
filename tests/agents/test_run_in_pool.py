"""Independent items run concurrently and fold in ITEM order.

:func:`~inqtrix.agents.scheduler.run_in_pool` is the concurrency shape
below the agent's own step order: the windows of a document analysis run
through it. Its items are independent by construction, so what must not
change is the observable order of the results, the bound on how many run
at once, and the stop after the first failure.
"""

import threading
from typing import Any

from inqtrix.agents.scheduler import run_in_pool


def _run(items: list[str], run_one: Any, max_parallel: int) -> list[Any]:
    return run_in_pool(
        items,
        run_one,
        max_parallel,
        skipped=lambda index, item: ("skipped", index, item),
        thread_name_prefix="inqtrix-test-pool",
    )


def test_results_fold_in_item_order_not_completion_order() -> None:
    """The regression guard: later items may finish FIRST."""
    started = threading.Barrier(4)

    def run_one(index: int, item: str) -> str:
        # Every worker waits for all four, then the LAST index returns
        # first — completion order is deliberately reversed.
        started.wait(timeout=5)
        return f"A{index}"

    assert _run(["q1", "q2", "q3", "q4"], run_one, 4) == ["A1", "A2", "A3", "A4"]


def test_a_single_item_takes_the_sequential_path() -> None:
    """No pool for the trivial case."""
    threads: set[int] = set()

    def run_one(index: int, item: str) -> str:
        threads.add(threading.get_ident())
        return item

    _run(["nur eine"], run_one, 4)
    assert threads == {threading.get_ident()}


def test_cap_of_one_restores_the_sequential_loop() -> None:
    """The documented escape hatch must really be sequential."""
    threads: set[int] = set()

    def run_one(index: int, item: str) -> str:
        threads.add(threading.get_ident())
        return item

    _run(["a", "b", "c"], run_one, 1)
    assert threads == {threading.get_ident()}


def test_the_cap_bounds_how_many_run_at_once() -> None:
    """Without a bound one run could out-demand the whole model lane."""
    lock = threading.Lock()
    live = 0
    peak = 0

    def run_one(index: int, item: str) -> str:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            threading.Event().wait(0.02)
            return item
        finally:
            with lock:
                live -= 1

    _run([f"q{n}" for n in range(8)], run_one, 3)
    assert peak <= 3


def test_every_item_is_run_exactly_once_with_its_own_index() -> None:
    seen: list[tuple[int, str]] = []
    lock = threading.Lock()

    def run_one(index: int, item: str) -> str:
        with lock:
            seen.append((index, item))
        return item

    _run(["a", "b", "c"], run_one, 3)
    assert sorted(seen) == [(1, "a"), (2, "b"), (3, "c")]


def test_a_sibling_that_raises_stops_items_that_have_not_started() -> None:
    """The sequential loop stopped at the first exception; the pool must too.

    Without this, a capability that is simply down would be paid for once
    per item instead of once per call.
    """
    started: list[int] = []
    barrier = threading.Event()

    def run_one(index: int, item: str) -> str:
        started.append(index)
        if index == 1:
            raise RuntimeError("capability down")
        # Hold the second worker until the first has raised, so the two
        # remaining items decide AFTER the failure is known.
        barrier.wait(timeout=5.0)
        return item

    try:
        _run(["a", "b", "c", "d"], run_one, max_parallel=2)
    except RuntimeError:
        pass
    finally:
        barrier.set()

    # 1 raised and 2 was already in flight; 3 and 4 never started.
    assert 1 in started
    assert 3 not in started and 4 not in started
