"""Instance-admin audit surface (``/v1/admin/audit/*``).

The audit READ layer: the filterable list the admin panel
renders, the NDJSON/CSV export, the per-run event drill-down for the
run drawer, and pseudonym resolution. Like every administrative surface
it is SESSION-ONLY and instance-admin gated via
:func:`require_instance_admin`.

Pseudonym resolution deliberately needs no lookup table: the stable
references are deterministic (HMAC over the canonical user UUID with the
instance pepper), so the match is recomputed over the tenant's user
directory. Every resolution — hit or miss — is itself written to the
durable audit trail, because re-identification is exactly the kind of
action the trail exists for. The bulk export writes an ``export.audit``
row BEFORE streaming for the same reason.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from inqtrix.auth.log_redaction import (
    stable_pseudonym,
    stable_pseudonyms_active,
)
from inqtrix.auth.permissions import AuditEntry
from inqtrix.server.routers._admin_guard import TENANT, require_instance_admin
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

_CSV_COLUMNS = (
    "id",
    "occurred_at",
    "action",
    "outcome",
    "actor_pseudonym",
    "actor_type",
    "resource_type",
    "resource_id",
    "workspace_id",
    "detail",
    "origin",
    "correlation",
)


def _csv_cell(value: Any) -> Any:
    """CSV cell rendering with spreadsheet-formula neutralization.

    resource_id can carry ATTACKER-CONTROLLED text (the attempted login
    identifier of auth.login_failed rows) — a leading = + - @ or tab
    would execute as a formula when the export is opened in Excel/Calc.
    OWASP mitigation: prefix such cells with a single quote.
    """
    if isinstance(value, dict):
        rendered = json.dumps(value, ensure_ascii=False)
    elif value is None:
        return ""
    else:
        rendered = value
    if isinstance(rendered, str) and rendered[:1] in ("=", "+", "-", "@", "\t"):
        return f"'{rendered}"
    return rendered


def _parse_audit_filters(request: Request) -> dict[str, Any] | None:
    """Query params → reader kwargs; ``None`` signals a 400 (bad cursor)."""
    params = request.query_params
    filters: dict[str, Any] = {
        "action_prefix": params.get("action", "").strip(),
        "actor_pseudonym": params.get("actor", "").strip(),
        "outcome": params.get("outcome", "").strip(),
        "resource_type": params.get("resource_type", "").strip(),
    }
    for query_name, kwarg in (
        ("from", "occurred_from"),
        ("to", "occurred_to"),
    ):
        raw = params.get(query_name, "").strip()
        if raw:
            try:
                filters[kwarg] = float(raw)
            except ValueError:
                return None
    cursor = params.get("cursor", "").strip()
    if cursor:
        try:
            filters["before_id"] = int(cursor)
        except ValueError:
            return None
    return filters

# Only user pseudonyms are resolvable: resource/tenant references have no
# admin-facing directory to recompute against, and users are the subject
# an operator actually needs to re-identify.
_PSEUDONYM_PATTERN = re.compile(r"^usr_[0-9a-f]{16}$")


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the audit admin surface against the container."""
    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency
    users = provider.users
    audit = container.permission_service.audit_sink
    # getattr-defensive: focused test containers compose this router
    # without a run store; the events route then answers 503.
    run_store = getattr(container, "run_store", None)

    @router.get("/v1/admin/audit")
    async def list_audit(request: Request):
        """Filterable, newest-first audit page for the admin panel."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        filters = _parse_audit_filters(request)
        if filters is None:
            return error_response(
                400, "Invalid cursor or time filter", "invalid_cursor"
            )
        try:
            limit = int(request.query_params.get("limit", "50"))
        except ValueError:
            return error_response(
                400, "Invalid limit", "invalid_request_error"
            )
        rows, next_before = await audit.list_audit_entries(
            tenant_id=TENANT, limit=limit, **filters
        )
        return {
            "object": "list",
            "data": rows,
            "next_cursor": str(next_before) if next_before else None,
        }

    @router.get("/v1/admin/audit/export")
    async def export_audit(request: Request):
        """Stream the filtered trail as NDJSON or CSV (audited itself).

        A supplied ``cursor`` deliberately RESUMES the export from that
        keyset position (partial export by request); omit it for the
        full trail — the panel's export buttons always omit it.
        """
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        principal, _session, _mirror = resolved
        filters = _parse_audit_filters(request)
        if filters is None:
            return error_response(
                400, "Invalid cursor or time filter", "invalid_cursor"
            )
        export_format = request.query_params.get("format", "ndjson")
        if export_format not in ("ndjson", "csv"):
            return error_response(
                400,
                "format must be ndjson or csv",
                "invalid_request_error",
            )
        # Audit BEFORE streaming (fail-loud): handing out the trail in
        # bulk is exactly the kind of action the trail exists for.
        await audit.record(
            AuditEntry(
                tenant_id=TENANT,
                actor_user_id=principal.user_id,
                action="export.audit",
                resource_type="audit_log",
                resource_id=export_format,
                detail={
                    key: str(value)
                    for key, value in filters.items()
                    if value
                },
            )
        )

        async def _pages() -> AsyncIterator[list[dict[str, Any]]]:
            before_id = filters.pop("before_id", None)
            while True:
                rows, next_before = await audit.list_audit_entries(
                    tenant_id=TENANT,
                    limit=200,
                    before_id=before_id,
                    **filters,
                )
                if rows:
                    yield rows
                if not next_before:
                    return
                before_id = next_before

        async def _ndjson() -> AsyncIterator[bytes]:
            try:
                async for rows in _pages():
                    for row in rows:
                        yield (
                            json.dumps(row, ensure_ascii=False).encode()
                            + b"\n"
                        )
            except Exception:
                # Headers are already committed (200) — the client gets
                # a truncated file. Never let that stay silent app-side.
                log.warning(
                    "Audit export (ndjson) aborted mid-stream; the client download is "
                    "incomplete.",
                    exc_info=True,
                )
                raise

        async def _csv() -> AsyncIterator[bytes]:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS)
            writer.writeheader()
            yield buffer.getvalue().encode()
            try:
                async for rows in _pages():
                    buffer.seek(0)
                    buffer.truncate()
                    for row in rows:
                        writer.writerow(
                            {
                                column: _csv_cell(row.get(column))
                                for column in _CSV_COLUMNS
                            }
                        )
                    yield buffer.getvalue().encode()
            except Exception:
                log.warning(
                    "Audit export (csv) aborted mid-stream; the client download is "
                    "incomplete.",
                    exc_info=True,
                )
                raise

        media_type = (
            "application/x-ndjson" if export_format == "ndjson" else "text/csv"
        )
        return StreamingResponse(
            _ndjson() if export_format == "ndjson" else _csv(),
            media_type=media_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="audit-export.{export_format}"'
                ),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @router.get("/v1/admin/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request):
        """Durable step list for the admin run drawer (visibility-free)."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        principal, _session, _mirror = resolved
        if run_store is None:
            return error_response(
                503, "Run store is unavailable", "server_error"
            )
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            return error_response(
                400, "Invalid after parameter", "invalid_request_error"
            )
        events = await asyncio.to_thread(
            run_store.events_snapshot, run_id, after=after
        )
        # Audit BEFORE answering, like the sibling trace export: this
        # returns ANOTHER user's run steps, which carry user-derived
        # content (queries, progress prose). An admin reading foreign
        # run data is exactly the access an audit trail must record.
        if audit is not None:
            await audit.record(
                AuditEntry(
                    tenant_id=TENANT,
                    actor_user_id=principal.user_id,
                    action="admin.run_events_read",
                    resource_type="run",
                    resource_id=run_id,
                    detail={"event_count": str(len(events))},
                )
            )
        return {"object": "list", "data": events}

    @router.post("/v1/admin/audit/resolve-pseudonym")
    async def resolve_pseudonym(request: Request):
        """Re-identify one ``usr_<hex16>`` pseudonym for an instance admin."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        principal, _session, _mirror = resolved
        if not stable_pseudonyms_active():
            # Without the instance pepper the references are process-local:
            # recomputing them here would only ever match pseudonyms this
            # very process wrote — a misleading half-answer, so refuse.
            return error_response(
                409,
                ("Pseudonym resolution requires INQTRIX_PSEUDONYM_PEPPER to be set; "
                "without it, pseudonyms are stable only within a process."),
                "conflict",
            )
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        pseudonym = str((body or {}).get("pseudonym", "")).strip().lower()
        if not _PSEUDONYM_PATTERN.fullmatch(pseudonym):
            return error_response(
                400,
                "Field 'pseudonym' must match usr_<16 hex characters>",
                "invalid_request_error",
            )
        rows = await users.list_users(tenant_id=TENANT)
        match = next(
            (
                row
                for row in rows
                if stable_pseudonym("usr", row.user_id) == pseudonym
            ),
            None,
        )
        # Audit BEFORE answering: a re-identification without a trail must
        # not exist, so a failing sink fails the request loudly.
        await audit.record(
            AuditEntry(
                tenant_id=TENANT,
                actor_user_id=principal.user_id,
                action="audit.pseudonym_resolved",
                resource_type="user",
                resource_id=str(match.user_id) if match else pseudonym,
                detail={
                    "pseudonym": pseudonym,
                    "found": "true" if match else "false",
                },
            )
        )
        if match is None:
            return {"found": False}
        return {
            "found": True,
            "user": {
                "id": str(match.user_id),
                "email": match.email,
                "display_name": match.display_name,
            },
        }

    return router
