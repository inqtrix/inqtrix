"""Quota endpoints: the caller's own meter + the instance-admin surface.

Two audiences, one router (mounted only when ``quota_service`` is wired,
i.e. a cookie-session mode with quotas on):

* ``GET /v1/quota/usage`` — the authenticated caller's own usage per
  dimension. Feeds the composer meters; available to any scoped
  principal.
* ``/v1/admin/quota*`` — the editable admin surface, gated to the instance
  admin (``instance_role == "admin"``, 404-not-403 house convention): an
  overview of metered users with their usage and effective limits, plus
  setting the tenant default / per-user overrides and resetting flow usage.

Quota accounting is keyed by the canonical ``(tenant_id, user_id)`` pair.
Administration is tenant-wide, so it lives on the instance-admin axis —
never on workspace ownership (a workspace owner administers collaboration,
not the deployment's quotas). Every admin mutation is audited
(``quota.override`` / ``quota.override_cleared`` / ``quota.reset``).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.auth.permissions import AuditEntry
from inqtrix.quota.models import QuotaDimension, period_end
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")


def _parse_dimension(value: object) -> QuotaDimension | None:
    """Parse a dimension string, or ``None`` when it is not valid."""
    try:
        return QuotaDimension(str(value))
    except ValueError:
        return None


def _parse_user_target(value: object) -> tuple[uuid.UUID | None, bool]:
    """Parse a canonical user UUID or the explicit tenant-default target."""
    if value == "default":
        return None, True
    try:
        return uuid.UUID(str(value)), True
    except (ValueError, TypeError, AttributeError):
        return None, False


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the quota self + admin routes against the container.

    Raises:
        RuntimeError: When called without a wired quota service —
            registration is a composition decision, not a runtime
            fallback (mirrors the knowledge/files routers).
    """
    quota_service = container.quota_service
    if quota_service is None:
        raise RuntimeError(
            "build_router(quota) requires a wired quota service; register "
            "the router only when quotas are enabled."
        )
    router = APIRouter()
    principal_dep = container.principal_dependency
    provider = container.auth_provider
    workspace_admin = container.workspace_admin
    users = getattr(container.auth_provider, "users", None)

    async def _admin(request: Request):
        """Resolve the caller as an instance admin (the quota-admin axis).

        Quota administration is tenant-wide platform administration, so it
        is gated on ``instance_role == "admin"`` (the users-mirror single
        source of truth) via the shared :func:`require_instance_admin`
        guard — never on workspace ownership. Returns ``(principal, None)``
        for an admin or ``(None, error_response)`` otherwise.
        """
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return None, error
        principal, _session, _mirror = resolved
        return principal, None

    @router.get("/v1/quota/usage")
    async def my_usage(request: Request):
        """The caller's own usage per dimension (the composer meter).

        Empty for an unscoped principal (no canonical user UUID) — the UI
        only shows the meter for authenticated users anyway.
        """
        principal = await principal_dep(request)
        quota_account = quota_service.subject_for(principal)
        if quota_account is None:
            return {"object": "list", "data": []}
        rows = await quota_service.usage_for(quota_account)
        return {
            "object": "list",
            "data": [
                {
                    "dimension": row.dimension.value,
                    "used": row.used,
                    "limit": row.limit,
                    "remaining": row.remaining,
                    "period_start": row.period_start,
                    "reset_at": period_end(row.period_start),
                }
                for row in rows
            ],
        }

    @router.get("/v1/admin/quota")
    async def admin_overview(request: Request):
        """Admin overview: metered users, usage, limits, and ceilings."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        snapshot = await quota_service.admin_snapshot(principal.tenant_id)
        if users is not None and snapshot["subjects"]:
            profiles = await users.profiles_for_user_ids(
                tenant_id=principal.tenant_id,
                user_ids=tuple(row["user_id"] for row in snapshot["subjects"]),
            )
            for row in snapshot["subjects"]:
                profile = profiles.get(row["user_id"])
                row["display_name"] = (
                    profile.display_name if profile is not None else None
                )
                row["email"] = profile.email if profile is not None else None
        for row in snapshot["subjects"]:
            row["user_id"] = str(row["user_id"])
        return snapshot

    @router.put("/v1/admin/quota/limits")
    async def set_limit(request: Request):
        """Set the tenant default or a per-user override (instance admin only).

        Body: ``{"user_id": "<uuid>"|"default", "dimension": "<dim>",
        "value": <int>=0>}``. ``value`` ``0`` is explicit unlimited; the
        operator ceiling still clamps at read.
        """
        principal, error = await _admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        subject_user_id, valid_target = _parse_user_target(body.get("user_id"))
        if not valid_target:
            return error_response(
                400,
                "Field 'user_id' must be a UUID or 'default'",
                "invalid_request_error",
            )
        dimension = _parse_dimension(body.get("dimension"))
        if dimension is None:
            return error_response(
                400,
                "Field 'dimension' must be one of "
                + ", ".join(d.value for d in QuotaDimension)
                + ".",
                "invalid_request_error",
            )
        raw_value = body.get("value")
        if not isinstance(raw_value, int) or isinstance(raw_value, bool) or raw_value < 0:
            return error_response(
                400,
                "Field 'value' must be a non-negative integer (0 = unlimited)",
                "invalid_request_error",
            )
        await quota_service.set_limit_for(
            tenant_id=principal.tenant_id,
            subject_user_id=subject_user_id,
            dimension=dimension,
            value=raw_value,
            set_by_user_id=principal.user_id,
        )
        await _audit(
            principal,
            "quota.override",
            "default" if subject_user_id is None else str(subject_user_id),
            dimension,
            {"value": str(raw_value)},
        )
        return {
            "user_id": "default" if subject_user_id is None else str(subject_user_id),
            "dimension": dimension.value,
            "value": raw_value,
        }

    @router.delete("/v1/admin/quota/limits", status_code=204)
    async def clear_limit(
        request: Request,
        user_id: str = "",
        dimension: str = "",
    ):
        """Drop a limit so it falls back to the next layer (instance admin only).

        ``user_id`` and ``dimension`` are query parameters. The former is
        either a canonical user UUID or the explicit ``default`` target,
        matching the body-carried target used by PUT and reset.
        """
        principal, error = await _admin(request)
        if error is not None:
            return error
        subject_user_id, valid_target = _parse_user_target(user_id)
        if not valid_target:
            return error_response(
                400,
                "Query parameter 'user_id' must be a UUID or 'default'",
                "invalid_request_error",
            )
        parsed = _parse_dimension(dimension)
        if parsed is None:
            return error_response(
                400, "Invalid dimension", "invalid_request_error"
            )
        await quota_service.clear_limit_for(
            tenant_id=principal.tenant_id,
            subject_user_id=subject_user_id,
            dimension=parsed,
        )
        target = "default" if subject_user_id is None else str(subject_user_id)
        await _audit(principal, "quota.override_cleared", target, parsed, {})

    @router.post("/v1/admin/quota/reset")
    async def reset_usage(request: Request):
        """Zero one user's current-window flow usage (instance admin only).

        Stock dimensions cannot be reset (freed by deletion, never
        reset) — the attempt is a 400, not a silent no-op.
        """
        principal, error = await _admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        if not isinstance(body, dict):
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        subject_user_id, valid_target = _parse_user_target(body.get("user_id"))
        if not valid_target or subject_user_id is None:
            return error_response(
                400, "Field 'user_id' must be a user UUID", "invalid_request_error"
            )
        dimension = _parse_dimension(body.get("dimension"))
        if dimension is None:
            return error_response(
                400, "Invalid dimension", "invalid_request_error"
            )
        try:
            await quota_service.reset_for(
                tenant_id=principal.tenant_id,
                subject_user_id=subject_user_id,
                dimension=dimension,
            )
        except ValueError as exc:
            # Stock dimension reset rejected by the store contract.
            return error_response(400, str(exc), "invalid_request_error")
        await _audit(principal, "quota.reset", str(subject_user_id), dimension, {})
        return {"user_id": str(subject_user_id), "dimension": dimension.value}

    async def _audit(
        principal,
        action: str,
        target_id: str,
        dimension: QuotaDimension,
        detail: dict[str, str],
    ) -> None:
        if workspace_admin is None:
            return
        await workspace_admin.record(
            AuditEntry(
                tenant_id=principal.tenant_id,
                actor_user_id=principal.user_id,
                action=action,
                resource_type="quota",
                resource_id=f"{target_id}:{dimension.value}",
                detail=detail,
            )
        )

    return router
