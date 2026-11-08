"""Enriched audit writing for the event catalog (OCSF-oriented).

:mod:`inqtrix.auth.audit_entries` builds the entry and its envelope; this
module writes it.

Two failure disciplines, chosen PER CALL SITE:

* Security-relevant admin actions keep the established FAIL-LOUD path
  (call ``sink.record`` directly and let a failing sink fail the
  request — e.g. pseudonym resolution, trace export).
* Lifecycle telemetry (service starts, uploads, deletions) uses
  :meth:`AuditService.record` — FAIL-SAFE but never fail-SILENT: an
  audit outage must not take down runs, and every swallowed failure is
  a WARNING.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from inqtrix.auth.audit_entries import build_audit_entry
from inqtrix.auth.permissions import AuditEntry
from inqtrix.observability.context import current_log_context

if TYPE_CHECKING:
    from inqtrix.auth.permissions import AuditSink

log = logging.getLogger(__name__)


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
