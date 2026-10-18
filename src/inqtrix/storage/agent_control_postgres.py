"""Durable agent control store (Postgres tier).

Lockstep counterpart of
:class:`~inqtrix.agents.control_memory.MemoryAgentControlStore` — same
port, same error types, same ordering. HTTP-loop only (NullPool engine via
:class:`~inqtrix.project.base_session_store.BaseSessionStore`), with ONE
deliberate exception: the decision/answer writers built by
:meth:`decide_approval_and_resume` / :meth:`answer_clarification_and_resume`
run inside the RUN STORE's resume transaction on the run store's private
loop. Task-cancel and user-artifact writers use the same callback
shape for live run authorization. These writers therefore use only the
session they are handed, never ``self._session``.
"""

from __future__ import annotations

import uuid
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import case, exists, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from inqtrix.agents.artifact_lines import count_line_changes
from inqtrix.agents.control_ports import (
    TERMINAL_APPROVAL_SETTLE_NOTE,
    APPROVAL_STATUS_BY_DECISION,
    ApprovalAlreadyDecided,
    ApprovalNotFound,
    ApprovalRecord,
    ArtifactLocked,
    ContextArchiveImmutable,
    ArtifactBatchRevision,
    ArtifactNotFound,
    ArtifactPublicationFenced,
    ArtifactRecord,
    ArtifactRevisionConflict,
    ArtifactRevisionRecord,
    ClarificationAlreadyAnswered,
    ClarificationNotFound,
    ClarificationRecord,
    PlanNotFound,
    PlanRecord,
    PlanTaskNotFound,
    PlanTaskRecord,
    SESSION_SINGLETON_ARTIFACT_KINDS,
    RUN_SINGLETON_ARTIFACT_KINDS,
    transitioned_plan_task,
    requested_task_cancellation,
)
from inqtrix.auth.permissions import SharePermission
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.pagination import encode_cursor
from inqtrix.project.base_session_store import BaseSessionStore
from inqtrix.runs.durable_store import DEFAULT_TENANT
from inqtrix.storage.agent_control_orm import (
    run_approvals,
    run_artifact_revisions,
    run_artifacts,
    run_clarifications,
    run_plan_tasks,
    run_plans,
)
from inqtrix.storage.resource_access import lock_resource_access
from inqtrix.storage.runs_orm import runs
from inqtrix.agents.context_archive import (
    ARCHIVE_PAYLOAD_KEY, ARCHIVE_READ_WINDOW_CHARS, TRANSCRIPT_PAYLOAD_KEY,
    ArchiveCatalogueEntry, ArchiveIntegrityError, ArchiveWindow,
    catalogue_entry, is_private_archive, is_exact_archive, legacy_window, part_info,
    prepare_archive, validate_parts, window_bounds, window_from_parts,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

_ARTIFACT_META_COLUMNS = (
    run_artifacts.c.artifact_id,
    run_artifacts.c.tenant_id,
    run_artifacts.c.run_id,
    run_artifacts.c.session_id,
    run_artifacts.c.kind,
    run_artifacts.c.title,
    run_artifacts.c.status,
    run_artifacts.c.revision,
    run_artifacts.c.updated_by,
    run_artifacts.c.payload,
    run_artifacts.c.refs,
    run_artifacts.c.created_at,
    run_artifacts.c.updated_at,
)

_MUTABLE_ARTIFACT = ~(
    (run_artifacts.c.kind == "context_archive")
    & run_artifacts.c.payload[ARCHIVE_PAYLOAD_KEY].is_not(None)
)
_PUBLIC_ARTIFACT = run_artifacts.c.payload[TRANSCRIPT_PAYLOAD_KEY].is_(None)


class PostgresAgentControlStore(BaseSessionStore):
    """Postgres :class:`~inqtrix.agents.control_ports.AgentControlStore`."""

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
    ) -> None:
        super().__init__(engine=engine, app_role=app_role)
        self._restrict_to_workspace_members = restrict_to_workspace_members
        self._sharing_enabled = sharing_enabled

    async def _lock_runtime_authority(
        self, session: "AsyncSession", run_id: str
    ) -> None:
        """Lock the run and the effective actor's live edit authority.

        Agent-runtime control rows are part of the run's durable result. The
        actor pointer is sampled before the canonical user->resource->share
        lock order, then re-read after the run row lock lands. A concurrent
        handoff aborts this old segment instead of writing under either
        actor's stale authority.
        """
        actor_user_id = (
            await session.execute(
                select(runs.c.execution_actor_user_id).where(
                    runs.c.run_id == run_id
                )
            )
        ).scalar_one_or_none()
        access = await lock_resource_access(
            session,
            tenant_id=DEFAULT_TENANT,
            actor_user_id=actor_user_id,
            resource_type="run",
            resource_table=runs,
            id_column=runs.c.run_id,
            resource_id=run_id,
            owner_column=runs.c.created_by_user_id,
            minimum=SharePermission.EDIT,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        )
        if access is None:
            raise AuthorizationRevoked("run execution authority was revoked")
        current_actor = (
            await session.execute(
                select(runs.c.execution_actor_user_id).where(
                    runs.c.run_id == run_id
                )
            )
        ).scalar_one_or_none()
        if current_actor != actor_user_id:
            raise AuthorizationRevoked("run execution actor changed")

    # -- plans ----------------------------------------------------------- #

    async def save_plan(
        self,
        *,
        run_id: str,
        plan: PlanRecord,
        tasks: list[PlanTaskRecord],
    ) -> PlanRecord:
        async with self._session() as session:
            await self._lock_runtime_authority(session, run_id)
            return await _save_plan_tx(
                session, run_id=run_id, plan=plan, tasks=tasks
            )

    async def get_plan(
        self, run_id: str, *, version: int | None = None
    ) -> tuple[PlanRecord, list[PlanTaskRecord]]:
        async with self._session() as session:
            query = select(run_plans).where(run_plans.c.run_id == run_id)
            if version is None:
                query = query.order_by(run_plans.c.version.desc()).limit(1)
            else:
                query = query.where(run_plans.c.version == version)
            row = (await session.execute(query)).mappings().first()
            if row is None:
                raise PlanNotFound(run_id)
            plan = _plan_from_row(row)
            task_rows = (
                (
                    await session.execute(
                        select(run_plan_tasks)
                        .where(run_plan_tasks.c.plan_id == plan.plan_id)
                        .order_by(run_plan_tasks.c.ordinal)
                    )
                )
                .mappings()
                .all()
            )
            return plan, [_task_from_row(r) for r in task_rows]

    async def list_plan_versions(self, run_id: str) -> list[PlanRecord]:
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(run_plans)
                        .where(run_plans.c.run_id == run_id)
                        .order_by(run_plans.c.version.desc())
                    )
                )
                .mappings()
                .all()
            )
            return [_plan_from_row(row) for row in rows]

    async def settle_terminal_control_rows(
        self, run_id: str
    ) -> tuple[int, int]:
        """Release ``writing`` artifacts and settle pending approvals.

        Idempotent CAS pair for a run that cannot re-enter its graph —
        the second call is a ``(0, 0)`` no-op. Content and revision of a
        released artifact stay untouched: the last streamed state simply
        becomes user-editable again.
        """
        now = time.time()
        async with self._session() as session:
            # Probe first, lock-free: reconcile runs on every plan read
            # of a terminal run, and the settled steady state must not
            # pay an authority lock plus two empty UPDATEs per poll.
            has_candidates = (
                await session.execute(
                    select(literal(1)).where(
                        or_(
                            exists(
                                select(literal(1)).where(
                                    run_artifacts.c.run_id == run_id,
                                    run_artifacts.c.status == "writing",
                                )
                            ),
                            exists(
                                select(literal(1)).where(
                                    run_approvals.c.run_id == run_id,
                                    run_approvals.c.status == "pending",
                                )
                            ),
                        )
                    )
                )
            ).scalar_one_or_none()
            if has_candidates is None:
                return 0, 0
            await self._lock_runtime_authority(session, run_id)
            released = (
                await session.execute(
                    update(run_artifacts)
                    .where(
                        run_artifacts.c.run_id == run_id,
                        run_artifacts.c.status == "writing",
                    )
                    .values(status="ready", updated_at=now)
                )
            ).rowcount
            settled = (
                await session.execute(
                    update(run_approvals)
                    .where(
                        run_approvals.c.run_id == run_id,
                        run_approvals.c.status == "pending",
                    )
                    .values(
                        status="rejected",
                        decision="",
                        note=TERMINAL_APPROVAL_SETTLE_NOTE,
                        decided_at=now,
                    )
                )
            ).rowcount
            return max(0, int(released)), max(0, int(settled))

    async def transition_plan_task(
        self,
        *,
        run_id: str,
        plan_id: str,
        task_id: str,
        status: str,
        child_run_id: str | None = None,
        result_summary: str | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> PlanTaskRecord:
        async with self._session() as session:
            await self._lock_runtime_authority(session, run_id)
            row = (
                await session.execute(
                    select(run_plan_tasks)
                    .where(
                        run_plan_tasks.c.run_id == run_id,
                        run_plan_tasks.c.plan_id == plan_id,
                        run_plan_tasks.c.task_id == task_id,
                    )
                    .with_for_update()
                )
            ).mappings().first()
            if row is None:
                raise PlanTaskNotFound(task_id)
            stored = transitioned_plan_task(
                _task_from_row(row),
                status=status,
                child_run_id=child_run_id,
                result_summary=result_summary,
                result_payload=result_payload,
            )
            await session.execute(
                update(run_plan_tasks)
                .where(
                    run_plan_tasks.c.run_id == run_id,
                    run_plan_tasks.c.plan_id == plan_id,
                    run_plan_tasks.c.task_id == task_id,
                )
                .values(
                    status=stored.status,
                    child_run_id=stored.child_run_id,
                    result_summary=stored.result_summary,
                    result_payload=stored.result_payload,
                )
            )
            return stored

    async def request_plan_task_cancel(
        self,
        *,
        run_id: str,
        plan_id: str,
        task_id: str,
        authorize: Any,
    ) -> tuple[PlanTaskRecord, str | None]:
        async def _write(
            session: "AsyncSession", cancel_child: Any
        ) -> tuple[PlanTaskRecord, str | None]:
            row = (
                await session.execute(
                    select(run_plan_tasks)
                    .where(
                        run_plan_tasks.c.run_id == run_id,
                        run_plan_tasks.c.plan_id == plan_id,
                        run_plan_tasks.c.task_id == task_id,
                    )
                    .with_for_update()
                )
            ).mappings().first()
            if row is None:
                raise PlanTaskNotFound(task_id)
            stored = requested_task_cancellation(_task_from_row(row))
            await session.execute(
                update(run_plan_tasks)
                .where(
                    run_plan_tasks.c.run_id == run_id,
                    run_plan_tasks.c.plan_id == plan_id,
                    run_plan_tasks.c.task_id == task_id,
                )
                .values(
                    status=stored.status,
                    result_summary=stored.result_summary,
                    result_payload=stored.result_payload,
                )
            )
            child_status = (
                await cancel_child(stored.child_run_id)
                if stored.child_run_id
                and stored.status in {"cancel_requested", "cancelled"}
                else None
            )
            return stored, child_status

        return await authorize(_write)

    # -- approvals -------------------------------------------------------- #

    async def create_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        async with self._session() as session:
            await self._lock_runtime_authority(session, approval.run_id)
            created_at = approval.created_at or time.time()
            if approval.subject_type == "plan":
                approval = replace(
                    approval,
                    subject_id=await _resolve_approval_plan_subject_tx(
                        session, approval
                    ),
                )
            await session.execute(
                insert(run_approvals).values(
                    approval_id=approval.approval_id,
                    run_id=approval.run_id,
                    kind=approval.kind,
                    status=approval.status,
                    subject_type=approval.subject_type,
                    subject_id=approval.subject_id,
                    payload=dict(approval.payload),
                    decision=approval.decision,
                    decision_payload=dict(approval.decision_payload),
                    note=approval.note,
                    decided_by_user_id=approval.decided_by_user_id,
                    interrupt_key=approval.interrupt_key,
                    created_at=created_at,
                    decided_at=approval.decided_at,
                )
            )
            return replace(approval, created_at=created_at)

    async def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(run_approvals)
                        .where(run_approvals.c.run_id == run_id)
                        .order_by(
                            run_approvals.c.created_at.desc(),
                            run_approvals.c.approval_id.desc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [_approval_from_row(row) for row in rows]

    async def get_approval(self, run_id: str, approval_id: str) -> ApprovalRecord:
        async with self._session() as session:
            return await _get_approval_tx(session, run_id, approval_id)

    async def decide_approval_and_resume(
        self,
        *,
        run_id: str,
        approval_id: str,
        decision: str,
        decision_payload: dict[str, Any],
        note: str,
        decided_by_user_id: uuid.UUID | None,
        resume: Any,
        edited_plan: PlanRecord | None = None,
        edited_tasks: list[PlanTaskRecord] | None = None,
    ) -> tuple[ApprovalRecord, dict[str, Any]]:
        async def _writer(session: "AsyncSession") -> None:
            # Runs INSIDE the run store's waiting->queued transaction:
            # a rollback there undoes the decision, a crash can never
            # separate decision from resume.
            decided = (
                await session.execute(
                    update(run_approvals)
                    .where(
                        run_approvals.c.approval_id == approval_id,
                        run_approvals.c.run_id == run_id,
                        run_approvals.c.status == "pending",
                    )
                    .values(
                        status=APPROVAL_STATUS_BY_DECISION[decision],
                        decision=decision,
                        decision_payload=dict(decision_payload),
                        note=note,
                        decided_by_user_id=decided_by_user_id,
                        decided_at=time.time(),
                    )
                    .returning(
                        run_approvals.c.approval_id,
                        run_approvals.c.subject_type,
                        run_approvals.c.subject_id,
                    )
                )
            ).first()
            if decided is None:
                raise await _approval_cas_miss(session, run_id, approval_id)
            if edited_plan is not None:
                await _save_plan_tx(
                    session,
                    run_id=run_id,
                    plan=edited_plan,
                    tasks=list(edited_tasks or []),
                )
            elif decided[1] == "plan":
                target_status = (
                    "approved" if decision == "approve" else "rejected"
                )
                updated = (
                    await session.execute(
                        update(run_plans)
                        .where(
                            run_plans.c.run_id == run_id,
                            run_plans.c.plan_id == decided[2],
                        )
                        .values(status=target_status)
                        .returning(run_plans.c.plan_id)
                    )
                ).scalar_one_or_none()
                if updated is None:
                    raise PlanNotFound(run_id)

        summary = await resume(_writer)
        approval = await self.get_approval(run_id, approval_id)
        return approval, summary

    # -- clarifications ----------------------------------------------------- #

    async def create_clarification(
        self, clarification: ClarificationRecord
    ) -> ClarificationRecord:
        async with self._session() as session:
            await self._lock_runtime_authority(
                session, clarification.run_id
            )
            created_at = clarification.created_at or time.time()
            await session.execute(
                insert(run_clarifications).values(
                    clarification_id=clarification.clarification_id,
                    run_id=clarification.run_id,
                    question=clarification.question,
                    options=[dict(option) for option in clarification.options],
                    questions=[
                        dict(question)
                        for question in clarification.questions
                    ],
                    answers=dict(clarification.answers),
                    default_assumption=clarification.default_assumption,
                    status=clarification.status,
                    answer=clarification.answer,
                    option_id=clarification.option_id,
                    answered_by_user_id=clarification.answered_by_user_id,
                    created_at=created_at,
                    answered_at=clarification.answered_at,
                )
            )
            return replace(clarification, created_at=created_at)

    async def list_clarifications(self, run_id: str) -> list[ClarificationRecord]:
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(run_clarifications)
                        .where(run_clarifications.c.run_id == run_id)
                        .order_by(
                            run_clarifications.c.created_at.desc(),
                            run_clarifications.c.clarification_id.desc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [_clarification_from_row(row) for row in rows]

    async def get_clarification(
        self, run_id: str, clarification_id: str
    ) -> ClarificationRecord:
        async with self._session() as session:
            return await _get_clarification_tx(
                session, run_id, clarification_id
            )

    async def answer_clarification_and_resume(
        self,
        *,
        run_id: str,
        clarification_id: str,
        answer: str,
        option_id: str,
        answers: dict[str, Any],
        answered_by_user_id: uuid.UUID | None,
        resume: Any,
    ) -> tuple[ClarificationRecord, dict[str, Any]]:
        async def _writer(session: "AsyncSession") -> None:
            answered = (
                await session.execute(
                    update(run_clarifications)
                    .where(
                        run_clarifications.c.clarification_id
                        == clarification_id,
                        run_clarifications.c.run_id == run_id,
                        run_clarifications.c.status == "pending",
                    )
                    .values(
                        status="answered",
                        answer=answer,
                        option_id=option_id,
                        answers=dict(answers),
                        answered_by_user_id=answered_by_user_id,
                        answered_at=time.time(),
                    )
                    .returning(run_clarifications.c.clarification_id)
                )
            ).scalar_one_or_none()
            if answered is None:
                raise await _clarification_cas_miss(
                    session, run_id, clarification_id
                )

        summary = await resume(_writer)
        clarification = await self.get_clarification(run_id, clarification_id)
        return clarification, summary

    # -- artifacts ----------------------------------------------------------- #

    async def put_context_archive(
        self, *, run_id: str, title: str, text: str,
        transcript: dict[str, Any] | None = None,
    ) -> list[ArtifactRecord]:
        parts = prepare_archive(run_id, title, text, transcript=transcript)
        async with self._session() as session:
            await self._lock_runtime_authority(session, run_id)
            if not parts:
                return []
            now = time.time()
            # Statement batching bounds bind parameters; the transaction is
            # still ONE atomic publication, including replay verification.
            for start in range(0, len(parts), 500):
                await session.execute(pg_insert(run_artifacts).values([
                    {
                        "artifact_id": part.artifact_id, "run_id": run_id, "kind": "context_archive",
                        "session_id": None, "title": part.title, "status": "ready", "revision": 1,
                        "updated_by": "agent", "content_markdown": part.text, "payload": part.payload,
                        "refs": [], "created_at": now, "updated_at": now,
                    }
                    for part in parts[start:start + 500]
                ]).on_conflict_do_nothing())
            landed = {}
            for start in range(0, len(parts), 500):
                rows = (await session.execute(select(run_artifacts).where(
                    run_artifacts.c.artifact_id.in_([part.artifact_id for part in parts[start:start + 500]])
                ))).mappings().all()
                landed.update({row["artifact_id"]: _artifact_from_row(row) for row in rows})
            result = []
            for part in parts:
                row = landed.get(part.artifact_id)
                if row is None or row.run_id != run_id or row.kind != "context_archive" or row.session_id is not None:
                    raise ArtifactNotFound(part.artifact_id)
                if (row.revision != 1 or row.status != "ready" or row.title != part.title
                        or row.content_markdown != part.text or row.payload != part.payload or row.refs):
                    raise ArchiveIntegrityError("An archive identity already holds different data.")
                result.append(row)
            # Immutable originals need no second body in the revision table.
            return result

    async def read_context_archive(
        self, run_id: str, artifact_id: str, *, offset: int | None = None,
        content_hash: str = "", max_chars: int = ARCHIVE_READ_WINDOW_CHARS,
        include_private: bool = False,
    ) -> ArchiveWindow:
        async with self._session() as session:
            seed = (await session.execute(select(*_ARTIFACT_META_COLUMNS).where(
                run_artifacts.c.artifact_id == artifact_id, run_artifacts.c.run_id == run_id,
                run_artifacts.c.kind == "context_archive",
                literal(True) if include_private else _PUBLIC_ARTIFACT,
            ))).mappings().first()
            if seed is None:
                raise ArtifactNotFound(artifact_id)
            if offset is not None and offset != 0 and not content_hash:
                raise ValueError("An archive continuation requires its content fingerprint.")
            info = part_info(artifact_id, seed["payload"])
            if info is None:
                # Legacy rows can still change; body and its metadata therefore
                # come from the SAME statement snapshot.
                row = (await session.execute(select(run_artifacts).where(
                    run_artifacts.c.artifact_id == artifact_id, run_artifacts.c.run_id == run_id,
                    run_artifacts.c.kind == "context_archive",
                ))).mappings().first()
                if row is None:
                    raise ArtifactNotFound(artifact_id)
                return legacy_window(artifact_id, row["title"], row["content_markdown"],
                                     offset=0 if offset is None else offset,
                                     max_chars=max_chars, content_hash=content_hash)
            rows = (await session.execute(select(*_ARTIFACT_META_COLUMNS).where(
                run_artifacts.c.run_id == run_id, run_artifacts.c.kind == "context_archive",
                run_artifacts.c.payload[ARCHIVE_PAYLOAD_KEY]["source_id"].as_string() == info.source_id,
            ))).mappings().all()
            parts = []
            for row in rows:
                part = part_info(row["artifact_id"], row["payload"])
                if part is None or row["revision"] != 1 or row["status"] != "ready":
                    raise ArchiveIntegrityError("Archive part is not an immutable ready record.")
                parts.append(part)
            metadata = validate_parts(parts)
            start, end = window_bounds(info.total_chars, info.start if offset is None else offset,
                                       max_chars, content_hash, info.content_hash)
            wanted = [part.artifact_id for part in metadata if part.start < end and part.end > start]
            body_rows = (await session.execute(select(
                run_artifacts.c.artifact_id, run_artifacts.c.content_markdown,
            ).where(run_artifacts.c.run_id == run_id, run_artifacts.c.artifact_id.in_(wanted)))).all() if wanted else []
            return window_from_parts(metadata, dict(body_rows), offset=start, max_chars=max_chars,
                                     content_hash=content_hash)

    async def list_context_archive_sources(self, run_id: str, *, include_private: bool = False) -> list[ArchiveCatalogueEntry]:
        async with self._session() as session:
            # One statement snapshot. Neither source membership nor its cursor
            # relies on an allocation-time id or a maximum timestamp.
            rows = (await session.execute(select(
                *_ARTIFACT_META_COLUMNS,
                case(
                    (run_artifacts.c.payload[ARCHIVE_PAYLOAD_KEY].is_(None),
                     func.char_length(run_artifacts.c.content_markdown)),
                    else_=0,
                ).label("stored_chars"),
            ).where(run_artifacts.c.run_id == run_id, run_artifacts.c.kind == "context_archive",
                    run_artifacts.c.status == "ready",
                    literal(True) if include_private else _PUBLIC_ARTIFACT))).mappings().all()
            result = []
            for row in rows:
                entry = catalogue_entry(row["artifact_id"], row["title"], row["revision"],
                                        row["payload"], row["stored_chars"], row["created_at"])
                if entry is not None:
                    result.append(entry)
            return result

    async def list_source_artifacts(self, run_ids: list[str]) -> list[ArtifactRecord]:
        result = []
        async with self._session() as session:
            # Bounded bind lists, no body materialization for source discovery.
            for start in range(0, len(run_ids), 500):
                rows = (await session.execute(select(*_ARTIFACT_META_COLUMNS).where(
                    run_artifacts.c.run_id.in_(run_ids[start:start + 500])
                ))).mappings().all()
                result.extend(_artifact_from_row(row, meta_only=True) for row in rows)
        return result

    async def list_source_inputs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        result = []
        async with self._session() as session:
            for start in range(0, len(run_ids), 500):
                for kind, table, key in (
                    ("clarification", run_clarifications, "clarification_id"),
                    ("approval", run_approvals, "approval_id"),
                ):
                    rows = (await session.execute(select(
                        table.c.run_id, table.c[key].label("id"), table.c.created_at,
                    ).where(table.c.run_id.in_(run_ids[start:start + 500]), table.c.status != "pending"))).mappings().all()
                    result.extend({"kind": kind, **dict(row)} for row in rows)
        return result

    async def upsert_artifact(
        self,
        *,
        run_id: str,
        kind: str,
        session_id: str | None,
        title: str,
        status: str,
        content_markdown: str,
        payload: dict[str, Any],
        refs: list[dict[str, Any]],
        updated_by: str,
        artifact_id: str | None = None,
        expected_revision: int | None = None,
        expected_run_attempt: int | None = None,
    ) -> ArtifactRecord:
        async with self._session() as session:
            await self._lock_runtime_authority(session, run_id)
            if expected_run_attempt is not None:
                run_fence = (
                    await session.execute(
                        select(
                            runs.c.status,
                            runs.c.attempt,
                            runs.c.cancel_requested,
                        )
                        .where(runs.c.run_id == run_id)
                        .with_for_update()
                    )
                ).first()
                current_status = str(run_fence[0]) if run_fence else "missing"
                current_attempt = int(run_fence[1]) if run_fence else None
                cancel_requested = bool(run_fence[2]) if run_fence else False
                if (
                    run_fence is None
                    or current_status != "running"
                    or cancel_requested
                    or current_attempt != expected_run_attempt
                ):
                    raise ArtifactPublicationFenced(
                        expected_attempt=expected_run_attempt,
                        current_attempt=current_attempt,
                        status=(
                            "cancel_requested"
                            if cancel_requested
                            else current_status
                        ),
                    )
            existing = await _find_upsert_target(
                session,
                artifact_id=artifact_id,
                session_id=session_id,
                run_id=run_id,
                kind=kind,
            )
            if existing is not None and (
                existing.kind != kind
                or existing.session_id != session_id
                or (session_id is None and existing.run_id != run_id)
            ):
                raise ArtifactNotFound(artifact_id or existing.artifact_id)
            if existing is not None and existing.kind == "context_archive" and is_exact_archive(existing.payload):
                raise ContextArchiveImmutable(existing.artifact_id)
            now = time.time()
            if existing is None:
                if artifact_id is None:
                    raise ValueError("new artifacts need an explicit id")
                # A concurrent writer can create the row (same artifact_id,
                # or same (session|run, kind) partial-unique key) between the
                # find above and this insert. ON CONFLICT DO NOTHING absorbs
                # that race in one statement (no aborted transaction); an
                # empty RETURNING means the racer won, so we surface a
                # revision conflict — the SAME exception the CAS path and the
                # user PUT raise — instead of a raw IntegrityError that would
                # abort the node. The caller's reconcile loop then fast-
                # forwards against the landed row.
                inserted = (
                    await session.execute(
                        pg_insert(run_artifacts)
                        .values(
                            artifact_id=artifact_id,
                            run_id=run_id,
                            session_id=session_id,
                            kind=kind,
                            title=title,
                            status=status,
                            revision=1,
                            updated_by=updated_by,
                            content_markdown=content_markdown,
                            payload=dict(payload),
                            refs=[dict(ref) for ref in refs],
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing()
                        .returning(run_artifacts.c.artifact_id)
                    )
                ).scalar_one_or_none()
                if inserted is None:
                    # Under READ COMMITTED the skip means the racer already
                    # committed, so this fresh-snapshot re-read sees its row
                    # (by the same artifact_id/(session|run, kind) precedence
                    # the insert used). The ``else 0`` is a defensive floor
                    # for the otherwise-unreachable "landed vanished" case.
                    landed = await _find_upsert_target(
                        session,
                        artifact_id=artifact_id,
                        session_id=session_id,
                        run_id=run_id,
                        kind=kind,
                    )
                    raise ArtifactRevisionConflict(
                        landed.revision if landed is not None else 0
                    )
                revision = 1
                target_id = artifact_id
            else:
                target_id = existing.artifact_id
                # Atomic in-database increment: a Python-computed
                # revision would race a concurrent user PUT and collide
                # on the revisions primary key; the row lock serializes
                # writers and RETURNING yields the authoritative number.
                # With expected_revision set (agent guard), the same
                # UPDATE also CAS-checks the revision — mirroring the user
                # PUT path, so an interleaved user edit yields a loud
                # conflict instead of a silent clobber.
                guarded = update(run_artifacts).where(
                    run_artifacts.c.artifact_id == target_id
                )
                if expected_revision is not None:
                    guarded = guarded.where(
                        run_artifacts.c.revision == expected_revision
                    )
                revision_row = (
                    await session.execute(
                        guarded.values(
                            run_id=run_id,
                            title=title,
                            status=status,
                            revision=run_artifacts.c.revision + 1,
                            updated_by=updated_by,
                            content_markdown=content_markdown,
                            payload=dict(payload),
                            refs=[dict(ref) for ref in refs],
                            updated_at=now,
                        )
                        .returning(run_artifacts.c.revision)
                    )
                ).scalar_one_or_none()
                if revision_row is None:
                    # CAS miss: re-read the authoritative revision for the
                    # conflict payload (the row still exists — only its
                    # revision moved).
                    current = (
                        await session.execute(
                            select(run_artifacts.c.revision).where(
                                run_artifacts.c.artifact_id == target_id
                            )
                        )
                    ).scalar_one()
                    raise ArtifactRevisionConflict(current)
                revision = revision_row
            await session.execute(
                insert(run_artifact_revisions).values(
                    artifact_id=target_id,
                    revision=revision,
                    created_by=updated_by,
                    content_markdown=content_markdown,
                    created_at=now,
                    status=status,
                )
            )
            row = (
                (
                    await session.execute(
                        select(run_artifacts).where(
                            run_artifacts.c.artifact_id == target_id
                        )
                    )
                )
                .mappings()
                .one()
            )
            record = _artifact_from_row(row)
            # The previous body is already in memory (existing) — count
            # the line delta once here; only the RETURNED record carries
            # it, reads keep yielding None.
            counted = count_line_changes(
                existing.content_markdown if existing is not None else "",
                content_markdown,
            )
            if counted is not None:
                record = replace(
                    record, lines_added=counted[0], lines_removed=counted[1]
                )
            return record

    async def revise_session_artifacts_atomically(
        self,
        *,
        run_id: str,
        session_id: str | None,
        revisions: list[ArtifactBatchRevision],
    ) -> list[ArtifactRecord]:
        ids = [item.artifact_id for item in revisions]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact revision batch contains duplicate ids")
        if not revisions:
            return []
        async with self._session() as session:
            await self._lock_runtime_authority(session, run_id)
            rows = (
                await session.execute(
                    select(run_artifacts)
                    .where(
                        run_artifacts.c.artifact_id.in_(ids),
                        run_artifacts.c.session_id == session_id,
                    )
                    .with_for_update()
                )
            ).mappings().all()
            by_id = {
                str(row["artifact_id"]): _artifact_from_row(row)
                for row in rows
            }
            for item in revisions:
                current = by_id.get(item.artifact_id)
                if current is None or (session_id is None and current.run_id != run_id):
                    raise ArtifactNotFound(item.artifact_id)
                if current.kind == "context_archive" and is_exact_archive(current.payload):
                    raise ContextArchiveImmutable(item.artifact_id)
                if current.revision != item.expected_revision:
                    raise ArtifactRevisionConflict(current.revision)
            now = time.time()
            stored_rows: list[ArtifactRecord] = []
            for item in revisions:
                current = by_id[item.artifact_id]
                next_revision = current.revision + 1
                await session.execute(
                    update(run_artifacts)
                    .where(run_artifacts.c.artifact_id == item.artifact_id)
                    .values(
                        run_id=run_id,
                        revision=next_revision,
                        updated_by="agent",
                        content_markdown=item.content_markdown,
                        updated_at=now,
                    )
                )
                await session.execute(
                    insert(run_artifact_revisions).values(
                        artifact_id=item.artifact_id,
                        revision=next_revision,
                        created_by="agent",
                        content_markdown=item.content_markdown,
                        created_at=now,
                        status="ready",
                    )
                )
                counted = count_line_changes(
                    current.content_markdown, item.content_markdown
                )
                stored_rows.append(
                    replace(
                        current,
                        run_id=run_id,
                        revision=next_revision,
                        updated_by="agent",
                        content_markdown=item.content_markdown,
                        updated_at=now,
                        lines_added=counted[0] if counted else None,
                        lines_removed=counted[1] if counted else None,
                    )
                )
            return stored_rows

    async def user_update_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        content_markdown: str,
        expected_revision: int,
        authorize: Any,
    ) -> ArtifactRecord:
        async def _write(
            session: "AsyncSession", _cancel_child: Any
        ) -> ArtifactRecord:
            now = time.time()
            row = (
                (
                    await session.execute(
                        update(run_artifacts)
                        .where(
                            run_artifacts.c.artifact_id == artifact_id,
                            run_artifacts.c.run_id == run_id,
                            _PUBLIC_ARTIFACT,
                            run_artifacts.c.status == "ready",
                            _MUTABLE_ARTIFACT,
                            run_artifacts.c.revision == expected_revision,
                        )
                        .values(
                            revision=expected_revision + 1,
                            updated_by="user",
                            content_markdown=content_markdown,
                            updated_at=now,
                        )
                        .returning(run_artifacts)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                # Distinguish the three misses loudly: no such artifact,
                # a row still being written, and a stale expected
                # revision each raise their own error.
                current = (
                    (
                        await session.execute(
                            select(
                                run_artifacts.c.status,
                                run_artifacts.c.revision,
                                run_artifacts.c.kind,
                                run_artifacts.c.payload,
                            ).where(
                                run_artifacts.c.artifact_id == artifact_id,
                                run_artifacts.c.run_id == run_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if current is None or is_private_archive(current["payload"]):
                    raise ArtifactNotFound(artifact_id)
                if current["kind"] == "context_archive" and is_exact_archive(current["payload"]):
                    raise ContextArchiveImmutable(artifact_id)
                if current["status"] == "writing":
                    raise ArtifactLocked(artifact_id)
                raise ArtifactRevisionConflict(current["revision"])
            await session.execute(
                insert(run_artifact_revisions).values(
                    artifact_id=artifact_id,
                    revision=expected_revision + 1,
                    created_by="user",
                    content_markdown=content_markdown,
                    created_at=now,
                    status="ready",
                )
            )
            return _artifact_from_row(row)

        return await authorize(_write)

    async def rename_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        title: str,
        authorize: Any,
    ) -> ArtifactRecord:
        async def _write(
            session: "AsyncSession", _cancel_child: Any
        ) -> ArtifactRecord:
            # Metadata-only: no revision bump, no revision row —
            # the content history must not record a rename.
            row = (
                (
                    await session.execute(
                        update(run_artifacts)
                        .where(
                            run_artifacts.c.artifact_id == artifact_id,
                            run_artifacts.c.run_id == run_id,
                            run_artifacts.c.status == "ready",
                            _MUTABLE_ARTIFACT,
                        )
                        .values(title=title, updated_at=time.time())
                        .returning(run_artifacts)
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                current = (
                    (
                        await session.execute(
                            select(run_artifacts.c.status, run_artifacts.c.kind, run_artifacts.c.payload).where(
                                run_artifacts.c.artifact_id == artifact_id,
                                run_artifacts.c.run_id == run_id,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if current is None or is_private_archive(current["payload"]):
                    raise ArtifactNotFound(artifact_id)
                if current["kind"] == "context_archive" and is_exact_archive(current["payload"]):
                    raise ContextArchiveImmutable(artifact_id)
                raise ArtifactLocked(artifact_id)
            return _artifact_from_row(row)

        return await authorize(_write)

    async def get_artifact(
        self, run_id: str, artifact_id: str, *, revision: int | None = None,
        include_private: bool = False,
    ) -> tuple[ArtifactRecord, list[ArtifactRevisionRecord]]:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        select(run_artifacts).where(
                            run_artifacts.c.artifact_id == artifact_id,
                            run_artifacts.c.run_id == run_id,
                            literal(True) if include_private else _PUBLIC_ARTIFACT,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ArtifactNotFound(artifact_id)
            artifact = _artifact_from_row(row)
            if artifact.kind == "context_archive" and is_exact_archive(artifact.payload):
                if revision not in (None, 1):
                    raise ArtifactNotFound(artifact_id)
                return artifact, [ArtifactRevisionRecord(
                    artifact_id=artifact.artifact_id, revision=1, created_by=artifact.updated_by,
                    content_markdown="", created_at=artifact.created_at, status=artifact.status,
                )]
            if revision is not None:
                body = (
                    await session.execute(
                        select(
                            run_artifact_revisions.c.content_markdown
                        ).where(
                            run_artifact_revisions.c.artifact_id
                            == artifact_id,
                            run_artifact_revisions.c.revision == revision,
                        )
                    )
                ).scalar_one_or_none()
                if body is None:
                    raise ArtifactNotFound(artifact_id)
                artifact = replace(artifact, content_markdown=body)
            revision_rows = (
                (
                    await session.execute(
                        select(
                            run_artifact_revisions.c.artifact_id,
                            run_artifact_revisions.c.revision,
                            run_artifact_revisions.c.created_by,
                            run_artifact_revisions.c.created_at,
                            run_artifact_revisions.c.status,
                        )
                        .where(
                            run_artifact_revisions.c.artifact_id
                            == artifact_id
                        )
                        .order_by(run_artifact_revisions.c.revision.desc())
                    )
                )
                .mappings()
                .all()
            )
            return artifact, [
                ArtifactRevisionRecord(
                    artifact_id=r["artifact_id"],
                    revision=r["revision"],
                    created_by=r["created_by"],
                    content_markdown="",
                    created_at=r["created_at"],
                    status=r["status"],
                )
                for r in revision_rows
            ]

    async def get_session_artifact(
        self, session_id: str, kind: str
    ) -> ArtifactRecord | None:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        select(run_artifacts).where(
                            run_artifacts.c.session_id == session_id,
                            run_artifacts.c.kind == kind,
                        )
                    )
                )
                .mappings()
                .first()
            )
            return _artifact_from_row(row) if row is not None else None

    async def get_session_artifact_by_id(
        self, session_id: str, artifact_id: str
    ) -> ArtifactRecord:
        async with self._session() as session:
            row = (
                (
                    await session.execute(
                        select(run_artifacts).where(
                            run_artifacts.c.session_id == session_id,
                            run_artifacts.c.artifact_id == artifact_id,
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row is None:
                raise ArtifactNotFound(artifact_id)
            return _artifact_from_row(row)

    async def list_session_artifacts(
        self, session_id: str
    ) -> list[ArtifactRecord]:
        async with self._session() as session:
            rows = (
                (
                    await session.execute(
                        select(run_artifacts)
                        .where(run_artifacts.c.session_id == session_id)
                        .order_by(
                            run_artifacts.c.created_at.asc(),
                            run_artifacts.c.artifact_id.asc(),
                        )
                    )
                )
                .mappings()
                .all()
            )
            return [_artifact_from_row(row, meta_only=True) for row in rows]

    async def list_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        limit: int = 50,
        after: tuple[float, str] | None = None,
        include_private: bool = False,
    ) -> tuple[list[ArtifactRecord], str | None]:
        async with self._session() as session:
            query = select(*_ARTIFACT_META_COLUMNS).where(
                run_artifacts.c.run_id == run_id,
                literal(True) if include_private else _PUBLIC_ARTIFACT,
            )
            if kind is not None:
                query = query.where(run_artifacts.c.kind == kind)
            if after is not None:
                after_created, after_id = after
                query = query.where(
                    (run_artifacts.c.created_at < after_created)
                    | (
                        (run_artifacts.c.created_at == after_created)
                        & (run_artifacts.c.artifact_id < after_id)
                    )
                )
            rows = (
                (
                    await session.execute(
                        query.order_by(
                            run_artifacts.c.created_at.desc(),
                            run_artifacts.c.artifact_id.desc(),
                        ).limit(limit + 1)
                    )
                )
                .mappings()
                .all()
            )
            page = [_artifact_from_row(row, meta_only=True) for row in rows]
            next_cursor = None
            if len(page) > limit:
                page = page[:limit]
                last = page[-1]
                next_cursor = encode_cursor(last.created_at, last.artifact_id)
            return page, next_cursor


# -- transaction bodies shared with the resume-transaction writers ---------- #


async def _resolve_approval_plan_subject_tx(
    session: "AsyncSession", approval: ApprovalRecord
) -> str:
    """Resolve and lock a plan subject in the approval's tenant and run.

    An explicit client subject is validated just like the legacy empty-subject
    path.  The key-share lock keeps the parent stable until the approval insert
    commits, and the run predicate prevents a globally valid plan id from being
    attached to another run.
    """
    query = select(run_plans.c.plan_id).where(
        run_plans.c.tenant_id == DEFAULT_TENANT,
        run_plans.c.run_id == approval.run_id,
    )
    if approval.subject_id:
        query = query.where(run_plans.c.plan_id == approval.subject_id)
    else:
        version = int(approval.payload.get("plan_version", 0) or 0)
        if version > 0:
            query = query.where(run_plans.c.version == version)
        else:
            query = query.order_by(run_plans.c.version.desc()).limit(1)
    plan_id = (
        await session.execute(
            query.with_for_update(read=True, key_share=True)
        )
    ).scalar_one_or_none()
    if plan_id is None:
        raise PlanNotFound(approval.run_id)
    return str(plan_id)


async def _save_plan_tx(
    session: "AsyncSession",
    *,
    run_id: str,
    plan: PlanRecord,
    tasks: list[PlanTaskRecord],
) -> PlanRecord:
    latest = (
        await session.execute(
            select(func.max(run_plans.c.version)).where(
                run_plans.c.run_id == run_id
            )
        )
    ).scalar_one()
    version = plan.version if plan.version > 0 else (latest or 0) + 1
    if version != (latest or 0) + 1:
        raise ValueError(f"plan version must be {(latest or 0) + 1}, got {version}")
    await session.execute(
        update(run_plans)
        .where(
            run_plans.c.run_id == run_id,
            run_plans.c.status.notin_(("rejected", "superseded")),
        )
        .values(status="superseded")
    )
    created_at = plan.created_at or time.time()
    await session.execute(
        insert(run_plans).values(
            plan_id=plan.plan_id,
            run_id=run_id,
            version=version,
            status=plan.status,
            created_by=plan.created_by,
            summary_markdown=plan.summary_markdown,
            assumptions=list(plan.assumptions),
            success_criteria=list(plan.success_criteria),
            reason=plan.reason,
            created_at=created_at,
        )
    )
    if tasks:
        await session.execute(
            insert(run_plan_tasks),
            [
                {
                    "task_id": task.task_id,
                    "plan_id": plan.plan_id,
                    "run_id": run_id,
                    "ordinal": task.ordinal,
                    "title": task.title,
                    "tool_kind": task.tool_kind,
                    "objective": task.objective,
                    "queries": list(task.queries),
                    "gap_ids": list(task.gap_ids),
                    "depends_on": list(task.depends_on),
                    "budget": dict(task.budget),
                    "params": dict(task.params),
                    "expected_output": task.expected_output,
                    "status": task.status,
                    "child_run_id": task.child_run_id,
                    "result_summary": task.result_summary,
                    "result_payload": dict(task.result_payload),
                }
                for task in tasks
            ],
        )
    return replace(
        plan, run_id=run_id, version=version, created_at=created_at
    )


async def _approval_cas_miss(
    session: "AsyncSession", run_id: str, approval_id: str
) -> Exception:
    stored = await _get_approval_tx(session, run_id, approval_id)
    return ApprovalAlreadyDecided(stored)


async def _get_approval_tx(
    session: "AsyncSession", run_id: str, approval_id: str
) -> ApprovalRecord:
    row = (
        (
            await session.execute(
                select(run_approvals).where(
                    run_approvals.c.approval_id == approval_id,
                    run_approvals.c.run_id == run_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ApprovalNotFound(approval_id)
    return _approval_from_row(row)


async def _clarification_cas_miss(
    session: "AsyncSession", run_id: str, clarification_id: str
) -> Exception:
    stored = await _get_clarification_tx(session, run_id, clarification_id)
    return ClarificationAlreadyAnswered(stored)


async def _get_clarification_tx(
    session: "AsyncSession", run_id: str, clarification_id: str
) -> ClarificationRecord:
    row = (
        (
            await session.execute(
                select(run_clarifications).where(
                    run_clarifications.c.clarification_id == clarification_id,
                    run_clarifications.c.run_id == run_id,
                )
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise ClarificationNotFound(clarification_id)
    return _clarification_from_row(row)


async def _find_upsert_target(
    session: "AsyncSession",
    *,
    artifact_id: str | None,
    session_id: str | None,
    run_id: str,
    kind: str,
) -> ArtifactRecord | None:
    if artifact_id is not None:
        query = select(run_artifacts).where(
            run_artifacts.c.artifact_id == artifact_id
        )
    elif session_id is not None:
        if kind not in SESSION_SINGLETON_ARTIFACT_KINDS:
            return None
        query = select(run_artifacts).where(
            run_artifacts.c.session_id == session_id,
            run_artifacts.c.kind == kind,
        )
    else:
        if kind not in RUN_SINGLETON_ARTIFACT_KINDS:
            return None
        query = select(run_artifacts).where(
            run_artifacts.c.run_id == run_id,
            run_artifacts.c.kind == kind,
            run_artifacts.c.session_id.is_(None),
        )
    row = (await session.execute(query)).mappings().first()
    return None if row is None else _artifact_from_row(row)


# -- row mappers ------------------------------------------------------------- #


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return json.loads(value or "[]")
    return list(value or [])


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value or "{}")
    return dict(value or {})


def _plan_from_row(row: Any) -> PlanRecord:
    return PlanRecord(
        plan_id=row["plan_id"],
        run_id=row["run_id"],
        version=row["version"],
        status=row["status"],
        created_by=row["created_by"],
        summary_markdown=row["summary_markdown"],
        assumptions=tuple(_json_list(row["assumptions"])),
        success_criteria=tuple(_json_list(row["success_criteria"])),
        reason=row["reason"],
        created_at=row["created_at"],
    )


def _task_from_row(row: Any) -> PlanTaskRecord:
    return PlanTaskRecord(
        task_id=row["task_id"],
        plan_id=row["plan_id"],
        run_id=row["run_id"],
        ordinal=row["ordinal"],
        title=row["title"],
        tool_kind=row["tool_kind"],
        objective=row["objective"],
        queries=tuple(_json_list(row["queries"])),
        gap_ids=tuple(_json_list(row["gap_ids"])),
        depends_on=tuple(_json_list(row["depends_on"])),
        budget=_json_dict(row["budget"]),
        params=_json_dict(row["params"]),
        expected_output=row["expected_output"],
        status=row["status"],
        child_run_id=row["child_run_id"],
        result_summary=row["result_summary"],
        result_payload=_json_dict(row["result_payload"]),
    )


def _approval_from_row(row: Any) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row["approval_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        status=row["status"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        payload=_json_dict(row["payload"]),
        decision=row["decision"],
        decision_payload=_json_dict(row["decision_payload"]),
        note=row["note"],
        decided_by_user_id=row["decided_by_user_id"],
        interrupt_key=row["interrupt_key"],
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


def _clarification_from_row(row: Any) -> ClarificationRecord:
    return ClarificationRecord(
        clarification_id=row["clarification_id"],
        run_id=row["run_id"],
        question=row["question"],
        options=tuple(_json_list(row["options"])),
        questions=tuple(_json_list(row["questions"])),
        answers=dict(row["answers"] or {}),
        default_assumption=row["default_assumption"],
        status=row["status"],
        answer=row["answer"],
        option_id=row["option_id"],
        answered_by_user_id=row["answered_by_user_id"],
        created_at=row["created_at"],
        answered_at=row["answered_at"],
    )


def _artifact_from_row(row: Any, *, meta_only: bool = False) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row["artifact_id"],
        run_id=row["run_id"],
        kind=row["kind"],
        session_id=row["session_id"],
        title=row["title"],
        status=row["status"],
        revision=row["revision"],
        updated_by=row["updated_by"],
        content_markdown="" if meta_only else row["content_markdown"],
        payload=_json_dict(row["payload"]),
        refs=tuple(_json_list(row["refs"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
