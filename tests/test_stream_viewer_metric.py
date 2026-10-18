"""The concurrent-viewer histogram feeding the shared-poller decision.

A shared event poller per entity is worth building only if multiple
viewers per run occur in practice. This instrument is that evidence
gate: each new subscription observes the concurrency its entity just
reached — per job kind, deliberately without entity ids.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from inqtrix.observability.metrics_defs import (
    active_metrics,
    set_active_metrics,
)
from inqtrix.runs.durable_store import DurableJobStoreBase


class _Recorder:
    def __init__(self) -> None:
        self.observed: list[tuple[str, int]] = []

    def observe_stream_viewers(self, *, job_kind: str, concurrent: int) -> None:
        self.observed.append((job_kind, concurrent))


class _InertThread:
    def start(self) -> None:
        return None


def _store_stub() -> SimpleNamespace:
    return SimpleNamespace(
        _lock=threading.Lock(),
        _closing=False,
        _closed=False,
        _subscriptions=set(),
        _job_kind="Test job",
    )


class _Subscription:
    """Hashable stand-in (SimpleNamespace defines __eq__ and loses __hash__)."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id


def _subscribe(store, entity_id: str) -> None:
    DurableJobStoreBase._start_subscription(
        store, _Subscription(entity_id), _InertThread()
    )


def test_each_join_observes_the_entity_concurrency() -> None:
    recorder = _Recorder()
    previous = active_metrics()
    set_active_metrics(recorder)  # type: ignore[arg-type]
    try:
        store = _store_stub()
        _subscribe(store, "run_a")
        _subscribe(store, "run_a")
        _subscribe(store, "run_b")
        _subscribe(store, "run_a")
    finally:
        set_active_metrics(previous)
    assert recorder.observed == [
        ("Test job", 1),
        ("Test job", 2),
        ("Test job", 1),
        ("Test job", 3),
    ], (
        "the histogram must see the concurrency the entity reached at "
        "each join — per entity, never summed across entities"
    )


def test_without_active_metrics_subscription_still_registers() -> None:
    previous = active_metrics()
    set_active_metrics(None)
    try:
        store = _store_stub()
        _subscribe(store, "run_a")
    finally:
        set_active_metrics(previous)
    assert len(store._subscriptions) == 1


class _ConstructorStore:
    """Records registrations; never starts the poller thread."""

    def __init__(self) -> None:
        self.registered: list[tuple] = []

    def _start_subscription(self, subscription, thread) -> None:
        self.registered.append((subscription, thread))


def _polling_subscription(store, *, stream: bool | None):
    from inqtrix.runs.durable_store import PollingJobSubscription

    kwargs = {} if stream is None else {"stream": stream}
    return PollingJobSubscription(
        store,
        "run_a",
        "default",
        [{"sequence": 1, "type": "inqtrix.run.progress"}],
        terminal_events=frozenset({"inqtrix.run.completed"}),
        thread_label="test-events",
        **kwargs,
    )


def test_one_shot_replay_read_never_registers_a_viewer() -> None:
    """``stream=False`` (the JSON polling fallback) is a replay read.

    Registration is what feeds the viewer histogram AND spawns the
    poller thread; a ~3s polling cadence counted as stream joins would
    drown the 5b evidence gate in per-poll artifacts and read genuine
    overlap as 1. Killing mutant: dropping ``and stream`` from the
    constructor's registration gate.
    """
    store = _ConstructorStore()
    subscription = _polling_subscription(store, stream=False)
    assert store.registered == [], (
        "a one-shot replay read must never register as a stream viewer"
    )
    assert subscription._thread is None, "and must not build a poller thread"


def test_streaming_subscription_default_still_registers() -> None:
    # Control for the flag: the default (indexing store passes nothing)
    # keeps the pre-flag behavior — non-terminal replay registers once.
    store = _ConstructorStore()
    subscription = _polling_subscription(store, stream=None)
    assert len(store.registered) == 1
    assert subscription._thread is not None


def test_the_real_histogram_carries_no_entity_labels() -> None:
    prometheus = __import__("importlib").import_module("prometheus_client")
    from inqtrix.observability.metrics_defs import build_call_metrics

    registry = prometheus.CollectorRegistry()
    metrics = build_call_metrics(registry)
    metrics.observe_stream_viewers(job_kind="Run", concurrent=2)
    metrics.observe_stream_viewers(job_kind="Run", concurrent=1)
    rendered = prometheus.generate_latest(registry).decode()
    series = [
        line
        for line in rendered.splitlines()
        if line.startswith("inqtrix_stream_concurrent_viewers")
    ]
    assert series, "the histogram must render"
    assert any('job_kind="Run"' in line for line in series)
    assert any('le="1.0"' in line for line in series)
    # The label SET is the cardinality contract: job_kind only.
    assert all("entity" not in line and "run_id" not in line for line in series)
