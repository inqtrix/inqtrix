"""The account erasure as a deletion operation, against PostgreSQL.

Two properties of the deletion machine carry the erasure: one operation
per account, registered under the id the account was marked with when it
was cut off, and aggregate deletions the erasure registers for itself
without handing them to a worker. The erasure's port onto the machine
also has to find every aggregate the account still owns, in the order
they must go.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

import pytest
import sqlalchemy as sa

from inqtrix.runs.deletion_operations import (
    DeletionOperationConflict,
    DeletionOperationStatus,
    DeletionStage,
    DeletionTargetKind,
)
from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.services.user_erasure_aggregates import DeletionMachineAggregates
from inqtrix.services.user_erasure_executor import AggregateRef
from inqtrix.services.user_erasure_inventory import _tables
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.migrate import run_migrations
from inqtrix.sync_bridge import run_coro_sync
from tests.storage._canonical_users import ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
#: The deletion store writes its terminal states in the default tenant.
TENANT = "default"
_NOW = 1_700_000_000.0

#: Every table the enumeration scenario writes, children before parents.
_AGGREGATE_TABLES = (
    "source_lifecycles",
    "knowledge_documents",
    "knowledge_collections",
    "vector_index_records",
    "asset_records",
    "asset_sections",
    "runs",
    "agent_sessions",
    "knowledge_sessions",
    "users",
)


class _RecordingQueue:
    """The worker queue, recording what was handed to it."""

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, *, operation_id: str, tenant_id: str) -> None:
        del tenant_id
        self.enqueued.append(operation_id)

    def ack(self, message_id: str) -> None:
        del message_id


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest.fixture()
def factory():
    engine = build_engine(TEST_DATABASE_URL, null_pool=True)
    yield build_session_factory(engine)
    run_coro_sync(engine.dispose())


def _transaction(factory, body):
    async def run():
        async with factory() as session:
            async with session.begin():
                return await body(session)

    return run_coro_sync(run())


@pytest.fixture()
def people(factory):
    """Two administrators and the account they erase, removed afterwards
    together with every receipt, event and audit row naming them."""
    ids = [uuid.uuid4() for _ in range(3)]
    _transaction(
        factory, lambda session: ensure_canonical_users(session, ids, tenant_id=TENANT)
    )
    yield ids

    async def cleanup(session):
        t = _tables()
        operations = t["deletion_operations"]
        theirs = sa.select(operations.c.operation_id).where(
            operations.c.created_by_user_id.in_(ids)
        )
        events = t["deletion_operation_events"]
        await session.execute(
            sa.delete(events).where(events.c.operation_id.in_(theirs))
        )
        await session.execute(
            sa.delete(operations).where(operations.c.created_by_user_id.in_(ids))
        )
        for table, column in (
            ("user_events", "target_user_id"),
            ("user_authorization_generations", "user_id"),
            ("audit_log", "actor_user_id"),
            ("users", "id"),
        ):
            await session.execute(
                sa.delete(t[table]).where(t[table].c[column].in_(ids))
            )

    _transaction(factory, cleanup)


@pytest.fixture()
def tenant(factory):
    """A tenant of its own for the enumeration, removed afterwards."""
    tenant_id = f"t-{uuid.uuid4().hex[:12]}"
    yield tenant_id

    async def cleanup(session):
        tables = _tables()
        for name in _AGGREGATE_TABLES:
            table = tables[name]
            await session.execute(
                sa.delete(table).where(table.c.tenant_id == tenant_id)
            )

    _transaction(factory, cleanup)


@pytest.fixture()
def queue():
    return _RecordingQueue()


@pytest.fixture()
def store(queue):
    store = PostgresDeletionOperationStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        max_concurrent=1,
        completed_ttl_seconds=3600,
        worker_id="erasure-operation-test",
    )
    yield store
    store.close()


def _operation_id() -> str:
    """Operation ids are global keys, so every scenario mints its own."""
    return f"del_{uuid.uuid4().hex}"


def _submit(store, *, admin, account, operation_id, dispatch=True):
    return store.submit(
        target_kind=DeletionTargetKind.USER,
        target_id=str(account),
        manifest=(),
        tenant_id=TENANT,
        created_by_user_id=admin,
        workspace_id=None,
        work=lambda handle: None,
        total_items=2,
        dispatch=dispatch,
        operation_id=operation_id,
    )


def test_the_schema_accepts_every_kind_and_stage_the_code_can_write(factory) -> None:
    """A kind or stage added without its migration fails here instead of as
    a CHECK violation on the first deletion that reaches it."""

    async def read(session):
        rows = await session.execute(
            sa.text(
                "SELECT conname, pg_get_constraintdef(oid) AS definition "
                "FROM pg_constraint WHERE conrelid = "
                "'deletion_operations'::regclass AND conname IN "
                "('ck_deletion_operations_target_kind', "
                "'ck_deletion_operations_stage')"
            )
        )
        return {
            row.conname: set(re.findall(r"'([a-z_]+)'::text", row.definition))
            for row in rows
        }

    accepted = _transaction(factory, read)

    assert {kind.value for kind in DeletionTargetKind} <= accepted[
        "ck_deletion_operations_target_kind"
    ]
    assert {stage.value for stage in DeletionStage} <= accepted[
        "ck_deletion_operations_stage"
    ]


def test_an_erasure_registers_under_the_id_the_account_was_marked_with(
    people, store, queue
) -> None:
    admin, _other, account = people
    marked = _operation_id()

    summary = _submit(store, admin=admin, account=account, operation_id=marked)

    assert summary["operation_id"] == marked
    assert summary["status"] == DeletionOperationStatus.QUEUED.value
    assert queue.enqueued == [marked]


def test_one_account_has_one_erasure_whoever_asks(people, store, queue) -> None:
    """A second click meets the operation; another administrator meets a
    conflict, whether that operation still runs or has failed."""
    admin, other_admin, account = people
    first = _operation_id()
    _submit(store, admin=admin, account=account, operation_id=first)

    again = _submit(store, admin=admin, account=account, operation_id=_operation_id())
    with pytest.raises(DeletionOperationConflict):
        _submit(
            store, admin=other_admin, account=account, operation_id=_operation_id()
        )
    assert store.fail(first, "probe failure") is True
    with pytest.raises(DeletionOperationConflict):
        _submit(
            store, admin=other_admin, account=account, operation_id=_operation_id()
        )
    failed = _submit(
        store, admin=admin, account=account, operation_id=_operation_id()
    )

    assert again["operation_id"] == first
    assert failed["operation_id"] == first
    assert failed["status"] == DeletionOperationStatus.DELETE_FAILED.value
    assert queue.enqueued == [first], "a met operation is not enqueued twice"


def test_an_operation_registered_for_inline_execution_reaches_no_worker(
    people, store, queue
) -> None:
    """The erasure runs its aggregate deletions itself: with two deletion
    slots, waiting for queued children could block the erasure forever."""
    admin, _other, account = people
    inline = _operation_id()

    registered = _submit(
        store, admin=admin, account=account, operation_id=inline, dispatch=False
    )
    assert store.fail(inline, "probe failure") is True
    reopened = store.retry(
        inline,
        tenant_id=TENANT,
        created_by_user_id=admin,
        workspace_id=None,
        dispatch=False,
    )

    assert registered["status"] == DeletionOperationStatus.QUEUED.value
    assert reopened["status"] == DeletionOperationStatus.QUEUED.value
    assert queue.enqueued == []


def test_a_registration_on_an_event_loop_is_refused_before_it_waits(
    people, store
) -> None:
    """Registering can wait for a lock a transaction of another request
    holds; on that request's event loop it would wait for itself. The store
    refuses before it touches the database, and the same write from a
    worker thread goes through."""
    admin, _other, account = people
    operation_id = _operation_id()

    async def on_the_loop(write):
        write()

    def register():
        _submit(store, admin=admin, account=account, operation_id=operation_id)

    def reopen():
        store.retry(
            operation_id, tenant_id=TENANT, created_by_user_id=admin, workspace_id=None
        )

    for write in (register, reopen):
        with pytest.raises(RuntimeError, match="event loop"):
            asyncio.run(on_the_loop(write))
    assert (
        store.find_for_target(
            DeletionTargetKind.USER,
            str(account),
            tenant_id=TENANT,
            created_by_user_id=admin,
            workspace_id=None,
        )
        is None
    )

    async def from_a_worker_thread():
        return await asyncio.to_thread(
            _submit, store, admin=admin, account=account, operation_id=operation_id
        )

    registered = asyncio.run(from_a_worker_thread())
    assert registered["operation_id"] == operation_id


def test_the_port_names_every_aggregate_the_account_owns_in_order(
    factory, tenant
) -> None:
    subject, colleague = uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]

    def key(name: str) -> str:
        return f"{name}-{tag}"

    async def content(session):
        await ensure_canonical_users(session, [subject, colleague], tenant_id=tenant)
        tables = _tables()
        for session_table, workspace in (
            ("agent_sessions", "ws-a"),
            ("knowledge_sessions", None),
        ):
            await session.execute(
                sa.insert(tables[session_table]).values(
                    id=key(session_table),
                    tenant_id=tenant,
                    created_by_user_id=subject,
                    workspace_id=workspace,
                    created_at=_NOW,
                    updated_at=_NOW,
                )
            )
        await session.execute(
            sa.insert(tables["agent_sessions"]).values(
                id=key("colleague-session"),
                tenant_id=tenant,
                created_by_user_id=colleague,
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        # A run inside one of the account's own sessions goes with that
        # session; one inside somebody else's session is the account's own.
        for run_id, owner, parent, session_id in (
            (key("root-run"), subject, None, None),
            (key("child-run"), subject, key("root-run"), None),
            (key("colleague-run"), colleague, None, None),
            (key("agent-session-run"), subject, None, key("agent_sessions")),
            (key("knowledge-session-run"), subject, None, key("knowledge_sessions")),
            (key("foreign-session-run"), subject, None, key("colleague-session")),
        ):
            await session.execute(
                sa.insert(tables["runs"]).values(
                    run_id=run_id,
                    tenant_id=tenant,
                    question="fixture",
                    created_at=_NOW,
                    created_by_user_id=owner,
                    parent_run_id=parent,
                    session_id=session_id,
                    workspace_id="ws-a",
                )
            )
        # Created in the reverse of their deletion order, so neither the
        # insertion order nor the owner index's (created_at, id) order can
        # stand in for the enumeration's own ORDER BY.
        for section_id, owner, workspace, created_at in (
            (key("s-b"), subject, "ws-b", _NOW),
            (key("s-a2"), subject, "ws-a", _NOW + 1),
            (key("s-a1"), subject, "ws-a", _NOW + 2),
            (key("colleague-section"), colleague, "ws-a", _NOW + 3),
        ):
            await session.execute(
                sa.insert(tables["asset_sections"]).values(
                    id=section_id,
                    tenant_id=tenant,
                    created_by_user_id=owner,
                    workspace_id=workspace,
                    title="fixture",
                    created_at=created_at,
                    updated_at=created_at,
                )
            )
        # An asset is no aggregate of its own: it goes with its section.
        await session.execute(
            sa.insert(tables["asset_records"]).values(
                id=key("asset"),
                tenant_id=tenant,
                section_id=key("s-a1"),
                created_by_user_id=subject,
                workspace_id="ws-a",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        for collection_id, owner in (
            (key("own-collection"), subject),
            (key("foreign-collection"), colleague),
        ):
            await session.execute(
                sa.insert(tables["knowledge_collections"]).values(
                    id=collection_id,
                    tenant_id=tenant,
                    name="fixture",
                    embedding_model="probe",
                    embedding_dim=8,
                    created_at=_NOW,
                    created_by_user_id=owner,
                )
            )
        for document_id, collection_id, source_owner in (
            (key("doc-in-foreign"), key("foreign-collection"), subject),
            (key("doc-in-own"), key("own-collection"), subject),
            (key("colleague-doc"), key("foreign-collection"), colleague),
        ):
            await session.execute(
                sa.insert(tables["knowledge_documents"]).values(
                    id=document_id,
                    tenant_id=tenant,
                    collection_id=collection_id,
                    title="fixture",
                    source_owner_user_id=source_owner,
                    created_at=_NOW,
                )
            )
        await session.execute(
            sa.insert(tables["vector_index_records"]).values(
                id=key("index"),
                tenant_id=tenant,
                created_by_user_id=subject,
                workspace_id="ws-a",
                model="probe",
                created_at=_NOW,
                updated_at=_NOW,
            )
        )
        await session.execute(
            sa.update(tables["knowledge_documents"])
            .where(tables["knowledge_documents"].c.id == key("doc-in-own"))
            .values(source_id=key("document:backed"))
        )
        # Only a live source of the subject that nothing backs any more is
        # an aggregate of its own; the rest go with their asset, section or
        # document, or are tombstones already.
        for source_id, owner, state in (
            (key("document:orphan"), subject, "active"),
            (f"asset:{key('asset')}", subject, "active"),
            (f"section:{key('s-a1')}", subject, "deleting"),
            (key("document:backed"), subject, "active"),
            (key("document:gone"), subject, "deleted"),
            (key("document:colleague-orphan"), colleague, "active"),
        ):
            await session.execute(
                sa.insert(tables["source_lifecycles"]).values(
                    tenant_id=tenant,
                    source_id=source_id,
                    owner_key=str(owner),
                    workspace_key="",
                    owner_user_id=owner,
                    workspace_id=None,
                    state=state,
                    epoch=1,
                    updated_at=_NOW,
                )
            )

    _transaction(factory, content)
    # Only the enumeration is under test: it reads through the session
    # factory alone, so the surfaces that delete stay unbound.
    port = DeletionMachineAggregates(
        service=None,
        store=None,
        run_store=None,
        session_factory=factory,
        app_role=APP_ROLE,
    )

    pending = port.pending(tenant_id=tenant, user_id=subject)

    assert pending == (
        AggregateRef("agent_session", (key("agent_sessions"),), "ws-a"),
        AggregateRef("knowledge_session", (key("knowledge_sessions"),), None),
        AggregateRef("run", (key("foreign-session-run"),), "ws-a"),
        AggregateRef("run", (key("root-run"),), "ws-a"),
        AggregateRef("section", (key("s-a1"),), "ws-a"),
        AggregateRef("section", (key("s-a2"),), "ws-a"),
        AggregateRef("section", (key("s-b"),), "ws-b"),
        AggregateRef("knowledge_document", (key("doc-in-foreign"),)),
        AggregateRef("knowledge_collection", (key("own-collection"),)),
        AggregateRef("vector_index", (key("index"),), "ws-a"),
        AggregateRef("source", (key("document:orphan"),), None),
    )


def test_the_wired_erasure_reaches_the_database_from_a_fresh_loop_every_time(
    store,
) -> None:
    """``run_coro_sync`` runs every call on a new event loop, so the engine
    the container wires must never hand one call's connection to the next."""
    from inqtrix.server.container import _bind_user_erasure
    from inqtrix.settings import Settings, StorageSettings

    class _Service:
        bound = None

        def bind_user_erasure(self, erasure) -> None:
            self.bound = erasure

    class _Handle:
        operation_id = "del_probe"
        completed = 0

        def complete(self) -> None:
            self.completed += 1

    service, handle = _Service(), _Handle()
    _bind_user_erasure(
        Settings(
            storage=StorageSettings(
                backend="postgres",
                database_url=TEST_DATABASE_URL,
                app_role=APP_ROLE,
            )
        ),
        asset_deletion_service=service,
        deletion_store=store,
        run_store=None,
    )

    # No such account: each call only looks it up and finishes.
    for _ in range(3):
        service.bound.execute(
            handle, tenant_id=TENANT, user_id=uuid.uuid4(), actor_user_id=None
        )

    assert handle.completed == 3


class _Machine:
    """The deletion machine wired as the worker wires it, on this database."""

    def __init__(self, tmp_path, *, knowledge=None) -> None:
        from inqtrix.auth.permissions import AuthorizationService
        from inqtrix.project.asset_records_postgres import PostgresAssetStore
        from inqtrix.server.container import _bind_user_erasure
        from inqtrix.services.asset_deletion_service import AssetDeletionService
        from inqtrix.services.asset_records_service import AssetRecordsService
        from inqtrix.services.file_service import FileService
        from inqtrix.settings import Settings, StorageSettings
        from inqtrix.storage.content_postgres import PostgresFileRegistry
        from inqtrix.storage.object_store import LocalFSObjectStore

        self.engine = build_engine(TEST_DATABASE_URL, null_pool=True)
        self.factory = build_session_factory(self.engine)
        self.library = PostgresAssetStore(engine=self.engine, app_role=APP_ROLE)
        self.registry = PostgresFileRegistry(
            session_factory=self.factory, app_role=APP_ROLE
        )
        self.blobs = LocalFSObjectStore(root=tmp_path / "objects")
        self.deletions = PostgresDeletionOperationStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            max_concurrent=1,
            completed_ttl_seconds=3600,
            worker_id="erasure-machine-test",
            recover_orphans=False,
        )
        self.service = AssetDeletionService(
            assets=AssetRecordsService(store=self.library, durable=True),
            operation_store=self.deletions,
            files=FileService(
                registry=self.registry,
                object_store=self.blobs,
                # The owner is always admitted before any port is consulted.
                permissions=AuthorizationService(members=None, shares=None, audit=None),
                max_file_bytes=1_000_000,
            ),
            knowledge=knowledge,
            vector_indexes=None,
        )
        _bind_user_erasure(
            Settings(
                storage=StorageSettings(
                    backend="postgres",
                    database_url=TEST_DATABASE_URL,
                    app_role=APP_ROLE,
                )
            ),
            asset_deletion_service=self.service,
            deletion_store=self.deletions,
            run_store=None,
        )

    def erase(self, *, admin: uuid.UUID, subject: uuid.UUID) -> dict:
        """Start the erasure as the admin route does; wait for its end."""
        import time

        from inqtrix.auth.principal import Principal
        from inqtrix.storage.user_lifecycle_postgres import (
            PostgresUserLifecycleTransaction,
        )

        users = _tables()["users"]
        _transaction(
            self.factory,
            lambda session: session.execute(
                sa.update(users).where(users.c.id == admin).values(instance_role="admin")
            ),
        )
        outcome, report = run_coro_sync(
            PostgresUserLifecycleTransaction(
                session_factory=self.factory, app_role=APP_ROLE
            ).begin_erasure(
                tenant_id=TENANT,
                user_id=subject,
                actor_user_id=admin,
                operation_id=_operation_id(),
            )
        )
        assert "operation_id" in report, (outcome, report)
        summary = run_coro_sync(
            self.service.start_user_erasure(
                subject,
                principal=Principal(
                    user_id=admin, kind="oidc_session", tenant_id=TENANT, role="admin"
                ),
                operation_id=report["operation_id"],
            )
        )
        deadline = time.monotonic() + 60
        while True:
            receipt = self.deletions.get(
                str(summary["operation_id"]),
                tenant_id=TENANT,
                created_by_user_id=admin,
                workspace_id=None,
            )
            if receipt["status"] in {"deleted", "delete_failed"}:
                return receipt
            assert time.monotonic() < deadline, receipt
            time.sleep(0.05)

    def owned(self, subject: uuid.UUID) -> dict[str, int]:
        """What still names the account, per table and lifecycle state."""

        async def count(session) -> dict[str, int]:
            t = _tables()
            counts = {}
            for table, column in (
                ("asset_sections", "created_by_user_id"),
                ("asset_records", "created_by_user_id"),
                ("files", "owner_user_id"),
                ("users", "id"),
            ):
                counts[table] = (
                    await session.execute(
                        sa.select(sa.func.count())
                        .select_from(t[table])
                        .where(t[table].c[column] == subject)
                    )
                ).scalar_one()
            lifecycles = t["source_lifecycles"]
            for state, number in (
                await session.execute(
                    sa.select(lifecycles.c.state, sa.func.count())
                    .where(lifecycles.c.owner_user_id == subject)
                    .group_by(lifecycles.c.state)
                )
            ).all():
                counts[f"source_lifecycles:{state}"] = number
            return counts

        return _transaction(self.factory, count)

    def close(self, subject: uuid.UUID) -> None:
        async def cleanup(session):
            t = _tables()
            for table, column in (
                ("asset_records", "created_by_user_id"),
                ("files", "owner_user_id"),
                ("asset_sections", "created_by_user_id"),
                ("source_lifecycles", "owner_user_id"),
                ("deletion_operation_assets", "created_by_user_id"),
            ):
                await session.execute(
                    sa.delete(t[table]).where(t[table].c[column] == subject)
                )

        _transaction(self.factory, cleanup)
        self.deletions.close()
        run_coro_sync(self.library.aclose())
        run_coro_sync(self.engine.dispose())


def test_an_account_is_erased_through_the_real_deletion_machine(
    people, tmp_path
) -> None:
    """The erasure drives the real deletion machine, not a stand-in.

    The account opened its library once -- three prepared sections -- and
    keeps one file in it. Its erasure deletes every section together with
    the file and its blob, leaves the machine's own tombstones in place,
    and finishes. A stand-in that deletes rows directly sees neither the
    sections nor the tombstones.
    """
    from inqtrix.content.ports import FileRecord
    from inqtrix.project.scoped_upsert import ResourceScope

    admin, _, subject = people
    tag = uuid.uuid4().hex[:12]
    machine = _Machine(tmp_path)
    file_id = f"fl_{tag}"
    object_key = f"tenants/{TENANT}/files/{file_id}"
    try:
        sections = run_coro_sync(
            machine.library.ensure_default_sections(
                created_by_user_id=subject, workspace_id=None
            )
        )
        library = next(s for s in sections if s.semantic_role == "library")
        asset = run_coro_sync(
            machine.library.upsert_asset(
                id=f"fa_{tag}", section_id=library.id, group_id=None,
                title="A", label="A", file_name="a.txt", mime_type="text/plain",
                origin="library", page_count=1, parse_status="parsed",
                parse_warning=None, text_truncated=False, size_bytes=5,
                server_file_id=None, extracted_text="hello", created_at=_NOW,
                updated_at=_NOW, created_by_user_id=subject, workspace_id=None,
            )
        )
        source = tmp_path / "a.txt"
        source.write_bytes(b"hello")
        machine.blobs.put(object_key, source)
        run_coro_sync(
            machine.registry.create(
                FileRecord(
                    id=file_id, tenant_id=TENANT, owner_user_id=subject,
                    workspace_id=None, file_name="a.txt",
                    content_type="text/plain", size_bytes=5, sha256="a" * 64,
                    object_key=object_key, created_at=_NOW,
                )
            )
        )
        run_coro_sync(
            machine.library.finalize_asset_upload(
                id=asset.id, section_id=library.id, group_id=None,
                title=asset.title, label=asset.label, file_name="a.txt",
                mime_type="text/plain", origin=asset.origin, page_count=1,
                parse_status="parsed", parse_warning=None, text_truncated=False,
                size_bytes=5, server_file_id=file_id, parser_id="markitdown:test",
                created_at=_NOW, updated_at=_NOW + 1,
                scope=ResourceScope.from_record(asset),
            )
        )
        # The upload made the file's source live.
        assert machine.owned(subject) == {
            "asset_sections": 3,
            "asset_records": 1,
            "files": 1,
            "users": 1,
            "source_lifecycles:active": 1,
        }

        receipt = machine.erase(admin=admin, subject=subject)

        assert receipt["status"] == "deleted", receipt.get("error")
        assert not machine.blobs.exists(object_key)
        # Three section tombstones and the file's own: the machine's record
        # that these sources were deleted, ids without content.
        assert machine.owned(subject) == {
            "asset_sections": 0,
            "asset_records": 0,
            "files": 0,
            "users": 0,
            "source_lifecycles:deleted": 4,
        }
    finally:
        machine.close(subject)


class _SourceCleanup:
    """The Knowledge service's source cleanup, recording what it removed."""

    def __init__(self) -> None:
        self.cleaned: list[str] = []

    async def mark_source_deleting(self, source_id: str, **_kwargs) -> None:
        del source_id

    async def prepare_source_cleanup(self, source_id: str, *, deletion_permit):
        from inqtrix.knowledge.source_cleanup import empty_source_cleanup_plan

        del source_id
        return empty_source_cleanup_plan(deletion_permit)

    async def delete_source(self, source_id: str, **_kwargs) -> None:
        self.cleaned.append(source_id)

    async def count_source_residuals(self, source_id: str, **_kwargs) -> int:
        del source_id
        return 0


def test_a_source_that_outlived_its_document_is_settled_by_the_erasure(
    people, tmp_path
) -> None:
    """A live source whose document is gone used to stop the erasure for
    good: no aggregate deleted it, and its lifecycle row kept naming the
    account. The erasure now deletes it as an aggregate of its own -- the
    Knowledge store cleans up by plan, nothing remains, the tombstone
    stays -- and finishes."""
    admin, _, subject = people
    source_id = f"document:{uuid.uuid4().hex}"
    knowledge = _SourceCleanup()
    machine = _Machine(tmp_path, knowledge=knowledge)
    try:
        _transaction(
            machine.factory,
            lambda session: session.execute(
                sa.insert(_tables()["source_lifecycles"]).values(
                    tenant_id=TENANT,
                    source_id=source_id,
                    owner_key=str(subject),
                    workspace_key="",
                    owner_user_id=subject,
                    workspace_id=None,
                    state="active",
                    epoch=1,
                    updated_at=_NOW,
                )
            ),
        )

        receipt = machine.erase(admin=admin, subject=subject)

        assert receipt["status"] == "deleted", receipt.get("error")
        assert knowledge.cleaned == [source_id]
        assert machine.owned(subject) == {
            "asset_sections": 0,
            "asset_records": 0,
            "files": 0,
            "users": 0,
            "source_lifecycles:deleted": 1,
        }
    finally:
        machine.close(subject)
