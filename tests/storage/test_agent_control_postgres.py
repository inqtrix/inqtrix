"""Postgres integration tests for the agent control store (gated suite).

Lockstep with the memory tier in ``tests/test_agent_control.py``: same
state machines, same error types. The one-transaction property (the
approval decision commits or rolls back TOGETHER with the run's
``waiting -> queued`` flip) can only be proven here.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from importlib import import_module
import os
import threading
import time
import uuid
from typing import Any, Callable

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.agents.control_ports import (
    ApprovalRecord,
    ArtifactBatchRevision,
    ArtifactLocked,
    ArtifactNotFound,
    ArtifactRevisionConflict,
    ClarificationRecord,
    ContextArchiveImmutable,
    PlanNotFound,
    PlanRecord,
    PlanTaskRecord,
    settle_terminal_plan_tasks,
)
from inqtrix.agents.context_archive import (
    ArchiveCursorError,
    ArchiveIntegrityError,
    ArchiveVersionConflict,
    catalogue_page,
    prepare_archive,
)
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.runs.postgres_store import PostgresRunStore
from inqtrix.server.runs import RunActive, RunNotFound
from inqtrix.services.agent_control_service import (
    AgentControlService,
    AgentControlValidationError,
)
from inqtrix.storage.agent_control_postgres import PostgresAgentControlStore
from inqtrix.storage.db import build_engine, build_session_factory
from inqtrix.storage.identity_postgres import PostgresIdentityBackend
from inqtrix.storage.migrate import run_migrations
from inqtrix.storage.agent_sessions_orm import agent_sessions

from tests.storage._canonical_users import ensure_canonical_users

TEST_DATABASE_URL = os.environ.get("INQTRIX_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.postgres

APP_ROLE = "inqtrix_app"
OWNER_USER_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
EDITOR_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")

_MIGRATION_0043 = import_module(
    "inqtrix.storage.migrations.versions."
    "0043_agent_task_execution_contract"
)


@pytest.fixture(scope="session", autouse=True)
def schema_migrated():
    if TEST_DATABASE_URL:
        run_migrations(TEST_DATABASE_URL)
    yield


@pytest_asyncio.fixture()
async def wiped():
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
            # Control rows cascade with their runs.
            await session.execute(text("DELETE FROM run_events"))
            await session.execute(
                text("DELETE FROM resource_shares WHERE resource_type = 'run'")
            )
            await session.execute(text("DELETE FROM runs"))
            await session.execute(text("DELETE FROM agent_sessions"))
            await session.execute(text("DELETE FROM agent_session_groups"))
            await ensure_canonical_users(
                session, (OWNER_USER_ID, EDITOR_USER_ID)
            )
    await engine.dispose()
    yield


@pytest.fixture()
def run_store(wiped):
    store = PostgresRunStore(
        engine=build_engine(TEST_DATABASE_URL),
        app_role=APP_ROLE,
        queue=None,
        max_concurrent=2,
        max_queue_size=10,
        completed_ttl_seconds=300,
        worker_id="pytest-agent-control",
    )
    yield store
    store.close()


@pytest_asyncio.fixture()
async def control_store(wiped):
    store = PostgresAgentControlStore(
        engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE
    )
    yield store
    await store.aclose()


@pytest.fixture()
def service(control_store, run_store):
    return AgentControlService(
        store=control_store,
        run_store=run_store,
        audit=None,
        editor_persistence=None,
        durable=True,
    )


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("condition was not reached before timeout")


def _parked_agent_run(
    run_store: PostgresRunStore,
    *,
    resumed_release: threading.Event | None = None,
    session_id: str = "sess-pg",
    owner_user_id: uuid.UUID | None = None,
) -> str:
    calls = {"count": 0}

    def segmented(handle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        if resumed_release is not None:
            assert resumed_release.wait(timeout=10.0)
        handle.complete({"answer": "fortgesetzt"})

    async def _seed_session() -> None:
        async with run_store._session("default") as session:
            await session.execute(
                pg_insert(agent_sessions)
                .values(
                    id=session_id,
                    tenant_id="default",
                    created_by_user_id=owner_user_id,
                    workspace_id=None,
                    title="Agent control fixture",
                    group_id=None,
                    items_json="[]",
                    lifecycle_status="active",
                    created_at=time.time(),
                    updated_at=time.time(),
                )
                .on_conflict_do_nothing(index_elements=(agent_sessions.c.id,))
            )

    run_store._call(_seed_session())

    summary = run_store.submit(
        question="Agentenauftrag",
        stack_name="default",
        work=segmented,
        request_payload={"question": "x", "body": {"mode": "research"}},
        kind="agent",
        session_id=session_id,
        created_by_user_id=owner_user_id,
        created_by_tenant_id="default" if owner_user_id is not None else None,
    )
    run_id = summary["run_id"]
    visible_to = _scoped(owner_user_id) if owner_user_id is not None else None
    _wait_until(
        lambda: run_store.get(run_id, visible_to=visible_to)["status"]
        == "waiting_for_approval"
    )
    return run_id


def _completed_agent_child(
    run_store: PostgresRunStore,
    *,
    parent_run_id: str,
    parent_task_id: str,
    attempt: int,
    owner_user_id: uuid.UUID,
) -> str:
    def work(handle: Any) -> None:
        handle.complete({"answer": f"child attempt {attempt}"})

    summary = run_store.submit(
        question=f"Child research attempt {attempt}",
        stack_name="default",
        work=work,
        request_payload={
            "question": "Child research",
            "body": {
                "mode": "research",
                "parent_task_id": parent_task_id,
                "parent_task_attempt": attempt,
            },
        },
        kind="agent_child",
        parent_run_id=parent_run_id,
        root_run_id=parent_run_id,
        created_by_user_id=owner_user_id,
        created_by_tenant_id="default",
    )
    child_run_id = summary["run_id"]
    visible_to = _scoped(owner_user_id)
    _wait_until(
        lambda: run_store.get(child_run_id, visible_to=visible_to)["status"]
        == "completed"
    )
    return child_run_id


def _scoped(user_id: uuid.UUID) -> UserContext:
    return UserContext(
        principal=Principal(
            user_id=user_id,
            kind="oidc_session",
            tenant_id="default",
            role="member",
        )
    )


def _grant_edit_share(
    run_store: PostgresRunStore, run_id: str
) -> str:
    identity = PostgresIdentityBackend(
        session_factory=run_store._session_factory,
        app_role=APP_ROLE,
    )
    (pending,) = run_store._call(
        identity.create_shares(
            tenant_id="default",
            resource_type="run",
            resource_id=run_id,
            owner_user_id=OWNER_USER_ID,
            granted_by_user_id=OWNER_USER_ID,
            invitees=((EDITOR_USER_ID, SharePermission.EDIT),),
        )
    )
    accepted = run_store._call(
        identity.accept_share_by_id(
            tenant_id="default",
            share_id=pending.id,
            recipient_user_id=EDITOR_USER_ID,
            owner_user_id=OWNER_USER_ID,
        )
    )
    assert accepted is not None
    return pending.id


def _approval(run_id: str, *, kind: str = "plan") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=f"apr_{uuid.uuid4().hex[:8]}", run_id=run_id, kind=kind
    )


@pytest.mark.asyncio
async def test_decision_and_resume_commit_together(service, run_store):
    resumed_release = threading.Event()
    run_id = _parked_agent_run(
        run_store, resumed_release=resumed_release
    )
    approval = await service.store.create_approval(_approval(run_id))

    try:
        decided, summary, replayed = await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=None,
        )
    finally:
        resumed_release.set()

    assert not replayed
    assert decided.status == "approved"
    assert summary["status"] in {"queued", "running", "completed"}
    _wait_until(lambda: run_store.get(run_id)["status"] == "completed")

    events = run_store.subscribe(run_id)
    try:
        decision_event = next(
            event
            for event in events.replay
            if event["type"] == "inqtrix.agent.approval.decided"
        )
        resumed = [
            event
            for event in events.replay
            if event["type"] == "inqtrix.run.queued"
            and event["data"].get("resumed")
        ]
        assert len(resumed) == 1
        assert resumed[0]["sequence"] < decision_event["sequence"]
    finally:
        events.close()


@pytest.mark.asyncio
async def test_plan_approval_rejects_subject_from_another_run(
    service, run_store
):
    run_a = _parked_agent_run(run_store, session_id="sess-pg-a")
    run_b = _parked_agent_run(run_store, session_id="sess-pg-b")
    await service.store.save_plan(
        run_id=run_a,
        plan=PlanRecord(
            plan_id="plan-pg-a", run_id=run_a, version=1,
            status="proposed", created_by="agent",
        ),
        tasks=[],
    )
    await service.store.save_plan(
        run_id=run_b,
        plan=PlanRecord(
            plan_id="plan-pg-b", run_id=run_b, version=1,
            status="proposed", created_by="agent",
        ),
        tasks=[],
    )

    with pytest.raises(PlanNotFound):
        await service.store.create_approval(
            ApprovalRecord(
                approval_id="apr-cross-run-pg",
                run_id=run_a,
                kind="plan",
                subject_type="plan",
                subject_id="plan-pg-b",
            )
        )

    assert await service.store.list_approvals(run_a) == []


@pytest.mark.asyncio
async def test_plan_decision_updates_plan_status_in_resume_transaction(
    service, run_store
):
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-pg-v1",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[],
    )
    approval = await service.store.create_approval(
        ApprovalRecord(
            approval_id="apr-plan-pg",
            run_id=run_id,
            kind="plan",
            subject_type="plan",
            payload={"plan_version": 1},
        )
    )
    assert approval.subject_id == "plan-pg-v1"

    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
        plan_body=None,
        note="",
        principal=None,
    )

    plan, _tasks = await service.store.get_plan(run_id)
    assert plan.status == "approved"


@pytest.mark.asyncio
async def test_edited_plan_discards_planner_model_tier_before_persistence(service, run_store):
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-routing", run_id=run_id, version=1,
            status="proposed", created_by="agent",
        ),
        tasks=[PlanTaskRecord(
            task_id="r", plan_id="plan-routing", run_id=run_id, ordinal=0,
            title="Find evidence", tool_kind="web_research",
            queries=["Which evidence supports the claim?"],
            params={"profile": "compact", "model_tier": "high"},
        )],
    )
    approval = await service.store.create_approval(ApprovalRecord(
        approval_id="approval-routing", run_id=run_id, kind="plan",
        subject_type="plan", payload={"plan_version": 1},
    ))
    await service.decide_approval(
        run_id=run_id, approval_id=approval.approval_id, decision="edit", note="",
        principal=None,
        plan_body={
            "summary_markdown": "Edited research plan",
            "tasks": [
                {"id": "r", "title": "Find evidence", "tool_kind": "web_research",
                 "queries": ["Which evidence supports the claim?"],
                 "params": {"profile": "compact", "model_tier": "high", "recency": "year"}},
                {"id": "s", "title": "Synthesize", "tool_kind": "synthesis", "depends_on": ["r"]},
            ],
        },
    )

    plan, tasks = await service.store.get_plan(run_id)
    assert plan.version == 2
    assert plan.status == "approved"
    assert tasks[0].params == {"profile": "compact", "recency": "year"}


@pytest.mark.asyncio
async def test_migration_backfills_legacy_decided_plan_subject(
    service, run_store
):
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-legacy-subject",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[],
    )
    approval = await service.store.create_approval(
        ApprovalRecord(
            approval_id="apr-legacy-subject",
            run_id=run_id,
            kind="plan",
            subject_type="plan",
            payload={"plan_version": 1},
        )
    )
    engine = build_engine(TEST_DATABASE_URL)
    factory = build_session_factory(engine)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE run_approvals SET subject_id = '', "
                        "status = 'approved', decision = 'approve' "
                        "WHERE approval_id = :approval_id"
                    ),
                    {"approval_id": approval.approval_id},
                )
                await session.execute(
                    text(_MIGRATION_0043._APPROVAL_SUBJECT_BACKFILL_SQL)
                )
                subject_id = (
                    await session.execute(
                        text(
                            "SELECT subject_id FROM run_approvals "
                            "WHERE approval_id = :approval_id"
                        ),
                        {"approval_id": approval.approval_id},
                    )
                ).scalar_one()
    finally:
        await engine.dispose()

    assert subject_id == "plan-legacy-subject"


@pytest.mark.asyncio
async def test_task_transition_retry_and_terminal_contract_postgres(
    service, run_store
):
    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    child_1 = _completed_agent_child(
        run_store,
        parent_run_id=run_id,
        parent_task_id="task-pg",
        attempt=1,
        owner_user_id=OWNER_USER_ID,
    )
    child_2 = _completed_agent_child(
        run_store,
        parent_run_id=run_id,
        parent_task_id="task-pg",
        attempt=2,
        owner_user_id=OWNER_USER_ID,
    )
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-task-pg",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="task-pg",
                plan_id="plan-task-pg",
                run_id=run_id,
                ordinal=0,
                title="Research",
                tool_kind="web_research",
            )
        ],
    )
    await service.store.transition_plan_task(
        run_id=run_id,
        plan_id="plan-task-pg",
        task_id="task-pg",
        status="running",
        child_run_id=child_1,
    )
    retry = await service.store.transition_plan_task(
        run_id=run_id,
        plan_id="plan-task-pg",
        task_id="task-pg",
        status="running",
        child_run_id=child_2,
    )
    assert retry.child_run_id == child_2
    terminal = await service.store.transition_plan_task(
        run_id=run_id,
        plan_id="plan-task-pg",
        task_id="task-pg",
        status="insufficient_evidence",
        child_run_id=child_2,
        result_summary="One source remained after verification.",
        result_payload={
            "claims": [{"text": "One source remained."}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1},
        },
    )
    assert terminal.status == "insufficient_evidence"
    assert terminal.result_payload["usage"] == {
        "prompt_tokens": 5,
        "completion_tokens": 1,
    }
    with pytest.raises(ValueError):
        await service.store.transition_plan_task(
            run_id=run_id,
            plan_id="plan-task-pg",
            task_id="task-pg",
            status="completed",
        )


@pytest.mark.asyncio
async def test_task_cancellation_state_machine_postgres(service, run_store):
    run_id = _parked_agent_run(run_store)
    plan = PlanRecord(
        plan_id="plan-task-cancel-pg",
        run_id=run_id,
        version=1,
        status="approved",
        created_by="agent",
    )
    await service.store.save_plan(
        run_id=run_id,
        plan=plan,
        tasks=[
            PlanTaskRecord(
                task_id="pending-pg",
                plan_id=plan.plan_id,
                run_id=run_id,
                ordinal=0,
                title="Pending",
                tool_kind="web_instant",
            ),
            PlanTaskRecord(
                task_id="running-pg",
                plan_id=plan.plan_id,
                run_id=run_id,
                ordinal=1,
                title="Running",
                tool_kind="web_instant",
            ),
        ],
    )
    await service.store.transition_plan_task(
        run_id=run_id,
        plan_id=plan.plan_id,
        task_id="running-pg",
        status="running",
    )

    pending = await service.request_task_cancel(
        run_id,
        "pending-pg",
        workspace_id=None,
        principal=None,
    )
    running = await service.request_task_cancel(
        run_id,
        "running-pg",
        workspace_id=None,
        principal=None,
    )

    assert pending.status == "cancelled"
    assert pending.result_payload["failure_code"] == "task_cancelled"
    assert running.status == "cancel_requested"
    await settle_terminal_plan_tasks(service.store, run_id, status="cancelled")
    _settled_plan, settled_tasks = await service.store.get_plan(run_id)
    settled = next(task for task in settled_tasks if task.task_id == "running-pg")
    assert settled.status == "cancelled"
    assert settled.result_payload == {
        "failure_code": "task_cancelled",
        "failure_reason": "user_requested_task_cancel",
    }


@pytest.mark.asyncio
async def test_shared_editor_task_cancel_reaches_child_postgres(
    service, run_store
) -> None:
    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    _grant_edit_share(run_store, run_id)
    child_started = threading.Event()
    child_release = threading.Event()

    def child_work(handle: Any) -> None:
        child_started.set()
        assert child_release.wait(timeout=10.0)
        if handle.cancel_event.is_set():
            handle.cancel("task_cancelled")
        else:
            handle.complete({"answer": "late"})

    child = run_store.submit(
        question="Child research",
        stack_name="default",
        work=child_work,
        request_payload={
            "question": "Child research",
            "body": {
                "mode": "research",
                "parent_task_id": "research",
                "parent_task_attempt": 1,
            },
        },
        kind="agent_child",
        parent_run_id=run_id,
        root_run_id=run_id,
        created_by_user_id=OWNER_USER_ID,
        created_by_tenant_id="default",
    )
    assert child_started.wait(timeout=5.0)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-shared-child-pg",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="research",
                plan_id="plan-shared-child-pg",
                run_id=run_id,
                ordinal=0,
                title="Research",
                tool_kind="web_research",
                status="running",
                child_run_id=child["run_id"],
            )
        ],
    )
    editor = _scoped(EDITOR_USER_ID)

    try:
        task = await service.request_task_cancel(
            run_id,
            "research",
            workspace_id=None,
            principal=editor.principal,
            visible_to=editor,
        )
        assert task.status == "cancel_requested"
        child_release.set()
        _wait_until(
            lambda: run_store.get(
                child["run_id"], visible_to=_scoped(OWNER_USER_ID)
            )["status"]
            == "cancelled"
        )
    finally:
        child_release.set()


@pytest.mark.asyncio
async def test_deleting_child_run_clears_task_reference_without_deleting_task(
    control_store, run_store
) -> None:
    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    child_run_id = _completed_agent_child(
        run_store,
        parent_run_id=run_id,
        parent_task_id="research",
        attempt=1,
        owner_user_id=OWNER_USER_ID,
    )
    plan = PlanRecord(
        plan_id="plan-child-reference-pg",
        run_id=run_id,
        version=1,
        status="approved",
        created_by="agent",
    )
    task = PlanTaskRecord(
        task_id="research",
        plan_id=plan.plan_id,
        run_id=run_id,
        ordinal=0,
        title="Research",
        tool_kind="web_research",
        status="running",
        child_run_id=child_run_id,
    )
    saved_plan = await control_store.save_plan(
        run_id=run_id,
        plan=plan,
        tasks=[task],
    )

    run_store.delete(child_run_id, requester_user_id=OWNER_USER_ID)

    with pytest.raises(RunNotFound):
        run_store.get(child_run_id, visible_to=_scoped(OWNER_USER_ID))
    stored_plan, stored_tasks = await control_store.get_plan(run_id)
    assert stored_plan == saved_plan
    assert stored_tasks == [replace(task, child_run_id=None)]


@pytest.mark.asyncio
async def test_decision_rolls_back_when_run_is_not_waiting(service, run_store):
    """The decision CAS lives inside the resume transaction — a run
    that is no longer waiting leaves the approval untouched."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    run_store.cancel(run_id)
    _wait_until(lambda: run_store.get(run_id)["status"] == "cancelled")

    with pytest.raises(RunActive):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=None,
        )

    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    events = run_store.subscribe(run_id)
    try:
        assert "inqtrix.agent.approval.decided" not in {
            event["type"] for event in events.replay
        }
    finally:
        events.close()


@pytest.mark.asyncio
async def test_edit_decision_writes_plan_in_the_same_transaction(
    service, run_store
):
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan_pg_v1",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="t1",
                plan_id="plan_pg_v1",
                run_id=run_id,
                ordinal=0,
                title="Alt",
                tool_kind="rag_query",
            )
        ],
    )
    approval = await service.store.create_approval(_approval(run_id))

    decided, _summary, _replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body={
            "summary_markdown": "Neu",
            "tasks": [
                {
                    "id": "t1",
                    "title": "Suche",
                    "tool_kind": "rag_query",
                    "queries": ["Welche belastbare Evidenz liegt vor?"],
                },
                {
                    "id": "s",
                    "title": "Synthese",
                    "tool_kind": "synthesis",
                    "depends_on": ["t1"],
                },
            ],
        },
        note="",
        principal=None,
    )

    assert decided.status == "edited"
    plan, tasks, versions = await service.get_plan(run_id)
    assert plan.version == 2
    assert plan.created_by == "user"
    assert [task.task_id for task in tasks] == ["t1", "s"]
    assert [(v.version, v.status) for v in versions] == [
        (2, "approved"),
        (1, "superseded"),
    ]
    _wait_until(lambda: run_store.get(run_id)["status"] == "completed")


@pytest.mark.asyncio
async def test_invalid_edit_leaves_everything_pending(service, run_store):
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))

    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body={"tasks": [{"id": "t1", "title": "x", "tool_kind": "rag_query"}]},
            note="",
            principal=None,
        )

    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    assert run_store.get(run_id)["status"] == "waiting_for_approval"
    with pytest.raises(PlanNotFound):
        await service.store.get_plan(run_id)


@pytest.mark.asyncio
async def test_clarification_answer_resumes(service, run_store):
    run_id = _parked_agent_run(run_store)
    clarification = await service.store.create_clarification(
        ClarificationRecord(
            clarification_id=f"clr_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            question="Zeitraum?",
            options=({"id": "q1", "label": "Q1"},),
        )
    )

    answered, _summary, replayed = await service.answer_clarification(
        run_id=run_id,
        clarification_id=clarification.clarification_id,
        answer="Das erste Quartal",
        option_id=None,
        principal=None,
    )

    assert not replayed
    assert answered.status == "answered"
    assert answered.answer == "Das erste Quartal"
    _wait_until(lambda: run_store.get(run_id)["status"] == "completed")

    # Replay of the same answer is idempotent, a different one conflicts.
    _answered, _summary2, replayed = await service.answer_clarification(
        run_id=run_id,
        clarification_id=clarification.clarification_id,
        answer="Das erste Quartal",
        option_id=None,
        principal=None,
    )
    assert replayed


@pytest.mark.asyncio
async def test_artifact_matrix_and_revisions(service, run_store, control_store):
    run_id = _parked_agent_run(run_store)
    await control_store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-pg",
        title="Memo",
        status="ready",
        content_markdown="# V1",
        payload={},
        refs=[{"label": "K1"}],
        updated_by="agent",
        artifact_id="art_pg",
    )
    # Session upsert without id finds the memo and bumps the revision.
    bumped = await control_store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-pg",
        title="Memo",
        status="ready",
        content_markdown="# V2",
        payload={},
        refs=[],
        updated_by="agent",
    )
    assert bumped.artifact_id == "art_pg"
    assert bumped.revision == 2

    with pytest.raises(ArtifactRevisionConflict) as conflict:
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art_pg",
            content_markdown="# stale",
            expected_revision=1,
            principal=None,
        )
    assert conflict.value.current_revision == 2

    edited = await service.user_update_artifact(
        run_id=run_id,
        artifact_id="art_pg",
        content_markdown="# V3",
        expected_revision=2,
        principal=None,
    )
    assert edited.revision == 3
    assert edited.updated_by == "user"

    await control_store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-pg",
        title="Memo",
        status="writing",
        content_markdown="# V4",
        payload={},
        refs=[],
        updated_by="agent",
    )
    with pytest.raises(ArtifactLocked):
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art_pg",
            content_markdown="# x",
            expected_revision=4,
            principal=None,
        )

    artifact, revisions = await control_store.get_artifact(run_id, "art_pg")
    assert artifact.revision == 4
    assert [row.revision for row in revisions] == [4, 3, 2, 1]
    old, _ = await control_store.get_artifact(run_id, "art_pg", revision=1)
    assert old.content_markdown == "# V1"

    page, cursor = await control_store.list_artifacts(run_id, limit=10)
    assert [row.artifact_id for row in page] == ["art_pg"]
    assert cursor is None
    assert page[0].content_markdown == ""


@pytest.mark.asyncio
async def test_line_delta_and_rename_lockstep_postgres(
    service, run_store, control_store
):
    """The line delta on the REAL SQL paths: the upsert returns the write's
    line delta (reads stay None), the batch CAS counts per row, and the
    rename is metadata-only (no revision, no revision row)."""
    run_id = _parked_agent_run(run_store)
    created = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-pg-p9",
        title="Bericht",
        status="ready",
        content_markdown="eins\nzwei",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_pg_p9",
    )
    assert (created.lines_added, created.lines_removed) == (2, 0)
    updated = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-pg-p9",
        title="Bericht",
        status="ready",
        content_markdown="eins\ndrei\nvier",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_pg_p9",
        expected_revision=1,
    )
    assert (updated.lines_added, updated.lines_removed) == (2, 1)
    read, _ = await control_store.get_artifact(run_id, "art_pg_p9")
    assert read.lines_added is None and read.lines_removed is None

    rows = await control_store.revise_session_artifacts_atomically(
        run_id=run_id,
        session_id="sess-pg-p9",
        revisions=[
            ArtifactBatchRevision(
                artifact_id="art_pg_p9",
                expected_revision=2,
                content_markdown="eins\ndrei\nvier\nfuenf",
            )
        ],
    )
    assert (rows[0].lines_added, rows[0].lines_removed) == (1, 0)

    renamed = await service.rename_artifact(
        run_id=run_id,
        artifact_id="art_pg_p9",
        title="Bericht V2",
        principal=None,
    )
    assert renamed.title == "Bericht V2"
    assert renamed.revision == 3
    row, revisions = await control_store.get_artifact(run_id, "art_pg_p9")
    assert row.title == "Bericht V2"
    assert row.revision == 3
    assert len(revisions) == 3
    with pytest.raises(ArtifactNotFound):
        await service.rename_artifact(
            run_id=run_id,
            artifact_id="art_pg_unbekannt",
            title="X",
            principal=None,
        )


@pytest.mark.asyncio
async def test_late_user_artifact_changes_preserve_durable_terminal_log(
    service, run_store, control_store, monkeypatch, caplog,
):
    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    caller = _scoped(OWNER_USER_ID)
    await control_store.upsert_artifact(
        run_id=run_id, kind="deliverable", session_id="sess-pg", title="Original",
        status="ready", content_markdown="Original body", payload={}, refs=[],
        updated_by="agent", artifact_id="late-user-artifact",
    )
    run_store.complete(run_id, {"answer": "Finished"})
    before = run_store.subscribe(run_id, visible_to=caller, stream=False).replay
    emitted = []
    original_emit = run_store.emit
    def emit(*args, **kwargs):
        emitted.append(args)
        return original_emit(*args, **kwargs)
    monkeypatch.setattr(run_store, "emit", emit)
    caplog.clear()
    edited = await service.user_update_artifact(
        run_id=run_id, artifact_id="late-user-artifact", content_markdown="Edited body",
        expected_revision=1, principal=caller.principal, visible_to=caller,
    )
    renamed = await service.rename_artifact(
        run_id=run_id, artifact_id="late-user-artifact", title="Renamed",
        principal=caller.principal, visible_to=caller,
    )
    assert edited.revision == renamed.revision == 2
    stored, revisions = await control_store.get_artifact(run_id, "late-user-artifact")
    assert (stored.title, stored.content_markdown) == ("Renamed", "Edited body")
    assert len(revisions) == 2
    assert emitted == []
    assert run_store.subscribe(run_id, visible_to=caller, stream=False).replay == before
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]


@pytest.mark.asyncio
async def test_revoked_editor_cannot_update_artifact_postgres(
    service, run_store, control_store
) -> None:
    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    share_id = _grant_edit_share(run_store, run_id)
    await control_store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-revoked-editor-pg",
        title="Memo",
        status="ready",
        content_markdown="# V1",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art-revoked-editor-pg",
    )
    editor = _scoped(EDITOR_USER_ID)
    edited = await service.user_update_artifact(
        run_id=run_id,
        artifact_id="art-revoked-editor-pg",
        content_markdown="# V2",
        expected_revision=1,
        principal=editor.principal,
        visible_to=editor,
    )
    assert edited.revision == 2
    identity = PostgresIdentityBackend(
        session_factory=run_store._session_factory,
        app_role=APP_ROLE,
    )
    revoked = run_store._call(
        identity.revoke_share_by_id(
            tenant_id="default",
            share_id=share_id,
            revoked_by_user_id=OWNER_USER_ID,
            owner_user_id=OWNER_USER_ID,
        )
    )
    assert revoked is not None

    with pytest.raises(RunNotFound):
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art-revoked-editor-pg",
            content_markdown="# forbidden",
            expected_revision=2,
            principal=editor.principal,
            visible_to=editor,
        )
    stored, _revisions = await control_store.get_artifact(
        run_id, "art-revoked-editor-pg"
    )
    assert stored.revision == 2
    assert stored.content_markdown == "# V2"


@pytest.mark.asyncio
async def test_revoked_effective_actor_cannot_persist_runtime_artifact_postgres(
    run_store, control_store
) -> None:
    """Agent writes lock the same live run/share boundary as user edits."""
    release = threading.Event()
    run_id = _parked_agent_run(
        run_store,
        resumed_release=release,
        owner_user_id=OWNER_USER_ID,
        session_id="sess-runtime-revoke-pg",
    )
    share_id = _grant_edit_share(run_store, run_id)
    editor = _scoped(EDITOR_USER_ID)
    try:
        run_store.resume_run(
            run_id,
            actor_user_id=EDITOR_USER_ID,
            execution_scopes=frozenset({"agent:write"}),
        )
        _wait_until(
            lambda: run_store.get(run_id, visible_to=editor)["status"]
            == "running"
        )
        identity = PostgresIdentityBackend(
            session_factory=run_store._session_factory,
            app_role=APP_ROLE,
        )
        revoked = run_store._call(
            identity.revoke_share_by_id(
                tenant_id="default",
                share_id=share_id,
                revoked_by_user_id=OWNER_USER_ID,
                owner_user_id=OWNER_USER_ID,
            )
        )
        assert revoked is not None

        with pytest.raises(AuthorizationRevoked):
            await control_store.upsert_artifact(
                run_id=run_id,
                kind="memo",
                session_id="sess-runtime-revoke-pg",
                title="Forbidden",
                status="writing",
                content_markdown="must not land",
                payload={},
                refs=[],
                updated_by="agent",
                artifact_id="art-runtime-revoke-pg",
            )
        with pytest.raises(ArtifactNotFound):
            await control_store.get_artifact(
                run_id, "art-runtime-revoke-pg"
            )
    finally:
        release.set()
        _wait_until(
            lambda: run_store.get(
                run_id, visible_to=_scoped(OWNER_USER_ID)
            )["status"]
            == "failed"
        )


@pytest.mark.asyncio
async def test_concurrent_first_memo_insert_surfaces_conflict_not_integrityerror(
    run_store, control_store
):
    """Two runs of the same session doing a first-ever memo write must not
    abort with a raw IntegrityError.

    ``expected_revision=0`` promises "I expect to CREATE; a concurrently
    inserted row is a conflict." The insert branch ignored that and raised a
    raw IntegrityError on the partial-unique/PK collision, which _flush_memo's
    reconcile loop (catches only ArtifactRevisionConflict) could not absorb.
    Now the loser gets a reconcilable ArtifactRevisionConflict whether it lost
    the insert race (ON CONFLICT DO NOTHING) or found the row already present
    (the CAS guard) — never a 500. A few rounds raise the odds of the true
    interleave; the invariant (exactly one success, the other a conflict, no
    raw error) must hold every round.
    """
    for round_index in range(6):
        run_id = _parked_agent_run(
            run_store,
            session_id=f"sess-race-run-{round_index}",
        )
        artifact_id = f"art_race_{round_index}_memo"
        session_id = f"sess-race-{round_index}"

        async def first_write(marker: str):
            return await control_store.upsert_artifact(
                run_id=run_id,
                kind="memo",
                session_id=session_id,
                title="Memo",
                status="writing",
                content_markdown=marker,
                payload={},
                refs=[],
                updated_by="agent",
                artifact_id=artifact_id,
                expected_revision=0,
            )

        results = await asyncio.gather(
            first_write("# A"), first_write("# B"), return_exceptions=True
        )
        oks = [r for r in results if not isinstance(r, Exception)]
        raw = [
            r
            for r in results
            if isinstance(r, Exception)
            and not isinstance(r, ArtifactRevisionConflict)
        ]
        assert raw == [], f"round {round_index}: raw error escaped: {raw}"
        assert len(oks) == 1, f"round {round_index}: {results}"

        artifact, _ = await control_store.get_artifact(run_id, artifact_id)
        assert artifact.revision == 1


@pytest.mark.asyncio
async def test_session_memo_lineage_and_agent_cas_on_postgres(
    service, run_store, control_store
):
    """Postgres lockstep for the cross-run memo read + agent-side CAS."""
    run1 = _parked_agent_run(run_store, session_id="sess-pg-lin")
    await control_store.upsert_artifact(
        run_id=run1,
        kind="memo",
        session_id="sess-pg-lin",
        title="Memo",
        status="ready",
        content_markdown="# Turn 1",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_pg_lin",
    )
    edited = await service.user_update_artifact(
        run_id=run1,
        artifact_id="art_pg_lin",
        content_markdown="# Turn 1 + Nutzer",
        expected_revision=1,
        principal=None,
    )
    assert edited.revision == 2

    prior = await control_store.get_session_artifact("sess-pg-lin", "memo")
    assert prior is not None
    assert prior.revision == 2
    assert prior.content_markdown == "# Turn 1 + Nutzer"

    run_store.cancel(run1)
    _wait_until(lambda: run_store.get(run1)["status"] == "cancelled")
    run2 = _parked_agent_run(run_store, session_id="sess-pg-lin")
    with pytest.raises(ArtifactRevisionConflict) as conflict:
        await control_store.upsert_artifact(
            run_id=run2,
            kind="memo",
            session_id="sess-pg-lin",
            title="Memo",
            status="writing",
            content_markdown="# stale base",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_pg_lin",
            expected_revision=1,
        )
    assert conflict.value.current_revision == 2
    advanced = await control_store.upsert_artifact(
        run_id=run2,
        kind="memo",
        session_id="sess-pg-lin",
        title="Memo",
        status="writing",
        content_markdown="# Turn 2",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_pg_lin",
        expected_revision=2,
    )
    assert advanced.revision == 3
    # expected_revision=0 = "expect to create": an existing row conflicts.
    with pytest.raises(ArtifactRevisionConflict) as insert_conflict:
        await control_store.upsert_artifact(
            run_id=run2,
            kind="memo",
            session_id="sess-pg-lin",
            title="Memo",
            status="ready",
            content_markdown="# would clobber",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_pg_lin",
            expected_revision=0,
        )
    assert insert_conflict.value.current_revision == 3


@pytest.mark.asyncio
async def test_kernel_kinds_pass_the_db_checks(service, run_store, control_store):
    """Migration 0040 gate: tool approvals + deliverable artifacts persist.

    The offline stores never see the CHECK constraints — only a live
    Postgres proves migration 0040 widened ``ck_run_approvals_kind`` and
    ``ck_run_artifacts_kind`` to match the constants.
    """
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(
        ApprovalRecord(
            approval_id="apr_tool_pg",
            run_id=run_id,
            kind="tool",
            payload={
                "actions": [
                    {"tool": "web_instant", "args": {"query": "EU AI Act"}}
                ]
            },
        )
    )
    stored = await control_store.get_approval(run_id, approval.approval_id)
    assert stored.kind == "tool"
    assert stored.payload["actions"][0]["args"] == {"query": "EU AI Act"}

    artifact = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-pg",
        title="E-Mail-Entwurf",
        status="ready",
        content_markdown="# Entwurf",
        payload={"deliverable_kind": "email"},
        refs=[],
        updated_by="agent",
        artifact_id="art_deliverable_pg",
    )
    assert artifact.kind == "deliverable"
    assert artifact.payload == {"deliverable_kind": "email"}


@pytest.mark.asyncio
async def test_multi_deliverable_and_atomic_batch_cas(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    first = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-pg",
        title="One",
        status="ready",
        content_markdown="old one",
        payload={"stable": 1},
        refs=[{"url": "https://example.com/one"}],
        updated_by="agent",
        artifact_id="art_pg_one",
    )
    second = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-pg",
        title="Two",
        status="ready",
        content_markdown="old two",
        payload={"stable": 2},
        refs=[{"url": "https://example.com/two"}],
        updated_by="agent",
        artifact_id="art_pg_two",
    )
    rows = await control_store.list_session_artifacts("sess-pg")
    assert {row.artifact_id for row in rows if row.kind == "deliverable"} == {
        first.artifact_id,
        second.artifact_id,
    }

    revised = await control_store.revise_session_artifacts_atomically(
        run_id=run_id,
        session_id="sess-pg",
        revisions=[
            ArtifactBatchRevision(first.artifact_id, 1, "new one"),
            ArtifactBatchRevision(second.artifact_id, 1, "new two"),
        ],
    )
    assert [row.revision for row in revised] == [2, 2]
    assert revised[0].payload == {"stable": 1}
    assert tuple(revised[0].refs) == ({"url": "https://example.com/one"},)


@pytest.mark.asyncio
async def test_multiple_sessionless_deliverables(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    first = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id=None,
        title="One",
        status="ready",
        content_markdown="one",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_sessionless_pg_one",
    )
    second = await control_store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id=None,
        title="Two",
        status="ready",
        content_markdown="two",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_sessionless_pg_two",
    )

    assert first.artifact_id != second.artifact_id
    rows, _cursor = await control_store.list_artifacts(
        run_id, kind="deliverable", limit=10
    )
    assert {row.artifact_id for row in rows} == {
        first.artifact_id,
        second.artifact_id,
    }


@pytest.mark.asyncio
async def test_control_rows_cascade_with_their_run(service, run_store, control_store):
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    await control_store.upsert_artifact(
        run_id=run_id,
        kind="critic_report",
        session_id=None,
        title="Kritik",
        status="ready",
        content_markdown="ok",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_critic",
    )
    run_store.cancel(run_id)
    _wait_until(lambda: run_store.get(run_id)["status"] == "cancelled")
    run_store.delete(run_id)

    engine = build_engine(TEST_DATABASE_URL)
    try:
        factory = build_session_factory(engine)
        async with factory() as session:
            for table in ("run_approvals", "run_artifacts", "run_artifact_revisions"):
                count = (
                    await session.execute(
                        text(f"SELECT count(*) FROM {table}")
                    )
                ).scalar_one()
                assert count == 0, table
    finally:
        await engine.dispose()
    assert approval.approval_id  # silence unused warning


@pytest.mark.asyncio
async def test_terminal_run_settles_control_rows_postgres(
    service, run_store, control_store
):
    """Postgres twin: writing→ready, pending approval→rejected, idempotent."""
    run_id = _parked_agent_run(run_store)
    await control_store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-settle-pg",
        title="Memo",
        status="writing",
        content_markdown="# Gestreamter Stand",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_settle_pg",
    )
    approval = await control_store.create_approval(_approval(run_id))
    assert run_store.cancel(run_id)["status"] == "cancelled"

    assert await service.reconcile_terminal_tasks(run_id) is True

    released, _revisions = await control_store.get_artifact(
        run_id, "art_settle_pg"
    )
    assert released.status == "ready"
    assert released.revision == 1
    assert released.content_markdown == "# Gestreamter Stand"

    settled = await control_store.get_approval(run_id, approval.approval_id)
    assert settled.status == "rejected"
    assert settled.decision == ""
    assert "endete" in settled.note
    assert settled.decided_at is not None

    assert await control_store.settle_terminal_control_rows(run_id) == (0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("material_location", ["result", "artifact"])
async def test_retained_evidence_reads_use_the_real_run_visibility(run_store, material_location):
    from types import SimpleNamespace

    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.evidence_read import read_registered_evidence

    control = PostgresAgentControlStore(
        engine=build_engine(TEST_DATABASE_URL, null_pool=True), app_role=APP_ROLE,
    )

    def seed(handle):
        ledger = {"searches": {
            "query_owned": {"query_id": "query_owned", "source_run_id": handle.run_id,
                            "query": "An owned source query", "provider_answer": "Only the owner can read this answer.",
                            "citations": [{"url": "https://example.org/owned", "citation_id": "citation_owned"}]},
        }}
        if material_location == "artifact":
            asyncio.run(control.upsert_artifact(
                run_id=handle.run_id, artifact_id=f"art_{handle.run_id[-12:]}_evidence",
                kind="evidence_bundle", session_id=None, title="Agent evidence",
                status="ready", content_markdown="", refs=[],
                payload={"web_search_ledger": ledger}, updated_by="agent",
            ))
        handle.complete({"answer": "Retained evidence.", **(
            {"web_search_ledger": ledger} if material_location == "result" else {}
        )})

    origin = run_store.submit(
        question="Retained evidence", stack_name="default", work=seed,
        mode="research", kind="standard", created_by_user_id=OWNER_USER_ID,
        created_by_tenant_id="default",
    )["run_id"]
    _wait_until(lambda: run_store.get(origin, visible_to=_scoped(OWNER_USER_ID))["status"] == "completed")

    def read_as(principal):
        reader = run_store.submit(
            question="Read retained evidence", stack_name="default",
            work=lambda handle: handle.complete({"answer": "Reader ready."}),
            mode="agent_kernel", kind="agent", created_by_user_id=principal.principal.user_id,
            created_by_tenant_id="default",
        )["run_id"]
        _wait_until(lambda: run_store.get(reader, visible_to=principal)["status"] == "completed")
        deps = KernelDeps(
            run_id=reader, control=control,
            platform=None, llm=None, model=None, reasoning_effort=None, timeout=1,
            run_service=SimpleNamespace(run_store=run_store),
            capability_context=SimpleNamespace(visible_to=principal),
        )
        ref, = deps.register_references([{"url": "https://example.org/owned", "source_run_ids": [origin]}])
        return read_registered_evidence(deps, reference_id=ref["reference_id"])

    try:
        owned = await asyncio.to_thread(read_as, _scoped(OWNER_USER_ID))
        assert owned["searches"]["query_owned"]["provider_answer"] == "Only the owner can read this answer."
        foreign = await asyncio.to_thread(read_as, _scoped(EDITOR_USER_ID))
        assert foreign["searches"] == {}
        assert foreign["unavailable_origins"][0]["status"] == "unavailable"
    finally:
        await control.aclose()


@pytest.mark.asyncio
async def test_archive_replay_is_atomic_and_does_not_duplicate_revision_bodies(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    body = " \r\n" + "ä🙂e\u0301\n" * 10_000 + "\t "
    first, replay = await asyncio.gather(*(
        control_store.put_context_archive(run_id=run_id, title="Original", text=body)
        for _ in range(2)
    ))
    assert first == replay
    assert "".join(row.content_markdown for row in first) == body
    async with control_store._session() as session:
        count = (await session.execute(text(
            "SELECT count(*) FROM run_artifact_revisions r JOIN run_artifacts a "
            "ON r.artifact_id = a.artifact_id WHERE a.run_id = :run_id"
        ), {"run_id": run_id})).scalar_one()
    assert count == 0
    row, history = await control_store.get_artifact(run_id, first[0].artifact_id, revision=1)
    assert row == first[0]
    assert [(r.revision, r.content_markdown) for r in history] == [(1, "")]
    with pytest.raises(ArtifactNotFound):
        await control_store.get_artifact(run_id, first[0].artifact_id, revision=2)
    sources = await control_store.list_context_archive_sources(run_id)
    assert len(sources) == 1 and sources[0].total_chars == len(body)


@pytest.mark.asyncio
async def test_archive_collision_rolls_back_every_new_part(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    body = "x" * 50_001
    parts = prepare_archive(run_id, "Original", body)
    collision = await control_store.upsert_artifact(
        run_id=run_id, kind="context_archive", session_id=None, title="Existing",
        status="ready", content_markdown="preserve", payload={}, refs=[],
        updated_by="agent", artifact_id=parts[-1].artifact_id,
    )
    before, _ = await control_store.get_artifact(run_id, collision.artifact_id)
    with pytest.raises(ArchiveIntegrityError):
        await control_store.put_context_archive(run_id=run_id, title="Original", text=body)
    listed, _ = await control_store.list_artifacts(run_id)
    assert [row.artifact_id for row in listed] == [collision.artifact_id]
    after, _ = await control_store.get_artifact(run_id, collision.artifact_id)
    assert before == after


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["upsert", "user_update", "rename", "batch"])
async def test_archive_rejects_mutation_in_postgres(operation, run_store, control_store, service):
    run_id = _parked_agent_run(run_store)
    row, = await control_store.put_context_archive(run_id=run_id, title="Original", text="preserve")
    with pytest.raises(ContextArchiveImmutable):
        if operation == "upsert":
            await control_store.upsert_artifact(
                run_id=run_id, kind="context_archive", session_id=None, title="Changed",
                status="ready", content_markdown="changed", payload={}, refs=[],
                updated_by="agent", artifact_id=row.artifact_id,
            )
        elif operation == "user_update":
            await service.user_update_artifact(
                run_id=run_id, artifact_id=row.artifact_id, content_markdown="changed",
                expected_revision=1, principal=None,
            )
        elif operation == "rename":
            await service.rename_artifact(
                run_id=run_id, artifact_id=row.artifact_id, title="Changed", principal=None,
            )
        else:
            await control_store.revise_session_artifacts_atomically(
                run_id=run_id, session_id=None,
                revisions=[ArtifactBatchRevision(row.artifact_id, 1, "changed")],
            )
    current, _ = await control_store.get_artifact(run_id, row.artifact_id)
    assert current == row


@pytest.mark.asyncio
async def test_postgres_archive_reads_selected_text_and_refuses_corrupted_parts(run_store, control_store):
    from sqlalchemy import event

    run_id = _parked_agent_run(run_store)
    body = "a" * 19_999 + "🙂\r\n" + "b" * 40_000 + "END"
    rows = await control_store.put_context_archive(run_id=run_id, title="Original", text=body)
    fingerprint = hashlib.sha256(body.encode()).hexdigest()
    statements = []

    def record_statement(_conn, _cursor, statement, parameters, _context, _many):
        statements.append((statement, parameters))

    event.listen(control_store._engine.sync_engine, "before_cursor_execute", record_statement)
    try:
        window = await control_store.read_context_archive(
            run_id, rows[0].artifact_id, offset=19_990, max_chars=40, content_hash=fingerprint,
        )
    finally:
        event.remove(control_store._engine.sync_engine, "before_cursor_execute", record_statement)
    assert window.text == body[19_990:20_030]
    assert window.offset == 19_990 and window.end == 20_030
    body_reads = [(sql, args) for sql, args in statements if "content_markdown" in sql]
    assert len(body_reads) == 1
    # The body query fetches only overlapping parts, not the full source.
    assert set(body_reads[0][1]) == {run_id, rows[0].artifact_id, rows[1].artifact_id}
    with pytest.raises(ValueError, match="fingerprint"):
        await control_store.read_context_archive(run_id, rows[0].artifact_id, offset=20_000)
    with pytest.raises(ArchiveVersionConflict):
        await control_store.read_context_archive(run_id, rows[0].artifact_id, content_hash="stale")
    direct = await control_store.read_context_archive(run_id, rows[1].artifact_id)
    assert direct.offset == 20_000 and direct.text == body[20_000:40_000]
    async with control_store._session() as session:
        await session.execute(text(
            "UPDATE run_artifacts SET content_markdown = :body WHERE artifact_id = :id"
        ), {"body": "corrupted", "id": rows[0].artifact_id})
    with pytest.raises(ArchiveIntegrityError):
        await control_store.read_context_archive(run_id, rows[0].artifact_id)


@pytest.mark.asyncio
async def test_postgres_catalogue_continuations_bind_complete_snapshot_including_legacy(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    for index in range(4):
        await control_store.put_context_archive(
            run_id=run_id, title=f"Original {index}", text=str(index) * 25_000,
        )
    await control_store.upsert_artifact(
        run_id=run_id, kind="context_archive", session_id=None, title="Legacy",
        status="ready", content_markdown="stored legacy", payload={}, refs=[],
        updated_by="agent", artifact_id="legacy",
    )
    entries = await control_store.list_context_archive_sources(run_id)
    first = catalogue_page(run_id, entries, limit=3)
    last = catalogue_page(run_id, entries, cursor=first.next_cursor, limit=3)
    assert len(first.entries) == 3 and len(last.entries) == 2
    assert len({entry.source_id for entry in (*first.entries, *last.entries)}) == 5
    assert last.entries[-1].fidelity == "legacy_rendered_text"
    assert last.entries[-1].total_chars == 13
    await control_store.put_context_archive(run_id=run_id, title="Added", text="after first page")
    with pytest.raises(ArchiveCursorError):
        catalogue_page(run_id, await control_store.list_context_archive_sources(run_id), cursor=first.next_cursor)


@pytest.mark.asyncio
async def test_sessionless_archive_and_batch_revision_cannot_cross_runs(run_store, control_store):
    run_id = _parked_agent_run(run_store)
    foreign_run = _parked_agent_run(run_store, session_id="other-session")
    row, = await control_store.put_context_archive(run_id=run_id, title="Original", text="private")
    with pytest.raises(ArtifactNotFound):
        await control_store.read_context_archive(foreign_run, row.artifact_id)
    assert await control_store.list_context_archive_sources(foreign_run) == []
    legacy = await control_store.upsert_artifact(
        run_id=run_id, kind="context_archive", session_id=None, title="Legacy",
        status="ready", content_markdown="preserve", payload={}, refs=[],
        updated_by="agent", artifact_id="legacy",
    )
    with pytest.raises(ArtifactNotFound):
        await control_store.revise_session_artifacts_atomically(
            run_id=foreign_run, session_id=None,
            revisions=[ArtifactBatchRevision(legacy.artifact_id, 1, "changed")],
        )
    current, _ = await control_store.get_artifact(run_id, legacy.artifact_id)
    assert current.content_markdown == "preserve" and current.revision == 1


@pytest.mark.asyncio
async def test_private_checkpoint_originals_use_owner_reader_and_hide_product_routes(run_store, control_store):
    from inqtrix.agents.context_archive import text_hash
    from inqtrix.agents.context_sources import ContextReadInput, encode_source
    from inqtrix.capabilities import CapabilityContext, CapabilityError
    from inqtrix.project.agent_sessions_postgres import PostgresAgentSessionStore
    from inqtrix.services.agent_sessions_service import AgentSessionsService
    from inqtrix.services.agent_source_service import AgentSourceService

    run_id = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    body = " \r\nPrivate checkpoint ä🙂e\u0301\t " * 1000
    rows = await control_store.put_context_archive(run_id=run_id, title="Transcript", text=body,
        transcript={"schema": 1, "checkpoint_id": "checkpoint-version", "format": "checkpoint_json"})
    assert await control_store.list_artifacts(run_id) == ([], None)
    assert await control_store.list_context_archive_sources(run_id) == []
    for action in (control_store.get_artifact, control_store.read_context_archive):
        with pytest.raises(ArtifactNotFound):
            await action(run_id, rows[0].artifact_id)
    metadata = await control_store.list_source_artifacts([run_id])
    assert len(metadata) == len(rows) and all(row.content_markdown == "" for row in metadata)
    sessions = PostgresAgentSessionStore(engine=build_engine(TEST_DATABASE_URL), app_role=APP_ROLE)
    try:
        source = AgentSourceService(sessions=AgentSessionsService(store=sessions, run_store=run_store),
                                   run_store=run_store, control=control_store)
        owner = _scoped(OWNER_USER_ID)
        context = CapabilityContext(principal=owner.principal, visible_to=owner, run_id=run_id)
        metadata = run_store.source_run_catalogue("sess-pg", visible_to=owner)
        assert len(metadata) == 1 and set(metadata[0]) == {"run_id", "question", "kind", "mode", "status", "created_at"}
        assert run_store.source_run_catalogue("sess-pg", visible_to=_scoped(EDITOR_USER_ID)) == []
        ref = encode_source("transcript", run_id, rows[0].artifact_id, text_hash(body))
        first = await source.read(ContextReadInput(source_id=ref), context)
        assert first.text == body[:20_000] and first.next_read
        second = await source.read(ContextReadInput(**first.next_read), context)
        assert first.text + second.text == body
        reader = _scoped(EDITOR_USER_ID)
        with pytest.raises(CapabilityError):
            await source.read(ContextReadInput(source_id=ref), replace(context, principal=reader.principal, visible_to=reader))
    finally:
        await sessions.aclose()


def test_a_role_run_is_no_conversation_source_and_names_its_profile(run_store):
    """An internal role child holds nothing but material its parent already read."""
    parent = _parked_agent_run(run_store, owner_user_id=OWNER_USER_ID)
    owner = _scoped(OWNER_USER_ID)

    def submit_child(body: dict) -> str:
        summary = run_store.submit(
            question="Review the outputs.", stack_name="default",
            work=lambda handle: handle.complete({"answer": ""}),
            request_payload={"question": "Review the outputs.", "body": {"mode": "agent_kernel", **body}},
            kind="agent_child", parent_run_id=parent, root_run_id=parent,
            created_by_user_id=OWNER_USER_ID, created_by_tenant_id="default",
        )
        _wait_until(lambda: run_store.get(summary["run_id"], visible_to=owner)["status"] == "completed")
        return summary["run_id"]

    role = submit_child({"work_profile": "review", "origin_key": "role-review"})
    delegated = submit_child({"origin_key": "call_research"})
    listed = {row["run_id"] for row in run_store.source_run_catalogue("sess-pg", visible_to=owner)}
    assert {parent, delegated} <= listed
    assert role not in listed
    assert run_store.get(role, visible_to=owner)["work_profile"] == "review"
    assert "work_profile" not in run_store.get(delegated, visible_to=owner)


@pytest.mark.asyncio
async def test_saved_conversation_retains_originals_and_aggregate_delete_removes_them(run_store, control_store):
    from sqlalchemy import update
    from inqtrix.storage.runs_orm import runs

    run_id = _parked_agent_run(run_store)
    await control_store.put_context_archive(run_id=run_id, title="Transcript", text="Keep original",
        transcript={"schema": 1, "checkpoint_id": "checkpoint-version", "format": "checkpoint_json"})
    run_store.cancel(run_id)
    ordinary = run_store.submit(question="Ordinary", stack_name="fixture", work=lambda h: h.complete({"answer": "Done"}))
    _wait_until(lambda: run_store.get(ordinary["run_id"])["status"] == "completed")
    async def backdate():
        async with run_store._session("default") as session:
            await session.execute(update(runs).where(runs.c.run_id.in_([run_id, ordinary["run_id"]]))
                                  .values(finished_at=time.time() - 400))
    run_store._call(backdate())
    assert run_store.get(run_id)["run_id"] == run_id
    with pytest.raises(RunNotFound):
        run_store.get(ordinary["run_id"])
    assert len(await control_store.list_context_archive_sources(run_id, include_private=True)) == 1
    async def tombstone():
        async with run_store._session("default") as session:
            await session.execute(agent_sessions.update().where(agent_sessions.c.id == "sess-pg")
                .values(lifecycle_status="deleting", deletion_operation_id="del_saved_original", deletion_stage="queued"))
    run_store._call(tombstone())
    assert run_store.get(run_id)["run_id"] == run_id
    arguments = dict(tenant_id="default", requester_user_id=None, workspace_id=None, run_ids=(run_id,))
    run_store.prepare_agent_session_aggregate_deletion("sess-pg", **arguments)
    run_store.delete_agent_session_aggregate("sess-pg", **arguments)
    assert await control_store.list_source_artifacts([run_id]) == []
    assert not any(run_store.agent_session_residuals("sess-pg", **arguments).values())
