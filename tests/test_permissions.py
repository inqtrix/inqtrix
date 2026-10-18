"""Unit tests for the permission chokepoint and the memory backend."""

from __future__ import annotations

import logging
import uuid

import pytest

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import (
    AuthorizationService,
    ResourceNotFound,
    SharePermission,
    WorkspaceNotFound,
    WorkspaceRole,
    share_permissions_for_resource,
    share_permissions_satisfying,
)
from inqtrix.auth.principal import ANONYMOUS_PRINCIPAL, STATIC_PRINCIPAL, Principal


SCOPED_KINDS = ("oidc_session", "pat")
"""Both scoped principal kinds — PAT must be filtered exactly like an
OIDC session (pins the exclusion set against an inclusion refactor)."""


def user_id(name: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"inqtrix-test:{name}")


def oidc_principal(
    name: str, *, tenant_id: str = "default", kind: str = "oidc_session"
) -> Principal:
    return Principal(
        user_id=user_id(name), kind=kind, tenant_id=tenant_id, role="member"
    )


def make_service(store: MemoryIdentityStore | None = None) -> tuple[
    AuthorizationService, MemoryIdentityStore
]:
    identity = store or MemoryIdentityStore()
    return (
        AuthorizationService(members=identity, shares=identity, audit=identity),
        identity,
    )


# ------------------------------------------------------------------ #
# Ordered enums
# ------------------------------------------------------------------ #


def test_workspace_role_ordering_is_total_and_ascending():
    assert WorkspaceRole.OWNER.at_least(WorkspaceRole.VIEWER)
    assert WorkspaceRole.EDITOR.at_least(WorkspaceRole.COMMENTER)
    assert not WorkspaceRole.VIEWER.at_least(WorkspaceRole.COMMENTER)
    assert WorkspaceRole.VIEWER.at_least(WorkspaceRole.VIEWER)


def test_share_permission_ordering_is_total_and_ascending():
    assert SharePermission.EDIT.at_least(SharePermission.VIEW)
    assert SharePermission.EDIT.at_least(SharePermission.SUGGEST)
    assert SharePermission.SUGGEST.at_least(SharePermission.VIEW)
    assert not SharePermission.VIEW.at_least(SharePermission.EDIT)
    assert not SharePermission.SUGGEST.at_least(SharePermission.EDIT)
    assert not SharePermission.VIEW.at_least(SharePermission.SUGGEST)
    assert SharePermission.VIEW.at_least(SharePermission.VIEW)


def test_share_permission_policy_is_resource_specific() -> None:
    assert share_permissions_for_resource("run") == (
        SharePermission.VIEW,
        SharePermission.EDIT,
    )
    assert share_permissions_for_resource("editor_document") == (
        SharePermission.VIEW,
        SharePermission.SUGGEST,
        SharePermission.EDIT,
    )
    assert share_permissions_for_resource("file") == ()
    assert share_permissions_satisfying(
        "editor_document", SharePermission.VIEW
    ) == (
        SharePermission.VIEW,
        SharePermission.SUGGEST,
        SharePermission.EDIT,
    )
    assert share_permissions_satisfying(
        "editor_document", SharePermission.SUGGEST
    ) == (SharePermission.SUGGEST, SharePermission.EDIT)
    assert share_permissions_satisfying(
        "editor_document", SharePermission.EDIT
    ) == (SharePermission.EDIT,)
    assert share_permissions_satisfying(
        "run", SharePermission.SUGGEST
    ) == ()


# ------------------------------------------------------------------ #
# Legacy principals stay unscoped
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [ANONYMOUS_PRINCIPAL, STATIC_PRINCIPAL])
async def test_legacy_principals_resolve_to_no_scoping(principal):
    service, _ = make_service()
    assert await service.resolve_user_context(principal) is None
    # Workspace checks stay unscoped, but only ownerless legacy resources
    # are visible. Owned multi-user data never becomes tenant-public.
    assert await service.resolve_workspace(principal, "ws_any") == "ws_any"
    assert await service.can(
        principal,
        SharePermission.EDIT,
        owner_user_id=None,
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="r1",
    )
    assert not await service.can(
        principal,
        SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="r2",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_scoped_principal_resolves_memberships_server_side(kind):
    service, store = make_service()
    store.add_workspace("ws_a")
    store.add_member("ws_a", user_id("alice"), WorkspaceRole.EDITOR)

    context = await service.resolve_user_context(
        oidc_principal("alice", kind=kind)
    )

    assert context is not None
    assert context.workspace_ids == ("ws_a",)


# ------------------------------------------------------------------ #
# Workspace resolution: 404-not-403, indistinguishable absence
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", SCOPED_KINDS)
async def test_non_member_and_unknown_workspace_raise_identically(kind):
    service, store = make_service()
    store.add_workspace("ws_b")
    store.add_member("ws_b", user_id("bob"), WorkspaceRole.OWNER)
    alice = oidc_principal("alice", kind=kind)

    with pytest.raises(WorkspaceNotFound):
        await service.resolve_workspace(alice, "ws_b")
    with pytest.raises(WorkspaceNotFound):
        await service.resolve_workspace(alice, "ws_does_not_exist")


@pytest.mark.asyncio
async def test_min_role_below_threshold_hides_the_workspace():
    service, store = make_service()
    store.add_workspace("ws_a")
    store.add_member("ws_a", user_id("alice"), WorkspaceRole.VIEWER)
    alice = oidc_principal("alice")

    assert await service.resolve_workspace(alice, "ws_a") == "ws_a"
    with pytest.raises(WorkspaceNotFound):
        await service.resolve_workspace(
            alice, "ws_a", min_role=WorkspaceRole.EDITOR
        )


@pytest.mark.asyncio
async def test_tenant_mismatch_hides_the_workspace():
    service, store = make_service()
    store.add_workspace("ws_a", tenant_id="tenant-x")
    store.add_member("ws_a", user_id("alice"), WorkspaceRole.OWNER)

    with pytest.raises(WorkspaceNotFound):
        await service.resolve_workspace(
            oidc_principal("alice", tenant_id="default"), "ws_a"
        )


# ------------------------------------------------------------------ #
# Resource authority: owner or accepted direct share only
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_workspace_role_never_implies_resource_access():
    service, store = make_service()
    store.add_workspace("ws_a")
    store.add_member("ws_a", user_id("alice"), WorkspaceRole.VIEWER)
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="prompt_template",
        resource_id="r1",
        permission=SharePermission.EDIT,
        granted_by_user_id=user_id("owner"),
    )
    alice = oidc_principal("alice")

    # Workspace membership is an administrative namespace, not a resource
    # grant. Only ownership or the direct share below authorizes access.
    assert not await service.can(
        alice,
        SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="r2",
    )
    assert await service.can(
        alice,
        SharePermission.EDIT,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="r1",
    )


@pytest.mark.asyncio
async def test_sharing_master_switch_suspends_without_deleting_grants() -> None:
    """The module gate removes live access and preserves reactivation state."""
    identity = MemoryIdentityStore()
    identity.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="editor_document",
        resource_id="ed_suspended",
        permission=SharePermission.EDIT,
        granted_by_user_id=user_id("owner"),
    )
    disabled = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
        sharing_enabled=False,
    )
    enabled = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
        sharing_enabled=True,
    )
    alice = oidc_principal("alice")
    owner = oidc_principal("owner")

    assert not await disabled.can(
        alice,
        SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="editor_document",
        resource_id="ed_suspended",
    )
    assert await disabled.can(
        owner,
        SharePermission.EDIT,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="editor_document",
        resource_id="ed_suspended",
    )
    assert await enabled.can(
        alice,
        SharePermission.EDIT,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="editor_document",
        resource_id="ed_suspended",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted", "minimum", "allowed"),
    [
        (SharePermission.VIEW, SharePermission.VIEW, True),
        (SharePermission.VIEW, SharePermission.SUGGEST, False),
        (SharePermission.VIEW, SharePermission.EDIT, False),
        (SharePermission.SUGGEST, SharePermission.VIEW, True),
        (SharePermission.SUGGEST, SharePermission.SUGGEST, True),
        (SharePermission.SUGGEST, SharePermission.EDIT, False),
        (SharePermission.EDIT, SharePermission.VIEW, True),
        (SharePermission.EDIT, SharePermission.SUGGEST, True),
        (SharePermission.EDIT, SharePermission.EDIT, True),
    ],
)
async def test_editor_document_share_access_follows_permission_order(
    granted: SharePermission,
    minimum: SharePermission,
    allowed: bool,
) -> None:
    service, store = make_service()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="editor_document",
        resource_id="ed_1",
        permission=granted,
        granted_by_user_id=user_id("owner"),
    )

    assert (
        await service.can(
            oidc_principal("alice"),
            minimum,
            owner_user_id=user_id("owner"),
            resource_tenant_id="default",
            resource_type="editor_document",
            resource_id="ed_1",
        )
        is allowed
    )


@pytest.mark.asyncio
async def test_suggest_grant_on_legacy_resource_fails_closed() -> None:
    service, store = make_service()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="run",
        resource_id="run_1",
        permission=SharePermission.SUGGEST,
        granted_by_user_id=user_id("owner"),
    )

    assert not await service.can(
        oidc_principal("alice"),
        SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="run",
        resource_id="run_1",
    )


def test_memory_share_lookup_rejects_permission_unsupported_by_resource() -> None:
    store = MemoryIdentityStore()
    alice_id = user_id("alice")
    store.add_share(
        recipient_user_id=alice_id,
        resource_type="run",
        resource_id="run_1",
        permission=SharePermission.SUGGEST,
        granted_by_user_id=user_id("owner"),
    )

    assert (
        store.permission_for_sync(
            tenant_id="default",
            resource_type="run",
            resource_id="run_1",
            recipient_user_id=alice_id,
        )
        is None
    )


@pytest.mark.asyncio
async def test_non_shareable_resource_owner_access_is_unchanged() -> None:
    service, _store = make_service()
    alice = oidc_principal("alice")

    assert await service.can(
        alice,
        SharePermission.EDIT,
        owner_user_id=alice.user_id,
        resource_tenant_id="default",
        resource_type="file",
        resource_id="file_1",
    )


@pytest.mark.asyncio
async def test_revoked_share_stops_granting():
    service, store = make_service()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="prompt_template", resource_id="p1",
        permission=SharePermission.EDIT,
        granted_by_user_id=user_id("owner"),
    )
    alice = oidc_principal("alice")
    assert await service.can(
        alice,
        SharePermission.EDIT,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="p1",
    )

    store.revoke_share(
        recipient_user_id=user_id("alice"),
        resource_type="prompt_template", resource_id="p1",
    )
    assert not await service.can(
        alice,
        SharePermission.EDIT,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template",
        resource_id="p1",
    )


# ------------------------------------------------------------------ #
# require(): hidden from the client, loud for operators
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_require_denial_raises_not_found_and_is_audited(caplog):
    service, store = make_service()
    alice = oidc_principal("alice")

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with pytest.raises(ResourceNotFound):
            await service.require(
                alice, SharePermission.VIEW,
                owner_user_id=user_id("owner"),
                resource_tenant_id="default",
                resource_type="prompt_template", resource_id="r1",
            )

    authz_messages = [
        message for message in caplog.messages if "authz denied" in message
    ]
    assert any("actor_ref=usr_" in message for message in authz_messages)
    assert all(str(user_id("alice")) not in message for message in authz_messages)
    assert all("resource_ref=res_" in message for message in authz_messages)
    assert all(
        "resource=prompt_template/r1" not in message
        for message in authz_messages
    )
    assert all("permission" not in message for message in authz_messages)
    assert len(store.audit_entries) == 1
    entry = store.audit_entries[0]
    assert entry.action == "authz.denied"
    assert entry.actor_user_id == user_id("alice")
    assert entry.resource_id == "r1"
    assert entry.detail == {"permission": "view"}


@pytest.mark.asyncio
async def test_require_passes_silently_when_granted():
    service, store = make_service()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="prompt_template", resource_id="r1",
        permission=SharePermission.VIEW,
        granted_by_user_id=user_id("owner"),
    )

    await service.require(
        oidc_principal("alice"), SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="default",
        resource_type="prompt_template", resource_id="r1",
    )
    assert store.audit_entries == []


@pytest.mark.asyncio
async def test_shares_are_tenant_scoped():
    service, store = make_service()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="skill_template", resource_id="s1",
        permission=SharePermission.EDIT,
        granted_by_user_id=user_id("owner"),
        tenant_id="tenant-x",
    )

    assert not await service.can(
        oidc_principal("alice", tenant_id="default"),
        SharePermission.VIEW,
        owner_user_id=user_id("owner"),
        resource_tenant_id="tenant-x",
        resource_type="skill_template", resource_id="s1",
    )


# ------------------------------------------------------------------ #
# Settings bridge
# ------------------------------------------------------------------ #


def test_postgres_backend_without_url_fails_loudly():
    from inqtrix.server.container import build_permission_service
    from inqtrix.settings import Settings, StorageSettings

    with pytest.raises(RuntimeError, match="INQTRIX_DATABASE_URL"):
        build_permission_service(
            Settings(storage=StorageSettings(backend="postgres"))
        )


def test_storage_settings_survive_serialization_round_trip():
    from inqtrix.settings import Settings, StorageSettings

    settings = Settings(
        storage=StorageSettings(
            backend="postgres",
            database_url="postgresql+asyncpg://example/db",
        )
    )
    restored = Settings.model_validate(settings.model_dump())
    assert restored.storage.backend == "postgres"
    assert restored.storage.database_url == "postgresql+asyncpg://example/db"
    assert restored.storage.app_role == "inqtrix_app"


# ------------------------------------------------------------------ #
# Memory store arrangement guards
# ------------------------------------------------------------------ #


def test_add_member_to_unknown_workspace_fails_loudly():
    store = MemoryIdentityStore()
    with pytest.raises(KeyError, match="unknown workspace"):
        store.add_member("ws_missing", user_id("alice"), WorkspaceRole.OWNER)


@pytest.mark.asyncio
async def test_permission_for_unknown_recipient_is_none():
    store = MemoryIdentityStore()
    assert (
        await store.permission_for(
            tenant_id="default",
            resource_type="prompt_template",
            resource_id="p1",
            recipient_user_id=user_id("alice"),
        )
        is None
    )


@pytest.mark.asyncio
async def test_direct_user_id_shape_matches_share_lookup():
    store = MemoryIdentityStore()
    store.add_share(
        recipient_user_id=user_id("alice"),
        resource_type="prompt_template", resource_id="p1",
        permission=SharePermission.VIEW,
        granted_by_user_id=user_id("owner"),
    )
    grant = await store.permission_for(
        tenant_id="default",
        resource_type="prompt_template",
        resource_id="p1",
        recipient_user_id=user_id("alice"),
    )
    assert grant is SharePermission.VIEW
