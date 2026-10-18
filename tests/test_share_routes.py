"""HTTP contracts for direct sharing with canonical user UUIDs.

The owner creates and revises a pending share, the recipient accepts or
removes it, and the owner may withdraw it.  Duplicate active grants and stale
revisions are explicit conflicts; removed aggregate/group surfaces stay absent.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.server.routers.shares import build_router as build_shares_router
from inqtrix.server.routers.users import build_router as build_users_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_oidc_bff import ISSUER, FakeIdp, make_provider, run_login


@dataclass(frozen=True)
class ShareWorld:
    """Two authenticated users and resources served by one container."""

    owner: TestClient
    recipient: TestClient
    owner_csrf: str
    recipient_csrf: str
    owned_run_id: str
    foreign_run_id: str
    owner_user_id: uuid.UUID
    recipient_user_id: uuid.UUID


def make_world() -> ShareWorld:
    identity = MemoryIdentityStore()
    idp = FakeIdp()
    provider, users = make_provider(idp)
    container = build_container(
        providers=ProviderContext(llm=KnowledgeStubLLM(), search=StubSearch()),
        strategies=None,
        settings=Settings(
            server=ServerSettings(public_base_url=""),
            storage=StorageSettings(backend="memory", database_url=""),
        ),
        semaphore_factory=lambda: asyncio.Semaphore(1),
        auth_provider=provider,
        permissions=AuthorizationService(
            members=identity,
            shares=identity,
            audit=identity,
        ),
        workspace_admin=identity,
    )
    assert container.share_service is not None
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_shares_router(container))
    app.include_router(build_users_router(container))

    owner_client = TestClient(app, base_url="http://127.0.0.1:5100")
    run_login(owner_client, idp)

    idp.claims_override.update(
        {
            "sub": "user-2",
            "email": "bob@example.com",
            "preferred_username": "Bob Beispiel",
        }
    )
    recipient_client = TestClient(app, base_url="http://127.0.0.1:5100")
    run_login(recipient_client, idp)
    idp.claims_override.clear()

    async def _users():
        owner = await users.find_user(
            tenant_id="default",
            issuer=ISSUER,
            subject="user-1234",
        )
        recipient = await users.find_user(
            tenant_id="default",
            issuer=ISSUER,
            subject="user-2",
        )
        assert owner is not None
        assert recipient is not None
        return owner, recipient

    owner_user, recipient_user = asyncio.run(_users())
    foreign_user_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
    asyncio.run(
        users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject="foreign-owner",
            email="foreign@example.com",
            email_verified=True,
            display_name="Foreign Owner",
            canonical_user_id=foreign_user_id,
        )
    )
    owned_run_id = container.run_store.submit(
        question="meine Recherche",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "ok"}),
        created_by_user_id=owner_user.user_id,
        created_by_tenant_id="default",
    )["run_id"]
    foreign_run_id = container.run_store.submit(
        question="fremde Recherche",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "ok"}),
        created_by_user_id=foreign_user_id,
        created_by_tenant_id="default",
    )["run_id"]
    return ShareWorld(
        owner=owner_client,
        recipient=recipient_client,
        owner_csrf=owner_client.cookies.get("inqtrix_csrf"),
        recipient_csrf=recipient_client.cookies.get("inqtrix_csrf"),
        owned_run_id=owned_run_id,
        foreign_run_id=foreign_run_id,
        owner_user_id=owner_user.user_id,
        recipient_user_id=recipient_user.user_id,
    )


@pytest.fixture()
def world() -> ShareWorld:
    return make_world()


def grant(
    world: ShareWorld,
    *,
    run_id: str | None = None,
    user_id: uuid.UUID | str | None = None,
    permission: str = "view",
):
    return world.owner.post(
        "/v1/shares",
        json={
            "resource_type": "run",
            "resource_id": run_id or world.owned_run_id,
            "invitees": [
                {
                    "user_id": str(user_id or world.recipient_user_id),
                    "permission": permission,
                }
            ],
        },
        headers={"X-CSRF-Token": world.owner_csrf},
    )


def test_user_search_returns_canonical_user_id(world: ShareWorld):
    found = world.owner.get("/v1/users/search", params={"q": "bo"})

    assert found.status_code == 200
    assert found.json()["data"] == [
        {
            "id": str(world.recipient_user_id),
            "display_name": "Bob Beispiel",
            "email": "bob@example.com",
        }
    ]
    assert world.owner.get(
        "/v1/users/search", params={"q": "b"}
    ).status_code == 400


def test_grant_list_and_owner_withdrawal(world: ShareWorld):
    created_response = grant(world)
    assert created_response.status_code == 201
    created = created_response.json()["data"][0]
    assert created["recipient_user_id"] == str(world.recipient_user_id)
    assert created["permission"] == "view"
    assert created["revision"] == 1
    assert created["accepted_at"] is None

    listed = world.owner.get(
        "/v1/shares",
        params={
            "resource_type": "run",
            "resource_id": world.owned_run_id,
        },
    )
    assert listed.status_code == 200
    assert listed.json()["data"] == [created]

    revoked = world.owner.delete(
        f"/v1/shares/{created['id']}",
        headers={"X-CSRF-Token": world.owner_csrf},
    )
    assert revoked.status_code == 204
    assert revoked.content == b""
    assert world.owner.get(
        "/v1/shares",
        params={
            "resource_type": "run",
            "resource_id": world.owned_run_id,
        },
    ).json()["data"] == []


def test_active_duplicate_is_conflict_and_does_not_upsert(world: ShareWorld):
    original = grant(world).json()["data"][0]

    duplicate = grant(world, permission="edit")

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["type"] == "conflict"
    listed = world.owner.get(
        "/v1/shares",
        params={
            "resource_type": "run",
            "resource_id": world.owned_run_id,
        },
    ).json()["data"]
    assert len(listed) == 1
    assert listed[0]["id"] == original["id"]
    assert listed[0]["permission"] == "view"
    assert listed[0]["revision"] == original["revision"]


def test_owner_updates_permission_with_optimistic_revision(world: ShareWorld):
    created = grant(world).json()["data"][0]

    updated_response = world.owner.patch(
        f"/v1/shares/{created['id']}",
        json={"permission": "edit", "expected_revision": 1},
        headers={"X-CSRF-Token": world.owner_csrf},
    )

    assert updated_response.status_code == 200
    assert updated_response.json()["object"] == "share"
    updated = updated_response.json()["data"]
    assert updated["id"] == created["id"]
    assert updated["permission"] == "edit"
    assert updated["revision"] == 2

    stale = world.owner.patch(
        f"/v1/shares/{created['id']}",
        json={"permission": "view", "expected_revision": 1},
        headers={"X-CSRF-Token": world.owner_csrf},
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "message": stale.json()["error"]["message"],
        "type": "conflict",
        "current_revision": 2,
    }


def test_recipient_cannot_list_or_update_owner_share(world: ShareWorld):
    created = grant(world).json()["data"][0]

    listed = world.recipient.get(
        "/v1/shares",
        params={
            "resource_type": "run",
            "resource_id": world.owned_run_id,
        },
    )
    updated = world.recipient.patch(
        f"/v1/shares/{created['id']}",
        json={"permission": "edit", "expected_revision": 1},
        headers={"X-CSRF-Token": world.recipient_csrf},
    )

    assert listed.status_code == 404
    assert updated.status_code == 404


def test_accept_is_idempotent_and_preserves_timestamp(world: ShareWorld):
    created = grant(world).json()["data"][0]

    first = world.recipient.post(
        f"/v1/shares/{created['id']}/accept",
        headers={"X-CSRF-Token": world.recipient_csrf},
    )
    second = world.recipient.post(
        f"/v1/shares/{created['id']}/accept",
        headers={"X-CSRF-Token": world.recipient_csrf},
    )

    assert first.status_code == 200
    assert first.json()["object"] == "share"
    assert first.json()["data"]["accepted_at"] is not None
    assert second.status_code == 200
    assert second.json() == first.json()
    assert world.owner.post(
        f"/v1/shares/{created['id']}/accept",
        headers={"X-CSRF-Token": world.owner_csrf},
    ).status_code == 404


@pytest.mark.parametrize("accept_first", [False, True])
def test_recipient_can_decline_or_leave_share(
    world: ShareWorld, accept_first: bool
):
    created = grant(world).json()["data"][0]
    if accept_first:
        assert world.recipient.post(
            f"/v1/shares/{created['id']}/accept",
            headers={"X-CSRF-Token": world.recipient_csrf},
        ).status_code == 200

    removed = world.recipient.delete(
        f"/v1/shares/{created['id']}",
        headers={"X-CSRF-Token": world.recipient_csrf},
    )

    assert removed.status_code == 204
    assert removed.content == b""
    assert world.recipient.post(
        f"/v1/shares/{created['id']}/accept",
        headers={"X-CSRF-Token": world.recipient_csrf},
    ).status_code == 404


def test_foreign_resource_and_unknown_share_are_hidden(world: ShareWorld):
    assert grant(world, run_id=world.foreign_run_id).status_code == 404
    assert world.owner.get(
        "/v1/shares",
        params={
            "resource_type": "run",
            "resource_id": world.foreign_run_id,
        },
    ).status_code == 404
    assert world.owner.delete(
        "/v1/shares/missing",
        headers={"X-CSRF-Token": world.owner_csrf},
    ).status_code == 404


def test_unknown_invitee_and_malformed_user_id_are_rejected(world: ShareWorld):
    unknown = grant(
        world,
        user_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
    )
    malformed = grant(world, user_id="not-a-uuid")

    assert unknown.status_code == 400
    assert "User not found" in unknown.json()["error"]["message"]
    assert malformed.status_code == 400


@pytest.mark.parametrize("legacy_field", ["subject_id", "recipient_user_id"])
def test_post_rejects_legacy_invitee_identity_fields(
    world: ShareWorld, legacy_field: str
):
    response = world.owner.post(
        "/v1/shares",
        json={
            "resource_type": "run",
            "resource_id": world.owned_run_id,
            "invitees": [
                {
                    legacy_field: str(world.recipient_user_id),
                    "permission": "view",
                }
            ],
        },
        headers={"X-CSRF-Token": world.owner_csrf},
    )

    assert response.status_code == 400


def test_validation_errors(world: ShareWorld):
    headers = {"X-CSRF-Token": world.owner_csrf}
    assert world.owner.post(
        "/v1/shares",
        json={"resource_type": "run", "resource_id": world.owned_run_id},
        headers=headers,
    ).status_code == 400
    assert grant(world, permission="manage").status_code == 400
    assert world.owner.patch(
        "/v1/shares/missing",
        json={"permission": "manage", "expected_revision": 1},
        headers=headers,
    ).status_code == 400
    assert world.owner.get(
        "/v1/shares", params={"resource_type": "run"}
    ).status_code == 400


def test_suggest_permission_is_rejected_for_existing_resource(
    world: ShareWorld,
) -> None:
    response = grant(world, permission="suggest")

    assert response.status_code == 400
    assert "allowed values are 'view', 'edit'" in response.json()["error"]["message"]


def test_removed_aggregate_share_routes_stay_absent(world: ShareWorld):
    for path in (
        "/v1/shares/shared-with-me",
        "/v1/shares/outgoing",
    ):
        assert not any(
            route.path == path
            and "GET" in (getattr(route, "methods", None) or set())
            for route in world.owner.app.routes
        )
