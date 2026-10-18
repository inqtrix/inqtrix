"""Erasing an account, proven against a real PostgreSQL.

The claims this suite has to settle cannot be made against a stub,
because every one of them is a property of the schema and of the steps
that run against it under the application role: that the foreign keys
admit each delete, that the cascades reach the children nobody names, that
rows belonging to other people survive with their pointer moved or
cleared, that the audit trail keeps its pseudonymous actor, that the rows
go without the tenant lock, and that an interrupted erasure resumes.

The deletion machine itself is not under test here: a stand-in deletes
the aggregate rows and records when it was asked, so the order of the
stages is visible.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
import sqlalchemy as sa

from inqtrix.auth.lifecycle import LoginCommand, UserDisabledError, UserLifecycleStatus
from inqtrix.auth.sessions import AuthSession
from inqtrix.runs.deletion_operations import DeletionStage
from inqtrix.services.user_erasure import DELETED_ACCOUNT_ISSUER
from inqtrix.services.user_erasure_executor import (
    AggregateRef,
    ErasureResiduals,
    UserErasure,
    ensure_deleted_account,
)
from inqtrix.services.user_erasure_inventory import _tables, collect_inventory
from inqtrix.storage.auth_postgres import PostgresUserDirectory, lock_tenant_security
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.user_lifecycle_postgres import PostgresUserLifecycleTransaction
from inqtrix.sync_bridge import run_coro_sync
from tests.storage._canonical_users import ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
_NOW = 1_700_000_000.0
_HASH = "0" * 64

#: Every table these scenarios write, children before parents. A row the
#: list misses keeps its user alive and fails the teardown loudly.
_CLEANUP_ORDER = (
    "editor_collaboration_comment_messages",
    "editor_collaboration_comment_threads",
    "editor_collaboration_updates",
    "editor_documents",
    "runs",
    "workspaces",
    "knowledge_collections",
    "auth_sessions",
    "personal_access_tokens",
    "local_credentials",
    "account_preferences",
    "user_events",
    "user_authorization_generations",
    "audit_log",
    "tenant_security_state",
    "users",
)


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest.fixture()
def factory():
    # No pool: the scenarios and the erasure both run on the sync bridge's
    # loop, and a pooled asyncpg connection must never cross loops.
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    yield build_session_factory(engine)
    run_coro_sync(engine.dispose())


@pytest.fixture()
def tenant(factory):
    """A tenant of its own, removed again after the scenario.

    The last-admin invariant counts per tenant, and the erasure commits in
    its own transactions, so nothing can be rolled back: the teardown
    deletes what the scenario wrote instead.
    """
    tenant_id = f"t-{uuid.uuid4().hex[:12]}"
    yield tenant_id

    async def cleanup(session):
        tables = _tables()
        for name in _CLEANUP_ORDER:
            table = tables[name]
            await session.execute(
                sa.delete(table).where(table.c.tenant_id == tenant_id)
            )

    _transaction(factory, cleanup)


def _transaction(factory, body):
    """Run ``body(session)`` in one committed owner transaction."""

    async def run():
        async with factory() as session:
            async with session.begin():
                return await body(session)

    return run_coro_sync(run())


def _count(factory, table: str, **where) -> int:
    async def body(session):
        t = _tables()[table]
        query = sa.select(sa.func.count()).select_from(t)
        for column, value in where.items():
            query = query.where(t.c[column] == value)
        return int((await session.execute(query)).scalar() or 0)

    return _transaction(factory, body)


def _user(factory, user_id: uuid.UUID):
    async def body(session):
        users = _tables()["users"]
        return (
            await session.execute(sa.select(users).where(users.c.id == user_id))
        ).first()

    return _transaction(factory, body)


def _lifecycle(factory) -> PostgresUserLifecycleTransaction:
    return PostgresUserLifecycleTransaction(
        session_factory=factory, app_role=APP_ROLE
    )


class _Crash(RuntimeError):
    """A simulated worker death between two stages."""


class _Handle:
    """The deletion operation's handle, recording what the erasure did."""

    def __init__(self, operation_id: str, *, crash_after: DeletionStage | None = None):
        self.operation_id = operation_id
        self.stages: list[DeletionStage] = []
        self.checkpoints: list[tuple[DeletionStage, int, int]] = []
        self.completed = False
        self._crash_after = crash_after

    def assert_current(self) -> None:
        pass

    def progress(self, stage, *, completed_items, total_items) -> None:
        self.stages.append(stage)
        self.checkpoints.append((stage, completed_items, total_items))
        if stage is self._crash_after:
            raise _Crash(stage.value)

    def complete(self) -> None:
        self.completed = True


class _Aggregates:
    """Stands in for the deletion machine: deletes what it is handed."""

    def __init__(self, factory, refs=(), *, deleter=None):
        self._factory = factory
        self._refs = tuple(refs)
        self._deleter = deleter
        self.deleted: list[AggregateRef] = []

    def pending(self, *, tenant_id, user_id):
        return tuple(ref for ref in self._refs if ref not in self.deleted)

    def delete(self, ref, *, tenant_id, user_id) -> None:
        if self._deleter is not None:
            _transaction(self._factory, lambda session: self._deleter(session, ref))
        self.deleted.append(ref)


def _erasure(factory, aggregates=None, *, batch_size: int = 500) -> UserErasure:
    return UserErasure(
        lifecycle=_lifecycle(factory),
        session_factory=factory,
        app_role=APP_ROLE,
        aggregates=aggregates or _Aggregates(factory),
        batch_size=batch_size,
    )


def _people(factory, tenant: str, *, colleagues: int = 0):
    """An administrator, the account to erase, and its colleagues."""
    admin, subject = uuid.uuid4(), uuid.uuid4()
    colleague_ids = [uuid.uuid4() for _ in range(colleagues)]

    async def body(session):
        await ensure_canonical_users(
            session, [admin, subject, *colleague_ids], tenant_id=tenant
        )
        await _promote(session, admin)

    _transaction(factory, body)
    return admin, subject, colleague_ids


async def _promote(session, user_id: uuid.UUID) -> None:
    users = _tables()["users"]
    await session.execute(
        sa.update(users).where(users.c.id == user_id).values(instance_role="admin")
    )


def _begin(factory, tenant: str, admin: uuid.UUID, subject: uuid.UUID) -> str:
    outcome, report = run_coro_sync(
        _lifecycle(factory).begin_erasure(
            tenant_id=tenant,
            user_id=subject,
            actor_user_id=admin,
            operation_id=f"del_{uuid.uuid4().hex}",
        )
    )
    assert outcome is UserLifecycleStatus.UPDATED, (outcome, report)
    return report["operation_id"]


def _erase(factory, tenant: str, admin: uuid.UUID, subject: uuid.UUID, aggregates=None):
    handle = _Handle(_begin(factory, tenant, admin, subject))
    _erasure(factory, aggregates).execute(
        handle, tenant_id=tenant, user_id=subject, actor_user_id=admin
    )
    return handle


async def _document(session, *, tenant: str, document_id: str, owner: uuid.UUID):
    await session.execute(
        sa.insert(_tables()["editor_documents"]).values(
            id=document_id,
            tenant_id=tenant,
            title="fixture",
            content_markdown="",
            created_by_user_id=owner,
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


async def _thread(session, *, tenant: str, document_id: str, author: uuid.UUID):
    thread_id = uuid.uuid4()
    await session.execute(
        sa.insert(_tables()["editor_collaboration_comment_threads"]).values(
            id=thread_id,
            tenant_id=tenant,
            document_id=document_id,
            generation=1,
            revision=1,
            anchor={},
            created_at=_NOW,
            updated_at=_NOW,
            last_command_id=uuid.uuid4(),
            last_command_payload_hash=_HASH,
            last_command_kind="create",
            created_by_user_id=author,
        )
    )
    return thread_id


async def _collection(session, *, tenant: str, owner: uuid.UUID) -> str:
    collection_id = f"kc_{uuid.uuid4().hex[:12]}"
    await session.execute(
        sa.insert(_tables()["knowledge_collections"]).values(
            id=collection_id,
            tenant_id=tenant,
            name="fixture",
            embedding_model="probe",
            embedding_dim=8,
            created_at=_NOW,
            created_by_user_id=owner,
        )
    )
    return collection_id


async def _scalar(session, table: str, column: str, **where):
    t = _tables()[table]
    query = sa.select(t.c[column])
    for name, value in where.items():
        query = query.where(t.c[name] == value)
    return (await session.execute(query)).scalar_one()


def test_the_account_and_its_own_content_are_gone(factory, tenant) -> None:
    admin, subject, _ = _people(factory, tenant)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"

    async def content(session):
        await _document(session, tenant=tenant, document_id=document_id, owner=subject)
        await session.execute(
            sa.insert(_tables()["account_preferences"]).values(
                user_id=subject, tenant_id=tenant, updated_at=_NOW
            )
        )

    _transaction(factory, content)
    handle = _erase(factory, tenant, admin, subject)

    assert handle.completed is True
    assert handle.stages == [
        DeletionStage.AGGREGATES_REMOVED,
        DeletionStage.ROWS_REMOVED,
        DeletionStage.RESIDUALS_VERIFIED,
    ]
    assert _user(factory, subject) is None
    assert _count(factory, "editor_documents", id=document_id) == 0
    assert _count(factory, "account_preferences", user_id=subject) == 0

    async def audit_trail(session):
        audit = _tables()["audit_log"]
        rows = await session.execute(
            sa.select(audit.c.action, audit.c.actor_user_id).where(
                audit.c.tenant_id == tenant,
                audit.c.resource_id == str(subject),
            )
        )
        return {(row.action, row.actor_user_id) for row in rows}

    trail = _transaction(factory, audit_trail)
    assert ("user.erasure_started", admin) in trail
    assert ("user.erased", admin) in trail


def test_the_operation_reports_every_aggregate_as_it_goes(factory, tenant) -> None:
    """A large account shows how far its erasure got, not one long silence
    until its slowest stage closes."""
    admin, subject, _ = _people(factory, tenant)
    refs = [AggregateRef("agent_session", (f"session-{i}",)) for i in range(2)]

    handle = _erase(factory, tenant, admin, subject, _Aggregates(factory, refs))

    assert handle.checkpoints == [
        (DeletionStage.AGGREGATES_REMOVED, 1, 4),
        (DeletionStage.AGGREGATES_REMOVED, 2, 4),
        (DeletionStage.ROWS_REMOVED, 3, 4),
        (DeletionStage.RESIDUALS_VERIFIED, 4, 4),
    ]


def test_the_audit_trail_keeps_its_pseudonymous_actor(factory, tenant) -> None:
    """The point of the whole design: history survives, the person does not."""
    admin, subject, _ = _people(factory, tenant)
    pseudonym = "usr_feedfacefeedface"

    async def audit_row(session):
        await session.execute(
            sa.insert(_tables()["audit_log"]).values(
                tenant_id=tenant,
                actor_user_id=subject,
                action="probe.action",
                resource_type="probe",
                resource_id="probe",
                actor_pseudonym=pseudonym,
            )
        )

    _transaction(factory, audit_row)
    _erase(factory, tenant, admin, subject)

    async def read(session):
        audit = _tables()["audit_log"]
        return (
            await session.execute(
                sa.select(audit.c.actor_user_id, audit.c.actor_pseudonym).where(
                    audit.c.tenant_id == tenant, audit.c.action == "probe.action"
                )
            )
        ).first()

    row = _transaction(factory, read)
    assert row is not None, "the audit row must survive the erasure"
    assert row.actor_user_id is None, "the person must be unresolvable"
    assert row.actor_pseudonym == pseudonym, "who-did-what must stay filterable"


def test_a_colleagues_message_survives_with_the_mention_stripped(
    factory, tenant
) -> None:
    """A mention is not content: the message belongs to whoever wrote it."""
    admin, subject, (colleague,) = _people(factory, tenant, colleagues=1)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    shared, only = uuid.uuid4(), uuid.uuid4()

    async def content(session):
        await _document(session, tenant=tenant, document_id=document_id, owner=colleague)
        thread_id = await _thread(
            session, tenant=tenant, document_id=document_id, author=colleague
        )
        for message_id, mentions in (
            (shared, [str(subject), str(colleague)]),
            (only, [str(subject)]),
        ):
            await session.execute(
                sa.insert(_tables()["editor_collaboration_comment_messages"]).values(
                    id=message_id,
                    tenant_id=tenant,
                    document_id=document_id,
                    thread_id=thread_id,
                    revision=1,
                    author_user_id=colleague,
                    body_markdown="fixture",
                    mention_user_ids=mentions,
                    created_at=_NOW,
                    last_command_id=uuid.uuid4(),
                    last_command_payload_hash=_HASH,
                    last_command_kind="create",
                )
            )

    _transaction(factory, content)
    _erase(factory, tenant, admin, subject)

    def mentions(message_id):
        return _transaction(
            factory,
            lambda session: _scalar(
                session,
                "editor_collaboration_comment_messages",
                "mention_user_ids",
                id=message_id,
            ),
        )

    assert mentions(shared) == [str(colleague)], (
        "stripping one mention must not disturb the others"
    )
    assert mentions(only) == [], "the last mention leaves an empty array"
    assert _count(factory, "editor_documents", id=document_id) == 1


def test_a_decision_on_somebody_elses_run_keeps_the_run(factory, tenant) -> None:
    admin, subject, (owner,) = _people(factory, tenant, colleagues=1)
    run_id = f"run_{uuid.uuid4().hex}"

    async def content(session):
        await session.execute(
            sa.insert(_tables()["runs"]).values(
                run_id=run_id,
                tenant_id=tenant,
                question="fixture",
                created_at=_NOW,
                created_by_user_id=owner,
                execution_actor_user_id=subject,
            )
        )

    _transaction(factory, content)
    _erase(factory, tenant, admin, subject)

    async def read(session):
        runs = _tables()["runs"]
        return (
            await session.execute(
                sa.select(
                    runs.c.created_by_user_id, runs.c.execution_actor_user_id
                ).where(runs.c.run_id == run_id)
            )
        ).first()

    row = _transaction(factory, read)
    assert row is not None, "another person's run must survive"
    assert row.created_by_user_id == owner
    assert row.execution_actor_user_id is None


def test_a_workspace_refuses_the_start_with_the_reason_the_preview_shows(
    factory, tenant
) -> None:
    """Preview and start ask one function, so they cannot disagree."""
    admin, subject, _ = _people(factory, tenant)

    async def content(session):
        await session.execute(
            sa.insert(_tables()["workspaces"]).values(
                id=uuid.uuid4(),
                tenant_id=tenant,
                name="shared workspace",
                created_by_user_id=subject,
            )
        )

    _transaction(factory, content)
    preview = _transaction(
        factory, lambda session: collect_inventory(session, user_id=subject)
    )
    outcome, report = run_coro_sync(
        _lifecycle(factory).begin_erasure(
            tenant_id=tenant,
            user_id=subject,
            actor_user_id=admin,
            operation_id="del_refused",
        )
    )

    assert outcome is UserLifecycleStatus.REFUSED
    assert report["refused"] == list(preview.blockers)
    assert "workspaces" in report["refused"][0]
    assert _user(factory, subject).erasure_operation_id is None, (
        "a refused start changes nothing"
    )


def test_data_outside_postgres_no_longer_blocks_the_start(factory, tenant) -> None:
    """It is deleted through its own surface first instead."""
    admin, subject, _ = _people(factory, tenant)
    _transaction(
        factory, lambda session: _collection(session, tenant=tenant, owner=subject)
    )

    preview = _transaction(
        factory, lambda session: collect_inventory(session, user_id=subject)
    )

    assert preview.blockers == ()
    assert preview.delegated_rows == 1
    assert _begin(factory, tenant, admin, subject)


def test_data_outside_postgres_goes_through_its_surface_before_any_row(
    factory, tenant
) -> None:
    """The rows are the map to the blobs and vectors: read the map first."""
    admin, subject, _ = _people(factory, tenant)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"

    async def content(session):
        await _document(session, tenant=tenant, document_id=document_id, owner=subject)
        return await _collection(session, tenant=tenant, owner=subject)

    collection_id = _transaction(factory, content)
    rows_when_deleted: list[int] = []

    async def deleter(session, ref):
        rows_when_deleted.append(
            int(
                (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(_tables()["editor_documents"])
                        .where(_tables()["editor_documents"].c.id == document_id)
                    )
                ).scalar()
            )
        )
        collections = _tables()["knowledge_collections"]
        await session.execute(
            sa.delete(collections).where(collections.c.id == ref.target_ids[0])
        )

    aggregates = _Aggregates(
        factory,
        [AggregateRef("knowledge_collection", (collection_id,))],
        deleter=deleter,
    )
    handle = _erase(factory, tenant, admin, subject, aggregates)

    assert handle.completed is True
    assert [ref.target_ids for ref in aggregates.deleted] == [(collection_id,)]
    assert rows_when_deleted == [1], (
        "the aggregate must go while the account's rows are still there"
    )
    assert _user(factory, subject) is None


def test_an_aggregate_nobody_deleted_is_named_and_left_alone(factory, tenant) -> None:
    """Deleting its row here would strand its vectors outside PostgreSQL."""
    admin, subject, _ = _people(factory, tenant)
    collection_id = _transaction(
        factory, lambda session: _collection(session, tenant=tenant, owner=subject)
    )
    handle = _Handle(_begin(factory, tenant, admin, subject))

    with pytest.raises(ErasureResiduals) as residual:
        _erasure(factory).execute(
            handle, tenant_id=tenant, user_id=subject, actor_user_id=admin
        )

    assert "knowledge_collections.created_by_user_id" in str(residual.value)
    assert _count(factory, "knowledge_collections", id=collection_id) == 1
    assert _user(factory, subject) is not None
    assert handle.completed is False


def test_work_in_somebody_elses_document_moves_to_the_placeholder(
    factory, tenant
) -> None:
    """The colleague's document keeps its change chain; nobody living is
    credited with the departed account's edits and resolutions."""
    admin, subject, (owner,) = _people(factory, tenant, colleagues=1)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"

    async def content(session):
        await _document(session, tenant=tenant, document_id=document_id, owner=owner)
        await session.execute(
            sa.insert(_tables()["editor_collaboration_updates"]).values(
                tenant_id=tenant,
                document_id=document_id,
                generation=1,
                sequence=1,
                update_bytes=b"\x00",
                update_hash=_HASH,
                actor_kind="human",
                change_kind="direct",
                actor_user_id=subject,
                created_at=_NOW,
            )
        )
        thread_id = await _thread(
            session, tenant=tenant, document_id=document_id, author=owner
        )
        threads = _tables()["editor_collaboration_comment_threads"]
        await session.execute(
            sa.update(threads)
            .where(threads.c.id == thread_id)
            .values(status="resolved", resolved_by_user_id=subject, resolved_at=_NOW)
        )
        return thread_id

    thread_id = _transaction(factory, content)
    handle = _erase(factory, tenant, admin, subject)

    async def read(session):
        users = _tables()["users"]
        placeholder = (
            await session.execute(
                sa.select(users.c.id, users.c.disabled_at).where(
                    users.c.tenant_id == tenant,
                    users.c.issuer == DELETED_ACCOUNT_ISSUER,
                )
            )
        ).one()
        actor = await _scalar(
            session,
            "editor_collaboration_updates",
            "actor_user_id",
            document_id=document_id,
        )
        resolver = await _scalar(
            session,
            "editor_collaboration_comment_threads",
            "resolved_by_user_id",
            id=thread_id,
        )
        return placeholder, actor, resolver

    placeholder, actor, resolver = _transaction(factory, read)
    assert handle.completed is True
    assert placeholder.disabled_at is not None, "the placeholder never signs in"
    assert actor == placeholder.id
    assert resolver == placeholder.id
    assert _count(factory, "editor_documents", id=document_id) == 1
    assert _user(factory, subject) is None


def test_the_rows_go_without_holding_the_tenant_lock(factory, tenant) -> None:
    """Sign-ins and every other identity change take the tenant lock, so a
    large account must never hold it while its rows are deleted."""
    admin, subject, _ = _people(factory, tenant)

    async def content(session):
        for _ in range(3):
            await _document(
                session,
                tenant=tenant,
                document_id=f"doc_{uuid.uuid4().hex[:12]}",
                owner=subject,
            )

    _transaction(factory, content)
    acquired: list[bool] = []

    class Probing(UserErasure):
        async def _delete_batch(self, tenant_id, table, predicate, limit):
            if table.name == "editor_documents" and not acquired:
                async with factory() as other:
                    async with other.begin():
                        await other.execute(sa.text("SET LOCAL lock_timeout = '1s'"))
                        await lock_tenant_security(other, tenant_id)
                        acquired.append(True)
            return await super()._delete_batch(tenant_id, table, predicate, limit)

    erasure = Probing(
        lifecycle=_lifecycle(factory),
        session_factory=factory,
        app_role=APP_ROLE,
        aggregates=_Aggregates(factory),
        batch_size=1,
    )
    handle = _Handle(_begin(factory, tenant, admin, subject))
    erasure.execute(handle, tenant_id=tenant, user_id=subject, actor_user_id=admin)

    assert acquired == [True]
    assert _user(factory, subject) is None


def test_an_interrupted_erasure_resumes_where_it_stopped(factory, tenant) -> None:
    admin, subject, _ = _people(factory, tenant)
    document_id = f"doc_{uuid.uuid4().hex[:12]}"
    _transaction(
        factory,
        lambda session: _document(
            session, tenant=tenant, document_id=document_id, owner=subject
        ),
    )
    operation_id = _begin(factory, tenant, admin, subject)

    with pytest.raises(_Crash):
        _erasure(factory).execute(
            _Handle(operation_id, crash_after=DeletionStage.ROWS_REMOVED),
            tenant_id=tenant,
            user_id=subject,
            actor_user_id=admin,
        )
    assert _count(factory, "editor_documents", id=document_id) == 0
    assert _user(factory, subject) is not None

    resumed = _Handle(operation_id)
    _erasure(factory).execute(
        resumed, tenant_id=tenant, user_id=subject, actor_user_id=admin
    )
    assert resumed.completed is True
    assert _user(factory, subject) is None

    # The account went but the terminal write was lost: finishing is all
    # that is left, without an error.
    finished = _Handle(operation_id)
    _erasure(factory).execute(
        finished, tenant_id=tenant, user_id=subject, actor_user_id=admin
    )
    assert finished.completed is True


def test_the_start_cuts_the_account_off_and_keeps_it_off(factory, tenant) -> None:
    admin, subject, _ = _people(factory, tenant)
    session_id, token_id = uuid.uuid4().hex, f"pat_{uuid.uuid4().hex}"
    login_subject = f"local-{subject.hex}"

    async def ways_in(session):
        tables = _tables()
        await session.execute(
            sa.update(tables["users"])
            .where(tables["users"].c.id == subject)
            .values(disabled_at=sa.func.now())
        )
        await session.execute(
            sa.insert(tables["auth_sessions"]).values(
                id=session_id,
                tenant_id=tenant,
                user_id=subject,
                issuer="local",
                subject=login_subject,
                csrf_random="0" * 32,
                created_at=_NOW,
                expires_at=time.time() + 3600,
            )
        )
        await session.execute(
            sa.insert(tables["personal_access_tokens"]).values(
                token_id=token_id,
                tenant_id=tenant,
                owner_user_id=subject,
                name="fixture",
                secret_hmac=_HASH,
                created_at=_NOW,
            )
        )
        await session.execute(
            sa.insert(tables["local_credentials"]).values(
                user_id=subject,
                tenant_id=tenant,
                email=f"{login_subject}@example.com",
                subject=login_subject,
                password_hash="fixture",
                created_at=_NOW,
            )
        )

    _transaction(factory, ways_in)
    lifecycle = _lifecycle(factory)

    operation_id = _begin(factory, tenant, admin, subject)

    row = _user(factory, subject)
    assert row.erasure_operation_id == operation_id
    assert row.disabled_at is None, (
        "the deletion machine acts as the owner, so the account must count "
        "as active for resource authorization"
    )
    assert _count(factory, "auth_sessions", id=session_id) == 0
    revoked_at = _transaction(
        factory,
        lambda session: _scalar(
            session, "personal_access_tokens", "revoked_at", token_id=token_id
        ),
    )
    password_off = _transaction(
        factory,
        lambda session: _scalar(
            session, "local_credentials", "disabled_at", user_id=subject
        ),
    )
    assert revoked_at is not None
    assert password_off is not None

    again = run_coro_sync(
        lifecycle.begin_erasure(
            tenant_id=tenant,
            user_id=subject,
            actor_user_id=admin,
            operation_id="del_second_click",
        )
    )
    assert again == (UserLifecycleStatus.UPDATED, {"operation_id": operation_id})
    for change in (
        lifecycle.set_disabled(
            tenant_id=tenant, user_id=subject, disabled_at=None, actor_user_id=admin
        ),
        lifecycle.set_role(
            tenant_id=tenant, user_id=subject, role="admin", actor_user_id=admin
        ),
    ):
        assert run_coro_sync(change) is UserLifecycleStatus.ERASING
    with pytest.raises(UserDisabledError):
        run_coro_sync(
            lifecycle.provision_login(
                LoginCommand(
                    tenant_id=tenant,
                    issuer=row.issuer,
                    subject=row.subject,
                    email=row.email,
                    email_verified=True,
                    display_name=None,
                    session=AuthSession(
                        id=uuid.uuid4().hex,
                        user_id=subject,
                        issuer=row.issuer,
                        subject=row.subject,
                        email=row.email,
                        display_name=None,
                        groups=(),
                        csrf_random="0" * 32,
                        created_at=time.time(),
                        expires_at=time.time() + 60,
                    ),
                )
            )
        )


def test_an_admin_being_erased_is_no_longer_an_administrator(
    factory, tenant
) -> None:
    """Neither for the last-admin invariant nor for the admin wake list."""
    admin, erasing, (colleague,) = _people(factory, tenant, colleagues=1)
    _transaction(factory, lambda session: _promote(session, erasing))
    _begin(factory, tenant, admin, erasing)
    lifecycle = _lifecycle(factory)

    demoted = run_coro_sync(
        lifecycle.set_role(
            tenant_id=tenant, user_id=admin, role="user", actor_user_id=admin
        )
    )
    assert demoted is UserLifecycleStatus.LAST_ADMIN

    woken_before = _count(factory, "user_events", target_user_id=erasing)
    disabled = run_coro_sync(
        lifecycle.set_disabled(
            tenant_id=tenant,
            user_id=colleague,
            disabled_at=time.time(),
            actor_user_id=admin,
        )
    )

    assert disabled is UserLifecycleStatus.UPDATED
    assert _count(factory, "user_events", target_user_id=erasing) == woken_before, (
        "an account being erased must not be woken as an administrator: the "
        "new event would name it after its rows were removed"
    )


def test_the_placeholder_never_appears_in_the_admin_listing(factory, tenant) -> None:
    admin, subject, _ = _people(factory, tenant)

    async def make(session):
        return await ensure_deleted_account(session, tenant_id=tenant)

    placeholder = _transaction(factory, make)
    again = _transaction(factory, make)
    listed = run_coro_sync(
        PostgresUserDirectory(session_factory=factory, app_role=APP_ROLE).list_users(
            tenant_id=tenant
        )
    )

    assert again == placeholder, "one placeholder per tenant"
    assert {user.user_id for user in listed} == {admin, subject}
