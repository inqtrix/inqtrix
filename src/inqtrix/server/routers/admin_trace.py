"""Instance-admin trace surface (``/v1/admin/runs/{run_id}/trace*``).

The admin drill-down from a run to its raw trace. ``GET .../trace/export``
returns the FULL trace document from whichever sink the Baukasten runs —
the Langfuse REST API in ``otlp`` mode or the OTLP-JSON file spool in
``file`` mode (that document stays replayable). In ``off``/``local`` mode
the endpoint answers with a clear 409 instead of pretending an export
exists.

SESSION-ONLY and instance-admin gated like every admin surface; the
run lookup itself is deliberately visibility-free (``owner_user_id``
precedent — authorization happened at the guard). Every export is
written to the audit trail BEFORE the response: raw traces can carry
forensic content, so handing one out is exactly the kind of action the
trail exists for.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.auth.permissions import AuditEntry
from inqtrix.observability.trace_readers import (
    TraceExportUnavailable,
    build_trace_reader,
)
from inqtrix.server.routers._admin_guard import TENANT, require_instance_admin
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def _trace_ui_url(settings, html_path: str | None) -> str | None:
    """Join INQTRIX_TRACE_UI_URL with the htmlPath from the trace API."""
    base = str(settings.observability.trace_ui_url or "").strip().rstrip("/")
    if not base or not html_path:
        return None
    return f"{base}/{html_path.lstrip('/')}"



async def _trace_id_from_audit(audit, run_id: str) -> str | None:
    """Recover a deleted run's trace id from the audit row that outlives it.

    The terminal run row carries ``correlation.trace_id`` and is retained
    under the audit retention window, so forensics stay possible for exactly
    the case that matters most: after the run was deleted.
    """
    try:
        rows, _ = await audit.list_audit_entries(
            tenant_id=TENANT,
            resource_type="run",
            resource_id=run_id,
            limit=50,
        )
    except Exception:  # noqa: BLE001 - a missing trace id is not an outage
        return None
    for row in rows:
        candidate = (row.get("correlation") or {}).get("trace_id")
        if candidate:
            return str(candidate)
    return None

def build_router(container: "AppContainer") -> APIRouter:
    """Bind the admin trace surface against the container."""
    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency
    run_store = container.run_store
    audit = container.permission_service.audit_sink
    settings = container.settings

    @router.get("/v1/admin/runs/{run_id}/trace/export")
    async def export_trace(run_id: str, request: Request):
        """Export the run's full trace from Langfuse or the file spool."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        principal, _session, _mirror = resolved

        # Store round-trips block; keep the event loop free.
        trace_id = await asyncio.to_thread(run_store.trace_id, run_id)
        if trace_id is None:
            # run_events cascades with the run; the WORM audit row does not.
            # Primary source stays run_events because a RUNNING run has no
            # terminal audit row yet. The client-held trace id is deliberately
            # not accepted here: no caller-supplied identifier reaches a
            # trace fetch.
            trace_id = await _trace_id_from_audit(audit, run_id)
        if trace_id is None:
            return error_response(
                404,
                ("No trace ID is known for this run; either the run does not exist or it"
                " ran with INQTRIX_TRACING=off."),
                "not_found",
            )
        try:
            reader = build_trace_reader(settings)
            export = await asyncio.to_thread(
                reader.get_trace, run_id, trace_id
            )
        except TraceExportUnavailable as exc:
            return error_response(409, str(exc), "conflict")
        except Exception:  # noqa: BLE001 — a down Langfuse is routine ops
            log.warning(
                "Trace export failed for run %s.",
                run_id,
                exc_info=True,
            )
            return error_response(
                502,
                ("The trace backend is currently unreachable; check the Langfuse service"
                " and OTEL_EXPORTER_OTLP_ENDPOINT."),
                "bad_gateway",
            )

        # Audit BEFORE answering: raw traces can carry forensic content
        # (prompts, provider responses) — a failing sink fails loudly.
        await audit.record(
            AuditEntry(
                tenant_id=TENANT,
                actor_user_id=principal.user_id,
                action="export.trace",
                resource_type="run",
                resource_id=run_id,
                detail={
                    "trace_id": trace_id,
                    "source": export.source,
                },
            )
        )
        document = export.as_document()
        ui_url = _trace_ui_url(settings, export.html_path)
        if ui_url:
            document["ui_url"] = ui_url
        return document

    return router
