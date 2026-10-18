"""Tests for the optional Prometheus ``/metrics`` endpoint (measure 2.5).

The endpoint is off by default, mounted only when
``INQTRIX_METRICS_ENABLED`` is set AND the optional ``metrics`` extra is
installed, Bearer-gated when an API key is configured, and carries only
bounded-cardinality series (no run-id/subject/session labels). The
admission counter is driven by the real 429 rejection path, so a broken
wiring (counter in a different registry than the one exposed) shows up
here as a stale zero.
"""

from __future__ import annotations

import logging
import sys
import threading
import time

import pytest

pytest.importorskip(
    "prometheus_client", reason="optional 'metrics' extra not installed"
)

from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

import inqtrix.research.web_research as web_research_module
import inqtrix.server.metrics as metrics_module
from inqtrix.settings import ServerSettings

from tests.contract._app import (
    make_contract_client,
    minimal_agent_result,
    wait_for_run_status,
)


def _sample_value(text: str, name: str, **labels: str) -> float | None:
    """Return the value of one sample, or ``None`` if absent."""
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return sample.value
    return None



def test_admission_reasons_cover_every_call_site() -> None:
    """The pre-initialised label set must name every reason a router can
    record, else the series for a new reason appears only after its first
    rejection and rate()/absent() alerts miss it."""
    import re
    from pathlib import Path

    from inqtrix.server.metrics import _ADMISSION_REASONS

    used = {
        match
        for path in Path("src/inqtrix/server").rglob("*.py")
        for match in re.findall(r'record_admission_rejected\("([a-z_]+)"\)', path.read_text())
    }
    assert used, "no call sites found"
    assert used <= set(_ADMISSION_REASONS), used - set(_ADMISSION_REASONS)

def test_metrics_absent_by_default():
    """Default settings never mount /metrics (opt-in only)."""
    with make_contract_client() as client:
        assert client.get("/metrics").status_code == 404


def test_metrics_missing_extra_stays_off(monkeypatch):
    """Flag on but the extra missing: /metrics unmounted, no crash."""
    # Force the guarded import to fail even though the extra is installed.
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    with make_contract_client(
        server_settings=ServerSettings(metrics_enabled=True)
    ) as client:
        assert client.get("/metrics").status_code == 404


def test_metrics_endpoint_exposes_run_gauges():
    """The endpoint renders the run gauges with bounded cardinality."""
    with make_contract_client(
        server_settings=ServerSettings(metrics_enabled=True)
    ) as client:
        client.get("/health")  # one templated HTTP sample
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert _sample_value(body, "inqtrix_run_queue_depth") == 0.0
    assert _sample_value(body, "inqtrix_run_active") == 0.0
    # capacity is emitted for the in-process memory backend.
    assert _sample_value(body, "inqtrix_run_capacity") is not None
    # HTTP histograms are labelled by route TEMPLATE, never a raw id.
    assert "http_request" in body


def test_metrics_bearer_gated_when_api_key_set():
    """With an API key, /metrics requires the same Bearer token."""
    with make_contract_client(
        server_settings=ServerSettings(
            api_key="secret-token-123", metrics_enabled=True
        )
    ) as client:
        assert client.get("/metrics").status_code == 401
        ok = client.get(
            "/metrics",
            headers={"Authorization": "Bearer secret-token-123"},
        )
        assert ok.status_code == 200
        assert "inqtrix_run_queue_depth" in ok.text


def test_queue_full_increments_admission_counter(monkeypatch):
    """A real queue-full 429 bumps the reason=queue_full counter.

    The counter must live in the SAME registry the endpoint exposes, so
    this goes red if the admission wiring regresses to a dead counter.
    """
    release = threading.Event()

    def blocking_run(*args, **kwargs):
        release.wait(timeout=5)
        return minimal_agent_result()

    monkeypatch.setattr(web_research_module, "run_web_graph", blocking_run)

    with make_contract_client(
        server_settings=ServerSettings(
            run_max_concurrent=1,
            run_queue_max_size=0,
            metrics_enabled=True,
        ),
    ) as client:
        first = client.post("/v1/runs", json={"question": "blockiert"})
        assert first.status_code == 202
        run_id = first.json()["run_id"]
        wait_for_run_status(client, run_id, "running")

        overflow = client.post("/v1/runs", json={"question": "zu viel"})
        release.set()
        wait_for_run_status(client, run_id, "completed")

        assert overflow.status_code == 429
        body = client.get("/metrics").text

    assert (
        _sample_value(
            body,
            "inqtrix_run_admission_rejected_total",
            reason="queue_full",
        )
        == 1.0
    )
    # The run summary polling never leaks the concrete id into a label.
    assert run_id not in body


def test_record_admission_rejected_is_noop_when_disabled(monkeypatch):
    """Off by default: the call site helper never touches a counter."""
    monkeypatch.setattr(metrics_module, "_admission_counter", None)
    # Must not raise even though metrics were never set up.
    metrics_module.record_admission_rejected("queue_full")


def test_call_metrics_feed_from_provider_wrapper(monkeypatch):
    """The provider tracing wrapper feeds llm counters/histograms/tokens —
    independent of tracing state, labeled by canonical model + feature."""
    from prometheus_client import CollectorRegistry, generate_latest

    from inqtrix.observability import metrics_defs
    from inqtrix.observability.context import bind_feature, reset_feature
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    registry = CollectorRegistry()
    holder = metrics_defs.build_call_metrics(registry)
    monkeypatch.setattr(metrics_defs, "_active", holder)

    llm = instrument_llm(FakeLLM(), provider_name="fake", policy=SUMMARY)
    token = bind_feature("knowledge")
    try:
        llm.complete_with_metadata("hallo")
    finally:
        reset_feature(token)

    text = generate_latest(registry).decode()
    assert _sample_value(
        text,
        "inqtrix_llm_requests_total",
        provider="fake",
        model="fake-1",
        operation="text_completion",
        outcome="success",
    ) == 1.0
    assert _sample_value(
        text,
        "inqtrix_llm_tokens_total",
        model="fake-1",
        feature="knowledge",
        token_type="input",
    ) > 0
    # No run ids or question text anywhere in the exposition.
    assert "hallo" not in text


def test_call_metrics_error_outcome(monkeypatch):
    from prometheus_client import CollectorRegistry, generate_latest

    from inqtrix.observability import metrics_defs
    from inqtrix.observability.provider_tracing import instrument_llm
    from tests.test_provider_tracing import SUMMARY, FakeLLM

    registry = CollectorRegistry()
    monkeypatch.setattr(
        metrics_defs, "_active", metrics_defs.build_call_metrics(registry)
    )

    class _BoomLLM(FakeLLM):
        def complete_with_metadata(self, *args, **kwargs):
            raise TimeoutError("provider timed out")

    llm = instrument_llm(_BoomLLM(), provider_name="fake", policy=SUMMARY)
    with pytest.raises(TimeoutError):
        llm.complete_with_metadata("x")
    text = generate_latest(registry).decode()
    assert _sample_value(
        text,
        "inqtrix_llm_requests_total",
        provider="fake",
        model="unknown",
        operation="text_completion",
        outcome="timeout",
    ) == 1.0


def test_call_outcomes_stay_inside_the_bounded_label_set(monkeypatch, caplog):
    """Known outcomes keep their label; anything else is counted as error,
    with one WARNING, instead of minting a new series."""
    from prometheus_client import CollectorRegistry, generate_latest

    from inqtrix.observability import metrics_defs

    registry = CollectorRegistry()
    holder = metrics_defs.build_call_metrics(registry)
    monkeypatch.setattr(metrics_defs, "_unknown_outcome_warned", False)
    llm = {"provider": "fake", "model": "fake-1", "operation": "chat", "feature": "chat"}

    with caplog.at_level("WARNING", logger=metrics_defs.log.name):
        holder.observe_llm(outcome="incomplete", duration_seconds=0.1, **llm)
        holder.observe_llm(outcome="surprise", duration_seconds=0.1, **llm)
        holder.observe_llm(outcome="another_surprise", duration_seconds=0.1, **llm)
        holder.observe_search(
            provider="fake", engine="web", outcome="surprise", duration_seconds=0.1
        )

    text = generate_latest(registry).decode()
    llm_labels = {"provider": "fake", "model": "fake-1", "operation": "chat"}
    assert _sample_value(
        text, "inqtrix_llm_requests_total", outcome="incomplete", **llm_labels
    ) == 1.0
    assert _sample_value(
        text, "inqtrix_llm_requests_total", outcome="error", **llm_labels
    ) == 2.0
    assert _sample_value(
        text, "inqtrix_search_requests_total", provider="fake", engine="web", outcome="error"
    ) == 1.0
    assert "surprise" not in text
    assert [record.getMessage() for record in caplog.records] == [
        "Metric label guard: provider call outcome 'surprise' is not in the bounded "
        "label set; such calls are counted as outcome=\"error\"."
    ]


def test_retrieval_indexing_and_worker_job_feeds(monkeypatch):
    """2: the retrieval/indexing/worker seams feed the shared holder."""
    from prometheus_client import CollectorRegistry, generate_latest

    from inqtrix.observability import metrics_defs

    registry = CollectorRegistry()
    monkeypatch.setattr(
        metrics_defs, "_active", metrics_defs.build_call_metrics(registry)
    )

    import time as time_module

    from inqtrix.knowledge.retrieval import _observe_retrieval_step
    from inqtrix.services.indexing_service import _count_indexed_document
    from inqtrix.worker.indexing_loop import _count_worker_job

    _observe_retrieval_step("rerank", time_module.monotonic())
    _count_indexed_document("completed")
    _count_worker_job("indexing", "terminal")

    text = generate_latest(registry).decode()
    assert _sample_value(
        text,
        "inqtrix_retrieval_duration_seconds_count",
        step="rerank",
    ) == 1.0
    assert _sample_value(
        text, "inqtrix_indexing_documents_total", outcome="completed"
    ) == 1.0
    assert _sample_value(
        text, "inqtrix_worker_jobs_total", loop="indexing", outcome="terminal"
    ) == 1.0


@pytest.mark.parametrize(
    ("error_type", "labels"),
    [
        ("contextualization_provider_circuit_open", ("contextualization", "circuit_open")),
        ("contextualization_circuit_state_unavailable", ("contextualization", "unavailable")),
        ("embedding_provider_rate_limited", ("embedding", "rate_limited")),
        ("vector_store_timeout", ("vector_store", "timeout")),
        ("quota_dependency_error", ("quota", "error")),
        ("validation_error", ("validation", "error")),
        ("dependency_error", ("other", "error")),
        ("brand_new_failure", ("other", "other")),
    ],
)
def test_pause_labels_come_from_the_error_type_inside_bounded_sets(error_type, labels):
    from inqtrix.observability.metrics_defs import pause_labels

    assert pause_labels(error_type) == labels


def test_indexing_pauses_and_automatic_resumes_feed_the_shared_holder(monkeypatch):
    """A landed pause and each step of an automatic-resume streak are counted,
    labelled by source and kind, never by the raw error type."""
    from prometheus_client import CollectorRegistry, generate_latest

    from inqtrix.indexing_auto_resume import AutoResumePolicy
    from inqtrix.observability import metrics_defs
    from inqtrix.server.indexing import IndexingJobStore

    registry = CollectorRegistry()
    monkeypatch.setattr(
        metrics_defs, "_active", metrics_defs.build_call_metrics(registry)
    )
    jobs = IndexingJobStore(
        max_concurrent=1,
        max_queue_size=5,
        completed_ttl_seconds=30,
        event_buffer_size=50,
        history_limit=10,
        auto_resume=AutoResumePolicy(max_attempts=2),
        clock=lambda: 1_000.0,
        random_source=lambda: 0.5,
    )

    def work(handle):
        handle.begin(1)
        handle.pause_dependency(
            "embedding provider throttled", error_type="embedding_provider_rate_limited"
        )

    job_id = jobs.submit(
        collection_id="kc_metrics",
        collection_name="Metrics",
        embedding_model="stub-embed-8",
        work=work,
    )["job_id"]

    def wait_for_pause(count: int) -> None:
        deadline = time.time() + 5
        while time.time() < deadline:
            if jobs.get(job_id)["status"] == "paused_dependency" and sum(
                1 for event in jobs.subscribe(job_id).replay
                if event["type"] == "inqtrix.index.paused_dependency"
            ) == count:
                return
            time.sleep(0.01)
        pytest.fail(f"pause {count} did not land: {jobs.get(job_id)['status']}")

    def settled() -> None:
        # Resume only once the pausing run has ended: its slot (one) frees
        # then, so a sentinel job that completes proves it.
        sentinel = jobs.submit(
            collection_id="kc_sentinel",
            collection_name="Sentinel",
            embedding_model="stub-embed-8",
            work=lambda handle: (handle.begin(0), handle.complete()),
        )["job_id"]
        deadline = time.time() + 5
        while jobs.get(sentinel)["status"] != "completed" and time.time() < deadline:
            time.sleep(0.01)
        assert jobs.get(sentinel)["status"] == "completed"

    for count in (1, 2):
        wait_for_pause(count)
        settled()
        jobs.resume(job_id, automatic=True, now=10_000.0)
    wait_for_pause(3)

    text = generate_latest(registry).decode()
    assert _sample_value(
        text,
        "inqtrix_indexing_pauses_total",
        status="paused_dependency",
        source="embedding",
        kind="rate_limited",
    ) == 3.0
    assert {
        outcome: _sample_value(text, "inqtrix_indexing_auto_resumes_total", outcome=outcome)
        for outcome in ("planned", "resumed", "stopped")
    } == {"planned": 2.0, "resumed": 2.0, "stopped": 1.0}
    assert "embedding_provider_rate_limited" not in text


def test_retrieval_step_noop_without_active_metrics():
    """No holder published (metrics off) → seams stay silent no-ops."""
    import time as time_module

    from inqtrix.knowledge.retrieval import _observe_retrieval_step
    from inqtrix.observability import metrics_defs
    from inqtrix.services.indexing_service import _count_indexed_document

    previous = metrics_defs._active
    metrics_defs._active = None
    try:
        _observe_retrieval_step("hybrid_search", time_module.monotonic())
        _count_indexed_document("failed")
    finally:
        metrics_defs._active = previous


def test_worker_metrics_off_while_metrics_enabled_warns_once(monkeypatch, caplog):
    """Metrics globally on but the worker port left at 0 is a silent
    half-configuration: the API exports series while the worker — where
    every run, LLM call and retrieval step happens — silently exports
    none. Warn once per process so the gap is visible without spamming.
    """
    from types import SimpleNamespace

    from inqtrix.worker import metrics as worker_metrics

    monkeypatch.setattr(worker_metrics, "_off_while_enabled_warned", False)
    settings = SimpleNamespace(
        queue=SimpleNamespace(worker_metrics_port=0),
        server=SimpleNamespace(metrics_enabled=True),
    )

    with caplog.at_level("WARNING", logger="inqtrix"):
        assert worker_metrics.start_worker_metrics(settings) is False
        assert worker_metrics.start_worker_metrics(settings) is False

    warnings = [
        rec for rec in caplog.records
        if "INQTRIX_WORKER_METRICS_PORT" in rec.message
    ]
    assert len(warnings) == 1, "the gap must be announced exactly once"
    assert "INQTRIX_METRICS_ENABLED=true" in warnings[0].message


def test_worker_metrics_fully_off_stays_silent(monkeypatch, caplog):
    """Metrics off everywhere is a deliberate default, not a
    misconfiguration — it must not produce a warning."""
    from types import SimpleNamespace

    from inqtrix.worker import metrics as worker_metrics

    monkeypatch.setattr(worker_metrics, "_off_while_enabled_warned", False)
    settings = SimpleNamespace(
        queue=SimpleNamespace(worker_metrics_port=0),
        server=SimpleNamespace(metrics_enabled=False),
    )

    with caplog.at_level("WARNING", logger="inqtrix"):
        assert worker_metrics.start_worker_metrics(settings) is False

    assert not [
        rec for rec in caplog.records
        if "INQTRIX_WORKER_METRICS_PORT" in rec.message
    ]


def test_worker_metrics_bind_failure_stays_off(monkeypatch, caplog):
    """An unbindable port logs ERROR, never crashes the
    worker, and leaves the holder unset (no phantom series)."""
    import socket
    from types import SimpleNamespace

    from inqtrix.observability import metrics_defs
    from inqtrix.worker.metrics import start_worker_metrics

    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    monkeypatch.setattr(metrics_defs, "_active", None)
    settings = SimpleNamespace(
        queue=SimpleNamespace(worker_metrics_port=port),
        server=SimpleNamespace(metrics_enabled=True),
    )
    try:
        with caplog.at_level("ERROR", logger="inqtrix"):
            assert start_worker_metrics(settings) is False
    finally:
        blocker.close()
    assert metrics_defs.active_metrics() is None
    assert any("could not bind port" in rec.message for rec in caplog.records)


def test_metric_model_label_fallback_cap(monkeypatch):
    """Non-catalog model labels are admission-capped;
    an unbounded spray collapses to "other" instead of minting series."""
    from inqtrix.observability import metrics_defs

    monkeypatch.setattr(metrics_defs, "_fallback_model_labels", set())
    monkeypatch.setattr(metrics_defs, "_fallback_cap_warned", False)
    names = [
        f"unknownmodel{chr(97 + i % 26)}{chr(97 + i // 26)}"
        for i in range(metrics_defs._FALLBACK_MODEL_LABEL_LIMIT + 10)
    ]
    labels = [metrics_defs.metric_model_label(name) for name in names]
    assert "other" in labels
    assert len(set(labels)) <= metrics_defs._FALLBACK_MODEL_LABEL_LIMIT + 1
    # An early-admitted label stays stable after the cap engaged.
    assert metrics_defs.metric_model_label(names[0]) == labels[0] != "other"


def test_queue_wait_observed_only_by_runs_loop():
    """Run_queue_wait is a RUN metric — the shared base
    loop and the indexing loop must not feed it."""
    from inqtrix.worker.indexing_loop import IndexingWorkerLoop
    from inqtrix.worker.loop import BaseWorkerLoop, WorkerLoop

    assert BaseWorkerLoop._observes_queue_wait is False
    assert WorkerLoop._observes_queue_wait is True
    assert IndexingWorkerLoop._observes_queue_wait is False


def test_fenced_run_handle_records_terminal_outcome():
    """The landed terminal is the honest segment outcome;
    a fenced write leaves it None so the loop skips the observation."""
    import threading

    from inqtrix.worker.loop import FencedRunHandle

    class _Store:
        def complete(self, run_id, result, snapshot=None, fence_attempt=None):
            return True

        def fail(self, run_id, message, error_type="server_error", fence_attempt=None):
            return True

        def mark_cancelled(self, run_id, reason="cancelled", fence_attempt=None):
            return False

        def emit(self, *args, **kwargs):
            pass

    handle = FencedRunHandle(_Store(), "run_x", threading.Event(), 1)
    assert handle.terminal_outcome is None
    handle.complete({})
    assert handle.terminal_outcome == "completed"

    handle2 = FencedRunHandle(_Store(), "run_y", threading.Event(), 1)
    handle2.fail("boom")
    assert handle2.terminal_outcome == "failed"

    fenced = FencedRunHandle(_Store(), "run_z", threading.Event(), 1)
    fenced.cancel()
    assert fenced.terminal_landed is False
    assert fenced.terminal_outcome is None


def test_map_cancellable_propagates_feature_context():
    """Research fan-out threads must inherit the submitting
    thread's contextvars (feature label) via copy_context."""
    from inqtrix.nodes import _map_cancellable
    from inqtrix.observability.context import (
        bind_feature,
        current_feature,
        reset_feature,
    )

    token = bind_feature("research")
    try:
        results = _map_cancellable(
            {},
            lambda item: current_feature(),
            ["a", "b", "c"],
            max_workers=2,
            operation_label="test",
        )
    finally:
        reset_feature(token)
    assert results == ["research", "research", "research"]


def test_document_revision_job_counts_single_failed_sample(monkeypatch):
    """The primary ingest path counts exactly one
    failed sample at the shared chokepoint — in-process and worker alike
    — and pause-class errors stay uncounted."""
    from types import SimpleNamespace

    from prometheus_client import CollectorRegistry, generate_latest

    import inqtrix.services.indexing_service as indexing_service
    from inqtrix.knowledge.contextualize import (
        ContextualizationDependencyError,
    )
    from inqtrix.observability import metrics_defs

    registry = CollectorRegistry()
    monkeypatch.setattr(
        metrics_defs, "_active", metrics_defs.build_call_metrics(registry)
    )

    def _boom_steps(handle, *, counted, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(
        indexing_service, "_run_document_revision_steps", _boom_steps
    )
    with pytest.raises(RuntimeError):
        indexing_service.execute_document_revision_job(
            SimpleNamespace(),
            knowledge_service=SimpleNamespace(),
            document_id="kd_x",
            revision_id="rev_x",
        )
    text = generate_latest(registry).decode()
    assert _sample_value(
        text, "inqtrix_indexing_documents_total", outcome="failed"
    ) == 1.0

    def _pause_steps(handle, *, counted, **kwargs):
        raise ContextualizationDependencyError(
            error_type="contextualization_provider_timeout"
        )

    monkeypatch.setattr(
        indexing_service, "_run_document_revision_steps", _pause_steps
    )
    with pytest.raises(ContextualizationDependencyError):
        indexing_service.execute_document_revision_job(
            SimpleNamespace(),
            knowledge_service=SimpleNamespace(),
            document_id="kd_x",
            revision_id="rev_x",
        )
    text = generate_latest(registry).decode()
    assert _sample_value(
        text, "inqtrix_indexing_documents_total", outcome="failed"
    ) == 1.0  # unchanged: the pause did not count

    def _late_failure_steps(handle, *, counted, **kwargs):
        counted["completed"] = True
        metrics_defs.active_metrics().count_indexed_documents(
            outcome="completed"
        )
        raise RuntimeError("terminal write failed after completion")

    monkeypatch.setattr(
        indexing_service, "_run_document_revision_steps", _late_failure_steps
    )
    with pytest.raises(RuntimeError):
        indexing_service.execute_document_revision_job(
            SimpleNamespace(),
            knowledge_service=SimpleNamespace(),
            document_id="kd_x",
            revision_id="rev_x",
        )
    text = generate_latest(registry).decode()
    assert _sample_value(
        text, "inqtrix_indexing_documents_total", outcome="failed"
    ) == 1.0  # no second failed sample for a completed document
    assert _sample_value(
        text, "inqtrix_indexing_documents_total", outcome="completed"
    ) == 1.0


def test_map_cancellable_plain_path_cancels_queued_on_failure():
    """The no-cancel-event path keeps ex.map's
    cancel-queued-remainder semantics after the contextvars fix."""
    import threading

    from inqtrix.nodes import _map_cancellable

    executed: list[int] = []
    release = threading.Event()
    lock = threading.Lock()

    def fn(item):
        with lock:
            executed.append(item)
        if item == 0:
            raise RuntimeError("first item fails")
        release.wait(timeout=5)
        return item

    with pytest.raises(RuntimeError, match="first item fails"):
        try:
            _map_cancellable(
                {},
                fn,
                list(range(8)),
                max_workers=2,
                operation_label="test",
            )
        finally:
            release.set()
    # ex.map parity: the queued remainder was cancelled, not executed.
    assert len(executed) < 8


class _Captured(logging.Handler):
    """Collect records straight off the logger.

    The application logger does not propagate to root, so the usual capture
    fixture sees nothing; attaching here reads what actually gets emitted.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _scrape_reports(handler: _Captured) -> list[logging.LogRecord]:
    return [r for r in handler.records if "Prometheus scrape took" in str(r.msg)]


def test_slow_scrape_names_which_side_was_slow(monkeypatch):
    """A slow scrape must say whether it waited for a thread or for work.

    Both causes look identical from outside — the client sees one duration.
    Without the split an operator cannot tell a contended thread pool from a
    slow database, and the two need opposite remedies.
    """
    # The threshold decides whether the split is worth reporting; zero makes
    # every scrape report so the accounting itself can be checked.
    monkeypatch.setattr(metrics_module, "_SLOW_SCRAPE_SECONDS", 0.0)
    handler = _Captured()
    logger = logging.getLogger("inqtrix")
    logger.addHandler(handler)
    try:
        with make_contract_client(
            server_settings=ServerSettings(metrics_enabled=True)
        ) as client:
            assert client.get("/metrics").status_code == 200
    finally:
        logger.removeHandler(handler)

    reported = _scrape_reports(handler)
    assert reported, "a slow scrape must be explained, not just be slow"
    total, waited, worked = reported[-1].args
    # The halves must account for the whole, or the split is decorative.
    assert abs((waited + worked) - total) < 1e-6
    assert waited >= 0.0 and worked > 0.0


def test_scrape_split_can_see_a_starved_pool(monkeypatch):
    """The instrument must be able to SEE thread starvation, not report zero.

    An always-zero wait reading would make a healthy pool indistinguishable
    from a broken probe. This occupies the shared pool for a known interval
    and requires the split to blame the wait rather than the collection.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    monkeypatch.setattr(metrics_module, "_SLOW_SCRAPE_SECONDS", 0.0)
    handler = _Captured()
    logger = logging.getLogger("inqtrix")
    logger.addHandler(handler)

    hold_for = 0.5

    async def scenario() -> int:
        loop = asyncio.get_running_loop()
        pool = ThreadPoolExecutor(max_workers=1)
        loop.set_default_executor(pool)
        occupied = threading.Barrier(2, timeout=10)

        def hold() -> None:
            occupied.wait()
            time.sleep(hold_for)

        held = loop.run_in_executor(pool, hold)
        # Releasing the barrier from here puts the worker inside hold(), so
        # the single slot is taken for the next hold_for seconds.
        occupied.wait()

        with make_contract_client(
            server_settings=ServerSettings(metrics_enabled=True)
        ) as client:
            app = client.app
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://metrics.test"
        ) as request_client:
            response = await request_client.get("/metrics")
        await held
        pool.shutdown(wait=False)
        return response.status_code

    try:
        assert asyncio.run(scenario()) == 200
    finally:
        logger.removeHandler(handler)

    reported = _scrape_reports(handler)
    assert reported
    _total, waited, worked = reported[-1].args
    assert waited > worked, (
        f"a scrape queued behind a full pool must blame the wait "
        f"(waited={waited:.3f}s, worked={worked:.3f}s)"
    )
