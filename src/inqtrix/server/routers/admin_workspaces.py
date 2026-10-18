"""Admin workspace + membership management (``/v1/admin/workspaces*``).

The instance-admin surface for the platform's collaboration spaces: an
instance admin creates workspaces, assigns/positions users into them, and
manages member roles. This is the org-administration axis — gated on ``instance_role == "admin"`` via the
shared :func:`require_instance_admin` guard, never on workspace ownership.
``WorkspaceRole`` (viewer..owner) stays the in-workspace collaboration role.

Mounted only for a cookie-session provider that carries a user mirror AND a
workspace-admin (membership) store; in every other configuration the routes
are plain 404s. Denials hide behind 404 (the permission layer's not-403
convention); repositories commit every mutation together with its audit and
user invalidations.

One UX invariant: a workspace must not be left without an OWNER by demoting
or removing its last one (409 ``last_owner``). The repository locks the
workspace before checking and mutating membership, so concurrent admin calls
cannot violate the invariant.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from inqtrix.auth.permissions import LastWorkspaceOwnerError, WorkspaceRole
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response
from inqtrix.services.workspace_administration import (
    WorkspaceNameError,
    create_workspace_for_admin,
    normalize_workspace_name,
)

if TYPE_CHECKING:
    from inqtrix.server.container import AppContainer

def _parse_role(value: object) -> WorkspaceRole | None:
    """Parse a workspace-role string, or ``None`` when it is not valid."""
    try:
        return WorkspaceRole(str(value))
    except ValueError:
        return None


def build_router(container: "AppContainer") -> APIRouter:
    """Bind the admin workspace/membership routes against the container.

    Raises:
        RuntimeError: When called without a wired workspace-admin store —
            registration is a composition decision, not a runtime fallback
            (mirrors the knowledge/files/quota routers).
    """
    workspace_admin = container.workspace_admin
    if workspace_admin is None:
        raise RuntimeError(
            "build_router(admin_workspaces) requires a wired workspace_admin; "
            "register the router only for a membership-backed cookie-session mode."
        )
    router = APIRouter()
    provider = container.auth_provider
    principal_dep = container.principal_dependency
    users = getattr(provider, "users", None)

    async def _admin(request: Request):
        """Resolve the caller as an instance admin, or yield the error."""
        resolved, error = await require_instance_admin(
            provider, request, principal_dep
        )
        if error is not None:
            return None, error
        principal, _session, _mirror = resolved
        return principal, None

    async def _members_or_404(tenant_id: str, workspace_id: str):
        """``list_members`` with the absent-workspace case mapped to a 404."""
        members = await workspace_admin.list_members(
            tenant_id=tenant_id, workspace_id=workspace_id
        )
        if members is None:
            return None, error_response(
                404, "Workspace not found", "not_found"
            )
        return members, None

    # ----------------------------------------------------------------- #
    # Workspaces
    # ----------------------------------------------------------------- #

    @router.get("/v1/admin/workspaces")
    async def list_workspaces(request: Request):
        """Every workspace in the tenant with its member count."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        rows = await workspace_admin.list_all_workspaces(
            tenant_id=principal.tenant_id
        )
        return {
            "object": "list",
            "data": [
                {
                    "workspace_id": workspace_id,
                    "name": name,
                    "created_by_user_id": str(created_by_user_id),
                    "member_count": member_count,
                }
                for workspace_id, name, created_by_user_id, member_count in rows
            ],
        }

    @router.post("/v1/admin/workspaces", status_code=201)
    async def create_workspace(request: Request):
        """Create one workspace; the creating admin becomes its OWNER."""
        principal, error = await _admin(request)
        if error is not None:
            return error
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
        return {"workspace_id": workspace_id, "name": created_name}

    @router.patch("/v1/admin/workspaces/{workspace_id}")
    async def rename_workspace(workspace_id: str, request: Request):
        """Rename one workspace."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        try:
            name = normalize_workspace_name((body or {}).get("name", ""))
        except WorkspaceNameError as exc:
            return error_response(400, str(exc), "invalid_request_error")
        renamed = await workspace_admin.rename_workspace(
            tenant_id=principal.tenant_id,
            workspace_id=workspace_id,
            name=name,
            actor_user_id=principal.user_id,
        )
        if not renamed:
            return error_response(
                404, "Workspace not found", "not_found"
            )
        return {"workspace_id": workspace_id, "name": name}

    @router.delete("/v1/admin/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(workspace_id: str, request: Request):
        """Delete one workspace and cascade its memberships."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        deleted = await workspace_admin.delete_workspace(
            tenant_id=principal.tenant_id,
            workspace_id=workspace_id,
            actor_user_id=principal.user_id,
        )
        if not deleted:
            return error_response(
                404, "Workspace not found", "not_found"
            )

    # ----------------------------------------------------------------- #
    # Members
    # ----------------------------------------------------------------- #

    @router.get("/v1/admin/workspaces/{workspace_id}/members")
    async def list_members(workspace_id: str, request: Request):
        """Members of one workspace, enriched with display name + email."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        members, error = await _members_or_404(principal.tenant_id, workspace_id)
        if error is not None:
            return error
        profiles = {}
        if users is not None and members:
            profiles = await users.profiles_for_user_ids(
                tenant_id=principal.tenant_id,
                user_ids=tuple(user_id for user_id, _role in members),
            )
        data = []
        for user_id, role in members:
            profile = profiles.get(user_id)
            data.append(
                {
                    "user_id": str(user_id),
                    "role": role.value,
                    "display_name": (
                        profile.display_name if profile is not None else None
                    ),
                    "email": profile.email if profile is not None else None,
                }
            )
        return {"object": "list", "data": data}

    @router.post(
        "/v1/admin/workspaces/{workspace_id}/members", status_code=201
    )
    async def add_member(workspace_id: str, request: Request):
        """Assign a user to the workspace at a role (or update their role)."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        raw_user_id = str((body or {}).get("user_id", "")).strip()
        try:
            user_id = uuid.UUID(raw_user_id)
        except ValueError:
            return error_response(
                400, "Field 'user_id' must be a UUID", "invalid_request_error"
            )
        role = _parse_role((body or {}).get("role"))
        if role is None:
            valid = ", ".join(member.value for member in WorkspaceRole)
            return error_response(
                400,
                f"Field 'role' must be one of {valid}",
                "invalid_request_error",
            )
        # The target must be a known user (no phantom memberships).
        if users is not None and not await users.has_user_id(
            tenant_id=principal.tenant_id, user_id=user_id
        ):
            return error_response(404, "User not found", "not_found")
        try:
            assigned = await workspace_admin.assign_member(
                tenant_id=principal.tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                role=role,
                actor_user_id=principal.user_id,
            )
        except LastWorkspaceOwnerError:
            return error_response(
                409,
                "The last OWNER cannot be demoted",
                "last_owner",
            )
        if not assigned:
            return error_response(404, "Workspace not found", "not_found")
        return {"user_id": str(user_id), "role": role.value}

    @router.patch("/v1/admin/workspaces/{workspace_id}/members/{user_id}")
    async def set_member_role(
        workspace_id: str, user_id: uuid.UUID, request: Request
    ):
        """Change an existing member's role."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        role = _parse_role((body or {}).get("role"))
        if role is None:
            valid = ", ".join(member.value for member in WorkspaceRole)
            return error_response(
                400,
                f"Field 'role' must be one of {valid}",
                "invalid_request_error",
            )
        try:
            updated = await workspace_admin.set_existing_member_role(
                tenant_id=principal.tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                role=role,
                actor_user_id=principal.user_id,
            )
        except LastWorkspaceOwnerError:
            return error_response(
                409,
                "The last OWNER cannot be demoted",
                "last_owner",
            )
        if not updated:
            return error_response(404, "Member not found", "not_found")
        return {"user_id": str(user_id), "role": role.value}

    @router.delete(
        "/v1/admin/workspaces/{workspace_id}/members/{user_id}", status_code=204
    )
    async def remove_member(
        workspace_id: str, user_id: uuid.UUID, request: Request
    ):
        """Remove a member from the workspace."""
        principal, error = await _admin(request)
        if error is not None:
            return error
        members, error = await _members_or_404(principal.tenant_id, workspace_id)
        if error is not None:
            return error
        if user_id not in {member_user_id for member_user_id, _role in members}:
            return error_response(
                404, "Member not found", "not_found"
            )
        try:
            removed = await workspace_admin.remove_member(
                tenant_id=principal.tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                actor_user_id=principal.user_id,
            )
        except LastWorkspaceOwnerError:
            return error_response(
                409,
                "The last OWNER cannot be removed",
                "last_owner",
            )
        if not removed:
            return error_response(404, "Member not found", "not_found")
    return router
