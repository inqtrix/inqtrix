"""Per-user project namespace adoption (cross-device).

The MemoryUserDirectory mirrors the Postgres contract for
``resolve_default_workspace``: the first authenticated boot ADOPTS the browser's
namespace, and every later call (any device/browser) returns that same adopted
value — so a user's project data follows them instead of being stranded under a
per-browser random id. These assertions would go red if the adopt stopped being
idempotent or did not survive a re-login.
"""

import uuid
from types import SimpleNamespace

import pytest

from inqtrix.auth.directory import MemoryUserDirectory, MirroredUser
from inqtrix.storage.auth_postgres import PostgresUserDirectory


async def _record(
    users: MemoryUserDirectory, *, sub: str = "user-1"
) -> uuid.UUID:
    mirrored = await users.record_login(
        tenant_id="default",
        issuer="oidc",
        subject=sub,
        email=f"{sub}@example.com",
        email_verified=True,
        display_name=sub,
    )
    return mirrored.user_id


@pytest.mark.asyncio
async def test_first_call_adopts_then_is_idempotent() -> None:
    users = MemoryUserDirectory()
    user_id = await _record(users)
    # First boot adopts this browser's namespace.
    first = await users.resolve_default_workspace(
        tenant_id="default", user_id=user_id, candidate="ws_browserA",
    )
    assert first == "ws_browserA"
    # A second device sends ITS browser id, but the user already has a canonical
    # namespace — it is returned unchanged (the data follows the user).
    second = await users.resolve_default_workspace(
        tenant_id="default", user_id=user_id, candidate="ws_browserB",
    )
    assert second == "ws_browserA"


@pytest.mark.asyncio
async def test_adopted_namespace_survives_relogin() -> None:
    users = MemoryUserDirectory()
    user_id = await _record(users)
    await users.resolve_default_workspace(
        tenant_id="default", user_id=user_id, candidate="ws_browserA",
    )
    # A re-login refreshes the profile but must NOT drop the adopted namespace
    # (else the data would orphan on every login).
    await _record(users)
    mirrored = await users.find_user(tenant_id="default", issuer="oidc", subject="user-1")
    assert mirrored is not None
    assert mirrored.default_workspace_id == "ws_browserA"


@pytest.mark.asyncio
async def test_unknown_user_returns_none() -> None:
    users = MemoryUserDirectory()
    result = await users.resolve_default_workspace(
        tenant_id="default", user_id=uuid.uuid4(), candidate="ws_x",
    )
    # No mirror row → None, so the caller falls back to the browser-local id.
    assert result is None


@pytest.mark.asyncio
async def test_same_external_subject_is_isolated_between_tenants() -> None:
    users = MemoryUserDirectory()
    first = await users.record_login(
        tenant_id="tenant-a",
        issuer="oidc",
        subject="same-subject",
        email="a@example.com",
        email_verified=True,
        display_name="A",
    )
    second = await users.record_login(
        tenant_id="tenant-b",
        issuer="oidc",
        subject="same-subject",
        email="b@example.com",
        email_verified=True,
        display_name="B",
    )

    assert first.user_id != second.user_id
    assert await users.find_user(
        tenant_id="tenant-a", issuer="oidc", subject="same-subject"
    ) == first
    assert await users.find_by_user_id(
        tenant_id="tenant-b", user_id=first.user_id
    ) is None
    assert await users.promote_if_no_admin(
        tenant_id="tenant-a", user_id=first.user_id
    )
    assert await users.promote_if_no_admin(
        tenant_id="tenant-b", user_id=second.user_id
    )


def test_postgres_user_mapping_preserves_non_default_tenant() -> None:
    """A canonical UUID lookup must not silently remap its user to default."""
    user_id = uuid.uuid4()
    mapped = PostgresUserDirectory._to_user(
        SimpleNamespace(
            id=user_id,
            tenant_id="tenant-b",
            issuer="oidc",
            subject="alice",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
            disabled_at=None,
            instance_role="user",
            last_login_at=None,
            default_workspace_id=None,
            erasure_operation_id=None,
        ),
        MirroredUser,
    )

    assert mapped.user_id == user_id
    assert mapped.tenant_id == "tenant-b"
