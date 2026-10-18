"""Workspace-scoped direct sharing over canonical user UUIDs.

The optional boundary is enforced both by the user picker and by the grant
write.  Membership facts and share recipients use local ``users.id`` values;
IdP subjects are only external login bindings.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService, WorkspaceRole
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.server.routers.users import build_router as build_users_router
from inqtrix.settings import (
    ServerSettings,
    Settings,
    SharingSettings,
    StorageSettings,
)

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_oidc_bff import ISSUER, FakeIdp, make_provider, run_login

GRANTOR_SUBJECT = "user-1234"
COMEMBER_SUBJECT = "co-member"
STRANGER_SUBJECT = "stranger"
UNKNOWN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_memory_identity_atomic_capabilities_require_a_bound_event_sink():
    identity = MemoryIdentityStore()
    assert not identity.atomic_share_effects
    assert not identity.atomic_workspace_effects

    identity.bind_user_event_sink(lambda **_kwargs: None)

    assert identity.atomic_share_effects
    assert identity.atomic_workspace_effects


def make_world(*, restrict: bool):
    identity = MemoryIdentityStore()
    idp = FakeIdp()
    provider, users = make_provider(idp)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
            sharing=SharingSettings(restrict_to_workspace_members=restrict),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
        permissions=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
            restrict_to_workspace_members=restrict,
        ),
        workspace_admin=identity,
    )
    assert container.share_service is not None
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_shares_router(container))
    app.include_router(build_users_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    run_login(client, idp)

    async def _arrange():
        grantor = await users.find_user(
            tenant_id="default",
            issuer=ISSUER,
            subject=GRANTOR_SUBJECT,
        )
        assert grantor is not None
        comember = await users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject=COMEMBER_SUBJECT,
            email="cora@example.com",
            email_verified=True,
            display_name="Cora Member",
        )
        stranger = await users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject=STRANGER_SUBJECT,
            email="stan@example.com",
            email_verified=True,
            display_name="Stan Stranger",
        )

        workspace_id, _ = await identity.create_workspace(
            tenant_id="default",
            name="Team",
            created_by_user_id=grantor.user_id,
        )
        await identity.assign_member(
            tenant_id="default",
            workspace_id=workspace_id,
            user_id=comember.user_id,
            role=WorkspaceRole.EDITOR,
        )
        await identity.create_workspace(
            tenant_id="default",
            name="Other",
            created_by_user_id=stranger.user_id,
        )
        return grantor.user_id, comember.user_id, stranger.user_id, workspace_id

    grantor_id, comember_id, stranger_id, shared_workspace = asyncio.run(
        _arrange()
    )
    owned = container.run_store.submit(
        question="meine Recherche",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "ok"}),
        created_by_user_id=grantor_id,
        created_by_tenant_id="default",
    )["run_id"]
    csrf = client.cookies.get("inqtrix_csrf")
    return (
        client,
        csrf,
        owned,
        identity,
        shared_workspace,
        comember_id,
        stranger_id,
    )


def grant(
    client: TestClient,
    csrf: str,
    owned: str,
    recipient_user_id: uuid.UUID,
    permission: str = "view",
):
    return client.post(
        "/v1/shares",
        json={
            "resource_type": "run",
            "resource_id": owned,
            "invitees": [
                {
                    "user_id": str(recipient_user_id),
                    "permission": permission,
                }
            ],
        },
        headers={"X-CSRF-Token": csrf},
    )


def search(client: TestClient, query: str) -> set[uuid.UUID]:
    return {
        uuid.UUID(row["id"])
        for row in client.get(
            "/v1/users/search", params={"q": query}
        ).json()["data"]
    }


def test_off_keeps_sharing_tenant_wide():
    assert SharingSettings().restrict_to_workspace_members is False
    client, csrf, owned, _identity, _ws, _comember_id, stranger_id = (
        make_world(restrict=False)
    )

    with client:
        assert stranger_id in search(client, "stan")
        assert grant(client, csrf, owned, stranger_id).status_code == 201


def test_on_grant_allows_comember_denies_stranger():
    client, csrf, owned, identity, _ws, comember_id, stranger_id = make_world(
        restrict=True
    )

    with client:
        assert grant(client, csrf, owned, comember_id).status_code == 201
        denied = grant(client, csrf, owned, stranger_id)
        assert denied.status_code == 404
        assert any(
            entry.action == "share.denied"
            and entry.detail.get("recipient_user_id") == str(stranger_id)
            for entry in identity.audit_entries
        )


def test_on_nonexistent_invitee_is_hidden_like_a_non_member(caplog):
    client, csrf, owned, _identity, _ws, _comember_id, stranger_id = (
        make_world(restrict=True)
    )

    with caplog.at_level(logging.WARNING, logger="inqtrix"):
        with client:
            ghost = grant(client, csrf, owned, UNKNOWN_USER_ID)
            stranger = grant(client, csrf, owned, stranger_id)
            assert ghost.status_code == 404
            assert stranger.status_code == 404
            assert ghost.json() == stranger.json()

    authz_messages = [
        message for message in caplog.messages if "authz denied" in message
    ]
    assert len(authz_messages) == 2
    assert all("action=share" in message for message in authz_messages)
    assert all(str(UNKNOWN_USER_ID) not in message for message in authz_messages)
    assert all(str(stranger_id) not in message for message in authz_messages)
    assert all(owned not in message for message in authz_messages)


def test_on_typeahead_scopes_to_comembers():
    client, _csrf, _owned, _identity, _ws, comember_id, stranger_id = (
        make_world(restrict=True)
    )

    with client:
        assert search(client, "cora") == {comember_id}
        assert stranger_id not in search(client, "stan")


def test_on_grant_rechecks_membership_after_removal():
    client, csrf, owned, identity, workspace_id, comember_id, _stranger_id = (
        make_world(restrict=True)
    )

    with client:
        assert grant(client, csrf, owned, comember_id).status_code == 201
        asyncio.run(
            identity.remove_member(
                tenant_id="default",
                workspace_id=workspace_id,
                user_id=comember_id,
            )
        )
        duplicate_or_denied = grant(
            client,
            csrf,
            owned,
            comember_id,
            permission="edit",
        )
        assert duplicate_or_denied.status_code == 404


def test_last_common_workspace_removal_revokes_existing_share():
    client, csrf, owned, identity, workspace_id, comember_id, _stranger_id = (
        make_world(restrict=True)
    )

    with client:
        created = grant(client, csrf, owned, comember_id)
        assert created.status_code == 201
        share_id = created.json()["data"][0]["id"]
        assert asyncio.run(
            identity.get_share(tenant_id="default", share_id=share_id)
        ) is not None

        assert asyncio.run(
            identity.remove_member(
                tenant_id="default",
                workspace_id=workspace_id,
                user_id=comember_id,
            )
        )

        assert asyncio.run(
            identity.get_share(tenant_id="default", share_id=share_id)
        ) is None
        assert any(
            entry.action == "share.workspace_boundary_revoked"
            for entry in identity.audit_entries
        )


def test_another_common_workspace_preserves_existing_share():
    client, csrf, owned, identity, workspace_id, comember_id, _stranger_id = (
        make_world(restrict=True)
    )

    with client:
        created = grant(client, csrf, owned, comember_id)
        assert created.status_code == 201
        share_id = created.json()["data"][0]["id"]
        record = asyncio.run(
            identity.get_share(tenant_id="default", share_id=share_id)
        )
        assert record is not None

        async def arrange_and_remove() -> None:
            second_workspace_id, _ = await identity.create_workspace(
                tenant_id="default",
                name="Second Team",
                created_by_user_id=record.granted_by_user_id,
            )
            await identity.assign_member(
                tenant_id="default",
                workspace_id=second_workspace_id,
                user_id=comember_id,
                role=WorkspaceRole.EDITOR,
            )
            assert await identity.remove_member(
                tenant_id="default",
                workspace_id=workspace_id,
                user_id=comember_id,
            )

        asyncio.run(arrange_and_remove())

        assert asyncio.run(
            identity.get_share(tenant_id="default", share_id=share_id)
        ) is not None


def test_share_workspace_predicate_uses_canonical_user_ids():
    identity = MemoryIdentityStore()
    permissions = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
    )
    first_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    second_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    third_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
    nobody_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
    loner_id = uuid.UUID("55555555-5555-4555-8555-555555555555")

    async def scenario():
        workspace_id, _ = await identity.create_workspace(
            tenant_id="default",
            name="Team",
            created_by_user_id=first_id,
        )
        await identity.assign_member(
            tenant_id="default",
            workspace_id=workspace_id,
            user_id=second_id,
            role=WorkspaceRole.VIEWER,
        )
        second_workspace_id, _ = await identity.create_workspace(
            tenant_id="default",
            name="Team2",
            created_by_user_id=first_id,
        )
        await identity.assign_member(
            tenant_id="default",
            workspace_id=second_workspace_id,
            user_id=second_id,
            role=WorkspaceRole.VIEWER,
        )
        await identity.create_workspace(
            tenant_id="default",
            name="Other",
            created_by_user_id=third_id,
        )
        return (
            await permissions.share_workspace(
                tenant_id="default",
                user_id_a=first_id,
                user_id_b=second_id,
            ),
            await permissions.share_workspace(
                tenant_id="default",
                user_id_a=first_id,
                user_id_b=third_id,
            ),
            await permissions.share_workspace(
                tenant_id="default",
                user_id_a=first_id,
                user_id_b=nobody_id,
            ),
            await permissions.share_workspace(
                tenant_id="default",
                user_id_a=loner_id,
                user_id_b=first_id,
            ),
        )

    same, different, no_target, no_grantor = asyncio.run(scenario())
    assert same is True
    assert different is False
    assert no_target is False
    assert no_grantor is False
