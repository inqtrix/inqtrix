"""Instance administration routes (``/v1/admin/*``).

User management for the cookie-session modes (local/oidc): list
users, change the instance role, enable/disable, and (local mode) create
accounts. Authorization is SESSION-ONLY and admin-gated — a PAT can never
administer users (a leaked token must not be able to promote itself).
Denial is hidden behind a 404 (the permission layer's not-403 convention).

Two invariants keep the deployment from locking itself out:
* the LAST active admin can be neither demoted nor disabled (409);
* an admin cannot disable themselves.

Disable is a complete cut-off cascade (no half-disabled state): mirror
flag + session purge + PAT revoke, plus the local credential for local
accounts so the password login is refused too.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.lifecycle import AdminAuthorizationError, UserLifecycleStatus
from inqtrix.runs.deletion_operations import (
    DeletionOperationConflict,
    DeletionOperationStatus,
    new_deletion_operation_id,
)
from inqtrix.server.routers._admin_guard import require_instance_admin
from inqtrix.services.request_parsing import error_response

if TYPE_CHECKING:
    from inqtrix.auth.directory import MirroredUser

TENANT = "default"
_MIN_PASSWORD_LEN = 12
log = logging.getLogger("inqtrix")


def _user_payload(user: "MirroredUser") -> dict:
    return {
        "id": str(user.user_id),
        "email": user.email,
        "display_name": user.display_name,
        "instance_role": user.instance_role,
        "disabled": user.disabled_at is not None,
        "last_login_at": user.last_login_at,
        # An account being erased is no longer disabled in the authorization
        # sense (the deletion machine acts as its owner) but can never sign
        # in again; the listing names that state instead.
        "erasure_operation_id": user.erasure_operation_id,
    }


def _revoked_admin_response(
    *, actor_user_id: uuid.UUID | None, command: str
):
    """Hide a lifecycle-time admin revocation while keeping it observable."""
    log_authorization_denial(
        log,
        action=command,
        principal_kind="oidc_session",
        actor_user_id=actor_user_id,
        tenant_id=TENANT,
        resource_type="admin_surface",
        resource_id=TENANT,
    )
    return error_response(404, "Not found", "not_found")


def build_admin_router(
    provider: Any,
    principal_dependency: Callable[..., Any] | None = None,
    *,
    deletions: Any | None = None,
) -> APIRouter:
    """Bind the admin routes against one cookie-session provider.

    ``deletions`` is the deletion service; an account erasure is one of its
    operations. Without it the erasure answers 503.
    """
    router = APIRouter()
    users = provider.users
    lifecycle = provider.lifecycle

    async def _require_admin(request: Request):
        """Resolve a session principal and require instance admin.

        Delegates to the shared :func:`require_instance_admin` guard so the
        instance-admin check is defined exactly once and
        every administrative surface (users, quota, workspaces) authorizes
        byte-identically. The local name keeps the call sites below
        unchanged.
        """
        return await require_instance_admin(
            provider,
            request,
            principal_dependency,
        )

    async def _find_target(user_id: uuid.UUID) -> "MirroredUser | None":
        return await users.find_by_user_id(tenant_id=TENANT, user_id=user_id)

    @router.get("/v1/admin/users")
    async def list_users(request: Request):
        resolved, error = await _require_admin(request)
        if error is not None:
            return error
        rows = await users.list_users(tenant_id=TENANT)
        return {"users": [_user_payload(user) for user in rows]}

    @router.patch("/v1/admin/users/{user_id}")
    async def set_role(user_id: uuid.UUID, request: Request):
        resolved, error = await _require_admin(request)
        if error is not None:
            return error
        principal, session, _mirror = resolved
        try:
            body = await request.json()
        except Exception:
            return error_response(
                400, "Invalid JSON body", "invalid_request_error"
            )
        role = str((body or {}).get("instance_role", "")).strip()
        if role not in {"admin", "user"}:
            return error_response(
                400,
                "Field 'instance_role' must be 'admin' or 'user'",
                "invalid_request_error",
            )
        target = await _find_target(user_id)
        if target is None:
            return error_response(404, "User not found", "not_found")
        if role == "user" and target.user_id == session.user_id:
            # Self-demotion is blocked (mirrors the self-disable guard and the
            # UI): an admin must not silently drop their own access — another
            # admin demotes them. The last-admin guard below is the separate,
            # always-on invariant.
            return error_response(
                409, "You cannot demote yourself", "self_demote"
            )
        if lifecycle is None:
            return error_response(
                503, "User management is unavailable", "server_error"
            )
        try:
            outcome = await lifecycle.set_role(
                tenant_id=TENANT,
                user_id=target.user_id,
                role=role,
                actor_user_id=principal.user_id,
            )
        except AdminAuthorizationError:
            return _revoked_admin_response(
                actor_user_id=principal.user_id, command="set_role"
            )
        if outcome is UserLifecycleStatus.LAST_ADMIN:
            return error_response(
                409,
                "The last admin cannot be demoted",
                "last_admin",
            )
        if outcome is UserLifecycleStatus.ERASING:
            return error_response(
                409, "This account is being erased", "erasure_in_progress"
            )
        if outcome is UserLifecycleStatus.NOT_FOUND:
            return error_response(404, "User not found", "not_found")
        updated = await _find_target(user_id)
        return _user_payload(updated)

    async def _set_user_disabled(
        user_id: uuid.UUID, *, disabled: bool, request: Request
    ):
        resolved, error = await _require_admin(request)
        if error is not None:
            return error
        principal, session, _mirror = resolved
        target = await _find_target(user_id)
        if target is None:
            return error_response(404, "User not found", "not_found")
        if disabled and target.user_id == session.user_id:
            return error_response(
                409, "You cannot deactivate yourself", "self_disable"
            )
        now = time.time()
        disabled_at = now if disabled else None
        if lifecycle is None:
            return error_response(
                503, "User management is unavailable", "server_error"
            )
        try:
            outcome = await lifecycle.set_disabled(
                tenant_id=TENANT,
                user_id=target.user_id,
                disabled_at=disabled_at,
                actor_user_id=principal.user_id,
            )
        except AdminAuthorizationError:
            return _revoked_admin_response(
                actor_user_id=principal.user_id, command="set_disabled"
            )
        if outcome is UserLifecycleStatus.LAST_ADMIN:
            return error_response(
                409,
                "The last admin cannot be deactivated",
                "last_admin",
            )
        if outcome is UserLifecycleStatus.ERASING:
            return error_response(
                409, "This account is being erased", "erasure_in_progress"
            )
        if outcome is UserLifecycleStatus.NOT_FOUND:
            return error_response(404, "User not found", "not_found")
        updated = await _find_target(user_id)
        return _user_payload(updated)

    @router.get("/v1/admin/users/{user_id}/erasure-preview")
    async def preview_user_erasure(user_id: uuid.UUID, request: Request):
        """Count what erasing this account would reach, before it happens.

        An irreversible action has to be legible at the moment it is
        taken, so the confirmation shows the same plan the erasure
        executes -- including ``foreign_rows``, the replies colleagues
        wrote inside documents that would go with the account. That
        number is the only loss an administrator inflicts on somebody who
        was never asked.
        """
        resolved, error = await _require_admin(request)
        if error is not None:
            return error
        target = await _find_target(user_id)
        if target is None:
            return error_response(404, "User not found", "not_found")
        if lifecycle is None or not hasattr(lifecycle, "inventory"):
            return error_response(
                503, "User management is unavailable", "server_error"
            )
        preview = await lifecycle.inventory(tenant_id=TENANT, user_id=user_id)
        if not preview:
            # A backend without the capability answers empty. Returning it
            # would render as a measured zero and invite an erasure on a
            # scope nobody counted; unavailable is the truthful answer.
            return error_response(
                503, "The erasure scope cannot be counted", "server_error"
            )
        return preview

    @router.delete("/v1/admin/users/{user_id}")
    async def erase_user_account(user_id: uuid.UUID, request: Request):
        """Start erasing one account; the erasure is a deletion operation.

        The account is cut off at once -- every session, token and sign-in
        refused -- and the deletion worker removes its data in short steps
        without blocking anybody else. The answer is 202 with the operation
        the administrator follows to its end. Refusals are 409 and carry the
        same reasons the erasure preview shows as blockers: both ask one
        function.
        """
        resolved, error = await _require_admin(request)
        if error is not None:
            return error
        principal, session, _mirror = resolved
        target = await _find_target(user_id)
        if target is None:
            return error_response(404, "User not found", "not_found")
        if target.user_id == session.user_id:
            return error_response(
                409, "You cannot erase yourself", "self_erase"
            )
        if (
            lifecycle is None
            or not getattr(lifecycle, "erases_accounts", False)
            or deletions is None
        ):
            return error_response(
                503, "User management is unavailable", "server_error"
            )
        try:
            outcome, report = await lifecycle.begin_erasure(
                tenant_id=TENANT,
                user_id=target.user_id,
                actor_user_id=principal.user_id,
                operation_id=new_deletion_operation_id(),
            )
        except AdminAuthorizationError:
            return _revoked_admin_response(
                actor_user_id=principal.user_id, command="erase"
            )
        if outcome is UserLifecycleStatus.NOT_FOUND:
            return error_response(404, "User not found", "not_found")
        if outcome is UserLifecycleStatus.LAST_ADMIN:
            return error_response(
                409, "The last admin cannot be erased", "last_admin"
            )
        if outcome is UserLifecycleStatus.REFUSED:
            refused = list(report.get("refused") or ())
            return error_response(
                409,
                "This account cannot be erased yet: " + " ".join(refused),
                "erasure_refused",
                reasons=refused,
            )
        operation_id = str(report["operation_id"])
        try:
            summary = await deletions.start_user_erasure(
                target.user_id,
                principal=principal,
                operation_id=operation_id,
            )
            if summary.get("status") == DeletionOperationStatus.DELETE_FAILED.value:
                # Deleting again resumes the failed erasure where it stopped.
                summary = await deletions.retry(
                    operation_id, principal=principal, workspace_id=None
                )
        except DeletionOperationConflict:
            # The operation may be running or failed; only the
            # administrator who started it can follow or retry it.
            return error_response(
                409,
                "Another administrator started erasing this account",
                "deletion_in_progress",
            )
        log.info("account erasure started: operation=%s", operation_id)
        return JSONResponse(status_code=202, content=summary)

    @router.post("/v1/admin/users/{user_id}:disable")
    async def disable_user(user_id: uuid.UUID, request: Request):
        return await _set_user_disabled(user_id, disabled=True, request=request)

    @router.post("/v1/admin/users/{user_id}:enable")
    async def enable_user(user_id: uuid.UUID, request: Request):
        return await _set_user_disabled(user_id, disabled=False, request=request)

    if provider.mode == "local":
        from inqtrix.auth.credentials import (
            LocalCredential,
            hash_password,
            new_subject,
        )

        @router.post("/v1/admin/users", status_code=201)
        async def create_local_user(request: Request):
            resolved, error = await _require_admin(request)
            if error is not None:
                return error
            principal, _session, _mirror = resolved
            try:
                body = await request.json()
            except Exception:
                return error_response(
                    400, "Invalid JSON body", "invalid_request_error"
                )
            email = str((body or {}).get("email", "")).strip()
            password = str((body or {}).get("password", ""))
            role = str((body or {}).get("instance_role", "user")).strip()
            if "@" not in email or len(email) > 320:
                return error_response(
                    400, "Invalid email address", "invalid_request_error"
                )
            if len(password) < _MIN_PASSWORD_LEN:
                return error_response(
                    400,
                    f'Password must be at least {_MIN_PASSWORD_LEN} characters long',
                    "invalid_request_error",
                )
            if role not in {"admin", "user"}:
                return error_response(
                    400,
                    "Field 'instance_role' must be 'admin' or 'user'",
                    "invalid_request_error",
                )
            display_name = str((body or {}).get("display_name", "")).strip() or email
            credential = LocalCredential(
                user_id=uuid.uuid4(),
                subject=new_subject(),
                email=email,
                password_hash=hash_password(password),
                display_name=display_name,
                created_at=time.time(),
            )
            if lifecycle is None:
                return error_response(
                    503, "User management is unavailable", "server_error"
                )
            try:
                user = await lifecycle.create_local_account(
                    tenant_id=TENANT,
                    credential=credential,
                    role=role,
                    first_only=False,
                    actor_user_id=principal.user_id,
                )
            except AdminAuthorizationError:
                return _revoked_admin_response(
                    actor_user_id=principal.user_id,
                    command="create_local_account",
                )
            if user is None:
                return error_response(
                    409, "Email address is already in use", "duplicate_email"
                )
            return _user_payload(user)

        @router.post("/v1/admin/users/{user_id}:reset-password")
        async def reset_password(user_id: uuid.UUID, request: Request):
            """Admin sets a new password for a local account.

            Forgotten-password recovery without email: the admin supplies the
            new password (the UI shows it once, like create). Live sessions are
            purged so the old password — and anything minted with it — stops
            working; PATs are intentionally left intact (a reset is not the
            full disable cut-off). Does not change the disabled state.
            """
            resolved, error = await _require_admin(request)
            if error is not None:
                return error
            principal, _session, _mirror = resolved
            try:
                body = await request.json()
            except Exception:
                return error_response(
                    400, "Invalid JSON body", "invalid_request_error"
                )
            password = str((body or {}).get("password", ""))
            if len(password) < _MIN_PASSWORD_LEN:
                return error_response(
                    400,
                    f'Password must be at least {_MIN_PASSWORD_LEN} characters long',
                    "invalid_request_error",
                )
            if lifecycle is None:
                return error_response(
                    503, "User management is unavailable", "server_error"
                )
            try:
                changed = await lifecycle.reset_local_password(
                    tenant_id=TENANT,
                    user_id=user_id,
                    password_hash=hash_password(password),
                    actor_user_id=principal.user_id,
                )
            except AdminAuthorizationError:
                return _revoked_admin_response(
                    actor_user_id=principal.user_id,
                    command="reset_local_password",
                )
            if not changed:
                return error_response(
                    404, "No local account found", "not_found"
                )
            return {"reset": True}

    return router
