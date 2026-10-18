"""Agent control service + memory store: state machines, the composed
decision-and-resume commit, artifact 409 matrix, events, audit (offline
tier).

The Postgres lockstep counterpart lives in
``tests/storage/test_agent_control_postgres.py``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    ApprovalAlreadyDecided,
    session_memo_artifact_id,
    ApprovalRecord,
    ArtifactLocked,
    ArtifactNotFound,
    ArtifactRevisionConflict,
    ClarificationRecord,
    PlanNotFound,
    PlanRecord,
    PlanTaskCancellationConflict,
    PlanTaskRecord,
    settle_terminal_plan_tasks,
)
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.server.runs import RunActive, RunHandle, RunNotFound, RunStore
from inqtrix.services.agent_control_service import (
    AgentControlService,
    AgentControlUnavailable,
    AgentControlValidationError,
)

PRINCIPAL = Principal(
    user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
    kind="oidc_session",
    tenant_id="default",
    role="member",
)
VISIBLE = UserContext(principal=PRINCIPAL)


async def _request_task_cancel_unscoped(
    store: MemoryAgentControlStore,
    *,
    run_id: str,
    plan_id: str,
    task_id: str,
) -> PlanTaskRecord:
    async def _authorize(control_write: Any) -> Any:
        return control_write(None, lambda _child_run_id: "cancelled")

    task, _child_status = await store.request_plan_task_cancel(
        run_id=run_id,
        plan_id=plan_id,
        task_id=task_id,
        authorize=_authorize,
    )
    return task


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    pytest.fail("condition was not reached before timeout")


@pytest.fixture()
def run_store() -> RunStore:
    return RunStore(
        max_concurrent=2,
        max_queue_size=8,
        completed_ttl_seconds=60,
        event_buffer_size=64,
    )


@pytest.fixture()
def identity() -> MemoryIdentityStore:
    return MemoryIdentityStore()


@pytest.fixture()
def service(run_store: RunStore, identity: MemoryIdentityStore) -> AgentControlService:
    return AgentControlService(
        store=MemoryAgentControlStore(),
        run_store=run_store,
        audit=identity,
        editor_persistence=None,
        durable=False,
    )


async def _bind_scoped_control_authority(
    *,
    run_store: RunStore,
    identity: MemoryIdentityStore,
    control: MemoryAgentControlStore,
    user_ids: tuple[uuid.UUID, ...],
) -> MemoryUserDirectory:
    """Compose the production memory authority boundary for focused races."""
    users = MemoryUserDirectory()
    for index, user_id in enumerate(user_ids):
        await users.record_login(
            tenant_id="default",
            issuer="https://issuer.example",
            subject=f"user-{index}",
            email=f"user-{index}@example.com",
            email_verified=True,
            display_name=f"User {index}",
            canonical_user_id=user_id,
        )
    authority = MemoryAuthorityCoordinator()
    authority.bind_users(users)
    identity.bind_authority_coordinator(authority)
    run_store.bind_authorization(
        share_lookup=identity.permission_for_sync,
        share_workspace_check=identity.share_workspace_sync,
        resource_access_guard=identity.resource_access_guard_sync,
        restrict_to_workspace_members=False,
    )
    run_store.bind_authority_coordinator(authority)
    control.bind_authority_coordinator(authority)
    control.bind_execution_guard(run_store.execution_control_guard)
    return users


def _parked_agent_run(
    run_store: RunStore,
    *,
    resumed_release: threading.Event | None = None,
    knowledge_collection_ids: list[str] | None = None,
) -> str:
    """Submit an agent run that parks itself and completes on resume."""
    calls = {"count": 0}

    def segmented(handle: RunHandle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        if resumed_release is not None:
            assert resumed_release.wait(timeout=10.0)
        handle.complete({"answer": "fortgesetzt"})

    summary = run_store.submit(
        question="Agentenauftrag",
        stack_name="default",
        work=segmented,
        kind="agent",
        session_id="sess-1",
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
        request_payload={
            "body": {
                "knowledge_filters": {
                    "collection_ids": list(
                        knowledge_collection_ids
                        if knowledge_collection_ids is not None
                        else []
                    )
                }
            }
        },
    )
    run_id = summary["run_id"]
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "waiting_for_approval"
    )
    return run_id


def _approval(run_id: str, *, kind: str = "plan") -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=f"apr_{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        kind=kind,
    )


def _plan_body() -> dict[str, Any]:
    return {
        "summary_markdown": "Ueberarbeiteter Plan",
        "tasks": [
            {
                "id": "t1",
                "title": "Interne Suche",
                "tool_kind": "rag_query",
                "queries": ["Welche internen Berichte sind relevant?"],
                "params": {"profile": "standard"},
            },
            {
                "id": "s",
                "title": "Synthese",
                "tool_kind": "synthesis",
                "depends_on": ["t1"],
            },
        ],
        "success_criteria": ["Alle Kernfragen belegt."],
    }


# -- approval decision state machine ------------------------------------- #


@pytest.mark.asyncio
async def test_memory_decision_stays_pending_until_resume_invokes_writer() -> None:
    """A failed/blocked resume never exposes a transient decided row."""
    store = MemoryAgentControlStore()
    approval = await store.create_approval(_approval("run-atomic"))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_resume(writer: Any) -> dict[str, Any]:
        assert callable(writer)
        entered.set()
        await release.wait()
        raise RunActive("resume rejected")

    decision = asyncio.create_task(
        store.decide_approval_and_resume(
            run_id="run-atomic",
            approval_id=approval.approval_id,
            decision="approve",
            decision_payload={},
            note="",
            decided_by_user_id=PRINCIPAL.user_id,
            resume=blocked_resume,
        )
    )
    await entered.wait()
    assert (
        await store.get_approval("run-atomic", approval.approval_id)
    ).status == "pending"
    release.set()
    with pytest.raises(RunActive):
        await decision
    assert (
        await store.get_approval("run-atomic", approval.approval_id)
    ).status == "pending"


@pytest.mark.asyncio
async def test_disabled_actor_cannot_commit_memory_decision_or_resume(
    run_store: RunStore, identity: MemoryIdentityStore
) -> None:
    """The shared authority boundary revalidates active user at resume."""
    control = MemoryAgentControlStore()
    users = await _bind_scoped_control_authority(
        run_store=run_store,
        identity=identity,
        control=control,
        user_ids=(PRINCIPAL.user_id,),
    )
    service = AgentControlService(
        store=control,
        run_store=run_store,
        audit=None,
        editor_persistence=None,
        durable=False,
    )
    run_id = _parked_agent_run(run_store)
    approval = await control.create_approval(_approval(run_id))
    assert await users.set_disabled(
        tenant_id="default",
        user_id=PRINCIPAL.user_id,
        disabled_at=time.time(),
    )

    with pytest.raises(RunNotFound):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )

    assert (await control.get_approval(run_id, approval.approval_id)).status == (
        "pending"
    )
    with run_store._lock:
        assert run_store._records[run_id].status.value == "waiting_for_approval"


@pytest.mark.asyncio
async def test_approve_decision_resumes_run_and_audits(
    service: AgentControlService,
    run_store: RunStore,
    identity: MemoryIdentityStore,
) -> None:
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
            note="passt",
            principal=PRINCIPAL,
        )
    finally:
        resumed_release.set()

    assert not replayed
    assert decided.status == "approved"
    assert decided.decided_by_user_id == PRINCIPAL.user_id
    assert decided.note == "passt"
    assert summary["status"] in {"queued", "running", "completed"}
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )

    subscription = run_store.subscribe(run_id, visible_to=VISIBLE)
    try:
        decision_event = next(
            event
            for event in subscription.replay
            if event["type"] == "inqtrix.agent.approval.decided"
        )
        assert decision_event["data"]["decided_by_user_id"] == str(
            PRINCIPAL.user_id
        )
        resumed_event = next(
            event
            for event in subscription.replay
            if event["type"] == "inqtrix.run.queued"
            and event["data"].get("resumed")
        )
        # The decision event follows the resumed-queued event: rows are
        # truth, events are post-commit signals.
        assert resumed_event["sequence"] < decision_event["sequence"]
    finally:
        subscription.close()
    actions = [entry.action for entry in identity.audit_entries]
    assert "agent.approval_decided" in actions
    entry = identity.audit_entries[-1]
    assert entry.actor_type == "user"
    assert entry.detail["decision"] == "approve"


@pytest.mark.asyncio
async def test_replay_of_same_decision_is_idempotent(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="reject",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
    )

    decided, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="reject",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
    )

    assert replayed
    assert decided.status == "rejected"

    with pytest.raises(ApprovalAlreadyDecided):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_decision_on_non_waiting_run_rolls_back(
    service: AgentControlService, run_store: RunStore
) -> None:
    summary = run_store.submit(
        question="normal",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent",
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    run_id = summary["run_id"]
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )
    approval = await service.store.create_approval(_approval(run_id))

    with pytest.raises(RunActive):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )

    # The decision must NOT stand: the approval stays pending.
    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    subscription = run_store.subscribe(run_id, visible_to=VISIBLE)
    try:
        assert "inqtrix.agent.approval.decided" not in {
            event["type"] for event in subscription.replay
        }
    finally:
        subscription.close()


@pytest.mark.asyncio
async def test_edit_decision_appends_approved_plan_version(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    initial = PlanRecord(
        plan_id="plan_v1",
        run_id=run_id,
        version=1,
        status="proposed",
        created_by="agent",
    )
    await service.store.save_plan(
        run_id=run_id,
        plan=initial,
        tasks=[
            PlanTaskRecord(
                task_id="t1",
                plan_id="plan_v1",
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
        plan_body=_plan_body(),
        note="",
        principal=PRINCIPAL,
    )

    assert decided.status == "edited"
    assert decided.decision_payload["plan"]["summary_markdown"] == (
        "Ueberarbeiteter Plan"
    )
    plan, tasks, versions = await service.get_plan(run_id)
    assert plan.version == 2
    assert plan.status == "approved"
    assert plan.created_by == "user"
    assert plan.reason == "user_edit"
    assert [task.task_id for task in tasks] == ["t1", "s"]
    assert [(v.version, v.status) for v in versions] == [
        (2, "approved"),
        (1, "superseded"),
    ]


@pytest.mark.asyncio
async def test_edit_decision_validates_through_the_plan_validator(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    broken = _plan_body()
    broken["tasks"][1]["depends_on"] = []  # synthesis no longer covers t1

    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=broken,
            note="",
            principal=PRINCIPAL,
        )

    assert any("synthesis" in error for error in exc_info.value.errors)
    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    with pytest.raises(PlanNotFound):
        await service.store.get_plan(run_id)


@pytest.mark.asyncio
async def test_task_transitions_follow_retry_and_terminal_contract(
    service: AgentControlService,
) -> None:
    await service.store.save_plan(
        run_id="run-task",
        plan=PlanRecord(
            plan_id="plan-task",
            run_id="run-task",
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="t1",
                plan_id="plan-task",
                run_id="run-task",
                ordinal=0,
                title="Research",
                tool_kind="web_research",
            )
        ],
    )

    first = await service.store.transition_plan_task(
        run_id="run-task",
        plan_id="plan-task",
        task_id="t1",
        status="running",
        child_run_id="run-child-1",
    )
    assert first.child_run_id == "run-child-1"
    retry = await service.store.transition_plan_task(
        run_id="run-task",
        plan_id="plan-task",
        task_id="t1",
        status="running",
        child_run_id="run-child-2",
    )
    assert retry.child_run_id == "run-child-2"
    terminal = await service.store.transition_plan_task(
        run_id="run-task",
        plan_id="plan-task",
        task_id="t1",
        status="insufficient_evidence",
        child_run_id="run-child-2",
        result_summary="Only one independent source was available.",
        result_payload={
            "evidence": [{"url": "https://example.test/source"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        },
    )
    assert terminal.status == "insufficient_evidence"
    assert terminal.result_payload["usage"]["prompt_tokens"] == 3
    replay = await service.store.transition_plan_task(
        run_id="run-task",
        plan_id="plan-task",
        task_id="t1",
        status="insufficient_evidence",
        child_run_id="run-child-2",
        result_summary="Only one independent source was available.",
        result_payload=terminal.result_payload,
    )
    assert replay == terminal
    with pytest.raises(ValueError):
        await service.store.transition_plan_task(
            run_id="run-task",
            plan_id="plan-task",
            task_id="t1",
            status="completed",
        )
    with pytest.raises(ValueError):
        await service.store.transition_plan_task(
            run_id="run-task",
            plan_id="plan-task",
            task_id="t1",
            status="insufficient_evidence",
            child_run_id="run-child-3",
        )
    with pytest.raises(ValueError):
        await service.store.transition_plan_task(
            run_id="run-task",
            plan_id="plan-task",
            task_id="t1",
            status="insufficient_evidence",
            result_summary="A different terminal result.",
        )
    with pytest.raises(ValueError):
        await service.store.transition_plan_task(
            run_id="run-task",
            plan_id="plan-task",
            task_id="t1",
            status="insufficient_evidence",
            result_payload={"claims": [{"text": "different"}]},
        )


@pytest.mark.asyncio
async def test_task_transition_rejects_metadata_before_its_lifecycle_stage(
    service: AgentControlService,
) -> None:
    await service.store.save_plan(
        run_id="run-task-metadata",
        plan=PlanRecord(
            plan_id="plan-task-metadata",
            run_id="run-task-metadata",
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="t1",
                plan_id="plan-task-metadata",
                run_id="run-task-metadata",
                ordinal=0,
                title="Research",
                tool_kind="web_research",
            )
        ],
    )

    with pytest.raises(ValueError, match="before running"):
        await service.store.transition_plan_task(
            run_id="run-task-metadata",
            plan_id="plan-task-metadata",
            task_id="t1",
            status="skipped",
            child_run_id="run-child-early",
        )
    with pytest.raises(ValueError, match="terminal status"):
        await service.store.transition_plan_task(
            run_id="run-task-metadata",
            plan_id="plan-task-metadata",
            task_id="t1",
            status="running",
            result_summary="Too early.",
        )


@pytest.mark.asyncio
async def test_task_cancellation_is_atomic_idempotent_and_source_only(
    service: AgentControlService,
) -> None:
    plan = PlanRecord(
        plan_id="plan-task-cancel",
        run_id="run-task-cancel",
        version=1,
        status="approved",
        created_by="agent",
    )
    tasks = [
        PlanTaskRecord(
            task_id="pending",
            plan_id=plan.plan_id,
            run_id=plan.run_id,
            ordinal=0,
            title="Pending",
            tool_kind="web_instant",
        ),
        PlanTaskRecord(
            task_id="running",
            plan_id=plan.plan_id,
            run_id=plan.run_id,
            ordinal=1,
            title="Running",
            tool_kind="web_instant",
        ),
        PlanTaskRecord(
            task_id="s",
            plan_id=plan.plan_id,
            run_id=plan.run_id,
            ordinal=2,
            title="Synthesis",
            tool_kind="synthesis",
        ),
    ]
    await service.store.save_plan(run_id=plan.run_id, plan=plan, tasks=tasks)
    await service.store.transition_plan_task(
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        task_id="running",
        status="running",
    )

    pending = await _request_task_cancel_unscoped(
        service.store,
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        task_id="pending",
    )
    assert pending.status == "cancelled"
    assert pending.result_payload["failure_code"] == "task_cancelled"
    assert (
        await _request_task_cancel_unscoped(
            service.store,
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            task_id="pending",
        )
    ) == pending

    running = await _request_task_cancel_unscoped(
        service.store,
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        task_id="running",
    )
    assert running.status == "cancel_requested"
    await settle_terminal_plan_tasks(
        service.store,
        plan.run_id,
        status="cancelled",
    )
    _settled_plan, settled_tasks = await service.store.get_plan(plan.run_id)
    cancelled = next(task for task in settled_tasks if task.task_id == "running")
    assert cancelled.status == "cancelled"
    assert cancelled.result_payload == {
        "failure_code": "task_cancelled",
        "failure_reason": "user_requested_task_cancel",
    }

    with pytest.raises(PlanTaskCancellationConflict):
        await _request_task_cancel_unscoped(
            service.store,
            run_id=plan.run_id,
            plan_id=plan.plan_id,
            task_id="s",
        )


@pytest.mark.asyncio
async def test_settlement_summaries_follow_the_run_language(
    service: AgentControlService,
):
    """What a user reads after pressing Stop is in the run's language.

    The graph holds the language and passes it. The HTTP recovery boundary
    for a run cancelled while waiting holds only a run id, omits it, and
    gets ENGLISH -- the product's fallback everywhere -- never a silent
    German default.
    """
    for index, (language, expected) in enumerate(
        (
            ("de", "Wegen Abbruch nicht ausgeführt."),
            ("en", "Not executed because the run was cancelled."),
            (None, "Not executed because the run was cancelled."),
        )
    ):
        plan = PlanRecord(
            plan_id=f"plan-lang-{index}",
            run_id=f"run-lang-{index}",
            version=1,
            status="approved",
            created_by="agent",
        )
        await service.store.save_plan(
            run_id=plan.run_id,
            plan=plan,
            tasks=[
                PlanTaskRecord(
                    task_id="pending",
                    plan_id=plan.plan_id,
                    run_id=plan.run_id,
                    ordinal=0,
                    title="Pending",
                    tool_kind="web_instant",
                )
            ],
        )
        await settle_terminal_plan_tasks(
            service.store,
            plan.run_id,
            status="cancelled",
            language=language,
        )
        _p, tasks = await service.store.get_plan(plan.run_id)
        pending = next(t for t in tasks if t.task_id == "pending")
        assert pending.status == "skipped"
        assert pending.result_summary == expected


@pytest.mark.asyncio
async def test_shared_editor_task_cancel_reaches_the_running_child(
    service: AgentControlService,
    run_store: RunStore,
    identity: MemoryIdentityStore,
) -> None:
    """The caller's root edit grant governs the child cancel atomically."""
    run_store.bind_authorization(
        share_lookup=identity.permission_for_sync,
        share_workspace_check=identity.share_workspace_sync,
        resource_access_guard=identity.resource_access_guard_sync,
        restrict_to_workspace_members=False,
    )
    run_id = _parked_agent_run(run_store)
    child_started = threading.Event()
    child_release = threading.Event()

    def child_work(handle: RunHandle) -> None:
        child_started.set()
        assert child_release.wait(timeout=5.0)
        if handle.cancel_event.is_set():
            handle.cancel("task_cancelled")
        else:
            handle.complete({"answer": "late"})

    child = run_store.submit(
        question="Child research",
        stack_name="default",
        work=child_work,
        kind="agent_child",
        parent_run_id=run_id,
        root_run_id=run_id,
        request_payload={
            "body": {
                "parent_task_id": "research",
                "parent_task_attempt": 1,
            }
        },
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    assert child_started.wait(timeout=2.0)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-shared-child-cancel",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="research",
                plan_id="plan-shared-child-cancel",
                run_id=run_id,
                ordinal=0,
                title="Research",
                tool_kind="web_research",
                status="running",
                child_run_id=child["run_id"],
            )
        ],
    )
    recipient_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    recipient_principal = Principal(
        user_id=recipient_id,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    recipient = UserContext(principal=recipient_principal)
    users = MemoryUserDirectory()
    await users.record_login(
        tenant_id="default",
        issuer="https://issuer.example",
        subject="owner",
        email="owner@example.com",
        email_verified=True,
        display_name="Owner",
        canonical_user_id=PRINCIPAL.user_id,
    )
    await users.record_login(
        tenant_id="default",
        issuer="https://issuer.example",
        subject="editor",
        email="editor@example.com",
        email_verified=True,
        display_name="Editor",
        canonical_user_id=recipient_id,
    )
    authority = MemoryAuthorityCoordinator()
    authority.bind_users(users)
    identity.bind_authority_coordinator(authority)
    run_store.bind_authority_coordinator(authority)
    identity.add_share(
        recipient_user_id=recipient_id,
        resource_type="run",
        resource_id=run_id,
        permission=SharePermission.EDIT,
        granted_by_user_id=PRINCIPAL.user_id,
    )

    try:
        task = await service.request_task_cancel(
            run_id,
            "research",
            workspace_id=None,
            principal=recipient_principal,
            visible_to=recipient,
        )
        assert task.status == "cancel_requested"
        child_release.set()
        _wait_until(
            lambda: run_store.get(child["run_id"], visible_to=VISIBLE)["status"]
            == "cancelled"
        )
    finally:
        child_release.set()


@pytest.mark.asyncio
async def test_task_cancel_missing_child_rolls_back_the_control_row(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-missing-child",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="research",
                plan_id="plan-missing-child",
                run_id=run_id,
                ordinal=0,
                title="Research",
                tool_kind="web_research",
                status="running",
                child_run_id="run-missing-child",
            )
        ],
    )

    with pytest.raises(RunNotFound):
        await service.request_task_cancel(
            run_id,
            "research",
            workspace_id=None,
            principal=PRINCIPAL,
            visible_to=VISIBLE,
        )

    _plan, tasks = await service.store.get_plan(run_id)
    assert tasks[0].status == "running"


@pytest.mark.asyncio
async def test_cancel_settlement_folds_child_completed_before_parent_cancel(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    """The synchronous waiting-run cancel cannot downgrade a finished child."""
    run_id = _parked_agent_run(run_store)
    child = run_store.submit(
        question="Child research",
        stack_name="default",
        work=lambda handle: handle.complete(
            {
                "answer": "Belegte Antwort",
                "references": [{"url": "https://example.test/source"}],
                "top_claims": [],
            }
        ),
        kind="agent_child",
        parent_run_id=run_id,
        root_run_id=run_id,
        request_payload={
            "body": {"parent_task_id": "r", "parent_task_attempt": 1}
        },
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    child_id = child["run_id"]
    _wait_until(
        lambda: run_store.get(child_id, visible_to=VISIBLE)["status"]
        == "completed"
    )
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-cancel-child",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="r",
                plan_id="plan-cancel-child",
                run_id=run_id,
                ordinal=0,
                title="Research",
                tool_kind="web_research",
                status="running",
                child_run_id=child_id,
            ),
            PlanTaskRecord(
                task_id="s",
                plan_id="plan-cancel-child",
                run_id=run_id,
                ordinal=1,
                title="Synthesis",
                tool_kind="synthesis",
                depends_on=("r",),
            ),
        ],
    )

    assert run_store.cancel(run_id, visible_to=VISIBLE)["status"] == "cancelled"
    await service.settle_cancelled_tasks(run_id)

    _plan, tasks, _versions = await service.get_plan(run_id)
    assert tasks[0].status == "completed"
    assert tasks[0].result_payload["evidence"] == [
        {"url": "https://example.test/source"}
    ]
    assert tasks[1].status == "skipped"


@pytest.mark.asyncio
async def test_tree_cancel_reconciles_nested_agent_plan_rows(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    """Immediate child cancellation cannot leave its own plan in progress."""
    root_id = _parked_agent_run(run_store)

    def parked_child(handle: RunHandle) -> None:
        handle.wait("waiting_for_input")

    child = run_store.submit(
        question="Nested mission",
        stack_name="default",
        work=parked_child,
        kind="agent_child",
        parent_run_id=root_id,
        root_run_id=root_id,
        request_payload={"body": {"mode": "workspace_agent"}},
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    child_id = child["run_id"]
    _wait_until(
        lambda: run_store.get(child_id, visible_to=VISIBLE)["status"]
        == "waiting_for_input"
    )
    await service.store.save_plan(
        run_id=child_id,
        plan=PlanRecord(
            plan_id="plan-nested-cancel",
            run_id=child_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="running",
                plan_id="plan-nested-cancel",
                run_id=child_id,
                ordinal=0,
                title="Begonnen",
                tool_kind="web_instant",
                status="running",
            ),
            PlanTaskRecord(
                task_id="pending",
                plan_id="plan-nested-cancel",
                run_id=child_id,
                ordinal=1,
                title="Ausstehend",
                tool_kind="synthesis",
            ),
        ],
    )

    summary, affected = run_store.cancel_tree(root_id, visible_to=VISIBLE)
    assert summary["status"] == "cancelled"
    assert child_id in affected
    await service.reconcile_terminal_run_tree(affected)

    _plan, tasks = await service.store.get_plan(child_id)
    assert [task.status for task in tasks] == ["failed", "skipped"]


@pytest.mark.asyncio
async def test_plan_read_repairs_cancelled_run_after_post_commit_gap(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    """Reload is the durable recovery when cancel committed before settle."""
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-read-recovery",
            run_id=run_id,
            version=1,
            status="approved",
            created_by="agent",
        ),
        tasks=[
            PlanTaskRecord(
                task_id="pending",
                plan_id="plan-read-recovery",
                run_id=run_id,
                ordinal=0,
                title="Ausstehend",
                tool_kind="synthesis",
            )
        ],
    )
    assert run_store.cancel(run_id, visible_to=VISIBLE)["status"] == "cancelled"
    _raw_plan, raw_tasks = await service.store.get_plan(run_id)
    assert raw_tasks[0].status == "pending"

    _plan, repaired, _versions = await service.get_plan(run_id)
    assert repaired[0].status == "skipped"


@pytest.mark.asyncio
async def test_plan_approval_rejects_subject_from_another_run() -> None:
    store = MemoryAgentControlStore()
    await store.save_plan(
        run_id="run-a",
        plan=PlanRecord(
            plan_id="plan-a", run_id="run-a", version=1,
            status="proposed", created_by="agent",
        ),
        tasks=[],
    )
    await store.save_plan(
        run_id="run-b",
        plan=PlanRecord(
            plan_id="plan-b", run_id="run-b", version=1,
            status="proposed", created_by="agent",
        ),
        tasks=[],
    )

    with pytest.raises(PlanNotFound):
        await store.create_approval(
            ApprovalRecord(
                approval_id="apr-cross-run",
                run_id="run-a",
                kind="plan",
                subject_type="plan",
                subject_id="plan-b",
            )
        )

    assert await store.list_approvals("run-a") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("approve", "approved"), ("reject", "rejected")],
)
async def test_plan_decision_updates_approval_and_plan_together(
    service: AgentControlService,
    run_store: RunStore,
    decision: str,
    expected_status: str,
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-v1",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[],
    )
    approval = await service.store.create_approval(
        ApprovalRecord(
            approval_id=f"apr-{decision}",
            run_id=run_id,
            kind="plan",
            subject_type="plan",
            payload={"plan_version": 1},
        )
    )
    assert approval.subject_id == "plan-v1"

    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision=decision,
        plan_body=None,
        note="",
        principal=PRINCIPAL,
    )

    plan, _tasks = await service.store.get_plan(run_id)
    assert plan.status == expected_status


@pytest.mark.asyncio
async def test_plan_update_failure_leaves_memory_approval_pending(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan-missing",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[],
    )
    approval = await service.store.create_approval(
        ApprovalRecord(
            approval_id="apr-missing-plan",
            run_id=run_id,
            kind="plan",
            subject_type="plan",
            payload={"plan_version": 1},
        )
    )
    store = service.store
    assert isinstance(store, MemoryAgentControlStore)
    with store._lock:
        store._plans.pop("plan-missing")
        store._plan_tasks.pop("plan-missing", None)

    with pytest.raises(PlanNotFound):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )

    stored = await store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    assert (
        run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "waiting_for_approval"
    )


@pytest.mark.asyncio
async def test_edited_plan_rejects_client_managed_task_budget(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    body = _plan_body()
    body["tasks"][0]["budget"] = {"max_tokens": 1800}

    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=body,
            note="",
            principal=PRINCIPAL,
        )

    assert exc_info.value.error_type == "task_budget_server_managed"
    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"


def _research_edit_plan(profile: str) -> dict[str, Any]:
    return {
        "summary_markdown": "Explicit research",
        "tasks": [
            {
                "id": "r1",
                "title": "Research",
                "tool_kind": "web_research",
                "queries": ["Which evidence answers the question?"],
                "params": {"profile": profile},
            },
            {
                "id": "s",
                "title": "Synthesis",
                "tool_kind": "synthesis",
                "depends_on": ["r1"],
            },
        ],
    }


@pytest.mark.asyncio
async def test_edited_plan_removes_task_model_tier_without_mutating_input(service, run_store):
    from copy import deepcopy

    run_id = _parked_agent_run(run_store)
    body = _research_edit_plan("compact")
    body["tasks"][0]["params"].update(model_tier="high", recency="year")
    original = deepcopy(body)

    _plan, tasks = await service._parse_edited_plan(run_id, body, VISIBLE)

    assert tasks[0].params == {"profile": "compact", "recency": "year"}
    assert body == original


@pytest.mark.asyncio
async def test_edited_plan_still_refuses_unrecognized_task_parameters(service, run_store):
    run_id = _parked_agent_run(run_store)
    body = _research_edit_plan("compact")
    body["tasks"][0]["params"].update(model_tier="high", misspelled_profile="deep")

    with pytest.raises(AgentControlValidationError) as error:
        await service._parse_edited_plan(run_id, body, VISIBLE)

    assert any("misspelled_profile" in line for line in error.value.errors)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("depth", "profile", "accepted"),
    [
        ("normal", "compact", True),
        ("normal", "deep", False),
        ("deep", "deep", True),
        ("deep", "compact", False),
    ],
)
async def test_edited_research_plan_uses_server_selected_profile(
    service: AgentControlService,
    run_store: RunStore,
    depth: str,
    profile: str,
    accepted: bool,
) -> None:
    summary = run_store.submit(
        question="Agent assignment",
        stack_name="default",
        work=lambda handle: handle.complete({"answer": "done"}),
        kind="agent",
        agent_overrides={"depth": depth},
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    _wait_until(
        lambda: run_store.get(
            summary["run_id"], visible_to=VISIBLE
        )["status"] == "completed"
    )
    if accepted:
        _plan, tasks = await service._parse_edited_plan(
            summary["run_id"], _research_edit_plan(profile), None
        )
        assert tasks[0].params["profile"] == profile
        return
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service._parse_edited_plan(
            summary["run_id"], _research_edit_plan(profile), None
        )
    assert any("profile=" in error for error in exc_info.value.errors)


@pytest.mark.asyncio
async def test_edited_plan_preflights_collection_visibility(
    run_store: RunStore, identity: MemoryIdentityStore
) -> None:
    """An edited rag task pointing at an invisible collection is a 400 at
    edit time — the same collection-visibility rule retrieval enforces at
    runtime — never a silent approve that fails mid-run. The
    caller-visible catalog (THE shared resolver/validator the planner
    also runs) is the gate."""

    class CatalogKnowledge:
        def __init__(self) -> None:
            self.calls = 0

        async def list_collections(self, *, visible_to=None):
            self.calls += 1
            return [
                SimpleNamespace(id="kc_visible", name="Marktdaten"),
            ]

    knowledge = CatalogKnowledge()
    service = AgentControlService(
        store=MemoryAgentControlStore(),
        run_store=run_store,
        audit=identity,
        knowledge=knowledge,
        durable=False,
    )
    run_id = _parked_agent_run(
        run_store, knowledge_collection_ids=["kc_visible"]
    )
    approval = await service.store.create_approval(_approval(run_id))
    body = _plan_body()
    body["tasks"][0]["params"] = {
        "profile": "standard",
        "collection_ids": ["col-forbidden"],
    }

    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=body,
            note="",
            principal=PRINCIPAL,
        )

    assert knowledge.calls == 1
    assert any(
        "col-forbidden" in error for error in exc_info.value.errors
    )
    # Nothing was committed: the approval stays pending, no plan version.
    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    with pytest.raises(PlanNotFound):
        await service.store.get_plan(run_id)


@pytest.mark.asyncio
async def test_edited_plan_canonicalizes_collection_names(
    run_store: RunStore, identity: MemoryIdentityStore
) -> None:
    """A user edit naming a collection ("EU-AI-Act") saves the plan with
    the canonical id — the exact reference shape retrieval expects, so
    the approved plan can never fail as a raw unknown-collection error."""

    class CatalogKnowledge:
        async def list_collections(self, *, visible_to=None):
            return [
                SimpleNamespace(id="kc_18d4", name="EU-AI-Act"),
            ]

    service = AgentControlService(
        store=MemoryAgentControlStore(),
        run_store=run_store,
        audit=identity,
        knowledge=CatalogKnowledge(),
        durable=False,
    )
    run_id = _parked_agent_run(
        run_store, knowledge_collection_ids=["kc_18d4"]
    )
    approval = await service.store.create_approval(_approval(run_id))
    body = _plan_body()
    body["tasks"][0]["params"] = {
        "profile": "standard",
        "collection_ids": ["EU-AI-Act"],
    }

    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=body,
        note="",
        principal=PRINCIPAL,
    )

    _plan, tasks = await service.store.get_plan(run_id)
    assert tasks[0].params["collection_ids"] == ["kc_18d4"]


@pytest.mark.asyncio
async def test_edited_plan_cannot_expand_parked_run_collection_scope(
    run_store: RunStore, identity: MemoryIdentityStore
) -> None:
    """A late share is visible to the editor but outside the admitted run."""

    class CatalogKnowledge:
        def __init__(self) -> None:
            self.calls = 0

        async def list_collections(self, *, visible_to=None):
            self.calls += 1
            return [
                SimpleNamespace(id="kc_initial", name="Initial"),
                SimpleNamespace(id="kc_late", name="Late share"),
            ]

    knowledge = CatalogKnowledge()
    service = AgentControlService(
        store=MemoryAgentControlStore(),
        run_store=run_store,
        audit=identity,
        knowledge=knowledge,
        durable=False,
    )
    run_id = _parked_agent_run(
        run_store, knowledge_collection_ids=["kc_initial"]
    )
    approval = await service.store.create_approval(_approval(run_id))
    body = _plan_body()
    body["tasks"][0]["params"] = {
        "profile": "standard",
        "collection_ids": ["kc_late"],
    }

    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=body,
            note="",
            principal=PRINCIPAL,
        )

    assert knowledge.calls == 1
    assert any("kc_late" in error for error in exc_info.value.errors)
    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    with pytest.raises(PlanNotFound):
        await service.store.get_plan(run_id)


@pytest.mark.asyncio
async def test_edit_requires_plan_kind_and_plan_body(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    discovery = await service.store.create_approval(
        _approval(run_id, kind="discovery")
    )
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=discovery.approval_id,
            decision="edit",
            plan_body=_plan_body(),
            note="",
            principal=PRINCIPAL,
        )
    plan_approval = await service.store.create_approval(_approval(run_id))
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=plan_approval.approval_id,
            decision="edit",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=plan_approval.approval_id,
            decision="approve",
            plan_body=_plan_body(),
            note="",
            principal=PRINCIPAL,
        )


# -- tool approvals (kernel policy gates) --------------------------------- #


def _tool_approval(run_id: str) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=f"apr_{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        kind="tool",
        payload={
            "actions": [
                {
                    "tool": "web_instant",
                    "args": {"query": "EU AI Act Fristen"},
                    "summary": "Websuche",
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_tool_edit_replaces_args_and_resumes(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))

    decided, summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        actions_body=[
            {"tool": "web_instant", "args": {"query": "EU AI Act 2027"}}
        ],
    )

    assert not replayed
    assert decided.status == "edited"
    assert decided.decision_payload == {
        "actions": [
            {"tool": "web_instant", "args": {"query": "EU AI Act 2027"}}
        ]
    }
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )


@pytest.mark.asyncio
async def test_tool_edit_validation_matrix(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))

    # Tool swap is not an edit — the gate approved THIS tool.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "rag_query", "args": {"query": "x"}}],
        )
    # Exactly one action, args must be an object.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            actions_body=[],
        )
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "web_instant", "args": "nope"}],
        )
    # A plan body belongs to plan approvals, actions to tool approvals.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=_plan_body(),
            note="",
            principal=PRINCIPAL,
        )
    plan_approval = await service.store.create_approval(_approval(run_id))
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=plan_approval.approval_id,
            decision="edit",
            plan_body=_plan_body(),
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "web_instant", "args": {}}],
        )
    # actions without decision=edit is a 400, mirroring the plan rule.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "web_instant", "args": {}}],
        )


@pytest.mark.asyncio
async def test_tool_edit_replay_same_actions_is_idempotent(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))
    edited = [{"tool": "web_instant", "args": {"query": "praeziser"}}]

    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        actions_body=edited,
    )
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )

    replay, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        actions_body=edited,
    )
    assert replayed and replay.status == "edited"

    with pytest.raises(ApprovalAlreadyDecided):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "web_instant", "args": {"query": "anders"}}],
        )


# -- clarifications ------------------------------------------------------- #


@pytest.mark.asyncio
async def test_clarification_answer_resumes_and_validates_options(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    clarification = await service.store.create_clarification(
        ClarificationRecord(
            clarification_id=f"clr_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            question="Welcher Zeitraum?",
            options=(
                {"id": "q1", "label": "Q1"},
                {"id": "q2", "label": "Q2"},
            ),
            default_assumption="Q1",
        )
    )

    with pytest.raises(AgentControlValidationError):
        await service.answer_clarification(
            run_id=run_id,
            clarification_id=clarification.clarification_id,
            answer="Q1",
            option_id="q1",
            principal=PRINCIPAL,
        )
    with pytest.raises(AgentControlValidationError):
        await service.answer_clarification(
            run_id=run_id,
            clarification_id=clarification.clarification_id,
            answer=None,
            option_id="ghost",
            principal=PRINCIPAL,
        )

    answered, summary, replayed = await service.answer_clarification(
        run_id=run_id,
        clarification_id=clarification.clarification_id,
        answer=None,
        option_id="q2",
        principal=PRINCIPAL,
    )
    assert not replayed
    assert answered.status == "answered"
    assert answered.option_id == "q2"
    assert summary["status"] in {"queued", "running", "completed"}
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )

    # Replay of the same answer is idempotent.
    _answered, _summary, replayed = await service.answer_clarification(
        run_id=run_id,
        clarification_id=clarification.clarification_id,
        answer=None,
        option_id="q2",
        principal=PRINCIPAL,
    )
    assert replayed


@pytest.mark.asyncio
async def test_failed_clarification_resume_emits_no_answered_signal(
    service: AgentControlService, run_store: RunStore
) -> None:
    """Signals follow the composed row+resume commit, never precede it."""
    run_id = _parked_agent_run(run_store)
    clarification = await service.store.create_clarification(
        ClarificationRecord(
            clarification_id="clr-failed-resume",
            run_id=run_id,
            question="Welcher Zeitraum?",
        )
    )
    with run_store._lock:
        run_store._records[run_id].work = None

    with pytest.raises(RunActive):
        await service.answer_clarification(
            run_id=run_id,
            clarification_id=clarification.clarification_id,
            answer="Q1",
            option_id=None,
            principal=PRINCIPAL,
        )

    stored = await service.store.get_clarification(
        run_id, clarification.clarification_id
    )
    assert stored.status == "pending"
    subscription = run_store.subscribe(run_id, visible_to=VISIBLE)
    try:
        assert all(
            event["type"] != "inqtrix.agent.clarification.answered"
            for event in subscription.replay
        )
    finally:
        subscription.close()


# -- artifacts -------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_runtime_control_write_revalidates_effective_actor_after_revoke(
    run_store: RunStore, identity: MemoryIdentityStore
) -> None:
    """A stale tool segment cannot persist canvas/control state after revoke."""
    editor_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    control = MemoryAgentControlStore()
    await _bind_scoped_control_authority(
        run_store=run_store,
        identity=identity,
        control=control,
        user_ids=(PRINCIPAL.user_id, editor_id),
    )
    calls = {"count": 0}
    resumed = threading.Event()
    release = threading.Event()

    def segmented(handle: RunHandle) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            handle.wait("waiting_for_approval")
            return
        resumed.set()
        assert release.wait(timeout=5.0)
        handle.complete({"answer": "done"})

    summary = run_store.submit(
        question="Agentenauftrag",
        stack_name="default",
        work=segmented,
        kind="agent",
        session_id="sess-revoked-runtime-write",
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id="default",
    )
    run_id = summary["run_id"]
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "waiting_for_approval"
    )
    identity.add_share(
        recipient_user_id=editor_id,
        resource_type="run",
        resource_id=run_id,
        permission=SharePermission.EDIT,
        granted_by_user_id=PRINCIPAL.user_id,
    )
    try:
        run_store.resume_run(
            run_id,
            actor_user_id=editor_id,
            execution_scopes=frozenset({"agent:write"}),
        )
        assert await asyncio.to_thread(resumed.wait, 2.0)
        identity.revoke_share(
            recipient_user_id=editor_id,
            resource_type="run",
            resource_id=run_id,
        )

        with pytest.raises(AuthorizationRevoked):
            await control.upsert_artifact(
                run_id=run_id,
                kind="memo",
                session_id="sess-revoked-runtime-write",
                title="Forbidden",
                status="writing",
                content_markdown="must not land",
                payload={},
                refs=[],
                updated_by="agent",
                artifact_id="art-revoked-runtime-write",
            )
        with pytest.raises(ArtifactNotFound):
            await control.get_artifact(
                run_id, "art-revoked-runtime-write"
            )
    finally:
        release.set()
        _wait_until(
            lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
            == "failed"
        )


@pytest.mark.asyncio
async def test_sessionless_deliverables_are_not_implicit_singletons(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    first = await service.store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id=None,
        title="One",
        status="ready",
        content_markdown="# One",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_sessionless_one",
    )
    second = await service.store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id=None,
        title="Two",
        status="ready",
        content_markdown="# Two",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_sessionless_two",
    )

    assert first.artifact_id != second.artifact_id
    rows, _cursor = await service.store.list_artifacts(
        run_id, kind="deliverable", limit=10
    )
    assert {row.artifact_id for row in rows} == {
        first.artifact_id,
        second.artifact_id,
    }


@pytest.mark.asyncio
async def test_artifact_upsert_versioning_and_user_edit_matrix(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    store = service.store
    first = await store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-1",
        title="Memo",
        status="ready",
        content_markdown="# V1",
        payload={},
        refs=[{"label": "K1"}],
        updated_by="agent",
        artifact_id="art_memo",
    )
    assert first.revision == 1
    # Session upsert re-anchors to the same artifact, bumping revision.
    second = await store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-1",
        title="Memo",
        status="ready",
        content_markdown="# V2",
        payload={},
        refs=[],
        updated_by="agent",
    )
    assert second.artifact_id == "art_memo"
    assert second.revision == 2

    with pytest.raises(ArtifactRevisionConflict) as conflict:
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art_memo",
            content_markdown="# stale",
            expected_revision=1,
            principal=PRINCIPAL,
        )
    assert conflict.value.current_revision == 2

    edited = await service.user_update_artifact(
        run_id=run_id,
        artifact_id="art_memo",
        content_markdown="# V3 vom Nutzer",
        expected_revision=2,
        principal=PRINCIPAL,
    )
    assert edited.revision == 3
    assert edited.updated_by == "user"

    # Writing status locks user edits.
    await store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-1",
        title="Memo",
        status="writing",
        content_markdown="# V4 agent schreibt",
        payload={},
        refs=[],
        updated_by="agent",
    )
    with pytest.raises(ArtifactLocked):
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art_memo",
            content_markdown="# waehrend writing",
            expected_revision=4,
            principal=PRINCIPAL,
        )
    with pytest.raises(ArtifactNotFound):
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art_unbekannt",
            content_markdown="x",
            expected_revision=1,
            principal=PRINCIPAL,
        )

    artifact, revisions = await store.get_artifact(run_id, "art_memo")
    assert artifact.revision == 4
    assert [row.revision for row in revisions] == [4, 3, 2, 1]
    assert all(row.content_markdown == "" for row in revisions)
    old, _ = await store.get_artifact(run_id, "art_memo", revision=2)
    assert old.content_markdown == "# V2"
    with pytest.raises(ArtifactNotFound):
        await store.get_artifact(run_id, "art_memo", revision=99)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_late_user_artifact_changes_preserve_audit_without_emitting_to_closed_run(
    service, run_store, identity, monkeypatch, caplog, terminal_status,
):
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id, kind="deliverable", session_id="sess-1", title="Original",
        status="ready", content_markdown="Original body", payload={}, refs=[],
        updated_by="agent", artifact_id="late-artifact",
    )
    if terminal_status == "completed":
        run_store.complete(run_id, {"answer": "Finished"})
    elif terminal_status == "failed":
        run_store.fail(run_id, "Stopped")
    else:
        run_store.cancel(run_id, visible_to=VISIBLE)
    before = run_store.subscribe(run_id, visible_to=VISIBLE, stream=False).replay
    emitted = []
    original_emit = run_store.emit
    def emit(*args, **kwargs):
        emitted.append(args)
        return original_emit(*args, **kwargs)
    monkeypatch.setattr(run_store, "emit", emit)
    caplog.clear()
    edited = await service.user_update_artifact(
        run_id=run_id, artifact_id="late-artifact", content_markdown="Edited body",
        expected_revision=1, principal=PRINCIPAL, visible_to=VISIBLE,
    )
    renamed = await service.rename_artifact(
        run_id=run_id, artifact_id="late-artifact", title="Renamed",
        principal=PRINCIPAL, visible_to=VISIBLE,
    )
    assert edited.revision == renamed.revision == 2
    stored, revisions = await service.store.get_artifact(run_id, "late-artifact")
    assert (stored.title, stored.content_markdown) == ("Renamed", "Edited body")
    assert len(revisions) == 2
    assert emitted == []
    assert run_store.subscribe(run_id, visible_to=VISIBLE, stream=False).replay == before
    assert [entry.action for entry in identity.audit_entries][-2:] == [
        "agent.artifact_edited", "agent.artifact_renamed",
    ]
    assert not [r for r in caplog.records if r.levelname in {"WARNING", "ERROR"}]


@pytest.mark.asyncio
async def test_live_user_artifact_updates_keep_scoped_notifications(service, run_store, monkeypatch):
    from unittest.mock import AsyncMock
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id, kind="deliverable", session_id="sess-1", title="Original",
        status="ready", content_markdown="Original body", payload={}, refs=[],
        updated_by="agent", artifact_id="live-artifact",
    )
    read = AsyncMock(wraps=service._run_summary)
    monkeypatch.setattr(service, "_run_summary", read)
    await service.user_update_artifact(
        run_id=run_id, artifact_id="live-artifact", content_markdown="Edited body",
        expected_revision=1, principal=PRINCIPAL, visible_to=VISIBLE,
    )
    await service.rename_artifact(
        run_id=run_id, artifact_id="live-artifact", title="Renamed",
        principal=PRINCIPAL, visible_to=VISIBLE,
    )
    events = run_store.subscribe(run_id, visible_to=VISIBLE, stream=False).replay
    changes = [e for e in events if e["type"] == "inqtrix.agent.artifact.updated"]
    assert len(changes) == 2
    assert changes[0]["data"]["revision"] == 2
    assert changes[1]["data"]["renamed"] is True
    assert all(call.kwargs == {"principal": PRINCIPAL, "visible_to": VISIBLE} for call in read.call_args_list)
    assert read.call_count == 2


@pytest.mark.asyncio
async def test_notification_status_failure_does_not_undo_a_user_edit(service, run_store, identity, monkeypatch, caplog):
    from unittest.mock import AsyncMock
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id, kind="deliverable", session_id="sess-1", title="Original",
        status="ready", content_markdown="Original body", payload={}, refs=[],
        updated_by="agent", artifact_id="status-artifact",
    )
    monkeypatch.setattr(service, "_run_summary", AsyncMock(side_effect=RuntimeError("status unavailable")))
    edited = await service.user_update_artifact(
        run_id=run_id, artifact_id="status-artifact", content_markdown="Edited body",
        expected_revision=1, principal=PRINCIPAL, visible_to=VISIBLE,
    )
    assert edited.revision == 2
    assert (await service.store.get_artifact(run_id, "status-artifact"))[0].content_markdown == "Edited body"
    assert identity.audit_entries[-1].action == "agent.artifact_edited"
    assert any("could not be read for the artifact notification" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_rename_artifact_is_metadata_only(
    service: AgentControlService, run_store: RunStore
) -> None:
    """A rename changes title + updated_at and NOTHING else — no revision
    bump, no revision row, so the content history never records it and no
    change badge can appear for it."""
    run_id = _parked_agent_run(run_store)
    store = service.store
    await store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-1",
        title="Alter Titel",
        status="ready",
        content_markdown="# Inhalt",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_doc",
    )
    renamed = await service.rename_artifact(
        run_id=run_id,
        artifact_id="art_doc",
        title="Neuer Titel",
        principal=PRINCIPAL,
    )
    assert renamed.title == "Neuer Titel"
    assert renamed.revision == 1
    row, revisions = await store.get_artifact(run_id, "art_doc")
    assert row.title == "Neuer Titel"
    assert row.revision == 1
    assert len(revisions) == 1
    assert row.content_markdown == "# Inhalt"

    with pytest.raises(ArtifactNotFound):
        await service.rename_artifact(
            run_id=run_id,
            artifact_id="art_unbekannt",
            title="X",
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_rename_is_locked_while_the_agent_writes(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id,
        kind="deliverable",
        session_id="sess-1",
        title="Titel",
        status="writing",
        content_markdown="# schreibt",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_doc",
    )
    with pytest.raises(ArtifactLocked):
        await service.rename_artifact(
            run_id=run_id,
            artifact_id="art_doc",
            title="Neu",
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_artifact_edit_and_revoke_have_one_memory_linearization_order(
    service: AgentControlService,
    run_store: RunStore,
    identity: MemoryIdentityStore,
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id="sess-revoke-race",
        title="Memo",
        status="ready",
        content_markdown="# V1",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art-revoke-race",
    )
    recipient_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    recipient_principal = Principal(
        user_id=recipient_id,
        kind="oidc_session",
        tenant_id="default",
        role="member",
    )
    recipient = UserContext(principal=recipient_principal)
    identity.add_share(
        recipient_user_id=recipient_id,
        resource_type="run",
        resource_id=run_id,
        permission=SharePermission.EDIT,
        granted_by_user_id=PRINCIPAL.user_id,
    )
    authority_entered = threading.Event()
    release_write = threading.Event()

    @contextmanager
    def blocking_guard(**kwargs: Any) -> Iterator[None]:
        with identity.resource_access_guard_sync(**kwargs):
            if kwargs["actor_user_id"] == recipient_id:
                authority_entered.set()
                assert release_write.wait(timeout=5.0)
            yield

    run_store.bind_authorization(
        share_lookup=identity.permission_for_sync,
        share_workspace_check=identity.share_workspace_sync,
        resource_access_guard=blocking_guard,
        restrict_to_workspace_members=False,
    )
    edit = asyncio.create_task(
        service.user_update_artifact(
            run_id=run_id,
            artifact_id="art-revoke-race",
            content_markdown="# V2",
            expected_revision=1,
            principal=recipient_principal,
            visible_to=recipient,
        )
    )
    assert await asyncio.to_thread(authority_entered.wait, 2.0)
    revoke_started = threading.Event()

    def revoke() -> None:
        revoke_started.set()
        identity.revoke_share(
            recipient_user_id=recipient_id,
            resource_type="run",
            resource_id=run_id,
        )

    revoke_task = asyncio.create_task(asyncio.to_thread(revoke))
    assert await asyncio.to_thread(revoke_started.wait, 2.0)
    release_write.set()
    edited, _ = await asyncio.gather(edit, revoke_task)

    assert edited.revision == 2
    with pytest.raises(RunNotFound):
        await service.user_update_artifact(
            run_id=run_id,
            artifact_id="art-revoke-race",
            content_markdown="# forbidden",
            expected_revision=2,
            principal=recipient_principal,
            visible_to=recipient,
        )
    stored, _revisions = await service.store.get_artifact(
        run_id, "art-revoke-race"
    )
    assert stored.revision == 2
    assert stored.content_markdown == "# V2"


@pytest.mark.asyncio
async def test_session_memo_lineage_read_and_agent_cas(
    service: AgentControlService, run_store: RunStore
) -> None:
    """Cross-run session read + agent-side CAS on the memo artifact.

    Turn 1 writes the session memo; the user then edits it. A follow-up
    turn (a DIFFERENT run) must READ that latest user revision by session
    (get_session_artifact, run-agnostic) and its guarded write must REFUSE
    to advance from the pre-edit revision (ArtifactRevisionConflict) while
    succeeding from the current one.
    """
    store = service.store
    run1 = _parked_agent_run(run_store)
    await store.upsert_artifact(
        run_id=run1,
        kind="memo",
        session_id="sess-lin",
        title="Memo",
        status="ready",
        content_markdown="# Turn 1",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_lin_memo",
    )
    edited = await service.user_update_artifact(
        run_id=run1,
        artifact_id="art_lin_memo",
        content_markdown="# Turn 1 mit Nutzer-Edit",
        expected_revision=1,
        principal=PRINCIPAL,
    )
    assert edited.revision == 2

    # The follow-up run cannot see the memo run-scoped (different run)...
    run_store.cancel(run1, visible_to=VISIBLE)
    assert run_store.get(run1, visible_to=VISIBLE)["status"] == "cancelled"
    run2 = _parked_agent_run(run_store)
    with pytest.raises(ArtifactNotFound):
        await store.get_artifact(run2, "art_lin_memo")
    # ...but the session read reaches the LATEST user revision.
    prior = await store.get_session_artifact("sess-lin", "memo")
    assert prior is not None
    assert prior.revision == 2
    assert prior.content_markdown == "# Turn 1 mit Nutzer-Edit"

    # A guarded write from the STALE revision the agent might have cached
    # is refused instead of clobbering the user's edit...
    with pytest.raises(ArtifactRevisionConflict) as conflict:
        await store.upsert_artifact(
            run_id=run2,
            kind="memo",
            session_id="sess-lin",
            title="Memo",
            status="writing",
            content_markdown="# Turn 2 (stale base)",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_lin_memo",
            expected_revision=1,
        )
    assert conflict.value.current_revision == 2
    # ...and succeeds from the revision the agent actually read.
    advanced = await store.upsert_artifact(
        run_id=run2,
        kind="memo",
        session_id="sess-lin",
        title="Memo",
        status="writing",
        content_markdown="# Turn 2 (fortgesetzt)",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_lin_memo",
        expected_revision=2,
    )
    assert advanced.revision == 3
    # An unguarded write (expected_revision=None) stays a blind bump — the
    # write-once per-run kinds (evidence, critic) rely on that default.
    blind = await store.upsert_artifact(
        run_id=run2,
        kind="memo",
        session_id="sess-lin",
        title="Memo",
        status="ready",
        content_markdown="# Turn 2 final",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_lin_memo",
    )
    assert blind.revision == 4
    # expected_revision=0 means "expect to create": a row that already
    # exists is a conflict, never a silent clobber (fresh-session race).
    with pytest.raises(ArtifactRevisionConflict) as insert_conflict:
        await store.upsert_artifact(
            run_id=run2,
            kind="memo",
            session_id="sess-lin",
            title="Memo",
            status="ready",
            content_markdown="# would clobber",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id="art_lin_memo",
            expected_revision=0,
        )
    assert insert_conflict.value.current_revision == 4


@pytest.mark.asyncio
async def test_artifact_listing_paginates_and_filters(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    store = service.store
    for index in range(3):
        await store.upsert_artifact(
            run_id=run_id,
            kind="evidence_bundle" if index else "memo",
            session_id=None,
            title=f"A{index}",
            status="ready",
            content_markdown="body",
            payload={},
            refs=[],
            updated_by="agent",
            artifact_id=f"art_{index}",
        )
        await asyncio.sleep(0.01)

    page, cursor = await store.list_artifacts(run_id, limit=2)
    assert len(page) == 2
    assert cursor is not None
    assert all(row.content_markdown == "" for row in page)
    from inqtrix.pagination import decode_cursor

    rest, cursor2 = await store.list_artifacts(
        run_id, limit=2, after=decode_cursor(cursor)
    )
    assert len(rest) == 1
    assert cursor2 is None
    memos, _ = await store.list_artifacts(run_id, kind="memo", limit=10)
    assert [row.artifact_id for row in memos] == ["art_0"]


@pytest.mark.asyncio
async def test_export_without_editor_persistence_fails_loudly(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id,
        kind="memo",
        session_id=None,
        title="Memo",
        status="ready",
        content_markdown="# Inhalt",
        payload={},
        refs=[],
        updated_by="agent",
        artifact_id="art_memo",
    )
    with pytest.raises(AgentControlUnavailable):
        await service.export_artifact(
            run_id=run_id,
            artifact_id="art_memo",
            title=None,
            folder_id=None,
            principal=PRINCIPAL,
            caller_user_id=PRINCIPAL.user_id,
            workspace_id=None,
        )


@pytest.mark.asyncio
async def test_concurrent_decides_yield_exactly_one_resume(
    service: AgentControlService, run_store: RunStore
) -> None:
    """Two racing decisions: one wins, the other replays or conflicts."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    results: list[Any] = []
    errors: list[BaseException] = []

    async def _decide() -> None:
        try:
            results.append(
                await service.decide_approval(
                    run_id=run_id,
                    approval_id=approval.approval_id,
                    decision="approve",
                    plan_body=None,
                    note="",
                    principal=PRINCIPAL,
                )
            )
        except BaseException as exc:  # noqa: BLE001 — collected below
            errors.append(exc)

    def _run_decide() -> None:
        asyncio.run(_decide())

    threads = [threading.Thread(target=_run_decide) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors, errors
    assert len(results) == 2
    # Exactly one actually decided; the other took the replay path.
    assert sorted(result[2] for result in results) == [False, True]
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )


@pytest.mark.asyncio
async def test_edit_replay_with_different_plan_conflicts(
    service: AgentControlService, run_store: RunStore
) -> None:
    """The plan payload IS the edit decision: a retry with a DIFFERENT
    plan must 409, never be swallowed as an idempotent replay."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_approval(run_id))
    await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=_plan_body(),
        note="",
        principal=PRINCIPAL,
    )

    # Same verb, same plan -> idempotent replay.
    _decided, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="edit",
        plan_body=_plan_body(),
        note="",
        principal=PRINCIPAL,
    )
    assert replayed

    different = _plan_body()
    different["summary_markdown"] = "Ein ganz anderer Plan"
    with pytest.raises(ApprovalAlreadyDecided):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=different,
            note="",
            principal=PRINCIPAL,
        )
    # The differing plan was NOT applied: still exactly one version.
    _plan, _tasks, versions = await service.get_plan(run_id)
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_failed_edit_resume_restores_prior_plan_status(
    service: AgentControlService, run_store: RunStore
) -> None:
    """Memory revert must undo the supersede flip too (Postgres gets it
    from the transaction rollback — lockstep on the failure path)."""
    summary = run_store.submit(
        question="normal",
        stack_name="default",
        work=lambda handle: handle.complete({}),
        kind="agent",
        created_by_user_id=PRINCIPAL.user_id,
        created_by_tenant_id=PRINCIPAL.tenant_id,
    )
    run_id = summary["run_id"]
    _wait_until(
        lambda: run_store.get(run_id, visible_to=VISIBLE)["status"]
        == "completed"
    )
    await service.store.save_plan(
        run_id=run_id,
        plan=PlanRecord(
            plan_id="plan_v1",
            run_id=run_id,
            version=1,
            status="proposed",
            created_by="agent",
        ),
        tasks=[],
    )
    approval = await service.store.create_approval(_approval(run_id))

    with pytest.raises(RunActive):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            plan_body=_plan_body(),
            note="",
            principal=PRINCIPAL,
        )

    stored = await service.store.get_approval(run_id, approval.approval_id)
    assert stored.status == "pending"
    plan, _tasks, versions = await service.get_plan(run_id)
    assert plan.version == 1
    assert plan.status == "proposed"
    assert len(versions) == 1


@pytest.mark.asyncio
async def test_terminal_run_releases_canvas_and_settles_approvals(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    """A dead run leaves no locked canvas and no live-looking approval."""
    run_id = _parked_agent_run(run_store)
    artifact = await service.store.upsert_artifact(
        run_id=run_id,
        artifact_id="art_settle_memo",
        kind="memo",
        session_id="sess-settle",
        title="Memo",
        status="writing",
        content_markdown="# Gestreamter Stand",
        payload={},
        refs=[],
        updated_by="agent",
    )
    approval = await service.store.create_approval(_approval(run_id))
    clarification = await service.store.create_clarification(
        ClarificationRecord(
            clarification_id=f"clr_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            question="Welcher Zeitraum?",
            options=(
                {"id": "q1", "label": "Q1"},
                {"id": "q2", "label": "Q2"},
            ),
            default_assumption="Q1",
        )
    )
    assert run_store.cancel(run_id, visible_to=VISIBLE)["status"] == "cancelled"

    assert await service.reconcile_terminal_tasks(run_id) is True

    released, revisions = await service.store.get_artifact(
        run_id, artifact.artifact_id
    )
    del revisions
    assert released.status == "ready"
    assert released.revision == artifact.revision, (
        "release must not fabricate a revision nobody authored"
    )
    assert released.content_markdown == "# Gestreamter Stand"

    settled = await service.store.get_approval(run_id, approval.approval_id)
    assert settled.status == "rejected"
    assert settled.decision == "", (
        "system settlement must stay distinguishable from a user reject"
    )
    assert "endete" in settled.note
    assert settled.decided_by_user_id is None

    rounds = await service.store.list_clarifications(run_id)
    kept = [
        row for row in rounds
        if row.clarification_id == clarification.clarification_id
    ]
    assert kept and kept[0].status == "pending", (
        "unanswered rounds stay pending by contract — truthful and inert"
    )

    # Second pass is a no-op pair.
    assert await service.store.settle_terminal_control_rows(run_id) == (0, 0)

    # The canvas is editable again.
    edited = await service.user_update_artifact(
        run_id=run_id,
        artifact_id=artifact.artifact_id,
        content_markdown="# Wieder editierbar",
        expected_revision=released.revision,
        principal=PRINCIPAL,
    )
    assert edited.updated_by == "user"


@pytest.mark.asyncio
async def test_failed_run_settles_control_rows_too(
    service: AgentControlService,
    run_store: RunStore,
) -> None:
    """The failed branch of the reconcile settles exactly like cancelled."""
    run_id = _parked_agent_run(run_store)
    await service.store.upsert_artifact(
        run_id=run_id,
        artifact_id="art_settle_failed",
        kind="memo",
        session_id="sess-settle-failed",
        title="Memo",
        status="writing",
        content_markdown="# Stand",
        payload={},
        refs=[],
        updated_by="agent",
    )
    run_store.fail(run_id, "kaputt", error_type="server_error")
    assert run_store.get(run_id, visible_to=VISIBLE)["status"] == "failed"

    assert await service.reconcile_terminal_tasks(run_id) is True

    released, _revisions = await service.store.get_artifact(
        run_id, "art_settle_failed"
    )
    assert released.status == "ready"


# -- approval_scope (run-wide tool grants) ---------------------------------- #


@pytest.mark.asyncio
async def test_approval_scope_run_rides_decision_payload_and_replays(
    service: AgentControlService, run_store: RunStore
) -> None:
    """approve+run stores {"approval_scope": "run"}; the SAME retry
    replays 200, a retry with a DIFFERENT scope conflicts — the payload
    IS replay identity, exactly like an edited plan."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))

    decided, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        approval_scope="run",
    )
    assert not replayed
    assert decided.decision_payload == {"approval_scope": "run"}

    again, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        approval_scope="run",
    )
    assert replayed
    assert again.decision_payload == {"approval_scope": "run"}

    with pytest.raises(ApprovalAlreadyDecided):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_approval_scope_once_stores_nothing(
    service: AgentControlService, run_store: RunStore
) -> None:
    """"once" keeps the plain-approve replay identity: a later plain
    approve retry replays 200."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))

    decided, _summary, _replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
        approval_scope="once",
    )
    assert decided.decision_payload == {}

    _again, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
        plan_body=None,
        note="",
        principal=PRINCIPAL,
    )
    assert replayed


@pytest.mark.asyncio
async def test_approval_scope_validation_matrix(
    service: AgentControlService, run_store: RunStore
) -> None:
    run_id = _parked_agent_run(run_store)

    # Unknown scope value.
    tool_gate = await service.store.create_approval(_tool_approval(run_id))
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=tool_gate.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            approval_scope="forever",
        )
    assert "Unknown approval_scope" in str(exc_info.value)

    # Scope on a non-approve decision.
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=tool_gate.approval_id,
            decision="reject",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert "only for decision=approve" in str(exc_info.value)

    # Scope on a plan gate.
    plan_gate = await service.store.create_approval(_approval(run_id))
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=plan_gate.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert "only for tool approvals" in str(exc_info.value)

    # A run grant must never cover an always-gated tool.
    patch_gate = await service.store.create_approval(
        ApprovalRecord(
            approval_id=f"apr_{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            kind="tool",
            payload={
                "actions": [
                    {
                        "tool": "propose_editor_patch",
                        "args": {"instruction": "x"},
                        "summary": "Patch",
                    }
                ]
            },
        )
    )
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=patch_gate.approval_id,
            decision="approve",
            plan_body=None,
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert "propose_editor_patch" in str(exc_info.value)
    assert "for each call" in str(exc_info.value)


def test_session_memo_artifact_id_is_the_one_definition():
    """Mission flush and kernel hand-over derive the session memo id
    from the same rule (``uq_run_artifacts_session_memo``): the last
    twelve characters of the session id, or of the run id without one."""
    assert session_memo_artifact_id("sess_0123456789abcdef", "run_x") == (
        "art_456789abcdef_memo"
    )
    assert session_memo_artifact_id(None, "run_0123456789abcdef") == (
        "art_456789abcdef_memo"
    )
    assert session_memo_artifact_id("", "run_short") == "art_run_short_memo"


@pytest.mark.asyncio
async def test_revision_snapshots_carry_the_write_status(
    service: AgentControlService, run_store: RunStore
) -> None:
    """A memo flushed section by section must not list "Revision 22" in
    the reader's version picker, with fragments at 1 and 3. Each
    snapshot records the status its write carried — ``writing`` for
    a streaming flush, ``ready`` for a finished write or a user edit — so
    the picker can skip the streaming ones."""
    run_id = _parked_agent_run(run_store)
    store = service.store
    common = dict(
        run_id=run_id, kind="memo", session_id="sess-rev", title="Memo",
        payload={}, refs=[], updated_by="agent", artifact_id="art_rev",
    )
    await store.upsert_artifact(status="writing", content_markdown="# 1", **common)
    await store.upsert_artifact(status="writing", content_markdown="# 1\n\n## 2", **common)
    await store.upsert_artifact(status="ready", content_markdown="# 1\n\n## 2\n\n## 3", **common)
    await service.user_update_artifact(
        run_id=run_id, artifact_id="art_rev", content_markdown="# user",
        expected_revision=3, principal=PRINCIPAL,
    )
    _artifact, revisions = await store.get_artifact(run_id, "art_rev")
    assert [(row.revision, row.status, row.created_by) for row in revisions] == [
        (4, "ready", "user"),
        (3, "ready", "agent"),
        (2, "writing", "agent"),
        (1, "writing", "agent"),
    ]
