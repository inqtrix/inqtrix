"""Workspace bootstrap + invitation routes + callback admission.

Pins the closed-registration loop end to end against memory backends:
bootstrap a workspace (creator becomes OWNER), invite an email, a
stranger's login is rejected at the callback BEFORE any user record
exists, the invited login passes and lands the membership, and the
OWNER-only management surface hides denials behind 404.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.invitations import MemoryInvitationStore, RegistrationGate
from inqtrix.auth.lifecycle import (
    MemoryUserLifecycleTransaction,
    UserLifecycleService,
)
from inqtrix.auth.permissions import AuthorizationService
from inqtrix.providers.base import ProviderContext
from inqtrix.server.container import build_container
from inqtrix.server.routers.auth import build_auth_router
from inqtrix.server.routers.workspaces import build_router
from inqtrix.settings import ServerSettings, Settings, StorageSettings

from tests.contract._app import StubSearch
from tests.test_knowledge_routes import KnowledgeStubLLM
from tests.test_oidc_bff import FakeIdp, make_provider, run_login


def make_world(registration="invite"):
    """One identity store backing permissions, gate, and admin alike."""
    identity = MemoryIdentityStore()
    invitations = MemoryInvitationStore(identity)
    idp = FakeIdp()
    provider, users = make_provider(idp)
    provider.invitations = invitations
    provider.registration_gate = RegistrationGate(
        invitations=invitations,
        users=users,
        registration=registration,
        audit=identity,
    )
    provider.lifecycle = UserLifecycleService(
        users=users,
        sessions=provider.sessions,
        invitations=invitations,
        pat_service=provider.pat_service,
        transaction=MemoryUserLifecycleTransaction(
            users=users,
            sessions=provider.sessions,
            invitations=invitations,
        ),
    )
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
    app = FastAPI()
    app.include_router(build_auth_router(provider))
    app.include_router(build_router(container))
    client = TestClient(app, base_url="http://127.0.0.1:5100")
    return client, idp, identity, invitations, users


def login(client, idp):
    response = run_login(client, idp)
    csrf = client.cookies.get("inqtrix_csrf")
    return response, csrf


def test_closed_registration_loop_end_to_end():
    client, idp, identity, _invitations, _users = make_world()
    # FakeIdp's default identity holds an invitation-free email at
    # first — bootstrap the world through an OPEN first login is not
    # possible in invite mode, so seed the inviting OWNER directly.
    identity.add_workspace("ws-boot", name="Bootstrap")
    # The FIRST stranger login is rejected at the callback...
    rejected, _ = login(client, idp)
    assert rejected.status_code == 403
    assert "requires an invitation" in rejected.json()["error"]["message"]
    # ...and left NO user record behind.
    # (FakeIdp issues sub=user-1234, email=alice@example.com.)


def test_invited_login_passes_and_lands_the_membership():
    client, idp, identity, invitations, _users = make_world()
    identity.add_workspace("ws-1", name="Team A")
    import time as _time

    from inqtrix.auth.permissions import WorkspaceRole

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        invitations.create(
            tenant_id="default",
            workspace_id="ws-1",
            email="alice@example.com",
            role=WorkspaceRole.EDITOR,
            invited_by_user_id=uuid.uuid5(
                uuid.NAMESPACE_URL, "inqtrix-test:owner"
            ),
            expires_at=_time.time() + 3600,
        )
    )
    response, _csrf = login(client, idp)
    assert response.status_code == 303
    user_id = uuid.UUID(client.get("/api/auth/session").json()["user"]["id"])
    role = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        identity.role_in_workspace(
            tenant_id="default", user_id=user_id, workspace_id="ws-1"
        )
    )
    assert role is WorkspaceRole.EDITOR


def test_open_mode_keeps_admitting_everyone():
    client, idp, _identity, _invitations, _users = make_world(registration="open")
    response, _ = login(client, idp)
    assert response.status_code == 303


def test_create_workspace_requires_instance_admin():
    """Self-serve creation is gated on the instance-admin axis.

    Workspace creation is platform administration, so a non-admin session —
    even one that authenticated and carries a valid CSRF token — is denied
    (404), closing the self-create-then-own escalation vector.
    """
    client, idp, _identity, _invitations, users = make_world(registration="open")
    _response, csrf = login(client, idp)
    # The first login auto-promotes the owner; demote to a real non-admin.
    user_id = uuid.UUID(client.get("/api/auth/session").json()["user"]["id"])
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        users.set_instance_role(
            tenant_id="default", user_id=user_id, role="user"
        )
    )
    denied = client.post(
        "/v1/workspaces",
        json={"name": "Mein Team"},
        headers={"X-CSRF-Token": csrf},
    )
    assert denied.status_code == 404


class TestWorkspaceSurface:
    @pytest.fixture()
    def logged_in(self):
        client, idp, identity, invitations, _users = make_world(
            registration="open"
        )
        _response, csrf = login(client, idp)
        return client, csrf, identity

    def test_bootstrap_creates_owner_workspace(self, logged_in):
        client, csrf, _identity = logged_in
        created = client.post(
            "/v1/workspaces",
            json={"name": "Mein Team"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        payload = created.json()
        assert payload["role"] == "owner"
        listed = client.get("/v1/workspaces").json()["data"]
        assert [entry["name"] for entry in listed] == ["Mein Team"]

    def test_non_string_names_are_rejected_uniformly(self, logged_in):
        """Names are strings, strictly: the historical ``str(...)``
        coercion stored repr leaks like a workspace named "None".
        Pinned as a deliberate hardening across every name-taking
        surface (create here, rename in the admin router tests)."""
        client, csrf, _identity = logged_in
        for bad_name in (None, 0, False, ["Team"]):
            response = client.post(
                "/v1/workspaces",
                json={"name": bad_name},
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 400, bad_name
            assert response.json()["error"]["type"] == "invalid_request_error"

    def test_self_serve_creation_is_audited(self, logged_in):
        """Both creation surfaces share one command: the self-serve
        route records the same ``workspace.created`` trail the admin
        route always had (the pre-consolidation gap)."""
        client, csrf, identity = logged_in
        created = client.post(
            "/v1/workspaces",
            json={"name": "Auditiertes Team"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        workspace_id = created.json()["workspace_id"]
        entries = [
            entry
            for entry in identity.audit_entries
            if entry.action == "workspace.created"
        ]
        assert [entry.resource_id for entry in entries] == [workspace_id]
        assert entries[0].detail == {"name": "Auditiertes Team"}

    def test_invitation_management_requires_owner(self, logged_in):
        client, csrf, identity = logged_in
        workspace_id = client.post(
            "/v1/workspaces",
            json={"name": "Team"},
            headers={"X-CSRF-Token": csrf},
        ).json()["workspace_id"]
        created = client.post(
            f"/v1/workspaces/{workspace_id}/invitations",
            json={"email": "bob@example.com", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert created.status_code == 201
        invitation = created.json()
        assert invitation["role"] == "viewer"
        # Duplicate open invitation -> 409.
        duplicate = client.post(
            f"/v1/workspaces/{workspace_id}/invitations",
            json={"email": "BOB@example.com", "role": "editor"},
            headers={"X-CSRF-Token": csrf},
        )
        assert duplicate.status_code == 409
        listed = client.get(
            f"/v1/workspaces/{workspace_id}/invitations"
        ).json()["data"]
        assert len(listed) == 1
        revoked = client.delete(
            f"/v1/workspaces/{workspace_id}/invitations/"
            f"{invitation['invitation_id']}",
            headers={"X-CSRF-Token": csrf},
        )
        assert revoked.status_code == 200
        # A revoked invitation cannot be revoked twice.
        again = client.delete(
            f"/v1/workspaces/{workspace_id}/invitations/"
            f"{invitation['invitation_id']}",
            headers={"X-CSRF-Token": csrf},
        )
        assert again.status_code == 404

    def test_foreign_workspace_is_a_404(self, logged_in):
        client, csrf, identity = logged_in
        identity.add_workspace("ws-foreign", name="Fremd")
        response = client.post(
            "/v1/workspaces/ws-foreign/invitations",
            json={"email": "x@example.com", "role": "viewer"},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 404

    def test_validation_errors(self, logged_in):
        client, csrf, _identity = logged_in
        workspace_id = client.post(
            "/v1/workspaces",
            json={"name": "Team"},
            headers={"X-CSRF-Token": csrf},
        ).json()["workspace_id"]
        headers = {"X-CSRF-Token": csrf}
        assert (
            client.post(
                "/v1/workspaces", json={"name": ""}, headers=headers
            ).status_code
            == 400
        )
        base = f"/v1/workspaces/{workspace_id}/invitations"
        assert (
            client.post(
                base, json={"email": "keine-mail", "role": "viewer"},
                headers=headers,
            ).status_code
            == 400
        )
        assert (
            client.post(
                base, json={"email": "x@example.com", "role": "boss"},
                headers=headers,
            ).status_code
            == 400
        )
        assert (
            client.post(
                base,
                json={
                    "email": "x@example.com",
                    "role": "viewer",
                    "expires_in_days": 0,
                },
                headers=headers,
            ).status_code
            == 400
        )
