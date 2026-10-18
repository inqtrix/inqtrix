"""Agent state definition and initialization."""

from __future__ import annotations

import logging
import threading
import time
from queue import Queue
from typing import Any, Callable, NotRequired, TypedDict

from inqtrix.constants import MAX_TOTAL_SECONDS
from inqtrix.core.results import WebRecency
from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded
from inqtrix.i18n import detect_ui_language
from inqtrix.scoring import claim_assessments
from inqtrix.runtime_logging import (
    log_iteration_entry,
    new_run_id,
    sanitize_event_payload,
)

log = logging.getLogger("inqtrix")


class AgentState(TypedDict):
    """Full state for a single research agent run."""

    question: str
    history: str
    language: str
    language_source: NotRequired[str]
    search_language: str
    recency: str
    query_type: str
    answer_contract: NotRequired[str]
    answer_contract_source: NotRequired[str]
    aspects_source: NotRequired[str]
    event_mode: NotRequired[bool]
    evaluate_status: NotRequired[str]
    coverage_source: NotRequired[str]
    _confidence_stop: NotRequired[int]
    _stop_reason: NotRequired[str]
    queries: list[str]
    sub_questions: list[str]
    required_aspects: list[str]
    uncovered_aspects: list[str]
    aspect_coverage: float
    all_citations: list[str]
    source_tier_counts: dict[str, int]
    source_role_counts: NotRequired[dict[str, int]]
    source_quality_score: float
    consolidated_claims: list[dict[str, Any]]
    consolidated_claims_full: NotRequired[list[dict[str, Any]]]
    claim_status_counts: dict[str, int]
    claim_quality_score: float
    claim_needs_primary_total: int
    claim_needs_primary_supported: int
    search_offset: int
    gaps: str
    risk_score: int
    high_risk: bool
    round: int
    done: bool
    answer: str
    deadline: float
    progress: Queue | None
    start_time: float
    final_confidence: int
    answer_finish_reason: str
    answer_incomplete: bool
    answer_incomplete_reasons: list[str]
    competing_events: str
    prev_competing_events: str
    falsification_triggered: bool
    evidence_consistency: int
    evidence_sufficiency: int
    utility_scores: list[float]
    score_ledger: NotRequired[list[dict[str, Any]]]
    prev_citation_count: int
    prev_supported_claim_count: NotRequired[int]
    _conf_stable_rounds: int
    _run_id: str
    _event_seq: int
    _current_node: NotRequired[str]
    _max_rounds: NotRequired[int]
    _run_event_sink: NotRequired[Callable[[str, dict[str, Any]], None] | None]
    query_records: list[dict[str, Any]]
    source_records: dict[str, dict[str, Any]]
    provider_citation_records: list[dict[str, Any]]
    algorithm_failures: NotRequired[list[dict[str, Any]]]
    evidence_ledger: NotRequired[list[dict[str, Any]]]
    query_synthesis: NotRequired[dict[str, dict[str, Any]]]
    evidence_label_urls: NotRequired[dict[str, str]]
    evidence_label_by_id: NotRequired[dict[str, str]]
    visible_evidence_labels: NotRequired[list[str]]
    visible_evidence_label_count: NotRequired[int]
    rendered_evidence_ids: NotRequired[list[str]]
    allowed_citations: NotRequired[list[str]]
    report_references: NotRequired[list[dict[str, Any]]]
    rendered_evidence_record_count: NotRequired[int]
    omitted_evidence_record_count: NotRequired[int]
    omitted_evidence_by_query: NotRequired[list[dict[str, Any]]]
    node_model_resolutions: NotRequired[dict[str, dict[str, str]]]
    evidence_depth_gap: NotRequired[dict[str, Any]]
    _evidence_depth_gap_active: NotRequired[bool]
    answer_claim_bindings: list[dict[str, Any]]
    answer_evidence_bindings: NotRequired[list[dict[str, Any]]]
    evidence_contract_status: NotRequired[str]
    evidence_omissions: NotRequired[list[dict[str, Any]]]
    iteration_logs: list[dict[str, Any]]
    total_prompt_tokens: int
    total_completion_tokens: int
    _claim_extraction_attempts_total: NotRequired[int]
    _claim_extraction_failures_total: NotRequired[int]
    # Optional cancel-event field added for the implicit-cancel-on-disconnect
    # pathway in the HTTP server. NotRequired keeps every existing reader
    # untouched: state["_cancel_event"] is only present when the server
    # explicitly seeded it via initial_state(cancel_event=...).
    _cancel_event: NotRequired[threading.Event | None]
    # Optional hard per-run LLM-token budget (the opt-in quota cap).
    # ``0`` / absent = off; a positive value makes check_cancel_event
    # raise AgentCancelled once cumulative tokens reach it.
    _token_budget: NotRequired[int]
    # Optional server-validated recency filter supplied by a parent agent.
    # It remains separate from the classifier result so success and fallback
    # paths cannot accidentally replace the explicit execution contract.
    _web_recency: NotRequired[WebRecency]
    # A graph may preserve its historical library-mode diagnostic answer while
    # telling the native run service that the execution is terminally failed.
    # The native service lifts this marker into the shared typed failure path;
    # it is never used for planner/task control flow.
    _terminal_failure: NotRequired[dict[str, str]]


def initial_state(
    question: str,
    history: str = "",
    progress_queue: Queue | None = None,
    *,
    max_total_seconds: int = MAX_TOTAL_SECONDS,
    cancel_event: threading.Event | None = None,
    max_rounds: int | None = None,
    run_id: str | None = None,
    run_event_sink: Callable[[str, dict[str, Any]], None] | None = None,
    token_budget: int = 0,
    web_recency: WebRecency | None = None,
) -> dict[str, Any]:
    """Create the initial AgentState for a run.

    The optional ``cancel_event`` lets :func:`check_cancel_event`
    interrupt the loop at node boundaries when the HTTP server detects
    a client disconnect or native run cancellation. ``max_rounds`` and
    ``run_event_sink`` are optional native-UI metadata hooks; callers
    that do not expose live run events leave them unset.
    """
    deadline = time.monotonic() + max_total_seconds
    # A provisional language from the question so the first progress event
    # already appears in the right language. classify() replaces it with the
    # resolved answer language and records ``language_source``.
    initial_language = detect_ui_language(question)
    state: dict[str, Any] = {
        "question": question,
        "history": history,
        "language": initial_language,
        "search_language": "",
        "recency": web_recency or "",
        "query_type": "general",
        "answer_contract": "general",
        "event_mode": False,
        "queries": [],
        "sub_questions": [],
        "required_aspects": [],
        "uncovered_aspects": [],
        "aspect_coverage": 0.0,
        "all_citations": [],
        "source_tier_counts": {"primary": 0, "mainstream": 0, "stakeholder": 0, "unknown": 0, "low": 0},
        "source_quality_score": 0.0,
        "consolidated_claims": [],
        "claim_status_counts": {"verified": 0, "attributed": 0, "contested": 0, "unverified": 0},
        "claim_quality_score": 0.0,
        "claim_needs_primary_total": 0,
        "claim_needs_primary_supported": 0,
        "search_offset": 0,
        "gaps": "",
        "risk_score": 0,
        "high_risk": False,
        "round": 0,
        "done": False,
        "answer": "",
        "deadline": deadline,
        "progress": progress_queue,
        "start_time": time.monotonic(),
        "final_confidence": 0,
        "answer_finish_reason": "",
        "answer_incomplete": False,
        "answer_incomplete_reasons": [],
        "competing_events": "",
        "prev_competing_events": "",
        "falsification_triggered": False,
        "evidence_consistency": 0,
        "evidence_sufficiency": 0,
        "utility_scores": [],
        "score_ledger": [],
        "prev_citation_count": 0,
        "prev_supported_claim_count": 0,
        "_conf_stable_rounds": 0,
        "_run_id": run_id or new_run_id(),
        "_event_seq": 0,
        "_current_node": "",
        "query_records": [],
        "source_records": {},
        "provider_citation_records": [],
        "algorithm_failures": [],
        "evidence_ledger": [],
        "query_synthesis": {},
        "evidence_depth_gap": {},
        "_evidence_depth_gap_active": False,
        "answer_claim_bindings": [],
        "answer_evidence_bindings": [],
        "evidence_contract_status": "unknown",
        "evidence_omissions": [],
        "iteration_logs": [],
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "_claim_extraction_attempts_total": 0,
        "_claim_extraction_failures_total": 0,
    }
    if max_rounds is not None:
        state["_max_rounds"] = max(1, int(max_rounds))
    if run_event_sink is not None:
        state["_run_event_sink"] = run_event_sink
    if cancel_event is not None:
        state["_cancel_event"] = cancel_event
    if token_budget > 0:
        state["_token_budget"] = int(token_budget)
    if web_recency is not None:
        state["_web_recency"] = web_recency
    return state


def check_cancel_event(state: dict[str, Any]) -> None:
    """Abort the run at a graph checkpoint for either stop reason.

    Called by every LangGraph node at its entry point and at additional
    intra-node checkpoints (between fan-outs, per answer section).
    Provider-level cancellation (retry attempts, backoff sleeps) runs
    through :func:`inqtrix.providers.base.provider_cancel_scope` on the
    same event. Two reasons, both best-effort (an in-flight provider
    HTTP attempt is not interrupted mid-request):

    * the per-run cancel event was set (client disconnect / native run
      cancellation), or
    * the optional hard per-run token budget (``_token_budget``, the
      opt-in quota cap) has been reached by the cumulative token total.

    No-op when neither is configured (single-stack / library mode keep
    both absent).

    Args:
        state: The current :class:`AgentState`.

    Raises:
        AgentCancelled: When the cancel event is set, or when the token
            budget is configured and the cumulative token total reaches
            it.
    """
    event = state.get("_cancel_event")
    if event is not None and event.is_set():
        raise AgentCancelled("Run cancelled.")
    budget = state.get("_token_budget", 0)
    if budget:
        used = int(state.get("total_prompt_tokens", 0) or 0) + int(
            state.get("total_completion_tokens", 0) or 0
        )
        if used >= budget:
            log.warning(
                "Run stopped by token budget: %d/%d tokens "
                "(INQTRIX_QUOTA_MAX_TOKENS_PER_RUN).",
                used,
                budget,
            )
            raise AgentTokenBudgetExceeded(
                "Run stopped by token budget (max_tokens_per_run)."
            )


_VALID_PROGRESS_SEVERITIES = frozenset({"info", "warning", "success", "error"})


def emit_progress(
    s: dict,
    message: str,
    *,
    severity: str | None = None,
) -> None:
    """Send a progress update to the stream.

    Args:
        s: Mutable agent state. The optional ``"progress"`` queue and
            the run-event sink consume the message.
        message: User-visible localized text. Already passed through
            :func:`inqtrix.i18n.t` at the call site.
        severity: Optional explicit severity override. Accepted values
            are ``"info"``, ``"warning"``, ``"success"``, ``"error"``.
            When omitted (or set to an unknown string), classification
            falls back to :func:`_progress_event_severity` which scans
            *message* for warning markers. Pass an explicit severity in
            fallback or guardrail paths so a future i18n-string change
            cannot silently demote a warning to info (no silent
            fallbacks).
    """
    q = s.get("progress")
    if q is not None:
        q.put(("progress", message))
    snapshot = build_run_snapshot(s, last_message=message)
    if severity in _VALID_PROGRESS_SEVERITIES:
        resolved_severity = severity
    else:
        resolved_severity = _progress_event_severity(message)
    emit_run_event(
        s,
        "inqtrix.progress.message",
        {
            "message": message,
            "phase": snapshot.get("current_node", ""),
            "severity": resolved_severity,
            "snapshot": snapshot,
        },
    )


def _progress_event_severity(message: str) -> str:
    """Classify a human progress message for compact UI rendering."""
    text = message.lower()
    warning_markers = (
        "algo-fail",
        "fallback",
        "fehlgeschlagen",
        "failed",
        "warnung",
        "warning",
        "retry",
        "instabil",
        "unstable",
        "verlet",
        "violat",
        "unvollstaendig",
        "unvollständig",
        "kontextfenster",
        "context window",
    )
    if any(marker in text for marker in warning_markers):
        return "warning"
    return "info"


def build_run_snapshot(
    state: dict[str, Any],
    *,
    current_node: str | None = None,
    last_message: str = "",
) -> dict[str, Any]:
    """Build the compact native-UI progress view from agent state.

    The snapshot intentionally contains derived counters only, not raw
    provider payloads or full evidence ledgers. It is safe to ship over
    SSE and stable enough for UI cards while the richer final report is
    fetched from the result endpoint after completion.
    """
    node = current_node if current_node is not None else str(state.get("_current_node", "") or "")
    completed_rounds = max(0, int(state.get("round", 0) or 0))
    max_rounds = max(1, int(state.get("_max_rounds", completed_rounds or 1) or 1))
    active_round = completed_rounds
    if node in {"plan", "search"} and not bool(state.get("done", False)):
        active_round = min(max_rounds, completed_rounds + 1)
    elif node == "classify":
        active_round = 0
    else:
        active_round = min(max_rounds, completed_rounds)

    progress_estimate = _estimate_progress(
        node=node,
        completed_rounds=completed_rounds,
        active_round=active_round,
        max_rounds=max_rounds,
        done=bool(state.get("done", False)),
    )
    all_citations = state.get("all_citations", []) or []
    source_records = state.get("source_records", {}) or {}
    queries = state.get("queries", []) or []
    source_tier_counts = {
        str(key): int(value or 0)
        for key, value in dict(state.get("source_tier_counts", {}) or {}).items()
    }
    claim_status_counts = {
        str(key): int(value or 0)
        for key, value in dict(state.get("claim_status_counts", {}) or {}).items()
    }
    evidence_ledger = state.get("evidence_ledger", []) or []
    consolidated_claims = claim_assessments(state)
    snapshot: dict[str, Any] = {
        "current_node": node,
        "completed_rounds": completed_rounds,
        "active_round": active_round,
        "max_rounds": max_rounds,
        "total_queries": len(queries),
        "total_citations": len(all_citations),
        "total_sources": len(source_records) or len(set(all_citations)),
        "confidence": int(state.get("final_confidence", 0) or 0),
        "source_tier_counts": source_tier_counts,
        "source_quality_score": float(state.get("source_quality_score", 0.0) or 0.0),
        "claim_status_counts": claim_status_counts,
        "claim_quality_score": float(state.get("claim_quality_score", 0.0) or 0.0),
        "evidence_record_count": len(evidence_ledger),
        "consolidated_claim_count": len(consolidated_claims),
        "aspect_coverage": float(state.get("aspect_coverage", 0.0) or 0.0),
        "evidence_consistency": int(state.get("evidence_consistency", 0) or 0),
        "evidence_sufficiency": int(state.get("evidence_sufficiency", 0) or 0),
        "done": bool(state.get("done", False)),
        "progress_estimate": progress_estimate,
        "last_message": last_message,
    }
    # The searches the provider executed: each left a query record, while a
    # planned query a deadline stopped did not. Only a run that records
    # searches names them, so a parent never reads a mission or a role
    # as a research that ran none.
    query_records = state.get("query_records")
    if query_records is not None:
        snapshot["executed_queries"] = len(query_records)
    # The section the writer is on, counted in write order (k of n): typed,
    # so a reader never parses the progress message for it.
    writing = state.get("_writing_section")
    if writing:
        snapshot["writing_section"], snapshot["sections_total"] = (
            int(writing[0]),
            int(writing[1]),
        )
    execution = state.get("execution")
    if execution is not None:
        from pydantic import ValidationError

        from inqtrix.result import AgentExecution

        try:
            snapshot["execution"] = AgentExecution.model_validate(
                execution
            ).model_dump()
        except ValidationError as exc:
            log.warning(
                "Invalid agent execution block from run snapshot cannot be published "
                "(error_code=%s, error_count=%d)",
                type(exc).__name__,
                exc.error_count(),
            )
            raise RuntimeError(
                "Run snapshot contains an invalid agent execution block."
            ) from exc
    # Knowledge runs carry a compact execution receipt through the same
    # snapshot/result lifecycle as research metrics.  The projection is
    # explicitly text-free: queries, gate prose, grounding quotes and source
    # bodies stay out of SSE snapshots.  Parsing is strict so a malformed or
    # contradictory receipt cannot be published as a successful run.
    from inqtrix.result import KnowledgeResultState

    knowledge = KnowledgeResultState.from_sources(state)
    if knowledge is not None:
        snapshot.update(knowledge.to_export_fields())
    return snapshot


def _estimate_progress(
    *,
    node: str,
    completed_rounds: int,
    active_round: int,
    max_rounds: int,
    done: bool,
) -> float:
    """Return a bounded approximate progress fraction for native UIs."""
    if done:
        return 1.0
    if node == "direct_llm":
        return 0.5
    if node == "answer":
        return 0.92
    if node == "classify":
        return 0.03

    phase_fraction = {
        "plan": 0.15,
        "search": 0.55,
        "evaluate": 0.85,
    }.get(node, 0.0)
    round_index = max(0, active_round - 1)
    round_progress = (round_index + phase_fraction) / max_rounds
    base = min(0.88, max(0.05, round_progress * 0.84 + 0.05))
    if completed_rounds >= max_rounds:
        return min(0.9, base)
    return round(base, 3)


def emit_run_event(
    state: dict[str, Any],
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Emit a structured native-run event when a sink was provided."""
    sink = state.get("_run_event_sink")
    if not callable(sink):
        return
    try:
        sink(event_type, payload or {})
    except Exception as exc:  # noqa: BLE001 - event sinks are observability only
        log.warning(
            "Native run event sink failed (error_code=%s)",
            type(exc).__name__,
        )


def append_iteration_log(s: dict, entry: dict[str, Any], *, testing_mode: bool = False) -> None:
    """Add an iteration log entry and mirror it into debug logs.

    The entry runs through :func:`sanitize_event_payload` **before** it is
    stored in ``state["iteration_logs"]`` (testing mode) and before its
    operational projection is forwarded to the file-log path. The protected
    testing export retains reconstructable redacted evidence; ordinary logs
    receive only IDs, lifecycle/status fields, models, counters, usage and
    timings. Credential-bearing URL values, bearer tokens and provider raw
    payloads never appear in either sink.
    """
    materialized_entry = dict(entry)
    if "_run_id" in s:
        materialized_entry.setdefault("run_id", s.get("_run_id", ""))
    event_seq = int(s.get("_event_seq", 0) or 0) + 1
    s["_event_seq"] = event_seq
    materialized_entry.setdefault("event_seq", event_seq)
    event_name = str(materialized_entry.setdefault("event", "iteration_summary"))
    sanitized_entry = sanitize_event_payload(event_name, materialized_entry)
    if testing_mode:
        s["iteration_logs"].append(sanitized_entry)
    log_iteration_entry(sanitized_entry)


def track_tokens(s: dict, response: Any) -> None:
    """Count token usage from an API response."""
    if hasattr(response, "usage") and response.usage:
        s["total_prompt_tokens"] += getattr(response.usage, "prompt_tokens", 0) or 0
        s["total_completion_tokens"] += getattr(response.usage, "completion_tokens", 0) or 0
        return

    prompt_tokens = getattr(response, "prompt_tokens", None)
    completion_tokens = getattr(response, "completion_tokens", None)
    if prompt_tokens is not None or completion_tokens is not None:
        s["total_prompt_tokens"] += int(prompt_tokens or 0)
        s["total_completion_tokens"] += int(completion_tokens or 0)
