"""Enriched audit writing for the event catalog (OCSF-oriented).

ONE place builds the full audit envelope so every writer stays a
one-liner and the columns fill consistently:

* ``correlation`` — request_id/run_id from the ambient log context plus
  the active trace id, the drill-down join keys into JSON logs and
  Langfuse.
* ``origin`` — ip/user_agent/auth_method from
  :mod:`inqtrix.auth.request_origin` (empty for worker-side events —
  absence is meaningful).
* ``actor_pseudonym`` — the stable ``usr_<hex16>`` computed at write
  time, so the admin panel lists exactly the identifier logs and traces
  carry.

Two failure disciplines, chosen PER CALL SITE:

* Security-relevant admin actions keep the established FAIL-LOUD path
  (call ``sink.record`` directly and let a failing sink fail the
  request — e.g. pseudonym resolution, trace export).
* Lifecycle telemetry (service starts, uploads, deletions) uses
  :meth:`AuditService.record` — FAIL-SAFE but never fail-SILENT: an
  audit outage must not take down runs, and every swallowed failure is
  a WARNING (see feedback rule "keine stillen Fallbacks").
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any, Mapping

from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.auth.permissions import AuditEntry
from inqtrix.auth.request_origin import current_request_origin
from inqtrix.observability.context import current_log_context
from inqtrix.observability.otel import current_trace_id_hex

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuditSink

log = logging.getLogger("inqtrix")



def ambient_audit_envelope() -> tuple[dict[str, str], dict[str, str]]:
    """Return ``(origin, correlation)`` derived from the ambient context.

    The single derivation both audit entry points share: the sink path via
    :func:`build_audit_entry` and the in-transaction path via
    :func:`~inqtrix.storage.resource_access.append_audit_row`. Keeping it in
    one place is what stops one family of rows from being filterable in the
    admin panel while another is not.
    """
    context = current_log_context()
    correlation: dict[str, str] = {}
    request_id = str(context.get("request_id") or "")
    if request_id:
        correlation["request_id"] = request_id
    run_id = str(context.get("run_id") or "")
    if run_id:
        correlation["run_id"] = run_id
    trace_id = current_trace_id_hex() or ""
    if trace_id:
        correlation["trace_id"] = trace_id
    return current_request_origin(), correlation


def build_audit_entry(
    *,
    tenant_id: str,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_user_id: uuid.UUID | None = None,
    actor_type: str = "user",
    outcome: str = "success",
    detail: Mapping[str, str] | None = None,
    origin: Mapping[str, str] | None = None,
    correlation: Mapping[str, str] | None = None,
    workspace_id: uuid.UUID | None = None,
    run_id: str | None = None,
    trace_id: str | None = None,
) -> AuditEntry:
    """Assemble one entry with the ambient envelope filled in.

    Explicit ``origin``/``correlation`` values win over ambient ones —
    a route that resolved the client ip itself (login failures happen
    BEFORE a principal exists) passes them directly.
    """
    merged_origin, merged_correlation = ambient_audit_envelope()
    # Explicit ids win over the ambient ones: a caller that already knows the
    # run or trace it is recording is a better source than the context.
    if run_id:
        merged_correlation["run_id"] = run_id
    if trace_id:
        merged_correlation["trace_id"] = trace_id
    if correlation:
        merged_correlation.update(
            {k: str(v) for k, v in correlation.items() if v}
        )
    if origin:
        merged_origin.update({k: str(v) for k, v in origin.items() if v})

    return AuditEntry(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=dict(detail or {}),
        actor_type=actor_type,
        outcome=outcome,
        origin=merged_origin,
        correlation=merged_correlation,
        actor_pseudonym=(
            stable_pseudonym("usr", actor_user_id)
            if actor_user_id is not None
            else None
        ),
        workspace_id=workspace_id,
    )


class AuditService:
    """Fail-safe (never fail-SILENT) writer for lifecycle audit events."""

    def __init__(self, sink: "AuditSink") -> None:
        self._sink = sink

    async def record(self, entry: AuditEntry) -> None:
        """Write one entry; a sink failure warns loudly, never raises.

        For the lifecycle catalog only — security-critical admin actions
        must call the sink directly so a broken trail fails the request.
        """
        try:
            await self._sink.record(entry)
        except Exception:  # noqa: BLE001 — lifecycle audit must not kill runs
            # resource_id is attacker-influenced and for auth.* events IS
            # the attempted login identifier (an email address) — it must
            # never reach the log stream. Action + resource type identify
            # the failing writer well enough.
            log.warning(
                "Audit entry %s (%s) could not be written; the operation itself "
                "continued.",
                entry.action,
                entry.resource_type,
                exc_info=True,
            )

    async def record_event(self, **kwargs: Any) -> None:
        """Convenience: :func:`build_audit_entry` + fail-safe write."""
        await self.record(build_audit_entry(**kwargs))


async def audit_chat_completed(
    sink: "AuditSink | None",
    principal: Any,
    *,
    usage: Mapping[str, Any] | None,
    streamed: bool,
    failed: bool,
    enabled: bool,
    reason: str = "",
) -> None:
    """Service-start index row for one chat completion (both mouths).

    Chat has no run id — the ambient request id is the correlation
    anchor AND the resource id, so the row greps straight into the JSON
    logs.

    A mid-execution failure ALWAYS writes a row, with or without usage:
    a timed-out or crashed turn is exactly what an operator looks for in
    the index, and leaving it out would make the trail claim the request
    never happened. ``reason`` names the terminal condition. Only
    pre-execution rejects (nothing ran, no reason) stay unrecorded.
    """
    if sink is None or not enabled:
        return
    if failed and not usage and not reason:
        return
    detail: dict[str, str] = {"streamed": "true" if streamed else "false"}
    if reason:
        detail["reason"] = reason
    if usage:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if prompt_tokens or completion_tokens:
            detail["prompt_tokens"] = str(prompt_tokens)
            detail["completion_tokens"] = str(completion_tokens)
    request_id = str(current_log_context().get("request_id") or "chat")
    await AuditService(sink).record_event(
        tenant_id=getattr(principal, "tenant_id", "") or "default",
        actor_user_id=getattr(principal, "user_id", None),
        action="chat.completed",
        resource_type="chat",
        resource_id=request_id,
        outcome="failure" if failed else "success",
        detail=detail,
    )
