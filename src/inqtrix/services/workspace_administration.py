"""Shared workspace-name validation and admin creation command.

Three routes take a workspace name — self-serve creation
(``POST /v1/workspaces``), admin creation (``POST /v1/admin/workspaces``)
and admin rename (``PATCH /v1/admin/workspaces/{id}``). All of them must
apply the same validation, and both creation surfaces must behave
identically, so the validate/create body lives here exactly once; the
repositories write audit and invalidation effects inside the same mutation
boundary.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from inqtrix.auth.principal import Principal

WORKSPACE_NAME_MAX_LEN = 120
"""Upper bound for workspace names, shared by every name-taking surface."""


class WorkspaceNameError(ValueError):
    """Raised when a workspace name fails validation.

    Carries the user-facing German message so every router renders the
    byte-identical 400 the routes produced before the consolidation.
    """


class WorkspaceAdministrationPort(Protocol):
    """The store surface the creation command needs.

    Both identity backends implement the command atomically, including its
    audit and invalidation effects.
    """

    async def create_workspace(
        self, *, tenant_id: str, name: str, created_by_user_id: uuid.UUID
    ) -> tuple[str, str]:
        """See :class:`inqtrix.auth.permissions.MembershipAdminRepository`."""
        ...


class WorkspaceShareReconciliationPort(Protocol):
    """Startup command needed when sharing is workspace-restricted."""

    async def reconcile_workspace_shares(self, *, tenant_id: str) -> int:
        """Revoke invalid active shares and return the changed-row count."""
        ...


def normalize_workspace_name(value: object) -> str:
    """Validate one untrusted workspace-name value into its stored form.

    Strict on type: only strings are names. The historical routes ran
    non-string JSON values through ``str(...)`` and would store literal
    ``"None"``/``"123"`` names — a repr leak, not a contract — so the
    consolidated rule rejects them uniformly across create and rename.

    Args:
        value: The raw ``name`` value from the request body.

    Returns:
        The stripped name.

    Raises:
        WorkspaceNameError: When the value is not a string, or the
            stripped name is empty or longer than
            :data:`WORKSPACE_NAME_MAX_LEN`.
    """
    clean = value.strip() if isinstance(value, str) else ""
    if not clean or len(clean) > WORKSPACE_NAME_MAX_LEN:
        raise WorkspaceNameError(
            f"Field 'name' must be 1 to {WORKSPACE_NAME_MAX_LEN} characters long"
        )
    return clean


async def create_workspace_for_admin(
    workspace_admin: WorkspaceAdministrationPort,
    *,
    principal: "Principal",
    name: object,
) -> tuple[str, str]:
    """Create one workspace for an already-verified instance admin.

    Args:
        workspace_admin: The atomic membership-admin store; the caller
            guarantees it is wired.
        principal: The verified instance-admin principal. The caller
            is responsible for the ``require_instance_admin`` gate —
            this command only executes the decision.
        name: The raw, untrusted name value from the request body.

    Returns:
        ``(workspace_id, created_name)`` as stored.

    Raises:
        WorkspaceNameError: When the name fails
            :func:`normalize_workspace_name`.
    """
    clean_name = normalize_workspace_name(name)
    if principal.user_id is None:
        raise ValueError("Workspace creation requires a canonical user_id")
    workspace_id, created_name = await workspace_admin.create_workspace(
        tenant_id=principal.tenant_id,
        name=clean_name,
        created_by_user_id=principal.user_id,
    )
    return workspace_id, created_name


async def reconcile_workspace_shares_at_startup(
    workspace_admin: object | None, *, tenant_id: str
) -> int:
    """Run the mandatory restricted-sharing cleanup for either app factory.

    Raises:
        RuntimeError: When the active workspace backend does not implement the
            reconciliation command. Restricted sharing must fail during
            startup rather than silently serving a partially reconciled view.
    """
    reconcile = getattr(workspace_admin, "reconcile_workspace_shares", None)
    if not callable(reconcile):
        raise RuntimeError(
            "workspace-restricted sharing requires startup reconciliation support"
        )
    return int(await reconcile(tenant_id=tenant_id))


async def ensure_workspace_share_reconciliation(
    application: object,
    workspace_admin: object | None,
    *,
    tenant_id: str,
) -> int:
    """Complete the restricted-share startup boundary exactly once.

    The database contract can recover after process startup, for example when
    an orchestrated migration finishes while the API pod remains alive. The
    business-route gate must not open at that point until invalid workspace
    shares have been reconciled. The application-scoped lock serializes
    concurrent readiness probes without moving this mutation into every probe.

    Args:
        application: FastAPI application carrying the initialized gate state.
        workspace_admin: Backend that owns the reconciliation transaction.
        tenant_id: Tenant whose restricted shares must be reconciled.

    Returns:
        Number of invalid shares revoked by the probe that completed the
        boundary, or zero when another probe already completed it.

    Raises:
        RuntimeError: If the database gate did not initialize its state or the
            backend cannot perform the required reconciliation.
        Exception: Store failures propagate so readiness remains closed.
    """
    state = getattr(application, "state", None)
    if state is None:
        raise RuntimeError("application state is unavailable for reconciliation")
    if bool(getattr(state, "workspace_share_reconciliation_ready", False)):
        return 0
    lock = getattr(state, "workspace_share_reconciliation_lock", None)
    if lock is None:
        raise RuntimeError("workspace-share reconciliation lock is unavailable")
    async with lock:
        if bool(getattr(state, "workspace_share_reconciliation_ready", False)):
            return 0
        revoked = await reconcile_workspace_shares_at_startup(
            workspace_admin,
            tenant_id=tenant_id,
        )
        state.workspace_share_reconciliation_ready = True
        return revoked
