"""Invitation domain tests: admission policy and one-time acceptance.

Pins the admission contracts: open mode keeps the historical
admit-everyone behaviour, invite mode rejects unknown users with the
generic denial, acceptance consumes invitations exactly once and
creates memberships without downgrading existing ones, and disabled
users are denied in every mode.
"""

from __future__ import annotations

import time
import uuid

import pytest

from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.invitations import (
    DuplicateOpenInvitation,
    MemoryInvitationStore,
    RegistrationDenied,
    RegistrationGate,
)
from inqtrix.auth.permissions import WorkspaceRole

ISSUER = "http://idp.example"
OWNER_ID = uuid.uuid5(uuid.NAMESPACE_URL, "inqtrix-test:owner")


def make_world(registration="invite"):
    identity = MemoryIdentityStore()
    identity.add_workspace(workspace_id="ws-1", name="Team A")
    identity.add_workspace(workspace_id="ws-2", name="Team B")
    invitations = MemoryInvitationStore(identity)
    users = MemoryUserDirectory()
    gate = RegistrationGate(
        invitations=invitations,
        users=users,
        registration=registration,
    )
    return identity, invitations, users, gate


async def invite(invitations, email="alice@example.com", workspace="ws-1",
                 role=WorkspaceRole.EDITOR, ttl=3600.0):
    return await invitations.create(
        tenant_id="default",
        workspace_id=workspace,
        email=email,
        role=role,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() + ttl,
    )


async def admit(gate, users, *, sub="user-1", email="alice@example.com"):
    await gate.admit(
        tenant_id="default", issuer=ISSUER, sub=sub, email=email
    )
    user = await users.record_login(
        tenant_id="default",
        issuer=ISSUER,
        subject=sub,
        email=email,
        email_verified=True,
        display_name=email,
    )
    return await gate.accept(
        tenant_id="default", email=email, user_id=user.user_id
    )


@pytest.mark.asyncio
async def test_invite_mode_rejects_unknown_users_generically():
    _identity, _invitations, _users, gate = make_world()
    with pytest.raises(RegistrationDenied) as excinfo:
        await admit(gate, _users, email="stranger@example.com")
    # The denial must not confirm whether an invitation exists.
    assert str(excinfo.value) == (
        "Registration requires an invitation. Contact the administrator."
    )
    assert "stranger" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_acceptance_admits_and_creates_the_membership():
    identity, invitations, users, gate = make_world()
    await invite(invitations)
    accepted = await admit(gate, users)
    assert len(accepted) == 1
    role = await identity.role_in_workspace(
        tenant_id="default",
        user_id=accepted[0].accepted_by_user_id,
        workspace_id="ws-1",
    )
    assert role is WorkspaceRole.EDITOR


@pytest.mark.asyncio
async def test_acceptance_is_one_time():
    _identity, invitations, users, gate = make_world()
    await invite(invitations)
    first = await admit(gate, users)
    assert len(first) == 1
    # The user now exists in the mirror (callback records the login);
    # the second login passes WITHOUT consuming anything again.
    await users.record_login(
        tenant_id="default",
        issuer=ISSUER,
        subject="user-1",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice",
    )
    second = await admit(gate, users)
    assert second == ()


@pytest.mark.asyncio
async def test_case_insensitive_email_matching():
    _identity, invitations, users, gate = make_world()
    await invite(invitations, email="Alice@Example.com")
    accepted = await admit(gate, users, email="alice@example.COM")
    assert len(accepted) == 1


@pytest.mark.asyncio
async def test_multiple_workspaces_accept_in_one_login():
    identity, invitations, users, gate = make_world()
    await invite(invitations, workspace="ws-1", role=WorkspaceRole.VIEWER)
    await invite(invitations, workspace="ws-2", role=WorkspaceRole.OWNER)
    accepted = await admit(gate, users)
    assert len(accepted) == 2
    assert await identity.role_in_workspace(
        tenant_id="default",
        user_id=accepted[0].accepted_by_user_id,
        workspace_id="ws-2",
    ) is WorkspaceRole.OWNER


@pytest.mark.asyncio
async def test_expired_and_revoked_invitations_never_match():
    _identity, invitations, _users, gate = make_world()
    await invite(invitations, ttl=-1.0)
    revocable = await invite(
        invitations, email="bob@example.com", workspace="ws-2"
    )
    assert await invitations.revoke(
        tenant_id="default",
        workspace_id="ws-2",
        invitation_id=revocable.id,
        now=time.time(),
    )
    with pytest.raises(RegistrationDenied):
        await admit(gate, _users)
    with pytest.raises(RegistrationDenied):
        await admit(gate, _users, sub="user-2", email="bob@example.com")


@pytest.mark.asyncio
async def test_existing_users_pass_and_still_collect_new_invitations():
    identity, invitations, users, gate = make_world()
    existing = await users.record_login(
        tenant_id="default",
        issuer=ISSUER,
        subject="user-1",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice",
    )
    # No invitation: existing user passes anyway.
    assert await admit(gate, users) == ()
    # Invited AFTER registration: next login grants the membership.
    await invite(invitations, workspace="ws-2", role=WorkspaceRole.VIEWER)
    accepted = await admit(gate, users)
    assert len(accepted) == 1
    assert await identity.role_in_workspace(
        tenant_id="default", user_id=existing.user_id, workspace_id="ws-2"
    ) is WorkspaceRole.VIEWER


@pytest.mark.asyncio
async def test_existing_membership_is_never_downgraded():
    identity, invitations, users, gate = make_world()
    existing = await users.record_login(
        tenant_id="default",
        issuer=ISSUER,
        subject="user-1",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice",
    )
    identity.add_member("ws-1", existing.user_id, WorkspaceRole.OWNER)
    await invite(invitations, role=WorkspaceRole.VIEWER)
    await admit(gate, users)
    assert await identity.role_in_workspace(
        tenant_id="default", user_id=existing.user_id, workspace_id="ws-1"
    ) is WorkspaceRole.OWNER


@pytest.mark.asyncio
async def test_missing_email_with_unknown_user_is_denied_in_invite_mode():
    _identity, _invitations, _users, gate = make_world()
    with pytest.raises(RegistrationDenied):
        await admit(gate, _users, email="")


@pytest.mark.asyncio
async def test_open_mode_admits_everyone():
    _identity, _invitations, _users, gate = make_world(registration="open")
    assert await admit(gate, _users, email="anyone@example.com") == ()


@pytest.mark.asyncio
async def test_disabled_users_are_denied_in_every_mode():
    import dataclasses

    for registration in ("open", "invite"):
        _identity, _invitations, users, gate = make_world(registration)
        await users.record_login(
            tenant_id="default",
            issuer=ISSUER,
            subject="user-1",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
        )
        users.users[("default", ISSUER, "user-1")] = dataclasses.replace(
            users.users[("default", ISSUER, "user-1")], disabled_at=time.time()
        )
        with pytest.raises(RegistrationDenied, match="Account is disabled"):
            await admit(gate, users)


@pytest.mark.asyncio
async def test_duplicate_open_invitation_is_rejected():
    _identity, invitations, _users, _gate = make_world()
    await invite(invitations)
    with pytest.raises(DuplicateOpenInvitation):
        await invite(invitations, email="ALICE@example.com")
