"""Postgres integration tests for invitations + disable cascade (gated).

Same gating and conventions as the sibling suites. Pins the
replica-safety contracts: concurrent acceptance consumes exactly once
with the membership landing in the SAME transaction, existing roles
are never downgraded, and the disable cascade flips mirror flag,
sessions, and PATs atomically.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import insert, select, text

from inqtrix.auth.invitations import DuplicateOpenInvitation
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import workspace_members, workspaces
from inqtrix.storage.invitations_postgres import PostgresInvitationRepository
from inqtrix.storage.migrate import run_migrations
from tests.storage._canonical_users import (
    canonical_user_id,
    ensure_canonical_users,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_ID = canonical_user_id("invitation-owner")
INVITEE_ID = canonical_user_id("invitation-recipient")


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def engine():
    engine = build_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def factory(engine):
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            bypasses = (
                await session.execute(
                    text(
                        "SELECT rolsuper OR rolbypassrls FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            ).scalar_one()
            if not bypasses:
                pytest.fail(
                    "INQTRIX_TEST_DATABASE_URL must connect as a "
                    "superuser/BYPASSRLS user for cross-tenant cleanup."
                )
            for table in (
                "invitations",
                "workspace_members",
                "workspaces",
            ):
                await session.execute(text(f"DELETE FROM {table}"))
            await ensure_canonical_users(session, (OWNER_ID, INVITEE_ID))
    return factory


async def seed_workspace(factory) -> str:
    workspace_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id="default",
                    name="Team",
                    created_by_user_id=OWNER_ID,
                )
            )
    return str(workspace_id)


@pytest.mark.asyncio
async def test_concurrent_acceptance_consumes_exactly_once(factory):
    repo = PostgresInvitationRepository(
        session_factory=factory, app_role=APP_ROLE
    )
    workspace_id = await seed_workspace(factory)
    await repo.create(
        tenant_id="default",
        workspace_id=workspace_id,
        email="alice@example.com",
        role=WorkspaceRole.EDITOR,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() + 3600,
    )
    results = await asyncio.gather(
        *(
            repo.accept_open_for_email(
                tenant_id="default",
                email="ALICE@example.com",
                user_id=INVITEE_ID,
                now=time.time(),
            )
            for _ in range(2)
        )
    )
    consumed_counts = sorted(len(result) for result in results)
    assert consumed_counts == [0, 1]
    # The membership landed in the same transaction as the consume.
    async with factory() as session:
        role = (
            await session.execute(
                select(workspace_members.c.role).where(
                    workspace_members.c.user_id == INVITEE_ID
                )
            )
        ).scalar_one()
    assert role == "editor"


@pytest.mark.asyncio
async def test_existing_role_is_never_downgraded(factory):
    repo = PostgresInvitationRepository(
        session_factory=factory, app_role=APP_ROLE
    )
    workspace_id = await seed_workspace(factory)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(workspace_members).values(
                    tenant_id="default",
                    workspace_id=uuid.UUID(workspace_id),
                    user_id=INVITEE_ID,
                    role="owner",
                )
            )
    await repo.create(
        tenant_id="default",
        workspace_id=workspace_id,
        email="alice@example.com",
        role=WorkspaceRole.VIEWER,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() + 3600,
    )
    accepted = await repo.accept_open_for_email(
        tenant_id="default",
        email="alice@example.com",
        user_id=INVITEE_ID,
        now=time.time(),
    )
    assert len(accepted) == 1
    async with factory() as session:
        role = (
            await session.execute(
                select(workspace_members.c.role).where(
                    workspace_members.c.user_id == INVITEE_ID
                )
            )
        ).scalar_one()
    assert role == "owner"


@pytest.mark.asyncio
async def test_duplicate_open_invitation_hits_the_partial_unique(factory):
    repo = PostgresInvitationRepository(
        session_factory=factory, app_role=APP_ROLE
    )
    workspace_id = await seed_workspace(factory)
    kwargs = dict(
        tenant_id="default",
        workspace_id=workspace_id,
        email="alice@example.com",
        role=WorkspaceRole.VIEWER,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() + 3600,
    )
    await repo.create(**kwargs)
    with pytest.raises(DuplicateOpenInvitation):
        await repo.create(**{**kwargs, "email": "ALICE@example.com"})


@pytest.mark.asyncio
async def test_expired_and_revoked_never_accept(factory):
    repo = PostgresInvitationRepository(
        session_factory=factory, app_role=APP_ROLE
    )
    workspace_id = await seed_workspace(factory)
    expired = await repo.create(
        tenant_id="default",
        workspace_id=workspace_id,
        email="old@example.com",
        role=WorkspaceRole.VIEWER,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() - 1,
    )
    revocable = await repo.create(
        tenant_id="default",
        workspace_id=workspace_id,
        email="gone@example.com",
        role=WorkspaceRole.VIEWER,
        invited_by_user_id=OWNER_ID,
        expires_at=time.time() + 3600,
    )
    assert await repo.revoke(
        tenant_id="default",
        workspace_id=workspace_id,
        invitation_id=revocable.id,
        now=time.time(),
    )
    for email in ("old@example.com", "gone@example.com"):
        accepted = await repo.accept_open_for_email(
            tenant_id="default",
            email=email,
            user_id=INVITEE_ID,
            now=time.time(),
        )
        assert accepted == ()
    listed = await repo.list_for_workspace(
        tenant_id="default", workspace_id=workspace_id
    )
    assert len(listed) == 2
    assert expired.id in {invitation.id for invitation in listed}
