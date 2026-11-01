"""User lifecycle in Postgres: login profile, role audit, invitations and
the admins of one identity source.

Gated on ``INQTRIX_TEST_DATABASE_URL`` like the sibling suites; the memory
twins live in ``tests/test_user_lifecycle.py``. Every test owns a fresh
tenant, so admins that other suites leave in the shared database never take
part in a count here, and the fixture removes the tenant's rows afterwards.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from inqtrix.auth.invitations import CONFIRMED_EMAIL_REQUIRED, RegistrationDenied
from inqtrix.auth.lifecycle import (
    AdminActor,
    AdminAuthorizationError,
    ExternalProfile,
    LifecycleActor,
    LoginCommand,
    ProvisioningActor,
    UserDisabledError,
    UserLifecycleStatus,
    UserNotProvisionedError,
)
from inqtrix.auth.permissions import WorkspaceRole
from inqtrix.auth.sessions import AuthSession
from inqtrix.storage.auth_orm import auth_sessions
from inqtrix.storage.auth_postgres import PostgresUserDirectory
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_orm import (
    audit_log,
    invitations,
    tenant_security_state,
    users,
    workspace_members,
    workspaces,
)
from inqtrix.storage.invitations_postgres import PostgresInvitationRepository
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.pat_orm import personal_access_tokens
from inqtrix.storage.user_event_orm import user_events
from inqtrix.storage.user_lifecycle_postgres import (
    PostgresUserLifecycleTransaction,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
ISSUER = "https://idp.login-provisioning.example"
GATEWAY_ISSUER = "gateway:corp-sso"
_TENANT_TABLES = (
    auth_sessions,
    personal_access_tokens,
    user_events,
    audit_log,
    workspace_members,
    invitations,
    workspaces,
    users,
    tenant_security_state,
)


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def factory():
    engine = build_engine(TEST_DATABASE_URL)
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
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture()
async def tenant(factory) -> AsyncIterator[str]:
    tenant_id = f"t-login-{uuid.uuid4().hex[:12]}"
    yield tenant_id
    async with factory() as session:
        async with session.begin():
            for table in _TENANT_TABLES:
                await session.execute(
                    table.delete().where(table.c.tenant_id == tenant_id)
                )


def _lifecycle(
    factory, source_issuer: str | None = None
) -> PostgresUserLifecycleTransaction:
    return PostgresUserLifecycleTransaction(
        session_factory=factory, app_role=APP_ROLE, source_issuer=source_issuer
    )


def _login(
    tenant_id: str,
    subject: str,
    *,
    issuer: str = ISSUER,
    email: str = "",
    email_verified: bool = False,
    display_name: str | None = None,
    **flags: bool,
) -> LoginCommand:
    now = time.time()
    session = AuthSession(
        id=f"session-{uuid.uuid4()}",
        user_id=uuid.uuid4(),
        issuer=issuer,
        subject=subject,
        email=email or None,
        display_name=display_name,
        groups=(),
        csrf_random=uuid.uuid4().hex,
        created_at=now,
        expires_at=now + 3600,
    )
    return LoginCommand(
        tenant_id=tenant_id,
        issuer=issuer,
        subject=subject,
        email=email,
        email_verified=email_verified,
        display_name=display_name,
        session=session,
        **flags,
    )


async def _seed_user(
    factory,
    tenant_id: str,
    subject: str,
    *,
    email: str,
    email_verified: bool,
    display_name: str | None,
    issuer: str = ISSUER,
    instance_role: str = "user",
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(users).values(
                    id=user_id,
                    tenant_id=tenant_id,
                    issuer=issuer,
                    subject=subject,
                    email=email,
                    email_verified=email_verified,
                    display_name=display_name,
                    instance_role=instance_role,
                )
            )
    return user_id


async def _seed_admin(factory, tenant_id: str, subject: str, issuer: str) -> uuid.UUID:
    return await _seed_user(
        factory,
        tenant_id,
        subject,
        email=f"{subject}@example.com",
        email_verified=True,
        display_name=subject,
        issuer=issuer,
        instance_role="admin",
    )


async def _stored_session(factory, session_id: str):
    async with factory() as session:
        return (
            await session.execute(
                select(auth_sessions.c.email, auth_sessions.c.display_name).where(
                    auth_sessions.c.id == session_id
                )
            )
        ).first()


async def _role_updates(factory, tenant_id: str, user_id: uuid.UUID) -> list:
    async with factory() as session:
        rows = (
            await session.execute(
                select(audit_log.c.detail)
                .where(
                    audit_log.c.tenant_id == tenant_id,
                    audit_log.c.action == "user.role_updated",
                    audit_log.c.resource_id == str(user_id),
                )
                .order_by(audit_log.c.id)
            )
        ).scalars()
        return [dict(detail or {}) for detail in rows]


@pytest.mark.asyncio
async def test_login_without_email_or_name_keeps_the_stored_profile(
    factory, tenant
):
    await _seed_user(
        factory,
        tenant,
        "alice",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice Example",
    )
    command = _login(tenant, "alice")

    user = await _lifecycle(factory).provision_login(command)

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@example.com",
        True,
        "Alice Example",
    )
    stored = await _stored_session(factory, command.session.id)
    assert stored is not None
    assert (stored.email, stored.display_name) == (
        "alice@example.com",
        "Alice Example",
    )


@pytest.mark.asyncio
async def test_login_with_an_email_but_no_name_keeps_the_stored_name(
    factory, tenant
):
    await _seed_user(
        factory,
        tenant,
        "alice",
        email="alice@example.com",
        email_verified=True,
        display_name="Alice Example",
    )

    user = await _lifecycle(factory).provision_login(
        _login(tenant, "alice", email="alice@new.example")
    )

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@new.example",
        False,
        "Alice Example",
    )


@pytest.mark.asyncio
async def test_login_values_overwrite_the_stored_profile(factory, tenant):
    await _seed_user(
        factory,
        tenant,
        "alice",
        email="alice@example.com",
        email_verified=False,
        display_name="Alice Example",
    )

    user = await _lifecycle(factory).provision_login(
        _login(
            tenant,
            "alice",
            email="alice@new.example",
            email_verified=True,
            display_name="Alice New",
        )
    )

    assert (user.email, user.email_verified, user.display_name) == (
        "alice@new.example",
        True,
        "Alice New",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("email", "expected"),
    [("bob@example.com", "bob@example.com"), ("", "bob")],
    ids=["email", "subject"],
)
async def test_a_missing_name_falls_back_to_the_email_then_the_subject(
    factory, tenant, email: str, expected: str
):
    command = _login(tenant, "bob", email=email, email_verified=bool(email))

    user = await _lifecycle(factory).provision_login(command)

    assert user.display_name == expected
    stored = await _stored_session(factory, command.session.id)
    assert stored is not None and stored.display_name == expected


@pytest.mark.asyncio
async def test_the_name_fallback_fills_only_an_empty_stored_name(
    factory, tenant
):
    await _seed_user(
        factory,
        tenant,
        "carol",
        email="carol@example.com",
        email_verified=True,
        display_name=None,
    )

    user = await _lifecycle(factory).provision_login(_login(tenant, "carol"))

    assert user.display_name == "carol@example.com"


@pytest.mark.asyncio
async def test_a_login_upserts_on_the_issuer_subject_anchor(factory, tenant):
    lifecycle = _lifecycle(factory)
    other_issuer = "https://other-idp.example"
    await lifecycle.provision_login(
        _login(tenant, "user-1", email="old@example.com", display_name="Old Name")
    )
    # Same anchor, new profile data: the row is updated, not duplicated.
    await lifecycle.provision_login(
        _login(
            tenant,
            "user-1",
            email="new@example.com",
            email_verified=True,
            display_name="New Name",
        )
    )
    # Same subject under a different issuer: a separate identity.
    await lifecycle.provision_login(
        _login(
            tenant,
            "user-1",
            issuer=other_issuer,
            email="other@example.com",
            email_verified=True,
            display_name="Other",
        )
    )

    async with factory() as session:
        rows = (
            await session.execute(
                select(
                    users.c.issuer,
                    users.c.email,
                    users.c.display_name,
                    users.c.last_login_at,
                ).where(users.c.tenant_id == tenant, users.c.subject == "user-1")
            )
        ).all()
    by_issuer = {row.issuer: row for row in rows}
    assert set(by_issuer) == {ISSUER, other_issuer}
    assert (by_issuer[ISSUER].email, by_issuer[ISSUER].display_name) == (
        "new@example.com",
        "New Name",
    )
    assert by_issuer[ISSUER].last_login_at is not None


@pytest.mark.asyncio
async def test_a_role_granted_by_a_login_claim_is_audited_once(factory, tenant):
    lifecycle = _lifecycle(factory)

    first = await lifecycle.provision_login(
        _login(tenant, "dana", is_admin=True)
    )
    await lifecycle.provision_login(_login(tenant, "dana", is_admin=True))

    assert first.instance_role == "admin"
    assert await _role_updates(factory, tenant, first.user_id) == [
        {"instance_role": "admin", "source": "login_claim"}
    ]


@pytest.mark.asyncio
async def test_a_first_owner_grant_is_audited_with_its_source(factory, tenant):
    lifecycle = _lifecycle(factory)

    owner = await lifecycle.provision_login(
        _login(tenant, "erin", first_login_owner=True)
    )
    second = await lifecycle.provision_login(
        _login(tenant, "finn", first_login_owner=True)
    )

    assert owner.instance_role == "admin"
    assert await _role_updates(factory, tenant, owner.user_id) == [
        {"instance_role": "admin", "source": "first_owner"}
    ]
    assert second.instance_role == "user"
    assert await _role_updates(factory, tenant, second.user_id) == []


@pytest.mark.asyncio
async def test_a_login_regrants_admin_after_a_demotion_and_audits_it(
    factory, tenant
):
    lifecycle = _lifecycle(factory)
    dana = await lifecycle.provision_login(_login(tenant, "dana", is_admin=True))
    erin = await lifecycle.provision_login(_login(tenant, "erin", is_admin=True))
    await lifecycle.set_role(
        tenant_id=tenant,
        user_id=dana.user_id,
        role="user",
        actor_user_id=erin.user_id,
    )

    again = await lifecycle.provision_login(_login(tenant, "dana", is_admin=True))

    assert again.instance_role == "admin"
    assert [
        detail.get("source")
        for detail in await _role_updates(factory, tenant, dana.user_id)
    ] == ["login_claim", None, "login_claim"]


@pytest.mark.asyncio
async def test_a_login_without_a_role_change_writes_no_role_audit(
    factory, tenant
):
    user = await _lifecycle(factory).provision_login(_login(tenant, "gus"))

    assert user.instance_role == "user"
    assert await _role_updates(factory, tenant, user.user_id) == []


async def _invite(factory, tenant_id: str) -> str:
    owner_id = await _seed_user(
        factory,
        tenant_id,
        "owner",
        email="owner@example.com",
        email_verified=True,
        display_name="Owner",
    )
    workspace_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id=tenant_id,
                    name="Team",
                    created_by_user_id=owner_id,
                )
            )
    await PostgresInvitationRepository(
        session_factory=factory, app_role=APP_ROLE
    ).create(
        tenant_id=tenant_id,
        workspace_id=str(workspace_id),
        email="alice@example.com",
        role=WorkspaceRole.EDITOR,
        invited_by_user_id=owner_id,
        expires_at=time.time() + 3600,
    )
    return str(workspace_id)


async def _open_invitations(factory, tenant_id: str) -> int:
    async with factory() as session:
        return len(
            (
                await session.execute(
                    select(invitations.c.id).where(
                        invitations.c.tenant_id == tenant_id,
                        invitations.c.accepted_at.is_(None),
                    )
                )
            ).all()
        )


@pytest.mark.asyncio
async def test_an_unverified_email_never_accepts_an_invitation(factory, tenant):
    await _invite(factory, tenant)

    user = await _lifecycle(factory).provision_login(
        _login(tenant, "alice", email="alice@example.com")
    )

    async with factory() as session:
        membership = (
            await session.execute(
                select(workspace_members.c.role).where(
                    workspace_members.c.tenant_id == tenant,
                    workspace_members.c.user_id == user.user_id,
                )
            )
        ).first()
    assert membership is None
    assert await _open_invitations(factory, tenant) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invited", [True, False], ids=["invited", "not-invited"])
async def test_invite_only_registration_needs_a_verified_email(factory, tenant, invited):
    if invited:
        await _invite(factory, tenant)
    command = _login(
        tenant, "alice", email="alice@example.com", invitation_required=True
    )

    with pytest.raises(RegistrationDenied) as excinfo:
        await _lifecycle(factory).provision_login(command)

    assert (excinfo.value.reason, str(excinfo.value)) == (
        "email_unverified",
        CONFIRMED_EMAIL_REQUIRED,
    )

    async with factory() as session:
        created = (
            await session.execute(
                select(users.c.id).where(
                    users.c.tenant_id == tenant, users.c.subject == "alice"
                )
            )
        ).first()
    assert created is None
    assert await _stored_session(factory, command.session.id) is None
    assert await _open_invitations(factory, tenant) == (1 if invited else 0)


# The admins of one identity source. In the gateway mode an account of
# another issuer (a local admin from before the switch) can never sign in;
# the first-owner rule and the last-admin guards count only admins of the
# source, while administrative wake-ups still reach every admin.


async def _role(factory, user_id: uuid.UUID) -> str:
    async with factory() as session:
        return (
            await session.execute(
                select(users.c.instance_role).where(users.c.id == user_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_first_owner_counts_only_admins_of_the_identity_source(
    factory, tenant
):
    await _seed_admin(factory, tenant, "legacy-admin", "local")
    lifecycle = _lifecycle(factory, source_issuer=GATEWAY_ISSUER)

    first = await lifecycle.provision_login(
        _login(tenant, "gw-1", issuer=GATEWAY_ISSUER, first_login_owner=True)
    )
    second = await lifecycle.provision_login(
        _login(tenant, "gw-2", issuer=GATEWAY_ISSUER, first_login_owner=True)
    )

    assert first.instance_role == "admin"
    assert second.instance_role == "user"


@pytest.mark.asyncio
async def test_without_a_source_every_admin_counts_for_the_first_owner(
    factory, tenant
):
    await _seed_admin(factory, tenant, "legacy-admin", "local")

    first = await _lifecycle(factory).provision_login(
        _login(tenant, "oidc-1", first_login_owner=True)
    )

    assert first.instance_role == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["demote", "disable", "erase"])
async def test_the_last_admin_of_the_source_is_kept_beside_another_issuer(
    factory, tenant, command: str
):
    await _seed_admin(factory, tenant, "legacy-admin", "local")
    gateway = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, source_issuer=GATEWAY_ISSUER)

    if command == "demote":
        status = await lifecycle.set_role(
            tenant_id=tenant, user_id=gateway, role="user", actor_user_id=gateway
        )
    elif command == "disable":
        status = await lifecycle.set_disabled(
            tenant_id=tenant,
            user_id=gateway,
            disabled_at=time.time(),
            actor=AdminActor(gateway),
        )
    else:
        status, _report = await lifecycle.begin_erasure(
            tenant_id=tenant,
            user_id=gateway,
            actor=AdminActor(gateway),
            operation_id=f"del_{uuid.uuid4().hex}",
        )

    assert status is UserLifecycleStatus.LAST_ADMIN
    assert await _role(factory, gateway) == "admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["demote", "disable", "erase"])
async def test_an_admin_of_another_issuer_is_not_kept_as_the_last_admin(
    factory, tenant, command: str
):
    legacy = await _seed_admin(factory, tenant, "legacy-admin", "local")
    gateway = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, source_issuer=GATEWAY_ISSUER)

    if command == "demote":
        status = await lifecycle.set_role(
            tenant_id=tenant, user_id=legacy, role="user", actor_user_id=gateway
        )
    elif command == "disable":
        status = await lifecycle.set_disabled(
            tenant_id=tenant,
            user_id=legacy,
            disabled_at=time.time(),
            actor=AdminActor(gateway),
        )
    else:
        status, _report = await lifecycle.begin_erasure(
            tenant_id=tenant,
            user_id=legacy,
            actor=AdminActor(gateway),
            operation_id=f"del_{uuid.uuid4().hex}",
        )

    assert status is UserLifecycleStatus.UPDATED


@pytest.mark.asyncio
async def test_disabling_cuts_off_sessions_and_tokens(factory, tenant):
    lifecycle = _lifecycle(factory)
    actor = await _seed_admin(factory, tenant, "actor", ISSUER)
    alice = await lifecycle.provision_login(
        _login(
            tenant,
            "alice",
            email="alice@example.com",
            email_verified=True,
            display_name="Alice",
        )
    )
    token_id = f"tok-{uuid.uuid4().hex[:12]}"
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(personal_access_tokens).values(
                    token_id=token_id,
                    tenant_id=tenant,
                    owner_user_id=alice.user_id,
                    name="ci",
                    secret_hmac="ab" * 32,
                    created_at=time.time(),
                )
            )

    status = await lifecycle.set_disabled(
        tenant_id=tenant,
        user_id=alice.user_id,
        disabled_at=time.time(),
        actor=AdminActor(actor),
    )

    assert status is UserLifecycleStatus.UPDATED
    async with factory() as session:
        disabled_at = (
            await session.execute(
                select(users.c.disabled_at).where(users.c.id == alice.user_id)
            )
        ).scalar_one()
        live_sessions = (
            await session.execute(
                select(auth_sessions.c.id).where(
                    auth_sessions.c.user_id == alice.user_id
                )
            )
        ).all()
        revoked_at = (
            await session.execute(
                select(personal_access_tokens.c.revoked_at).where(
                    personal_access_tokens.c.token_id == token_id
                )
            )
        ).scalar_one()
    assert disabled_at is not None
    assert live_sessions == []
    assert revoked_at is not None


@pytest.mark.asyncio
async def test_admin_events_wake_admins_of_every_issuer(factory, tenant):
    legacy = await _seed_admin(factory, tenant, "legacy-admin", "local")
    gateway = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    member = await _seed_user(
        factory,
        tenant,
        "gw-member",
        email="gw-member@example.com",
        email_verified=True,
        display_name="Member",
        issuer=GATEWAY_ISSUER,
    )

    await _lifecycle(factory, source_issuer=GATEWAY_ISSUER).set_role(
        tenant_id=tenant, user_id=member, role="admin", actor_user_id=gateway
    )

    async with factory() as session:
        woken = set(
            (
                await session.execute(
                    select(user_events.c.target_user_id).where(
                        user_events.c.tenant_id == tenant,
                        user_events.c.resource_id == str(member),
                    )
                )
            ).scalars()
        )
    assert {legacy, gateway} <= woken


async def _member(
    factory, tenant_id: str, subject: str, issuer: str = GATEWAY_ISSUER
) -> uuid.UUID:
    return await _seed_user(
        factory,
        tenant_id,
        subject,
        email=f"{subject}@example.com",
        email_verified=True,
        display_name=subject,
        issuer=issuer,
    )


async def _set_disabled(
    lifecycle: PostgresUserLifecycleTransaction,
    tenant_id: str,
    user_id: uuid.UUID,
    actor: LifecycleActor,
    *,
    disabled: bool = True,
) -> UserLifecycleStatus:
    return await lifecycle.set_disabled(
        tenant_id=tenant_id,
        user_id=user_id,
        disabled_at=time.time() if disabled else None,
        actor=actor,
    )


async def _disable_state(factory, user_id: uuid.UUID) -> tuple[bool, str | None]:
    async with factory() as session:
        row = (
            await session.execute(
                select(users.c.disabled_at, users.c.disabled_source).where(
                    users.c.id == user_id
                )
            )
        ).one()
    return row.disabled_at is not None, row.disabled_source


async def _lifecycle_audit(factory, tenant_id: str, user_id: uuid.UUID):
    async with factory() as session:
        return (
            await session.execute(
                select(
                    audit_log.c.action,
                    audit_log.c.actor_user_id,
                    audit_log.c.actor_type,
                )
                .where(
                    audit_log.c.tenant_id == tenant_id,
                    audit_log.c.resource_id == str(user_id),
                )
                .order_by(audit_log.c.occurred_at, audit_log.c.id)
            )
        ).all()


@pytest.mark.asyncio
async def test_the_actor_decides_the_source_of_a_disable_and_its_audit(
    factory, tenant
):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    by_admin = await _member(factory, tenant, "alice")
    by_provider = await _member(factory, tenant, "bob")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, tenant, by_admin, AdminActor(admin))
        is UserLifecycleStatus.UPDATED
    )
    assert (
        await _set_disabled(lifecycle, tenant, by_provider, ProvisioningActor())
        is UserLifecycleStatus.UPDATED
    )

    assert await _disable_state(factory, by_admin) == (True, "admin")
    assert await _disable_state(factory, by_provider) == (True, "provisioning")
    assert [tuple(row) for row in await _lifecycle_audit(factory, tenant, by_admin)] == [
        ("user.disabled", admin, "user")
    ]
    assert [
        tuple(row) for row in await _lifecycle_audit(factory, tenant, by_provider)
    ] == [("user.disabled", None, "provisioning")]
    # The directory, which the admin listing reads, carries the source too.
    directory = PostgresUserDirectory(session_factory=factory, app_role=APP_ROLE)
    mirrored = await directory.find_by_user_id(tenant_id=tenant, user_id=by_provider)
    assert mirrored is not None and mirrored.disabled_source == "provisioning"


@pytest.mark.asyncio
async def test_a_disable_from_provisioning_cuts_the_account_off(factory, tenant):
    alice = await _member(factory, tenant, "alice")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    login = _login(tenant, "alice", issuer=GATEWAY_ISSUER)
    # Bind the seeded account to a live browser session first.
    await lifecycle.provision_login(login)

    await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())

    assert await _stored_session(factory, login.session.id) is None


@pytest.mark.asyncio
async def test_provisioning_reaches_only_accounts_of_the_identity_source(
    factory, tenant
):
    local = await _member(factory, tenant, "carol", issuer="local")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, tenant, local, ProvisioningActor())
        is UserLifecycleStatus.NOT_FOUND
    )
    assert await _disable_state(factory, local) == (False, None)
    assert await _lifecycle_audit(factory, tenant, local) == []


@pytest.mark.asyncio
async def test_provisioning_neither_takes_over_nor_lifts_an_administrators_disable(
    factory, tenant
):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _member(factory, tenant, "alice")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin))

    assert (
        await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())
        is UserLifecycleStatus.UPDATED
    )
    assert (
        await _set_disabled(
            lifecycle, tenant, alice, ProvisioningActor(), disabled=False
        )
        is UserLifecycleStatus.KEPT_DISABLED
    )

    assert await _disable_state(factory, alice) == (True, "admin")
    assert [row.action for row in await _lifecycle_audit(factory, tenant, alice)] == [
        "user.disabled",
        "user.scim_enable_ignored",
    ]


@pytest.mark.asyncio
async def test_a_disable_without_a_source_counts_as_an_administrators(
    factory, tenant
):
    alice = await _member(factory, tenant, "alice")
    async with factory() as session:
        async with session.begin():
            await session.execute(
                users.update()
                .where(users.c.id == alice)
                .values(disabled_at=text("now()"))
            )
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    assert (
        await _set_disabled(
            lifecycle, tenant, alice, ProvisioningActor(), disabled=False
        )
        is UserLifecycleStatus.KEPT_DISABLED
    )
    assert await _disable_state(factory, alice) == (True, None)


@pytest.mark.asyncio
async def test_provisioning_lifts_its_own_disable_and_an_administrator_lifts_any(
    factory, tenant
):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _member(factory, tenant, "alice")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())
    assert (
        await _set_disabled(
            lifecycle, tenant, alice, ProvisioningActor(), disabled=False
        )
        is UserLifecycleStatus.UPDATED
    )
    assert await _disable_state(factory, alice) == (False, None)

    await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin), disabled=False)
    assert await _disable_state(factory, alice) == (False, None)

    await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin))
    assert await _disable_state(factory, alice) == (True, "admin")


@pytest.mark.asyncio
async def test_provisioning_never_disables_the_last_admin_of_the_source(
    factory, tenant
):
    await _seed_admin(factory, tenant, "legacy-admin", "local")
    gateway = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    assert (
        await _set_disabled(lifecycle, tenant, gateway, ProvisioningActor())
        is UserLifecycleStatus.LAST_ADMIN
    )
    assert await _disable_state(factory, gateway) == (False, None)


@pytest.mark.asyncio
async def test_provisioning_without_an_identity_source_is_a_wiring_error(
    factory, tenant
):
    alice = await _member(factory, tenant, "alice")
    lifecycle = _lifecycle(factory, None)

    with pytest.raises(RuntimeError, match="identity source"):
        await _set_disabled(lifecycle, tenant, alice, ProvisioningActor())
    assert await _disable_state(factory, alice) == (False, None)


@pytest.mark.asyncio
async def test_provisioning_starts_an_erasure_without_a_user_in_the_audit(
    factory, tenant
):
    alice = await _member(factory, tenant, "alice")
    local = await _member(factory, tenant, "carol", issuer="local")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)

    status, report = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=alice,
        actor=ProvisioningActor(),
        operation_id="op-scim-erase",
    )
    refused, _ = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=local,
        actor=ProvisioningActor(),
        operation_id="op-scim-other",
    )

    assert (status, report) == (
        UserLifecycleStatus.UPDATED,
        {"operation_id": "op-scim-erase"},
    )
    assert refused is UserLifecycleStatus.NOT_FOUND
    started = [
        tuple(row)
        for row in await _lifecycle_audit(factory, tenant, alice)
        if row[0] == "user.erasure_started"
    ]
    assert started == [("user.erasure_started", None, "provisioning")]


@pytest.mark.asyncio
async def test_an_administrator_without_the_role_cannot_disable(factory, tenant):
    former = await _member(factory, tenant, "former-admin")
    alice = await _member(factory, tenant, "alice")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    with pytest.raises(AdminAuthorizationError):
        await _set_disabled(lifecycle, tenant, alice, AdminActor(former))
    assert await _disable_state(factory, alice) == (False, None)


def _profile(user_name: str = "alice.smith", **values) -> ExternalProfile:
    return ExternalProfile(user_name=user_name, **values)


async def _provisioned(
    lifecycle: PostgresUserLifecycleTransaction,
    tenant_id: str,
    subject: str,
    **values,
) -> uuid.UUID:
    outcome = await lifecycle.provision_external_user(
        tenant_id=tenant_id, subject=subject, profile=_profile(**values)
    )
    assert outcome.status is UserLifecycleStatus.UPDATED and outcome.user is not None
    return outcome.user.user_id


async def _user_row(factory, user_id: uuid.UUID):
    async with factory() as session:
        return (
            await session.execute(select(users).where(users.c.id == user_id))
        ).one()


async def _set_columns(factory, user_id: uuid.UUID, **values) -> None:
    async with factory() as session:
        async with session.begin():
            await session.execute(
                users.update().where(users.c.id == user_id).values(**values)
            )


@pytest.mark.asyncio
async def test_provisioning_creates_an_account_without_a_sign_in(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id=tenant,
        subject="oid-alice",
        profile=_profile(
            display_name="Alice Smith",
            name={"givenName": "Alice"},
            email="alice@example.com",
            active=False,
        ),
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and not outcome.adopted
    assert outcome.user is not None
    row = await _user_row(factory, outcome.user.user_id)
    assert (row.issuer, row.subject, row.scim_user_name, row.scim_name) == (
        GATEWAY_ISSUER,
        "oid-alice",
        "alice.smith",
        {"givenName": "Alice"},
    )
    assert (row.email, row.email_verified, row.display_name) == (
        "alice@example.com",
        False,
        "Alice Smith",
    )
    assert row.last_login_at is None
    assert (row.disabled_at is not None, row.disabled_source) == (True, "provisioning")
    assert outcome.user.scim_user_name == "alice.smith"
    assert outcome.user.created_at == row.created_at.timestamp()
    audit = await _lifecycle_audit(factory, tenant, outcome.user.user_id)
    assert [tuple(entry) for entry in audit] == [
        ("user.provisioned", None, "provisioning")
    ]


@pytest.mark.asyncio
async def test_provisioning_adopts_an_account_it_never_bound(factory, tenant):
    signed_in = await _member(factory, tenant, "oid-alice")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id=tenant,
        subject="oid-alice",
        profile=_profile(display_name="Other Name"),
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and outcome.adopted
    assert outcome.user is not None and outcome.user.user_id == signed_in
    row = await _user_row(factory, signed_in)
    assert (row.scim_user_name, row.display_name) == ("alice.smith", "oid-alice")
    assert [row.action for row in await _lifecycle_audit(factory, tenant, signed_in)] == [
        "user.scim_adopted"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state", ["bound", "tombstone", "erasing", "name_taken", "name_in_another_case"]
)
async def test_provisioning_refuses_what_is_already_taken(factory, tenant, state):
    alice = await _member(factory, tenant, "oid-alice")
    bob = await _member(factory, tenant, "oid-bob")
    changes = {
        "bound": (alice, {"scim_user_name": "alice.old"}),
        # A tombstone need not be bound: provisioning can delete an account
        # of the source that it never bound.
        "tombstone": (
            alice,
            {"scim_deleted_at": text("now()"), "disabled_at": text("now()")},
        ),
        "erasing": (alice, {"erasure_operation_id": "op-erase"}),
        "name_taken": (bob, {"scim_user_name": "alice.smith"}),
        "name_in_another_case": (bob, {"scim_user_name": "ALICE.Smith"}),
    }
    target, values = changes[state]
    await _set_columns(factory, target, **values)
    before = await _user_row(factory, alice)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-alice", profile=_profile()
    )

    assert outcome.status is UserLifecycleStatus.CONFLICT
    assert await _user_row(factory, alice) == before


@pytest.mark.asyncio
async def test_provisioning_binds_only_under_the_identity_source(factory, tenant):
    local = await _member(factory, tenant, "oid-alice", issuer="local")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    outcome = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-alice", profile=_profile()
    )

    assert outcome.status is UserLifecycleStatus.UPDATED and not outcome.adopted
    assert outcome.user is not None and outcome.user.user_id != local
    assert (await _user_row(factory, local)).scim_user_name is None


@pytest.mark.asyncio
async def test_a_change_from_provisioning_unconfirms_only_a_new_email(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", email="alice@example.com")
    await _set_columns(factory, alice, email_verified=True)

    renamed = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=alice,
        change=lambda profile: replace(profile, display_name="Alice S."),
    )
    after_rename = await _user_row(factory, alice)
    moved = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=alice,
        change=lambda profile: replace(profile, email="new@example.com"),
    )
    after_move = await _user_row(factory, alice)

    assert renamed.status is moved.status is UserLifecycleStatus.UPDATED
    assert (after_rename.display_name, after_rename.email_verified) == ("Alice S.", True)
    assert (after_move.email, after_move.email_verified) == ("new@example.com", False)
    assert [row.action for row in await _lifecycle_audit(factory, tenant, alice)] == [
        "user.provisioned",
        "user.scim_updated",
        "user.scim_updated",
    ]


@pytest.mark.asyncio
async def test_active_from_a_change_follows_the_disable_rules(factory, tenant):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    bob = await _provisioned(lifecycle, tenant, "oid-bob", user_name="bob")

    inactive = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=alice,
        change=lambda profile: replace(profile, active=False),
    )
    await _set_disabled(lifecycle, tenant, bob, AdminActor(admin))
    kept = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=bob,
        change=lambda profile: replace(profile, active=True),
    )

    assert inactive.status is kept.status is UserLifecycleStatus.UPDATED
    assert await _disable_state(factory, alice) == (True, "provisioning")
    assert await _disable_state(factory, bob) == (True, "admin")
    audit = await _lifecycle_audit(factory, tenant, bob)
    assert audit[-1].action == "user.scim_enable_ignored"


@pytest.mark.asyncio
async def test_a_change_to_a_taken_user_name_is_a_conflict(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await _provisioned(lifecycle, tenant, "oid-bob", user_name="bob")

    outcome = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=alice,
        change=lambda profile: replace(profile, user_name="BOB", display_name="x"),
    )

    assert outcome.status is UserLifecycleStatus.CONFLICT
    row = await _user_row(factory, alice)
    assert (row.scim_user_name, row.display_name) == ("alice.smith", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["other_issuer", "tombstone", "erasing"])
async def test_a_change_reaches_no_foreign_tombstoned_or_erased_account(
    factory, tenant, state
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    if state == "other_issuer":
        target = await _member(factory, tenant, "carol", issuer="local")
    else:
        target = await _provisioned(lifecycle, tenant, "oid-carol", user_name="carol")
        values = (
            {"scim_deleted_at": text("now()"), "disabled_at": text("now()")}
            if state == "tombstone"
            else {"erasure_operation_id": "op-erase"}
        )
        await _set_columns(factory, target, **values)
    before = await _user_row(factory, target)

    outcome = await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=target,
        change=lambda profile: replace(profile, display_name="Changed"),
    )

    assert outcome.status is UserLifecycleStatus.NOT_FOUND
    assert await _user_row(factory, target) == before


@pytest.mark.asyncio
async def test_deprovisioning_cuts_off_and_leaves_a_tombstone_once(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    login = _login(tenant, "oid-alice", issuer=GATEWAY_ISSUER)
    await lifecycle.provision_login(login)

    first = await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    second = await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    again = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-alice", profile=_profile()
    )

    assert first is UserLifecycleStatus.UPDATED
    assert second is UserLifecycleStatus.NOT_FOUND
    assert again.status is UserLifecycleStatus.CONFLICT
    row = await _user_row(factory, alice)
    assert row.scim_deleted_at is not None
    assert await _disable_state(factory, alice) == (True, "provisioning")
    assert await _stored_session(factory, login.session.id) is None
    assert [row.action for row in await _lifecycle_audit(factory, tenant, alice)][-2:] == [
        "user.disabled",
        "user.deprovisioned",
    ]


@pytest.mark.asyncio
async def test_deprovisioning_frees_the_user_name_but_never_the_identity(factory, tenant):
    """A new person of the source can take the user name of a deleted
    account; the deleted identity itself does not come back."""
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)

    successor = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-successor", profile=_profile()
    )
    # A free user name, so that only the identity can refuse it.
    returning = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-alice", profile=_profile("alice.returns")
    )

    row = await _user_row(factory, alice)
    assert row.scim_deleted_at is not None and row.scim_user_name is None
    assert successor.status is UserLifecycleStatus.UPDATED
    assert successor.user is not None and successor.user.scim_user_name == "alice.smith"
    assert returning.status is UserLifecycleStatus.CONFLICT


@pytest.mark.asyncio
async def test_deprovisioning_keeps_an_administrators_disable_and_the_last_admin(
    factory, tenant
):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin))

    kept_source = await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    last_admin = await lifecycle.deprovision(tenant_id=tenant, user_id=admin)

    assert kept_source is UserLifecycleStatus.UPDATED
    assert await _disable_state(factory, alice) == (True, "admin")
    assert last_admin is UserLifecycleStatus.LAST_ADMIN
    spared = await _user_row(factory, admin)
    assert (spared.disabled_at, spared.scim_deleted_at) == (None, None)


@pytest.mark.asyncio
async def test_the_provisioning_page_shows_the_source_without_tombstones_or_erasures(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    first = await _provisioned(lifecycle, tenant, "oid-a", user_name="a.user")
    disabled = await _provisioned(
        lifecycle, tenant, "oid-b", user_name="B.User", active=False
    )
    tombstone = await _provisioned(lifecycle, tenant, "oid-c", user_name="c.user")
    await lifecycle.deprovision(tenant_id=tenant, user_id=tombstone)
    erasing = await _provisioned(lifecycle, tenant, "oid-d", user_name="d.user")
    await _set_columns(factory, erasing, erasure_operation_id="op-erase")
    await _member(factory, tenant, "oid-e", issuer="local")
    unbound = await _member(factory, tenant, "oid-f")
    directory = PostgresUserDirectory(session_factory=factory, app_role=APP_ROLE)

    async def page(**query):
        total, users_on_page = await directory.list_users_page(
            tenant_id=tenant, issuer=GATEWAY_ISSUER, **query
        )
        return total, [user.user_id for user in users_on_page]

    assert await page() == (3, [first, disabled, unbound])
    assert await page(user_name="b.user") == (1, [disabled])
    assert await page(subject="oid-f") == (1, [unbound])
    assert await page(start_index=2, count=1) == (3, [disabled])


@pytest.mark.asyncio
async def test_a_filter_finds_a_user_name_the_way_the_unique_index_compares_it(factory, tenant):
    # Python lowers a final sigma to its final form, the database's lower()
    # need not; the filter must compare like the index and the uniqueness check.
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    name = "ΟΔΥΣΣΕΥΣ@example.com"
    user = await _provisioned(lifecycle, tenant, "oid-greek", user_name=name)
    directory = PostgresUserDirectory(session_factory=factory, app_role=APP_ROLE)

    total, found = await directory.list_users_page(
        tenant_id=tenant, issuer=GATEWAY_ISSUER, user_name=name
    )

    assert (total, [entry.user_id for entry in found]) == (1, [user])


@pytest.mark.asyncio
async def test_an_administrators_erasure_frees_the_user_name_at_once(factory, tenant):
    # The identity system may give the name to someone new while the erasure runs.
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "admin", GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", user_name="alice@example.com")

    status, _ = await lifecycle.begin_erasure(
        tenant_id=tenant, user_id=alice, actor=AdminActor(admin), operation_id="op-admin"
    )
    reused = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-newcomer", profile=_profile(user_name="alice@example.com")
    )

    assert status is UserLifecycleStatus.UPDATED
    assert reused.status is UserLifecycleStatus.UPDATED
    assert (await _user_row(factory, alice)).scim_user_name is None


async def _admin_wakes(factory, tenant_id: str, admin: uuid.UUID) -> int:
    async with factory() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(user_events)
                .where(
                    user_events.c.tenant_id == tenant_id,
                    user_events.c.target_user_id == admin,
                )
            )
        ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command", ["create", "adopt", "profile", "deprovision-disabled", "erasure-refused"]
)
async def test_every_provisioning_change_wakes_the_administrators(factory, tenant, command):
    # An open admin list of another administrator follows every change.
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "admin", GATEWAY_ISSUER)
    alice = None
    if command == "adopt":
        alice = await _member(factory, tenant, "oid-alice")
    elif command != "create":
        alice = await _provisioned(
            lifecycle,
            tenant,
            "oid-alice",
            user_name="alice",
            active=command != "deprovision-disabled",
        )
    if command == "erasure-refused":
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    insert(workspaces).values(
                        id=uuid.uuid4(),
                        tenant_id=tenant,
                        name="shared workspace",
                        created_by_user_id=alice,
                    )
                )
        await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    before = await _admin_wakes(factory, tenant, admin)

    if command == "create":
        await _provisioned(lifecycle, tenant, "oid-bob", user_name="bob")
    elif command == "adopt":
        adopted = await lifecycle.provision_external_user(
            tenant_id=tenant, subject="oid-alice", profile=_profile(user_name="alice")
        )
        assert adopted.adopted
    elif command == "profile":
        await lifecycle.update_external_profile(
            tenant_id=tenant,
            user_id=alice,
            change=lambda profile: replace(profile, display_name="Alice Renamed"),
        )
    elif command == "deprovision-disabled":
        await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    else:
        outcome, _ = await lifecycle.begin_erasure(
            tenant_id=tenant, user_id=alice, actor=ProvisioningActor(), operation_id="op-refused"
        )
        assert outcome is UserLifecycleStatus.REFUSED

    assert await _admin_wakes(factory, tenant, admin) > before


@pytest.mark.asyncio
async def test_a_sign_in_keeps_the_provisioning_binding(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", name={"givenName": "Alice"})

    resolved = await lifecycle.provision_login(
        _login(
            tenant,
            "oid-alice",
            issuer=GATEWAY_ISSUER,
            email="alice@example.com",
            email_verified=True,
        )
    )

    assert resolved.user_id == alice
    row = await _user_row(factory, alice)
    assert (row.scim_user_name, row.scim_name) == ("alice.smith", {"givenName": "Alice"})
    assert (row.email, row.email_verified) == ("alice@example.com", True)
    assert row.last_login_at is not None


def _paused_before_write(entered: asyncio.Event, release: asyncio.Event):
    """A lifecycle on sessions that stop before their first write to ``users``.

    The pause comes after the tenant lock and the reads, so a command
    that takes the lock holds it while it waits.
    """

    class PauseBeforeWrite(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if (
                not entered.is_set()
                and getattr(statement, "table", None) is users
                and (
                    getattr(statement, "is_insert", False)
                    or getattr(statement, "is_update", False)
                )
            ):
                entered.set()
                await release.wait()
            return await super().execute(statement, *args, **kwargs)

    engine = build_engine(TEST_DATABASE_URL)
    paused = PostgresUserLifecycleTransaction(
        session_factory=async_sessionmaker(
            engine, expire_on_commit=False, class_=PauseBeforeWrite
        ),
        app_role=APP_ROLE,
        source_issuer=GATEWAY_ISSUER,
    )
    return engine, paused


@pytest.mark.asyncio
async def test_a_sign_in_waits_for_provisioning_and_binds_to_its_account(
    factory, tenant
):
    entered, release = asyncio.Event(), asyncio.Event()
    engine, paused = _paused_before_write(entered, release)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    try:
        provisioning = asyncio.create_task(
            paused.provision_external_user(
                tenant_id=tenant, subject="oid-alice", profile=_profile()
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        sign_in = asyncio.create_task(
            lifecycle.provision_login(
                _login(tenant, "oid-alice", issuer=GATEWAY_ISSUER)
            )
        )
        await asyncio.sleep(0.3)
        waited = not sign_in.done()
        release.set()
        outcome = await asyncio.wait_for(provisioning, timeout=10)
        signed_in = await asyncio.wait_for(sign_in, timeout=10)
    finally:
        release.set()
        await engine.dispose()

    assert waited, "the sign-in did not wait for the tenant lock"
    assert outcome.status is UserLifecycleStatus.UPDATED and not outcome.adopted
    assert outcome.user is not None and signed_in.user_id == outcome.user.user_id
    async with factory() as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(users)
                .where(users.c.tenant_id == tenant, users.c.subject == "oid-alice")
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_a_sign_in_during_a_change_from_provisioning_comes_after_it(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", email="old@example.com")
    entered, release = asyncio.Event(), asyncio.Event()
    engine, paused = _paused_before_write(entered, release)
    try:
        change = asyncio.create_task(
            paused.update_external_profile(
                tenant_id=tenant,
                user_id=alice,
                change=lambda profile: replace(profile, email="scim@example.com"),
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        sign_in = asyncio.create_task(
            lifecycle.provision_login(
                _login(
                    tenant,
                    "oid-alice",
                    issuer=GATEWAY_ISSUER,
                    email="login@example.com",
                    email_verified=True,
                )
            )
        )
        await asyncio.sleep(0.3)
        waited = not sign_in.done()
        release.set()
        await asyncio.wait_for(change, timeout=10)
        await asyncio.wait_for(sign_in, timeout=10)
    finally:
        release.set()
        await engine.dispose()

    assert waited, "the sign-in did not wait for the tenant lock"
    # The sign-in ran after the change, so its confirmed address wins.
    row = await _user_row(factory, alice)
    assert (row.email, row.email_verified) == ("login@example.com", True)


@pytest.mark.asyncio
async def test_a_change_without_a_user_name_leaves_an_account_unbound(factory, tenant):
    first = await _member(factory, tenant, "oid-f")
    second = await _member(factory, tenant, "oid-g")
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    outcomes = [
        await lifecycle.update_external_profile(
            tenant_id=tenant,
            user_id=target,
            change=lambda profile: replace(profile, display_name="Renamed"),
        )
        for target in (first, second)
    ]

    assert [outcome.status for outcome in outcomes] == [
        UserLifecycleStatus.UPDATED,
        UserLifecycleStatus.UPDATED,
    ]
    for target in (first, second):
        row = await _user_row(factory, target)
        assert (row.scim_user_name, row.display_name) == (None, "Renamed")


@pytest.mark.asyncio
async def test_an_account_being_erased_is_no_new_target(factory, tenant):
    # The erasure lifts disabled_at so the deletion machine can act as the
    # owner; the account must still not become a share recipient, a
    # workspace member or a search hit.
    active = await _member(factory, tenant, "anna")
    disabled = await _member(factory, tenant, "anton")
    erasing = await _member(factory, tenant, "anke")
    await _set_columns(factory, disabled, disabled_at=text("now()"))
    await _set_columns(factory, erasing, erasure_operation_id="op-erase")
    directory = PostgresUserDirectory(session_factory=factory, app_role=APP_ROLE)

    known = {
        user_id: await directory.has_user_id(tenant_id=tenant, user_id=user_id)
        for user_id in (active, disabled, erasing)
    }
    found = {
        user.user_id for user in await directory.search(tenant_id=tenant, query="an")
    }

    assert known == {active: True, disabled: False, erasing: False}
    assert found == {active}


@pytest.mark.asyncio
async def test_provisioning_starts_at_most_its_hourly_limit_of_erasures(factory, tenant):
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    by_admin = await _member(factory, tenant, "anna")
    first = await _member(factory, tenant, "bert")
    second = await _member(factory, tenant, "cleo")
    for deprovisioned in (first, second):
        await lifecycle.deprovision(tenant_id=tenant, user_id=deprovisioned)

    # An administrator's erasure does not count against provisioning.
    admin_started, _ = await lifecycle.begin_erasure(
        tenant_id=tenant, user_id=by_admin, actor=AdminActor(admin), operation_id="op-admin"
    )
    allowed, _ = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=first,
        actor=ProvisioningActor(),
        operation_id="op-first",
        hourly_limit=1,
    )
    limited, report = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=second,
        actor=ProvisioningActor(),
        operation_id="op-second",
        hourly_limit=1,
    )

    assert (admin_started, allowed) == (
        UserLifecycleStatus.UPDATED,
        UserLifecycleStatus.UPDATED,
    )
    assert (limited, report) == (UserLifecycleStatus.LIMITED, {})
    assert (await _user_row(factory, second)).erasure_operation_id is None


@pytest.mark.asyncio
async def test_a_refused_erasure_from_provisioning_is_audited_with_its_reasons(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _member(factory, tenant, "alice")
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(workspaces).values(
                    id=uuid.uuid4(),
                    tenant_id=tenant,
                    name="shared workspace",
                    created_by_user_id=alice,
                )
            )
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)

    outcome, report = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=alice,
        actor=ProvisioningActor(),
        operation_id="op-refused",
    )

    assert outcome is UserLifecycleStatus.REFUSED
    async with factory() as session:
        rows = (
            await session.execute(
                select(audit_log.c.action, audit_log.c.actor_type, audit_log.c.detail).where(
                    audit_log.c.tenant_id == tenant,
                    audit_log.c.resource_id == str(alice),
                )
            )
        ).all()
    refused = [row for row in rows if row.action == "user.erasure_refused"]
    assert [(row.action, row.actor_type) for row in refused] == [
        ("user.erasure_refused", "provisioning")
    ]
    assert refused[0].detail == {"reasons": report["refused"]}
    assert (await _user_row(factory, alice)).erasure_operation_id is None


@pytest.mark.asyncio
async def test_the_erasure_start_lifts_the_disable_with_its_source(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)

    status, _ = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=alice,
        actor=ProvisioningActor(),
        operation_id="op-erase",
    )

    row = await _user_row(factory, alice)
    assert status is UserLifecycleStatus.UPDATED
    assert (row.disabled_at, row.disabled_source, row.erasure_operation_id) == (
        None,
        None,
        "op-erase",
    )


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_an_unknown_identity_gets_no_account(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)

    with pytest.raises(UserNotProvisionedError):
        await lifecycle.provision_login(
            _login(tenant, "oid-new", issuer=GATEWAY_ISSUER, provisioned_only=True)
        )

    async with factory() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(users).where(users.c.tenant_id == tenant)
            )
        ).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_a_provisioned_account_signs_in(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")

    user = await lifecycle.provision_login(
        _login(tenant, "oid-alice", issuer=GATEWAY_ISSUER, provisioned_only=True)
    )

    assert user.user_id == alice


@pytest.mark.asyncio
async def test_when_provisioning_owns_creation_a_tombstone_stays_disabled(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    assert (
        await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
        is UserLifecycleStatus.UPDATED
    )

    with pytest.raises(UserDisabledError):
        await lifecycle.provision_login(
            _login(tenant, "oid-alice", issuer=GATEWAY_ISSUER, provisioned_only=True)
        )


@pytest.mark.asyncio
async def test_a_sign_in_returns_the_whole_account(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", name={"givenName": "Alice"})

    user = await lifecycle.provision_login(_login(tenant, "oid-alice", issuer=GATEWAY_ISSUER))

    row = await _user_row(factory, alice)
    assert (user.scim_user_name, user.scim_name) == ("alice.smith", {"givenName": "Alice"})
    assert user.created_at == row.created_at.timestamp()


async def _enable_details(factory, tenant_id: str, user_id: uuid.UUID) -> list:
    async with factory() as session:
        rows = (
            await session.execute(
                select(audit_log.c.detail).where(
                    audit_log.c.tenant_id == tenant_id,
                    audit_log.c.action == "user.enabled",
                    audit_log.c.resource_id == str(user_id),
                )
            )
        ).scalars()
        return [dict(detail or {}) for detail in rows]


@pytest.mark.asyncio
async def test_an_administrator_lifts_a_tombstone_with_the_disable(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    assert (
        await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
        is UserLifecycleStatus.UPDATED
    )

    status = await _set_disabled(lifecycle, tenant, alice, AdminActor(admin), disabled=False)

    row = await _user_row(factory, alice)
    assert status is UserLifecycleStatus.UPDATED
    assert (row.disabled_at, row.scim_deleted_at, row.disabled_source) == (None, None, None)
    assert await _enable_details(factory, tenant, alice) == [
        {"restored_from_deprovisioning": "true"}
    ]


@pytest.mark.asyncio
async def test_a_restored_account_stays_unbound_until_provisioning_binds_it(
    factory, tenant
):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin), disabled=False)
    restored = await _user_row(factory, alice)

    bound = await lifecycle.provision_external_user(
        tenant_id=tenant, subject="oid-alice", profile=_profile()
    )

    assert restored.scim_user_name is None
    assert bound.status is UserLifecycleStatus.UPDATED and bound.adopted
    assert bound.user is not None and bound.user.user_id == alice
    assert bound.user.scim_user_name == "alice.smith"


@pytest.mark.asyncio
async def test_an_enable_without_a_tombstone_names_no_restore(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _member(factory, tenant, "alice")
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin))

    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin), disabled=False)

    assert await _enable_details(factory, tenant, alice) == [{}]


@pytest.mark.asyncio
async def test_provisioning_never_lifts_a_tombstone(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)

    status = await _set_disabled(lifecycle, tenant, alice, ProvisioningActor(), disabled=False)

    row = await _user_row(factory, alice)
    assert status is UserLifecycleStatus.NOT_FOUND
    assert row.disabled_at is not None and row.scim_deleted_at is not None


async def _provisioning_state(factory, user_id: uuid.UUID) -> tuple:
    row = await _user_row(factory, user_id)
    return (
        row.id,
        row.disabled_at,
        row.disabled_source,
        row.scim_user_name,
        row.scim_name,
        row.scim_deleted_at,
    )


@pytest.mark.asyncio
async def test_a_mode_switch_through_oidc_keeps_the_provisioning_state(factory, tenant):
    # The gateway signs JWTs of the same identity provider the OIDC mode
    # trusts: both modes bind accounts under one issuer key.
    issuer = "https://idp.example/realms/corp"
    gateway = _lifecycle(factory, issuer)
    admin = await _seed_admin(factory, tenant, "gw-admin", issuer)
    alice = await _provisioned(gateway, tenant, "oid-alice", user_name="alice")
    bob = await _provisioned(gateway, tenant, "oid-bob", user_name="bob")
    carol = await _provisioned(gateway, tenant, "oid-carol", user_name="carol")
    await gateway.deprovision(tenant_id=tenant, user_id=bob)
    await _set_disabled(gateway, tenant, carol, AdminActor(admin))
    before = [await _provisioning_state(factory, user) for user in (alice, bob, carol)]

    oidc = _lifecycle(factory, None)
    signed_in = await oidc.provision_login(
        _login(tenant, "oid-alice", issuer=issuer, email="alice@corp.example", email_verified=True)
    )
    for subject in ("oid-bob", "oid-carol"):
        with pytest.raises(UserDisabledError):
            await oidc.provision_login(_login(tenant, subject, issuer=issuer))

    assert signed_in.user_id == alice
    assert [await _provisioning_state(factory, user) for user in (alice, bob, carol)] == before
    # Back in the gateway mode: the tombstone stays out of reach, its
    # subject stays taken, and active=true leaves the admin disable.
    lifted = await gateway.update_external_profile(
        tenant_id=tenant, user_id=bob, change=lambda profile: replace(profile, active=True)
    )
    taken = await gateway.provision_external_user(
        tenant_id=tenant, subject="oid-bob", profile=_profile("bob-again")
    )
    kept = await gateway.update_external_profile(
        tenant_id=tenant, user_id=carol, change=lambda profile: replace(profile, active=True)
    )
    assert (lifted.status, taken.status) == (
        UserLifecycleStatus.NOT_FOUND,
        UserLifecycleStatus.CONFLICT,
    )
    assert kept.user is not None and kept.user.disabled_at is not None


@pytest.mark.asyncio
async def test_provisioning_erases_only_what_it_deprovisioned(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    admin = await _seed_admin(factory, tenant, "gw-admin", GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    # An administrator restores the account before provisioning's erasure
    # of it starts: it is no longer provisioning's to erase.
    await _set_disabled(lifecycle, tenant, alice, AdminActor(admin), disabled=False)

    status, report = await lifecycle.begin_erasure(
        tenant_id=tenant,
        user_id=alice,
        actor=ProvisioningActor(),
        operation_id="op-late-erase",
    )

    assert (status, report) == (UserLifecycleStatus.NOT_FOUND, {})
    assert (await _user_row(factory, alice)).erasure_operation_id is None


@pytest.mark.asyncio
async def test_a_cleared_scim_name_is_sql_null(factory, tenant):
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice", name={"givenName": "Alice"})
    bob = await _member(factory, tenant, "oid-bob")

    await lifecycle.update_external_profile(
        tenant_id=tenant, user_id=alice, change=lambda profile: replace(profile, name=None)
    )
    await lifecycle.update_external_profile(
        tenant_id=tenant,
        user_id=bob,
        change=lambda profile: replace(profile, display_name="Bob B."),
    )

    async with factory() as session:
        nulls = (
            await session.execute(
                text("SELECT scim_name IS NULL FROM users WHERE id IN (:a, :b)"),
                {"a": alice, "b": bob},
            )
        ).scalars().all()
    assert nulls == [True, True]


async def _seed_pat(factory, tenant_id: str, owner: uuid.UUID) -> str:
    token_id = f"tok-{uuid.uuid4().hex[:12]}"
    async with factory() as session:
        async with session.begin():
            await session.execute(
                insert(personal_access_tokens).values(
                    token_id=token_id,
                    tenant_id=tenant_id,
                    owner_user_id=owner,
                    name="ci",
                    secret_hmac="ab" * 32,
                    created_at=time.time(),
                )
            )
    return token_id


async def _woken_and_revoked(factory, tenant_id: str, user_id: uuid.UUID, token_id: str):
    """How often the account's own events woke it, and whether its token is revoked."""
    async with factory() as session:
        woken = (
            await session.execute(
                select(func.count())
                .select_from(user_events)
                .where(
                    user_events.c.tenant_id == tenant_id,
                    user_events.c.target_user_id == user_id,
                    user_events.c.resource_id == str(user_id),
                )
            )
        ).scalar_one()
        revoked_at = (
            await session.execute(
                select(personal_access_tokens.c.revoked_at).where(
                    personal_access_tokens.c.token_id == token_id
                )
            )
        ).scalar_one()
    return woken, revoked_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["deprovision", "active-false"])
async def test_provisioning_cuts_off_and_wakes_the_account(factory, tenant, command):
    """A deactivation through provisioning ends the account's tokens and wakes
    its open streams in the same transaction."""
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    alice = await _provisioned(lifecycle, tenant, "oid-alice")
    token_id = await _seed_pat(factory, tenant, alice)
    woken_before, _ = await _woken_and_revoked(factory, tenant, alice, token_id)

    if command == "deprovision":
        await lifecycle.deprovision(tenant_id=tenant, user_id=alice)
    else:
        await lifecycle.update_external_profile(
            tenant_id=tenant, user_id=alice, change=lambda profile: replace(profile, active=False)
        )

    woken, revoked = await _woken_and_revoked(factory, tenant, alice, token_id)
    assert woken > woken_before
    assert revoked


@pytest.mark.asyncio
async def test_two_erasures_at_once_start_only_one_within_the_limit(factory, tenant):
    """The hourly limit is counted under the tenant lock: two DELETEs that
    arrive together at the limit start exactly one erasure."""
    lifecycle = _lifecycle(factory, GATEWAY_ISSUER)
    first = await _provisioned(lifecycle, tenant, "oid-first", user_name="first")
    second = await _provisioned(lifecycle, tenant, "oid-second", user_name="second")
    for buried in (first, second):
        await lifecycle.deprovision(tenant_id=tenant, user_id=buried)
    entered, release = asyncio.Event(), asyncio.Event()
    engine, paused = _paused_before_write(entered, release)
    try:
        holding = asyncio.create_task(
            paused.begin_erasure(
                tenant_id=tenant,
                user_id=first,
                actor=ProvisioningActor(),
                operation_id="op-first",
                hourly_limit=1,
            )
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        waiting = asyncio.create_task(
            lifecycle.begin_erasure(
                tenant_id=tenant,
                user_id=second,
                actor=ProvisioningActor(),
                operation_id="op-second",
                hourly_limit=1,
            )
        )
        await asyncio.sleep(0.3)
        waited = not waiting.done()
        release.set()
        held, _ = await asyncio.wait_for(holding, timeout=10)
        later, _ = await asyncio.wait_for(waiting, timeout=10)
    finally:
        release.set()
        await engine.dispose()

    assert waited, "the second erasure did not wait for the tenant lock"
    assert (held, later) == (UserLifecycleStatus.UPDATED, UserLifecycleStatus.LIMITED)
