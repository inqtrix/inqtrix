"""One place builds the full audit envelope, so every writer stays a one-liner.

The columns then fill consistently:

* ``correlation`` — request_id/run_id from the ambient log context plus
  the active trace id, the drill-down join keys into JSON logs and
  Langfuse.
* ``origin`` — ip/user_agent/auth_method from
  :mod:`inqtrix.auth.request_origin` (empty for worker-side events —
  absence is meaningful).
* ``actor_pseudonym`` — the stable ``usr_<hex16>`` computed at write
  time, so the admin panel lists exactly the identifier logs and traces
  carry.
"""

from __future__ import annotations

import uuid
from typing import Mapping

from inqtrix.auth.log_redaction import stable_pseudonym
from inqtrix.auth.permissions import AuditEntry
from inqtrix.auth.request_origin import current_request_origin
from inqtrix.observability.context import current_log_context
from inqtrix.observability.otel import current_trace_id_hex


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
