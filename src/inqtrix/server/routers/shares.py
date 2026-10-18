"""HTTP lifecycle for direct user-to-resource shares."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.shares import (
    ShareBackendUnsupported,
    ShareConflict,
    ShareNotAllowed,
    ShareValidationError,
)
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

_SCOPED_KINDS = frozenset({"oidc_session", "pat"})


def build_router(container: "AppContainer") -> APIRouter:
    """Bind canonical v0.2 share routes."""
    router = APIRouter()
    principal_dep = container.principal_dependency
    service = container.share_service
    users = getattr(container.auth_provider, "users", None)

    def _unsupported(exc: ShareBackendUnsupported) -> Any:
        return error_response(501, str(exc), "unsupported")

    async def _scoped_principal(request: Request):
        principal = await principal_dep(request)
        if principal.kind not in _SCOPED_KINDS or principal.user_id is None:
            return None, error_response(404, "Not found", "not_found")
        return principal, None

    async def _share_payload(record, tenant_id: str) -> dict[str, Any]:
        profiles = {}
        if users is not None:
            profiles = await users.profiles_for_user_ids(
                tenant_id=tenant_id, user_ids=(record.recipient_user_id,)
            )
        profile = profiles.get(record.recipient_user_id)
        return {
            "id": record.id,
            "recipient_user_id": str(record.recipient_user_id),
            "resource_type": record.resource_type,
            "resource_id": record.resource_id,
            "permission": record.permission.value,
            "revision": record.revision,
            "granted_by_user_id": str(record.granted_by_user_id),
            "created_at": record.created_at,
            "accepted_at": record.accepted_at,
            "display_name": profile.display_name if profile is not None else None,
            "email": profile.email if profile is not None else None,
        }

    async def _share_payloads(records, tenant_id: str) -> list[dict[str, Any]]:
        profiles = {}
        user_ids = tuple({record.recipient_user_id for record in records})
        if users is not None and user_ids:
            profiles = await users.profiles_for_user_ids(
                tenant_id=tenant_id, user_ids=user_ids
            )
        return [
            {
                "id": record.id,
                "recipient_user_id": str(record.recipient_user_id),
                "resource_type": record.resource_type,
                "resource_id": record.resource_id,
                "permission": record.permission.value,
                "revision": record.revision,
                "granted_by_user_id": str(record.granted_by_user_id),
                "created_at": record.created_at,
                "accepted_at": record.accepted_at,
                "display_name": (
                    profiles[record.recipient_user_id].display_name
                    if record.recipient_user_id in profiles
                    else None
                ),
                "email": (
                    profiles[record.recipient_user_id].email
                    if record.recipient_user_id in profiles
                    else None
                ),
            }
            for record in records
        ]

    @router.get("/v1/shares")
    async def list_shares(
        request: Request, resource_type: str = "", resource_id: str = ""
    ):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        if not resource_type or not resource_id:
            return error_response(
                400,
                "Parameters 'resource_type' and 'resource_id' are required",
                "invalid_request_error",
            )
        try:
            records = await service.list_for_resource(
                principal, resource_type=resource_type, resource_id=resource_id
            )
        except ShareValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        except ShareNotAllowed:
            return error_response(404, "Resource not found", "not_found")
        return {
            "object": "list",
            "data": await _share_payloads(records, principal.tenant_id),
        }

    @router.post("/v1/shares", status_code=201)
    async def create_shares(request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict):
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        resource_type = str(body.get("resource_type", "")).strip()
        resource_id = str(body.get("resource_id", "")).strip()
        raw_invitees = body.get("invitees")
        if not resource_type or not resource_id:
            return error_response(
                400,
                "Fields 'resource_type' and 'resource_id' are required",
                "invalid_request_error",
            )
        if not isinstance(raw_invitees, list) or not raw_invitees:
            return error_response(
                400,
                "Field 'invitees' must be a non-empty list",
                "invalid_request_error",
            )
        invitees: list[tuple[uuid.UUID, SharePermission]] = []
        for item in raw_invitees:
            if not isinstance(item, dict):
                return error_response(
                    400,
                    "Each entry in 'invitees' must be an object",
                    "invalid_request_error",
                )
            try:
                recipient_user_id = uuid.UUID(str(item.get("user_id", "")))
            except (ValueError, TypeError, AttributeError):
                return error_response(
                    400, "Field 'user_id' must be a UUID", "invalid_request_error"
                )
            try:
                permission = SharePermission(str(item.get("permission", "")))
            except ValueError:
                return error_response(
                    400,
                    "Field 'permission' must be 'view', 'suggest' or 'edit'",
                    "invalid_request_error",
                )
            invitees.append((recipient_user_id, permission))
        try:
            created = await service.grant(
                principal,
                resource_type=resource_type,
                resource_id=resource_id,
                invitees=invitees,
            )
        except ShareConflict as exc:
            return error_response(409, str(exc), "conflict")
        except ShareValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        except ShareNotAllowed:
            return error_response(404, "Resource not found", "not_found")
        return {
            "object": "list",
            "data": await _share_payloads(created, principal.tenant_id),
        }

    @router.patch("/v1/shares/{share_id}")
    async def update_share(share_id: str, request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(400, "Invalid JSON body", "invalid_request_error")
        if not isinstance(body, dict) or type(body.get("expected_revision")) is not int:
            return error_response(
                400,
                "Fields 'permission' and 'expected_revision' are required",
                "invalid_request_error",
            )
        try:
            permission = SharePermission(str(body.get("permission", "")))
        except ValueError:
            return error_response(
                400,
                "Field 'permission' must be 'view', 'suggest' or 'edit'",
                "invalid_request_error",
            )
        try:
            updated = await service.update_permission(
                principal,
                share_id=share_id,
                permission=permission,
                expected_revision=body["expected_revision"],
            )
        except ShareConflict as exc:
            extra = (
                {"current_revision": exc.current_revision}
                if exc.current_revision is not None
                else {}
            )
            return error_response(409, str(exc), "conflict", **extra)
        except ShareValidationError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        except ShareNotAllowed:
            return error_response(404, "Share not found", "not_found")
        return {
            "object": "share",
            "data": await _share_payload(updated, principal.tenant_id),
        }

    @router.post("/v1/shares/{share_id}/accept")
    async def accept_share(share_id: str, request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            accepted = await service.accept(principal, share_id=share_id)
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        if accepted is None:
            return error_response(404, "Share not found", "not_found")
        return {
            "object": "share",
            "data": await _share_payload(accepted, principal.tenant_id),
        }

    @router.delete("/v1/shares/{share_id}", status_code=204)
    async def remove_share(share_id: str, request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            removed = await service.remove(principal, share_id=share_id)
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        if removed is None:
            return error_response(404, "Share not found", "not_found")
        return Response(status_code=204)

    @router.get("/v1/shares/inbox")
    async def inbox(request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            items = await service.inbox(principal)
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        grantors = {}
        grantor_ids = tuple({item.granted_by_user_id for item in items})
        if users is not None and grantor_ids:
            grantors = await users.profiles_for_user_ids(
                tenant_id=principal.tenant_id, user_ids=grantor_ids
            )

        def payload(item) -> dict[str, Any]:
            profile = grantors.get(item.granted_by_user_id)
            return {
                "id": item.share_id,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "resource_title": item.resource_title,
                "permission": item.permission.value,
                "revision": item.revision,
                "granted_by_user_id": str(item.granted_by_user_id),
                "granted_by_display_name": (
                    profile.display_name if profile is not None else None
                ),
                "created_at": item.created_at,
                "accepted_at": item.accepted_at,
            }

        return {
            "object": "map",
            "data": {
                "pending": [payload(item) for item in items if item.accepted_at is None],
                "accepted": [payload(item) for item in items if item.accepted_at is not None],
            },
        }

    @router.get("/v1/shares/mine")
    async def mine(request: Request):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        try:
            items = await service.mine(principal)
        except ShareBackendUnsupported as exc:
            return _unsupported(exc)
        return {
            "object": "list",
            "data": [
                {
                    "resource_type": item.resource_type,
                    "resource_id": item.resource_id,
                    "resource_title": item.resource_title,
                    "share_count": item.share_count,
                    "pending_count": item.pending_count,
                }
                for item in items
            ],
        }

    return router
