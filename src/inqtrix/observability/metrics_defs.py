"""Shared Prometheus metric definitions.

ONE neutral module owns every metric name, label set, and bucket layout
so the API server and the worker expose IDENTICAL series from their own
per-process registries (the server keeps its per-app registry contract
from ``server/metrics.py``; the worker builds a fresh registry — never
the global default, never shared objects).

Cardinality contract (same as the existing HTTP metrics): labels carry
BOUNDED vocabularies only — provider/operation/outcome enums, canonical
model ids via ``model_cards`` normalization, feature names. Never run
ids, user ids, questions, or raw region-variant model strings.

Deep call sites (provider wrappers, retrieval, worker loops) reach the
process holder through :func:`set_active_metrics` /
:func:`active_metrics` — the exact pattern of the active content
policy. With metrics off the holder stays ``None`` and every recording
helper is a cheap no-op; a failing metrics emit must never touch the
call it measures (fail-safe, WARNING once — never fail-silent).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("inqtrix")

# Provider calls regularly run for minutes (deep research answers) —
# buckets must resolve the long tail past 120s.
LLM_LATENCY_BUCKETS = (
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    40.0,
    80.0,
    120.0,
    240.0,
    480.0,
    # The configured per-call ceilings (reasoning/editor timeouts) sit
    # at 600s — the top bucket must resolve up to that tail.
    600.0,
)
SEARCH_LATENCY_BUCKETS = (
    0.25,
    0.5,
    1.0,
    2.0,
    5.0,
    10.0,
    20.0,
    40.0,
    80.0,
    # Logical search operations may run against a 600s budget.
    180.0,
    600.0,
)
RETRIEVAL_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
RUN_DURATION_BUCKETS = (
    1.0,
    5.0,
    15.0,
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1200.0,
    2400.0,
)
QUEUE_WAIT_BUCKETS = (0.05, 0.25, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0)

# Bounded outcome vocabulary for provider calls; anything unexpected
# collapses to "error" instead of minting new series. "incomplete" is a
# billed response the provider stopped early (content filter, length).
_CALL_OUTCOMES = ("success", "incomplete", "timeout", "cancelled", "error")

_classify_failure_warned = False
_unknown_outcome_warned = False

# Where an indexing pause came from and what kind of failure it was, both
# derived from the pause's error type so the label sets stay bounded without
# a second copy of the error vocabulary.
_PAUSE_SOURCES = (
    ("contextualization_", "contextualization"),
    ("embedding_provider_", "embedding"),
    ("vector_store_", "vector_store"),
    ("quota_", "quota"),
    ("validation_", "validation"),
)
_PAUSE_KINDS = ("circuit_open", "rate_limited", "timeout", "unavailable", "error")


def pause_labels(error_type: str) -> tuple[str, str]:
    """Bounded ``(source, kind)`` labels for an indexing pause's error type."""
    source = next(
        (name for prefix, name in _PAUSE_SOURCES if error_type.startswith(prefix)),
        "other",
    )
    kind = next((kind for kind in _PAUSE_KINDS if error_type.endswith(kind)), "other")
    return source, kind


def _call_outcome_label(outcome: str) -> str:
    """Keep a provider call's outcome inside the bounded label set."""
    global _unknown_outcome_warned
    if outcome in _CALL_OUTCOMES:
        return outcome
    if not _unknown_outcome_warned:
        _unknown_outcome_warned = True
        log.warning(
            "Metric label guard: provider call outcome %r is not in the bounded "
            "label set; such calls are counted as outcome=\"error\".",
            outcome,
        )
    return "error"


# Hard admission cap for NON-catalog model labels: the card catalog is
# bounded by construction, but per-request model overrides are free-form
# strings — without a cap one client could mint unlimited series.
_FALLBACK_MODEL_LABEL_LIMIT = 50
_fallback_model_labels: set[str] = set()
_fallback_cap_warned = False


def metric_model_label(model_id: str | None) -> str:
    """Canonical model label with a hard cardinality guard.

    Card matches use the card id (region/version variants collapse).
    Unmatched ids fall back to the region/version-stripped normal form,
    admitted into a bounded per-process set: legitimate operator-config
    names register early and stay stable, while an unbounded spray of
    per-request override strings collapses to ``other`` after the cap
    (with one WARNING so the collapse is never silent).
    """
    global _fallback_cap_warned
    if not model_id:
        return "unknown"
    from inqtrix.model_cards import _normalize, resolve_model_card

    card = resolve_model_card(model_id)
    if card is not None:
        return card.id
    normalized = _normalize(model_id) or "unknown"
    if normalized in _fallback_model_labels:
        return normalized
    if len(_fallback_model_labels) < _FALLBACK_MODEL_LABEL_LIMIT:
        _fallback_model_labels.add(normalized)
        return normalized
    if not _fallback_cap_warned:
        _fallback_cap_warned = True
        log.warning(
            "Metric label guard: more than %d distinct non-catalog model names in this "
            "process; further names are counted as model=\"other\" (cardinality limit).",
            _FALLBACK_MODEL_LABEL_LIMIT,
        )
    return "other"


def normalize_outcome(error_type: str | None) -> str:
    """Map an error classification onto the bounded outcome label set."""
    if not error_type:
        return "success"
    lowered = error_type.lower()
    if "timeout" in lowered or "deadline" in lowered:
        return "timeout"
    if "cancel" in lowered:
        return "cancelled"
    return "error"


def outcome_from_exception(exc: BaseException) -> str:
    """Bounded outcome label for a raised provider exception.

    Considers BOTH the exception type name (TimeoutError,
    CancelledError, provider Deadline errors) and the run-failure
    classification — the latter alone maps most exceptions to
    ``server_error`` and would hide the timeout/cancel split the
    dashboards need.
    """
    from inqtrix.execution_failures import classify_execution_failure

    global _classify_failure_warned
    try:
        code = classify_execution_failure(exc)
    except Exception:  # noqa: BLE001 — labeling must never raise
        code = "error"
        if not _classify_failure_warned:
            _classify_failure_warned = True
            log.warning(
                "Error classification for metric outcome failed; affected calls are "
                "still counted as outcome=\"error\" without a more specific "
                "classification."
            )
    return normalize_outcome(f"{type(exc).__name__}:{code}")


@dataclass
class CallMetrics:
    """Registry-bound instruments shared by API and worker processes."""

    llm_requests: Any
    llm_duration: Any
    llm_tokens: Any
    search_requests: Any
    search_duration: Any
    retrieval_duration: Any
    run_duration: Any
    run_queue_wait: Any
    worker_jobs: Any
    indexing_documents: Any
    stream_viewers: Any
    indexing_pauses: Any
    indexing_auto_resumes: Any

    # ---- recording helpers (fail-safe, never fail-silent) ---------- #

    def observe_stream_viewers(
        self, *, job_kind: str, concurrent: int
    ) -> None:
        """Record the concurrency ONE entity reached when a viewer joined.

        The distribution answers the deferred shared-poller question
        ("do multiple viewers per run occur at all?") without run ids as
        labels — cardinality contract of this module.
        """
        try:
            self.stream_viewers.labels(job_kind=job_kind).observe(
                max(1, int(concurrent))
            )
        except Exception:  # noqa: BLE001 — metrics never break the call
            _warn_once("stream_viewers")

    def observe_llm(
        self,
        *,
        provider: str,
        model: str,
        operation: str,
        outcome: str,
        duration_seconds: float,
        feature: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> None:
        try:
            self.llm_requests.labels(
                provider=provider,
                model=model,
                operation=operation,
                outcome=_call_outcome_label(outcome),
            ).inc()
            self.llm_duration.labels(
                provider=provider, model=model, operation=operation
            ).observe(max(0.0, duration_seconds))
            if prompt_tokens:
                self.llm_tokens.labels(
                    model=model, feature=feature, token_type="input"
                ).inc(prompt_tokens)
            if completion_tokens:
                self.llm_tokens.labels(
                    model=model, feature=feature, token_type="output"
                ).inc(completion_tokens)
        except Exception:  # noqa: BLE001 — metrics never break the call
            _warn_once("llm")

    def observe_search(
        self,
        *,
        provider: str,
        engine: str,
        outcome: str,
        duration_seconds: float,
    ) -> None:
        try:
            self.search_requests.labels(
                provider=provider,
                engine=engine,
                outcome=_call_outcome_label(outcome),
            ).inc()
            self.search_duration.labels(
                provider=provider, engine=engine
            ).observe(max(0.0, duration_seconds))
        except Exception:  # noqa: BLE001
            _warn_once("search")

    def observe_retrieval_step(
        self, *, step: str, duration_seconds: float
    ) -> None:
        try:
            self.retrieval_duration.labels(step=step).observe(
                max(0.0, duration_seconds)
            )
        except Exception:  # noqa: BLE001
            _warn_once("retrieval")

    def observe_run(
        self, *, mode: str, outcome: str, duration_seconds: float
    ) -> None:
        try:
            self.run_duration.labels(mode=mode, outcome=outcome).observe(
                max(0.0, duration_seconds)
            )
        except Exception:  # noqa: BLE001
            _warn_once("run")

    def observe_queue_wait(self, *, seconds: float) -> None:
        try:
            self.run_queue_wait.observe(max(0.0, seconds))
        except Exception:  # noqa: BLE001
            _warn_once("queue_wait")

    def count_worker_job(self, *, loop: str, outcome: str) -> None:
        try:
            self.worker_jobs.labels(loop=loop, outcome=outcome).inc()
        except Exception:  # noqa: BLE001
            _warn_once("worker_jobs")

    def count_indexed_documents(
        self, *, outcome: str, amount: int = 1
    ) -> None:
        try:
            self.indexing_documents.labels(outcome=outcome).inc(amount)
        except Exception:  # noqa: BLE001
            _warn_once("indexing")

    def count_indexing_pause(self, *, status: str, error_type: str) -> None:
        source, kind = pause_labels(error_type)
        try:
            self.indexing_pauses.labels(
                status=status, source=source, kind=kind
            ).inc()
        except Exception:  # noqa: BLE001
            _warn_once("indexing_pauses")

    def count_auto_resume(self, *, outcome: str) -> None:
        """``outcome`` is ``planned``, ``resumed`` or ``stopped``."""
        try:
            self.indexing_auto_resumes.labels(outcome=outcome).inc()
        except Exception:  # noqa: BLE001
            _warn_once("indexing_auto_resumes")


_warned: set[str] = set()


def _warn_once(family: str) -> None:
    """Fail-safe, never fail-SILENT: one WARNING per family per process."""
    if family in _warned:
        return
    _warned.add(family)
    log.warning(
        "Metric recording (%s) failed; affected values are missing from /metrics.",
        family,
        exc_info=True,
    )


def build_call_metrics(registry: Any) -> CallMetrics:
    """Construct every shared instrument bound to *registry*.

    Import-guarded like ``server/metrics.py``: callers only invoke this
    after ``prometheus_client`` imported successfully.
    """
    from prometheus_client import Counter, Histogram

    llm_requests = Counter(
        "inqtrix_llm_requests",
        "Provider LLM calls by outcome.",
        labelnames=("provider", "model", "operation", "outcome"),
        registry=registry,
    )
    llm_duration = Histogram(
        "inqtrix_llm_request_duration_seconds",
        "Provider LLM call latency.",
        labelnames=("provider", "model", "operation"),
        buckets=LLM_LATENCY_BUCKETS,
        registry=registry,
    )
    llm_tokens = Counter(
        "inqtrix_llm_tokens",
        "LLM tokens consumed, by canonical model and product feature.",
        labelnames=("model", "feature", "token_type"),
        registry=registry,
    )
    search_requests = Counter(
        "inqtrix_search_requests",
        "Web-search provider calls by outcome.",
        labelnames=("provider", "engine", "outcome"),
        registry=registry,
    )
    search_duration = Histogram(
        "inqtrix_search_request_duration_seconds",
        "Web-search call latency.",
        labelnames=("provider", "engine"),
        buckets=SEARCH_LATENCY_BUCKETS,
        registry=registry,
    )
    retrieval_duration = Histogram(
        "inqtrix_retrieval_duration_seconds",
        "Knowledge retrieval step latency (hybrid_search, rerank).",
        labelnames=("step",),
        buckets=RETRIEVAL_LATENCY_BUCKETS,
        registry=registry,
    )
    run_duration = Histogram(
        "inqtrix_run_duration_seconds",
        "Run execution-segment duration by mode and outcome.",
        labelnames=("mode", "outcome"),
        buckets=RUN_DURATION_BUCKETS,
        registry=registry,
    )
    run_queue_wait = Histogram(
        "inqtrix_run_queue_wait_seconds",
        "Time between enqueue and worker claim.",
        buckets=QUEUE_WAIT_BUCKETS,
        registry=registry,
    )
    worker_jobs = Counter(
        "inqtrix_worker_jobs",
        "Worker job terminations by loop and outcome.",
        labelnames=("loop", "outcome"),
        registry=registry,
    )
    indexing_documents = Counter(
        "inqtrix_indexing_documents",
        "Documents finishing an indexing pass.",
        labelnames=("outcome",),
        registry=registry,
    )
    indexing_pauses = Counter(
        "inqtrix_indexing_pauses",
        "Indexing jobs that paused, by where the failure came from.",
        labelnames=("status", "source", "kind"),
        registry=registry,
    )
    indexing_auto_resumes = Counter(
        "inqtrix_indexing_auto_resumes",
        "Automatic resumes of dependency-paused indexing jobs: planned, "
        "performed, or stopped.",
        labelnames=("outcome",),
        registry=registry,
    )
    stream_viewers = Histogram(
        "inqtrix_stream_concurrent_viewers",
        "Concurrent live event subscribers on one entity, observed each "
        "time a STREAM viewer joins (one-shot JSON polling reads do not "
        "count). Distribution only — deliberately no entity ids as "
        "labels.",
        labelnames=("job_kind",),
        buckets=(1, 2, 3, 4, 5, 8, 13),
        registry=registry,
    )

    return CallMetrics(
        llm_requests=llm_requests,
        llm_duration=llm_duration,
        llm_tokens=llm_tokens,
        search_requests=search_requests,
        search_duration=search_duration,
        retrieval_duration=retrieval_duration,
        run_duration=run_duration,
        run_queue_wait=run_queue_wait,
        worker_jobs=worker_jobs,
        indexing_documents=indexing_documents,
        stream_viewers=stream_viewers,
        indexing_pauses=indexing_pauses,
        indexing_auto_resumes=indexing_auto_resumes,
    )


# ---- process holder (active-content-policy pattern) ---------------- #

_active: CallMetrics | None = None


def set_active_metrics(metrics: CallMetrics | None) -> None:
    """Publish the process instruments (server setup / worker startup)."""
    global _active
    _active = metrics


def active_metrics() -> CallMetrics | None:
    """The published instruments, or ``None`` when metrics are off."""
    return _active
