from __future__ import annotations

import time
import uuid

import pytest

from inqtrix.auth.principal import Principal
from inqtrix.project.agent_sessions_memory import MemoryAgentSessionStore
from inqtrix.project.agent_sessions_ports import AgentSessionNotFound
from inqtrix.project.knowledge_sessions_memory import MemoryKnowledgeSessionStore
from inqtrix.project.knowledge_sessions_ports import KnowledgeSessionNotFound
from inqtrix.project.scoped_upsert import ResourceScope
from inqtrix.runs.deletion_operations import (
    DeletionOperationStore,
    DeletionStage,
    DeletionTargetKind,
    SessionDeletionContext,
)
from inqtrix.services.agent_sessions_service import AgentSessionsService
from inqtrix.services.asset_deletion_service import AssetDeletionService
from inqtrix.services.knowledge_sessions_service import KnowledgeSessionsService


OWNER = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SCOPE = ResourceScope(created_by_user_id=OWNER, workspace_id="ws-a")


@pytest.mark.asyncio
async def test_session_tombstones_fence_agent_and_knowledge_mutation() -> None:
    agent_store = MemoryAgentSessionStore()
    await agent_store.claim_session(
        id="as_1",
        title="Agent",
        created_at=1.0,
        created_by_user_id=OWNER,
        workspace_id="ws-a",
    )
    await agent_store.set_session_deletion_state(
        "as_1",
        scope=SCOPE,
        lifecycle_status="deleting",
        deletion_operation_id="del_agent",
        deletion_stage="queued",
        deletion_error=None,
    )
    with pytest.raises(AgentSessionNotFound):
        await agent_store.claim_session(
            id="as_1",
            title="Resurrected",
            created_at=2.0,
            created_by_user_id=OWNER,
            workspace_id="ws-a",
        )
    with pytest.raises(AgentSessionNotFound):
        await agent_store.upsert_session(
            id="as_1",
            title="Resurrected",
            items_json="[]",
            group_id=None,
            created_at=1.0,
            updated_at=2.0,
            created_by_user_id=OWNER,
            workspace_id="ws-a",
        )

    knowledge_store = MemoryKnowledgeSessionStore()
    await knowledge_store.upsert_session(
        id="ks_1",
        title="Knowledge",
        items_json="[]",
        group_id=None,
        created_at=1.0,
        updated_at=1.0,
        created_by_user_id=OWNER,
        workspace_id="ws-a",
    )
    await knowledge_store.set_session_deletion_state(
        "ks_1",
        scope=SCOPE,
        lifecycle_status="delete_failed",
        deletion_operation_id="del_knowledge",
        deletion_stage="delete_failed",
        deletion_error="dependency unavailable",
    )
    with pytest.raises(KnowledgeSessionNotFound):
        await knowledge_store.upsert_session(
            id="ks_1",
            title="Resurrected",
            items_json="[]",
            group_id=None,
            created_at=1.0,
            updated_at=2.0,
            created_by_user_id=OWNER,
            workspace_id="ws-a",
        )


def test_failed_session_deletion_retries_same_context_and_operation() -> None:
    store = DeletionOperationStore()
    attempts = 0
    context = SessionDeletionContext(
        target_kind=DeletionTargetKind.AGENT_SESSION,
        session_id="as_1",
        run_ids=("run_1", "run_2"),
    )

    def work(handle) -> None:
        nonlocal attempts
        attempts += 1
        record = store.get_record(
            handle.operation_id,
            tenant_id="default",
            created_by_user_id=OWNER,
            workspace_id="ws-a",
        )
        assert record.session_context == context
        if attempts == 1:
            raise RuntimeError("checkpoint store unavailable")
        handle.complete()

    created = store.submit(
        target_kind=DeletionTargetKind.AGENT_SESSION,
        target_id="as_1",
        manifest=(),
        tenant_id="default",
        created_by_user_id=OWNER,
        workspace_id="ws-a",
        session_context=context,
        total_items=2,
        work=work,
    )
    failed = _wait(store, created["operation_id"], "delete_failed")
    assert failed["retryable"] is True

    retried = store.retry(
        created["operation_id"],
        tenant_id="default",
        created_by_user_id=OWNER,
        workspace_id="ws-a",
    )
    assert retried["operation_id"] == created["operation_id"]
    completed = _wait(store, created["operation_id"], "deleted")
    assert completed["attempt"] == 2


def test_agent_session_executor_requires_zero_residuals_before_completion() -> None:
    run_store = _RunAggregate()
    checkpointer = _Checkpointer()
    agent_sessions = AgentSessionsService(
        store=MemoryAgentSessionStore(),
        run_store=run_store,
        durable=True,
    )
    service = AssetDeletionService(
        assets=object(),
        operation_store=DeletionOperationStore(),
        files=None,
        knowledge=None,
        vector_indexes=None,
    )
    service.bind_session_deletion(
        agent_sessions=agent_sessions,
        knowledge_sessions=KnowledgeSessionsService(
            store=MemoryKnowledgeSessionStore(), durable=True
        ),
        agent_checkpointer=checkpointer,
    )
    handle = _Handle()
    principal = Principal(
        user_id=OWNER,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    context = SessionDeletionContext(
        target_kind=DeletionTargetKind.AGENT_SESSION,
        session_id="as_1",
        run_ids=("run_1", "run_2"),
    )

    service.execute(
        handle,
        manifest=(),
        target_kind=DeletionTargetKind.AGENT_SESSION,
        target_id="as_1",
        principal=principal,
        visible_to=None,
        workspace_id="ws-a",
        session_context=context,
    )

    assert checkpointer.deleted == ["run_1", "run_2"]
    assert run_store.prepared == [("as_1", ("run_1", "run_2"))]
    assert run_store.deleted == [("as_1", ("run_1", "run_2"))]
    assert handle.progressed == [
        (DeletionStage.SESSION_DATA_REMOVED, 1, 2),
        (DeletionStage.RESIDUALS_VERIFIED, 2, 2),
    ]
    assert handle.completed is True

    run_store.prepare_error = RuntimeError("runs are still stopping")
    still_running = _Handle()
    with pytest.raises(RuntimeError, match="still stopping"):
        service.execute(
            still_running,
            manifest=(),
            target_kind=DeletionTargetKind.AGENT_SESSION,
            target_id="as_1",
            principal=principal,
            visible_to=None,
            workspace_id="ws-a",
            session_context=context,
        )
    assert checkpointer.deleted == ["run_1", "run_2"]

    run_store.prepare_error = None
    run_store.residuals = {"events": 1}
    failed_handle = _Handle()
    with pytest.raises(RuntimeError, match="dependent data"):
        service.execute(
            failed_handle,
            manifest=(),
            target_kind=DeletionTargetKind.AGENT_SESSION,
            target_id="as_1",
            principal=principal,
            visible_to=None,
            workspace_id="ws-a",
            session_context=context,
        )
    assert failed_handle.completed is False


def test_knowledge_session_executor_requires_zero_residuals_before_completion() -> None:
    run_store = _RunAggregate()
    knowledge_sessions = KnowledgeSessionsService(
        store=MemoryKnowledgeSessionStore(),
        run_store=run_store,
        durable=True,
    )
    service = AssetDeletionService(
        assets=object(),
        operation_store=DeletionOperationStore(),
        files=None,
        knowledge=None,
        vector_indexes=None,
    )
    service.bind_session_deletion(
        agent_sessions=AgentSessionsService(
            store=MemoryAgentSessionStore(), durable=True
        ),
        knowledge_sessions=knowledge_sessions,
    )
    handle = _Handle()
    principal = Principal(
        user_id=OWNER,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    context = SessionDeletionContext(
        target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
        session_id="ks_1",
        run_ids=("run_knowledge_1", "run_knowledge_2"),
    )

    service.execute(
        handle,
        manifest=(),
        target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
        target_id="ks_1",
        principal=principal,
        visible_to=None,
        workspace_id="ws-a",
        session_context=context,
    )

    assert run_store.knowledge_prepared == [
        ("ks_1", ("run_knowledge_1", "run_knowledge_2"))
    ]
    assert run_store.knowledge_deleted == [
        ("ks_1", ("run_knowledge_1", "run_knowledge_2"))
    ]
    assert handle.progressed == [
        (DeletionStage.SESSION_DATA_REMOVED, 1, 2),
        (DeletionStage.RESIDUALS_VERIFIED, 2, 2),
    ]
    assert handle.completed is True

    run_store.residuals = {"events": 1}
    failed_handle = _Handle()
    with pytest.raises(RuntimeError, match="dependent data"):
        service.execute(
            failed_handle,
            manifest=(),
            target_kind=DeletionTargetKind.KNOWLEDGE_SESSION,
            target_id="ks_1",
            principal=principal,
            visible_to=None,
            workspace_id="ws-a",
            session_context=context,
        )
    assert failed_handle.completed is False


def _wait(
    store: DeletionOperationStore, operation_id: str, expected: str
) -> dict[str, object]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = store.get(
            operation_id,
            tenant_id="default",
            created_by_user_id=OWNER,
            workspace_id="ws-a",
        )
        if result["status"] == expected:
            return result
        time.sleep(0.01)
    raise AssertionError(f"operation did not reach {expected}")


class _RunAggregate:
    def __init__(self) -> None:
        self.deleted: list[tuple[str, tuple[str, ...]]] = []
        self.knowledge_deleted: list[tuple[str, tuple[str, ...]]] = []
        self.residuals: dict[str, int] = {}
        self.prepared: list[tuple[str, tuple[str, ...]]] = []
        self.knowledge_prepared: list[tuple[str, tuple[str, ...]]] = []
        self.prepare_error: RuntimeError | None = None

    def session_owners(self, session_id: str):
        del session_id
        return set()

    def delete_agent_session_aggregate(
        self, session_id: str, *, run_ids: tuple[str, ...], **_: object
    ) -> None:
        self.deleted.append((session_id, run_ids))

    def prepare_agent_session_aggregate_deletion(
        self, session_id: str, *, run_ids: tuple[str, ...], **_: object
    ) -> None:
        self.prepared.append((session_id, run_ids))
        if self.prepare_error is not None:
            raise self.prepare_error

    def agent_session_residuals(self, *_: object, **__: object) -> dict[str, int]:
        return dict(self.residuals)

    def delete_knowledge_session_aggregate(
        self, session_id: str, *, run_ids: tuple[str, ...], **_: object
    ) -> None:
        self.knowledge_deleted.append((session_id, run_ids))

    def prepare_knowledge_session_aggregate_deletion(
        self, session_id: str, *, run_ids: tuple[str, ...], **_: object
    ) -> None:
        self.knowledge_prepared.append((session_id, run_ids))
        if self.prepare_error is not None:
            raise self.prepare_error

    def knowledge_session_residuals(
        self, *_: object, **__: object
    ) -> dict[str, int]:
        return dict(self.residuals)


class _Checkpointer:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_thread_strict(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class _Handle:
    operation_id = "del_1"
    manages_asset_lifecycle = True

    def __init__(self) -> None:
        self.progressed: list[tuple[DeletionStage, int, int]] = []
        self.completed = False

    def assert_current(self) -> None:
        return None

    def progress(
        self,
        stage: DeletionStage,
        *,
        completed_items: int,
        total_items: int,
    ) -> None:
        self.progressed.append((stage, completed_items, total_items))

    def complete(self) -> None:
        self.completed = True
