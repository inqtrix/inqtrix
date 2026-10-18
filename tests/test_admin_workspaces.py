"""Admin workspace + membership management (``/v1/admin/workspaces*``).

Pins the instance-admin surface end to end over the real cookie-session
harness: an instance admin creates workspaces, assigns/repositions users, and
the last-owner guard keeps a workspace from being orphaned. Authorization is
the instance-admin axis (the shared ``require_instance_admin`` guard) — the
unit-level branch coverage lives in ``test_quota_admin_routes.py``; here the
focus is the workspace/membership behaviour and its guards.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.permissions import (
    LastWorkspaceOwnerError,
    AuthorizationService,
    WorkspaceRole,
)
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.admin_workspaces import build_router
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_oidc_bff import ISSUER, FakeIdp, make_provider, run_login

ADMIN_SUB = "user-1234"


def make_world():
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
            members=identity, shares=identity, audit=identity
        ),
        workspace_admin=identity,
    )
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    return client, container, idp, identity, users


def login(client, idp) -> str:
    run_login(client, idp)  # first login auto-promotes the owner to admin
    return client.get("/api/auth/session").json()["csrf_token"]


def current_user_id(client: TestClient) -> str:
    return client.get("/api/auth/session").json()["user"]["id"]


def demote(users, subject: str = ADMIN_SUB) -> None:
    user = asyncio.run(
        users.find_user(tenant_id="default", issuer=ISSUER, subject=subject)
    )
    assert user is not None
    asyncio.run(
        users.set_instance_role(
            tenant_id="default", user_id=user.user_id, role="user"
        )
    )


def seed_user(users, sub: str, name: str) -> str:
    user = asyncio.run(
        users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject=sub,
            email=f"{sub}@example.com",
            email_verified=True,
            display_name=name,
        )
    )
    return str(user.user_id)


def create_workspace(client, csrf, name="Team") -> str:
    resp = client.post(
        "/v1/admin/workspaces",
        json={"name": name},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["workspace_id"]


def test_create_list_rename_delete_workspace():
    client, _container, idp, _identity, _users = make_world()
    with client:
        csrf = login(client, idp)
        admin_user_id = current_user_id(client)
        workspace_id = create_workspace(client, csrf, "Research")

        listing = client.get("/v1/admin/workspaces").json()["data"]
        assert listing == [
            {
                "workspace_id": workspace_id,
                "name": "Research",
                "created_by_user_id": admin_user_id,
                "member_count": 1,
            }
        ]

        renamed = client.patch(
            f"/v1/admin/workspaces/{workspace_id}",
            json={"name": "Legal"},
            headers={"X-CSRF-Token": csrf},
        )
        assert renamed.status_code == 200
        assert client.get("/v1/admin/workspaces").json()["data"][0]["name"] == (
            "Legal"
        )

        deleted = client.delete(
            f"/v1/admin/workspaces/{workspace_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert deleted.status_code == 204
        assert client.get("/v1/admin/workspaces").json()["data"] == []


def test_non_string_names_rejected_on_create_and_rename():
    """Create and rename share one name validator: non-string JSON
    values are a uniform 400 on both, never a repr-leaked "None"."""
    client, _container, idp, _identity, _users = make_world()
    with client:
        csrf = login(client, idp)
        workspace_id = create_workspace(client, csrf, "Research")
        for bad_name in (None, 0, False):
            created = client.post(
                "/v1/admin/workspaces",
                json={"name": bad_name},
                headers={"X-CSRF-Token": csrf},
            )
            assert created.status_code == 400, bad_name
            renamed = client.patch(
                f"/v1/admin/workspaces/{workspace_id}",
                json={"name": bad_name},
                headers={"X-CSRF-Token": csrf},
            )
            assert renamed.status_code == 400, bad_name
        # The workspace kept its original name through every rejection.
        listing = client.get("/v1/admin/workspaces").json()["data"]
        assert listing[0]["name"] == "Research"


def test_assign_change_and_remove_member():
    client, _container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        bob_user_id = seed_user(users, "bob", "Bob B")
        admin_user_id = current_user_id(client)
        workspace_id = create_workspace(client, csrf)

        added = client.post(
            f"/v1/admin/workspaces/{workspace_id}/members",
            json={"user_id": bob_user_id, "role": "editor"},
            headers={"X-CSRF-Token": csrf},
        )
        assert added.status_code == 201
        assert added.json() == {"user_id": bob_user_id, "role": "editor"}

        members = client.get(
            f"/v1/admin/workspaces/{workspace_id}/members"
        ).json()["data"]
        by_user_id = {row["user_id"]: row for row in members}
        assert by_user_id[bob_user_id]["role"] == "editor"
        assert by_user_id[bob_user_id]["display_name"] == "Bob B"
        assert by_user_id[bob_user_id]["email"] == "bob@example.com"
        assert by_user_id[admin_user_id]["role"] == "owner"

        changed = client.patch(
            f"/v1/admin/workspaces/{workspace_id}/members/{bob_user_id}",
            json={"role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert changed.status_code == 200
        assert changed.json()["role"] == "viewer"

        removed = client.delete(
            f"/v1/admin/workspaces/{workspace_id}/members/{bob_user_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert removed.status_code == 204
        remaining = client.get(
            f"/v1/admin/workspaces/{workspace_id}/members"
        ).json()["data"]
        assert [row["user_id"] for row in remaining] == [admin_user_id]


def test_last_owner_cannot_be_demoted_or_removed():
    client, _container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        bob_user_id = seed_user(users, "bob", "Bob B")
        admin_user_id = current_user_id(client)
        workspace_id = create_workspace(client, csrf)

        # The creator is the sole OWNER -> demote and remove both 409.
        demoted = client.patch(
            f"/v1/admin/workspaces/{workspace_id}/members/{admin_user_id}",
            json={"role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert demoted.status_code == 409
        removed = client.delete(
            f"/v1/admin/workspaces/{workspace_id}/members/{admin_user_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert removed.status_code == 409

        # The POST (assign) path guards too: re-assigning the sole OWNER a
        # lower role is an upsert-demote, so it is refused (409) as well.
        reassigned = client.post(
            f"/v1/admin/workspaces/{workspace_id}/members",
            json={"user_id": admin_user_id, "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert reassigned.status_code == 409

        # Add a second OWNER, then the first can be demoted.
        client.post(
            f"/v1/admin/workspaces/{workspace_id}/members",
            json={"user_id": bob_user_id, "role": "owner"},
            headers={"X-CSRF-Token": csrf},
        )
        ok = client.patch(
            f"/v1/admin/workspaces/{workspace_id}/members/{admin_user_id}",
            json={"role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert ok.status_code == 200


def test_assign_unknown_user_is_404():
    client, _container, idp, _identity, _users = make_world()
    with client:
        csrf = login(client, idp)
        workspace_id = create_workspace(client, csrf)
        resp = client.post(
            f"/v1/admin/workspaces/{workspace_id}/members",
            json={"user_id": str(uuid.uuid4()), "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 404


def test_role_patch_never_inserts_a_missing_membership():
    client, _container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        missing_member_id = seed_user(users, "not-a-member", "Not Member")
        workspace_id = create_workspace(client, csrf)

        response = client.patch(
            f"/v1/admin/workspaces/{workspace_id}/members/{missing_member_id}",
            json={"role": "editor"},
            headers={"X-CSRF-Token": csrf},
        )

        assert response.status_code == 404
        rows = client.get(
            f"/v1/admin/workspaces/{workspace_id}/members"
        ).json()["data"]
        assert missing_member_id not in {row["user_id"] for row in rows}


def test_operations_on_missing_workspace_are_404():
    client, _container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        bob_user_id = seed_user(users, "bob", "Bob B")
        headers = {"X-CSRF-Token": csrf}
        assert client.get("/v1/admin/workspaces/nope/members").status_code == 404
        assert (
            client.patch(
                "/v1/admin/workspaces/nope", json={"name": "X"}, headers=headers
            ).status_code
            == 404
        )
        assert (
            client.delete(
                "/v1/admin/workspaces/nope", headers=headers
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/v1/admin/workspaces/nope/members",
                json={"user_id": bob_user_id, "role": "viewer"},
                headers=headers,
            ).status_code
            == 404
        )


def test_validation_errors():
    client, _container, idp, _identity, _users = make_world()
    with client:
        csrf = login(client, idp)
        headers = {"X-CSRF-Token": csrf}
        assert (
            client.post(
                "/v1/admin/workspaces", json={"name": ""}, headers=headers
            ).status_code
            == 400
        )
        workspace_id = create_workspace(client, csrf)
        admin_user_id = current_user_id(client)
        assert (
            client.post(
                f"/v1/admin/workspaces/{workspace_id}/members",
                json={"user_id": admin_user_id, "role": "bogus"},
                headers=headers,
            ).status_code
            == 400
        )


def test_audit_trail_records_workspace_mutations():
    client, _container, idp, identity, users = make_world()
    with client:
        csrf = login(client, idp)
        bob_user_id = seed_user(users, "bob", "Bob B")
        admin_user_id = current_user_id(client)
        workspace_id = create_workspace(client, csrf)
        client.post(
            f"/v1/admin/workspaces/{workspace_id}/members",
            json={"user_id": bob_user_id, "role": "editor"},
            headers={"X-CSRF-Token": csrf},
        )
        client.delete(
            f"/v1/admin/workspaces/{workspace_id}/members/{bob_user_id}",
            headers={"X-CSRF-Token": csrf},
        )
        actions = [entry.action for entry in identity.audit_entries]
        assert "workspace.created" in actions
        assert "workspace.member_added" in actions
        assert "workspace.member_removed" in actions
        # Actor is the admin session.
        assert all(
            entry.actor_user_id == uuid.UUID(admin_user_id)
            for entry in identity.audit_entries
            if entry.action.startswith("workspace.")
        )


def test_workspace_mutation_invalidates_peer_instance_admin():
    client, container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        peer_admin_id = uuid.UUID(seed_user(users, "peer-admin", "Peer Admin"))
        asyncio.run(
            users.set_instance_role(
                tenant_id="default", user_id=peer_admin_id, role="admin"
            )
        )
        workspace_id = create_workspace(client, csrf)
        events = container.user_event_store
        cursor = asyncio.run(events.current_cursor(tenant_id="default"))

        renamed = client.patch(
            f"/v1/admin/workspaces/{workspace_id}",
            json={"name": "Renamed"},
            headers={"X-CSRF-Token": csrf},
        )

        assert renamed.status_code == 200
        page = asyncio.run(
            events.page_after(
                tenant_id="default",
                target_user_id=peer_admin_id,
                cursor=cursor,
            )
        )
        assert any(
            event.scope == "workspaces"
            and event.resource_id == workspace_id
            for event in page.events
        )


def test_non_admin_is_denied():
    client, _container, idp, _identity, users = make_world()
    with client:
        csrf = login(client, idp)
        demote(users)  # genuine non-admin (first login auto-promotes)
        assert client.get("/v1/admin/workspaces").status_code == 404
        assert (
            client.post(
                "/v1/admin/workspaces",
                json={"name": "X"},
                headers={"X-CSRF-Token": csrf},
            ).status_code
            == 404
        )


def test_unauthenticated_is_401():
    client, _container, _idp, _identity, _users = make_world()
    with client:
        assert client.get("/v1/admin/workspaces").status_code == 401


def test_concurrent_owner_demotions_cannot_orphan_workspace():
    """The store-level lock serializes two competing last-owner changes."""

    async def exercise():
        store = MemoryIdentityStore()
        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        workspace_id, _ = await store.create_workspace(
            tenant_id="default", name="Team", created_by_user_id=owner_a
        )
        await store.assign_member(
            tenant_id="default",
            workspace_id=workspace_id,
            user_id=owner_b,
            role=WorkspaceRole.OWNER,
        )

        async def demote(user_id: uuid.UUID):
            try:
                return await store.assign_member(
                    tenant_id="default",
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role=WorkspaceRole.VIEWER,
                )
            except LastWorkspaceOwnerError as exc:
                return exc

        outcomes = await asyncio.gather(demote(owner_a), demote(owner_b))
        members = await store.list_members(
            tenant_id="default", workspace_id=workspace_id
        )
        return outcomes, members

    outcomes, members = asyncio.run(exercise())
    assert sum(isinstance(item, LastWorkspaceOwnerError) for item in outcomes) == 1
    assert members is not None
    assert sum(role is WorkspaceRole.OWNER for _user_id, role in members) == 1


def test_concurrent_remove_and_role_patch_cannot_resurrect_member():
    async def exercise():
        store = MemoryIdentityStore()
        owner_id = uuid.uuid4()
        member_id = uuid.uuid4()
        workspace_id, _ = await store.create_workspace(
            tenant_id="default", name="Team", created_by_user_id=owner_id
        )
        await store.assign_member(
            tenant_id="default",
            workspace_id=workspace_id,
            user_id=member_id,
            role=WorkspaceRole.VIEWER,
        )
        await asyncio.gather(
            store.remove_member(
                tenant_id="default",
                workspace_id=workspace_id,
                user_id=member_id,
            ),
            store.set_existing_member_role(
                tenant_id="default",
                workspace_id=workspace_id,
                user_id=member_id,
                role=WorkspaceRole.EDITOR,
            ),
        )
        return await store.list_members(
            tenant_id="default", workspace_id=workspace_id
        )

    members = asyncio.run(exercise())
    assert members is not None
    assert len(members) == 1
    assert members[0][1] is WorkspaceRole.OWNER
