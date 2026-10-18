"""Workspace creation and invitation management (cookie-session modes).

Two authorization axes meet here. Creating a workspace is platform
administration — gated on the instance-admin role
(``require_instance_admin``), so the deployment's collaboration spaces are
set up by an admin, never self-served. The creating admin becomes the
workspace OWNER, which is purely the collaboration axis: it carries
invitation and sharing rights *within* that workspace and confers no
tenant-wide power.

Invitation management (create/list/revoke) therefore stays on the
collaboration axis, requiring the workspace OWNER role via
``AuthorizationService.resolve_workspace``; denials hide behind 404 (the
permission layer's not-403 convention; membership is not disclosed).

Mounted only when a cookie-session provider carries an invitation
repository (postgres storage); in every other configuration the routes are
plain 404s.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.auth.invitations import (
    DEFAULT_INVITATION_TTL_DAYS,
    DuplicateOpenInvitation,
)
from inqtrix.auth.permissions import WorkspaceNotFound, WorkspaceRole
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response
from inqtrix.services.workspace_administration import (
    WorkspaceNameError,
    create_workspace_for_admin,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

log = logging.getLogger("inqtrix")

_SCOPED_KINDS = frozenset({"oidc_session", "pat"})


def _invitation_payload(invitation) -> dict:
    return {
        "invitation_id": invitation.id,
        "workspace_id": invitation.workspace_id,
        "email": invitation.email,
        "role": invitation.role.value,
        "invited_by_user_id": str(invitation.invited_by_user_id),
        "created_at": invitation.created_at,
        "expires_at": invitation.expires_at,
        "accepted_at": invitation.accepted_at,
        "revoked_at": invitation.revoked_at,
    }


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the workspace/invitation routes against the container."""
    router = APIRouter()
    principal_dep = container.principal_dependency
    provider = container.auth_provider
    permissions = container.permission_service
    workspace_admin = container.workspace_admin
    invitations = container.auth_provider.invitations

    async def _scoped_principal(request: Request):
        principal = await principal_dep(request)
        if principal.kind not in _SCOPED_KINDS:
            # Unscoped legacy principals have no identity to anchor
            # ownership on — the surface does not exist for them.
            return None, error_response(
                404, "Not found", "not_found"
            )
        return principal, None

    @router.post("/v1/workspaces", status_code=201)
    async def create_workspace(request: Request):
        """Create one workspace; the creating instance admin becomes its OWNER.

        Workspace creation is platform administration, so it is gated on the
        instance-admin axis (``require_instance_admin``) — not self-serve.
        The resulting OWNER role is purely collaborative (invitations and
        sharing within the workspace) and carries no tenant-wide power, so
        creating a workspace is not a privilege-escalation vector.
        """
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return error
        principal, _session, _mirror = resolved
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            workspace_id, created_name = await create_workspace_for_admin(
                workspace_admin,
                principal=principal,
                name=(body or {}).get("name", ""),
            )
        except WorkspaceNameError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        return {
            "workspace_id": workspace_id,
            "name": created_name,
            "role": WorkspaceRole.OWNER.value,
        }

    @router.get("/v1/workspaces")
    async def list_workspaces(request: Request):
        """The caller's memberships (id, name, role)."""
        principal, error = await _scoped_principal(request)
        if error is not None:
            return error
        rows = await workspace_admin.list_workspaces_for(
            tenant_id=principal.tenant_id, user_id=principal.user_id
        )
        return {
            "object": "list",
            "data": [
                {
                    "workspace_id": workspace_id,
                    "name": name,
                    "role": role.value,
                }
                for workspace_id, name, role in rows
            ],
        }

    async def _owner_workspace(request: Request, workspace_id: str):
        principal, error = await _scoped_principal(request)
        if error is not None:
            return None, error
        try:
            await permissions.resolve_workspace(
                principal, workspace_id, min_role=WorkspaceRole.OWNER
            )
        except WorkspaceNotFound:
            return None, error_response(
                404, "Workspace not found", "not_found"
            )
        return principal, None

    @router.post(
        "/v1/workspaces/{workspace_id}/invitations", status_code=201
    )
    async def create_invitation(workspace_id: str, request: Request):
        """Invite one email address (OWNER only)."""
        principal, error = await _owner_workspace(request, workspace_id)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        email = str((body or {}).get("email", "")).strip()
        if "@" not in email or len(email) > 320:
            return error_response(
                400,
                "Field 'email' must be an email address",
                "invalid_request_error",
            )
        raw_role = str((body or {}).get("role", "")).strip()
        try:
            role = WorkspaceRole(raw_role)
        except ValueError:
            valid = ", ".join(member.value for member in WorkspaceRole)
            return error_response(
                400,
                f"Field 'role' must be one of {valid}",
                "invalid_request_error",
            )
        expires_in_days = (body or {}).get("expires_in_days")
        if expires_in_days is None:
            expires_in_days = DEFAULT_INVITATION_TTL_DAYS
        if not isinstance(expires_in_days, int) or expires_in_days < 1:
            return error_response(
                400,
                "Field 'expires_in_days' must be a positive integer",
                "invalid_request_error",
            )
        try:
            invitation = await invitations.create(
                tenant_id=principal.tenant_id,
                workspace_id=workspace_id,
                email=email,
                role=role,
                invited_by_user_id=principal.user_id,
                expires_at=time.time() + expires_in_days * 86_400.0,
            )
        except DuplicateOpenInvitation:
            return error_response(
                409,
                "An open invitation already exists for this email address.",
                "invitation_conflict",
            )
        return _invitation_payload(invitation)

    @router.get("/v1/workspaces/{workspace_id}/invitations")
    async def list_invitations(workspace_id: str, request: Request):
        """Every invitation of the workspace (OWNER only)."""
        _principal, error = await _owner_workspace(request, workspace_id)
        if error is not None:
            return error
        rows = await invitations.list_for_workspace(
            tenant_id="default", workspace_id=workspace_id
        )
        return {
            "object": "list",
            "data": [_invitation_payload(invitation) for invitation in rows],
        }

    @router.delete(
        "/v1/workspaces/{workspace_id}/invitations/{invitation_id}"
    )
    async def revoke_invitation(
        workspace_id: str, invitation_id: str, request: Request
    ):
        """Revoke one OPEN invitation (OWNER only)."""
        _principal, error = await _owner_workspace(request, workspace_id)
        if error is not None:
            return error
        revoked = await invitations.revoke(
            tenant_id="default",
            workspace_id=workspace_id,
            invitation_id=invitation_id,
            now=time.time(),
        )
        if not revoked:
            return error_response(
                404, "Invitation not found", "not_found"
            )
        return {"revoked": True}

    return router
