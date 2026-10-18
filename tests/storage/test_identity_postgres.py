"""Postgres integration tests for canonical identity and direct sharing.

The module is gated on ``INQTRIX_TEST_DATABASE_URL``. The configured database
must be disposable, but the v0.2 migration chain itself is intentionally
irreversible: the session fixture upgrades to head and verifies that a request
to cross the hard-cut boundary is rejected instead of destroying data.

RLS assertions run under ``SET LOCAL ROLE inqtrix_app``. The cleanup connection
must be a superuser or carry ``BYPASSRLS`` because it removes rows across every
tenant between tests.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from importlib import import_module
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import Table, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.lifecycle import AdminAuthorizationError
from inqtrix.auth.permissions import (
    AuthorizationService,
    LastWorkspaceOwnerError,
    ResourceNotFound,
    SharePermission,
    WorkspaceNotFound,
    WorkspaceRole,
)
from inqtrix.auth.principal import Principal
from inqtrix.auth.shares import ShareConflict, ShareRecord
from inqtrix.storage.db import build_engine, build_session_factory, tenant_session
from inqtrix.storage.identity_orm import (
    audit_log,
    identity_metadata,
    invitations,
    resource_shares,
    tenant_security_state,
    users,
    workspace_members,
    workspaces,
)
from inqtrix.storage.identity_postgres import PostgresIdentityBackend
from inqtrix.storage.knowledge_orm import knowledge_collections
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_event_orm import user_events
from inqtrix.storage.user_lifecycle_postgres import (
    PostgresUserLifecycleTransaction,
)

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
TENANT_ID = "default"
SessionFactory = async_sessionmaker[AsyncSession]


@pytest.fixture(scope="session", autouse=True)
def migrated_schema() -> Iterator[None]:
    """Upgrade to head and pin the irreversible v0.2 boundary."""
    if not TEST_DATABASE_URL:
        yield
        return
    run_migrations(TEST_DATABASE_URL)
    for revision in (
        "0045_canonical_user_ids",
        "0046_execution_authority",
        "0047_resource_sync_and_reindex",
    ):
        migration = import_module(
            f"inqtrix.storage.migrations.versions.{revision}"
        )
        with pytest.raises(RuntimeError, match="irreversible"):
            migration.downgrade()
    yield


@pytest_asyncio.fixture()
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = build_engine(TEST_DATABASE_URL)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def session_factory(engine: AsyncEngine) -> SessionFactory:
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
                    "superuser/BYPASSRLS user for cross-tenant cleanup"
                )
            for table in (
                user_events,
                audit_log,
                resource_shares,
                invitations,
                workspace_members,
                knowledge_collections,
                workspaces,
                tenant_security_state,
            ):
                await session.execute(table.delete())
            # This module shares one disposable integration database with
            # independent storage suites.  Their resource rows legitimately
            # retain RESTRICT references to their own canonical users between
            # modules.  Identity fixtures use a dedicated issuer, so remove
            # only identities created here instead of deleting the global
            # user registry and making the suite order-dependent.
            await session.execute(
                users.delete().where(users.c.issuer == "https://idp.example")
            )
    return factory


def scoped(
    factory: SessionFactory,
    tenant_id: str = TENANT_ID,
) -> AbstractAsyncContextManager[AsyncSession]:
    """Open one app-role transaction bound to *tenant_id*."""
    return tenant_session(factory, tenant_id=tenant_id, app_role=APP_ROLE)


def backend(
    factory: SessionFactory,
    *,
    restrict_to_workspace_members: bool = False,
) -> PostgresIdentityBackend:
    """Build the identity repository under test."""
    return PostgresIdentityBackend(
        session_factory=factory,
        app_role=APP_ROLE,
        restrict_to_workspace_members=restrict_to_workspace_members,
    )


async def _insert_user(
    session: AsyncSession,
    *,
    tenant_id: str,
    label: str,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Insert one active canonical user in an existing transaction."""
    canonical_id = user_id or uuid.uuid4()
    unique_label = f"{label}-{canonical_id.hex}"
    await session.execute(
        insert(users).values(
            id=canonical_id,
            tenant_id=tenant_id,
            issuer="https://idp.example",
            subject=unique_label,
            email=f"{unique_label}@example.com",
        )
    )
    return canonical_id


async def create_user(
    factory: SessionFactory,
    *,
    label: str,
    tenant_id: str = TENANT_ID,
) -> uuid.UUID:
    """Persist one active canonical user and return its UUID."""
    async with scoped(factory, tenant_id) as session:
        return await _insert_user(
            session,
            tenant_id=tenant_id,
            label=label,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["demote", "disable"])
async def test_inflight_admin_command_revalidates_actor_after_security_lock(
    session_factory: SessionFactory,
    engine: AsyncEngine,
    revocation: str,
) -> None:
    """A demoted/disabled actor cannot commit an already-admitted command."""
    async with scoped(session_factory) as session:
        actor_user_id = await _insert_user(
            session, tenant_id=TENANT_ID, label="actor"
        )
        peer_admin_id = await _insert_user(
            session, tenant_id=TENANT_ID, label="peer-admin"
        )
        target_user_id = await _insert_user(
            session, tenant_id=TENANT_ID, label="target"
        )
        await session.execute(
            update(users)
            .where(users.c.id.in_((actor_user_id, peer_admin_id)))
            .values(instance_role="admin")
        )

    entered_transaction = asyncio.Event()
    release_transaction = asyncio.Event()

    class BarrierSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            table = getattr(statement, "table", None)
            if (
                not entered_transaction.is_set()
                and table is tenant_security_state
            ):
                entered_transaction.set()
                await release_transaction.wait()
            return await super().execute(statement, *args, **kwargs)

    barrier_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=BarrierSession,
    )
    lifecycle = PostgresUserLifecycleTransaction(
        session_factory=barrier_factory,
        app_role=APP_ROLE,
    )
    command = asyncio.create_task(
        lifecycle.set_role(
            tenant_id=TENANT_ID,
            user_id=target_user_id,
            role="admin",
            actor_user_id=actor_user_id,
        )
    )
    await asyncio.wait_for(entered_transaction.wait(), timeout=5)

    async with scoped(session_factory) as session:
        values = (
            {"instance_role": "user"}
            if revocation == "demote"
            else {"disabled_at": func.now()}
        )
        await session.execute(
            update(users).where(users.c.id == actor_user_id).values(**values)
        )
    release_transaction.set()

    with pytest.raises(AdminAuthorizationError):
        await command
    async with scoped(session_factory) as session:
        target_role = await session.scalar(
            select(users.c.instance_role).where(users.c.id == target_user_id)
        )
    assert target_role == "user"


async def create_collection(
    factory: SessionFactory,
    *,
    owner_user_id: uuid.UUID,
    collection_id: str,
    tenant_id: str = TENANT_ID,
) -> str:
    """Insert the minimal real shareable resource used by identity tests."""
    async with scoped(factory, tenant_id) as session:
        await session.execute(
            insert(knowledge_collections).values(
                id=collection_id,
                tenant_id=tenant_id,
                name=collection_id,
                embedding_model="test-embedding",
                embedding_dim=3,
                created_by_user_id=owner_user_id,
                created_at=time.time(),
            )
        )
    return collection_id


def oidc(user_id: uuid.UUID, *, tenant_id: str = TENANT_ID) -> Principal:
    """Build one scoped principal with a canonical UUID."""
    return Principal(
        user_id=user_id,
        kind="oidc_session",
        tenant_id=tenant_id,
        role="member",
    )


async def insert_minimal_row(
    session: AsyncSession,
    table: Table,
    tenant_id: str,
) -> dict[str, object]:
    """Insert one valid canonical row and return values for an RLS replay."""
    values: dict[str, object] = {"tenant_id": tenant_id}
    if table is users:
        marker = uuid.uuid4().hex
        values.update(
            issuer="https://idp.example",
            subject=f"user-{marker}",
            email=f"user-{marker}@example.com",
        )
    elif table is tenant_security_state:
        pass
    elif table is workspaces:
        owner_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="workspace-owner",
        )
        values.update(name="Workspace", created_by_user_id=owner_user_id)
    elif table is workspace_members:
        owner_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="member-owner",
        )
        member_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="member",
        )
        workspace_id = (
            await session.execute(
                insert(workspaces)
                .values(
                    tenant_id=tenant_id,
                    name="Workspace",
                    created_by_user_id=owner_user_id,
                )
                .returning(workspaces.c.id)
            )
        ).scalar_one()
        values.update(
            workspace_id=workspace_id,
            user_id=member_user_id,
            role=WorkspaceRole.VIEWER.value,
        )
    elif table is invitations:
        owner_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="inviter",
        )
        workspace_id = (
            await session.execute(
                insert(workspaces)
                .values(
                    tenant_id=tenant_id,
                    name="Workspace",
                    created_by_user_id=owner_user_id,
                )
                .returning(workspaces.c.id)
            )
        ).scalar_one()
        values.update(
            workspace_id=workspace_id,
            email="invitee@example.com",
            role=WorkspaceRole.VIEWER.value,
            invited_by_user_id=owner_user_id,
            expires_at=text("now() + interval '1 day'"),
        )
    elif table is resource_shares:
        owner_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="share-owner",
        )
        recipient_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="share-recipient",
        )
        values.update(
            recipient_user_id=recipient_user_id,
            resource_type="knowledge_collection",
            resource_id="kc_rls",
            permission=SharePermission.VIEW.value,
            granted_by_user_id=owner_user_id,
        )
    elif table is audit_log:
        actor_user_id = await _insert_user(
            session,
            tenant_id=tenant_id,
            label="audit-actor",
        )
        values.update(
            actor_user_id=actor_user_id,
            action="authz.denied",
            resource_type="knowledge_collection",
            resource_id="kc_rls",
            detail={},
        )
    else:
        raise AssertionError(f"no row factory for table {table.name}")
    await session.execute(insert(table).values(**values))
    return values


TENANT_TABLES = (
    users,
    tenant_security_state,
    workspaces,
    workspace_members,
    invitations,
    resource_shares,
    audit_log,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TENANT_TABLES, ids=lambda table: table.name)
async def test_cross_tenant_select_returns_zero_rows(
    session_factory: SessionFactory,
    table: Table,
) -> None:
    async with scoped(session_factory, "tenant-a") as session:
        await insert_minimal_row(session, table, "tenant-a")

    async with scoped(session_factory, "tenant-a") as session:
        own = (await session.execute(select(table.c.tenant_id))).all()
    async with scoped(session_factory, "tenant-b") as session:
        foreign = (await session.execute(select(table.c.tenant_id))).all()

    assert own
    assert foreign == []


@pytest.mark.asyncio
@pytest.mark.parametrize("table", TENANT_TABLES, ids=lambda table: table.name)
async def test_cross_tenant_insert_violates_with_check_everywhere(
    session_factory: SessionFactory,
    table: Table,
) -> None:
    with pytest.raises(DBAPIError, match="row-level security"):
        async with scoped(session_factory, "tenant-a") as session:
            values = await insert_minimal_row(session, table, "tenant-a")
            if table is not audit_log:
                await session.execute(table.delete())
            await session.execute(
                insert(table).values(**{**values, "tenant_id": "tenant-b"})
            )


@pytest.mark.asyncio
async def test_cross_tenant_delete_silently_affects_zero_rows(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(
        session_factory,
        label="owner",
        tenant_id="tenant-a",
    )
    identity = backend(session_factory)
    await identity.create_workspace(
        tenant_id="tenant-a",
        name="Workspace",
        created_by_user_id=owner_user_id,
    )

    async with scoped(session_factory, "tenant-b") as session:
        result = await session.execute(workspaces.delete())
    async with scoped(session_factory, "tenant-a") as session:
        remaining = (await session.execute(select(workspaces.c.id))).all()

    assert result.rowcount == 0
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_rls_catalog_covers_current_identity_metadata(
    session_factory: SessionFactory,
) -> None:
    async with session_factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, "
                    "c.relforcerowsecurity, "
                    "EXISTS (SELECT 1 FROM pg_policies p "
                    "WHERE p.schemaname = 'public' "
                    "AND p.tablename = c.relname) AS has_policy "
                    "FROM pg_class c "
                    "WHERE c.relnamespace = 'public'::regnamespace "
                    "AND c.relkind = 'r'"
                )
            )
        ).all()
    catalog = {name: (rls, forced, policy) for name, rls, forced, policy in rows}
    for table_name in identity_metadata.tables:
        assert table_name in catalog, f"{table_name} missing in database"
        rls, forced, policy = catalog[table_name]
        assert rls and forced and policy, (
            f"{table_name}: ENABLE={rls} FORCE={forced} policy={policy}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant_guc", [None, ""], ids=["unset", "empty"])
async def test_query_without_usable_tenant_context_fails_loudly(
    session_factory: SessionFactory,
    tenant_guc: str | None,
) -> None:
    """Seed a row first: the policy expression is a per-row InitPlan, so an
    empty table never reaches the resolver and would hide a missing guard."""
    async with scoped(session_factory) as session:
        await insert_minimal_row(session, workspaces, TENANT_ID)

    async with session_factory() as session:
        async with session.begin():
            await session.execute(text(f'SET LOCAL ROLE "{APP_ROLE}"'))
            if tenant_guc is not None:
                await session.execute(
                    text("SELECT set_config('inqtrix.tenant_id', :value, true)"),
                    {"value": tenant_guc},
                )
            with pytest.raises(DBAPIError, match="tenant_id"):
                await session.execute(select(workspaces.c.id))


@pytest.mark.asyncio
async def test_cross_tenant_insert_violates_with_check(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(
        session_factory,
        label="owner",
        tenant_id="tenant-a",
    )
    with pytest.raises(DBAPIError, match="row-level security"):
        async with scoped(session_factory, "tenant-a") as session:
            await session.execute(
                insert(workspaces).values(
                    tenant_id="tenant-b",
                    name="Foreign",
                    created_by_user_id=owner_user_id,
                )
            )


@pytest.mark.asyncio
async def test_audit_log_is_insert_only_for_the_app_role(
    session_factory: SessionFactory,
) -> None:
    actor_user_id = await create_user(session_factory, label="actor")
    async with scoped(session_factory) as session:
        await session.execute(
            insert(audit_log).values(
                tenant_id=TENANT_ID,
                actor_user_id=actor_user_id,
                action="authz.denied",
                resource_type="knowledge_collection",
                resource_id="kc_audit",
                detail={},
            )
        )

    with pytest.raises(DBAPIError, match="permission denied"):
        async with scoped(session_factory) as session:
            await session.execute(update(audit_log).values(action="tampered"))

    with pytest.raises(DBAPIError, match="permission denied"):
        async with scoped(session_factory) as session:
            await session.execute(audit_log.delete())


@pytest.mark.asyncio
async def test_duplicate_active_direct_share_is_rejected(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_unique",
    )
    grant = {
        "tenant_id": TENANT_ID,
        "recipient_user_id": recipient_user_id,
        "resource_type": "knowledge_collection",
        "resource_id": "kc_unique",
        "permission": SharePermission.VIEW.value,
        "granted_by_user_id": owner_user_id,
    }
    async with scoped(session_factory) as session:
        await session.execute(insert(resource_shares).values(**grant))

    with pytest.raises(IntegrityError):
        async with scoped(session_factory) as session:
            await session.execute(insert(resource_shares).values(**grant))

    async with scoped(session_factory) as session:
        await session.execute(
            update(resource_shares)
            .where(resource_shares.c.revoked_at.is_(None))
            .values(
                revoked_at=func.now(),
                revoked_by_user_id=owner_user_id,
            )
        )
        await session.execute(
            insert(resource_shares).values(
                **{**grant, "permission": SharePermission.EDIT.value}
            )
        )


async def arrange_identity_facts(
    factory: SessionFactory,
) -> tuple[
    PostgresIdentityBackend,
    str,
    str,
    uuid.UUID,
    uuid.UUID,
    uuid.UUID,
]:
    """Create canonical users, one workspace, and one accepted direct share."""
    owner_user_id = await create_user(factory, label="owner")
    recipient_user_id = await create_user(factory, label="recipient")
    outsider_user_id = await create_user(factory, label="outsider")
    identity = backend(factory)
    workspace_id, _name = await identity.create_workspace(
        tenant_id=TENANT_ID,
        name="Team",
        created_by_user_id=owner_user_id,
    )
    assert await identity.assign_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=recipient_user_id,
        role=WorkspaceRole.EDITOR,
    )
    resource_id = await create_collection(
        factory,
        owner_user_id=owner_user_id,
        collection_id="kc_access",
    )
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id=resource_id,
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.EDIT),),
    )
    accepted = await identity.accept_share_by_id(
        tenant_id=TENANT_ID,
        share_id=share.id,
        recipient_user_id=recipient_user_id,
        owner_user_id=owner_user_id,
    )
    assert accepted is not None and accepted.accepted_at is not None
    return (
        identity,
        workspace_id,
        resource_id,
        owner_user_id,
        recipient_user_id,
        outsider_user_id,
    )


@pytest.mark.asyncio
async def test_postgres_backend_uses_uuid_membership_and_direct_shares(
    session_factory: SessionFactory,
) -> None:
    (
        identity,
        workspace_id,
        resource_id,
        owner_user_id,
        recipient_user_id,
        outsider_user_id,
    ) = await arrange_identity_facts(session_factory)
    service = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
    )

    context = await service.resolve_user_context(oidc(recipient_user_id))
    assert context is not None
    assert context.workspace_ids == (workspace_id,)
    assert (
        await identity.role_in_workspace(
            tenant_id=TENANT_ID,
            user_id=recipient_user_id,
            workspace_id=workspace_id,
        )
        is WorkspaceRole.EDITOR
    )
    assert await service.can(
        oidc(recipient_user_id),
        SharePermission.EDIT,
        owner_user_id=owner_user_id,
        resource_tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id=resource_id,
    )
    assert not await service.can(
        oidc(outsider_user_id),
        SharePermission.VIEW,
        owner_user_id=owner_user_id,
        resource_tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id=resource_id,
    )
    with pytest.raises(WorkspaceNotFound):
        await service.resolve_workspace(oidc(outsider_user_id), workspace_id)


@pytest.mark.asyncio
async def test_audit_sink_appends_uuid_denials(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    denied_user_id = await create_user(session_factory, label="denied")
    identity = backend(session_factory)
    service = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
    )

    with pytest.raises(ResourceNotFound):
        await service.require(
            oidc(denied_user_id),
            SharePermission.VIEW,
            owner_user_id=owner_user_id,
            resource_tenant_id=TENANT_ID,
            resource_type="knowledge_collection",
            resource_id="kc_hidden",
        )

    async with scoped(session_factory) as session:
        rows = (
            await session.execute(
                select(audit_log.c.action, audit_log.c.actor_user_id)
            )
        ).all()
    assert ("authz.denied", denied_user_id) in rows


@pytest.mark.asyncio
async def test_memory_and_postgres_agree_on_direct_share_access(
    session_factory: SessionFactory,
) -> None:
    (
        identity,
        workspace_id,
        resource_id,
        owner_user_id,
        recipient_user_id,
        _outsider_user_id,
    ) = await arrange_identity_facts(session_factory)
    memory = MemoryIdentityStore()
    memory.add_workspace(workspace_id)
    memory.add_member(
        workspace_id,
        recipient_user_id,
        WorkspaceRole.EDITOR,
    )
    memory.add_share(
        recipient_user_id=recipient_user_id,
        resource_type="knowledge_collection",
        resource_id=resource_id,
        permission=SharePermission.EDIT,
        granted_by_user_id=owner_user_id,
    )

    for store in (memory, identity):
        service = AuthorizationService(
            members=store,
            shares=store,
            audit=MemoryIdentityStore(),
        )
        assert await service.can(
            oidc(recipient_user_id),
            SharePermission.EDIT,
            owner_user_id=owner_user_id,
            resource_tenant_id=TENANT_ID,
            resource_type="knowledge_collection",
            resource_id=resource_id,
        ), type(store).__name__


@pytest.mark.asyncio
async def test_revoke_shares_for_resource_clears_every_direct_grant(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    alice_user_id = await create_user(session_factory, label="alice")
    bob_user_id = await create_user(session_factory, label="bob")
    identity = backend(session_factory)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_doomed",
    )
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_survivor",
    )
    doomed = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_doomed",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=(
            (alice_user_id, SharePermission.VIEW),
            (bob_user_id, SharePermission.EDIT),
        ),
    )
    assert len(doomed) == 2
    (survivor,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_survivor",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((alice_user_id, SharePermission.VIEW),),
    )

    assert (
        await identity.revoke_shares_for_resource(
            tenant_id=TENANT_ID,
            resource_type="knowledge_collection",
            resource_id="kc_doomed",
            revoked_by_user_id=owner_user_id,
        )
        == 2
    )
    assert await identity.list_shares_for_resource(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_doomed",
    ) == ()
    remaining = await identity.list_shares_for_resource(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_survivor",
    )
    assert [record.id for record in remaining] == [survivor.id]


@pytest.mark.asyncio
async def test_consent_gates_direct_share_access(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    wrong_user_id = await create_user(session_factory, label="wrong")
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_consent",
    )
    identity = backend(session_factory)
    service = AuthorizationService(
        members=identity,
        shares=identity,
        audit=identity,
    )
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_consent",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
    )
    assert share.accepted_at is None

    async def can_view() -> bool:
        return await service.can(
            oidc(recipient_user_id),
            SharePermission.VIEW,
            owner_user_id=owner_user_id,
            resource_tenant_id=TENANT_ID,
            resource_type="knowledge_collection",
            resource_id="kc_consent",
        )

    assert not await can_view()
    assert (
        await identity.accept_share_by_id(
            tenant_id=TENANT_ID,
            share_id=share.id,
            recipient_user_id=wrong_user_id,
            owner_user_id=owner_user_id,
        )
        is None
    )
    accepted = await identity.accept_share_by_id(
        tenant_id=TENANT_ID,
        share_id=share.id,
        recipient_user_id=recipient_user_id,
        owner_user_id=owner_user_id,
    )
    assert accepted is not None and accepted.accepted_at is not None
    accepted_again = await identity.accept_share_by_id(
        tenant_id=TENANT_ID,
        share_id=share.id,
        recipient_user_id=recipient_user_id,
        owner_user_id=owner_user_id,
    )
    assert accepted_again == accepted
    assert await can_view()


@pytest.mark.asyncio
async def test_permission_cas_preserves_acceptance(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_cas",
    )
    identity = backend(session_factory)
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_cas",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
    )
    accepted = await identity.accept_share_by_id(
        tenant_id=TENANT_ID,
        share_id=share.id,
        recipient_user_id=recipient_user_id,
        owner_user_id=owner_user_id,
    )
    assert accepted is not None

    updated = await identity.update_share_permission(
        tenant_id=TENANT_ID,
        share_id=share.id,
        permission=SharePermission.EDIT,
        expected_revision=share.revision,
        actor_user_id=owner_user_id,
    )
    assert updated is not None
    assert updated.permission is SharePermission.EDIT
    assert updated.revision == share.revision + 1
    assert updated.accepted_at == accepted.accepted_at

    with pytest.raises(ShareConflict) as stale:
        await identity.update_share_permission(
            tenant_id=TENANT_ID,
            share_id=share.id,
            permission=SharePermission.VIEW,
            expected_revision=share.revision,
            actor_user_id=owner_user_id,
        )
    assert stale.value.current_revision == updated.revision


@pytest.mark.asyncio
async def test_inbox_and_outgoing_direct_share_repositories(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    alice_user_id = await create_user(session_factory, label="alice")
    bob_user_id = await create_user(session_factory, label="bob")
    identity = backend(session_factory)
    for collection_id in ("kc_inbox_a", "kc_inbox_b"):
        await create_collection(
            session_factory,
            owner_user_id=owner_user_id,
            collection_id=collection_id,
        )
    first_batch = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_inbox_a",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=(
            (alice_user_id, SharePermission.VIEW),
            (bob_user_id, SharePermission.VIEW),
        ),
    )
    (accepted_share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_inbox_b",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((alice_user_id, SharePermission.EDIT),),
    )
    await identity.accept_share_by_id(
        tenant_id=TENANT_ID,
        share_id=accepted_share.id,
        recipient_user_id=alice_user_id,
        owner_user_id=owner_user_id,
    )

    inbox = await identity.inbox_for_recipient(
        tenant_id=TENANT_ID,
        recipient_user_id=alice_user_id,
    )
    assert {record.resource_id for record in inbox} == {
        "kc_inbox_a",
        "kc_inbox_b",
    }
    assert len(
        await identity.list_active_shares(tenant_id=TENANT_ID)
    ) == 3

    alice_pending = next(
        record
        for record in first_batch
        if record.recipient_user_id == alice_user_id
    )
    await identity.revoke_share_by_id(
        tenant_id=TENANT_ID,
        share_id=alice_pending.id,
        revoked_by_user_id=alice_user_id,
        owner_user_id=owner_user_id,
    )
    inbox_after = await identity.inbox_for_recipient(
        tenant_id=TENANT_ID,
        recipient_user_id=alice_user_id,
    )
    assert {record.resource_id for record in inbox_after} == {"kc_inbox_b"}


@pytest.mark.asyncio
async def test_disabled_actor_cannot_revoke_direct_share(
    session_factory: SessionFactory,
) -> None:
    """The final revoke transaction locks and rechecks the current actor."""
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    identity = backend(session_factory)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_disabled_revoke",
    )
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_disabled_revoke",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
    )
    async with scoped(session_factory) as session:
        await session.execute(
            update(users)
            .where(users.c.id == owner_user_id)
            .values(disabled_at=func.now())
        )

    revoked = await identity.revoke_share_by_id(
        tenant_id=TENANT_ID,
        share_id=share.id,
        revoked_by_user_id=owner_user_id,
        owner_user_id=owner_user_id,
    )

    assert revoked is None
    retained = await identity.get_share(
        tenant_id=TENANT_ID, share_id=share.id
    )
    assert retained is not None
    assert retained.revoked_at is None


async def _exercise_membership_admin(
    store: Any,
    *,
    owner_user_id: uuid.UUID,
    member_user_id: uuid.UUID,
) -> None:
    """Run one UUID membership-admin contract against either backend."""
    workspace_id, _name = await store.create_workspace(
        tenant_id=TENANT_ID,
        name="Team",
        created_by_user_id=owner_user_id,
    )
    assert await store.list_all_workspaces(tenant_id=TENANT_ID) == (
        (workspace_id, "Team", owner_user_id, 1),
    )
    assert await store.assign_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=member_user_id,
        role=WorkspaceRole.EDITOR,
    )
    assert dict(
        await store.list_members(
            tenant_id=TENANT_ID,
            workspace_id=workspace_id,
        )
    ) == {
        owner_user_id: WorkspaceRole.OWNER,
        member_user_id: WorkspaceRole.EDITOR,
    }
    assert await store.set_existing_member_role(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=member_user_id,
        role=WorkspaceRole.VIEWER,
    )
    missing_member_id = uuid.uuid4()
    assert not await store.set_existing_member_role(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=missing_member_id,
        role=WorkspaceRole.EDITOR,
    )
    assert missing_member_id not in dict(
        await store.list_members(
            tenant_id=TENANT_ID,
            workspace_id=workspace_id,
        )
    )
    assert await store.rename_workspace(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        name="Renamed",
    )
    assert (await store.list_all_workspaces(tenant_id=TENANT_ID))[0][1] == (
        "Renamed"
    )
    assert await store.remove_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=member_user_id,
    )
    assert not await store.remove_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        user_id=member_user_id,
    )
    assert await store.list_members(
        tenant_id=TENANT_ID,
        workspace_id="not-a-uuid",
    ) is None
    assert not await store.assign_member(
        tenant_id=TENANT_ID,
        workspace_id=str(uuid.uuid4()),
        user_id=uuid.uuid4(),
        role=WorkspaceRole.VIEWER,
    )
    assert await store.delete_workspace(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        actor_user_id=owner_user_id,
    )
    assert not await store.delete_workspace(
        tenant_id=TENANT_ID,
        workspace_id=workspace_id,
        actor_user_id=owner_user_id,
    )


@pytest.mark.asyncio
async def test_membership_admin_repository_uuid_parity(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    member_user_id = await create_user(session_factory, label="member")
    stores = (MemoryIdentityStore(), backend(session_factory))
    for store in stores:
        await _exercise_membership_admin(
            store,
            owner_user_id=owner_user_id,
            member_user_id=member_user_id,
        )


@pytest.mark.asyncio
async def test_last_workspace_owner_cannot_be_removed_or_downgraded(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    identity = backend(session_factory)
    workspace_id, _name = await identity.create_workspace(
        tenant_id=TENANT_ID,
        name="Protected",
        created_by_user_id=owner_user_id,
    )

    with pytest.raises(LastWorkspaceOwnerError):
        await identity.assign_member(
            tenant_id=TENANT_ID,
            workspace_id=workspace_id,
            user_id=owner_user_id,
            role=WorkspaceRole.EDITOR,
        )
    with pytest.raises(LastWorkspaceOwnerError):
        await identity.remove_member(
            tenant_id=TENANT_ID,
            workspace_id=workspace_id,
            user_id=owner_user_id,
            actor_user_id=owner_user_id,
        )


@pytest.mark.asyncio
async def test_concurrent_direct_grants_leave_one_explicit_winner(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    identity = backend(session_factory)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_race",
    )

    async def grant(
        permission: SharePermission,
    ) -> tuple[ShareRecord, ...]:
        return await identity.create_shares(
            tenant_id=TENANT_ID,
            resource_type="knowledge_collection",
            resource_id="kc_race",
            owner_user_id=owner_user_id,
            granted_by_user_id=owner_user_id,
            invitees=((recipient_user_id, permission),),
        )

    results = await asyncio.gather(
        grant(SharePermission.VIEW),
        grant(SharePermission.EDIT),
        return_exceptions=True,
    )
    successes = [result for result in results if not isinstance(result, Exception)]
    conflicts = [result for result in results if isinstance(result, ShareConflict)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    active = await identity.list_shares_for_resource(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_race",
    )
    assert len(active) == 1


async def arrange_shared_workspace(
    factory: SessionFactory,
    *,
    workspace_count: int = 1,
) -> tuple[PostgresIdentityBackend, uuid.UUID, uuid.UUID, list[str]]:
    """Create two users who share *workspace_count* workspaces."""
    owner_user_id = await create_user(factory, label="owner")
    recipient_user_id = await create_user(factory, label="recipient")
    identity = backend(factory, restrict_to_workspace_members=True)
    workspace_ids: list[str] = []
    for index in range(workspace_count):
        workspace_id, _name = await identity.create_workspace(
            tenant_id=TENANT_ID,
            name=f"Shared {index}",
            created_by_user_id=owner_user_id,
        )
        assert await identity.assign_member(
            tenant_id=TENANT_ID,
            workspace_id=workspace_id,
            user_id=recipient_user_id,
            role=WorkspaceRole.VIEWER,
        )
        workspace_ids.append(workspace_id)
    return identity, owner_user_id, recipient_user_id, workspace_ids


@pytest.mark.asyncio
async def test_last_shared_workspace_revokes_pending_and_accepted_atomically(
    session_factory: SessionFactory,
) -> None:
    (
        identity,
        owner_user_id,
        recipient_user_id,
        workspace_ids,
    ) = await arrange_shared_workspace(session_factory)
    for collection_id in ("kc_pending", "kc_accepted"):
        await create_collection(
            session_factory,
            owner_user_id=owner_user_id,
            collection_id=collection_id,
        )
    (pending,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_pending",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
        restrict_to_members=True,
    )
    (accepted,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_accepted",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.EDIT),),
        restrict_to_members=True,
    )
    assert (
        await identity.accept_share_by_id(
            tenant_id=TENANT_ID,
            share_id=accepted.id,
            recipient_user_id=recipient_user_id,
            owner_user_id=owner_user_id,
            restrict_to_members=True,
        )
    ) is not None

    assert await identity.remove_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_ids[0],
        user_id=recipient_user_id,
        actor_user_id=owner_user_id,
    )

    async with scoped(session_factory) as session:
        membership_count = await session.scalar(
            select(func.count())
            .select_from(workspace_members)
            .where(workspace_members.c.user_id == recipient_user_id)
        )
        share_rows = (
            await session.execute(
                select(
                    resource_shares.c.id,
                    resource_shares.c.revoked_at,
                    resource_shares.c.revoked_by_user_id,
                ).where(
                    resource_shares.c.id.in_(
                        [uuid.UUID(pending.id), uuid.UUID(accepted.id)]
                    )
                )
            )
        ).all()
        revoke_audits = await session.scalar(
            select(func.count())
            .select_from(audit_log)
            .where(audit_log.c.action == "share.workspace_boundary_revoked")
        )
    assert membership_count == 0
    assert len(share_rows) == 2
    assert all(row.revoked_at is not None for row in share_rows)
    assert all(row.revoked_by_user_id == owner_user_id for row in share_rows)
    assert revoke_audits == 2


@pytest.mark.asyncio
async def test_second_shared_workspace_preserves_direct_share(
    session_factory: SessionFactory,
) -> None:
    (
        identity,
        owner_user_id,
        recipient_user_id,
        workspace_ids,
    ) = await arrange_shared_workspace(session_factory, workspace_count=2)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_retained",
    )
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_retained",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
        restrict_to_members=True,
    )

    assert await identity.remove_member(
        tenant_id=TENANT_ID,
        workspace_id=workspace_ids[0],
        user_id=recipient_user_id,
        actor_user_id=owner_user_id,
    )
    retained = await identity.get_share(
        tenant_id=TENANT_ID,
        share_id=share.id,
    )
    assert retained is not None
    assert retained.revoked_at is None
    assert await identity.workspace_ids_for(
        tenant_id=TENANT_ID,
        user_id=recipient_user_id,
    ) == (workspace_ids[1],)


@pytest.mark.asyncio
async def test_targeted_reconcile_does_not_lock_unrelated_share_resources(
    session_factory: SessionFactory,
) -> None:
    """A member change must not wait on an unrelated resource mutation."""
    affected_user_id = await create_user(session_factory, label="affected")
    other_owner_id = await create_user(session_factory, label="other-owner")
    other_recipient_id = await create_user(
        session_factory, label="other-recipient"
    )
    unrelated_owner_id = await create_user(
        session_factory, label="unrelated-owner"
    )
    unrelated_recipient_id = await create_user(
        session_factory, label="unrelated-recipient"
    )
    identity = backend(session_factory, restrict_to_workspace_members=True)
    for collection_id, owner_user_id in (
        ("kc_00_unrelated_locked", unrelated_owner_id),
        ("kc_10_owned_by_affected", affected_user_id),
        ("kc_20_recipient_affected", other_owner_id),
    ):
        await create_collection(
            session_factory,
            owner_user_id=owner_user_id,
            collection_id=collection_id,
        )
    (unrelated,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_00_unrelated_locked",
        owner_user_id=unrelated_owner_id,
        granted_by_user_id=unrelated_owner_id,
        invitees=((unrelated_recipient_id, SharePermission.VIEW),),
    )
    (owned_by_affected,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_10_owned_by_affected",
        owner_user_id=affected_user_id,
        granted_by_user_id=affected_user_id,
        invitees=((other_recipient_id, SharePermission.VIEW),),
    )
    (shared_to_affected,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_20_recipient_affected",
        owner_user_id=other_owner_id,
        granted_by_user_id=other_owner_id,
        invitees=((affected_user_id, SharePermission.VIEW),),
    )

    async with scoped(session_factory) as blocker:
        await blocker.execute(
            select(knowledge_collections.c.id)
            .where(knowledge_collections.c.id == "kc_00_unrelated_locked")
            .with_for_update()
        )
        async with scoped(session_factory) as reconciliation:
            await reconciliation.execute(
                text("SET LOCAL lock_timeout = '250ms'")
            )
            revoked = await identity._reconcile_workspace_shares(
                reconciliation,
                tenant_id=TENANT_ID,
                actor_user_id=affected_user_id,
                affected_user_ids={affected_user_id},
            )

    assert revoked == 2
    assert await identity.get_share(
        tenant_id=TENANT_ID, share_id=owned_by_affected.id
    ) is None
    assert await identity.get_share(
        tenant_id=TENANT_ID, share_id=shared_to_affected.id
    ) is None
    assert await identity.get_share(
        tenant_id=TENANT_ID, share_id=unrelated.id
    ) is not None


@pytest.mark.asyncio
async def test_workspace_delete_reconciles_direct_shares(
    session_factory: SessionFactory,
) -> None:
    (
        identity,
        owner_user_id,
        recipient_user_id,
        workspace_ids,
    ) = await arrange_shared_workspace(session_factory)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_workspace_delete",
    )
    (share,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_workspace_delete",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.EDIT),),
        restrict_to_members=True,
    )

    assert await identity.delete_workspace(
        tenant_id=TENANT_ID,
        workspace_id=workspace_ids[0],
        actor_user_id=owner_user_id,
    )
    assert await identity.list_members(
        tenant_id=TENANT_ID,
        workspace_id=workspace_ids[0],
    ) is None
    assert await identity.get_share(
        tenant_id=TENANT_ID,
        share_id=share.id,
    ) is None

    async with scoped(session_factory) as session:
        revoked = (
            await session.execute(
                select(
                    resource_shares.c.revoked_at,
                    resource_shares.c.revoked_by_user_id,
                ).where(resource_shares.c.id == uuid.UUID(share.id))
            )
        ).one()
    assert revoked.revoked_at is not None
    assert revoked.revoked_by_user_id == owner_user_id


@pytest.mark.asyncio
async def test_startup_reconcile_revokes_boundary_invalid_and_orphaned_shares(
    session_factory: SessionFactory,
) -> None:
    owner_user_id = await create_user(session_factory, label="owner")
    recipient_user_id = await create_user(session_factory, label="recipient")
    identity = backend(session_factory, restrict_to_workspace_members=True)
    await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id="kc_no_common_workspace",
    )
    (boundary_invalid,) = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id="kc_no_common_workspace",
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=((recipient_user_id, SharePermission.VIEW),),
    )
    orphaned_id = uuid.uuid4()
    async with scoped(session_factory) as session:
        await session.execute(
            insert(resource_shares).values(
                id=orphaned_id,
                tenant_id=TENANT_ID,
                recipient_user_id=recipient_user_id,
                resource_type="knowledge_collection",
                resource_id="kc_missing",
                permission=SharePermission.EDIT.value,
                granted_by_user_id=owner_user_id,
                accepted_at=func.now(),
            )
        )

    revoked = await identity.reconcile_workspace_shares(tenant_id=TENANT_ID)
    assert revoked == 2
    assert await identity.get_share(
        tenant_id=TENANT_ID,
        share_id=boundary_invalid.id,
    ) is None
    assert await identity.get_share(
        tenant_id=TENANT_ID,
        share_id=str(orphaned_id),
    ) is None
    async with scoped(session_factory) as session:
        rows = (
            await session.execute(
                select(resource_shares.c.resource_id).where(
                    resource_shares.c.id.in_(
                        [uuid.UUID(boundary_invalid.id), orphaned_id]
                    ),
                    resource_shares.c.revoked_at.isnot(None),
                )
            )
        ).all()
    assert {row.resource_id for row in rows} == {
        "kc_no_common_workspace",
        "kc_missing",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revoker", "accept_first", "expected"),
    [
        ("owner", False, "share.revoked"),
        ("owner", True, "share.revoked"),
        ("recipient", False, "share.declined"),
        ("recipient", True, "share.left"),
    ],
)
async def test_share_revocation_records_the_same_action_as_the_memory_twin(
    session_factory: SessionFactory,
    revoker: str,
    accept_first: bool,
    expected: str,
) -> None:
    """The Postgres path must answer WHO ended a share and HOW.

    The memory twin derives three distinct actions from the same facts. This
    path wrote one flat "share.removed" without a recipient, so a rights audit
    could not tell a decline from a revocation, and the actor was blank in the
    admin panel because the row bypassed the audit writer that stamps the
    pseudonym.
    """
    identity = backend(session_factory)
    owner_user_id = await create_user(session_factory, label="share-owner")
    recipient_user_id = await create_user(
        session_factory, label="share-recipient"
    )
    collection_id = await create_collection(
        session_factory,
        owner_user_id=owner_user_id,
        collection_id=f"kc_{expected}_{int(accept_first)}",
    )
    created = await identity.create_shares(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id=collection_id,
        owner_user_id=owner_user_id,
        granted_by_user_id=owner_user_id,
        invitees=[(recipient_user_id, SharePermission.VIEW)],
    )
    if accept_first:
        assert await identity.accept_share_by_id(
            tenant_id=TENANT_ID,
            share_id=created[0].id,
            recipient_user_id=recipient_user_id,
            owner_user_id=owner_user_id,
        ) is not None

    revoked_by = owner_user_id if revoker == "owner" else recipient_user_id
    assert await identity.revoke_share_by_id(
        tenant_id=TENANT_ID,
        share_id=created[0].id,
        revoked_by_user_id=revoked_by,
        owner_user_id=owner_user_id,
    ) is not None

    rows, _ = await identity.list_audit_entries(
        tenant_id=TENANT_ID,
        resource_type="knowledge_collection",
        resource_id=collection_id,
        limit=20,
    )
    actions = [row["action"] for row in rows]
    assert expected in actions
    assert "share.removed" not in actions
    row = next(row for row in rows if row["action"] == expected)
    assert row["detail"]["recipient_user_id"] == str(recipient_user_id)
    # Every share row must carry the actor reference the panel filters on.
    assert row["actor_pseudonym"], row
    grant_row = next(row for row in rows if row["action"] == "share.granted")
    assert grant_row["actor_pseudonym"], grant_row
