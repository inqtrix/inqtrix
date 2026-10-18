"""Shared semantic telemetry projections for both Agent engines."""

from __future__ import annotations

from typing import Any

from inqtrix.i18n import t


_MODEL_RETRY_CONTEXT: dict[str, tuple[str, str]] = {
    # (translation key suffix, phase). The label is resolved at emission
    # time against the run's answer language — these lines surface exactly
    # when a provider or model call is being retried, so they must read in
    # the reader's language.
    "agent_intake": ("intake", "intake"),
    "agent_discovery_analyst": ("discovery_analyst", "discovery"),
    "agent_skill_point_check": ("skill_point_check", "intake"),
    "agent_plan": ("plan", "planning"),
    "agent_contradiction": ("contradiction", "evidence"),
    "agent_sufficiency": ("sufficiency", "evidence"),
    "agent_synthesis": ("synthesis", "synthesis"),
    "agent_citation_repair": ("citation_repair", "synthesis"),
    "agent_answer": ("answer", "synthesis"),
    "agent_answer_light": ("answer", "synthesis"),
    "agent_memory_reflection": ("memory_reflection", "critic"),
    "agent_file_analysis": ("file_analysis", "execution"),
    "agent_patch": ("patch", "patch"),
    "agent_kernel": ("kernel", "execution"),
    "agent_context_projection": ("context_projection", "execution"),
    "agent_deep_review": ("deep_review", "critic"),
    "agent_model": ("model", "execution"),
}


def provider_retry_activity(
    notice: dict[str, object],
    *,
    task_id: str = "",
    purpose: str = "",
    scope: str = "task",
    phase: str = "execution",
    kind: str = "searching",
    language: str = "",
) -> dict[str, Any]:
    """Project a provider retry notice onto the existing activity event.

    The provider owns retry policy and exact timeout diagnostics; this helper
    only turns the notice into the one Agent activity vocabulary consumed by
    the timeline and task detail views.
    """
    failed_attempt = max(1, int(notice.get("attempt", 1) or 1))
    max_attempts = max(
        failed_attempt,
        int(notice.get("max_attempts", failed_attempt) or failed_attempt),
    )
    next_attempt = min(max_attempts, failed_attempt + 1)
    delay_seconds = max(0.0, float(notice.get("delay_seconds", 0.0) or 0.0))
    query = str(notice.get("query") or "").strip()
    error_code = str(
        notice.get("error_code")
        or notice.get("status_code")
        or "transient_error"
    )
    retry_text = t(
        {"language": language or ""},
        "telemetry_retry_text",
        failed=failed_attempt,
        max=max_attempts,
        code=error_code,
        next=next_attempt,
        delay=f"{delay_seconds:.1f}",
    )
    return {
        "kind": kind,
        "scope": scope,
        "phase": phase,
        "operation": str(notice.get("operation") or "web.search.instant"),
        # The bare query keeps the retry notice on the SAME transcript
        # row as its invocation (the FE keys rows by query/detail) —
        # without it every retry opened a ghost row that never settled.
        "query": query or None,
        "detail": " · ".join(
            part for part in (query or purpose, retry_text) if part
        ),
        "status": "started",
        "attempt": next_attempt,
        "task_id": task_id or None,
        "purpose": purpose or None,
        "retry": {
            "failed_attempt": failed_attempt,
            "next_attempt": next_attempt,
            "max_attempts": max_attempts,
            "delay_seconds": delay_seconds,
            "error_code": error_code,
        },
        "configured_timeout_seconds": notice.get(
            "configured_timeout_seconds"
        ),
        "effective_timeout_seconds": notice.get(
            "effective_timeout_seconds"
        ),
        "transport_timeout_seconds": notice.get(
            "transport_timeout_seconds"
        ),
    }


def model_retry_activity(
    notice: dict[str, object],
    *,
    node: str,
    language: str = "",
) -> dict[str, Any]:
    """Project one LLM retry with a user-facing Agent phase description."""
    key, phase = _MODEL_RETRY_CONTEXT.get(
        node,
        _MODEL_RETRY_CONTEXT["agent_model"],
    )
    purpose = t({"language": language or ""}, f"telemetry_{key}")
    return provider_retry_activity(
        notice,
        purpose=purpose,
        scope="run",
        phase=phase,
        kind="working",
        language=language,
    )
