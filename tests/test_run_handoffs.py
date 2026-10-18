"""Committed run hand-offs remain exactly-once under parallel draining.

Run cancellation uses one canonical root-to-leaf lock order, so no lock
inversion needs a deadlock retry; this module pins the concurrent dispatch
contract.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from queue import SimpleQueue

from inqtrix.runs.postgres_store import PostgresRunStore


def _make_store() -> PostgresRunStore:
    """Build the attributes used by the committed hand-off drain."""
    store = object.__new__(PostgresRunStore)
    store._lock = threading.RLock()
    store._local = {}
    store._swept_waiting = SimpleQueue()
    store._parents_to_wake = SimpleQueue()
    store._failed_cascades = SimpleQueue()
    return store


def test_parallel_parent_wake_drains_each_committed_handoff_once() -> None:
    """Concurrent terminal callers cannot consume the same wake twice."""
    store = _make_store()
    calls: list[str] = []
    calls_lock = threading.Lock()

    class DispatchQueue:
        def enqueue(self, *, run_id: str, tenant_id: str) -> None:
            assert tenant_id == "default"
            with calls_lock:
                calls.append(run_id)

    store._dispatch_queue = DispatchQueue()
    expected = [f"parent-{index}" for index in range(200)]
    for run_id in expected:
        store._parents_to_wake.put(run_id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: store._dispatch_woken_parents(), range(8)))

    assert sorted(calls) == sorted(expected)
