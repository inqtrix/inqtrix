"""Postgres integration tests for the durable run store (gated suite).

Same gating and conventions as the identity/content suites: a
disposable database via ``INQTRIX_TEST_DATABASE_URL``, operations under
the restricted app role, RLS as the second defense layer. The parity
assertions mirror the in-memory contract: the 16-key summary shape,
gap-free 1-based event sequences, the snapshot companion BEFORE its
carrier, terminal-state absorption, and claim fencing.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib import import_module

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.exceptions import AgentProviderTimeout, AgentRateLimited
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.project.agent_sessions_postgres import PostgresAgentSessionStore
from inqtrix.project.knowledge_sessions_ports import KnowledgeSessionNotFound
from inqtrix.project.knowledge_sessions_postgres import (
    PostgresKnowledgeSessionStore,
)
from inqtrix.result import ResearchResult
from inqtrix.runs.deletion_operations import (
    DeletionTargetKind,
    SessionDeletionContext,
)
from inqtrix.runs.deletion_postgres import PostgresDeletionOperationStore
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.server.runs import (
    RunActive,
    RunNotFound,
    RunParentInactive,
    RunSessionActive,
)
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.deletions_orm import deletion_operations
from inqtrix.storage.identity_orm import audit_log, resource_shares, users
from inqtrix.storage.identity_postgres import PostgresIdentityBackend
from inqtrix.storage.knowledge_sessions_orm import knowledge_sessions
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.runs_orm import run_events, runs
from inqtrix.storage.usage_orm import llm_usage
from inqtrix.state import build_run_snapshot

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
USER_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
USER_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
OWNER_1 = uuid.UUID("33333333-3333-4333-8333-333333333333")
OWNER_2 = uuid.UUID("44444444-4444-4444-8444-444444444444")
INTRUDER = uuid.UUID("55555555-5555-4555-8555-555555555555")
SHARE_OWNER = uuid.UUID("66666666-6666-4666-8666-666666666666")
SHARE_RECIPIENT = uuid.UUID("77777777-7777-4777-8777-777777777777")
RUN_TEST_USERS = (
    USER_A,
    USER_B,
    OWNER_1,
    OWNER_2,
    INTRUDER,
    SHARE_OWNER,
    SHARE_RECIPIENT,
)

RUN_SUMMARY_KEYS = {
    "access",
    "run_id",
    "status",
    "queue_position",
    "question",
    "stack",
    "workspace_id",
    "mode",
    "agent_overrides",
    "created_at",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "snapshot",
    "error",
    "events_url",
    "result_url",
}


@pytest.fixture(scope="session", autouse=True)
def runs_schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def engine():
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
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(
                deletion_operations.delete().where(
                    deletion_operations.c.target_kind.in_(
                        ("agent_session", "knowledge_session")
                    )
                )
            )
            await session.execute(
                resource_shares.delete().where(resource_shares.c.resource_type == "run")
            )
            await session.execute(text("DELETE FROM runs"))
            await session.execute(text("DELETE FROM agent_sessions"))
            await session.execute(text("DELETE FROM knowledge_sessions"))
            for user_id in RUN_TEST_USERS:
                label = f"runs-pg-{user_id.hex}"
                await session.execute(
                    pg_insert(users)
                    .values(
                        id=user_id,
                        tenant_id="default",
                        issuer="https://runs-tests.example",
                        subject=label,
                        email=f"{label}@example.com",
                    )
                    .on_conflict_do_update(
                        index_elements=(users.c.id,),
                        set_={
                            "tenant_id": "default",
                            "issuer": "https://runs-tests.example",
                            "subject": label,
                            "email": f"{label}@example.com",
                            "disabled_at": None,
                        },
                    )
                )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def store(engine):
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-worker",
    )
    yield store
    store.close()


def wait_for_status(
    store,
    run_id,
    statuses,
    timeout=10.0,
    *,
    workspace_id=None,
    visible_to=None,
):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        summary = store.get(
            run_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
        )
        if summary["status"] in statuses:
            return summary
        time.sleep(0.05)
    pytest.fail(f"run {run_id} never reached {statuses}")


def submit_noop(store, *, work=None, **kwargs):
    def _default_work(handle):
        handle.emit(
            "inqtrix.node.started",
            {"node": "classify", "snapshot": {"current_node": "classify"}},
        )
        handle.complete(
            {"answer": "fertig", "metrics": {"rounds": 1}},
            snapshot={"current_node": "answer", "done": True},
        )

    if kwargs.get("kind") == "agent" and kwargs.get("session_id"):
        seed_agent_session(
            store,
            str(kwargs["session_id"]),
            created_by_user_id=kwargs.get("created_by_user_id"),
            workspace_id=kwargs.get("workspace_id"),
        )
    return store.submit(
        question=kwargs.pop("question", "Wie ist die Haftung geregelt?"),
        stack_name="default",
        work=work or _default_work,
        request_payload={"question": "x", "body": {"mode": "research"}},
        **kwargs,
    )


def seed_agent_session(
    store: PostgresRunStore,
    session_id: str,
    *,
    created_by_user_id: uuid.UUID | None = None,
    workspace_id: str | None = None,
) -> None:
    """Create the durable session registry required by agent admission."""

    async def _seed() -> None:
        async with store._session("default") as session:
            await session.execute(
                pg_insert(agent_sessions)
                .values(
                    id=session_id,
                    tenant_id="default",
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    title="Agent session fixture",
                    group_id=None,
                    items_json="[]",
                    lifecycle_status="active",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
                .on_conflict_do_nothing(index_elements=(agent_sessions.c.id,))
            )

    store._call(_seed())


def seed_knowledge_session(
    store: PostgresRunStore,
    session_id: str,
    *,
    created_by_user_id: uuid.UUID | None = None,
    workspace_id: str | None = None,
) -> None:
    """Create the durable registry row required by Knowledge-run admission."""

    async def _seed() -> None:
        async with store._session("default") as session:
            await session.execute(
                pg_insert(knowledge_sessions)
                .values(
                    id=session_id,
                    tenant_id="default",
                    created_by_user_id=created_by_user_id,
                    workspace_id=workspace_id,
                    title="Knowledge session fixture",
                    group_id=None,
                    items_json="[]",
                    lifecycle_status="active",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
                .on_conflict_do_nothing(index_elements=(knowledge_sessions.c.id,))
            )

    store._call(_seed())


class _RecordingDispatchQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def enqueue(self, *, run_id: str, tenant_id: str) -> None:
        self.enqueued.append((run_id, tenant_id))


class _RecordingDeletionQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str]] = []

    def enqueue(self, *, operation_id: str, tenant_id: str) -> None:
        self.enqueued.append((operation_id, tenant_id))


def test_worker_claim_store_dispatches_children_and_parent_wakes(
    store,
) -> None:
    """Claim ownership stays local while dispatch returns to Valkey."""
    del store  # fixture provides isolated migrated rows for this test
    queue = _RecordingDispatchQueue()
    dispatching = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-claim-dispatch",
    )
    try:
        seed_agent_session(
            dispatching,
            "session-dispatch",
            workspace_id="ws-agent",
        )
        parent = dispatching.submit(
            question="parent",
            stack_name="default",
            work=lambda _handle: None,
            workspace_id="ws-agent",
            kind="agent",
            session_id="session-dispatch",
            request_payload={
                "question": "parent",
                "body": {"mode": "workspace_agent"},
            },
        )
        assert dispatching.dispatch_status(parent["run_id"], "default") == "queued"
        assert dispatching.dispatch_status(parent["run_id"], "other") is None
        parent_claim = dispatching.claim_for_execution(
            parent["run_id"], "default", allow_takeover=False
        )
        assert parent_claim is not None
        assert parent_claim.workspace_id == "ws-agent"
        assert dispatching.dispatch_status(parent["run_id"], "default") == "running"
        child = dispatching.submit(
            question="child",
            stack_name="default",
            work=lambda _handle: None,
            workspace_id="ws-agent",
            kind="agent_child",
            parent_run_id=parent["run_id"],
            root_run_id=parent["run_id"],
            session_id="session-dispatch",
            request_payload={
                "question": "child",
                "body": {
                    "mode": "research",
                    "parent_task_id": "task-dispatch",
                    "parent_task_attempt": 2,
                },
            },
        )

        def child_progress() -> list[dict]:
            subscription = dispatching.subscribe(parent["run_id"])
            try:
                return [
                    event
                    for event in subscription.replay
                    if event["type"] == "inqtrix.agent.child.progress"
                ]
            finally:
                subscription.close()

        queued_count = len(child_progress())
        assert queued_count == 1
        dispatching.emit(
            child["run_id"],
            "inqtrix.output_text.delta",
            {"delta": "not copied to parent"},
        )
        assert len(child_progress()) == queued_count

        child_claim = dispatching.claim_for_execution(
            child["run_id"], "default", allow_takeover=False
        )
        assert child_claim is not None
        assert child_claim.workspace_id == "ws-agent"
        dispatching.mark_waiting(
            parent["run_id"],
            status="waiting_for_children",
            fence_attempt=parent_claim.attempt,
        )
        assert (
            dispatching.dispatch_status(parent["run_id"], "default")
            == "waiting_for_children"
        )
        dispatching.emit(
            child["run_id"],
            "inqtrix.node.started",
            {"node": "search", "snapshot": {"current_node": "search"}},
            fence_attempt=child_claim.attempt,
        )
        assert dispatching.complete(
            child["run_id"],
            {"answer": "done", "metrics": {"sources": 3}},
            snapshot={"current_node": "answer", "done": True},
            fence_attempt=child_claim.attempt,
        )

        assert dispatching.get(parent["run_id"])["status"] == "queued"
        assert dispatching.dispatch_status(parent["run_id"], "default") == "queued"
        assert [run_id for run_id, _tenant in queue.enqueued] == [
            parent["run_id"],
            child["run_id"],
            parent["run_id"],
        ]
        projected = child_progress()
        assert projected[-1]["data"]["run_status"] == "completed"
        assert projected[-1]["data"]["task_id"] == "task-dispatch"
        assert projected[-1]["data"]["attempt"] == 2
    finally:
        dispatching.close()


def test_child_park_projects_its_gate_to_the_parent_stream(store) -> None:
    """A CHILD parking on a human decision must reach the parent stream.

    ``inqtrix.run.waiting`` is on the projection whitelist and the memory
    twin projects it through its emit chokepoint — the Postgres park
    write appends events directly, so without an explicit projection a
    parked child stays invisible on every parent surface (the parent
    hangs on a gate nobody can see)."""
    del store
    queue = _RecordingDispatchQueue()
    dispatching = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-child-park",
    )
    try:
        seed_agent_session(
            dispatching,
            "session-child-park",
            workspace_id="ws-agent",
        )
        parent = dispatching.submit(
            question="parent",
            stack_name="default",
            work=lambda _handle: None,
            workspace_id="ws-agent",
            kind="agent",
            session_id="session-child-park",
            request_payload={
                "question": "parent",
                "body": {"mode": "agent_kernel"},
            },
        )
        parent_claim = dispatching.claim_for_execution(
            parent["run_id"], "default", allow_takeover=False
        )
        assert parent_claim is not None
        child = dispatching.submit(
            question="child",
            stack_name="default",
            work=lambda _handle: None,
            workspace_id="ws-agent",
            kind="agent_child",
            parent_run_id=parent["run_id"],
            root_run_id=parent["run_id"],
            session_id="session-child-park",
            request_payload={
                "question": "child",
                "body": {
                    "mode": "workspace_agent",
                    "parent_task_id": "call_park",
                    "parent_task_attempt": 1,
                },
            },
        )
        dispatching.mark_waiting(
            parent["run_id"],
            status="waiting_for_children",
            fence_attempt=parent_claim.attempt,
        )
        child_claim = dispatching.claim_for_execution(
            child["run_id"], "default", allow_takeover=False
        )
        assert child_claim is not None
        dispatching.mark_waiting(
            child["run_id"],
            status="waiting_for_approval",
            fence_attempt=child_claim.attempt,
        )

        subscription = dispatching.subscribe(parent["run_id"])
        try:
            parked = [
                event
                for event in subscription.replay
                if event["type"] == "inqtrix.agent.child.progress"
                and event["data"].get("run_status") == "waiting_for_approval"
            ]
        finally:
            subscription.close()
        assert parked, "child park was not projected onto the parent stream"
        assert parked[-1]["data"]["child_run_id"] == child["run_id"]
        assert parked[-1]["data"]["task_id"] == "call_park"
    finally:
        dispatching.close()


@pytest.mark.asyncio
async def test_migration_scrubs_active_legacy_child_token_budget(
    store,
) -> None:
    """0043 removes planner caps before a worker can replay the child."""
    del store
    queue = _RecordingDispatchQueue()
    durable = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-legacy-budget",
    )
    root = durable.submit(
        question="root",
        stack_name="default",
        work=lambda _handle: None,
        kind="agent",
        request_payload={"body": {"mode": "workspace_agent"}},
    )
    child = durable.submit(
        question="legacy child",
        stack_name="default",
        work=lambda _handle: None,
        kind="agent_child",
        parent_run_id=root["run_id"],
        request_payload={
            "body": {
                "mode": "research",
                "token_budget": 1800,
                "parent_task_id": "task-legacy",
            }
        },
    )
    migration = import_module(
        "inqtrix.storage.migrations.versions." "0043_agent_task_execution_contract"
    )
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                before = (
                    await session.execute(
                        text(
                            "SELECT request_payload::jsonb->'body'->>"
                            "'token_budget' FROM runs "
                            "WHERE run_id = :run_id"
                        ),
                        {"run_id": child["run_id"]},
                    )
                ).scalar_one()
                await session.execute(text(migration._LEGACY_CHILD_BUDGET_BACKFILL_SQL))
                after = (
                    await session.execute(
                        text(
                            "SELECT request_payload::jsonb->'body'->>"
                            "'token_budget' FROM runs "
                            "WHERE run_id = :run_id"
                        ),
                        {"run_id": child["run_id"]},
                    )
                ).scalar_one()
    finally:
        durable.close()
        await engine.dispose()

    assert before == "1800"
    assert after is None


def test_nested_children_use_canonical_root_and_cancel_recursively(
    store,
) -> None:
    """Durable lineage is parent-owned and root cancel covers all depths."""
    release = threading.Event()

    def blocking_work(handle) -> None:
        release.wait(timeout=10)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({"answer": "done"})

    root = store.submit(
        question="root",
        stack_name="default",
        work=blocking_work,
        kind="agent",
        request_payload={"body": {"mode": "workspace_agent"}},
    )
    finished_child = store.submit(
        question="finished child",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "finished"}),
        kind="agent_child",
        parent_run_id=root["run_id"],
        root_run_id=root["run_id"],
        request_payload={"body": {"mode": "research"}},
    )
    wait_for_status(store, finished_child["run_id"], {"completed"})
    child = store.submit(
        question="child",
        stack_name="default",
        work=blocking_work,
        kind="agent_child",
        parent_run_id=root["run_id"],
        root_run_id="caller-controlled-wrong-root",
        request_payload={"body": {"mode": "research"}},
    )
    grandchild = store.submit(
        question="grandchild",
        stack_name="default",
        work=blocking_work,
        kind="agent_child",
        parent_run_id=child["run_id"],
        root_run_id=child["run_id"],
        request_payload={"body": {"mode": "research"}},
    )
    try:
        assert child["root_run_id"] == root["run_id"]
        assert grandchild["root_run_id"] == root["run_id"]
        _summary, affected = store.cancel_tree(root["run_id"])
        assert set(affected) == {
            root["run_id"],
            finished_child["run_id"],
            child["run_id"],
            grandchild["run_id"],
        }
        release.set()
        for run in (root, child, grandchild):
            wait_for_status(store, run["run_id"], {"cancelled"})
    finally:
        release.set()


def test_origin_key_submit_or_find_is_atomic_postgres(store) -> None:
    release = threading.Event()

    def root_work(handle) -> None:
        release.wait(timeout=10)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({"answer": "done"})

    root = store.submit(
        question="root",
        stack_name="default",
        work=root_work,
        kind="agent",
        request_payload={"body": {"mode": "workspace_agent"}},
    )
    barrier = threading.Barrier(3)

    def submit_same_origin() -> dict:
        barrier.wait()
        return store.submit(
            question="same child",
            stack_name="default",
            work=lambda handle: handle.complete({"answer": "child"}),
            kind="agent_child",
            parent_run_id=root["run_id"],
            origin_key="task-1:attempt-1",
            request_payload={"body": {"mode": "research"}},
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit_same_origin) for _ in range(2)]
            barrier.wait()
            results = [future.result(timeout=10) for future in futures]
        assert results[0]["run_id"] == results[1]["run_id"]
        assert len(store.children(root["run_id"])) == 1
    finally:
        store.cancel(root["run_id"])
        release.set()
        wait_for_status(store, root["run_id"], {"cancelled"})


def test_child_submit_racing_root_cancel_is_serialized_postgres(store) -> None:
    """Either admission wins and is cascaded, or cancellation rejects it."""
    release = threading.Event()

    def blocking_work(handle) -> None:
        release.wait(timeout=10)
        if handle.cancel_event.is_set():
            handle.cancel("client_requested_cancel")
        else:
            handle.complete({"answer": "done"})

    root = store.submit(
        question="root",
        stack_name="default",
        work=blocking_work,
        kind="agent",
        request_payload={"body": {"mode": "workspace_agent"}},
    )
    barrier = threading.Barrier(3)

    def submit_child() -> dict:
        barrier.wait()
        return store.submit(
            question="racing child",
            stack_name="default",
            work=blocking_work,
            kind="agent_child",
            parent_run_id=root["run_id"],
            origin_key="racing-origin",
            request_payload={"body": {"mode": "research"}},
        )

    def cancel_root() -> dict:
        barrier.wait()
        return store.cancel(root["run_id"])

    submitted: dict | None = None
    rejected = False
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            submit_future = pool.submit(submit_child)
            cancel_future = pool.submit(cancel_root)
            barrier.wait()
            cancel_future.result(timeout=10)
            try:
                submitted = submit_future.result(timeout=10)
            except RunParentInactive:
                rejected = True
        release.set()
        wait_for_status(store, root["run_id"], {"cancelled"})
        assert (submitted is not None) is not rejected
        if submitted is not None:
            wait_for_status(store, submitted["run_id"], {"cancelled"})
        else:
            assert store.children(root["run_id"]) == []
    finally:
        release.set()


def test_cancelled_child_park_miss_projects_and_wakes_parent(store) -> None:
    del store
    queue = _RecordingDispatchQueue()
    dispatching = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-cancelled-park",
    )
    try:
        seed_agent_session(dispatching, "session-cancelled-park")
        parent = dispatching.submit(
            question="parent",
            stack_name="default",
            work=lambda _handle: None,
            kind="agent",
            session_id="session-cancelled-park",
            request_payload={"body": {"mode": "workspace_agent"}},
        )
        parent_claim = dispatching.claim_for_execution(
            parent["run_id"], "default", allow_takeover=False
        )
        assert parent_claim is not None
        child = dispatching.submit(
            question="child",
            stack_name="default",
            work=lambda _handle: None,
            kind="agent_child",
            parent_run_id=parent["run_id"],
            root_run_id=parent["run_id"],
            request_payload={
                "body": {
                    "parent_task_id": "task-cancelled-park",
                    "parent_task_attempt": 1,
                }
            },
        )
        child_claim = dispatching.claim_for_execution(
            child["run_id"], "default", allow_takeover=False
        )
        assert child_claim is not None
        dispatching.mark_waiting(
            parent["run_id"],
            status="waiting_for_children",
            fence_attempt=parent_claim.attempt,
        )

        cancelled = dispatching.cancel(child["run_id"])
        assert cancelled["status"] == "running"
        dispatching.mark_waiting(
            child["run_id"],
            status="waiting_for_input",
            fence_attempt=child_claim.attempt,
        )

        assert dispatching.get(child["run_id"])["status"] == "cancelled"
        assert dispatching.get(parent["run_id"])["status"] == "queued"
        child_events = dispatching.subscribe(child["run_id"])
        try:
            assert child_events.replay[-1]["type"] == "inqtrix.run.cancelled"
        finally:
            child_events.close()
        parent_events = dispatching.subscribe(parent["run_id"])
        try:
            projected = [
                event
                for event in parent_events.replay
                if event["type"] == "inqtrix.agent.child.progress"
            ]
            assert projected[-1]["data"]["run_status"] == "cancelled"
            assert projected[-1]["data"]["task_id"] == ("task-cancelled-park")
        finally:
            parent_events.close()
        assert queue.enqueued[-1] == (parent["run_id"], "default")
    finally:
        dispatching.close()


def test_submit_executes_and_keeps_the_wire_shape(store):
    summary = submit_noop(store)
    assert set(summary.keys()) == RUN_SUMMARY_KEYS
    assert summary["status"] in {"queued", "running"}

    final = wait_for_status(store, summary["run_id"], {"completed"})
    assert set(final.keys()) == RUN_SUMMARY_KEYS
    assert final["snapshot"]["done"] is True
    assert final["error"] is None
    assert final["queue_position"] is None

    result = store.result(summary["run_id"])
    assert result["run_id"] == summary["run_id"]
    assert result["status"] == "completed"
    assert result["answer"] == "fertig"


def test_knowledge_result_receipt_survives_store_restart(store):
    degradation = {
        "reason": "vector_overfetch_cap",
        "retrieval_mode": "dense",
        "stage": "vector_candidate_pool",
        "requested_candidate_pool": 40,
        "returned_candidate_pool": 7,
        "final_top_k": 4,
        "final_evidence_complete": False,
        "requested_top_k": 4,
        "returned_hits": 2,
        "candidate_cap": 64,
    }
    result_state = {
        "knowledge_profile": {
            "id": "standard",
            "requested": "standard",
            "auto_selected": False,
            "auto_reason": "",
            "degraded_stages": [],
        },
        "knowledge_gate": {
            "enabled": True,
            "sufficient": True,
            "coverage": "full",
            "rounds_used": 0,
            "max_rounds": 1,
        },
        "knowledge_grounding": {"enabled": True},
        "knowledge_retrieval": {
            "degradations": [degradation, dict(degradation)]
        },
        "knowledge_candidates": 7,
        "knowledge_evidence_used": 2,
    }
    exported = ResearchResult.from_raw(
        {"answer": "Belegte Antwort", "result_state": result_state}
    ).to_export_payload()
    snapshot = build_run_snapshot(
        result_state,
        current_node="answer",
        last_message="completed",
    )

    summary = submit_noop(
        store,
        work=lambda handle: handle.complete(exported, snapshot=snapshot),
    )
    wait_for_status(store, summary["run_id"], {"completed"})

    restarted = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-knowledge-result-reload",
    )
    try:
        restored = restarted.result(summary["run_id"])
        restored_summary = restarted.get(summary["run_id"])
        assert restored["knowledge_profile"]["id"] == "standard"
        assert restored["knowledge_retrieval"] == {
            "degradations": [degradation]
        }
        assert restored_summary["snapshot"]["knowledge_retrieval"] == (
            restored["knowledge_retrieval"]
        )
        assert "queries" not in restored_summary["snapshot"]
    finally:
        restarted.close()


def test_parallel_root_agent_session_claim_accepts_exactly_one(store):
    """The partial unique index is the cross-worker execution lease."""
    hold = threading.Event()
    barrier = threading.Barrier(2)

    def work(handle):
        hold.wait(timeout=10.0)
        handle.complete({"answer": "ok", "metrics": {}})

    def submit() -> str:
        barrier.wait(timeout=5.0)
        return store.submit(
            question="Agent",
            stack_name="default",
            work=work,
            request_payload={"question": "Agent"},
            kind="agent",
            session_id="sess-pg-exclusive",
        )["run_id"]

    try:
        seed_agent_session(store, "sess-pg-exclusive")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(submit) for _ in range(2)]
            outcomes: list[object] = []
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=10.0))
                except Exception as exc:  # noqa: BLE001 - contract assertion
                    outcomes.append(exc)
        assert sum(isinstance(item, str) for item in outcomes) == 1
        assert sum(isinstance(item, RunSessionActive) for item in outcomes) == 1
    finally:
        hold.set()


def test_agent_session_tombstone_blocks_children_until_runs_are_terminal(
    store,
) -> None:
    """Deletion fences child admission and waits for worker termination."""

    del store
    queue = _RecordingDispatchQueue()
    durable = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-session-deletion-fence",
    )
    session_id = "session-deletion-fence"
    try:
        seed_agent_session(durable, session_id)
        root = durable.submit(
            question="parent",
            stack_name="default",
            work=lambda _handle: None,
            kind="agent",
            session_id=session_id,
            request_payload={"body": {"mode": "workspace_agent"}},
        )
        claim = durable.claim_for_execution(
            root["run_id"], "default", allow_takeover=False
        )
        assert claim is not None

        async def _tombstone() -> None:
            async with durable._session("default") as session:
                await session.execute(
                    agent_sessions.update()
                    .where(agent_sessions.c.id == session_id)
                    .values(
                        lifecycle_status="deleting",
                        deletion_operation_id="del_session_fence",
                        deletion_stage="queued",
                    )
                )

        durable._call(_tombstone())

        with pytest.raises(RunSessionActive):
            durable.submit(
                question="child",
                stack_name="default",
                work=lambda _handle: None,
                kind="agent_child",
                parent_run_id=root["run_id"],
                root_run_id=root["run_id"],
                request_payload={"body": {"origin_key": "late-child"}},
            )

        with pytest.raises(RuntimeError, match="still stopping"):
            durable.prepare_agent_session_aggregate_deletion(
                session_id,
                tenant_id="default",
                requester_user_id=None,
                workspace_id=None,
                run_ids=(root["run_id"],),
            )
        assert durable.mark_cancelled(
            root["run_id"],
            reason="session_deleting",
            fence_attempt=claim.attempt,
        )
        durable.prepare_agent_session_aggregate_deletion(
            session_id,
            tenant_id="default",
            requester_user_id=None,
            workspace_id=None,
            run_ids=(root["run_id"],),
        )
        durable.delete_agent_session_aggregate(
            session_id,
            tenant_id="default",
            requester_user_id=None,
            workspace_id=None,
            run_ids=(root["run_id"],),
        )
        assert not any(
            durable.agent_session_residuals(
                session_id,
                tenant_id="default",
                requester_user_id=None,
                workspace_id=None,
                run_ids=(root["run_id"],),
            ).values()
        )
    finally:
        durable.close()


def test_knowledge_session_deletion_removes_only_its_owned_run_aggregate(
    store,
) -> None:
    """Two saved answers are deleted without trusting foreign run ids."""

    session_id = "knowledge-session-delete-owned"
    workspace_id = "ws-knowledge-owner"
    seed_knowledge_session(
        store,
        session_id,
        created_by_user_id=OWNER_1,
        workspace_id=workspace_id,
    )
    owned = [
        submit_noop(
            store,
            question=question,
            mode="knowledge",
            session_id=session_id,
            created_by_user_id=OWNER_1,
            created_by_tenant_id="default",
            workspace_id=workspace_id,
        )["run_id"]
        for question in ("Bekannte Frage", "Unbeantwortbare Frage")
    ]
    for run_id in owned:
        wait_for_status(
            store,
            run_id,
            {"completed"},
            workspace_id=workspace_id,
            visible_to=UserContext(
                principal=Principal(
                    user_id=OWNER_1,
                    kind="oidc_session",
                    tenant_id="default",
                    role="member",
                )
            ),
        )

    foreign = (
        "run_knowledge_foreign_owner",
        "run_knowledge_foreign_workspace",
        "run_knowledge_foreign_tenant",
    )

    async def _seed_foreign_runs() -> None:
        admin_engine = build_engine(TEST_DATABASE_URL)
        factory = build_session_factory(admin_engine)
        now = time.time()
        rows = (
            {
                "run_id": foreign[0],
                "tenant_id": "default",
                "workspace_id": workspace_id,
                "created_by_user_id": OWNER_2,
                "created_by_tenant_id": "default",
            },
            {
                "run_id": foreign[1],
                "tenant_id": "default",
                "workspace_id": "ws-knowledge-foreign",
                "created_by_user_id": OWNER_1,
                "created_by_tenant_id": "default",
            },
            {
                "run_id": foreign[2],
                "tenant_id": "tenant-foreign",
                "workspace_id": workspace_id,
                "created_by_user_id": OWNER_1,
                "created_by_tenant_id": "tenant-foreign",
            },
        )
        try:
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        runs.insert(),
                        [
                            {
                                **row,
                                "status": "completed",
                                "mode": "knowledge",
                                "kind": "standard",
                                "session_id": f"foreign-{index}",
                                "question": "Foreign content",
                                "stack_name": "default",
                                "execution_scopes": [],
                                "agent_overrides": {},
                                "request_payload": {},
                                "snapshot": {"done": True},
                                "result": {"answer": "must survive"},
                                "event_seq": 1,
                                "created_at": now + index,
                                "finished_at": now + index,
                            }
                            for index, row in enumerate(rows)
                        ],
                    )
                    await session.execute(
                        run_events.insert(),
                        [
                            {
                                "run_id": run_id,
                                "sequence": 1,
                                "tenant_id": row["tenant_id"],
                                "type": "inqtrix.node.finished",
                                "created_at": now + index,
                                "data": {"answer": "must survive"},
                            }
                            for index, (run_id, row) in enumerate(
                                zip(foreign, rows, strict=True)
                            )
                        ],
                    )
                    await session.execute(
                        llm_usage.insert(),
                        {
                            "tenant_id": "default",
                            "user_id": OWNER_1,
                            "workspace_id": workspace_id,
                            "run_id": owned[0],
                            "feature": "knowledge",
                            "operation": "chat",
                            "model": "test-model",
                            "input_tokens": 11,
                            "output_tokens": 7,
                            "request_count": 1,
                            "duration_ms": 25,
                            "outcome": "success",
                            "created_at": now,
                        },
                    )
        finally:
            await admin_engine.dispose()

    store._call(_seed_foreign_runs())

    malicious_ids = (*owned, *foreign)
    deletion_queue = _RecordingDeletionQueue()
    deletion_store = PostgresDeletionOperationStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=deletion_queue,
        max_concurrent=1,
        completed_ttl_seconds=3600,
        worker_id="knowledge-session-context-test",
        recover_orphans=False,
    )
    try:
        summary = deletion_store.submit(
            target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
            target_id=session_id,
            manifest=(),
            tenant_id="default",
            created_by_user_id=OWNER_1,
            workspace_id=workspace_id,
            work=lambda _handle: None,
            session_context=SessionDeletionContext(
                target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
                session_id=session_id,
                run_ids=malicious_ids,
            ),
            total_items=2,
        )
        record = deletion_store.get_record(
            str(summary["operation_id"]),
            tenant_id="default",
            created_by_user_id=OWNER_1,
            workspace_id=workspace_id,
        )
        assert record.session_context == SessionDeletionContext(
            target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
            session_id=session_id,
            run_ids=tuple(owned),
        )
        assert deletion_queue.enqueued == [
            (str(summary["operation_id"]), "default")
        ]
    finally:
        deletion_store.close()

    with pytest.raises(RunSessionActive):
        submit_noop(
            store,
            question="Must not race the deletion fence",
            mode="knowledge",
            session_id=session_id,
            created_by_user_id=OWNER_1,
            created_by_tenant_id="default",
            workspace_id=workspace_id,
        )

    with pytest.raises(RuntimeError, match="outside its owner scope"):
        store.prepare_knowledge_session_aggregate_deletion(
            session_id,
            tenant_id="default",
            requester_user_id=OWNER_1,
            workspace_id=workspace_id,
            run_ids=malicious_ids,
        )

    store.prepare_knowledge_session_aggregate_deletion(
        session_id,
        tenant_id="default",
        requester_user_id=OWNER_1,
        workspace_id=workspace_id,
        run_ids=tuple(owned),
    )
    store.delete_knowledge_session_aggregate(
        session_id,
        tenant_id="default",
        requester_user_id=OWNER_1,
        workspace_id=workspace_id,
        run_ids=tuple(owned),
    )
    assert not any(
        store.knowledge_session_residuals(
            session_id,
            tenant_id="default",
            requester_user_id=OWNER_1,
            workspace_id=workspace_id,
            run_ids=tuple(owned),
        ).values()
    )

    async def _counts() -> tuple[int, int, int, int, int, int]:
        admin_engine = build_engine(TEST_DATABASE_URL)
        factory = build_session_factory(admin_engine)
        try:
            async with factory() as session:
                owned_runs = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(runs)
                        .where(runs.c.run_id.in_(owned))
                    )
                    or 0
                )
                owned_events = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(run_events)
                        .where(run_events.c.run_id.in_(owned))
                    )
                    or 0
                )
                foreign_runs = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(runs)
                        .where(runs.c.run_id.in_(foreign))
                    )
                    or 0
                )
                foreign_events = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(run_events)
                        .where(run_events.c.run_id.in_(foreign))
                    )
                    or 0
                )
                retained_usage = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(llm_usage)
                        .where(llm_usage.c.run_id.in_(owned))
                    )
                    or 0
                )
                retained_audit = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(audit_log)
                        .where(
                            audit_log.c.action == "run.deleted",
                            audit_log.c.resource_id.in_(owned),
                        )
                    )
                    or 0
                )
                return (
                    owned_runs,
                    owned_events,
                    foreign_runs,
                    foreign_events,
                    retained_usage,
                    retained_audit,
                )
        finally:
            await admin_engine.dispose()

    assert store._call(_counts()) == (0, 0, 3, 3, 1, 2)


@pytest.mark.asyncio
async def test_retained_deletion_receipts_block_session_recreation(engine) -> None:
    """Delayed autosaves cannot recreate a terminally deleted session."""

    now = time.time()
    factory = build_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(
                deletion_operations.insert(),
                [
                    {
                        "operation_id": "del_agent_session_receipt",
                        "tenant_id": "default",
                        "target_kind": "agent_session",
                        "target_id": "session-deleted-agent",
                        "manifest": [],
                        "context": {},
                        "status": "deleted",
                        "stage": "deleted",
                        "completed_items": 0,
                        "total_items": 0,
                        "workspace_id": None,
                        "created_by_user_id": None,
                        "created_at": now,
                        "updated_at": now,
                        "finished_at": now,
                    },
                    {
                        "operation_id": "del_knowledge_session_receipt",
                        "tenant_id": "default",
                        "target_kind": "knowledge_session",
                        "target_id": "session-deleted-knowledge",
                        "manifest": [],
                        "context": {},
                        "status": "deleted",
                        "stage": "deleted",
                        "completed_items": 0,
                        "total_items": 0,
                        "workspace_id": None,
                        "created_by_user_id": None,
                        "created_at": now,
                        "updated_at": now,
                        "finished_at": now,
                    },
                ],
            )

    agent_store = PostgresAgentSessionStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    knowledge_store = PostgresKnowledgeSessionStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    try:
        with pytest.raises(AgentSessionNotFound):
            await agent_store.claim_session(
                id="session-deleted-agent",
                title="stale",
                created_at=now,
                created_by_user_id=None,
                workspace_id=None,
            )
        with pytest.raises(KnowledgeSessionNotFound):
            await knowledge_store.claim_session(
                id="session-deleted-knowledge",
                title="stale",
                created_at=now,
                created_by_user_id=None,
                workspace_id=None,
            )
        with pytest.raises(KnowledgeSessionNotFound):
            await knowledge_store.upsert_session(
                id="session-deleted-knowledge",
                title="stale",
                items_json="[]",
                group_id=None,
                created_at=now,
                updated_at=now,
                created_by_user_id=None,
                workspace_id=None,
            )
    finally:
        await agent_store.aclose()
        await knowledge_store.aclose()


def test_metrics_snapshot_counts_running_rows(store):
    """The durable metrics snapshot counts real RUNNING rows; no capacity.

    Capacity is None on the durable backend (the worker fleet owns the
    slots), and the RUNNING count comes from a live grouped COUNT — so a
    genuinely executing run shows up as active=1.
    """
    import threading

    idle = store.metrics_snapshot()
    assert idle.active == 0
    assert idle.queued == 0
    assert idle.capacity is None

    release = threading.Event()

    def blocking_work(handle):
        handle.emit(
            "inqtrix.node.started",
            {"node": "classify", "snapshot": {"current_node": "classify"}},
        )
        release.wait(timeout=10)
        handle.complete(
            {"answer": "fertig"}, snapshot={"current_node": "answer", "done": True}
        )

    summary = submit_noop(store, work=blocking_work)
    try:
        wait_for_status(store, summary["run_id"], {"running"})
        snap = store.metrics_snapshot()
        assert snap.active == 1
        assert snap.capacity is None
    finally:
        release.set()
    wait_for_status(store, summary["run_id"], {"completed"})
    assert store.metrics_snapshot().active == 0


def test_owned_run_handle_reads_elapsed_time_without_public_visibility(store):
    observed: list[float] = []
    completed = threading.Event()

    def work(handle):
        observed.append(handle.total_elapsed_seconds())
        handle.complete(
            {"answer": "done"},
            snapshot={"current_node": "answer", "done": True},
        )
        completed.set()

    summary = submit_noop(
        store,
        work=work,
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    assert completed.wait(timeout=10)
    owner = UserContext(principal=Principal(user_id=OWNER_1, kind="oidc_session"))
    final = store.get(summary["run_id"], visible_to=owner)

    assert final["status"] == "completed"
    assert observed and observed[0] >= 0.0
    with pytest.raises(RunNotFound):
        store.get(summary["run_id"])


def test_import_completed_run_is_idempotent_and_owner_scoped(store):
    summary = store.import_completed_run(
        source_run_id="local_imported_pg",
        question="imported report",
        stack_name="default",
        result={"answer": "the report body", "metrics": {"rounds": 1}},
        created_at=1000.0,
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    assert set(summary.keys()) == RUN_SUMMARY_KEYS
    run_id = summary["run_id"]
    assert run_id != "local_imported_pg"
    assert summary["status"] == "completed"
    owner_1 = _scoped(OWNER_1)
    assert store.result(run_id, visible_to=owner_1)["answer"] == "the report body"

    # Idempotent re-import for the same owner: same row, body untouched.
    again = store.import_completed_run(
        source_run_id="local_imported_pg",
        question="imported report",
        stack_name="default",
        result={"answer": "ignored on re-import"},
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )
    assert again["run_id"] == run_id
    assert store.result(run_id, visible_to=owner_1)["answer"] == "the report body"

    # A foreign principal importing the SAME id gets a fresh id; A stays intact.
    foreign = store.import_completed_run(
        source_run_id="local_imported_pg",
        question="B",
        stack_name="default",
        result={"answer": "owner B body"},
        created_by_user_id=OWNER_2,
        created_by_tenant_id="default",
    )
    assert foreign["run_id"] != run_id
    assert store.result(run_id, visible_to=owner_1)["answer"] == "the report body"
    assert store.result(
        foreign["run_id"], visible_to=_scoped(OWNER_2)
    )["answer"] == "owner B body"


def test_share_facts_match_the_single_owner_and_title_reads(store):
    """The owner listing's batch read answers what the single reads answer,
    run by run, in one statement."""
    from inqtrix.auth.shares import ShareFacts

    owned = store.import_completed_run(
        source_run_id="local_share_facts_owned",
        question="Owned report?",
        stack_name="default",
        result={"answer": "body"},
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )["run_id"]
    legacy = store.import_completed_run(
        source_run_id="local_share_facts_legacy",
        question="Legacy report?",
        stack_name="default",
        result={"answer": "body"},
    )["run_id"]
    wanted = [owned, legacy, "run_share_facts_missing"]

    facts = store.share_facts(wanted)

    assert facts == {
        owned: ShareFacts(OWNER_1, "Owned report?"),
        legacy: ShareFacts(None, "Legacy report?"),
    }
    for run_id in wanted:
        found = facts.get(run_id)
        assert ((found.owner_user_id, found.title) if found else (None, None)) == (
            store.owner_user_id(run_id),
            store.title(run_id),
        )
    assert store.share_facts([]) == {}


def test_delete_removes_terminal_run_owner_scoped(store):
    imported = store.import_completed_run(
        source_run_id="local_delete_pg",
        question="report",
        stack_name="default",
        result={"answer": "the body", "metrics": {"rounds": 1}},
        workspace_id="ws_owner",
        created_by_user_id=OWNER_1,
        created_by_tenant_id="default",
    )

    run_id = imported["run_id"]
    # Owner-only, namespace-scoped: a foreign user and the wrong workspace are
    # both the indistinct 404, and the row stays.
    with pytest.raises(RunNotFound):
        store.delete(
            run_id,
            workspace_id="ws_owner",
            requester_user_id=INTRUDER,
        )
    with pytest.raises(RunNotFound):
        store.delete(
            run_id,
            workspace_id="ws_other",
            requester_user_id=OWNER_1,
        )
    assert store.get(
        run_id,
        workspace_id="ws_owner",
        visible_to=_scoped(OWNER_1),
    )["run_id"] == run_id

    # The owner deletes it durably; the row (and its cascaded events) are gone.
    store.delete(
        run_id,
        workspace_id="ws_owner",
        requester_user_id=OWNER_1,
    )
    with pytest.raises(RunNotFound):
        store.get(run_id, workspace_id="ws_owner")


def test_event_stream_is_gap_free_with_companions_first(store):
    summary = submit_noop(store)
    wait_for_status(store, summary["run_id"], {"completed"})

    subscription = store.subscribe(summary["run_id"])
    try:
        events = subscription.replay
    finally:
        subscription.close()

    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(sequences) + 1))
    types = [event["type"] for event in events]
    assert types[0] == "inqtrix.run.queued"
    # Snapshot companions precede their carrier events.
    for index, event_type in enumerate(types):
        if event_type in {"inqtrix.run.started", "inqtrix.run.completed"}:
            assert types[index - 1] == "inqtrix.run.snapshot"
    assert types[-1] == "inqtrix.run.completed"
    for event in events:
        assert set(event.keys()) == {
            "type",
            "run_id",
            "sequence",
            "created_at",
            "data",
        }


def test_failed_work_lands_as_sanitized_failure(store):
    def boom(handle):
        raise RuntimeError("provider exploded")

    summary = submit_noop(store, work=boom)
    final = wait_for_status(store, summary["run_id"], {"failed"})
    assert final["error"] == {
        "message": "provider exploded",
        "type": "server_error",
    }
    with pytest.raises(RunNotFound):
        store.result(summary["run_id"])


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (
            AgentRateLimited("model-a", RuntimeError("429")),
            "rate_limited",
        ),
        (AgentProviderTimeout("provider request"), "provider_timeout"),
    ],
)
def test_no_queue_work_preserves_typed_native_failures(
    store,
    failure: Exception,
    error_type: str,
) -> None:
    def boom(_handle):
        raise failure

    summary = submit_noop(store, work=boom)
    final = wait_for_status(store, summary["run_id"], {"failed"})

    assert final["error"]["type"] == error_type


def test_cancel_of_queued_run_is_immediate(store):
    import threading

    release = threading.Event()

    def slow(handle):
        release.wait(10)
        handle.complete({"answer": "spaet"})

    first = submit_noop(store, work=slow)
    second = submit_noop(store, work=slow)
    queued = submit_noop(store, work=slow)
    try:
        cancelled = store.cancel(queued["run_id"])
        assert cancelled["status"] == "cancelled"
        events = store.subscribe(queued["run_id"])
        try:
            assert events.replay[-1]["type"] == "inqtrix.run.cancelled"
            assert events.replay[-1]["data"]["reason"] == "cancelled_before_start"
        finally:
            events.close()
    finally:
        release.set()
        wait_for_status(store, first["run_id"], {"completed"})
        wait_for_status(store, second["run_id"], {"completed"})


def test_cancel_pending_summary_exposes_cancel_requested(store):
    """Parity with the in-memory store: pending cancel is summary-visible.

    ``cancel_requested`` is absent on a plain running summary, ``True``
    while a cancel of a RUNNING run is pending, and absent again after
    the terminal transition (historical shape byte-identical).
    """
    import threading

    started = threading.Event()

    def cancellable(handle):
        started.set()
        if handle.cancel_event.wait(10):
            handle.cancel("client_requested_cancel")

    summary = submit_noop(store, work=cancellable)
    run_id = summary["run_id"]
    assert started.wait(10)
    running = wait_for_status(store, run_id, {"running"})
    assert "cancel_requested" not in running

    cancelled = store.cancel(run_id)

    assert cancelled["status"] == "running"
    assert cancelled["cancel_requested"] is True
    final = wait_for_status(store, run_id, {"cancelled"})
    assert "cancel_requested" not in final


def test_scoped_visibility_denies_with_404_semantics(store):
    summary = submit_noop(
        store,
        created_by_user_id=USER_A,
        created_by_tenant_id="default",
    )
    wait_for_status(
        store,
        summary["run_id"],
        {"completed"},
        visible_to=_scoped(USER_A),
    )

    foreign = UserContext(
        principal=Principal(user_id=USER_B, kind="oidc_session", tenant_id="default"),
        workspace_ids=(),
    )
    with pytest.raises(RunNotFound):
        store.get(summary["run_id"], visible_to=foreign)
    assert store.list(visible_to=foreign) == []

    owner = UserContext(
        principal=Principal(user_id=USER_A, kind="oidc_session", tenant_id="default"),
        workspace_ids=(),
    )
    assert store.get(summary["run_id"], visible_to=owner)["status"] == ("completed")


def test_claim_fencing_discards_zombie_terminal_writes(store):
    import threading

    release = threading.Event()

    def slow(handle):
        release.wait(10)
        handle.complete({"answer": "spaet"})

    try:
        # Fill both execution slots so the third run STAYS queued and
        # the worker-claim path can be exercised directly.
        submit_noop(store, work=slow)
        submit_noop(store, work=slow)
        third = submit_noop(store, work=slow)
        run_id = third["run_id"]
        assert store.get(run_id)["status"] == "queued"

        first = store.claim_for_execution(run_id, "default", allow_takeover=False)
        assert first is not None and first.attempt == 1
        # A fresh duplicate message must NOT steal a healthy run...
        assert (
            store.claim_for_execution(run_id, "default", allow_takeover=False) is None
        )
        # ...but a reclaim (owner stopped heartbeating) takes over.
        second = store.claim_for_execution(run_id, "default", allow_takeover=True)
        assert second is not None and second.attempt == 2

        # The zombie's write (old fence) is a discarded no-op...
        store.complete(run_id, {"answer": "zombie"}, fence_attempt=1)
        assert store.get(run_id)["status"] == "running"
        # ...while the current attempt's write lands.
        store.complete(run_id, {"answer": "echt"}, fence_attempt=2)
        assert store.get(run_id)["status"] == "completed"
        assert store.result(run_id)["answer"] == "echt"
    finally:
        release.set()


def test_orphan_sweep_fails_stale_rows_on_first_touch(store):
    """Queued/running rows from a dead process must not stay
    eternally running: a fresh no-queue store sweeps them visibly."""
    import threading

    release = threading.Event()

    def slow(handle):
        release.wait(10)
        handle.complete({"answer": "spaet"})

    try:
        running = submit_noop(store, work=slow)
        second = submit_noop(store, work=slow)
        third = submit_noop(store, work=slow)
        assert store.get(third["run_id"])["status"] == "queued"

        # Simulated restart: a second no-queue store over the same DB.
        restarted = PostgresRunStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            max_concurrent=2,
            max_queue_size=10,
            completed_ttl_seconds=300,
            worker_id="pytest-orphan",
        )
        try:
            swept = restarted.get(third["run_id"])
            assert swept["status"] == "failed"
            assert swept["error"]["type"] == "server_restarted"
            events = restarted.subscribe(third["run_id"])
            try:
                assert events.replay[-1]["type"] == "inqtrix.run.failed"
            finally:
                events.close()
            assert restarted.get(running["run_id"])["status"] == "failed"
            assert restarted.get(second["run_id"])["status"] == "failed"
        finally:
            restarted.close()
    finally:
        release.set()


def test_worker_shape_store_never_sweeps_foreign_rows(store):
    """The queue-backed worker builds its store with ``queue=None``
    (claim-mode wiring) — with ``recover_orphans=False`` its first DB
    touch must NOT fail runs another process still owns. Regression for
    the deployment-wide "Verwaister Run" sweep on worker start."""
    import threading

    release = threading.Event()

    def slow(handle):
        release.wait(10)
        handle.complete({"answer": "spaet"})

    try:
        running = submit_noop(store, work=slow)
        wait_for_status(store, running["run_id"], {"running"})

        worker_shaped = PostgresRunStore(
            engine=build_engine(TEST_DATABASE_URL),
            app_role=APP_ROLE,
            queue=None,
            recover_orphans=False,
            max_concurrent=2,
            max_queue_size=10,
            completed_ttl_seconds=300,
            worker_id="pytest-worker-shape",
        )
        try:
            # get() runs the lazy cleanup — the foreign RUNNING row
            # must survive it untouched.
            assert worker_shaped.get(running["run_id"])["status"] == "running"
        finally:
            worker_shaped.close()
    finally:
        release.set()
    wait_for_status(store, running["run_id"], {"completed"})


def test_stuck_row_cap_spares_terminal_rows(store):
    """The retention failsafe targets NON-terminal rows only —
    terminal rows are governed by the regular TTL."""
    import asyncio as aio

    from sqlalchemy import update as sa_update

    from inqtrix.storage.runs_orm import runs as runs_table

    summary = submit_noop(store)
    wait_for_status(store, summary["run_id"], {"completed"})

    async def backdate():
        async with store._session("default") as session:
            await session.execute(
                sa_update(runs_table)
                .where(runs_table.c.run_id == summary["run_id"])
                .values(created_at=time.time() - 30 * 86_400)
            )

    aio.run_coroutine_threadsafe(backdate(), store._loop).result()
    # Any read runs the lazy cleanup; the terminal row must survive
    # the stuck-row cap (its finished_at is fresh, the TTL governs).
    assert store.get(summary["run_id"])["status"] == "completed"


def test_enqueue_failure_keeps_the_accepted_run(engine):
    """A broker blip after the row commit must not turn an accepted
    run into a 500 — the reconciler re-dispatches it later."""

    class BrokenQueue:
        def enqueue(self, *, run_id, tenant_id):
            raise ConnectionError("broker weg")

    broken = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=BrokenQueue(),
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-enqueue",
    )
    try:
        summary = broken.submit(
            question="Broker-Test",
            stack_name="default",
            work=lambda handle: None,
            request_payload={"question": "x", "body": {"mode": "research"}},
        )
        assert summary["status"] == "queued"
        assert broken.get(summary["run_id"])["status"] == "queued"
        assert broken.stale_queued_runs(older_than_seconds=0) != []
    finally:
        broken.close()


def test_ttl_cleanup_removes_old_terminal_runs(store):
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=1,
        worker_id="pytest-ttl",
    )
    try:
        summary = submit_noop(store)
        # The run completes, ages past the 1s retention, and the lazy
        # cleanup on a later read evicts it — eviction shows up as the
        # canonical RunNotFound, indistinguishable from never-existed.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                store.get(summary["run_id"])
            except RunNotFound:
                break
            time.sleep(0.2)
        else:
            pytest.fail("terminal run was never TTL-evicted")
        assert all(item["run_id"] != summary["run_id"] for item in store.list())
    finally:
        store.close()


def test_an_incognito_run_ages_out_after_its_own_window(store):
    """An incognito ask leaves the database once its short window has
    passed, while an ordinary run of the same age stays for the ordinary
    retention."""
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=3600,
        incognito_ttl_seconds=1,
        worker_id="pytest-incognito-ttl",
    )

    def done(handle):
        handle.complete({"answer": "fertig", "metrics": {"rounds": 1}})

    def ask(body: dict) -> dict:
        return store.submit(
            question="Wie ist die Haftung geregelt?",
            stack_name="default",
            work=done,
            mode="knowledge",
            request_payload={"question": "x", "body": body},
        )

    try:
        incognito = ask({"mode": "knowledge", "incognito": True})
        ordinary = ask({"mode": "knowledge"})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                store.get(incognito["run_id"])
            except RunNotFound:
                break
            time.sleep(0.2)
        else:
            pytest.fail("incognito run was never evicted")
        while store.get(ordinary["run_id"])["status"] != "completed":
            assert time.monotonic() < deadline, "ordinary run never completed"
            time.sleep(0.2)
        time.sleep(1.2)
        assert store.get(ordinary["run_id"])["status"] == "completed"
    finally:
        store.close()


def test_a_sweep_removes_an_expired_incognito_run_without_any_access(store):
    """The incognito window holds while nobody reads the database: the
    expired ask stays until one sweep, which the API process runs on a
    clock, removes the row with its events and records the deletion, while
    an ordinary run of the same age stays."""
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=3600,
        incognito_ttl_seconds=1,
        worker_id="pytest-incognito-sweep",
    )

    def done(handle):
        handle.complete({"answer": "fertig", "metrics": {"rounds": 1}})

    def ask(body: dict) -> str:
        return store.submit(
            question="Wie ist die Haftung geregelt?",
            stack_name="default",
            work=done,
            mode="knowledge",
            request_payload={"question": "x", "body": body},
        )["run_id"]

    async def _facts(run_ids: list[str]):
        # Read past the store (and its cleanup on access) as the admin.
        admin_engine = build_engine(TEST_DATABASE_URL)
        factory = build_session_factory(admin_engine)
        try:
            async with factory() as session:
                statuses = dict(
                    (
                        await session.execute(
                            select(runs.c.run_id, runs.c.status).where(
                                runs.c.run_id.in_(run_ids)
                            )
                        )
                    ).all()
                )
                events = {
                    run_id: int(
                        await session.scalar(
                            select(func.count())
                            .select_from(run_events)
                            .where(run_events.c.run_id == run_id)
                        )
                        or 0
                    )
                    for run_id in run_ids
                }
                audited = set(
                    (
                        await session.execute(
                            select(audit_log.c.resource_id).where(
                                audit_log.c.action == "run.retention_deleted",
                                audit_log.c.resource_id.in_(run_ids),
                            )
                        )
                    ).scalars()
                )
                return statuses, events, audited
        finally:
            await admin_engine.dispose()

    try:
        incognito = ask({"mode": "knowledge", "incognito": True})
        ordinary = ask({"mode": "knowledge"})
        both = [incognito, ordinary]
        deadline = time.monotonic() + 15
        while store._call(_facts(both))[0] != {
            incognito: "completed",
            ordinary: "completed",
        }:
            assert time.monotonic() < deadline, "runs never completed"
            time.sleep(0.2)
        time.sleep(1.5)
        statuses, events, audited = store._call(_facts(both))
        # Nobody has read the store since the window closed.
        assert set(statuses) == {incognito, ordinary}
        assert events[incognito] > 0
        assert audited == set()

        store.sweep()

        statuses, events, audited = store._call(_facts(both))
        assert statuses == {ordinary: "completed"}
        assert events[incognito] == 0
        assert events[ordinary] > 0
        assert audited == {incognito}
    finally:
        store.close()


def _scoped(user_id: uuid.UUID) -> UserContext:
    return UserContext(
        principal=Principal(user_id=user_id, kind="oidc_session", tenant_id="default"),
        workspace_ids=(),
    )


def test_live_direct_share_admits_reads_and_gates_cancel(store):
    """The durable store resolves consent, permission, and revoke live.

    Accepted shared-in runs bypass the recipient's workspace namespace and
    carry the canonical access object. A view share admits reads but not
    mutation; a committed edit upgrade is effective immediately, and revoke
    removes the resource without a caller-supplied grant cache.
    """

    shared = submit_noop(
        store,
        created_by_user_id=SHARE_OWNER,
        created_by_tenant_id="default",
        workspace_id="ws-owner",
    )
    own = submit_noop(
        store,
        created_by_user_id=SHARE_RECIPIENT,
        created_by_tenant_id="default",
        workspace_id="ws-recipient",
    )
    wait_for_status(
        store,
        shared["run_id"],
        {"completed"},
        visible_to=_scoped(SHARE_OWNER),
    )
    wait_for_status(
        store,
        own["run_id"],
        {"completed"},
        visible_to=_scoped(SHARE_RECIPIENT),
    )
    recipient = _scoped(SHARE_RECIPIENT)
    identity = PostgresIdentityBackend(
        session_factory=store._session_factory,
        app_role=APP_ROLE,
    )
    (pending_share,) = store._call(
        identity.create_shares(
            tenant_id="default",
            resource_type="run",
            resource_id=shared["run_id"],
            owner_user_id=SHARE_OWNER,
            granted_by_user_id=SHARE_OWNER,
            invitees=((SHARE_RECIPIENT, SharePermission.VIEW),),
        )
    )

    with pytest.raises(RunNotFound):
        store.get(shared["run_id"], visible_to=recipient)
    accepted_share = store._call(
        identity.accept_share_by_id(
            tenant_id="default",
            share_id=pending_share.id,
            recipient_user_id=SHARE_RECIPIENT,
            owner_user_id=SHARE_OWNER,
        )
    )
    assert accepted_share is not None

    summary = store.get(shared["run_id"], visible_to=recipient)
    assert summary["access"] == {"mode": "shared", "permission": "view"}
    assert store.get(own["run_id"], visible_to=recipient)["access"] == {"mode": "owner"}

    listed = store.list(
        workspace_id="ws-recipient",
        visible_to=recipient,
    )
    by_id = {item["run_id"]: item for item in listed}
    assert set(by_id) == {own["run_id"], shared["run_id"]}
    assert by_id[shared["run_id"]]["workspace_id"] == "ws-owner"
    assert by_id[shared["run_id"]]["access"]["permission"] == "view"
    assert by_id[own["run_id"]]["access"] == {"mode": "owner"}

    result = store.result(shared["run_id"], visible_to=recipient)
    assert result["answer"] == "fertig"
    assert store.source_origins_visible([own["run_id"], shared["run_id"]], visible_to=recipient)
    assert store.source_origins_visible([own["run_id"]], visible_to=recipient, owner_only=True)
    assert not store.source_origins_visible([shared["run_id"]], visible_to=recipient, owner_only=True)
    assert not store.source_origins_visible([own["run_id"], "missing"], visible_to=recipient)
    with pytest.raises(RunNotFound):
        store.source_request_body(shared["run_id"], visible_to=recipient)
    assert store.source_request_body(own["run_id"], visible_to=recipient) == {"mode": "research"}


    subscription = store.subscribe(shared["run_id"], visible_to=recipient)
    try:
        assert any(
            event["type"] == "inqtrix.run.completed" for event in subscription.replay
        )
    finally:
        subscription.close()

    with pytest.raises(RunNotFound):
        store.cancel(shared["run_id"], visible_to=recipient)
    updated_share = store._call(
        identity.update_share_permission(
            tenant_id="default",
            share_id=pending_share.id,
            permission=SharePermission.EDIT,
            expected_revision=accepted_share.revision,
            actor_user_id=SHARE_OWNER,
        )
    )
    assert updated_share is not None
    cancelled = store.cancel(shared["run_id"], visible_to=recipient)
    assert cancelled["status"] == "completed"
    assert cancelled["access"] == {"mode": "shared", "permission": "edit"}

    revoked_share = store._call(
        identity.revoke_share_by_id(
            tenant_id="default",
            share_id=pending_share.id,
            revoked_by_user_id=SHARE_OWNER,
            owner_user_id=SHARE_OWNER,
        )
    )
    assert revoked_share is not None
    with pytest.raises(RunNotFound):
        store.get(shared["run_id"], visible_to=recipient)
    assert {
        item["run_id"]
        for item in store.list(workspace_id="ws-recipient", visible_to=recipient)
    } == {own["run_id"]}
    with pytest.raises(RunNotFound):
        store.get(shared["run_id"], visible_to=_scoped(INTRUDER))


# ---------------------------------------------------------------------------
# agent run tree + waiting statuses (lockstep with the memory store)
# ---------------------------------------------------------------------------


def test_agent_tree_keys_and_children_listing(store):
    calls = {"count": 0}

    def parked_parent(handle):
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        handle.complete({"answer": "fortgesetzt"})

    parent = submit_noop(
        store,
        work=parked_parent,
        kind="agent",
        session_id="sess-pg",
    )
    wait_for_status(store, parent["run_id"], {"waiting_for_approval"})
    assert parent["kind"] == "agent"
    assert parent["children_url"] == f"/v1/runs/{parent['run_id']}/children"
    assert parent["session_id"] == "sess-pg"

    first = submit_noop(
        store,
        question="erste Teilaufgabe",
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    time.sleep(0.02)
    second = submit_noop(
        store,
        question="zweite Teilaufgabe",
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    for summary in (first, second):
        wait_for_status(store, summary["run_id"], {"completed"})

    assert first["parent_run_id"] == parent["run_id"]
    assert first["root_run_id"] == parent["run_id"]
    children = store.children(parent["run_id"])
    assert [child["run_id"] for child in children] == [
        second["run_id"],
        first["run_id"],
    ]
    store.resume_run(parent["run_id"])
    wait_for_status(store, parent["run_id"], {"completed"})
    # Standard runs stay byte-identical: no agent-tree keys.
    standard = submit_noop(store, question="normaler Lauf")
    assert set(standard.keys()) == RUN_SUMMARY_KEYS
    wait_for_status(store, standard["run_id"], {"completed"})


def test_waiting_lifecycle_parks_resumes_and_completes(store):
    calls = {"count": 0}

    def segmented(handle):
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        handle.complete({"answer": "fortgesetzt"})

    summary = submit_noop(store, work=segmented)
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"waiting_for_approval"})
    # The auto-complete safety net must NOT complete the parked run.
    time.sleep(0.2)
    assert store.get(run_id)["status"] == "waiting_for_approval"

    resumed = store.resume_run(run_id)
    assert resumed["status"] in {"queued", "running"}
    wait_for_status(store, run_id, {"completed"})
    assert calls["count"] == 2
    assert store.result(run_id)["answer"] == "fortgesetzt"

    events = store.subscribe(run_id)
    try:
        types = [event["type"] for event in events.replay]
        assert "inqtrix.run.waiting" in types
        queued = [
            event for event in events.replay if event["type"] == "inqtrix.run.queued"
        ]
        assert queued[-1]["data"].get("resumed") is True
        sequences = [event["sequence"] for event in events.replay]
        assert sequences == list(range(1, len(sequences) + 1))
    finally:
        events.close()


def test_cancel_while_waiting_cascades_over_children(store):
    parent = submit_noop(
        store,
        work=lambda handle: handle.wait("waiting_for_input"),
        kind="agent",
    )
    wait_for_status(store, parent["run_id"], {"waiting_for_input"})
    child = submit_noop(
        store,
        question="Teilaufgabe",
        work=lambda handle: handle.wait("waiting_for_input"),
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    wait_for_status(store, child["run_id"], {"waiting_for_input"})

    cancelled = store.cancel(parent["run_id"])

    assert cancelled["status"] == "cancelled"
    assert store.get(child["run_id"])["status"] == "cancelled"
    for run_id in (parent["run_id"], child["run_id"]):
        events = store.subscribe(run_id)
        try:
            assert events.replay[-1]["type"] == "inqtrix.run.cancelled"
            assert events.replay[-1]["data"]["reason"] == "cancelled_while_waiting"
        finally:
            events.close()
    with pytest.raises(RunActive):
        store.resume_run(parent["run_id"])


def test_parent_failure_cascades_over_children_with_reason(store):
    """A FAILED parent cancels its live children stamped ``parent_failed``.

    The orphan-cleanup path distinct from an explicit cancel: the failure
    runs through ``_terminal_db``'s FAILED branch (fenced after the CAS),
    the subtree lock, and ``_cancel_row_db``/``_cancel_waiting_row_db``
    with the cascade reason threaded through — verified against real
    Postgres, where the fencing and lock order cannot be unit-simulated.
    """
    parent = submit_noop(
        store,
        work=lambda handle: handle.wait("waiting_for_input"),
        kind="agent",
    )
    wait_for_status(store, parent["run_id"], {"waiting_for_input"})
    child = submit_noop(
        store,
        question="Teilaufgabe",
        work=lambda handle: handle.wait("waiting_for_input"),
        kind="agent_child",
        parent_run_id=parent["run_id"],
        root_run_id=parent["run_id"],
    )
    wait_for_status(store, child["run_id"], {"waiting_for_input"})

    assert store.fail(parent["run_id"], "Simulierter Elternfehler") is True

    assert store.get(parent["run_id"])["status"] == "failed"
    child_final = wait_for_status(store, child["run_id"], {"cancelled"})
    assert child_final["status"] == "cancelled"
    events = store.subscribe(child["run_id"])
    try:
        assert events.replay[-1]["type"] == "inqtrix.run.cancelled"
        assert events.replay[-1]["data"]["reason"] == "parent_failed"
    finally:
        events.close()


def test_waiting_ttl_auto_cancels_with_approval_timeout(engine):
    impatient = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-worker-ttl",
        waiting_ttl_seconds=0.4,
    )
    try:
        summary = submit_noop(
            impatient,
            work=lambda handle: handle.wait("waiting_for_approval"),
        )
        run_id = summary["run_id"]
        wait_for_status(impatient, run_id, {"waiting_for_approval"})
        time.sleep(0.5)

        # Any store touch runs the sweep.
        final = wait_for_status(impatient, run_id, {"cancelled"})
        assert final["status"] == "cancelled"
        events = impatient.subscribe(run_id)
        try:
            assert events.replay[-1]["type"] == "inqtrix.run.cancelled"
            assert events.replay[-1]["data"]["reason"] == "approval_timeout"
        finally:
            events.close()
    finally:
        impatient.close()


def test_a_sweep_releases_the_closure_of_a_wait_it_timed_out(engine):
    """A sweep is the whole cleanup an access runs, post-commit handoffs
    included: the parked run it times out gives back its retained
    in-process closure at once, not at some later access."""
    impatient = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-worker-sweep-ttl",
        waiting_ttl_seconds=0.4,
    )
    try:
        run_id = submit_noop(
            impatient,
            work=lambda handle: handle.wait("waiting_for_approval"),
        )["run_id"]
        wait_for_status(impatient, run_id, {"waiting_for_approval"})
        assert run_id in impatient._local
        time.sleep(0.5)

        impatient.sweep()

        assert run_id not in impatient._local
        assert wait_for_status(impatient, run_id, {"cancelled"})["status"] == "cancelled"
    finally:
        impatient.close()


def test_waiting_child_ttl_projects_terminal_and_wakes_parent(store):
    del store
    impatient = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-child-ttl",
        waiting_ttl_seconds=10.0,
    )
    calls = {"count": 0}
    child_id = {"value": ""}

    def child_work(handle):
        handle.wait("waiting_for_input")

    def parent_work(handle):
        calls["count"] += 1
        if calls["count"] == 1:
            child = impatient.submit(
                question="Kind-Recherche",
                stack_name="default",
                work=child_work,
                kind="agent_child",
                parent_run_id=handle.run_id,
                root_run_id=handle.run_id,
                request_payload={
                    "body": {
                        "parent_task_id": "task-child-ttl",
                        "parent_task_attempt": 1,
                    }
                },
            )
            child_id["value"] = child["run_id"]
            handle.wait("waiting_for_children")
            return
        handle.complete({"answer": "resumed", "metrics": {}})

    try:
        parent = impatient.submit(
            question="Agent-Auftrag",
            stack_name="default",
            work=parent_work,
            kind="agent",
            request_payload={"body": {"mode": "workspace_agent"}},
        )
        parent_id = parent["run_id"]
        wait_for_status(impatient, parent_id, {"waiting_for_children"})
        wait_for_status(impatient, child_id["value"], {"waiting_for_input"})

        import asyncio

        async def _age_child_wait() -> None:
            aging_engine = build_engine(TEST_DATABASE_URL)
            factory = build_session_factory(aging_engine)
            try:
                async with factory() as session:
                    async with session.begin():
                        await session.execute(
                            text(
                                "UPDATE runs SET waiting_since = :old "
                                "WHERE run_id = :run_id"
                            ),
                            {
                                "old": time.time() - 20.0,
                                "run_id": child_id["value"],
                            },
                        )
            finally:
                await aging_engine.dispose()

        asyncio.run(_age_child_wait())
        assert impatient.get(child_id["value"])["status"] == "cancelled"
        wait_for_status(impatient, parent_id, {"completed"})

        child_events = impatient.subscribe(child_id["value"])
        try:
            assert child_events.replay[-1]["type"] == "inqtrix.run.cancelled"
            assert child_events.replay[-1]["data"]["reason"] == ("approval_timeout")
        finally:
            child_events.close()

        events = impatient.subscribe(parent_id)
        try:
            projected = [
                event
                for event in events.replay
                if event["type"] == "inqtrix.agent.child.progress"
            ]
            assert projected[-1]["data"]["run_status"] == "cancelled"
            resumed = [
                event
                for event in events.replay
                if event["type"] == "inqtrix.run.queued"
                and event["data"].get("resumed")
            ]
            assert projected[-1]["sequence"] < resumed[-1]["sequence"]
        finally:
            events.close()
        assert calls["count"] == 2
    finally:
        impatient.close()


def test_orphan_sweep_spares_waiting_rows(store):
    summary = submit_noop(
        store, work=lambda handle: handle.wait("waiting_for_approval")
    )
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"waiting_for_approval"})

    # A second process (fresh store instance) sweeps queued/running
    # orphans on first touch — a parked run must survive it: in queue
    # mode any worker can resume it later.
    second = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-worker-2",
    )
    try:
        assert second.get(run_id)["status"] == "waiting_for_approval"
        # But the closure died with the first process: resuming from
        # the restarted no-queue store fails loudly instead of hanging.
        with pytest.raises(RunActive):
            second.resume_run(run_id)
    finally:
        second.close()


def test_stuck_row_failsafe_spares_waiting_rows(store, engine):
    """A parked run aged past the stuck threshold must NOT be deleted.

    The stuck failsafe keys on created_at while the waiting TTL keys on
    waiting_since — without the waiting exclusion the failsafe would
    always fire first and erase the parked run without any event.
    """
    calls = {"count": 0}

    def segmented(handle):
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        handle.complete({"answer": "nach dem Sweep fortgesetzt"})

    summary = submit_noop(store, work=segmented)
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"waiting_for_approval"})

    # Age the row past the stuck threshold; keep the wait fresh. A
    # dedicated engine: the fixture engine is bound to another loop.
    import asyncio

    async def _age():
        aging_engine = build_engine(TEST_DATABASE_URL)
        try:
            factory = build_session_factory(aging_engine)
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "UPDATE runs SET created_at = :old " "WHERE run_id = :rid"
                        ),
                        {"old": time.time() - 30 * 86_400, "rid": run_id},
                    )
        finally:
            await aging_engine.dispose()

    asyncio.run(_age())

    # Any store touch runs the cleanup, including the stuck failsafe.
    assert store.get(run_id)["status"] == "waiting_for_approval"
    resumed = store.resume_run(run_id)
    assert resumed["status"] in {"queued", "running"}
    wait_for_status(store, run_id, {"completed"})


def test_waiting_ttl_sweep_does_not_deadlock_concurrent_dispatch(engine):
    """TTL sweep firing while runs dispatch must not freeze the store.

    The sweep runs on the store's event loop. Releasing local closures there
    while a dispatcher holds the store lock and waits on that loop deadlocks
    every later call.
    """
    impatient = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=20,
        completed_ttl_seconds=300,
        worker_id="pytest-worker-deadlock",
        waiting_ttl_seconds=0.4,
    )
    try:
        parked = submit_noop(
            impatient,
            work=lambda handle: handle.wait("waiting_for_approval"),
        )
        wait_for_status(impatient, parked["run_id"], {"waiting_for_approval"})
        time.sleep(0.5)

        # Concurrent submitters (dispatch holds the store lock while
        # blocking on the store loop) and readers (their cleanup sweep
        # runs ON that loop and records the timed-out parked run).
        # With the regression this interleaving wedges the whole store.
        import threading

        errors: list[BaseException] = []

        def _submit_and_wait() -> None:
            try:
                summary = submit_noop(impatient)
                wait_for_status(impatient, summary["run_id"], {"completed"})
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)

        def _read_loop() -> None:
            try:
                for _ in range(20):
                    impatient.list()
                    time.sleep(0.01)
            except BaseException as exc:  # noqa: BLE001 — surfaced below
                errors.append(exc)

        threads = [threading.Thread(target=_submit_and_wait) for _ in range(4)]
        threads += [threading.Thread(target=_read_loop) for _ in range(2)]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + 15
        for thread in threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads):
            pytest.fail("store deadlocked: workers never finished")
        assert not errors, errors
        final = wait_for_status(impatient, parked["run_id"], {"cancelled"})
        assert final["status"] == "cancelled"
    finally:
        impatient.close()


def test_mark_waiting_rejections_match_memory(store):
    summary = submit_noop(store)
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"completed"})

    with pytest.raises(ValueError, match="not a waiting status"):
        store.mark_waiting(run_id, status="running")
    with pytest.raises(RunActive):
        store.mark_waiting(run_id, status="waiting_for_input")
    with pytest.raises(RunNotFound):
        store.mark_waiting("run_unbekannt", status="waiting_for_input")


def test_fenced_park_with_pending_cancel_resolves_as_cancel(store):
    """A fenced park whose OWN attempt lost the CAS to a pending cancel
    must resolve as cancel (memory parity) — a fenced miss is only a
    zombie signal when claimed_by/attempt point at ANOTHER attempt."""
    import threading

    release = threading.Event()

    def slow(handle):
        release.wait(10)
        handle.complete({"answer": "spaet"})

    try:
        submit_noop(store, work=slow)
        submit_noop(store, work=slow)
        own = submit_noop(store, work=slow)["run_id"]
        zombie = submit_noop(store, work=slow)["run_id"]

        claim = store.claim_for_execution(own, "default", allow_takeover=False)
        assert claim is not None and claim.attempt == 1
        # Two-phase request against the running claim...
        store.cancel(own)
        # ...then the worker parks: the pending cancel wins, the run
        # ends CANCELLED — not RunActive -> worker fail() -> FAILED.
        store.mark_waiting(own, status="waiting_for_approval", fence_attempt=1)
        assert store.get(own)["status"] == "cancelled"
        events = store.subscribe(own)
        try:
            assert events.replay[-1]["type"] == "inqtrix.run.cancelled"
            assert events.replay[-1]["data"]["reason"] == "cancelled_while_waiting"
        finally:
            events.close()

        # The zombie guard stays: a superseded attempt's park neither
        # parks nor cancels the live attempt's run.
        first = store.claim_for_execution(zombie, "default", allow_takeover=False)
        assert first is not None and first.attempt == 1
        second = store.claim_for_execution(zombie, "default", allow_takeover=True)
        assert second is not None and second.attempt == 2
        with pytest.raises(RunActive, match="another worker attempt"):
            store.mark_waiting(zombie, status="waiting_for_approval", fence_attempt=1)
        assert store.get(zombie)["status"] == "running"
    finally:
        release.set()


def test_resume_error_types_match_memory(store):
    with pytest.raises(RunNotFound):
        store.resume_run("run_unbekannt")

    summary = submit_noop(store)
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"completed"})
    with pytest.raises(RunActive, match="is not waiting"):
        store.resume_run(run_id)


def test_emit_after_terminal_is_dropped_loudly(store):
    summary = submit_noop(store)
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"completed"})

    store.emit(run_id, "inqtrix.agent.artifact.updated", {"revision": 2})

    events = store.subscribe(run_id)
    try:
        assert events.replay[-1]["type"] == "inqtrix.run.completed"
    finally:
        events.close()


def test_child_projection_cannot_follow_parent_terminal_event(store):
    """Parent lock orders a racing child projection before terminalization."""
    del store
    import asyncio
    from types import MethodType

    queue = _RecordingDispatchQueue()
    projector = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-projector",
    )
    terminator = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        dispatch_queue=queue,
        recover_orphans=False,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-terminator",
    )
    projection_selected = threading.Event()
    release_projection = threading.Event()
    errors: list[BaseException] = []
    try:
        seed_agent_session(projector, "session-projection-order")
        parent = projector.submit(
            question="parent",
            stack_name="default",
            work=lambda _handle: None,
            kind="agent",
            session_id="session-projection-order",
            request_payload={"body": {"mode": "workspace_agent"}},
        )
        child = projector.submit(
            question="child",
            stack_name="default",
            work=lambda _handle: None,
            kind="agent_child",
            parent_run_id=parent["run_id"],
            root_run_id=parent["run_id"],
            request_payload={"body": {"parent_task_id": "task-projection-order"}},
        )
        original_append = projector._append_events_db

        async def _gated_append(
            self,
            session,
            run_id,
            tenant_id,
            events,
        ):
            del self
            if run_id == parent["run_id"] and any(
                event_type == "inqtrix.agent.child.progress"
                for event_type, _payload in events
            ):
                projection_selected.set()
                assert await asyncio.to_thread(release_projection.wait, 10.0)
            await original_append(session, run_id, tenant_id, events)

        projector._append_events_db = MethodType(  # type: ignore[method-assign]
            _gated_append, projector
        )

        def _emit_child() -> None:
            try:
                projector.emit(
                    child["run_id"],
                    "inqtrix.node.started",
                    {"node": "search", "snapshot": {"current_node": "search"}},
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _fail_parent() -> None:
            try:
                terminator.fail(parent["run_id"], "terminal race")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        child_thread = threading.Thread(target=_emit_child)
        child_thread.start()
        assert projection_selected.wait(timeout=10.0)
        terminal_thread = threading.Thread(target=_fail_parent)
        terminal_thread.start()
        time.sleep(0.1)
        assert terminal_thread.is_alive()
        release_projection.set()
        child_thread.join(timeout=10.0)
        terminal_thread.join(timeout=10.0)
        assert not child_thread.is_alive()
        assert not terminal_thread.is_alive()
        assert not errors, errors

        events = terminator.subscribe(parent["run_id"])
        try:
            types = [event["type"] for event in events.replay]
            assert "inqtrix.agent.child.progress" in types
            assert types[-1] == "inqtrix.run.failed"
        finally:
            events.close()
    finally:
        release_projection.set()
        projector.close()
        terminator.close()


# -- children park-and-resume (waiting_for_children) ------------------------ #


class _SegmentedParent:
    """Two-segment parent work: submit children + park, then complete.

    Mirrors the checkpointed agent algorithm: the first dispatch
    submits the children and parks ``waiting_for_children``; the
    re-dispatch (after the store woke the run) completes.
    """

    def __init__(self, store, child_works, *, park_gate=None):
        self.store = store
        self.child_works = child_works
        self.park_gate = park_gate
        self.child_ids = []
        self.segments = 0
        self.run_id = ""

    def __call__(self, handle):
        self.run_id = handle.run_id
        self.segments += 1
        if self.segments == 1:
            for work in self.child_works:
                summary = self.store.submit(
                    question="Kind-Recherche",
                    stack_name="default",
                    work=work,
                    request_payload={"question": "kind"},
                    kind="agent_child",
                    parent_run_id=self.run_id,
                    root_run_id=self.run_id,
                )
                self.child_ids.append(summary["run_id"])
            if self.park_gate is not None:
                assert self.park_gate.wait(timeout=10.0)
            handle.wait("waiting_for_children")
            return
        handle.complete({"answer": "fertig", "metrics": {}})


def test_children_wake_resumes_parent_after_last_child(store):
    import threading

    release_second = threading.Event()

    def child_one(handle):
        handle.complete({"answer": "eins", "metrics": {}})

    def child_two(handle):
        assert release_second.wait(timeout=10.0)
        handle.complete({"answer": "zwei", "metrics": {}})

    parent = _SegmentedParent(store, [child_one, child_two])
    run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        request_payload={"question": "agent"},
        kind="agent",
    )["run_id"]
    try:
        wait_for_status(store, run_id, {"waiting_for_children"})
        wait_for_status(store, parent.child_ids[0], {"completed"})
        # One child outstanding: the parent must still wait.
        assert store.get(run_id)["status"] == "waiting_for_children"
        release_second.set()
        wait_for_status(store, run_id, {"completed"})
        assert parent.segments == 2
        events = store.subscribe(run_id)
        try:
            resumed = [
                event
                for event in events.replay
                if event["type"] == "inqtrix.run.queued"
                and event["data"].get("resumed")
            ]
            assert resumed, "the wake must emit queued{resumed:true}"
        finally:
            events.close()
    finally:
        release_second.set()


def test_cancelling_waiting_child_wakes_parent(store):
    def child_work(handle):
        handle.wait("waiting_for_input")

    parent = _SegmentedParent(store, [child_work])
    run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        request_payload={"question": "agent"},
        kind="agent",
    )["run_id"]
    wait_for_status(store, run_id, {"waiting_for_children"})
    child_id = parent.child_ids[0]
    wait_for_status(store, child_id, {"waiting_for_input"})

    store.cancel(child_id)

    wait_for_status(store, run_id, {"completed"})
    assert parent.segments == 2


def test_children_park_self_heals_when_child_already_terminal(store):
    import threading

    park_gate = threading.Event()

    def child_work(handle):
        handle.complete({"answer": "kind fertig", "metrics": {}})

    parent = _SegmentedParent(store, [child_work], park_gate=park_gate)
    run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        request_payload={"question": "agent"},
        kind="agent",
    )["run_id"]
    # Let the child terminate FIRST: its wake probe finds the parent
    # RUNNING and no-ops — the park-time self-heal must close the gap.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if parent.child_ids and store.get(parent.child_ids[0])["status"] == "completed":
            break
        time.sleep(0.05)
    else:
        pytest.fail("child never completed")
    park_gate.set()
    wait_for_status(store, run_id, {"completed"})
    assert parent.segments == 2


def test_cancel_of_children_parked_parent_cascades(store):
    import threading

    hold_child = threading.Event()

    def child_work(handle):
        hold_child.wait(timeout=10.0)
        if handle.cancel_event.is_set():
            handle.cancel("cancelled")
            return
        handle.complete({"answer": "kind", "metrics": {}})

    parent = _SegmentedParent(store, [child_work])
    run_id = store.submit(
        question="Agent-Auftrag",
        stack_name="default",
        work=parent,
        request_payload={"question": "agent"},
        kind="agent",
    )["run_id"]
    try:
        wait_for_status(store, run_id, {"waiting_for_children"})
        store.cancel(run_id)
        hold_child.set()
        wait_for_status(store, run_id, {"cancelled"})
        wait_for_status(store, parent.child_ids[0], {"cancelled"})
        # The cascaded child terminal write must not resurrect the
        # parent (the wake CAS keys on waiting_for_children).
        time.sleep(0.2)
        assert store.get(run_id)["status"] == "cancelled"
        assert parent.segments == 1
    finally:
        hold_child.set()


def test_per_user_cap_admission_counts_queued_and_running(engine):
    """1.3: durable per-user fairness bound; parked runs excluded."""
    import threading

    from inqtrix.server.runs import RunPerUserLimit

    del engine  # fixture cleanup engine belongs to pytest's event loop
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=3,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-cap",
        max_concurrent_per_user=2,
    )
    hold = threading.Event()

    def slow(handle):
        hold.wait(timeout=10.0)
        handle.complete({"answer": "ok", "metrics": {}})

    def submit(user_id: uuid.UUID):
        return store.submit(
            question="F",
            stack_name="default",
            work=slow,
            request_payload={"question": "F"},
            created_by_user_id=user_id,
        )

    try:
        submit(USER_A)
        submit(USER_A)
        with pytest.raises(RunPerUserLimit):
            submit(USER_A)
        # A distinct canonical user is unaffected; anonymous is never capped.
        submit(USER_B)
        store.submit(
            question="F",
            stack_name="default",
            work=slow,
            request_payload={"question": "F"},
        )
    finally:
        hold.set()
        store.close()


def test_close_drains_active_local_worker_before_stopping_database_loop(engine):
    """A terminal write in flight must finish before its event loop closes."""
    del engine  # fixture performs isolated database cleanup before the test
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        recover_orphans=False,
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=300,
        worker_id="pytest-close-drain",
    )
    started = threading.Event()
    release = threading.Event()
    terminal_write_finished = threading.Event()

    def work(handle):
        started.set()
        assert release.wait(timeout=10.0)
        handle.complete({"answer": "done", "metrics": {}})
        terminal_write_finished.set()

    store.submit(
        question="Close drain",
        stack_name="default",
        work=work,
        request_payload={"question": "Close drain"},
    )
    assert started.wait(timeout=5.0)
    closer = threading.Thread(target=store.close)
    closer.start()
    try:
        time.sleep(0.1)
        assert closer.is_alive()
    finally:
        release.set()
    closer.join(timeout=10.0)

    assert not closer.is_alive()
    assert terminal_write_finished.is_set()
    assert store._loop.is_closed()


def test_close_drains_unclosed_event_subscription_before_disposing_engine(engine):
    """Store shutdown owns every poller even if an SSE consumer disappears."""
    del engine  # fixture performs isolated database cleanup before the test
    queue = _RecordingDispatchQueue()
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        recover_orphans=False,
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=300,
        worker_id="pytest-close-subscription",
    )
    summary = store.submit(
        question="Open stream",
        stack_name="default",
        work=lambda handle: None,
        request_payload={"question": "Open stream"},
    )
    subscription = store.subscribe(summary["run_id"])
    poller = subscription._thread
    assert poller is not None and poller.is_alive()
    assert subscription in store._subscriptions

    store.close()

    assert not poller.is_alive()
    assert store._subscriptions == set()
    assert store._loop.is_closed()


def test_subscription_start_is_atomic_with_store_close(engine, monkeypatch):
    """Shutdown cannot join a registered poller before Thread.start()."""
    del engine  # fixture performs isolated database cleanup before the test
    queue = _RecordingDispatchQueue()
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        recover_orphans=False,
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=300,
        worker_id="pytest-subscription-start-race",
    )
    summary = store.submit(
        question="Race stream startup",
        stack_name="default",
        work=lambda handle: None,
        request_payload={"question": "Race stream startup"},
    )
    entered_start = threading.Event()
    release_start = threading.Event()
    original_start = threading.Thread.start

    def controlled_start(thread):
        if thread.name.startswith("inqtrix-run-events-"):
            entered_start.set()
            assert release_start.wait(timeout=10.0)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", controlled_start)
    subscriptions = []
    subscribe_errors = []
    close_errors = []

    def subscribe():
        try:
            subscriptions.append(store.subscribe(summary["run_id"]))
        except BaseException as exc:  # pragma: no cover - assertion reports it
            subscribe_errors.append(exc)

    def close_store():
        try:
            store.close()
        except BaseException as exc:  # pragma: no cover - assertion reports it
            close_errors.append(exc)

    subscriber = threading.Thread(target=subscribe, name="pytest-subscribe")
    subscriber.start()
    assert entered_start.wait(timeout=5.0)
    closer = threading.Thread(target=close_store, name="pytest-close")
    closer.start()
    try:
        time.sleep(0.1)
        assert closer.is_alive()
    finally:
        release_start.set()
    subscriber.join(timeout=10.0)
    closer.join(timeout=10.0)

    assert not subscriber.is_alive()
    assert not closer.is_alive()
    assert subscribe_errors == []
    assert close_errors == []
    assert len(subscriptions) == 1
    poller = subscriptions[0]._thread
    assert poller is not None and not poller.is_alive()
    assert store._subscriptions == set()
    assert store._loop.is_closed()


def test_consumer_close_does_not_block_on_inflight_subscription_read(
    engine, monkeypatch
):
    """An SSE disconnect must not join a database poller on the API loop."""
    del engine  # fixture performs isolated database cleanup before the test
    queue = _RecordingDispatchQueue()
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=queue,
        recover_orphans=False,
        max_concurrent=1,
        max_queue_size=2,
        completed_ttl_seconds=300,
        worker_id="pytest-subscription-consumer-close",
    )
    summary = store.submit(
        question="Disconnect stream",
        stack_name="default",
        work=lambda handle: None,
        request_payload={"question": "Disconnect stream"},
    )
    read_started = threading.Event()
    release_read = threading.Event()

    def blocked_read(_run_id, _tenant_id, _after_sequence):
        read_started.set()
        assert release_read.wait(timeout=10.0)
        return []

    monkeypatch.setattr(store, "_events_after", blocked_read)
    subscription = store.subscribe(summary["run_id"])
    poller = subscription._thread
    assert poller is not None
    assert read_started.wait(timeout=5.0)
    try:
        started = time.monotonic()
        subscription.close()
        elapsed = time.monotonic() - started

        assert elapsed < 0.5
        assert poller.is_alive()
        assert subscription in store._subscriptions
    finally:
        release_read.set()
    poller.join(timeout=10.0)

    assert not poller.is_alive()
    assert subscription not in store._subscriptions
    store.close()
    assert store._loop.is_closed()


def test_run_lists_read_no_result_and_match_the_single_read(store):
    """Every run list builds summaries without loading a run's result.

    A deep report's stored result is a quarter of a megabyte. The Research
    Desk list, the internal list, a session's runs and the polled children
    of an agent run each read every listed run's result and full request
    payload to render one summary row. The lists select the summary columns;
    each listed summary stays identical to the single-run read.
    """
    import re

    from sqlalchemy import event

    from inqtrix.runs.shared import QUESTION_COLUMN_LIMIT

    release = threading.Event()

    def root_work(handle) -> None:
        release.wait(timeout=10)
        handle.complete({"answer": "done"})

    long_question = "Wie ist die Haftung geregelt? " * (QUESTION_COLUMN_LIMIT // 10)
    session_id = f"ses-{uuid.uuid4().hex[:12]}"
    root = store.submit(
        question="root",
        stack_name="default",
        work=root_work,
        kind="agent",
        request_payload={"body": {"mode": "workspace_agent"}},
    )
    try:
        child = store.submit(
            question=long_question,
            stack_name="default",
            work=lambda handle: handle.complete({"answer": "x" * 50_000}),
            kind="agent_child",
            parent_run_id=root["run_id"],
            origin_key="task-1:attempt-1",
            request_payload={
                "question": long_question,
                "body": {"mode": "research", "work_profile": "review"},
            },
        )
        wait_for_status(store, child["run_id"], {"completed"})
    finally:
        release.set()
    wait_for_status(store, root["run_id"], {"completed"})
    in_session = submit_noop(store, kind="agent", session_id=session_id)
    wait_for_status(store, in_session["run_id"], {"completed"})

    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = store._engine.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        page, _cursor = store.list_page(limit=50)
        everything = store.list()
        children = store.children(root["run_id"])
        session_runs = store.list_session_runs(session_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    summary_reads = [s for s in statements if "runs.snapshot" in s]
    assert len(summary_reads) == 4
    for statement in summary_reads:
        assert "runs.result" not in statement
        # The payload is read only by path, never as the whole column, and
        # at most twice: every path read decompresses it again.
        assert not re.search(r"runs\.request_payload(?!\s*->)", statement)
        assert statement.count("runs.request_payload") <= 2

    by_id = {item["run_id"]: item for item in [*page, *everything, *children, *session_runs]}
    assert {root["run_id"], child["run_id"], in_session["run_id"]} <= set(by_id)
    for listing in (page, everything, children, session_runs):
        for item in listing:
            # Every field a list shows equals the single-run read. Only the
            # single read adds the full question text (a list row carries the
            # "clipped" flag), and children inherit their parent's access.
            single = store.get(item["run_id"])
            assert {key: single[key] for key in item} == item
            assert set(single) - set(item) <= {"question_full", "access"}
    listed_child = next(item for item in children if item["run_id"] == child["run_id"])
    assert listed_child["origin_key"] == "task-1:attempt-1"
    assert listed_child["work_profile"] == "review"
    assert listed_child["question_clipped"] is True


def test_list_page_keyset_walks_history_and_positions_queued(store):
    """2.2: durable keyset paging + batched queue-position (no N+1)."""
    from inqtrix.pagination import decode_cursor

    # 5 completed runs (fast noop) — deterministic newest-first ordering.
    completed_ids = []
    for _ in range(5):
        completed_ids.append(submit_noop(store)["run_id"])
    for rid in completed_ids:
        wait_for_status(store, rid, {"completed"})

    seen = []
    after = None
    for _ in range(10):
        summaries, cursor = store.list_page(limit=2, after=after)
        seen.extend(s["run_id"] for s in summaries)
        if cursor is None:
            break
        after = decode_cursor(cursor)
    # Every run exactly once, newest-first.
    assert set(seen) == set(completed_ids)
    assert len(seen) == len(set(seen))
    assert seen == list(reversed(completed_ids))


def test_list_page_reports_true_global_queue_position_across_pages(store):
    import threading

    release = threading.Event()

    def blocking(handle):
        release.wait(timeout=10.0)
        handle.complete({"answer": "ok", "metrics": {}})

    # max_concurrent=2 -> 2 run, the rest queue. Submit 5.
    ids = []
    for i in range(5):
        ids.append(
            store.submit(
                question=f"q{i}",
                stack_name="default",
                work=blocking,
                request_payload={"question": "x"},
            )["run_id"]
        )
    try:
        # Let the store settle (2 running, 3 queued).
        wait_for_status(store, ids[0], {"running"})
        # Page size 2 forces the queued runs onto later pages; their
        # position must stay GLOBAL, not page-local.
        positions = {}
        after = None
        from inqtrix.pagination import decode_cursor

        for _ in range(10):
            summaries, cursor = store.list_page(limit=2, after=after)
            for s in summaries:
                if s["queue_position"] is not None:
                    positions[s["run_id"]] = s["queue_position"]
            if cursor is None:
                break
            after = decode_cursor(cursor)
        # The queued runs (submitted last, newest) carry ascending global
        # positions 1..3 regardless of which page they landed on.
        assert sorted(positions.values()) == [1, 2, 3]
    finally:
        release.set()


def test_checkpoint_cleanup_is_fenced_and_keeps_terminal_run_on_failure(store):
    from functools import partial
    from sqlalchemy import update
    from inqtrix.agents.checkpointing import cleanup_run_checkpoint
    from inqtrix.exceptions import RunCleanupFailed

    owner = _scoped(OWNER_1)
    summary = submit_noop(store, mode="agent_kernel", workspace_id="owner-space",
                          created_by_user_id=OWNER_1, created_by_tenant_id="default")
    run_id = summary["run_id"]
    deadline = time.monotonic() + 10
    while store.get(run_id, visible_to=owner)["status"] != "completed":
        assert time.monotonic() < deadline
        time.sleep(.02)
    calls = []
    class Checkpoints:
        unavailable = True
        def delete_thread_strict(self, value):
            calls.append(value)
            if self.unavailable:
                raise ConnectionError("Unavailable")
    checkpoints = Checkpoints()
    store.bind_checkpoint_cleanup(partial(cleanup_run_checkpoint, checkpoints))
    with pytest.raises(RunNotFound):
        store.delete(run_id, requester_user_id=OWNER_2)
    with pytest.raises(RunNotFound):
        store.delete(run_id, requester_user_id=OWNER_1, workspace_id="other-space")
    async def status(value):
        async with store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id == run_id).values(status=value))
    store._call(status("running"))
    with pytest.raises(RunActive):
        store.delete(run_id, requester_user_id=OWNER_1)
    assert calls == []
    store._call(status("completed"))
    with pytest.raises(RunCleanupFailed):
        store.delete(run_id, requester_user_id=OWNER_1)
    assert store.get(run_id, visible_to=owner)["status"] == "completed"
    checkpoints.unavailable = False
    store.delete(run_id, requester_user_id=OWNER_1)
    assert calls == [run_id, run_id]
    with pytest.raises(RunNotFound):
        store.get(run_id, visible_to=owner)


def test_retention_retries_checkpoint_cleanup_before_removing_terminal_anchor(store):
    from functools import partial
    from sqlalchemy import update
    from inqtrix.agents.checkpointing import cleanup_run_checkpoint

    summary = submit_noop(store, mode="agent_kernel")
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"completed"})
    class Checkpoints:
        unavailable = True
        calls = 0
        def delete_thread_strict(self, value):
            assert value == run_id
            self.calls += 1
            if self.unavailable:
                raise ConnectionError("Unavailable")
    checkpoints = Checkpoints()
    store.bind_checkpoint_cleanup(partial(cleanup_run_checkpoint, checkpoints))
    async def backdate():
        async with store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id == run_id).values(finished_at=time.time()-400))
    store._call(backdate())
    assert store.get(run_id)["status"] == "completed"
    checkpoints.unavailable = False
    with pytest.raises(RunNotFound):
        store.get(run_id)
    assert checkpoints.calls == 2


def test_initial_retention_sweep_already_owns_checkpoint_cleanup(store):
    from sqlalchemy import update
    summary = submit_noop(store, mode="agent_kernel")
    run_id = summary["run_id"]
    wait_for_status(store, run_id, {"completed"})
    async def backdate():
        async with store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id == run_id).values(finished_at=time.time()-400))
    store._call(backdate())
    calls = []
    reopened = PostgresRunStore(engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE,
        queue=None, max_concurrent=2, max_queue_size=10, completed_ttl_seconds=300,
        worker_id="retention-reopen", recover_orphans=True,
        checkpoint_cleanup=lambda rid, mode: calls.append((rid, mode)))
    try:
        assert calls == [(run_id, "agent_kernel")]
        with pytest.raises(RunNotFound):
            reopened.get(run_id)
    finally:
        reopened.close()


def test_cascading_run_delete_purges_descendant_checkpoints_under_terminal_fence(store):
    from sqlalchemy import update
    root = submit_noop(store, mode="agent_kernel")
    run_id = root["run_id"]
    wait_for_status(store, run_id, {"completed"})
    child_id = "run_" + uuid.uuid4().hex
    async def child(status):
        async with store._session("default") as session:
            row = dict((await session.execute(select(runs).where(runs.c.run_id == run_id))).mappings().one())
            row.update(run_id=child_id, parent_run_id=run_id, root_run_id=run_id, kind="agent_child", mode="workspace_agent", status=status)
            await session.execute(pg_insert(runs).values(**row).on_conflict_do_update(index_elements=[runs.c.run_id], set_={"status": status}))
    store._call(child("running"))
    calls = []
    store.bind_checkpoint_cleanup(lambda rid, mode: calls.append((rid, mode)))
    with pytest.raises(RunActive):
        store.delete(run_id)
    assert calls == []
    async def terminal_child():
        async with store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id == child_id).values(status="completed"))
    store._call(terminal_child())
    store.delete(run_id)
    assert calls == [(run_id, "agent_kernel"), (child_id, "workspace_agent")]
    with pytest.raises(RunNotFound):
        store.get(child_id)
