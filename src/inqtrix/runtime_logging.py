"""Helpers for protected run audit and content-minimized runtime logging.

Provider/model metadata extraction, audit sanitization and the stricter
container-log projection live together so their boundary stays explicit.
"""

from __future__ import annotations

import logging
import re
import uuid
from hashlib import sha1
from typing import Any

from inqtrix.observability.context import current_log_context
from inqtrix.observability.otel import add_span_event, span_is_recording
from inqtrix.search_result import GroundedSearchResult, GroundedSource
from inqtrix.settings import AgentSettings
from inqtrix.scoring import claim_assessments
from inqtrix.text import LOG_CUT_MARKER, shorten_visibly
from inqtrix.urls import (
    CredentialBearingUrlError,
    domain_from_url,
    normalize_url,
    redact_credential_url,
    safe_public_url_identity,
    sanitize_error,
    scrub_credential_urls,
)

log = logging.getLogger("inqtrix")

_SENSITIVE_EVENT_KEYS = frozenset({
    "access_token",
    "api_key",
    "auth_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "credential",
    "credentials",
    "env",
    "headers",
    "password",
    "raw",
    "raw_response",
    "request_body",
    "request_headers",
    "request_kwargs",
    "secret",
    "session_token",
    "x_api_key",
    "x_key",
})

# Persistence bound for evidence-content fields in protected iteration logs.
_LOG_CONTENT_CAP = 20000
_NARRATION_TEXT_CAP = 400

_TEXT_CAPS = {
    "answer_segment_preview": 500,
    "answer_preview": 800,
    "claim_prompt_view": 4000,
    "report_evidence_prompt": 6000,
    "unverified_evidence_prompt": 4000,
    "title": 180,
    "content_preview": 500,
    "context_preview": 1200,
    "query": 240,
    "status_reason": 180,
    # Keep reconstructable audit text bounded without admitting it to ordinary
    # container/file logs (which use ``_console_iteration_projection``).
    "evidence_snippet": _LOG_CONTENT_CAP,
    "snippet": _LOG_CONTENT_CAP,
    "claim_text": _LOG_CONTENT_CAP,
    "summary": _LOG_CONTENT_CAP,
    "provider_answer": _LOG_CONTENT_CAP,
    "text": _LOG_CONTENT_CAP,
    "as_reported": _LOG_CONTENT_CAP,
}

# Common envelope fields added to every event by the iteration-log wrappers
# (see :func:`inqtrix.state.append_iteration_log`) and by
# :func:`emit_runtime_event` itself. They are always allowed in addition to
# the per-event allowlist.
_COMMON_EVENT_KEYS = frozenset({
    "event",
    "event_seq",
    "node",
    "run_id",
    "timestamp",
    # Correlation envelope stamped by emit_runtime_event: joins a
    # forensic line with its OTel trace and HTTP request. Values are
    # ids only (the console projection keeps ``*_id`` tokens); subject
    # fields (user/workspace/tenant) ride the JSON formatter's context
    # instead of the payload.
    "trace_id",
    "span_id",
    "request_id",
})

# ``iteration_logs`` are protected run-audit data.  They deliberately retain
# the redacted queries, source text and prompt views needed to reconstruct a
# run.  Container/file logs have a different audience and therefore receive a
# fail-closed operational projection: identifiers, lifecycle/status fields,
# models, counters, usage and timings only.  Keep this policy separate from
# ``sanitize_event_payload`` so tightening console visibility never destroys
# evidence in the persisted audit representation.
_CONSOLE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:+-]{1,180}$")
_CONSOLE_MODEL_RE = re.compile(r"^[A-Za-z0-9_.:@+/-]{1,180}$")

_CONSOLE_ENUM_FIELDS = frozenset({
    "access_status",
    "answer_contract",
    "binding_status",
    "claim_extraction_mode",
    "claim_extraction_schema",
    "claim_status",
    "claim_type",
    "effort",
    "effort_source",
    "engine",
    "event",
    "evidence_contract_status",
    "fallback",
    "finish_reason",
    "kind",
    "model",
    "model_source",
    "node",
    "origin",
    "phase",
    "polarity",
    "provider",
    "record_type",
    "report_profile",
    "requested_tier",
    "run_mode",
    "status",
    "tier",
    "verification",
    "verification_basis",
})

_CONSOLE_CODE_FIELDS = frozenset({
    "error_code",
    "failure_code",
    "final_stop_reason",
    "reason",
    "status_reason",
    "stop_reason",
    "suppressed_stop_reason",
})

_CONSOLE_BOOLEAN_FIELDS = frozenset({
    "active",
    "available",
    "blocking",
    "cancelled",
    "complete",
    "done",
    "enabled",
    "failed",
    "final",
    "incomplete",
    "limit_hit",
    "parsed",
    "report_eligible",
    "supported",
})

_CONSOLE_NUMERIC_FIELDS = frozenset({
    "active_round",
    "attempt",
    "completion_tokens",
    "confidence",
    "confidence_stop",
    "duration_ms",
    "duration_s",
    "elapsed_ms",
    "elapsed_s",
    "event_seq",
    "final_confidence",
    "max_rounds",
    "min_rounds",
    "pages_read",
    "position",
    "prompt_tokens",
    "quality_score",
    "query_index",
    "rank",
    "round",
    "rows_matched",
    "rows_scanned",
    "sequence",
    "sources_found",
    "timestamp",
    "total",
    "total_citations",
    "total_completion_tokens",
    "total_prompt_tokens",
    "utility_score",
})

_CONSOLE_COUNT_MAP_FIELDS = frozenset({
    "claim_extraction_modes",
    "claim_status_counts",
    "source_tier_counts",
})

_CONSOLE_NUMERIC_SUFFIXES = (
    "_attempt",
    "_attempts",
    "_bytes",
    "_chars",
    "_count",
    "_depth",
    "_duration_ms",
    "_duration_s",
    "_dropped",
    "_elapsed_ms",
    "_elapsed_s",
    "_executed",
    "_extracted",
    "_failures",
    "_fallbacks",
    "_found",
    "_index",
    "_kept",
    "_length",
    "_pages",
    "_rank",
    "_rows",
    "_round",
    "_sanitized",
    "_selected",
    "_score",
    "_seconds",
    "_tokens",
    "_utilization",
)

_CONSOLE_BOOLEAN_SUFFIXES = (
    "_active",
    "_attempted",
    "_available",
    "_blocking",
    "_cancelled",
    "_complete",
    "_done",
    "_enabled",
    "_failed",
    "_fallback",
    "_final",
    "_hit",
    "_incomplete",
    "_parsed",
    "_supported",
)

# Per-event allowlists for forensic events. Events without a schema entry
# fall back to the recursive drop-list + URL/secret redaction in
# :func:`_sanitize_value` (hybrid policy: strict allowlist for the
# forensic-lineage events, drop-list for ``iteration_summary`` headers).
_EVENT_SCHEMAS: dict[str, frozenset[str]] = {
    # A bounded technical retrieval verdict.  Queries, excerpts and source ids
    # are deliberately absent because this event may cross a child boundary.
    "inqtrix.knowledge.retrieval.degraded": frozenset({
        "task_id",
        "attempt",
        "query_index",
        "reason",
        "retrieval_mode",
        "stage",
        "requested_candidate_pool",
        "returned_candidate_pool",
        "final_top_k",
        "final_evidence_complete",
        "requested_top_k",
        "returned_hits",
        "candidate_cap",
    }),
    # Canonical hydration excluded unsafe candidates. The event contains only
    # a stable warning code, bounded aggregate count and remediation metadata;
    # source ids, excerpts and queries are never valid fields.
    "inqtrix.knowledge.retrieval.warning": frozenset({
        "task_id",
        "attempt",
        "query_index",
        "code",
        "reason",
        "stage",
        "count",
        "recommended_action",
    }),
    # Knowledge quote verification is a terminal evidence decision.  It may
    # cross a delegated child boundary, so retain only bounded status/count
    # lineage and never the quote/source text itself.
    "inqtrix.knowledge.grounding.checked": frozenset({
        "task_id",
        "attempt",
        "query_index",
        "marker",
        "status",
        "failure_code",
        "format_repaired",
        "quotes_total",
        "quotes_verified",
    }),
    # Counter-only by design: the rejected quote texts never cross the
    # event boundary (same discipline as grounding.checked).
    "inqtrix.knowledge.answer.retry": frozenset({
        "task_id",
        "attempt",
        "query_index",
        "quotes_total",
        "quotes_unverified",
    }),
    # Agent-Desk narration: a strict allowlist so the prose channel can
    # never grow surprise fields — the transcript renders the payload
    # verbatim.
    "inqtrix.agent.narration": frozenset({
        "narration_id",
        "kind",
        "text",
        "phase",
        "final",
    }),
    # The draft of a reviewed answer, rendered verbatim like the answer it
    # precedes: a strict allowlist, and its Markdown under a key no audit
    # bound in ``_TEXT_CAPS`` names, because a draft is shown whole.
    "inqtrix.answer.draft": frozenset({
        "artifact_id",
        "draft_id",
        "markdown",
        "reference_labels",
    }),
    # An agent run's outcome record; only a delegated mission's crosses to
    # its parent, as numbers and its closed summary sentence, so its fields
    # are allowlisted: a label or detail smuggled into the payload must
    # never become the parent's message.
    "inqtrix.agent.outcome": frozenset({
        "outcome_id",
        "final",
        "review",
        "round",
        "revision",
        "accounting",
        "basis",
        "applied",
        "verified",
        "unverified_reasons",
        "reach",
        "codes",
        "limits",
        "open",
        "lines",
        "summary",
        # The delivered statements by state and the passages marked as
        # insufficiently supported: the run's own reader sees them under its
        # outputs; the parent's projection (runs/shared.py) takes none.
        "claims",
        "marks",
        "details",
    }),
    "run_start": frozenset({
        "run_id",
        "run_mode",
        "question_length",
        "history_length",
        "llm",
        "search",
        "settings",
    }),
    "run_end": frozenset({
        "run_id",
        "run_mode",
        "status",
        "reason",
        "elapsed_s",
        "round",
        "done",
        "cancelled",
        "final_confidence",
        "total_citations",
        "evidence_record_count",
        "consolidated_claims_count",
        "algorithm_failure_count",
        "blocking_algorithm_failure_count",
        "total_prompt_tokens",
        "total_completion_tokens",
    }),
    "algorithm_failure": frozenset({
        "phase",
        "reason",
        "message",
        "blocking",
        "round",
        "failed",
        "total",
        "attempts",
        "candidate_count",
        "claim_notice_samples",
        "has_citations",
        "has_ledger_context",
        "model",
        "capacity_phase",
        "context_window_tokens",
        "required_context_window_tokens",
        "estimated_input_tokens",
        "requested_output_tokens",
        "context_window_safety_tokens",
        "estimated_required_context_tokens",
    }),
    "node_model_resolution": frozenset({
        "node",
        "model",
        "tier",
        "effort",
        "model_source",
        "effort_source",
        "requested_tier",
    }),
    "node_model_resolution_warning": frozenset({
        "node",
        "model",
        "tier",
        "effort",
        "model_source",
        "effort_source",
        "requested_tier",
        "reason",
    }),
    "query_record": frozenset({
        "invocation_id",
        "query_id",
        "round",
        "query_index",
        "query",
        "domain_filter",
        "provider",
        "parameters",
        "status",
        "notice",
        "started_at",
        "finished_at",
        "duration_ms",
        "usage",
        "source_ids",
        "citation_ids",
    }),
    "query_invocation_started": frozenset({
        "invocation_id",
        "query_id",
        "round",
        "query_index",
        "query",
        "domain_filter",
        "provider",
        "parameters",
        "status",
        "started_at",
    }),
    "query_invocation_finished": frozenset({
        "invocation_id",
        "query_id",
        "round",
        "query_index",
        "provider",
        "status",
        "notice",
        "started_at",
        "finished_at",
        "duration_ms",
        "usage",
    }),
    "inqtrix.research.query.started": frozenset({
        "invocation_id",
        "query_id",
        "round",
        "query_index",
        "query",
        "domain_filter",
        "provider",
        "parameters",
        "status",
        "started_at",
    }),
    "inqtrix.research.query.finished": frozenset({
        "invocation_id",
        "query_id",
        "round",
        "query_index",
        "provider",
        "status",
        "notice",
        "started_at",
        "finished_at",
        "duration_ms",
        "usage",
    }),
    "source_record": frozenset({
        "source_id",
        "url",
        "canonical_url",
        "domain",
        "provider",
        "first_seen_query_id",
        "first_seen_rank",
        "origin",
        "tier",
        "tier_reason",
        "role_reason",
        "access_status",
    }),
    "provider_citation_record": frozenset({
        "citation_id",
        "query_id",
        "source_id",
        "url",
        "canonical_url",
        "rank",
        "origin",
        "provider",
        "title",
        "snippet",
        "source_date",
        "last_updated",
        "source",
        "annotation_start",
        "annotation_end",
    }),
    "claim_record": frozenset({
        "raw_claim_id",
        "query_id",
        "evidence_ids",
        "signature",
        "claim_text",
        "evidence_snippet",
        "claim_type",
        "polarity",
        "needs_primary",
        "source_ids",
        "citation_ids",
        "provider_refs",
        "source_urls",
        "published_date",
        "round",
    }),
    "query_synthesis": frozenset({
        "query_id",
        "query",
        "round",
        "provider_answer",
        "citation_urls_by_rank",
    }),
    "claim_merge": frozenset({
        "claim_id",
        "signature",
        "member_claim_ids",
        "status",
        "status_reason",
        "verification_basis",
        "evidence_snippets",
        "support_count",
        "supporting_evidence_ids",
        "supporting_domain_count",
        "contradicting_evidence_ids",
        "contradict_count",
        "values",
        "conflicting_claim_ids",
        "source_ids",
        "citation_ids",
        "source_urls",
        "round_first_seen",
        "round_last_updated",
    }),
    "evidence_record": frozenset({
        "evidence_id",
        "query_id",
        "query",
        "source_id",
        "citation_id",
        "canonical_url",
        "domain",
        "tier",
        "tier_reason",
        "role_reason",
        "provider",
        "source_title",
        "source_snippet",
        "source_date",
        "last_updated",
        "source_passages",
        "claims",
        "record_type",
        "report_eligible",
        "citation_set",
    }),
    "evidence_verification_projection": frozenset({
        "evidence_count",
        "verified_claim_supports",
        "attributed_claim_supports",
        "contested_claim_supports",
        "unverified_claim_supports",
        "supporting_evidence_link_count",
    }),
    "evidence_selection": frozenset({
        "consolidated_claim_count",
        "materialized_claim_count",
        "verified_claim_count",
        "attributed_claim_count",
        "primary_supported_claim_count",
        "contested_claim_count",
        "cross_checked_claim_count",
        "single_source_count",
        "report_eligible_evidence_count",
        "evidence_depth_gap",
    }),
    "score_snapshot": frozenset({
        "round",
        "phase",
        "source",
        "evidence",
        "claims",
        "coverage",
        "evaluate",
        "stop",
        "answer",
    }),
    "query_summary": frozenset({
        "query_id",
        "round",
        "query_index",
        "query",
        "answer_length",
        "claims_extracted",
        "claims_kept",
        "claims_sample",
        "claim_extraction_mode",
        "claim_extraction_schema",
        "claim_extraction_structured_supported",
        "claim_extraction_valid_empty",
        "claim_extraction_raw_claim_count",
        "claim_extraction_normalized_claim_count",
        "claim_extraction_filtered_claim_count",
        "evidence_record_count",
        "evidence_context_source_count",
        "source_ids",
        "citation_ids",
        "urls",
        "prompt_tokens",
        "completion_tokens",
        "provider_notice",
        "claim_notice",
        "domain_filter",
        "related_question_count",
    }),
    "stop_cascade": frozenset({
        "confidence",
        "confidence_stop_target",
        "round",
        "max_rounds",
        "min_rounds",
        "utility_score",
        "utility_stop",
        "plateau_stop",
        "stagnation_detected",
        "falsification_triggered",
        "done_after_utility",
        "done_after_plateau",
        "confidence_stop",
        "round_limit",
        "evidence_depth_gap_active",
        "evidence_depth_gap",
        "report_eligible_evidence_count",
        "min_report_eligible_evidence",
        "suppressed_by_min_rounds",
        "suppressed_by_report_evidence",
        "suppressed_stop_reason",
        "final_stop_reason",
        "final_done",
    }),
    "citation_selection": frozenset({
        "allowed_citations",
        "allowed_citation_count",
        "allowed_link_count",
        "removed_non_allowed_links",
        "answer_claim_binding_count",
        "answer_evidence_binding_count",
        "matched_evidence_binding_count",
        "unknown_citation_count",
        "expanded_evidence_label_links",
    }),
    "answer_prompt_inputs": frozenset({
        "report_profile",
        "model",
        "evidence_record_count",
        "rendered_evidence_record_count",
        "omitted_evidence_record_count",
        "evidence_overview_chars",
        "visible_evidence_label_count",
        "allowed_citation_count",
        "consolidated_claim_count",
        "algorithm_failure_count",
        "blocking_algorithm_failure_count",
        "evidence_overview",
        "section_plan",
    }),
    "answer_section": frozenset({
        "heading",
        "position",
        "model",
        "content_length",
        "content_preview",
        "finish_reason",
        "limit_hit",
        "incomplete",
        "incomplete_reasons",
        "prompt_tokens",
        "completion_tokens",
        "request_max_tokens",
        "max_output_tokens",
        "estimated_input_tokens",
        "requested_output_tokens",
        "estimated_required_context_tokens",
        "context_window_tokens",
        "required_context_window_tokens",
        "system_prompt_chars",
        "user_prompt_chars",
        "section_focus_record_count",
        "section_focus_labels",
        "section_allowed_citation_count",
        "section_scoped_evidence",
        "used_evidence_labels",
        "token_utilization",
        "thinking_likely_active",
        "visible_tokens_estimate",
        "thinking_tokens_estimate",
    }),
    "answer_claim_binding": frozenset({
        "binding_id",
        "answer_segment_id",
        "answer_segment_preview",
        "citation_url",
        "source_id",
        "citation_id",
        "claim_id",
        "claim_status",
        "binding_status",
    }),
    "answer_sentence_audit": frozenset({
        "binding_id",
        "citation_url",
        "evidence_id",
        "verification",
        "binding_status",
    }),
}



def new_run_id() -> str:
    """Return an opaque, log-safe identifier for one research run."""
    return f"run_{uuid.uuid4().hex}"


def make_record_id(prefix: str, *parts: Any) -> str:
    """Build a stable short id from semantic record parts."""
    raw = "|".join(str(part) for part in parts)
    digest = sha1(raw.encode("utf-8")).hexdigest()[:14]
    clean_prefix = re.sub(r"[^a-zA-Z0-9_]+", "_", prefix).strip("_") or "id"
    return f"{clean_prefix}_{digest}"


def _is_sensitive_event_key(key: object) -> bool:
    """Return whether a payload key is unsafe for structured logs."""
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    return normalized in _SENSITIVE_EVENT_KEYS


def _cap_text(key: object, text: str) -> str:
    cap = _TEXT_CAPS.get(str(key), 0)
    if cap <= 0 or len(text) <= cap:
        return text
    return f"{text[:cap].rstrip()}..."


def _sanitize_value(value: Any, *, apply_text_caps: bool = True) -> Any:
    """Recursively sanitize values before JSON serialization."""
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, val in value.items():
            if _is_sensitive_event_key(key):
                continue
            prepared = _cap_text(key, val) if apply_text_caps and isinstance(val, str) else val
            sanitized[str(key)] = _sanitize_value(prepared, apply_text_caps=apply_text_caps)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item, apply_text_caps=apply_text_caps) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_value(item, apply_text_caps=apply_text_caps) for item in value]
    if isinstance(value, (str, Exception)):
        return sanitize_error(value)
    return value


def redact_trace_payload(payload: Any) -> Any:
    """Share credential redaction without applying audit preview limits.

    The trace content policy bounds the serialized value and reports cuts.
    """
    return _sanitize_value(payload, apply_text_caps=False)


def sanitize_event_payload(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized copy of *payload* ready for protected audit/export.

    Two-layer policy:

    1. If *event* has a registered allowlist in :data:`_EVENT_SCHEMAS`, the
       top-level keys are filtered down to that allowlist (plus the common
       envelope fields ``event``/``event_seq``/``node``/``run_id``/
       ``timestamp``). Unknown keys, including credential-bearing
       provider fields like ``request_kwargs`` or ``raw_response``, are
       dropped before any value is inspected.
    2. The result is then passed through :func:`_sanitize_value`, which
       recursively drops sensitive keys, redacts URL query parameters
       such as ``api_key``/``token``/``sig``, and applies length caps to
       known free-text fields.

    Events without a schema entry (notably ``iteration_summary``) skip
    step 1 and rely solely on step 2. This is the hybrid policy: strict
    schemas for forensic-lineage events, drop-list defense-in-depth
    everywhere else.
    """
    schema = _EVENT_SCHEMAS.get(event)
    if schema is not None:
        allowed = schema | _COMMON_EVENT_KEYS
        payload = {k: v for k, v in payload.items() if k in allowed}
    if event == "inqtrix.agent.narration" and isinstance(payload.get("text"), str):
        # Narration is rendered verbatim as plain text.  It is a compact
        # process-status channel, never storage for an answer body.  Keep the
        # bound event-specific instead of weakening evidence/audit fields that
        # legitimately use the general 20k protected-log cap.
        payload = dict(payload)
        payload["text"] = shorten_visibly(
            payload["text"],
            _NARRATION_TEXT_CAP,
            marker=LOG_CUT_MARKER,
        )
    sanitized = _sanitize_value(payload)
    return sanitized if isinstance(sanitized, dict) else {}


def _console_token(value: Any) -> str | None:
    """Return one bounded operational token, never prose or a URL."""

    text = str(value or "").strip()
    if not text or not _CONSOLE_TOKEN_RE.fullmatch(text):
        return None
    return text


def _console_model_token(value: Any) -> str | None:
    """Return a provider/model label while rejecting URL- or path-shaped data."""

    text = str(value or "").strip()
    if (
        not text
        or not _CONSOLE_MODEL_RE.fullmatch(text)
        or "://" in text
        or text.startswith(("/", "./", "../"))
        or "//" in text
    ):
        return None
    return text


def _console_key_is_id(key: str) -> bool:
    normalized = key.lstrip("_")
    return normalized == "id" or normalized.endswith("_id")


def _console_key_is_id_list(key: str) -> bool:
    normalized = key.lstrip("_")
    return normalized == "ids" or normalized.endswith("_ids")


def _console_key_is_code_list(key: str) -> bool:
    normalized = key.lstrip("_")
    return normalized in {"codes", "reasons"} or normalized.endswith(
        ("_codes", "_reasons")
    )


def _console_numeric_key(key: str) -> bool:
    normalized = key.lstrip("_")
    return normalized in _CONSOLE_NUMERIC_FIELDS or normalized.endswith(
        _CONSOLE_NUMERIC_SUFFIXES
    )


def _console_boolean_key(key: str) -> bool:
    normalized = key.lstrip("_")
    return normalized in _CONSOLE_BOOLEAN_FIELDS or normalized.endswith(
        _CONSOLE_BOOLEAN_SUFFIXES
    )


def _project_console_value(key: str, value: Any) -> Any:
    """Project one audit value into the non-content operational log schema.

    Unknown values fail closed.  Mapping containers are traversed so useful
    counters inside e.g. ``usage`` or ``section_logs`` remain visible, while
    their prose siblings (headings, prompt previews, snippets) are omitted.
    """

    normalized_key = key.lstrip("_")

    if isinstance(value, dict):
        if normalized_key == "usage":
            usage: dict[str, int | float] = {}
            for raw_child_key, raw_child_value in value.items():
                child_key = str(raw_child_key)
                if (
                    _console_numeric_key(child_key)
                    and isinstance(raw_child_value, (int, float))
                    and not isinstance(raw_child_value, bool)
                ):
                    usage[child_key] = raw_child_value
            return usage or None

        if normalized_key in _CONSOLE_COUNT_MAP_FIELDS:
            counts: dict[str, int | float] = {}
            for raw_child_key, raw_child_value in value.items():
                child_key = _console_token(raw_child_key)
                if (
                    child_key is not None
                    and isinstance(raw_child_value, (int, float))
                    and not isinstance(raw_child_value, bool)
                ):
                    counts[child_key] = raw_child_value
            return counts or None

        projected: dict[str, Any] = {}
        for raw_child_key, raw_child_value in value.items():
            child_key = str(raw_child_key)
            child_value = _project_console_value(child_key, raw_child_value)
            if child_value is not None:
                projected[child_key] = child_value
        return projected or None

    if isinstance(value, (list, tuple)):
        if _console_key_is_id_list(key) or _console_key_is_code_list(key):
            tokens = [
                token
                for item in value
                if (token := _console_token(item)) is not None
            ]
            return tokens or None
        projected_items = [
            projected
            for item in value
            if isinstance(item, dict)
            and (projected := _project_console_value(key, item)) is not None
        ]
        return projected_items or None

    if isinstance(value, bool):
        return value if _console_boolean_key(key) else None

    if isinstance(value, (int, float)):
        return value if _console_numeric_key(key) else None

    if _console_key_is_id(key):
        return _console_token(value)

    if normalized_key in {"engine", "model"} or normalized_key.endswith("_model"):
        return _console_model_token(value)

    if (
        normalized_key in _CONSOLE_ENUM_FIELDS
        or normalized_key in _CONSOLE_CODE_FIELDS
        or normalized_key.endswith(
            (
                "_effort",
                "_kind",
                "_mode",
                "_phase",
                "_provider",
                "_status",
                "_tier",
                "_type",
            )
        )
    ):
        return _console_token(value)

    return None


def _console_iteration_projection(entry: dict[str, Any]) -> dict[str, Any]:
    """Return the fail-closed container/file-log view of an audit entry."""

    projected = _project_console_value("iteration", entry)
    if not isinstance(projected, dict):
        return {}
    # Keep the log line classifiable even for a future event whose content
    # fields are all intentionally withheld.
    projected.setdefault("event", "iteration_summary")
    return projected



def emit_runtime_event(event: str, payload: dict[str, Any]) -> None:
    """Attach one content-minimized runtime event to the current span.

    The ONE path for lineage events (stop cascades, answer sections,
    algorithm failures, iteration summaries). Recording requires an
    active span: ``INQTRIX_TRACING`` decides where those events are
    kept, ``OBSERVABILITY_PROFILE`` how deep they go, and the settings
    validator warns when depth is configured without a sink.

    The audit sanitizer first drops credentials and bounds retained
    fields; the stricter projection then admits only operational
    identifiers, codes, statuses, models, counters, usage and timings.
    Exact queries, URLs, evidence, prompts and provider prose therefore
    cannot reach the event even when a caller supplies them — full
    content lives ONLY on the dedicated content attributes, behind the
    capture policy.
    """
    if not span_is_recording():
        return
    event_payload = dict(payload)
    event_payload.setdefault("event", event)
    # The event hangs on its span, so trace and span ids need no
    # repeating. The request id does: it is the join key back to the
    # HTTP request and its log lines, and nothing on the span carries
    # it. ``setdefault`` never overwrites a caller-provided value.
    request_id = current_log_context().get("request_id")
    if request_id:
        event_payload.setdefault("request_id", request_id)
    sanitized = sanitize_event_payload(event, event_payload)
    console_payload = _console_iteration_projection(sanitized)
    console_event = str(console_payload.get("event", "iteration_summary"))
    # Attached to the CURRENT span (node/tool/run), so the waterfall
    # shows the lineage in place instead of in a separate stream that
    # has to be joined by hand.
    add_span_event(console_event, console_payload)


def normalize_source_provenance(
    result: GroundedSearchResult,
    *,
    query_id: str,
    provider: str,
    tier_explanations: dict[str, dict[str, str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build provider-neutral source and citation records for a search result.

    Args:
        result: A :class:`~inqtrix.search_result.GroundedSearchResult`. Each
            citation span of a :class:`~inqtrix.search_result.GroundedSource`
            becomes one citation record (a source without spans one record
            without offsets) and, de-duplicated by canonical URL, the source
            one source record. Per-source snippets are stored in full -- no
            cap.
        query_id: Stable ID of the query that produced the result.
        provider: Provider class label used for operator-facing lineage.
        tier_explanations: Optional mapping from canonical URL to
            ``{"tier": ..., "tier_reason": ...}``.

    Returns:
        Tuple of ``(source_records, citation_records)`` represented as
        plain dicts ready for state storage and structured logging.
    """
    grounded = sanitize_grounded_search_result(result)
    tier_explanations = tier_explanations or {}
    safe_tier_explanations = {
        normalize_url(redact_credential_url(str(url))): dict(explanation)
        for url, explanation in tier_explanations.items()
        if normalize_url(redact_credential_url(str(url)))
    }
    source_records: list[dict[str, Any]] = []
    citation_records: list[dict[str, Any]] = []
    seen_sources: set[str] = set()
    access_status = "answer" if grounded.answer else "empty_answer"

    for index, source in enumerate(grounded.sources, start=1):
        url = str(source.url or "").strip()
        canonical_url = normalize_url(url)
        if not canonical_url:
            continue
        try:
            safe_public_url_identity(url)
            source_access_status = access_status
        except CredentialBearingUrlError:
            source_access_status = "blocked_credentials"
        except ValueError:
            continue
        rank = int(source.rank or index)
        origin = str(source.origin or "provider_source")
        source_id = make_record_id("src", canonical_url)
        explanation = safe_tier_explanations.get(canonical_url, {})

        if source_id not in seen_sources:
            seen_sources.add(source_id)
            source_records.append(
                {
                    "source_id": source_id,
                    "url": url,
                    "canonical_url": canonical_url,
                    "domain": domain_from_url(canonical_url),
                    "provider": provider,
                    "first_seen_query_id": query_id,
                    "first_seen_rank": rank,
                    "origin": origin,
                    "tier": explanation.get("tier", "unknown"),
                    "tier_reason": explanation.get("tier_reason", ""),
                    "access_status": source_access_status,
                }
            )

        # One citation record per place of the answer that cites the source:
        # each carries the statement the review reads for it.
        for span in source.spans or (None,):
            citation_records.append(
                {
                    "citation_id": (
                        make_record_id("cit", query_id, rank, canonical_url, origin)
                        if span is None
                        else make_record_id(
                            "cit", query_id, rank, canonical_url, origin, span.start, span.end,
                        )
                    ),
                    "query_id": query_id,
                    "source_id": source_id,
                    "url": url,
                    "canonical_url": canonical_url,
                    "rank": rank,
                    "origin": origin,
                    "provider": provider,
                    "title": source.title,
                    "snippet": source.snippet,
                    "source_date": str(source.date or "")[:80],
                    "last_updated": str(source.last_updated or "")[:80],
                    "annotation_start": span.start if span else None,
                    "annotation_end": span.end if span else None,
                }
            )

    return source_records, citation_records


def sanitize_grounded_search_result(
    result: GroundedSearchResult,
) -> GroundedSearchResult:
    """Remove URL credentials before provider output enters run state.

    A credential-bearing source remains visible as a redacted, blocked
    discovery record instead of disappearing silently. Linked pages are not
    fetched. Product evidence is not truncated by telemetry limits. Provider
    usage metadata survives; the raw envelope remains exclusively in protected
    provider tracing, not in evidence prose or product state.
    """

    safe_answer = scrub_credential_urls(result.answer)
    offsets_preserved = safe_answer == result.answer
    safe_sources: list[GroundedSource] = []
    for source in result.sources:
        raw_url = str(source.url or "").strip()
        if not raw_url:
            continue
        try:
            safe_url = safe_public_url_identity(raw_url).url
        except CredentialBearingUrlError:
            safe_url = redact_credential_url(raw_url)
        except ValueError:
            # An invalid URL cannot be a durable source identity.  Its title
            # and snippet may still be represented by the provider synthesis,
            # but the malformed target itself is not persisted.
            continue
        safe_sources.append(
            GroundedSource(
                url=safe_url,
                title=scrub_credential_urls(source.title),
                snippet=scrub_credential_urls(source.snippet),
                date=scrub_credential_urls(source.date),
                last_updated=scrub_credential_urls(
                    source.last_updated
                ),
                rank=source.rank,
                origin=source.origin,
                spans=source.spans if offsets_preserved else (),
            )
        )
    return GroundedSearchResult(
        answer=safe_answer,
        sources=safe_sources,
        related_questions=[
            scrub_credential_urls(value)
            for value in result.related_questions
        ],
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        model=result.model,
        cache_read_tokens=result.cache_read_tokens,
        cache_creation_tokens=result.cache_creation_tokens,
        reasoning_tokens=result.reasoning_tokens,
        tool_call_count=result.tool_call_count,
    )


def forensic_enabled(settings: AgentSettings) -> bool:
    """Return whether detailed lineage events should be emitted."""
    return str(getattr(settings, "observability_profile", "summary")).lower() == "forensic"


def unwrap_provider(provider: object) -> object:
    """Return the innermost provider behind adapter/tracing shells.

    Follows the ``_provider`` chain (ConfiguredLLMProvider, the tracing
    wrappers) to a fixed point so run-start metadata names the real
    backend class, never a wrapper.
    """
    seen: set[int] = set()
    current = provider
    while id(current) not in seen:
        seen.add(id(current))
        wrapped = getattr(current, "_provider", None)
        if wrapped is None:
            break
        current = wrapped
    return current


def _clean_value(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    return value


def describe_llm_provider(provider: object) -> dict[str, Any]:
    """Extract human-readable runtime metadata for the active LLM provider."""
    resolved = unwrap_provider(provider)
    metadata: dict[str, Any] = {
        "provider": type(resolved).__name__,
    }

    models = getattr(provider, "models", None)
    if models is not None:
        metadata["reasoning_model"] = str(getattr(models, "reasoning_model", "") or "")
        metadata["classify_model"] = str(
            getattr(models, "effective_classify_model", "")
            or getattr(models, "classify_model", "")
            or getattr(models, "reasoning_model", "")
            or ""
        )
        metadata["claim_extract_model"] = str(
            getattr(models, "effective_claim_extract_model", "")
            or getattr(models, "claim_extract_model", "")
            or getattr(models, "reasoning_model", "")
            or ""
        )
        metadata["evaluate_model"] = str(
            getattr(models, "effective_evaluate_model", "")
            or getattr(models, "evaluate_model", "")
            or getattr(models, "reasoning_model", "")
            or ""
        )

    for attr_name in (
        "_default_max_tokens",
        "_context_window_tokens",
        "_token_budget_parameter",
        "_temperature",
    ):
        cleaned = _clean_value(getattr(resolved, attr_name, None))
        if cleaned is not None:
            metadata[attr_name.lstrip("_")] = cleaned

    thinking = getattr(resolved, "_thinking", None)
    if isinstance(thinking, dict) and thinking:
        metadata["thinking"] = dict(thinking)

    return metadata


def describe_search_provider(provider: object) -> dict[str, Any]:
    """Extract human-readable runtime metadata for the active search provider."""
    resolved = unwrap_provider(provider)
    metadata: dict[str, Any] = {
        "provider": type(resolved).__name__,
    }

    search_model = _clean_value(getattr(resolved, "search_model", None))
    model = _clean_value(getattr(resolved, "_model", None))
    agent_name = _clean_value(getattr(resolved, "_agent_name", None))
    agent_version = _clean_value(getattr(resolved, "_agent_version", None))
    agent_id = _clean_value(getattr(resolved, "_agent_id", None))

    if search_model is not None:
        metadata["engine"] = str(search_model)
    elif model is not None:
        metadata["engine"] = str(model)
    elif agent_name is not None:
        engine = str(agent_name)
        if agent_version is not None:
            engine = f"{engine}@{agent_version}"
        metadata["engine"] = engine
        metadata["agent_name"] = str(agent_name)
        if agent_version is not None:
            metadata["agent_version"] = str(agent_version)
    elif agent_id is not None:
        metadata["engine"] = str(agent_id)
        metadata["agent_id"] = str(agent_id)

    return metadata


def build_run_metadata(
    *,
    question: str,
    history: str,
    providers: Any,
    settings: AgentSettings,
    run_mode: str = "run",
) -> dict[str, Any]:
    """Build a structured payload for the start of a research run."""
    return {
        "event": "run_start",
        "run_mode": run_mode,
        "question_length": len(question or ""),
        "history_length": len(history or ""),
        "llm": describe_llm_provider(getattr(providers, "llm", None)),
        "search": describe_search_provider(getattr(providers, "search", None)),
        "settings": {
            "report_profile": str(settings.report_profile),
            "observability_profile": str(getattr(settings, "observability_profile", "summary")),
            "max_rounds": settings.max_rounds,
            "confidence_stop": settings.confidence_stop,
            "required_context_window_tokens": settings.required_context_window_tokens,
            "max_total_seconds": settings.max_total_seconds,
            "testing_mode": settings.testing_mode,
        },
    }


def log_run_start(
    *,
    question: str,
    history: str,
    providers: Any,
    settings: AgentSettings,
    run_mode: str = "run",
    run_id: str | None = None,
) -> None:
    """Write a compact start banner plus structured debug metadata."""
    metadata = build_run_metadata(
        question=question,
        history=history,
        providers=providers,
        settings=settings,
        run_mode=run_mode,
    )
    if run_id:
        metadata["run_id"] = run_id

    llm = metadata["llm"]
    search = metadata["search"]
    run_settings = metadata["settings"]

    safe_run_id = _console_token(metadata.get("run_id")) or "-"
    safe_run_mode = _console_token(run_mode) or "unknown"
    safe_report_profile = (
        _console_token(run_settings.get("report_profile")) or "unknown"
    )
    safe_observability_profile = (
        _console_token(run_settings.get("observability_profile")) or "unknown"
    )
    safe_llm_provider = _console_token(llm.get("provider")) or "unknown"
    safe_search_provider = _console_token(search.get("provider")) or "unknown"

    log.info(
        "RUN start: id=%s mode=%s profile=%s observability=%s llm=%s reasoning=%s classify=%s evaluate=%s claim_extract=%s default_max_tokens=%s context_window_tokens=%s required_context_window_tokens=%s search=%s engine=%s max_rounds=%d confidence_stop=%d max_total_seconds=%d testing_mode=%s question_len=%d history_len=%d",
        safe_run_id,
        safe_run_mode,
        safe_report_profile,
        safe_observability_profile,
        safe_llm_provider,
        _console_model_token(llm.get("reasoning_model")) or "-",
        _console_model_token(llm.get("classify_model")) or "-",
        _console_model_token(llm.get("evaluate_model")) or "-",
        _console_model_token(llm.get("claim_extract_model")) or "-",
        llm.get("default_max_tokens") if llm.get("default_max_tokens") is not None else "-",
        llm.get("context_window_tokens") if llm.get("context_window_tokens") is not None else "-",
        run_settings.get("required_context_window_tokens") or "-",
        safe_search_provider,
        _console_model_token(search.get("engine")) or "-",
        run_settings["max_rounds"],
        run_settings["confidence_stop"],
        run_settings["max_total_seconds"],
        run_settings["testing_mode"],
        metadata["question_length"],
        metadata["history_length"],
    )
    emit_runtime_event("run_start", metadata)


def log_run_end(
    *,
    run_id: str | None,
    run_mode: str,
    status: str,
    elapsed_s: float,
    state: dict[str, Any],
    reason: str = "",
) -> None:
    """Write a normalized run-end event through the existing logger."""
    payload = {
        "event": "run_end",
        "run_id": run_id or state.get("_run_id", ""),
        "run_mode": run_mode,
        "status": status,
        "reason": reason or state.get("_stop_reason", ""),
        "elapsed_s": round(elapsed_s, 3),
        "round": state.get("round", 0),
        "done": bool(state.get("done", False)),
        "cancelled": bool(state.get("cancelled", False)),
        "final_confidence": state.get("final_confidence", 0),
        "total_citations": len(state.get("all_citations", []) or []),
        "evidence_record_count": len(state.get("evidence_ledger", []) or []),
        "consolidated_claims_count": len(claim_assessments(state)),
        "algorithm_failure_count": len(state.get("algorithm_failures", []) or []),
        "blocking_algorithm_failure_count": sum(
            1
            for failure in state.get("algorithm_failures", []) or []
            if isinstance(failure, dict) and bool(failure.get("blocking"))
        ),
        "total_prompt_tokens": state.get("total_prompt_tokens", 0),
        "total_completion_tokens": state.get("total_completion_tokens", 0),
    }
    log.info(
        "RUN end: id=%s mode=%s status=%s reason=%s elapsed=%.1fs round=%s confidence=%s citations=%s",
        _console_token(payload["run_id"]) or "-",
        _console_token(run_mode) or "unknown",
        _console_token(status) or "unknown",
        _console_token(payload["reason"]) or "-",
        elapsed_s,
        payload["round"],
        payload["final_confidence"],
        payload["total_citations"],
    )
    emit_runtime_event("run_end", payload)


def log_iteration_entry(entry: dict[str, Any]) -> None:
    """Write the non-content operational projection to DEBUG logs.

    ``entry`` itself is the protected audit representation and is not mutated.
    Exact queries, provider prose, source/claim/evidence text, prompt views and
    URLs therefore remain available to the authorized run result while never
    being mirrored into ordinary container or file logs.
    """
    console_entry = _console_iteration_projection(entry)
    event = str(console_entry.get("event", "iteration_summary"))
    emit_runtime_event(event, console_entry)
