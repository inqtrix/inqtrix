"""In-memory agent control store (offline/test tier).

Lockstep counterpart of
:class:`~inqtrix.storage.agent_control_postgres.PostgresAgentControlStore`:
same port, same error types, same ordering. Thread-safe via one lock (the
agent runtime touches the store from worker threads); no awaits happen while
the lock is held.

Retention mirrors the Postgres ``ON DELETE CASCADE`` only logically: rows
whose run expired become unreachable (every route resolves the run first),
and process lifetime bounds growth — the same volatility contract as the
in-memory run store itself.
"""

from __future__ import annotations

import uuid
import threading
import time
from copy import deepcopy
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, Callable

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
from inqtrix.pagination import keyset_page
from inqtrix.agents.context_archive import (
    ARCHIVE_PAYLOAD_KEY, ARCHIVE_READ_WINDOW_CHARS, ArchiveCatalogueEntry, ArchiveIntegrityError,
    ArchiveWindow, catalogue_entry, is_private_archive, is_exact_archive, legacy_window,
    part_info, prepare_archive, validate_parts, window_bounds, window_from_parts,
)


class MemoryAgentControlStore:
    """Dict-backed :class:`~inqtrix.agents.control_ports.AgentControlStore`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._execution_guard: Callable[[str], Any] | None = None
        self._plans: dict[str, PlanRecord] = {}
        self._plan_tasks: dict[str, list[PlanTaskRecord]] = {}
        self._approvals: dict[str, ApprovalRecord] = {}
        self._clarifications: dict[str, ClarificationRecord] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._revisions: dict[str, list[ArtifactRevisionRecord]] = {}

    def bind_authority_coordinator(self, coordinator: Any) -> None:
        """Share the process-local transaction lock with authority stores."""
        self._lock = coordinator.lock

    def bind_execution_guard(self, guard: Callable[[str], Any]) -> None:
        """Guard agent-runtime writes with the run's live effective actor."""
        self._execution_guard = guard

    def _runtime_write_guard(self, run_id: str) -> Any:
        """Return the composed run-authority guard when scoped auth is active."""
        if self._execution_guard is None:
            return nullcontext()
        return self._execution_guard(run_id)

    # -- plans ---------------------------------------------------------- #

    async def save_plan(
        self,
        *,
        run_id: str,
        plan: PlanRecord,
        tasks: list[PlanTaskRecord],
    ) -> PlanRecord:
        with self._runtime_write_guard(run_id), self._lock:
            return self._save_plan_locked(run_id=run_id, plan=plan, tasks=tasks)

    def _save_plan_locked(
        self, *, run_id: str, plan: PlanRecord, tasks: list[PlanTaskRecord]
    ) -> PlanRecord:
        latest = max(
            (p.version for p in self._plans.values() if p.run_id == run_id),
            default=0,
        )
        version = plan.version if plan.version > 0 else latest + 1
        if version != latest + 1:
            raise ValueError(
                f"plan version must be {latest + 1}, got {version}"
            )
        for plan_id, existing in list(self._plans.items()):
            if existing.run_id == run_id and existing.status not in (
                "rejected",
                "superseded",
            ):
                self._plans[plan_id] = replace(existing, status="superseded")
        stored = replace(
            plan,
            run_id=run_id,
            version=version,
            created_at=plan.created_at or time.time(),
        )
        self._plans[stored.plan_id] = stored
        self._plan_tasks[stored.plan_id] = [
            replace(task, plan_id=stored.plan_id, run_id=run_id)
            for task in tasks
        ]
        return stored

    async def get_plan(
        self, run_id: str, *, version: int | None = None
    ) -> tuple[PlanRecord, list[PlanTaskRecord]]:
        with self._lock:
            candidates = [
                p for p in self._plans.values() if p.run_id == run_id
            ]
            if not candidates:
                raise PlanNotFound(run_id)
            if version is None:
                plan = max(candidates, key=lambda p: p.version)
            else:
                matching = [p for p in candidates if p.version == version]
                if not matching:
                    raise PlanNotFound(run_id)
                plan = matching[0]
            tasks = sorted(
                self._plan_tasks.get(plan.plan_id, []),
                key=lambda t: t.ordinal,
            )
            return plan, list(tasks)

    async def list_plan_versions(self, run_id: str) -> list[PlanRecord]:
        with self._lock:
            return sorted(
                (p for p in self._plans.values() if p.run_id == run_id),
                key=lambda p: p.version,
                reverse=True,
            )

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
        with self._runtime_write_guard(run_id), self._lock:
            tasks = self._plan_tasks.get(plan_id)
            if tasks is None:
                raise PlanTaskNotFound(task_id)
            for index, current in enumerate(tasks):
                if current.task_id == task_id and current.run_id == run_id:
                    stored = transitioned_plan_task(
                        current,
                        status=status,
                        child_run_id=child_run_id,
                        result_summary=result_summary,
                        result_payload=result_payload,
                    )
                    tasks[index] = stored
                    return stored
            raise PlanTaskNotFound(task_id)

    async def request_plan_task_cancel(
        self,
        *,
        run_id: str,
        plan_id: str,
        task_id: str,
        authorize: Any,
    ) -> tuple[PlanTaskRecord, str | None]:
        def _write(
            _transaction: Any, cancel_child: Any
        ) -> tuple[PlanTaskRecord, str | None]:
            with self._lock:
                tasks = self._plan_tasks.get(plan_id)
                if tasks is None:
                    raise PlanTaskNotFound(task_id)
                for index, current in enumerate(tasks):
                    if current.task_id != task_id or current.run_id != run_id:
                        continue
                    stored = requested_task_cancellation(current)
                    tasks[index] = stored
                    try:
                        child_status = (
                            cancel_child(stored.child_run_id)
                            if stored.child_run_id
                            and stored.status in {"cancel_requested", "cancelled"}
                            else None
                        )
                    except BaseException:
                        tasks[index] = current
                        raise
                    return stored, child_status
                raise PlanTaskNotFound(task_id)

        return await authorize(_write)

    # -- approvals ------------------------------------------------------- #

    async def create_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        with self._runtime_write_guard(approval.run_id), self._lock:
            stored = replace(
                approval, created_at=approval.created_at or time.time()
            )
            if stored.subject_type == "plan" and not stored.subject_id:
                version = int(stored.payload.get("plan_version", 0) or 0)
                matching = [
                    plan
                    for plan in self._plans.values()
                    if plan.run_id == stored.run_id
                    and (version <= 0 or plan.version == version)
                ]
                if not matching:
                    raise PlanNotFound(stored.run_id)
                stored = replace(
                    stored,
                    subject_id=max(
                        matching, key=lambda item: item.version
                    ).plan_id,
                )
            elif stored.subject_type == "plan":
                plan = self._plans.get(stored.subject_id)
                if plan is None or plan.run_id != stored.run_id:
                    raise PlanNotFound(stored.run_id)
            self._approvals[stored.approval_id] = stored
            return stored

    async def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return sorted(
                (a for a in self._approvals.values() if a.run_id == run_id),
                key=lambda a: (a.created_at, a.approval_id),
                reverse=True,
            )

    async def get_approval(self, run_id: str, approval_id: str) -> ApprovalRecord:
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.run_id != run_id:
                raise ApprovalNotFound(approval_id)
            return approval

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
        def _writer(_transaction: Any) -> None:
            # ``resume`` invokes this callback while the run store holds the
            # shared authority lock. Control readers use the same lock, so no
            # transient decided row can escape if the waiting->queued CAS
            # fails. This is the in-memory equivalent of the Postgres writer.
            with self._lock:
                approval = self._approvals.get(approval_id)
                if approval is None or approval.run_id != run_id:
                    raise ApprovalNotFound(approval_id)
                if approval.status != "pending":
                    raise ApprovalAlreadyDecided(approval)
                plan_decision = approval.subject_type == "plan"
                target_plan = (
                    self._plans.get(approval.subject_id)
                    if plan_decision and edited_plan is None
                    else None
                )
                if plan_decision and edited_plan is None and (
                    target_plan is None or target_plan.run_id != run_id
                ):
                    raise PlanNotFound(run_id)
                if edited_plan is not None:
                    self._save_plan_locked(
                        run_id=run_id,
                        plan=edited_plan,
                        tasks=list(edited_tasks or []),
                    )
                elif target_plan is not None:
                    self._plans[target_plan.plan_id] = replace(
                        target_plan,
                        status=(
                            "approved"
                            if decision == "approve"
                            else "rejected"
                        ),
                    )
                self._approvals[approval_id] = replace(
                    approval,
                    status=APPROVAL_STATUS_BY_DECISION[decision],
                    decision=decision,
                    decision_payload=dict(decision_payload),
                    note=note,
                    decided_by_user_id=decided_by_user_id,
                    decided_at=time.time(),
                )

        summary = await resume(_writer)
        with self._lock:
            approval = self._approvals.get(approval_id)
            if approval is None or approval.run_id != run_id:
                raise ApprovalNotFound(approval_id)
            return approval, summary

    # -- clarifications --------------------------------------------------- #

    async def create_clarification(
        self, clarification: ClarificationRecord
    ) -> ClarificationRecord:
        with self._runtime_write_guard(clarification.run_id), self._lock:
            stored = replace(
                clarification,
                created_at=clarification.created_at or time.time(),
            )
            self._clarifications[stored.clarification_id] = stored
            return stored

    async def list_clarifications(self, run_id: str) -> list[ClarificationRecord]:
        with self._lock:
            return sorted(
                (
                    c
                    for c in self._clarifications.values()
                    if c.run_id == run_id
                ),
                key=lambda c: (c.created_at, c.clarification_id),
                reverse=True,
            )

    async def get_clarification(
        self, run_id: str, clarification_id: str
    ) -> ClarificationRecord:
        with self._lock:
            clarification = self._clarifications.get(clarification_id)
            if clarification is None or clarification.run_id != run_id:
                raise ClarificationNotFound(clarification_id)
            return clarification

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
        def _writer(_transaction: Any) -> None:
            with self._lock:
                clarification = self._clarifications.get(clarification_id)
                if clarification is None or clarification.run_id != run_id:
                    raise ClarificationNotFound(clarification_id)
                if clarification.status != "pending":
                    raise ClarificationAlreadyAnswered(clarification)
                self._clarifications[clarification_id] = replace(
                    clarification,
                    status="answered",
                    answer=answer,
                    option_id=option_id,
                    answers=dict(answers),
                    answered_by_user_id=answered_by_user_id,
                    answered_at=time.time(),
                )

        summary = await resume(_writer)
        with self._lock:
            clarification = self._clarifications.get(clarification_id)
            if clarification is None or clarification.run_id != run_id:
                raise ClarificationNotFound(clarification_id)
            return clarification, summary

    # -- artifacts --------------------------------------------------------- #

    async def put_context_archive(
        self, *, run_id: str, title: str, text: str,
        transcript: dict[str, Any] | None = None,
    ) -> list[ArtifactRecord]:
        parts = prepare_archive(run_id, title, text, transcript=transcript)
        with self._runtime_write_guard(run_id), self._lock:
            # Validate the WHOLE batch before writing any new row.
            for part in parts:
                existing = self._artifacts.get(part.artifact_id)
                if existing is None:
                    continue
                if existing.run_id != run_id or existing.kind != "context_archive" or existing.session_id is not None:
                    raise ArtifactNotFound(part.artifact_id)
                if (existing.revision != 1 or existing.status != "ready"
                        or existing.title != part.title or existing.content_markdown != part.text
                        or existing.payload != part.payload or existing.refs):
                    raise ArchiveIntegrityError("An archive identity already holds different data.")
            now = time.time()
            result = []
            for part in parts:
                if part.artifact_id not in self._artifacts:
                    self._artifacts[part.artifact_id] = ArtifactRecord(
                        artifact_id=part.artifact_id, run_id=run_id, kind="context_archive",
                        session_id=None, title=part.title, status="ready", revision=1,
                        updated_by="agent", content_markdown=part.text, payload=part.payload,
                        refs=(), created_at=now, updated_at=now,
                    )
                result.append(deepcopy(self._artifacts[part.artifact_id]))
            return result

    async def read_context_archive(
        self, run_id: str, artifact_id: str, *, offset: int | None = None,
        content_hash: str = "", max_chars: int = ARCHIVE_READ_WINDOW_CHARS,
        include_private: bool = False,
    ) -> ArchiveWindow:
        with self._lock:
            seed = self._artifacts.get(artifact_id)
            if seed is None or seed.run_id != run_id or seed.kind != "context_archive":
                raise ArtifactNotFound(artifact_id)
            if is_private_archive(seed.payload) and not include_private:
                raise ArtifactNotFound(artifact_id)
            if offset is not None and offset != 0 and not content_hash:
                raise ValueError("An archive continuation requires its content fingerprint.")
            info = part_info(seed.artifact_id, seed.payload)
            if info is None:
                return legacy_window(seed.artifact_id, seed.title, seed.content_markdown,
                                     offset=0 if offset is None else offset,
                                     max_chars=max_chars, content_hash=content_hash)
            rows = []
            for row in self._artifacts.values():
                if row.run_id != run_id or row.kind != "context_archive":
                    continue
                meta = row.payload.get(ARCHIVE_PAYLOAD_KEY)
                if isinstance(meta, dict) and meta.get("source_id") == info.source_id:
                    rows.append(row)
            parts = []
            for row in rows:
                part = part_info(row.artifact_id, row.payload)
                if part is None or row.revision != 1 or row.status != "ready":
                    raise ArchiveIntegrityError("Archive part is not an immutable ready record.")
                parts.append(part)
            metadata = validate_parts(parts)
            start, end = window_bounds(info.total_chars, info.start if offset is None else offset,
                                       max_chars, content_hash, info.content_hash)
            wanted = {part.artifact_id for part in metadata if part.start < end and part.end > start}
            bodies = {row.artifact_id: row.content_markdown for row in rows if row.artifact_id in wanted}
            return window_from_parts(metadata, bodies, offset=start, max_chars=max_chars,
                                     content_hash=content_hash)

    async def list_context_archive_sources(self, run_id: str, *, include_private: bool = False) -> list[ArchiveCatalogueEntry]:
        with self._lock:
            result = []
            for row in self._artifacts.values():
                if row.run_id != run_id or row.kind != "context_archive" or row.status != "ready":
                    continue
                if is_private_archive(row.payload) and not include_private:
                    continue
                entry = catalogue_entry(row.artifact_id, row.title, row.revision, row.payload,
                                        len(row.content_markdown), row.created_at)
                if entry is not None:
                    result.append(entry)
            return result

    async def list_source_artifacts(self, run_ids: list[str]) -> list[ArtifactRecord]:
        allowed = set(run_ids)
        with self._lock:
            return [replace(row, content_markdown="", payload=deepcopy(row.payload))
                    for row in self._artifacts.values() if row.run_id in allowed]

    async def list_source_inputs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        allowed = set(run_ids)
        with self._lock:
            return [
                {"kind": kind, "run_id": row.run_id, "id": getattr(row, key), "created_at": row.created_at}
                for kind, records, key in (
                    ("clarification", self._clarifications.values(), "clarification_id"),
                    ("approval", self._approvals.values(), "approval_id"),
                )
                for row in records if row.run_id in allowed and row.status != "pending"
            ]

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
        # In-process execution has no claim takeover.  Keep the parameter in
        # lockstep with Postgres so the publisher uses one store contract.
        del expected_run_attempt
        with self._runtime_write_guard(run_id), self._lock:
            existing = self._find_upsert_target_locked(
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
            if (
                existing is not None
                and expected_revision is not None
                and existing.revision != expected_revision
            ):
                # Symmetric revision guard: a user edited this row since
                # the agent read it — refuse to overwrite (same conflict
                # the user PUT path raises).
                raise ArtifactRevisionConflict(existing.revision)
            now = time.time()
            if existing is None:
                if artifact_id is None:
                    raise ValueError("new artifacts need an explicit id")
                stored = ArtifactRecord(
                    artifact_id=artifact_id,
                    run_id=run_id,
                    kind=kind,
                    session_id=session_id,
                    title=title,
                    status=status,
                    revision=1,
                    updated_by=updated_by,
                    content_markdown=content_markdown,
                    payload=dict(payload),
                    refs=tuple(dict(ref) for ref in refs),
                    created_at=now,
                    updated_at=now,
                )
            else:
                stored = replace(
                    existing,
                    run_id=run_id,
                    title=title,
                    status=status,
                    revision=existing.revision + 1,
                    updated_by=updated_by,
                    content_markdown=content_markdown,
                    payload=dict(payload),
                    refs=tuple(dict(ref) for ref in refs),
                    updated_at=now,
                )
            self._artifacts[stored.artifact_id] = stored
            self._revisions.setdefault(stored.artifact_id, []).append(
                ArtifactRevisionRecord(
                    artifact_id=stored.artifact_id,
                    revision=stored.revision,
                    created_by=updated_by,
                    content_markdown=content_markdown,
                    created_at=now,
                    status=status,
                )
            )
            # Read parity with Postgres: only the RETURNED record
            # carries the line delta — the stored row stays None so
            # later reads never re-serve a stale count.
            counted = count_line_changes(
                existing.content_markdown if existing is not None else "",
                content_markdown,
            )
            if counted is None:
                return stored
            return replace(
                stored, lines_added=counted[0], lines_removed=counted[1]
            )

    def _find_upsert_target_locked(
        self,
        *,
        artifact_id: str | None,
        session_id: str | None,
        run_id: str,
        kind: str,
    ) -> ArtifactRecord | None:
        if artifact_id is not None:
            return self._artifacts.get(artifact_id)
        if (
            session_id is not None
            and kind not in SESSION_SINGLETON_ARTIFACT_KINDS
        ):
            return None
        if session_id is None and kind not in RUN_SINGLETON_ARTIFACT_KINDS:
            return None
        for artifact in self._artifacts.values():
            if artifact.kind != kind:
                continue
            if session_id is not None:
                if artifact.session_id == session_id:
                    return artifact
            elif artifact.session_id is None and artifact.run_id == run_id:
                return artifact
        return None

    async def user_update_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        content_markdown: str,
        expected_revision: int,
        authorize: Any,
    ) -> ArtifactRecord:
        def _write(_transaction: Any, _cancel_child: Any) -> ArtifactRecord:
            with self._lock:
                artifact = self._artifacts.get(artifact_id)
                if artifact is None or artifact.run_id != run_id or is_private_archive(artifact.payload):
                    raise ArtifactNotFound(artifact_id)
                if artifact.kind == "context_archive" and is_exact_archive(artifact.payload):
                    raise ContextArchiveImmutable(artifact_id)
                if artifact.status == "writing":
                    raise ArtifactLocked(artifact_id)
                if artifact.revision != expected_revision:
                    raise ArtifactRevisionConflict(artifact.revision)
                now = time.time()
                stored = replace(
                    artifact,
                    revision=artifact.revision + 1,
                    updated_by="user",
                    content_markdown=content_markdown,
                    updated_at=now,
                )
                self._artifacts[artifact_id] = stored
                self._revisions.setdefault(artifact_id, []).append(
                    ArtifactRevisionRecord(
                        artifact_id=artifact_id,
                        revision=stored.revision,
                        created_by="user",
                        content_markdown=content_markdown,
                        created_at=now,
                        status="ready",
                    )
                )
                return stored

        return await authorize(_write)

    async def rename_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        title: str,
        authorize: Any,
    ) -> ArtifactRecord:
        def _write(_transaction: Any, _cancel_child: Any) -> ArtifactRecord:
            with self._lock:
                artifact = self._artifacts.get(artifact_id)
                if artifact is None or artifact.run_id != run_id or is_private_archive(artifact.payload):
                    raise ArtifactNotFound(artifact_id)
                if artifact.kind == "context_archive" and is_exact_archive(artifact.payload):
                    raise ContextArchiveImmutable(artifact_id)
                if artifact.status == "writing":
                    raise ArtifactLocked(artifact_id)
                # Metadata-only rename: no revision bump, no revision row.
                stored = replace(
                    artifact, title=title, updated_at=time.time()
                )
                self._artifacts[artifact_id] = stored
                return stored

        return await authorize(_write)

    async def get_artifact(
        self, run_id: str, artifact_id: str, *, revision: int | None = None,
        include_private: bool = False,
    ) -> tuple[ArtifactRecord, list[ArtifactRevisionRecord]]:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None or artifact.run_id != run_id:
                raise ArtifactNotFound(artifact_id)
            if is_private_archive(artifact.payload) and not include_private:
                raise ArtifactNotFound(artifact_id)
            if artifact.kind == "context_archive" and is_exact_archive(artifact.payload):
                if revision not in (None, 1):
                    raise ArtifactNotFound(artifact_id)
                return deepcopy(artifact), [ArtifactRevisionRecord(
                    artifact_id=artifact.artifact_id, revision=1, created_by=artifact.updated_by,
                    content_markdown="", created_at=artifact.created_at, status=artifact.status,
                )]
            revisions = list(self._revisions.get(artifact_id, ()))
            if revision is not None:
                matching = [r for r in revisions if r.revision == revision]
                if not matching:
                    raise ArtifactNotFound(artifact_id)
                artifact = replace(
                    artifact, content_markdown=matching[0].content_markdown
                )
            metadata = [
                replace(row, content_markdown="") for row in revisions
            ]
            metadata.sort(key=lambda row: row.revision, reverse=True)
            return artifact, metadata

    async def get_session_artifact(
        self, session_id: str, kind: str
    ) -> ArtifactRecord | None:
        with self._lock:
            for artifact in self._artifacts.values():
                if artifact.session_id == session_id and artifact.kind == kind:
                    return artifact
            return None

    async def get_session_artifact_by_id(
        self, session_id: str, artifact_id: str
    ) -> ArtifactRecord:
        with self._lock:
            artifact = self._artifacts.get(artifact_id)
            if artifact is None or artifact.session_id != session_id:
                raise ArtifactNotFound(artifact_id)
            return artifact

    async def revise_session_artifacts_atomically(
        self,
        *,
        run_id: str,
        session_id: str | None,
        revisions: list[ArtifactBatchRevision],
    ) -> list[ArtifactRecord]:
        with self._runtime_write_guard(run_id), self._lock:
            ids = [item.artifact_id for item in revisions]
            if len(ids) != len(set(ids)):
                raise ValueError("artifact revision batch contains duplicate ids")
            current: list[ArtifactRecord] = []
            for item in revisions:
                artifact = self._artifacts.get(item.artifact_id)
                if (artifact is None or artifact.session_id != session_id
                        or (session_id is None and artifact.run_id != run_id)):
                    raise ArtifactNotFound(item.artifact_id)
                if artifact.kind == "context_archive" and is_exact_archive(artifact.payload):
                    raise ContextArchiveImmutable(item.artifact_id)
                if artifact.revision != item.expected_revision:
                    raise ArtifactRevisionConflict(artifact.revision)
                current.append(artifact)
            now = time.time()
            stored_rows: list[ArtifactRecord] = []
            for item, artifact in zip(revisions, current, strict=True):
                stored = replace(
                    artifact,
                    run_id=run_id,
                    revision=artifact.revision + 1,
                    updated_by="agent",
                    content_markdown=item.content_markdown,
                    updated_at=now,
                )
                self._artifacts[stored.artifact_id] = stored
                self._revisions.setdefault(stored.artifact_id, []).append(
                    ArtifactRevisionRecord(
                        artifact_id=stored.artifact_id,
                        revision=stored.revision,
                        created_by="agent",
                        content_markdown=stored.content_markdown,
                        created_at=now,
                        status="ready",
                    )
                )
                counted = count_line_changes(
                    artifact.content_markdown, item.content_markdown
                )
                stored_rows.append(
                    stored
                    if counted is None
                    else replace(
                        stored,
                        lines_added=counted[0],
                        lines_removed=counted[1],
                    )
                )
            return stored_rows

    async def list_session_artifacts(
        self, session_id: str
    ) -> list[ArtifactRecord]:
        with self._lock:
            return sorted(
                (
                    replace(artifact, content_markdown="")
                    for artifact in self._artifacts.values()
                    if artifact.session_id == session_id
                ),
                key=lambda artifact: (
                    artifact.created_at,
                    artifact.artifact_id,
                ),
            )

    async def settle_terminal_control_rows(
        self, run_id: str
    ) -> tuple[int, int]:
        """Memory twin of the Postgres settle (idempotent CAS pair)."""
        now = time.time()
        with self._lock:
            has_candidates = any(
                artifact.run_id == run_id and artifact.status == "writing"
                for artifact in self._artifacts.values()
            ) or any(
                approval.run_id == run_id and approval.status == "pending"
                for approval in self._approvals.values()
            )
        if not has_candidates:
            # Settled steady state pays no authority guard per poll —
            # mirror of the Postgres probe-first short circuit.
            return 0, 0
        released = 0
        settled = 0
        with self._runtime_write_guard(run_id), self._lock:
            for artifact_id, artifact in list(self._artifacts.items()):
                if artifact.run_id == run_id and artifact.status == "writing":
                    self._artifacts[artifact_id] = replace(
                        artifact, status="ready", updated_at=now
                    )
                    released += 1
            for approval_id, approval in list(self._approvals.items()):
                if approval.run_id == run_id and approval.status == "pending":
                    self._approvals[approval_id] = replace(
                        approval,
                        status="rejected",
                        decision="",
                        note=TERMINAL_APPROVAL_SETTLE_NOTE,
                        decided_at=now,
                    )
                    settled += 1
        return released, settled

    async def list_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        limit: int = 50,
        after: tuple[float, str] | None = None,
        include_private: bool = False,
    ) -> tuple[list[ArtifactRecord], str | None]:
        with self._lock:
            rows = [
                replace(artifact, content_markdown="", payload=(
                    deepcopy(artifact.payload)
                    if artifact.kind == "context_archive" and is_exact_archive(artifact.payload)
                    else artifact.payload
                ))
                for artifact in self._artifacts.values()
                if artifact.run_id == run_id
                and (kind is None or artifact.kind == kind)
                and (include_private or not is_private_archive(artifact.payload))
            ]
        rows.sort(key=lambda a: (a.created_at, a.artifact_id), reverse=True)
        return keyset_page(
            rows,
            limit=limit,
            after=after,
            created_at_of=lambda a: a.created_at,
            id_of=lambda a: a.artifact_id,
        )

    async def aclose(self) -> None:
        """No-op; symmetric with the Postgres store."""
