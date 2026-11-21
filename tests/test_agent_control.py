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
from typing import Any, Callable

import pytest

from inqtrix.agents.control_memory import MemoryAgentControlStore
from inqtrix.agents.control_ports import (
    ApprovalAlreadyDecided,
    ApprovalRecord,
    ArtifactLocked,
    ArtifactNotFound,
    ArtifactRevisionConflict,
    ClarificationRecord,
)
from inqtrix.auth.directory import MemoryUserDirectory
from inqtrix.auth.identity_memory import MemoryIdentityStore
from inqtrix.auth.memory_authority import MemoryAuthorityCoordinator
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.exceptions import RunNotFound
from inqtrix.runs.memory_store import RunStore
from inqtrix.runs.records import RunActive, RunHandle
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
        mode="research",
        workflow_contract=1,
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


def _approval(run_id: str, *, kind: str = "tool") -> ApprovalRecord:
    """A pending approval of *kind*; by default the kernel's tool gate."""
    if kind == "tool":
        return _tool_approval(run_id)
    return ApprovalRecord(
        approval_id=f"apr_{uuid.uuid4().hex[:8]}",
        run_id=run_id,
        kind=kind,
    )


# -- approval decision state machine ------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["discovery", "plan", "patch", "replan"])
async def test_only_a_tool_approval_is_created(kind: str) -> None:
    """The kernel's tool gate is the one approval a run still asks for; a
    stored approval of the retired kinds is read, never created anew."""
    store = MemoryAgentControlStore()

    with pytest.raises(ValueError) as refused:
        await store.create_approval(
            ApprovalRecord(approval_id="apr_retired", run_id="run-kinds", kind=kind)
        )
    created = await store.create_approval(_approval("run-kinds"))

    assert str(refused.value) == (
        f"An approval of kind {kind} is not created any more; a run asks only for "
        "tool approvals."
    )
    assert [row.approval_id for row in await store.list_approvals("run-kinds")] == [
        created.approval_id
    ]


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
        note="",
        principal=PRINCIPAL,
    )

    decided, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="reject",
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
            note="",
            principal=PRINCIPAL,
        )


@pytest.mark.asyncio
async def test_decision_on_non_waiting_run_rolls_back(
    service: AgentControlService, run_store: RunStore
) -> None:
    summary = run_store.submit(
        mode="research",
        workflow_contract=1,
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


# -- tool approvals (kernel policy gates) --------------------------------- #


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
            note="",
            principal=PRINCIPAL,
            actions_body=[],
        )
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            note="",
            principal=PRINCIPAL,
            actions_body=[{"tool": "web_instant", "args": "nope"}],
        )
    # An edit without an actions body is a 400.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="edit",
            note="",
            principal=PRINCIPAL,
        )
    # actions without decision=edit is a 400.
    with pytest.raises(AgentControlValidationError):
        await service.decide_approval(
            run_id=run_id,
            approval_id=approval.approval_id,
            decision="approve",
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
        mode="research",
        workflow_contract=1,
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
    (get_session_artifact_by_id, run-agnostic) and its guarded write must REFUSE
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
    prior = await store.get_session_artifact_by_id("sess-lin", "art_lin_memo")
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

    assert await service.reconcile_terminal_control_rows(run_id) is True

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

    assert await service.reconcile_terminal_control_rows(run_id) is True

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
    IS replay identity, exactly like edited actions."""
    run_id = _parked_agent_run(run_store)
    approval = await service.store.create_approval(_tool_approval(run_id))

    decided, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
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
        note="",
        principal=PRINCIPAL,
        approval_scope="once",
    )
    assert decided.decision_payload == {}

    _again, _summary, replayed = await service.decide_approval(
        run_id=run_id,
        approval_id=approval.approval_id,
        decision="approve",
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
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert "only for decision=approve" in str(exc_info.value)

    # A stored plan gate of a mission run is not decided at all, with or
    # without a scope.
    plan_gate = ApprovalRecord(
        approval_id=f"apr_{uuid.uuid4().hex[:8]}", run_id=run_id, kind="plan"
    )
    with service.store._lock:
        service.store._approvals[plan_gate.approval_id] = plan_gate
    with pytest.raises(AgentControlValidationError) as exc_info:
        await service.decide_approval(
            run_id=run_id,
            approval_id=plan_gate.approval_id,
            decision="approve",
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert exc_info.value.reason == "workflow_retired"

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
            note="",
            principal=PRINCIPAL,
            approval_scope="run",
        )
    assert "propose_editor_patch" in str(exc_info.value)
    assert "for each call" in str(exc_info.value)


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
