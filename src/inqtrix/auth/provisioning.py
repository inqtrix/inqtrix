"""Login-time provisioning shared by every cookie-session transport.

The single home for "turn a verified login into local instance facts" so
the OIDC (and future) callbacks cannot drift: one definition, no
redundant copy. It is deliberately I/O-only over the
:class:`~inqtrix.auth.directory.UserDirectory`; the claim-to-signal logic
lives in the pure :mod:`inqtrix.auth.mapping` layer, which this module
does not import.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inqtrix.auth.directory import UserDirectory


async def apply_admin_grant(
    users: "UserDirectory",
    *,
    tenant_id: str,
    user_id: uuid.UUID,
    is_admin: bool,
    first_login_owner: bool,
) -> None:
    """Grant-only instance-admin provisioning from a verified login.

    The one path the OIDC admin-claim mapping and any future group
    match flow through, so their semantics stay identical.

    Args:
        users: The local user mirror to mutate.
        tenant_id: Tenant anchor (currently always ``"default"``).
        user_id: Canonical local user UUID returned by login provisioning.
        is_admin: Whether the directory/IdP signalled admin for this user.
        first_login_owner: When no admin signal is present, whether the
            first ever login bootstraps the instance owner.

    Behaviour:
        Promotes to admin on a positive *is_admin* signal; otherwise, when
        *first_login_owner* is set, the first authenticated user on a fresh
        instance becomes the owner (a no-op once any admin exists). It
        NEVER demotes: role revocation is the admin surface's responsibility
        (it owns the last-admin guard), so a directory group or IdP claim
        only ever promotes — auto-demoting here would silently undo an
        admin-UI promotion and sidestep that guard.
    """
    if is_admin:
        await users.set_instance_role(
            tenant_id=tenant_id, user_id=user_id, role="admin"
        )
    elif first_login_owner:
        await users.promote_if_no_admin(
            tenant_id=tenant_id, user_id=user_id
        )
