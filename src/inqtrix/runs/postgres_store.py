"""Durable run store: records, events, and results in Postgres.

Same public surface as the in-memory
:class:`~inqtrix.server.runs.RunStore` — the runs router and
:class:`~inqtrix.services.run_service.RunService` cannot tell the
backends apart. Two execution modes:

* ``queue`` and ``dispatch_queue`` are both ``None``
  (``INQTRIX_STORAGE_BACKEND=postgres`` alone):
  records, events, and results are durable; execution stays in this
  process with the same thread-dispatch semantics as the memory store.
* an external dispatch queue set (``INQTRIX_QUEUE_BACKEND=valkey``): accepted runs are
  persisted and dispatched to the job stream; ``inqtrix-worker``
  processes claim and execute them. The run row is the source of
  truth — a lost stream message is recoverable from Postgres, and the
  guarded status transitions make at-least-once delivery idempotent.

The storage layer is async (asyncpg) while this surface is sync (the
routers and the run-handle call it from worker threads); the store
therefore owns one background event loop and funnels every database
operation through it. Sequence numbers are allocated via
``UPDATE runs SET event_seq = event_seq + 1 RETURNING event_seq`` —
serialized on the row lock, gap-free across processes, which is what
keeps the SSE stream byte-compatible with the in-memory store.
"""

from __future__ import annotations

import logging
import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from queue import Empty, Queue, SimpleQueue
from typing import TYPE_CHECKING, Any, Callable

from sqlalchemy import (
    Text,
    and_,
    any_,
    bindparam,
    case,
    delete,
    event,
    exists,
    false,
    func,
    insert,
    literal,
    or_,
    select,
    tuple_,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from inqtrix.storage.migration_contract import (
    assert_schema_head,
)
from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.auth.shares import ShareFacts
from inqtrix.constants import RUN_INCOGNITO_RETENTION_SECONDS
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.execution_failures import terminate_native_run
from inqtrix.observability.propagation import inject_traceparent
from inqtrix.runs.durable_store import (
    DEFAULT_TENANT,
    DurableJobStoreBase,
    PollingJobSubscription,
    _LocalJob,
)
from inqtrix.pagination import encode_cursor
from inqtrix.runs.ports import RunStoreMetrics
from inqtrix.runs.shared import (
    clipped_question,
    CHILD_PROGRESS_EVENT,
    access_annotation as _access_annotation,
    build_child_progress_payload,
    build_run_summary,
    expand_run_event,
    incognito_request,
    question_was_clipped,
    run_elapsed_seconds,
    run_segment_id,
    should_project_child_event,
)
from inqtrix.runtime_logging import new_run_id
from inqtrix.server.runs import (
    TERMINAL_RUN_STATUSES,
    WAITING_RUN_STATUSES,
    RunActive,
    RunHandle,
    RunNotFound,
    RunParentInactive,
    RunPerUserLimit,
    RunQueueFull,
    RunSessionActive,
    RunStatus,
    RunWork,
)
from inqtrix.storage.runs_orm import run_events, runs
from inqtrix.storage.agent_control_orm import (
    run_approvals,
    run_artifacts,
    run_clarifications,
    run_plan_tasks,
    run_plans,
)
from inqtrix.storage.agent_sessions_orm import agent_sessions
from inqtrix.storage.agent_memory_orm import agent_feedback, agent_memory_candidates
from inqtrix.storage.identity_orm import resource_shares, workspace_members
from inqtrix.storage.knowledge_sessions_orm import knowledge_sessions
from inqtrix.services.audit_service import build_audit_entry
from inqtrix.storage.resource_access import (
    append_resource_effects,
    lock_active_users,
    lock_resource_access,
    revoke_resource_shares,
)
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

    from inqtrix.auth.permissions import AuditSink
    from inqtrix.runs.valkey_queue import ValkeyRunQueue

log = logging.getLogger("inqtrix")

_TERMINAL_VALUES = tuple(status.value for status in TERMINAL_RUN_STATUSES)

_WAITING_VALUES = tuple(status.value for status in WAITING_RUN_STATUSES)

_ACTIVE_AGENT_SESSION_CONSTRAINT = "uq_runs_active_agent_session"

_STUCK_ROW_MAX_AGE_SECONDS = 7 * 86_400.0
"""Age at which a never-terminal row is force-failed regardless of mode —
the honest end of the line when every other recovery path missed it. The
terminal write starts the ordinary completed-TTL retention clock, so
request payloads (user conversation content) stay bounded: this cap plus
the terminal retention, exactly like any ordinarily failed run."""

_EXECUTION_LOST_ERROR = {
    "message": (
        "Execution of this run was lost; no process is running it."
    ),
    "type": "execution_lost",
}

_TERMINAL_EVENT_TYPES = frozenset(
    {"inqtrix.run.completed", "inqtrix.run.failed", "inqtrix.run.cancelled"}
)

_CLEANUP_HANDOFF_KEY = "inqtrix_run_cleanup_handoffs"


def _elapsed_sql(start_column: Any, now: float) -> Any:
    """Non-negative SQL duration from a nullable interval anchor."""
    return func.greatest(
        literal(0.0), literal(now) - func.coalesce(start_column, literal(now))
    )


def _terminal_timing_values(now: float) -> dict[str, Any]:
    """Close whichever runtime interval is open before terminalization."""
    return {
        "active_seconds": runs.c.active_seconds
        + case(
            (
                runs.c.status == RunStatus.RUNNING.value,
                _elapsed_sql(runs.c.active_started_at, now),
            ),
            else_=literal(0.0),
        ),
        "waiting_seconds": runs.c.waiting_seconds
        + case(
            (
                runs.c.status.in_(_WAITING_VALUES),
                _elapsed_sql(runs.c.waiting_since, now),
            ),
            else_=literal(0.0),
        ),
        "queued_seconds": runs.c.queued_seconds
        + case(
            (
                runs.c.status == RunStatus.QUEUED.value,
                _elapsed_sql(runs.c.queued_since, now),
            ),
            else_=literal(0.0),
        ),
        "active_started_at": None,
        "waiting_since": None,
        "queued_since": None,
    }


def _resume_reason_value(status: str) -> str:
    if status == RunStatus.WAITING_FOR_APPROVAL.value:
        return "approval"
    if status == RunStatus.WAITING_FOR_INPUT.value:
        return "input"
    if status == RunStatus.WAITING_FOR_CHILDREN.value:
        return "children"
    return "resume"


@dataclass(frozen=True)
class _RowView:
    """Flat view of one ``runs`` row for the shared summary builder."""

    run_id: str
    status: str
    question: str
    stack_name: str
    workspace_id: str | None
    mode: str
    agent_overrides: dict[str, Any]
    created_at: float
    started_at: float | None
    finished_at: float | None
    segment_count: int
    current_segment_id: str | None
    queued_since: float | None
    active_started_at: float | None
    active_seconds: float
    waiting_seconds: float
    queued_seconds: float
    waiting_since: float | None
    snapshot: dict[str, Any]
    error: dict[str, Any] | None
    kind: str = "standard"
    parent_run_id: str | None = None
    root_run_id: str | None = None
    session_id: str | None = None
    origin_key: str | None = None
    #: Internal work profile of a role child, lifted like ``origin_key``.
    work_profile: str | None = None
    cancel_requested: bool = False
    #: What the user actually asked, lifted from the durable payload.
    #: The column is clipped to keep list rows small; this never reaches
    #: a list response — only the single-run read publishes it.
    question_full: str | None = None
    #: The row's event allocator: the sequence of the last event this run
    #: emitted. It travels in the summary so a client holding both the
    #: summary and the event stream can tell which of the two is ahead.
    event_seq: int = 0


@dataclass(frozen=True)
class ClaimedRun:
    """Result of a successful worker claim on a queued run."""

    run_id: str
    tenant_id: str
    attempt: int
    request_payload: dict[str, Any]
    workspace_id: str | None = None
    kind: str = "standard"
    # Persisted attribution of the submitter, so the worker can meter
    # the run against the right quota subject without a live principal.
    created_by_user_id: uuid.UUID | None = None
    created_by_tenant_id: str | None = None
    execution_actor_user_id: uuid.UUID | None = None
    execution_scopes: tuple[str, ...] = ()


class PostgresRunStore(DurableJobStoreBase):
    """Durable run registry with the in-memory store's public surface.

    Args:
        engine: Async engine OWNED by this store. asyncpg pools are
            event-loop-affine: every connection must live on the
            store's background loop, so the engine is deliberately NOT
            shared with the identity/file backends (which run on the
            HTTP server's loop) — sharing one pool across loops
            corrupts connections.
        app_role: Restricted database role assumed per transaction
            (``SET LOCAL ROLE``) so forced RLS applies.
        queue: Job queue for worker dispatch; ``None`` keeps execution
            ownership in this process. The claim-mode worker deliberately
            leaves this ``None`` because it claims through
            :class:`WorkerLoop`.
        dispatch_queue: Optional external queue used only for submissions,
            resumes, and child-triggered parent wakes. Defaults to *queue*;
            the claim-mode worker passes Valkey here while keeping *queue*
            ``None`` so ownership/fencing semantics do not change.
        max_concurrent: In-process execution slots (no-queue mode) and
            part of the queue-saturation formula.
        max_queue_size: Waiting-run bound; exceeding it with all slots
            busy rejects submissions (HTTP 429 upstream).
        completed_ttl_seconds: Retention for terminal runs; lazy
            cleanup deletes older rows (events cascade).
        incognito_ttl_seconds: Retention for terminal incognito knowledge
            runs; the same cleanup deletes them after this shorter window.
        worker_id: Identity stamped into ``claimed_by`` for runs this
            process executes or fences.
        audit: Optional audit sink; visibility denials are persisted
            here in addition to the operator log line. The in-memory
            store deliberately logs denials only: it is sync/threaded,
            the audit sink is async.
        recover_orphans: Whether this instance may blanket-fail
            queued/running rows left by a previous process. ``None``
            infers from ``queue`` (no-queue single API process sweeps,
            queue mode never); the queue-backed WORKER passes an
            explicit ``False`` because its ``queue=None`` is claim-mode
            wiring and stream reclaim owns crash recovery there.

    Tenancy: run rows live in the single deployment tenant
    (``default``) at the RLS layer — exactly like the in-memory store,
    per-user visibility is the ``(created_by_user_id, created_by_tenant_id)``
    predicate, never the row tenant.
    """

    _loop_thread_name = "inqtrix-runs-db"
    _dispatch_thread_prefix = "inqtrix-run"
    _job_kind = "Native run"

    def __init__(
        self,
        *,
        engine: "AsyncEngine",
        app_role: str,
        queue: "ValkeyRunQueue | None" = None,
        dispatch_queue: "ValkeyRunQueue | None" = None,
        max_concurrent: int,
        max_queue_size: int,
        completed_ttl_seconds: int,
        worker_id: str,
        audit: "AuditSink | None" = None,
        waiting_ttl_seconds: float = 7 * 24 * 3600.0,
        queued_ttl_seconds: float = 24 * 3600.0,
        max_concurrent_per_user: int | None = None,
        restrict_to_workspace_members: bool = False,
        sharing_enabled: bool = True,
        recover_orphans: bool | None = None,
        audit_service_starts: bool = True,
        checkpoint_cleanup: Callable[[str, str], None] | None = None,
        incognito_ttl_seconds: int = RUN_INCOGNITO_RETENTION_SECONDS,
    ) -> None:
        # Validate BEFORE super().__init__ spawns the background loop
        # thread and adopts the engine — a config error must not leak
        # either.
        if float(waiting_ttl_seconds) <= 0:
            raise ValueError(
                f"waiting_ttl_seconds must be > 0, got {waiting_ttl_seconds}"
            )
        if float(queued_ttl_seconds) <= 0:
            raise ValueError(
                f"queued_ttl_seconds must be > 0, got {queued_ttl_seconds}"
            )
        # The engine/session/loop/dispatch plumbing lives in
        # DurableJobStoreBase; this store adds only its sizing,
        # retention, and audit state.
        super().__init__(
            engine=engine,
            app_role=app_role,
            worker_id=worker_id,
            queue=queue,
            recover_orphans=recover_orphans,
        )
        self._max_concurrent = max_concurrent
        self._dispatch_queue = dispatch_queue if dispatch_queue is not None else queue
        self._max_queue_size = max_queue_size
        self._completed_ttl_seconds = completed_ttl_seconds
        self._incognito_ttl_seconds = incognito_ttl_seconds
        self._audit = audit
        self._audit_service_starts = bool(audit_service_starts)
        self._checkpoint_cleanup = checkpoint_cleanup
        self._waiting_ttl_seconds = float(waiting_ttl_seconds)
        self._queued_ttl_seconds = float(queued_ttl_seconds)
        self._sharing_enabled = sharing_enabled
        self._max_concurrent_per_user = max_concurrent_per_user
        self._restrict_to_workspace_members = restrict_to_workspace_members
        # TTL-swept waiting run ids, handed over from the cleanup
        # coroutine (which must never take self._lock — see
        # _cleanup_db) to the sync surface, which releases their
        # retained no-queue closures in _release_swept_locals().
        self._swept_waiting: SimpleQueue[str] = SimpleQueue()
        # Parents flipped waiting_for_children -> queued by a child's
        # terminal write (or the park-time self-heal). Same handoff
        # pattern as _swept_waiting: the coroutine only records the id
        # post-commit, the sync mutators perform the actual dispatch in
        # _dispatch_woken_parents() — never from the store loop.
        self._parents_to_wake: SimpleQueue[str] = SimpleQueue()
        # Coroutine -> sync-mutator handoff for the parent-failure child
        # cascade (a coroutine must NEVER take self._lock — the
        # documented deadlock class): _terminal_db(FAILED) enqueues
        # (root_id, cascaded_child_ids), the calling mutator drains and
        # projects the cancellations into local work handles.
        self._failed_cascades: SimpleQueue[tuple[str, tuple[str, ...]]] = SimpleQueue()
        # The restart sweep runs eagerly so orphans of the previous
        # process are terminal before the first client read. A failure
        # keeps the one-shot flag set — the lazy first-cleanup fallback
        # remains, so startup gains no new hard dependency.
        if self._sweep_orphans:
            try:
                self.sweep()
            except Exception:  # noqa: BLE001 — lazy cleanup remains
                log.warning(
                    "Startup cleanup failed; it will be retried on the next database "
                    "access.",
                    exc_info=True,
                )

    def _release_swept_locals(self) -> None:
        """Apply post-commit handoffs produced by lazy cleanup.

        The cleanup transaction records swept ids and parents to wake only
        from SQLAlchemy's ``after_commit`` hook. The synchronous caller may
        therefore safely release retained closures and dispatch resumed
        parents without observing state that later rolls back.
        """
        swept: list[str] = []
        while True:
            try:
                swept.append(self._swept_waiting.get_nowait())
            except Empty:
                break
        if swept:
            with self._lock:
                for run_id in swept:
                    local = self._local.pop(run_id, None)
                    if local is not None:
                        local.cancel_event.set()
                        local.work = None
        self._dispatch_woken_parents()

    def _register_cleanup_handoffs(
        self,
        session: "AsyncSession",
        *,
        swept_run_ids: list[str],
        woken_parent_ids: list[str],
    ) -> None:
        """Publish cleanup side effects only after its transaction commits."""
        if not swept_run_ids and not woken_parent_ids:
            return
        sync_session = session.sync_session
        pending = sync_session.info.setdefault(
            _CLEANUP_HANDOFF_KEY,
            {"swept": [], "parents": [], "registered": False},
        )
        pending["swept"].extend(swept_run_ids)
        pending["parents"].extend(woken_parent_ids)
        if pending["registered"]:
            return
        pending["registered"] = True

        @event.listens_for(sync_session, "after_commit", once=True)
        def _publish(committed_session: Any) -> None:
            committed = committed_session.info.pop(_CLEANUP_HANDOFF_KEY, {})
            for run_id in committed.get("swept", ()):
                self._swept_waiting.put(run_id)
            for parent_id in committed.get("parents", ()):
                self._parents_to_wake.put(parent_id)

    def _dispatch_woken_parents(self) -> None:
        """Dispatch parents whose last child's terminal write woke them.

        Also drains the parent-failure child cascade first — every
        terminal-landing sync mutator passes through here, making it the
        ONE chokepoint where committed cascades project into local
        handles.

        The DB rows already flipped ``waiting_for_children -> queued``
        inside the child's terminal transaction (or the park-time
        self-heal); this performs the mode-appropriate dispatch on the
        SYNC surface — the store loop must never take ``self._lock``
        (see ``_cleanup_db``) nor block on a queue round-trip.

        Queue mode enqueues a fresh dispatch message (any worker resumes
        from payload + checkpoint); a lost enqueue is healed by the
        reconciler, exactly like :meth:`resume_run`. No-queue mode
        re-appends the retained closure, honoring the park handshake —
        and a parent whose closure is gone (process restarted while
        parked) is logged loudly: it cannot resume in-process and ends
        via the waiting/stuck lifecycle, never silently.
        """
        self._drain_failed_cascades()
        while True:
            try:
                parent_id = self._parents_to_wake.get_nowait()
            except Empty:
                break
            if self._dispatch_queue is not None:
                try:
                    self._dispatch_queue.enqueue(
                        run_id=parent_id, tenant_id=DEFAULT_TENANT
                    )
                except Exception:  # noqa: BLE001 — row committed; reconciler heals
                    log.warning(
                        "Child-completion dispatch for parent run %s could not be sent;"
                        " the reconciler will retry.",
                        parent_id,
                        exc_info=True,
                    )
                continue
            with self._lock:
                local = self._local.get(parent_id)
                if local is None or local.work is None:
                    log.warning(
                        "Parent run %s was woken by its children, but no execution "
                        "remains after a restart in no-queue mode; the run cannot "
                        "resume.",
                        parent_id,
                    )
                    continue
                if local.park_in_flight:
                    local.resume_requested = True
                else:
                    local.parked = False
                    self._pending.append(parent_id)
                    self._dispatch_locked()

    # -- public surface (RunStore parity) -------------------------------- #

    def submit(
        self,
        *,
        question: str,
        stack_name: str,
        work: RunWork,
        agent_overrides: dict[str, Any] | None = None,
        mode: str = "research",
        workspace_id: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        created_by_tenant_id: str | None = None,
        execution_scopes: frozenset[str] = frozenset(),
        request_payload: dict[str, Any] | None = None,
        kind: str = "standard",
        parent_run_id: str | None = None,
        root_run_id: str | None = None,
        session_id: str | None = None,
        origin_key: str | None = None,
    ) -> dict[str, Any]:
        """Persist one queued run, then dispatch locally or enqueue.

        ``origin_key`` needs no column here: the run service embeds it
        into ``request_payload["body"]`` and the row view lifts it back
        out for summaries.

        Raises:
            RunQueueFull: When the waiting queue is full and every
                execution slot is busy (queue-mode counts are
                cluster-wide via the database).
        """
        # Best-effort fence before admission (bounded by grace +
        # throttle): a lost execution should stop holding a concurrency
        # slot against new submissions within that window.
        self._expire_lost_executions()
        durable_payload = dict(request_payload or {})
        # Carry the submitter's trace context in the run row (W3C
        # traceparent): the queue message stays (run_id, tenant_id), the
        # worker extracts from the row — one trace across the boundary.
        inject_traceparent(durable_payload)
        if origin_key:
            body = dict(durable_payload.get("body") or {})
            body["origin_key"] = origin_key
            durable_payload["body"] = body
        summary, created = self._call(
            self._submit_db(
                tenant_id=DEFAULT_TENANT,
                question=clipped_question(question),
                stack_name=stack_name,
                agent_overrides=dict(agent_overrides or {}),
                mode=mode,
                workspace_id=workspace_id,
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
                execution_actor_user_id=created_by_user_id,
                execution_scopes=sorted(execution_scopes),
                request_payload=durable_payload or None,
                kind=kind,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                session_id=session_id,
                # The retention sweep's index of the flag the run service
                # put into the request; written once, never updated.
                incognito=incognito_request(durable_payload),
            )
        )
        self._release_swept_locals()
        if not created:
            return summary
        run_id = summary["run_id"]
        if self._dispatch_queue is not None:
            try:
                self._dispatch_queue.enqueue(run_id=run_id, tenant_id=DEFAULT_TENANT)
            except Exception:  # noqa: BLE001 — row is committed; visible
                # The run row is the source of truth: the worker
                # reconciler re-dispatches stale queued rows, so a
                # broker blip must not turn an accepted run into a 500.
                log.warning(
                    "Dispatch message for run %s could not be sent; the reconciler will"
                    " retry.",
                    run_id,
                    exc_info=True,
                )
        else:
            with self._lock:
                self._local[run_id] = _LocalJob(work=work)
                self._pending.append(run_id)
                self._dispatch_locked()
        return summary

    def import_completed_run(
        self,
        *,
        source_run_id: str,
        question: str,
        stack_name: str,
        result: dict[str, Any],
        status: str = "completed",
        mode: str = "research",
        agent_overrides: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        created_at: float | None = None,
        workspace_id: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        created_by_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an already-terminal imported run durably. See the port.

        Never queues or dispatches — the row is written terminal in one shot.

        Raises:
            ValueError: When *status* is not a terminal status.
        """
        if status not in _TERMINAL_VALUES:
            raise ValueError(
                f"import_completed_run requires a terminal status, got {status!r}"
            )
        source_run_id = source_run_id.strip()
        if not source_run_id:
            raise ValueError("source_run_id must not be empty")
        if len(source_run_id) > 255:
            raise ValueError("source_run_id must not exceed 255 characters")
        summary = self._call(
            self._import_completed_run_db(
                tenant_id=DEFAULT_TENANT,
                source_run_id=source_run_id,
                question=clipped_question(question),
                stack_name=stack_name,
                result=result,
                status=status,
                mode=mode,
                agent_overrides=dict(agent_overrides or {}),
                snapshot=dict(snapshot or {}),
                error=dict(error) if error else None,
                created_at=created_at,
                workspace_id=workspace_id,
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
            )
        )
        self._release_swept_locals()
        return summary

    def get(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return a public summary for *run_id*."""
        self._expire_lost_executions()
        summary = self._call(self._summary_db(run_id, workspace_id, visible_to))
        self._release_swept_locals()
        return summary

    def list(
        self,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Return public summaries, newest first (unbounded)."""
        self._expire_lost_executions()
        summaries = self._call(self._list_db(workspace_id, visible_to))
        self._release_swept_locals()
        return summaries

    def list_session_runs(
        self,
        session_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Visible summaries of one agent session, oldest first."""
        summaries = self._call(self._list_session_runs_db(session_id, visible_to))
        self._release_swept_locals()
        return summaries

    def session_owners(self, session_id: str) -> set[tuple[str | None, str | None]]:
        """Return every recorded owner identity for ``session_id``."""
        return self._call(self._session_owners_db(session_id))

    def delete_agent_session_aggregate(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        """Remove the fenced run lineage and every run-owned child row."""

        self._delete_session_aggregate(
            session_id,
            session_kind="agent",
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def delete_knowledge_session_aggregate(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        """Remove the fenced Knowledge runs and every run-owned child row."""

        self._delete_session_aggregate(
            session_id,
            session_kind="knowledge",
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def _delete_session_aggregate(
        self,
        session_id: str,
        *,
        session_kind: str,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        with self._lock:
            for run_id in run_ids:
                local = self._local.get(run_id)
                if local is not None:
                    local.cancel_event.set()
        self._call(
            self._delete_session_aggregate_db(
                session_id,
                session_kind=session_kind,
                tenant_id=tenant_id,
                requester_user_id=requester_user_id,
                workspace_id=workspace_id,
                run_ids=run_ids,
            )
        )
        self._release_swept_locals()

    def prepare_agent_session_aggregate_deletion(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        """Request cancellation and prove no worker can still checkpoint.

        Running work receives a durable cancel request and this attempt stops
        visibly. A retry may remove checkpoints only after every run reached a
        terminal state, preventing a late worker checkpoint from surviving the
        session deletion.
        """

        self._prepare_session_aggregate_deletion(
            session_id,
            session_kind="agent",
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def prepare_knowledge_session_aggregate_deletion(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        """Fence saved Knowledge runs before their session is removed."""

        self._prepare_session_aggregate_deletion(
            session_id,
            session_kind="knowledge",
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def _prepare_session_aggregate_deletion(
        self,
        session_id: str,
        *,
        session_kind: str,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        remaining = self._call(
            self._prepare_session_aggregate_deletion_db(
                session_id,
                session_kind=session_kind,
                tenant_id=tenant_id,
                requester_user_id=requester_user_id,
                workspace_id=workspace_id,
                run_ids=run_ids,
            )
        )
        with self._lock:
            for run_id in run_ids:
                local = self._local.get(run_id)
                if local is not None:
                    local.cancel_event.set()
        if remaining:
            raise RuntimeError(
                f"{session_kind} session runs are still stopping; "
                "retry deletion after they finish"
            )

    async def _prepare_session_aggregate_deletion_db(
        self,
        session_id: str,
        *,
        session_kind: str,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        expected = tuple(dict.fromkeys(run_ids))
        if not expected:
            return ()
        async with self._session(tenant_id) as session:
            rows = (
                (
                    await session.execute(
                        select(
                            runs.c.run_id,
                            runs.c.session_id,
                            runs.c.root_run_id,
                            runs.c.mode,
                            runs.c.kind,
                            runs.c.created_by_tenant_id,
                            runs.c.created_by_user_id,
                            runs.c.workspace_id,
                            runs.c.status,
                        )
                        .where(
                            runs.c.tenant_id == tenant_id,
                            runs.c.run_id.in_(expected),
                        )
                        .order_by(runs.c.created_at, runs.c.run_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .all()
            )
            session_roots = {
                str(row["run_id"]) for row in rows if row["session_id"] == session_id
            }
            for row in rows:
                if (
                    row["created_by_tenant_id"] not in (None, tenant_id)
                    or row["created_by_user_id"] != requester_user_id
                    or row["workspace_id"] != workspace_id
                ):
                    raise RuntimeError(
                        f"{session_kind} session contains a run outside its owner scope"
                    )
                if session_kind == "agent":
                    if (
                        row["session_id"] != session_id
                        and row["root_run_id"] not in session_roots
                    ):
                        raise RuntimeError(
                            "agent session contains an unrelated run lineage"
                        )
                elif (
                    row["session_id"] != session_id
                    or row["mode"] != "knowledge"
                    or row["kind"] != "standard"
                ):
                    raise RuntimeError(
                        "knowledge session contains an unrelated run"
                    )
                if row["status"] not in _TERMINAL_VALUES:
                    await self._cancel_row_db(
                        session,
                        str(row["run_id"]),
                        tenant_id,
                        str(row["status"]),
                        cascade_reason="session_deleting",
                    )
            remaining = tuple(
                (
                    await session.execute(
                        select(runs.c.run_id).where(
                            runs.c.tenant_id == tenant_id,
                            runs.c.run_id.in_(expected),
                            runs.c.status.not_in(_TERMINAL_VALUES),
                        )
                    )
                ).scalars()
            )
            return remaining

    async def _delete_session_aggregate_db(
        self,
        session_id: str,
        *,
        session_kind: str,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> None:
        expected = tuple(dict.fromkeys(run_ids))
        async with self._session(tenant_id) as session:
            current_session_ids = set(
                (
                    await session.execute(
                        select(runs.c.run_id).where(
                            runs.c.tenant_id == tenant_id,
                            runs.c.session_id == session_id,
                        )
                    )
                ).scalars()
            )
            if not current_session_ids.issubset(set(expected)):
                raise RuntimeError(
                    f"{session_kind} session gained a run after its deletion fence"
                )
            rows = (
                (
                    await session.execute(
                        select(
                            runs.c.run_id,
                            runs.c.session_id,
                            runs.c.root_run_id,
                            runs.c.mode,
                            runs.c.kind,
                            runs.c.created_by_tenant_id,
                            runs.c.created_by_user_id,
                            runs.c.workspace_id,
                            runs.c.status,
                        ).where(runs.c.run_id.in_(expected))
                    )
                )
                .mappings()
                .all()
                if expected
                else []
            )
            for row in rows:
                if (
                    row["created_by_tenant_id"] not in (None, tenant_id)
                    or row["created_by_user_id"] != requester_user_id
                    or row["workspace_id"] != workspace_id
                ):
                    raise RuntimeError(
                        f"{session_kind} session contains a run outside its owner scope"
                    )
                if session_kind == "agent":
                    if (
                        row["session_id"] != session_id
                        and row["root_run_id"] not in current_session_ids
                    ):
                        raise RuntimeError(
                            "agent session contains an unrelated run lineage"
                        )
                elif (
                    row["session_id"] != session_id
                    or row["mode"] != "knowledge"
                    or row["kind"] != "standard"
                ):
                    raise RuntimeError(
                        "knowledge session contains an unrelated run"
                    )
                if row["status"] not in _TERMINAL_VALUES:
                    raise RuntimeError(
                        f"{session_kind} session run is still active "
                        "after its deletion fence"
                    )
            present_ids = tuple(row["run_id"] for row in rows)
            if not present_ids:
                return
            await session.execute(
                delete(agent_memory_candidates).where(
                    agent_memory_candidates.c.tenant_id == tenant_id,
                    agent_memory_candidates.c.user_id == requester_user_id,
                    agent_memory_candidates.c.source_run_id.in_(present_ids),
                )
            )
            await session.execute(
                delete(agent_feedback).where(
                    agent_feedback.c.tenant_id == tenant_id,
                    agent_feedback.c.user_id == requester_user_id,
                    agent_feedback.c.run_id.in_(present_ids),
                )
            )
            for row in rows:
                run_id = str(row["run_id"])
                recipients = await revoke_resource_shares(
                    session,
                    tenant_id=tenant_id,
                    resource_type="run",
                    resource_id=run_id,
                    revoked_by_user_id=requester_user_id,
                )
                await append_resource_effects(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=requester_user_id,
                    owner_user_id=row["created_by_user_id"],
                    action="run.deleted",
                    resource_type="run",
                    resource_id=run_id,
                    scope="runs",
                    additional_targets=recipients,
                )
            await session.execute(
                delete(runs).where(
                    runs.c.tenant_id == tenant_id,
                    runs.c.run_id.in_(present_ids),
                )
            )

    def agent_session_residuals(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> dict[str, int]:
        return self._session_aggregate_residuals(
            session_id,
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def knowledge_session_residuals(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> dict[str, int]:
        return self._session_aggregate_residuals(
            session_id,
            tenant_id=tenant_id,
            requester_user_id=requester_user_id,
            workspace_id=workspace_id,
            run_ids=run_ids,
        )

    def _session_aggregate_residuals(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        workspace_id: str | None,
        run_ids: tuple[str, ...],
    ) -> dict[str, int]:
        del workspace_id
        return self._call(
            self._session_aggregate_residuals_db(
                session_id,
                tenant_id=tenant_id,
                requester_user_id=requester_user_id,
                run_ids=run_ids,
            )
        )

    async def _session_aggregate_residuals_db(
        self,
        session_id: str,
        *,
        tenant_id: str,
        requester_user_id: uuid.UUID | None,
        run_ids: tuple[str, ...],
    ) -> dict[str, int]:
        ids = tuple(dict.fromkeys(run_ids))
        async with self._session(tenant_id) as session:

            async def count(table, condition) -> int:
                return int(
                    await session.scalar(
                        select(func.count()).select_from(table).where(condition)
                    )
                    or 0
                )

            false_condition = false()
            run_condition = runs.c.run_id.in_(ids) if ids else false_condition
            controls = (
                ("events", run_events, run_events.c.run_id),
                ("plans", run_plans, run_plans.c.run_id),
                ("tasks", run_plan_tasks, run_plan_tasks.c.run_id),
                ("approvals", run_approvals, run_approvals.c.run_id),
                ("clarifications", run_clarifications, run_clarifications.c.run_id),
            )
            residuals = {
                "runs": await count(
                    runs,
                    or_(runs.c.session_id == session_id, run_condition),
                ),
                "artifacts": await count(
                    run_artifacts,
                    or_(
                        run_artifacts.c.session_id == session_id,
                        run_artifacts.c.run_id.in_(ids) if ids else false_condition,
                    ),
                ),
                "memory_candidates": await count(
                    agent_memory_candidates,
                    and_(
                        agent_memory_candidates.c.tenant_id == tenant_id,
                        agent_memory_candidates.c.user_id == requester_user_id,
                        (
                            agent_memory_candidates.c.source_run_id.in_(ids)
                            if ids
                            else false_condition
                        ),
                    ),
                ),
                "feedback": await count(
                    agent_feedback,
                    and_(
                        agent_feedback.c.tenant_id == tenant_id,
                        agent_feedback.c.user_id == requester_user_id,
                        agent_feedback.c.run_id.in_(ids) if ids else false_condition,
                    ),
                ),
                "shares": await count(
                    resource_shares,
                    and_(
                        resource_shares.c.tenant_id == tenant_id,
                        resource_shares.c.resource_type == "run",
                        (
                            resource_shares.c.resource_id.in_(ids)
                            if ids
                            else false_condition
                        ),
                    ),
                ),
            }
            for name, table, column in controls:
                residuals[name] = await count(
                    table, column.in_(ids) if ids else false_condition
                )
            return residuals

    async def _session_owners_db(
        self, session_id: str
    ) -> set[tuple[str | None, str | None]]:
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                await session.execute(
                    select(
                        runs.c.created_by_tenant_id,
                        runs.c.created_by_user_id,
                    )
                    .where(runs.c.session_id == session_id)
                    .distinct()
                )
            ).all()
            return {(row[0], row[1]) for row in rows}

    async def _list_session_runs_db(
        self,
        session_id: str,
        visible_to: "UserContext | None",
    ) -> list[dict[str, Any]]:
        async with self._session(DEFAULT_TENANT) as session:
            query = self._apply_run_visibility(
                _summary_select()
                .where(runs.c.session_id == session_id)
                .order_by(runs.c.created_at.asc(), runs.c.run_id.asc()),
                None,
                visible_to,
            )
            rows = (await session.execute(query)).mappings().all()
            summaries: list[dict[str, Any]] = []
            for row in rows:
                shared = (
                    SharePermission(row["_share_permission"])
                    if row.get("_share_permission") is not None
                    and not _visible_row(row, visible_to)
                    else None
                )
                summaries.append(
                    build_run_summary(
                        _summary_view(row),
                        queue_position=None,
                        access=_access_annotation(
                            shared,
                            owner_user_id=row["created_by_user_id"],
                        ),
                    )
                )
            return summaries

    def list_page(
        self,
        *,
        limit: int,
        after: tuple[float, str] | None = None,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One keyset page of visible summaries + the next cursor.

        Bounded read for the HTTP list endpoint (2.2): the durable table
        is 90-day-retained, so an active user's history is otherwise
        materialised whole on every poll. Keyed on the existing
        ``ix_runs_tenant_created_id`` (created_at, run_id) index.
        """
        result = self._call(self._list_page_db(workspace_id, visible_to, limit, after))
        self._release_swept_locals()
        return result

    def metrics_snapshot(self) -> RunStoreMetrics:
        """Current QUEUED/RUNNING counts for the ``/metrics`` collector.

        ``capacity`` is ``None``: in the durable topology the worker
        fleet owns the execution slots, not this API process, so there is
        no single in-process ceiling to report.
        """
        return self._call(self._metrics_snapshot_db())

    async def _metrics_snapshot_db(self) -> RunStoreMetrics:
        async with self._session("default") as session:
            rows = (
                await session.execute(
                    select(runs.c.status, func.count())
                    .where(
                        runs.c.status.in_(
                            (
                                RunStatus.QUEUED.value,
                                RunStatus.RUNNING.value,
                            )
                        )
                    )
                    .group_by(runs.c.status)
                )
            ).all()
        counts = {status: count for status, count in rows}
        return RunStoreMetrics(
            queued=counts.get(RunStatus.QUEUED.value, 0),
            active=counts.get(RunStatus.RUNNING.value, 0),
            capacity=None,
        )

    def owner_user_id(self, run_id: str) -> uuid.UUID | None:
        """The run's creator regardless of visibility (share layer)."""
        return self._call(self._owner_user_id_db(run_id))

    def share_facts(self, run_ids: Sequence[str]) -> dict[str, ShareFacts]:
        """Creator and question of each listed run regardless of visibility
        (share layer), in one read; unknown runs are absent."""
        return self._call(self._share_facts_db(sorted(set(run_ids))))

    def events_snapshot(
        self, run_id: str, *, after: int = 0
    ) -> list[dict[str, Any]]:
        """Durable event rows for the admin run drawer (visibility-free).

        Authorization happens at the instance-admin boundary before this
        lookup (``owner_user_id`` precedent). Row shape matches the
        owner SSE replay: ``{type, run_id, sequence, created_at, data}``.
        """
        return self._events_after(run_id, DEFAULT_TENANT, int(after))

    def trace_id(self, run_id: str) -> str | None:
        """Latest ``inqtrix.run.trace`` event's trace id (admin surface).

        Not visibility-gated — authorization happens at the instance-
        admin boundary before this lookup (``owner_user_id`` precedent).
        Targeted query: long runs carry thousands of event rows and this
        needs exactly one of them.
        """
        return self._call(self._trace_id_db(run_id))

    async def _trace_id_db(self, run_id: str) -> str | None:
        async with self._session(DEFAULT_TENANT) as session:
            rows = await session.execute(
                select(run_events.c.data)
                .where(
                    run_events.c.run_id == run_id,
                    run_events.c.type == "inqtrix.run.trace",
                )
                .order_by(run_events.c.sequence.desc())
                .limit(1)
            )
            row = rows.first()
        if row is None:
            return None
        value = str((row[0] or {}).get("trace_id") or "")
        return value or None

    def execution_request_body(self, run_id: str) -> dict[str, Any]:
        """Return the run's persisted execution body without exposing it."""
        return self._call(self._execution_request_body_db(run_id))

    async def _execution_request_body_db(self, run_id: str) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            row = (
                await session.execute(
                    select(runs.c.request_payload).where(runs.c.run_id == run_id)
                )
            ).first()
        if row is None:
            raise RunNotFound(run_id)
        payload = row[0] or {}
        if not isinstance(payload, dict):
            raise RuntimeError("Persisted run request payload is invalid.")
        body = payload.get("body")
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise RuntimeError("Persisted run request body is invalid.")
        return dict(body)

    def source_request_body(self, run_id: str, *, visible_to, workspace_id=None) -> dict[str, Any]:
        """Read source provenance using an owner-authorized storage statement."""
        return self._call(self._source_request_body_db(run_id, visible_to, workspace_id))

    async def _source_request_body_db(self, run_id, visible_to, workspace_id):
        async with self._session(DEFAULT_TENANT) as session:
            row, shared = await self._visible_row_db(session, run_id, workspace_id, visible_to)
            if shared is not None:
                raise RunNotFound(run_id)
            body = (row["request_payload"] or {}).get("body") or {}
            if not isinstance(body, dict):
                raise RuntimeError("Persisted run request body is invalid.")
            return dict(body)

    def source_run_catalogue(self, session_id: str, *, visible_to, workspace_id=None) -> list[dict[str, Any]]:
        """Discover originals without loading results or complete requests."""
        return self._call(self._source_run_catalogue_db(session_id, visible_to, workspace_id))

    async def _source_run_catalogue_db(self, session_id, visible_to, workspace_id):
        async with self._session(DEFAULT_TENANT) as session:
            query = select(runs.c.run_id, runs.c.question, runs.c.kind, runs.c.mode,
                           runs.c.status, runs.c.created_at).where(runs.c.session_id == session_id)
            # An internal role run is no conversation source.
            query = query.where(func.coalesce(func.json_extract_path_text(
                runs.c.request_payload, "body", "work_profile"), "") == "")
            query = query.where(
                and_(runs.c.created_by_user_id == visible_to.principal.user_id,
                     runs.c.created_by_tenant_id == visible_to.principal.tenant_id)
                if visible_to is not None else runs.c.created_by_user_id.is_(None)
            )
            if workspace_id is not None:
                query = query.where(runs.c.workspace_id == workspace_id)
            rows = (await session.execute(query.order_by(runs.c.created_at, runs.c.run_id))).mappings().all()
            return [dict(row) for row in rows]

    def source_origins_visible(self, run_ids: list[str], *, visible_to, owner_only: bool = False) -> bool:
        """Check current source grants in bounded metadata-only batches."""
        return self._call(self._source_origins_visible_db(run_ids, visible_to, owner_only))

    async def _source_origins_visible_db(self, run_ids, visible_to, owner_only):
        ids = sorted(set(run_ids))
        if not ids:
            return True
        async with self._session(DEFAULT_TENANT) as session:
            for start in range(0, len(ids), 500):
                batch = ids[start:start + 500]
                query = self._apply_run_visibility(
                    select(runs.c.run_id).where(runs.c.run_id.in_(batch)), None, visible_to,
                )
                if owner_only:
                    query = query.where(
                        and_(runs.c.created_by_user_id == visible_to.principal.user_id,
                             runs.c.created_by_tenant_id == visible_to.principal.tenant_id)
                        if visible_to is not None else runs.c.created_by_user_id.is_(None)
                    )
                found = set((await session.execute(query)).scalars().all())
                if found != set(batch):
                    return False
        return True

    def execution_principal(
        self,
        run_id: str,
        *,
        fallback: Principal | None = None,
    ) -> Principal | None:
        """Reconstruct the persisted actor for an in-process segment."""
        row = self._call(self._execution_authority_db(run_id))
        actor_user_id, tenant_id, scopes = row
        if actor_user_id is None:
            return fallback
        return Principal(
            user_id=uuid.UUID(str(actor_user_id)),
            kind="oidc_session",
            tenant_id=tenant_id or DEFAULT_TENANT,
            role="member",
            scopes=frozenset(scopes or ()),
        )

    def total_elapsed_seconds(self, run_id: str) -> float:
        """Return worker-visible wall time from the internal run row."""

        return self._call(self._total_elapsed_seconds_db(run_id))

    def check_execution_authority(self, run_id: str) -> None:
        """Assert the persisted actor still has live edit access."""
        try:
            self._call(self._check_execution_authority_db(run_id))
        except RunNotFound as exc:
            raise AuthorizationRevoked(
                "run execution authority is missing or revoked"
            ) from exc

    async def _execution_authority_db(
        self, run_id: str
    ) -> tuple[uuid.UUID | None, str | None, tuple[str, ...]]:
        async with self._session(DEFAULT_TENANT) as session:
            pointer = (
                await session.execute(
                    select(
                        runs.c.root_run_id,
                        runs.c.execution_actor_user_id,
                        runs.c.created_by_tenant_id,
                        runs.c.execution_scopes,
                    ).where(runs.c.run_id == run_id)
                )
            ).first()
            if pointer is None:
                raise RunNotFound(run_id)
            authority_run_id = str(pointer[0] or run_id)
            if authority_run_id == run_id:
                row = pointer[1:]
            else:
                row = (
                    await session.execute(
                        select(
                            runs.c.execution_actor_user_id,
                            runs.c.created_by_tenant_id,
                            runs.c.execution_scopes,
                        ).where(runs.c.run_id == authority_run_id)
                    )
                ).first()
                if row is None:
                    raise RunNotFound(run_id)
        return row[0], row[1], tuple(row[2] or ())

    async def _probe_run_path_db(
        self,
        session: "AsyncSession",
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Read one immutable target-to-root lineage without taking locks."""
        chain: list[dict[str, Any]] = []
        current_id = run_id
        seen: set[str] = set()
        canonical_root_id: str | None = None
        while True:
            if current_id in seen or len(seen) >= 64:
                raise RunNotFound(run_id)
            seen.add(current_id)
            row = (
                (await session.execute(select(runs).where(runs.c.run_id == current_id)))
                .mappings()
                .first()
            )
            if row is None:
                raise RunNotFound(run_id)
            snapshot = dict(row)
            chain.append(snapshot)
            row_root_id = str(snapshot["root_run_id"] or snapshot["run_id"])
            if canonical_root_id is None:
                canonical_root_id = row_root_id
            if row_root_id != canonical_root_id:
                raise RunNotFound(run_id)
            if str(snapshot["run_id"]) == canonical_root_id:
                break
            parent_run_id = snapshot["parent_run_id"]
            if not parent_run_id:
                raise RunNotFound(run_id)
            current_id = str(parent_run_id)
        return chain

    async def _lock_probed_run_path_db(
        self,
        session: "AsyncSession",
        *,
        run_id: str,
        chain: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Lock a previously probed lineage in canonical root-to-leaf order."""
        locked_path: list[dict[str, Any]] = []
        for expected in reversed(chain):
            locked = (
                (
                    await session.execute(
                        select(runs)
                        .where(runs.c.run_id == expected["run_id"])
                        .with_for_update()
                    )
                )
                .mappings()
                .first()
            )
            if locked is None:
                raise RunNotFound(run_id)
            locked_row = dict(locked)
            if (
                locked_row["root_run_id"] != expected["root_run_id"]
                or locked_row["parent_run_id"] != expected["parent_run_id"]
            ):
                raise RunNotFound(run_id)
            locked_path.append(locked_row)
        return locked_path[-1], locked_path[0], locked_path

    async def _lock_execution_path_db(
        self,
        session: "AsyncSession",
        run_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
        use_execution_actor: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any], Any | None]:
        """Lock one run lineage root-to-leaf and resolve root authority.

        Every execution mutation uses this order. A share revoke locks the
        same canonical root/share pair, so either the mutation commits before
        the revoke or it observes the revoke and cannot persist output.
        """
        chain = await self._probe_run_path_db(session, run_id)

        root_probe = chain[-1]
        resolved_actor_user_id = (
            root_probe["execution_actor_user_id"]
            if use_execution_actor
            else actor_user_id
        )
        access = await lock_resource_access(
            session,
            tenant_id=str(root_probe["tenant_id"]),
            actor_user_id=resolved_actor_user_id,
            resource_type="run",
            resource_table=runs,
            id_column=runs.c.run_id,
            resource_id=str(root_probe["run_id"]),
            owner_column=runs.c.created_by_user_id,
            minimum=SharePermission.EDIT,
            restrict_to_workspace_members=self._restrict_to_workspace_members,
            sharing_enabled=self._sharing_enabled,
        )

        target, root, locked_path = await self._lock_probed_run_path_db(
            session, run_id=run_id, chain=chain
        )
        authority_consistent = all(
            item["execution_actor_user_id"] == root["execution_actor_user_id"]
            and item["created_by_user_id"] == root["created_by_user_id"]
            and item["created_by_tenant_id"] == root["created_by_tenant_id"]
            for item in locked_path
        )
        authority_consistent = (
            authority_consistent
            and root["execution_actor_user_id"] == root_probe["execution_actor_user_id"]
        )
        if not authority_consistent:
            access = None
        return target, root, access

    async def _lock_system_run_path_db(
        self,
        session: "AsyncSession",
        run_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Lock lineage for retention/recovery without user authorization."""
        chain = await self._probe_run_path_db(session, run_id)
        target, root, _locked_path = await self._lock_probed_run_path_db(
            session, run_id=run_id, chain=chain
        )
        return target, root

    async def _lock_run_subtree_db(
        self,
        session: "AsyncSession",
        run_id: str,
    ) -> list[dict[str, Any]]:
        """Lock one already lineage-locked subtree in root-to-leaf order."""
        descendants = (
            select(
                runs.c.run_id,
                literal(0).label("depth"),
            )
            .where(runs.c.run_id == run_id)
            .cte("locked_run_subtree", recursive=True)
        )
        descendants = descendants.union_all(
            select(
                runs.c.run_id,
                (descendants.c.depth + 1).label("depth"),
            ).join(
                descendants,
                runs.c.parent_run_id == descendants.c.run_id,
            )
        )
        return [
            dict(row)
            for row in (
                await session.execute(
                    select(runs)
                    .join(
                        descendants,
                        descendants.c.run_id == runs.c.run_id,
                    )
                    .order_by(descendants.c.depth, runs.c.run_id)
                    .with_for_update(of=runs)
                )
            ).mappings()
        ]

    async def _check_execution_authority_db(self, run_id: str) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            _target, _root, access = await self._lock_execution_path_db(session, run_id)
            if access is None:
                raise RunNotFound(run_id)

    async def _owner_user_id_db(self, run_id: str) -> uuid.UUID | None:
        async with self._session("default") as session:
            row = (
                await session.execute(
                    select(runs.c.created_by_user_id).where(runs.c.run_id == run_id)
                )
            ).first()
        return row[0] if row is not None else None

    def title(self, run_id: str) -> str | None:
        """The run's question as a share-surface title, regardless of
        visibility — a pending-share recipient must see it to decide. ``None``
        when the run no longer exists, so the inbox skips it."""
        return self._call(self._title_db(run_id))

    async def _share_facts_db(self, run_ids: list[str]) -> dict[str, ShareFacts]:
        if not run_ids:
            return {}
        async with self._session("default") as session:
            rows = (
                await session.execute(
                    select(
                        runs.c.run_id, runs.c.created_by_user_id, runs.c.question
                    ).where(
                        runs.c.run_id
                        == any_(bindparam("run_ids", run_ids, type_=ARRAY(Text)))
                    )
                )
            ).all()
        return {
            row.run_id: ShareFacts(
                owner_user_id=row.created_by_user_id, title=row.question
            )
            for row in rows
        }

    async def _title_db(self, run_id: str) -> str | None:
        async with self._session("default") as session:
            row = (
                await session.execute(
                    select(runs.c.question).where(runs.c.run_id == run_id)
                )
            ).first()
        return row[0] if row is not None else None

    async def _delete_db(
        self,
        run_id: str,
        workspace_id: str | None,
        requester_user_id: uuid.UUID | None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            access = await lock_resource_access(
                session,
                tenant_id=DEFAULT_TENANT,
                actor_user_id=requester_user_id,
                resource_type="run",
                resource_table=runs,
                id_column=runs.c.run_id,
                resource_id=run_id,
                owner_column=runs.c.created_by_user_id,
                minimum=SharePermission.EDIT,
                restrict_to_workspace_members=self._restrict_to_workspace_members,
                sharing_enabled=self._sharing_enabled,
                owner_only=True,
            )
            if access is None:
                log_authorization_denial(
                    log,
                    action="delete",
                    principal_kind=None,
                    actor_user_id=requester_user_id,
                    tenant_id=DEFAULT_TENANT,
                    resource_type="run",
                    resource_id=run_id,
                )
                raise RunNotFound(run_id)
            row = (
                (await session.execute(select(runs).where(runs.c.run_id == run_id)))
                .mappings()
                .first()
            )
            if row is None:
                raise RunNotFound(run_id)
            if not _workspace_matches_row(row, workspace_id):
                raise RunNotFound(run_id)
            if row["status"] not in {status.value for status in TERMINAL_RUN_STATUSES}:
                raise RunActive(run_id)
            # Parent/root foreign keys cascade through descendants. Their
            # execution caches must be removed under the same terminal fence.
            subtree = await self._lock_run_subtree_db(session, run_id)
            if any(child["status"] not in _TERMINAL_VALUES for child in subtree):
                raise RunActive(run_id)
            if self._checkpoint_cleanup is not None:
                for child in subtree:
                    await asyncio.to_thread(self._checkpoint_cleanup, child["run_id"], child["mode"])
            recipients = await revoke_resource_shares(
                session,
                tenant_id=row["tenant_id"],
                resource_type="run",
                resource_id=run_id,
                revoked_by_user_id=requester_user_id,
            )
            await append_resource_effects(
                session,
                tenant_id=row["tenant_id"],
                actor_user_id=requester_user_id,
                owner_user_id=access.owner_user_id,
                action="run.deleted",
                resource_type="run",
                resource_id=run_id,
                scope="runs",
                additional_targets=recipients,
            )
            await session.execute(delete(runs).where(runs.c.run_id == run_id))

    def result(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return the stored result payload for a completed run."""
        result = self._call(self._result_db(run_id, workspace_id, visible_to))
        self._release_swept_locals()
        return result

    def cancel(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Request cancellation for a queued or running run.

        Queued runs transition to ``cancelled`` immediately (guarded —
        a concurrent worker claim wins or loses atomically); running
        runs get ``cancel_requested`` set, observed by the executing
        process at the next graph node boundary.
        """
        summary, _affected = self.cancel_tree(
            run_id,
            workspace_id=workspace_id,
            visible_to=visible_to,
        )
        return summary

    def cancel_tree(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Cancel one durable run tree and expose the committed affected ids."""
        summary, cascaded_children = self._call(
            self._cancel_db(run_id, workspace_id, visible_to)
        )
        self._release_swept_locals()
        self._dispatch_woken_parents()
        self._apply_cancelled_locals(
            run_id,
            str(summary["status"]),
            (run_id, *cascaded_children),
        )
        return summary, (run_id, *cascaded_children)

    def authorized_control_write(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        control_write: Any,
    ) -> Any:
        """Apply one control-table write under live root authorization."""
        result, cancellations = self._call(
            self._authorized_control_write_db(
                run_id,
                workspace_id,
                visible_to,
                control_write,
            )
        )
        self._release_swept_locals()
        self._dispatch_woken_parents()
        for child_run_id, child_status, affected_ids in cancellations:
            self._apply_cancelled_locals(
                child_run_id,
                child_status,
                affected_ids,
            )
        return result

    def _drain_failed_cascades(self) -> None:
        """Project committed parent-failure cascades into local handles.

        Sync-mutator half of the ``_failed_cascades`` handoff: the
        coroutine only enqueues (it must never take ``self._lock``);
        every mutator that can land a FAILED terminal drains here.
        """
        while True:
            try:
                root_id, children = self._failed_cascades.get_nowait()
            except Empty:
                return
            self._apply_cancelled_locals(
                root_id, RunStatus.FAILED.value, (root_id, *children)
            )

    def _apply_cancelled_locals(
        self,
        root_run_id: str,
        root_status: str,
        affected_ids: tuple[str, ...],
    ) -> None:
        """Project committed durable cancellation into local work handles."""
        with self._lock:
            for cancelled_id in affected_ids:
                local = self._local.get(cancelled_id)
                if local is None:
                    continue
                local.cancel_event.set()
                if (
                    cancelled_id == root_run_id
                    and root_status != RunStatus.CANCELLED.value
                ):
                    # Root is running: the executing worker observes
                    # the cancel event and its finally cleans up.
                    continue
                was_pending = True
                try:
                    self._pending.remove(cancelled_id)
                except ValueError:
                    was_pending = False
                local.work = None
                if was_pending or local.parked:
                    # Nothing executes this run (queued locally or
                    # parked waiting) — no worker finally will release
                    # the retained closure, so do it here.
                    self._local.pop(cancelled_id, None)

    def bind_checkpoint_cleanup(self, cleanup: Callable[[str, str], None]) -> None:
        """The bound cleanup owns the external cache, never run authorization."""
        self._checkpoint_cleanup = cleanup

    def sweep(self) -> None:
        """Run the cleanup pass every access runs, without waiting for one.

        Its own transaction under the default tenant (like the worker's
        reconciler), then the post-commit handoffs.
        """
        self._call(self._cleanup_pass_db())
        self._release_swept_locals()

    def delete(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        requester_user_id: uuid.UUID | None = None,
    ) -> None:
        """Permanently remove one terminal run (owner-only).

        Mirrors the in-memory store: creator identity gates the delete (not
        share visibility), terminal-only; the ``run_events`` rows cascade
        with the parent (FK ``ondelete=CASCADE``). ``RunNotFound`` for
        unknown / non-owner / cross-namespace ids; ``RunActive`` for a
        still-active run.
        """
        self._call(self._delete_db(run_id, workspace_id, requester_user_id))
        self._release_swept_locals()
        with self._lock:
            self._local.pop(run_id, None)
            try:
                self._pending.remove(run_id)
            except ValueError:
                pass

    def subscribe(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        stream: bool = True,
    ) -> PollingJobSubscription:
        """Subscribe to a run's event stream with full stored replay.

        ``stream=False`` serves a one-shot replay read (the JSON polling
        fallback): same visibility check and replay, but no poller
        thread, no registration, no viewer-metric observation.
        """
        self._expire_lost_executions()
        tenant_id, replay = self._call(
            self._replay_db(run_id, workspace_id, visible_to)
        )
        self._release_swept_locals()
        return PollingJobSubscription(
            self,
            run_id,
            tenant_id,
            replay,
            terminal_events=_TERMINAL_EVENT_TYPES,
            thread_label="inqtrix-run-events",
            stream=stream,
        )

    def unsubscribe(self, run_id: str, queue: Queue) -> None:
        """Parity no-op: polling subscriptions detach via ``close()``."""

    def emit(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        fence_attempt: int | None = None,
    ) -> None:
        """Append one event (plus snapshot companion) to the run.

        With *fence_attempt* set (worker handles), events from a
        superseded attempt are dropped with a visible log instead of
        interleaving with the superseding attempt's stream.
        """
        self._call(self._emit_db(run_id, event_type, payload or {}, fence_attempt))

    def complete(
        self,
        run_id: str,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        fence_attempt: int | None = None,
    ) -> bool:
        """Store the final result and mark the run completed.

        Returns:
            ``True`` when the terminal transition landed; ``False``
            when it was absorbed (already terminal or fenced out by a
            superseding claim) — the caller must NOT ack the dispatch
            message in that case.
        """
        landed = self._call(
            self._terminal_db(
                run_id,
                RunStatus.COMPLETED,
                fence_attempt=fence_attempt,
                result=dict(result),
                snapshot=dict(snapshot) if snapshot else None,
                event_builder=lambda row_snapshot: (
                    "inqtrix.run.completed",
                    {
                        "status": "completed",
                        "metrics": (
                            result.get("metrics")
                            if isinstance(result.get("metrics"), dict)
                            else {}
                        ),
                        "result_url": f"/v1/runs/{run_id}/result",
                        "snapshot": row_snapshot,
                    },
                ),
            )
        )
        self._dispatch_woken_parents()
        return landed

    def fail(
        self,
        run_id: str,
        message: str,
        *,
        error_type: str = "server_error",
        fence_attempt: int | None = None,
    ) -> bool:
        """Mark a run failed with a sanitized error payload.

        Returns:
            ``True`` when the transition landed (see :meth:`complete`).
        """
        error = {"message": sanitize_error(message), "type": error_type}
        landed = self._call(
            self._terminal_db(
                run_id,
                RunStatus.FAILED,
                fence_attempt=fence_attempt,
                error=error,
                event_builder=lambda row_snapshot: (
                    "inqtrix.run.failed",
                    {
                        "status": "failed",
                        "error": error,
                        "snapshot": row_snapshot,
                    },
                ),
            )
        )
        self._dispatch_woken_parents()
        return landed

    def mark_cancelled(
        self,
        run_id: str,
        *,
        reason: str,
        fence_attempt: int | None = None,
    ) -> bool:
        """Mark a running run cancelled after its worker observed it.

        Returns:
            ``True`` when the transition landed (see :meth:`complete`).
        """
        landed = self._call(
            self._terminal_db(
                run_id,
                RunStatus.CANCELLED,
                fence_attempt=fence_attempt,
                event_builder=lambda row_snapshot: (
                    "inqtrix.run.cancelled",
                    {
                        "status": "cancelled",
                        "reason": reason,
                        "snapshot": row_snapshot,
                    },
                ),
            )
        )
        self._dispatch_woken_parents()
        return landed

    def mark_waiting(
        self,
        run_id: str,
        *,
        status: RunStatus | str,
        fence_attempt: int | None = None,
    ) -> None:
        """Park a RUNNING run in a waiting status (agent interrupt).

        The executing closure returns right after; the local job
        (no-queue mode) is retained by the dispatch worker so
        :meth:`resume_run` can re-dispatch the same closure. Only the
        two waiting statuses are accepted, and only from RUNNING. When
        a cancel request is already pending, the run is CANCELLED
        instead of parked (reason ``cancelled_while_waiting``) — a
        cancelled assignment must not sit in a waiting status until
        its TTL (memory-store parity).
        """
        waiting = RunStatus(status)
        if waiting not in WAITING_RUN_STATUSES:
            raise ValueError(f"not a waiting status: {status!r}")
        with self._lock:
            local = self._local.get(run_id)
            if local is not None:
                # Pre-arm BEFORE the row becomes waiting-visible: a
                # resume racing the commit must already see the
                # in-flight flag, or it would dispatch a second worker
                # while this one is still unwinding.
                local.parked = True
                local.park_in_flight = True
        try:
            parked = self._call(self._mark_waiting_db(run_id, waiting, fence_attempt))
        except BaseException:
            with self._lock:
                local = self._local.get(run_id)
                if local is not None:
                    local.parked = False
                    local.park_in_flight = False
            raise
        # Park-time self-heal handoff: when every child was already
        # terminal, _mark_waiting_db flipped the row straight back to
        # queued — dispatch it now (park_in_flight defers the local
        # re-dispatch to this worker's unwind, exactly like resume_run).
        self._dispatch_woken_parents()
        if not parked:
            # Resolved as a cancel (a cancel request was pending):
            # nothing is retained, the unwind cleans up normally.
            with self._lock:
                local = self._local.get(run_id)
                if local is not None:
                    local.parked = False
                    local.park_in_flight = False

    def resume_run(
        self,
        run_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
        execution_scopes: frozenset[str] = frozenset(),
        control_write: Any = None,
    ) -> dict[str, Any]:
        """Move a waiting run back to QUEUED and dispatch it.

        Queue mode re-enqueues (any worker resumes from the persisted
        payload/checkpoint); no-queue mode re-appends the retained local
        closure — a waiting row without one (process restarted) fails
        loudly instead of hanging.

        *control_write* is the atomic control-write seam: an optional
        coroutine function ``await control_write(session)`` executed INSIDE the
        ``waiting -> queued`` transaction, right after the status CAS
        lands — the agent control store records approval/clarification
        decisions through it so a crash can never separate the decision
        from the resume. When it raises, the whole transaction (including
        the status flip) rolls back.

        Raises:
            RunNotFound: Unknown id.
            RunActive: The run is not in a waiting status, or no-queue
                mode retained no closure to re-dispatch.
            Whatever *control_write* raises (transaction rolled back).
        """
        if self._dispatch_queue is None:
            # Check the retained closure BEFORE flipping the row: a
            # queued row nothing can execute would hang until the
            # stuck-row failsafe.
            with self._lock:
                local = self._local.get(run_id)
            if local is None or local.work is None:
                # Distinguish the failure loudly, with memory-parity
                # error types: unknown id -> RunNotFound (raised by the
                # row probe), not waiting -> RunActive with the status,
                # waiting without a closure (process restarted) ->
                # RunActive naming the restart.
                current_status = self._call(self._status_db(run_id))
                if current_status not in _WAITING_VALUES:
                    raise RunActive(
                        f"run {run_id} is not waiting " f"(status {current_status})"
                    )
                raise RunActive(
                    f"run {run_id} has no retained work to resume "
                    "(no-queue mode after a restart)"
                )
        summary = self._call(
            self._resume_db(
                run_id,
                actor_user_id,
                execution_scopes,
                control_write,
            )
        )
        self._release_swept_locals()
        if self._dispatch_queue is not None:
            try:
                self._dispatch_queue.enqueue(run_id=run_id, tenant_id=DEFAULT_TENANT)
            except Exception:  # noqa: BLE001 — row committed; reconciler heals
                log.warning(
                    "Resume dispatch for run %s could not be sent; the reconciler will "
                    "retry.",
                    run_id,
                    exc_info=True,
                )
            return summary
        with self._lock:
            local = self._local.get(run_id)
            if local is None or local.work is None:
                # Cancelled/TTL-swept in the window between the check
                # and the row flip — _resume_db would have raised, so
                # this is unreachable in practice; guard it anyway.
                raise RunActive(f"run {run_id} lost its work while resuming")
            if local.park_in_flight:
                # The parking worker has not unwound yet: dispatching
                # now would run the same closure on two threads. The
                # unwind performs the deferred re-dispatch.
                local.resume_requested = True
            else:
                local.parked = False
                self._pending.append(run_id)
                self._dispatch_locked()
        return summary

    def children(self, run_id: str) -> list[dict[str, Any]]:
        """Summaries of this run's direct children, newest first.

        Authorization happens on the PARENT (the route resolves it via
        :meth:`get` first); children inherit that access.
        """
        children = self._call(self._children_db(run_id))
        self._release_swept_locals()
        return children

    # -- worker surface --------------------------------------------------- #

    def claim_for_execution(
        self,
        run_id: str,
        tenant_id: str,
        *,
        allow_takeover: bool,
    ) -> ClaimedRun | None:
        """Atomically claim one run for execution.

        Args:
            run_id: Run to claim.
            tenant_id: Tenant carried in the dispatch message.
            allow_takeover: Permit claiming a run already marked
                ``running`` — only correct for redeliveries (the
                previous worker stopped heartbeating); fresh duplicate
                messages must NOT take over a healthy execution.

        Returns:
            The claim with its fencing ``attempt``, or ``None`` when
            the run is terminal, missing, or running elsewhere.
        """
        return self._call(
            self._claim_db(run_id, tenant_id, allow_takeover=allow_takeover)
        )

    def cancel_requested_runs(self, run_ids: dict[str, str]) -> set[str]:
        """Subset of ``run_ids`` (id -> tenant) with a pending cancel."""
        return self._call(self._cancel_requested_db(run_ids))

    def dispatch_status(self, run_id: str, tenant_id: str) -> str | None:
        """Authoritative row status for worker duplicate classification.

        This intentionally skips cleanup and caller visibility: it is a
        worker-only read under the dispatch message's tenant. ``None`` means
        the row disappeared; storage errors propagate so uncertainty never
        turns a legitimate successor into an ACK.
        """
        return self._call(self._dispatch_status_db(run_id, tenant_id))

    def stale_queued_runs(self, *, older_than_seconds: float) -> list[tuple[str, str]]:
        """Queued ``(run_id, tenant_id)`` pairs older than the threshold.

        Re-enqueue feed for the worker's reconciler: a crash between
        the run-row insert and the stream ``XADD`` leaves the row
        queued with no message; re-dispatching is safe because fresh
        duplicates never take over a healthy execution.
        """
        return self._call(self._stale_queued_db(older_than_seconds))

    # -- in-process execution hooks (no-queue mode) ---------------------- #

    def _make_handle(self, run_id: str, cancel_event) -> RunHandle:
        return RunHandle(self, run_id, cancel_event)

    def _enter_execution_telemetry(
        self, stack, entity_id: str, claimed
    ) -> None:
        """Root span + log context for durable NO-QUEUE executions.

        Parity with the worker loop and the in-memory store: this is the
        third execution boundary — without it, postgres-without-workers
        runs would trace headless and their failures could never mark a
        run span (terminate_native_run marks the CURRENT span).
        """
        from inqtrix.observability.context import (
            bind_log_context,
            reset_log_context,
        )
        from inqtrix.observability.otel import run_execute_span

        stack.enter_context(
            run_execute_span(
                run_id=entity_id,
                tenant_id=str(
                    getattr(claimed, "tenant_id", "") or DEFAULT_TENANT
                ),
                attempt=int(getattr(claimed, "attempt", 1) or 1),
                payload=getattr(claimed, "request_payload", None),
            )
        )
        tokens = bind_log_context(
            run_id=entity_id,
            tenant=str(getattr(claimed, "tenant_id", "") or DEFAULT_TENANT),
        )
        stack.callback(reset_log_context, tokens)

    def _terminate_work_exception(
        self, handle: RunHandle, entity_id: str, exc: BaseException
    ) -> None:
        """Preserve typed run failures in durable no-queue mode."""
        del entity_id
        terminate_native_run(handle, exc)

    def _auto_complete(self, run_id: str) -> None:
        # exclude_waiting: a work closure that parked its run (agent
        # interrupt) returns normally — the safety net must not
        # terminal-write over the parked row (memory-store parity).
        self._call(
            self._terminal_db(
                run_id,
                RunStatus.COMPLETED,
                fence_attempt=None,
                warn_on_noop=False,
                exclude_waiting=True,
                event_builder=lambda row_snapshot: (
                    "inqtrix.run.completed",
                    {"status": "completed", "snapshot": row_snapshot},
                ),
            )
        )
        self._dispatch_woken_parents()

    # -- subscription poll bridge ----------------------------------------- #

    def _events_after(
        self, run_id: str, tenant_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        # The fence hook on the poll path is what lets an ALREADY
        # ATTACHED stream self-heal: the next subscription poll
        # terminalizes a lost run, reads its terminal event, and closes
        # the stream.
        self._expire_lost_executions()
        return self._call(self._events_after_db(run_id, tenant_id, after_sequence))

    # -- lost-execution fence (no-queue mode) ----------------------------- #

    def _expire_lost_executions(self) -> bool:
        expired = super()._expire_lost_executions()
        if expired:
            # Post-commit handoffs (woken parents of fenced children)
            # must dispatch immediately, even from the poller thread.
            self._release_swept_locals()
        return expired

    async def _lost_execution_candidates_db(
        self, grace_seconds: float
    ) -> list[str]:
        async with self._session(DEFAULT_TENANT) as session:
            return list(
                (
                    await session.execute(
                        select(runs.c.run_id).where(
                            runs.c.status.in_(
                                (
                                    RunStatus.QUEUED.value,
                                    RunStatus.RUNNING.value,
                                )
                            ),
                            func.coalesce(
                                runs.c.active_started_at,
                                runs.c.queued_since,
                                runs.c.created_at,
                            )
                            < time.time() - grace_seconds,
                        )
                    )
                )
                .scalars()
                .all()
            )

    async def _expire_lost_executions_db(self, entity_ids: list[str]) -> bool:
        async with self._session(DEFAULT_TENANT) as session:
            woken = await self._recover_orphans_db(
                session,
                candidate_ids=entity_ids,
                error=_EXECUTION_LOST_ERROR,
            )
            self._register_cleanup_handoffs(
                session,
                swept_run_ids=[],
                woken_parent_ids=woken,
            )
            return True

    # -- async database operations ----------------------------------------- #

    async def _admit_agent_child_db(
        self,
        session: "AsyncSession",
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Canonicalize one child and serialize it against tree cancellation.

        The canonical owner, effective actor, scopes, lineage, and idempotency
        identity all come from durable parent/root state. The common
        root-to-parent lock path matches every execution mutation and cancel.
        """
        if fields.get("kind") != "agent_child":
            return None
        parent_run_id = str(fields.get("parent_run_id") or "")
        if not parent_run_id:
            raise RunParentInactive("agent child has no parent run")
        try:
            parent, root, access = await self._lock_execution_path_db(
                session, parent_run_id
            )
        except RunNotFound as exc:
            raise RunParentInactive("agent child parent or root is missing") from exc
        requested_actor_user_id = fields.get("execution_actor_user_id")
        if access is None or requested_actor_user_id != root["execution_actor_user_id"]:
            raise AuthorizationRevoked(
                "agent child admission lost root execution authority"
            )
        canonical_root_id = str(root["run_id"])

        request_body = (fields.get("request_payload") or {}).get("body") or {}
        origin_key = (
            str(request_body.get("origin_key") or "")
            if isinstance(request_body, dict)
            else ""
        )
        if origin_key:
            existing_id = (
                await session.execute(
                    select(runs.c.run_id)
                    .where(
                        runs.c.kind == "agent_child",
                        runs.c.parent_run_id == parent_run_id,
                        func.json_extract_path_text(
                            runs.c.request_payload,
                            "body",
                            "origin_key",
                        )
                        == origin_key,
                    )
                    .order_by(runs.c.created_at, runs.c.run_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                existing = await self._row_db(session, existing_id)
                position = (
                    await self._queue_position_db(session, existing.created_at)
                    if existing.status == RunStatus.QUEUED.value
                    else None
                )
                return build_run_summary(existing, queue_position=position)

        inactive = {
            *_TERMINAL_VALUES,
            RunStatus.EXPIRED.value,
        }
        if (
            str(parent["status"]) in inactive
            or bool(parent["cancel_requested"])
            or str(root["status"]) in inactive
            or bool(root["cancel_requested"])
        ):
            raise RunParentInactive("agent child parent or root is no longer active")
        fields["root_run_id"] = canonical_root_id
        fields["created_by_user_id"] = root["created_by_user_id"]
        fields["created_by_tenant_id"] = root["created_by_tenant_id"]
        fields["execution_actor_user_id"] = root["execution_actor_user_id"]
        fields["execution_scopes"] = list(root["execution_scopes"] or ())
        fields["workspace_id"] = root["workspace_id"]
        fields["session_id"] = root["session_id"]
        return None

    async def _submit_db(
        self, *, tenant_id: str, **fields: Any
    ) -> tuple[dict[str, Any], bool]:
        async with self._session(tenant_id) as session:
            await self._cleanup_db(session)
            existing = await self._admit_agent_child_db(session, fields)
            if existing is not None:
                return existing, False
            session_id = fields.get("session_id")
            session_table = None
            if session_id and fields.get("kind") in {"agent", "agent_child"}:
                session_table = agent_sessions
            elif (
                session_id
                and fields.get("mode") == "knowledge"
                and fields.get("kind") == "standard"
            ):
                session_table = knowledge_sessions
            if session_table is not None:
                active_session = await session.scalar(
                    select(session_table.c.id)
                    .where(
                        session_table.c.tenant_id == tenant_id,
                        session_table.c.id == session_id,
                        session_table.c.created_by_user_id.is_not_distinct_from(
                            fields.get("created_by_user_id")
                        ),
                        session_table.c.workspace_id.is_not_distinct_from(
                            fields.get("workspace_id")
                        ),
                        session_table.c.lifecycle_status == "active",
                    )
                    .with_for_update()
                )
                if active_session is None:
                    raise RunSessionActive(str(session_id))
            effective_actor_user_id = fields.get("execution_actor_user_id")
            if effective_actor_user_id is not None and not await lock_active_users(
                session,
                tenant_id=tenant_id,
                user_ids=(effective_actor_user_id,),
            ):
                raise RunNotFound("inactive submitting user")
            queued = await session.scalar(
                select(func.count())
                .select_from(runs)
                .where(runs.c.status == RunStatus.QUEUED.value)
            )
            running = await session.scalar(
                select(func.count())
                .select_from(runs)
                .where(runs.c.status == RunStatus.RUNNING.value)
            )
            if (queued or 0) >= self._max_queue_size and (
                running or 0
            ) >= self._max_concurrent:
                raise RunQueueFull("native run queue is full")
            if (
                self._max_concurrent_per_user is not None
                and effective_actor_user_id is not None
            ):
                # Fairness bound UNDER the global cap, counted in the SAME
                # transaction as the insert. COUNTED: QUEUED+RUNNING
                # standard runs and agent CHILDREN (the runs that occupy
                # an execution slot). EXCLUDED: WAITING runs (parked,
                # slot-free) AND agent PARENTS (kind='agent'), which park
                # immediately and must not contend against their own
                # children for the user's budget. Scope is created_by_user_id
                # only (single-tenant today; see the memory-store note).
                #
                # APPROXIMATE bound: under READ COMMITTED the COUNT takes
                # no lock on the counted rows, so N concurrent submits by
                # one user can each observe cap-1 and all pass, transiently
                # reaching up to cap+N-1. That is acceptable for a
                # fairness bound (the memory store IS exact under its
                # lock); a hard guarantee would need SELECT FOR UPDATE on
                # a per-user sentinel or SERIALIZABLE retry, at real
                # hot-path cost. The docstring calls this out.
                in_flight = await session.scalar(
                    select(func.count())
                    .select_from(runs)
                    .where(
                        runs.c.execution_actor_user_id == effective_actor_user_id,
                        runs.c.kind != "agent",
                        runs.c.status.in_(
                            (
                                RunStatus.QUEUED.value,
                                RunStatus.RUNNING.value,
                            )
                        ),
                    )
                )
                if (in_flight or 0) >= self._max_concurrent_per_user:
                    raise RunPerUserLimit("per-user in-flight run cap reached")

            created_at = time.time()
            try:
                run_id = await self._insert_with_unique_id(
                    session,
                    tenant_id=tenant_id,
                    created_at=created_at,
                    **fields,
                )
            except IntegrityError as exc:
                if _ACTIVE_AGENT_SESSION_CONSTRAINT in str(exc.orig):
                    raise RunSessionActive(fields.get("session_id")) from exc
                raise
            position = await self._queue_position_db(session, created_at)
            await self._append_events_db(
                session,
                run_id,
                tenant_id,
                expand_run_event(
                    "inqtrix.run.queued",
                    {"status": "queued", "queue_position": position},
                    status=RunStatus.QUEUED.value,
                )[1],
            )
            await self._project_child_progress_db(
                session,
                child_run_id=run_id,
                kind=str(fields.get("kind") or "standard"),
                parent_run_id=fields.get("parent_run_id"),
                request_payload=fields.get("request_payload") or {},
                run_status=RunStatus.QUEUED.value,
                event_type="inqtrix.run.queued",
                payload={"status": "queued", "queue_position": position},
                snapshot={},
            )
            await append_resource_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=effective_actor_user_id,
                owner_user_id=fields.get("created_by_user_id"),
                action="run.created",
                resource_type="run",
                resource_id=run_id,
                scope="runs",
            )
            row = await self._row_db(session, run_id)
            return (
                build_run_summary(
                    row,
                    queue_position=position,
                    access=_access_annotation(
                        None,
                        owner_user_id=fields.get("created_by_user_id"),
                    ),
                ),
                True,
            )

    async def _import_completed_run_db(
        self,
        *,
        tenant_id: str,
        source_run_id: str,
        question: str,
        stack_name: str,
        result: dict[str, Any],
        status: str,
        mode: str,
        agent_overrides: dict[str, Any],
        snapshot: dict[str, Any],
        error: dict[str, Any] | None,
        created_at: float | None,
        workspace_id: str | None,
        created_by_user_id: uuid.UUID | None,
        created_by_tenant_id: str | None,
    ) -> dict[str, Any]:
        now = time.time()
        values: dict[str, Any] = {
            "tenant_id": tenant_id,
            "status": status,
            "question": question,
            "stack_name": stack_name,
            "workspace_id": workspace_id,
            "mode": mode,
            "agent_overrides": agent_overrides,
            "created_by_user_id": created_by_user_id,
            "created_by_tenant_id": created_by_tenant_id,
            "source_run_id": source_run_id,
            # created_at keeps the report's original date (display/ordering);
            # started_at/finished_at = now so the durable-retention clock starts
            # at import -- an old report is not pruned on the next cleanup.
            "created_at": created_at if created_at is not None else now,
            "started_at": now,
            "finished_at": now,
            "snapshot": snapshot,
            # Only a completed run carries a result payload; failed/cancelled
            # imports keep the error, matching the live terminal transitions.
            "result": result if status == RunStatus.COMPLETED.value else None,
            "error": error,
        }
        async with self._session(tenant_id) as session:
            await self._cleanup_db(session)
            if created_by_user_id is not None and not await lock_active_users(
                session,
                tenant_id=tenant_id,
                user_ids=(created_by_user_id,),
            ):
                raise RunNotFound("inactive importing user")
            run_id, created = await self._insert_import_with_unique_id(
                session,
                values=values,
                tenant_id=tenant_id,
                source_run_id=source_run_id,
                created_by_user_id=created_by_user_id,
            )
            if created:
                await append_resource_effects(
                    session,
                    tenant_id=tenant_id,
                    actor_user_id=created_by_user_id,
                    owner_user_id=created_by_user_id,
                    action="run.imported",
                    resource_type="run",
                    resource_id=run_id,
                    scope="runs",
                )
            row = await self._row_db(session, run_id)
            return build_run_summary(
                row,
                queue_position=None,
                access=_access_annotation(
                    None,
                    owner_user_id=created_by_user_id,
                ),
            )

    async def _insert_import_with_unique_id(
        self,
        session: "AsyncSession",
        *,
        values: dict[str, Any],
        tenant_id: str,
        source_run_id: str,
        created_by_user_id: uuid.UUID | None,
    ) -> tuple[str, bool]:
        """Return ``(run_id, created)`` for one idempotent import."""
        user_predicate = (
            runs.c.created_by_user_id == created_by_user_id
            if created_by_user_id is not None
            else runs.c.created_by_user_id.is_(None)
        )
        for _ in range(8):
            run_id = new_run_id()
            landed = (
                await session.execute(
                    pg_insert(runs)
                    .values(run_id=run_id, **values)
                    .on_conflict_do_nothing()
                    .returning(runs.c.run_id)
                )
            ).scalar_one_or_none()
            if landed is not None:
                return run_id, True
            existing = (
                await session.execute(
                    select(runs.c.run_id)
                    .where(
                        runs.c.tenant_id == tenant_id,
                        runs.c.source_run_id == source_run_id,
                        user_predicate,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is not None:
                return str(existing), False
            log.warning("Native run id collision detected; retrying allocation.")
        raise RuntimeError("could not allocate a unique native run id")

    async def _insert_with_unique_id(
        self,
        session: "AsyncSession",
        *,
        tenant_id: str,
        created_at: float,
        **fields: Any,
    ) -> str:
        for _ in range(8):
            run_id = new_run_id()
            result = await session.execute(
                pg_insert(runs)
                .values(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    status=RunStatus.QUEUED.value,
                    question=fields["question"],
                    stack_name=fields["stack_name"],
                    workspace_id=fields["workspace_id"],
                    mode=fields["mode"],
                    agent_overrides=fields["agent_overrides"],
                    request_payload=fields["request_payload"],
                    created_by_user_id=fields["created_by_user_id"],
                    created_by_tenant_id=fields["created_by_tenant_id"],
                    execution_actor_user_id=fields["execution_actor_user_id"],
                    execution_scopes=fields["execution_scopes"],
                    kind=fields["kind"],
                    parent_run_id=fields["parent_run_id"],
                    root_run_id=fields["root_run_id"],
                    session_id=fields["session_id"],
                    incognito=fields["incognito"],
                    created_at=created_at,
                )
                .on_conflict_do_nothing(index_elements=["run_id"])
                .returning(runs.c.run_id)
            )
            if result.scalar_one_or_none() is not None:
                return run_id
            log.warning("Native run id collision detected; retrying allocation.")
        raise RuntimeError("could not allocate a unique native run id")

    async def _queue_position_db(
        self, session: "AsyncSession", created_at: float
    ) -> int:
        earlier = await session.scalar(
            select(func.count())
            .select_from(runs)
            .where(
                runs.c.status == RunStatus.QUEUED.value,
                runs.c.created_at < created_at,
            )
        )
        return int(earlier or 0) + 1

    async def _status_db(self, run_id: str) -> str:
        """Just the status string; raises ``RunNotFound`` when missing."""
        async with self._session(DEFAULT_TENANT) as session:
            status = (
                await session.execute(
                    select(runs.c.status).where(runs.c.run_id == run_id)
                )
            ).scalar_one_or_none()
            if status is None:
                raise RunNotFound(run_id)
            return status

    async def _total_elapsed_seconds_db(self, run_id: str) -> float:
        """Read timing without applying the public owner/share predicate."""

        async with self._session(DEFAULT_TENANT) as session:
            row = await self._row_db(session, run_id)
            return float(run_elapsed_seconds(row) or 0.0)

    async def _row_db(self, session: "AsyncSession", run_id: str) -> _RowView:
        row = (
            (await session.execute(select(runs).where(runs.c.run_id == run_id)))
            .mappings()
            .first()
        )
        if row is None:
            raise RunNotFound(run_id)
        return _row_view(row)

    async def _visible_row_db(
        self,
        session: "AsyncSession",
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        *,
        for_update: bool = False,
    ) -> tuple[Any, "SharePermission | None"]:
        """The row plus the share grant that admitted it (if any).

        Shared-in runs bypass the workspace namespace filter — they
        carry the grantor's workspace id.
        """
        query = select(runs).where(runs.c.run_id == run_id)
        if for_update:
            query = query.with_for_update()
        row = (await session.execute(query)).mappings().first()
        if row is None:
            raise RunNotFound(run_id)
        if _visible_row(row, visible_to):
            if not _workspace_matches_row(row, workspace_id):
                raise RunNotFound(run_id)
            return row, None
        shared = await self._share_permission_db(
            session, row, visible_to, for_update=for_update
        )
        if shared is not None:
            return row, shared
        principal = visible_to.principal if visible_to is not None else None
        log_authorization_denial(
            log,
            action="read",
            principal_kind=principal.kind if principal is not None else None,
            actor_user_id=principal.user_id if principal is not None else None,
            tenant_id=principal.tenant_id if principal is not None else None,
            resource_type="run",
            resource_id=run_id,
        )
        if self._audit is not None and visible_to is not None:
            await self._audit.record(
                build_audit_entry(
                    tenant_id=visible_to.principal.tenant_id,
                    actor_user_id=visible_to.principal.user_id,
                    action="authz.denied",
                    resource_type="run",
                    resource_id=run_id,
                    detail={"surface": "runs"},
                    outcome="denied",
                )
            )
        raise RunNotFound(run_id)

    def _common_workspace_exists(
        self,
        owner_user_id: uuid.UUID,
        recipient_user_id: uuid.UUID,
    ):
        """Correlated SQL predicate for the continuous workspace boundary."""
        owner_members = workspace_members.alias("run_share_owner_members")
        recipient_members = workspace_members.alias("run_share_recipient_members")
        return exists(
            select(1)
            .select_from(
                owner_members.join(
                    recipient_members,
                    and_(
                        owner_members.c.tenant_id == recipient_members.c.tenant_id,
                        owner_members.c.workspace_id
                        == recipient_members.c.workspace_id,
                    ),
                )
            )
            .where(
                owner_members.c.tenant_id == DEFAULT_TENANT,
                owner_members.c.user_id == owner_user_id,
                recipient_members.c.user_id == recipient_user_id,
            )
        )

    def _share_permission_expr(self, visible_to: "UserContext"):
        """Correlated accepted-share permission for run list queries."""
        if not self._sharing_enabled:
            return literal(None, type_=resource_shares.c.permission.type)
        principal = visible_to.principal
        criteria = [
            resource_shares.c.tenant_id == principal.tenant_id,
            resource_shares.c.resource_type == "run",
            resource_shares.c.resource_id == runs.c.run_id,
            resource_shares.c.recipient_user_id == principal.user_id,
            resource_shares.c.accepted_at.isnot(None),
            resource_shares.c.revoked_at.is_(None),
        ]
        if self._restrict_to_workspace_members:
            criteria.append(
                self._common_workspace_exists(
                    runs.c.created_by_user_id, principal.user_id
                )
            )
        return (
            select(resource_shares.c.permission)
            .where(*criteria)
            .correlate(runs)
            .scalar_subquery()
        )

    async def _share_permission_db(
        self,
        session: "AsyncSession",
        row: Any,
        visible_to: "UserContext | None",
        *,
        for_update: bool,
    ) -> SharePermission | None:
        """Resolve and optionally lock the live direct share for one run."""
        if (
            not self._sharing_enabled
            or visible_to is None
            or visible_to.principal.user_id is None
            or row["created_by_user_id"] is None
        ):
            return None
        principal = visible_to.principal
        query = select(resource_shares.c.permission).where(
            resource_shares.c.tenant_id == principal.tenant_id,
            resource_shares.c.resource_type == "run",
            resource_shares.c.resource_id == row["run_id"],
            resource_shares.c.recipient_user_id == principal.user_id,
            resource_shares.c.accepted_at.isnot(None),
            resource_shares.c.revoked_at.is_(None),
        )
        if self._restrict_to_workspace_members:
            query = query.where(
                self._common_workspace_exists(
                    row["created_by_user_id"], principal.user_id
                )
            )
        if for_update:
            query = query.with_for_update()
        value = (await session.execute(query)).scalar_one_or_none()
        return SharePermission(value) if value is not None else None

    async def _summary_db(
        self,
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row, shared = await self._visible_row_db(
                session, run_id, workspace_id, visible_to
            )
            position = (
                await self._queue_position_db(session, row["created_at"])
                if row["status"] == RunStatus.QUEUED.value
                else None
            )
            view = _row_view(row)
            summary = build_run_summary(
                view,
                queue_position=position,
                access=_access_annotation(
                    shared, owner_user_id=row["created_by_user_id"]
                ),
            )
            # The single-run read is where the clipped question becomes
            # reachable again. Lists deliberately carry only the flag —
            # keeping them small is the reason the column is clipped at
            # all — so this key exists on THIS path and nowhere else.
            if question_was_clipped(view):
                summary["question_full"] = view.question_full
            return summary

    def _apply_run_visibility(
        self,
        query,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ):
        """Apply the owner/workspace/shared-in visibility WHERE (shared).

        The single definition of who may see a run, used by both the
        unbounded ``_list_db`` and the paginated ``_list_page_db`` so the
        two can never drift.
        """
        if visible_to is not None:
            shared_permission = self._share_permission_expr(visible_to)
            owned = and_(
                runs.c.created_by_user_id == visible_to.principal.user_id,
                runs.c.created_by_tenant_id == visible_to.principal.tenant_id,
            )
            if workspace_id is not None:
                owned = and_(owned, runs.c.workspace_id == workspace_id)
            return query.add_columns(
                shared_permission.label("_share_permission")
            ).where(or_(owned, shared_permission.isnot(None)))
        query = query.add_columns(
            literal(None, type_=resource_shares.c.permission.type).label(
                "_share_permission"
            )
        ).where(runs.c.created_by_user_id.is_(None))
        if workspace_id is not None:
            return query.where(runs.c.workspace_id == workspace_id)
        return query

    async def _queue_positions_for(
        self, session: "AsyncSession", run_ids: list[str]
    ) -> dict[str, int]:
        """Global queue positions for the QUEUED rows in *run_ids*.

        ONE window-function scan over the (small, capacity-bounded) set
        of QUEUED rows, replacing the old per-row COUNT(*) N+1. Positions
        are GLOBAL (1-based over every queued run), so a queued run on a
        later page still reports its true position.
        """
        if not run_ids:
            return {}
        ranked = (
            select(
                runs.c.run_id,
                func.row_number()
                .over(order_by=(runs.c.created_at, runs.c.run_id))
                .label("position"),
            )
            .where(runs.c.status == RunStatus.QUEUED.value)
            .subquery()
        )
        rows = (
            await session.execute(
                select(ranked.c.run_id, ranked.c.position).where(
                    ranked.c.run_id.in_(run_ids)
                )
            )
        ).all()
        return {run_id: int(position) for run_id, position in rows}

    async def _list_db(
        self,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> list[dict[str, Any]]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            query = self._apply_run_visibility(
                # run_id tiebreaker matches list_page's keyset order, so the
                # unbounded internal read and the paginated HTTP endpoint agree
                # on the relative order of runs sharing a created_at epoch.
                _summary_select().order_by(runs.c.created_at.desc(), runs.c.run_id.desc()),
                workspace_id,
                visible_to,
            )
            rows = (await session.execute(query)).mappings().all()
            positions = await self._queue_positions_for(
                session,
                [
                    row["run_id"]
                    for row in rows
                    if row["status"] == RunStatus.QUEUED.value
                ],
            )
            summaries = []
            for row in rows:
                shared = (
                    SharePermission(row["_share_permission"])
                    if row.get("_share_permission") is not None
                    and not _visible_row(row, visible_to)
                    else None
                )
                summaries.append(
                    build_run_summary(
                        _summary_view(row),
                        queue_position=positions.get(row["run_id"]),
                        access=_access_annotation(
                            shared,
                            owner_user_id=row["created_by_user_id"],
                        ),
                    )
                )
            return summaries

    async def _list_page_db(
        self,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        limit: int,
        after: tuple[float, str] | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            query = self._apply_run_visibility(
                _summary_select(),
                workspace_id,
                visible_to,
            )
            if after is not None:
                # Keyset over the (created_at, run_id) index — the run_id
                # tiebreaker is mandatory (created_at is a float epoch and
                # collides on bulk inserts, so a created_at-only cursor
                # would skip or repeat rows).
                query = query.where(
                    tuple_(runs.c.created_at, runs.c.run_id)
                    < tuple_(after[0], after[1])
                )
            query = query.order_by(
                runs.c.created_at.desc(), runs.c.run_id.desc()
            ).limit(limit + 1)
            rows = (await session.execute(query)).mappings().all()
            window = list(rows[: limit + 1])
            page_rows = window[:limit]
            next_cursor = (
                encode_cursor(page_rows[-1]["created_at"], page_rows[-1]["run_id"])
                if len(window) > limit and page_rows
                else None
            )
            positions = await self._queue_positions_for(
                session,
                [
                    row["run_id"]
                    for row in page_rows
                    if row["status"] == RunStatus.QUEUED.value
                ],
            )
            summaries = []
            for row in page_rows:
                shared = (
                    SharePermission(row["_share_permission"])
                    if row.get("_share_permission") is not None
                    and not _visible_row(row, visible_to)
                    else None
                )
                summaries.append(
                    build_run_summary(
                        _summary_view(row),
                        queue_position=positions.get(row["run_id"]),
                        access=_access_annotation(
                            shared,
                            owner_user_id=row["created_by_user_id"],
                        ),
                    )
                )
            return summaries, next_cursor

    async def _result_db(
        self,
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row, _shared = await self._visible_row_db(
                session, run_id, workspace_id, visible_to
            )
            if row["result"] is None:
                raise RunNotFound(run_id)
            return {
                "run_id": run_id,
                "status": row["status"],
                **row["result"],
            }

    async def _authorized_control_write_db(
        self,
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
        control_write: Any,
    ) -> tuple[Any, list[tuple[str, str, tuple[str, ...]]]]:
        """Run a control callback and optional child cancel in one transaction."""
        cancellations: list[tuple[str, str, tuple[str, ...]]] = []
        woken_parents: set[str] = set()
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            actor_user_id = (
                visible_to.principal.user_id if visible_to is not None else None
            )
            row, root, access = await self._lock_execution_path_db(
                session,
                run_id,
                actor_user_id=actor_user_id,
                use_execution_actor=False,
            )
            if access is None or not _workspace_matches_row(row, workspace_id):
                raise RunNotFound(run_id)

            async def _cancel_child(child_run_id: str) -> str:
                child, child_root, child_access = await self._lock_execution_path_db(
                    session,
                    child_run_id,
                    actor_user_id=actor_user_id,
                    use_execution_actor=False,
                )
                if (
                    child_access is None
                    or child["parent_run_id"] != run_id
                    or child_root["run_id"] != root["run_id"]
                ):
                    raise RunNotFound(child_run_id)
                subtree = await self._lock_run_subtree_db(session, child_run_id)
                affected_ids = tuple(str(item["run_id"]) for item in subtree)
                for item in subtree:
                    if item["status"] in _TERMINAL_VALUES:
                        continue
                    woken = await self._cancel_row_db(
                        session,
                        str(item["run_id"]),
                        str(item["tenant_id"]),
                        str(item["status"]),
                    )
                    if woken is not None:
                        woken_parents.add(woken)
                await append_resource_effects(
                    session,
                    tenant_id=str(root["tenant_id"]),
                    actor_user_id=actor_user_id,
                    owner_user_id=access.owner_user_id,
                    action="run.cancel_requested",
                    resource_type="run",
                    resource_id=child_run_id,
                    scope="runs",
                )
                fresh = await self._row_db(session, child_run_id)
                child_status = str(fresh.status)
                cancellations.append((child_run_id, child_status, affected_ids))
                return child_status

            result = await control_write(session, _cancel_child)
        for parent_run_id in sorted(woken_parents):
            self._parents_to_wake.put(parent_run_id)
        return result, cancellations

    async def _cancel_db(
        self,
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> tuple[dict[str, Any], list[str]]:
        """Cancel one run tree and return every descendant id.

        Active descendants receive the cancellation transition. Terminal
        descendants are still returned so control-row reconciliation sees
        the same complete tree as the in-memory store.
        """
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            actor_user_id = (
                visible_to.principal.user_id if visible_to is not None else None
            )
            row, _root, access = await self._lock_execution_path_db(
                session,
                run_id,
                actor_user_id=actor_user_id,
                use_execution_actor=False,
            )
            if access is None:
                raise RunNotFound(run_id)
            if not _workspace_matches_row(row, workspace_id):
                raise RunNotFound(run_id)
            shared = (
                SharePermission.EDIT
                if actor_user_id is not None and actor_user_id != access.owner_user_id
                else None
            )
            # Cascade: an agent run cancels its whole tree — children are
            # real runs a client may not even know about, so the parent
            # cancel is responsible for them (authorization was the
            # parent's; children inherit it). The per-child
            # wake probe no-ops here: the parent went terminal first.
            child_rows = (await self._lock_run_subtree_db(session, run_id))[1:]
            woken = await self._cancel_row_db(
                session, run_id, row["tenant_id"], row["status"]
            )
            cascaded: list[str] = []
            for child in child_rows:
                child_id = child["run_id"]
                child_tenant = child["tenant_id"]
                child_status = child["status"]
                cascaded.append(child_id)
                if child_status not in _TERMINAL_VALUES:
                    await self._cancel_row_db(
                        session, child_id, child_tenant, child_status
                    )
            await append_resource_effects(
                session,
                tenant_id=row["tenant_id"],
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="run.cancel_requested",
                resource_type="run",
                resource_id=run_id,
                scope="runs",
            )
            fresh = await self._row_db(session, run_id)
            position = (
                await self._queue_position_db(session, fresh.created_at)
                if fresh.status == RunStatus.QUEUED.value
                else None
            )
            summary = build_run_summary(
                fresh,
                queue_position=position,
                access=_access_annotation(
                    shared,
                    owner_user_id=row["created_by_user_id"],
                ),
            )
        if woken:
            self._parents_to_wake.put(woken)
        return summary, cascaded

    async def _cancel_row_db(
        self,
        session: "AsyncSession",
        run_id: str,
        tenant_id: str,
        status: str,
        *,
        cascade_reason: str | None = None,
    ) -> str | None:
        """Apply the status-appropriate cancel transition to one row.

        Every CAS can lose to a concurrent transition (claim, park,
        resume); each miss re-reads and degrades to the branch for the
        FRESH status instead of a silent no-op, so no run slips
        through a cancel because it changed state underneath it.

        ``cascade_reason`` overrides the per-state cancel reason on the
        emitted event (the parent-failure cascade passes ``parent_failed``
        so an orphan's cancel says WHY); ``None`` keeps the state default.

        Returns:
            The parent run id when this cancel terminally ended an
            agent child whose last sibling was already terminal (the
            wake probe fired — the caller hands the id to the
            post-commit dispatch); ``None`` otherwise. Immediate QUEUED
            and WAITING cancellations terminate here; a RUNNING child
            terminates later via ``_terminal_db``, which carries its
            own probe.
        """
        if status in _WAITING_VALUES:
            landed, woken_parent = await self._cancel_waiting_row_db(
                session, run_id, tenant_id, cascade_reason=cascade_reason
            )
            if landed:
                return woken_parent
            # Lost the CAS to a concurrent resume (or the run went
            # terminal, which is absorbing): degrade to the fresh
            # status — a resumed run must still receive the cancel.
            fresh = (
                await session.execute(
                    select(runs.c.status).where(runs.c.run_id == run_id)
                )
            ).scalar_one_or_none()
            if fresh not in (
                RunStatus.QUEUED.value,
                RunStatus.RUNNING.value,
            ):
                return None
            status = fresh
        if status == RunStatus.QUEUED.value:
            now = time.time()
            cancelled = (
                await session.execute(
                    update(runs)
                    .where(
                        runs.c.run_id == run_id,
                        runs.c.status == RunStatus.QUEUED.value,
                    )
                    .values(
                        status=RunStatus.CANCELLED.value,
                        cancel_requested=True,
                        finished_at=now,
                        **_terminal_timing_values(now),
                    )
                    .returning(
                        runs.c.kind,
                        runs.c.parent_run_id,
                        runs.c.request_payload,
                        runs.c.snapshot,
                        runs.c.attempt,
                    )
                )
            ).first()
            if cancelled is not None:
                return await self._record_terminal_run_db(
                    session,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    kind=str(cancelled[0] or "standard"),
                    parent_run_id=cancelled[1],
                    request_payload=dict(cancelled[2] or {}),
                    status=RunStatus.CANCELLED.value,
                    event_type="inqtrix.run.cancelled",
                    payload={
                        "status": "cancelled",
                        "reason": cascade_reason or "cancelled_before_start",
                    },
                    snapshot=dict(cancelled[3] or {}),
                    attempt=int(cancelled[4] or 0) or None,
                )
            else:
                # Lost the CAS to a concurrent claim: the run is
                # running now — degrade to the two-phase cancel
                # request instead of a stale no-op.
                status = RunStatus.RUNNING.value
        if status == RunStatus.RUNNING.value:
            # Guarded like the terminal writes: the row may have
            # gone terminal between the read and this statement —
            # a cancel_requested event AFTER the terminal event
            # would corrupt the log's ends-terminal invariant.
            requested = (
                await session.execute(
                    update(runs)
                    .where(
                        runs.c.run_id == run_id,
                        runs.c.status == RunStatus.RUNNING.value,
                    )
                    .values(cancel_requested=True)
                    .returning(runs.c.run_id)
                )
            ).scalar_one_or_none()
            if requested is not None:
                await self._append_events_db(
                    session,
                    run_id,
                    tenant_id,
                    expand_run_event(
                        "inqtrix.run.cancel_requested",
                        {
                            "status": "running",
                            "reason": (cascade_reason or "client_requested_cancel"),
                        },
                        status=RunStatus.RUNNING.value,
                    )[1],
                )
                return None
            # Lost the CAS: the run went terminal (absorbing, nothing
            # to do) OR it just PARKED itself — a parked run must not
            # slip through a cancel silently, so re-read and resolve
            # the wait immediately.
            fresh = (
                await session.execute(
                    select(runs.c.status).where(runs.c.run_id == run_id)
                )
            ).scalar_one_or_none()
            if fresh in _WAITING_VALUES:
                # The degrade path keeps the cascade attribution: a
                # parent-failure cancel that lost the RUNNING CAS to a
                # concurrent park must not flip to the generic
                # cancelled_while_waiting reason.
                _landed, woken_parent = await self._cancel_waiting_row_db(
                    session,
                    run_id,
                    tenant_id,
                    cascade_reason=cascade_reason,
                )
                return woken_parent
        return None

    async def _cancel_waiting_row_db(
        self,
        session: "AsyncSession",
        run_id: str,
        tenant_id: str,
        *,
        cascade_reason: str | None = None,
    ) -> tuple[bool, str | None]:
        """Cancel one waiting row immediately (nothing is executing).

        ``cascade_reason`` overrides the emitted cancel reason (the
        parent-failure cascade passes ``parent_failed``); ``None`` keeps
        the ``cancelled_while_waiting`` default.

        Attribution channel: the reason lives ON THE EVENTS —
        ``inqtrix.run.cancel_requested`` and ``inqtrix.run.cancelled``
        both carry it in their payload, on every path including the
        RUNNING->parked degrade. Run snapshots deliberately carry no
        cancel-reason field; consumers read the event stream.

        Returns:
            ``(landed, woken_parent)``. ``landed=False`` means the row left
            the waiting statuses concurrently and the caller must degrade to
            the fresh status. A non-null parent id means this was its last
            active child and the post-commit dispatcher must wake it.
        """
        now = time.time()
        waited = (
            await session.execute(
                update(runs)
                .where(
                    runs.c.run_id == run_id,
                    runs.c.status.in_(_WAITING_VALUES),
                )
                .values(
                    status=RunStatus.CANCELLED.value,
                    cancel_requested=True,
                    finished_at=now,
                    **_terminal_timing_values(now),
                )
                .returning(
                    runs.c.run_id,
                    runs.c.kind,
                    runs.c.parent_run_id,
                    runs.c.request_payload,
                    runs.c.snapshot,
                    runs.c.attempt,
                )
            )
        ).first()
        if waited is None:
            return False, None
        woken_parent = await self._record_terminal_run_db(
            session,
            run_id=run_id,
            tenant_id=tenant_id,
            kind=str(waited[1] or "standard"),
            parent_run_id=waited[2],
            request_payload=dict(waited[3] or {}),
            status=RunStatus.CANCELLED.value,
            event_type="inqtrix.run.cancelled",
            payload={
                "status": "cancelled",
                "reason": cascade_reason or "cancelled_while_waiting",
            },
            snapshot=dict(waited[4] or {}),
            attempt=int(waited[5] or 0) or None,
        )
        return True, woken_parent

    async def _mark_waiting_db(
        self,
        run_id: str,
        waiting: RunStatus,
        fence_attempt: int | None = None,
    ) -> bool:
        """Park one RUNNING row; returns whether it parked.

        ``False`` means a pending cancel request won instead: the row
        went terminal ``cancelled`` (reason ``cancelled_while_waiting``)
        — parking it would leave a cancelled assignment waiting until
        its TTL (memory-store parity).
        """
        woken_parent: str | None = None
        parked = False
        async with self._session(DEFAULT_TENANT) as session:
            _locked, _root, access = await self._lock_execution_path_db(session, run_id)
            if access is None:
                raise AuthorizationRevoked(
                    "run wait discarded after execution authority changed"
                )
            now = time.time()
            query = (
                update(runs)
                .where(
                    runs.c.run_id == run_id,
                    runs.c.status == RunStatus.RUNNING.value,
                    runs.c.cancel_requested.is_(False),
                )
                .values(
                    status=waiting.value,
                    waiting_since=now,
                    active_seconds=(
                        runs.c.active_seconds
                        + _elapsed_sql(runs.c.active_started_at, now)
                    ),
                    active_started_at=None,
                )
                .returning(
                    runs.c.snapshot,
                    runs.c.tenant_id,
                    runs.c.kind,
                    runs.c.parent_run_id,
                    runs.c.request_payload,
                    runs.c.attempt,
                )
            )
            if fence_attempt is not None:
                # Queue-worker park: a reclaimed zombie must not park a
                # run the live attempt owns (same fence as the terminal
                # writes).
                query = query.where(
                    runs.c.claimed_by == self._worker_id,
                    runs.c.attempt == fence_attempt,
                )
            row = (await session.execute(query)).first()
            if row is not None:
                parked = True
                waiting_payload = {
                    "status": waiting.value,
                    "snapshot": dict(row[0] or {}),
                }
                _, events = expand_run_event(
                    "inqtrix.run.waiting",
                    waiting_payload,
                    status=waiting.value,
                )
                await self._append_events_db(session, run_id, row[1], events)
                # A CHILD's park is the parent's gate signal: the
                # waiting event is on the projection whitelist, and the
                # memory twin projects it through its emit chokepoint —
                # this write path appends directly, so it must project
                # explicitly or a parked child stays invisible on every
                # parent surface.
                await self._project_child_progress_db(
                    session,
                    child_run_id=run_id,
                    kind=str(row[2] or "standard"),
                    parent_run_id=row[3],
                    request_payload=dict(row[4] or {}),
                    run_status=waiting.value,
                    event_type="inqtrix.run.waiting",
                    payload=waiting_payload,
                    snapshot=dict(row[0] or {}),
                    attempt=int(row[5]),
                )
                if waiting is RunStatus.WAITING_FOR_CHILDREN:
                    # Lost-wakeup self-heal, in the SAME transaction as
                    # the park: the last child may have gone terminal
                    # while this run was still unwinding towards the
                    # park — its wake probe then found the parent
                    # RUNNING and no-oped. Re-probing here closes the
                    # window: either the terminal write commits first
                    # (this probe sees zero outstanding and flips
                    # straight back to queued) or the park commits
                    # first (the terminal probe sees the waiting row).
                    woken_parent = await self._maybe_wake_parent_db(
                        session, parent_run_id=run_id
                    )
                if woken_parent is None:
                    return True
                # Self-heal hit: fall out of the session block so the
                # transaction commits, then hand the id to the
                # post-commit dispatch below.
            else:
                woken_parent = await self._resolve_waiting_miss_db(
                    session, run_id, fence_attempt
                )
                if woken_parent is None:
                    return False
        # The self-heal and cancelled-child paths both wake only after their
        # transaction committed. A root cancellation has no parent handoff.
        if woken_parent:
            self._parents_to_wake.put(woken_parent)
        return parked

    async def _resolve_waiting_miss_db(
        self,
        session: "AsyncSession",
        run_id: str,
        fence_attempt: int | None,
    ) -> str | None:
        """Resolve a missed park CAS: cancel, fence violation, or bug.

        Returns the parent id when cancelling an agent child woke its parent,
        otherwise ``None``. Every non-cancel miss raises loudly.
        """
        # The summary view (_row_db) omits the fencing pair, so the
        # fresh read selects it directly.
        current = (
            await session.execute(
                select(runs.c.status, runs.c.claimed_by, runs.c.attempt).where(
                    runs.c.run_id == run_id
                )
            )
        ).first()
        if current is None:
            raise RunNotFound(run_id)
        if (
            fence_attempt is not None
            and current.status == RunStatus.RUNNING.value
            and (
                current.claimed_by != self._worker_id
                or current.attempt != fence_attempt
            )
        ):
            # Fenced miss on a RUNNING row owned by ANOTHER attempt:
            # a zombie must not fall through into the (unfenced)
            # cancel resolution below — that would kill the LIVE
            # attempt's execution. A fenced miss on the OWN attempt
            # means cancel_requested blocked the park; that case
            # falls through and resolves as cancel (memory parity).
            raise RunActive(f"run {run_id} is owned by another worker attempt")
        if current.status == RunStatus.RUNNING.value:
            # RUNNING but cancel_requested: resolve the pending
            # cancel now instead of parking.
            now = time.time()
            cancelled = (
                await session.execute(
                    update(runs)
                    .where(
                        runs.c.run_id == run_id,
                        runs.c.status == RunStatus.RUNNING.value,
                    )
                    .values(
                        status=RunStatus.CANCELLED.value,
                        finished_at=now,
                        **_terminal_timing_values(now),
                    )
                    .returning(
                        runs.c.snapshot,
                        runs.c.tenant_id,
                        runs.c.kind,
                        runs.c.parent_run_id,
                        runs.c.request_payload,
                        runs.c.attempt,
                    )
                )
            ).first()
            if cancelled is not None:
                payload = {
                    "status": "cancelled",
                    "reason": "cancelled_while_waiting",
                    "snapshot": dict(cancelled[0] or {}),
                }
                return await self._record_terminal_run_db(
                    session,
                    run_id=run_id,
                    tenant_id=cancelled[1],
                    kind=str(cancelled[2] or "standard"),
                    parent_run_id=cancelled[3],
                    request_payload=dict(cancelled[4] or {}),
                    status=RunStatus.CANCELLED.value,
                    event_type="inqtrix.run.cancelled",
                    payload=payload,
                    snapshot=dict(cancelled[0] or {}),
                    attempt=int(cancelled[5] or 0) or None,
                )
            current = await self._row_db(session, run_id)
        # Not RUNNING (terminal or a caller bug) — loud, memory
        # parity: RunNotFound came from _row_db above if missing.
        raise RunActive(f"run {run_id} cannot wait from status {current.status}")

    async def _resume_db(
        self,
        run_id: str,
        actor_user_id: uuid.UUID | None,
        execution_scopes: frozenset[str],
        control_write: Any = None,
    ) -> dict[str, Any]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
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
                raise RunNotFound(run_id)
            current = (
                (await session.execute(select(runs).where(runs.c.run_id == run_id)))
                .mappings()
                .first()
            )
            if current is None:
                raise RunNotFound(run_id)
            if current["status"] not in _WAITING_VALUES:
                raise RunActive(
                    f"run {run_id} is not waiting (status {current['status']})"
                )
            now = time.time()
            segment_ordinal = int(current["segment_count"] or 0) + 1
            segment_id = run_segment_id(run_id, segment_ordinal)
            values: dict[str, Any] = {
                "status": RunStatus.QUEUED.value,
                "waiting_since": None,
                "waiting_seconds": (
                    runs.c.waiting_seconds + _elapsed_sql(runs.c.waiting_since, now)
                ),
                "queued_since": now,
                "segment_count": segment_ordinal,
                "current_segment_id": segment_id,
                "current_segment_reason": _resume_reason_value(str(current["status"])),
            }
            if actor_user_id is not None:
                values.update(
                    execution_actor_user_id=actor_user_id,
                    execution_scopes=sorted(execution_scopes),
                )
            row = (
                await session.execute(
                    update(runs)
                    .where(
                        runs.c.run_id == run_id,
                        runs.c.status.in_(_WAITING_VALUES),
                    )
                    .values(**values)
                    .returning(runs.c.tenant_id, runs.c.created_at)
                )
            ).first()
            if row is None:
                raise RunActive(
                    f"run {run_id} is not waiting (status {current['status']})"
                )
            if control_write is not None:
                # The control-store decision commits or rolls back
                # together with the status flip above.
                await control_write(session)
            position = await self._queue_position_db(session, row[1])
            _, events = expand_run_event(
                "inqtrix.run.queued",
                {
                    "status": "queued",
                    "queue_position": position,
                    "resumed": True,
                    "segment_id": segment_id,
                    "segment_ordinal": segment_ordinal,
                },
                status=RunStatus.QUEUED.value,
            )
            await self._append_events_db(session, run_id, row[0], events)
            await append_resource_effects(
                session,
                tenant_id=current["tenant_id"],
                actor_user_id=actor_user_id,
                owner_user_id=access.owner_user_id,
                action="run.resumed",
                resource_type="run",
                resource_id=run_id,
                scope="runs",
            )
            fresh = await self._row_db(session, run_id)
            return build_run_summary(fresh, queue_position=position)

    async def _children_db(self, run_id: str) -> list[dict[str, Any]]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            rows = (
                (
                    await session.execute(
                        _summary_select()
                        .where(runs.c.parent_run_id == run_id)
                        .order_by(runs.c.created_at.desc())
                    )
                )
                .mappings()
                .all()
            )
            # ONE window-function scan for the queued children's positions,
            # not a COUNT per child — the /children endpoint is polled by the
            # Agent Desk, so a wide fan-out must not issue N queue COUNTs
            # (mirrors _list_db / _list_page_db).
            positions = await self._queue_positions_for(
                session,
                [
                    row["run_id"]
                    for row in rows
                    if row["status"] == RunStatus.QUEUED.value
                ],
            )
            return [
                build_run_summary(
                    _summary_view(row),
                    queue_position=positions.get(row["run_id"]),
                )
                for row in rows
            ]

    async def _replay_db(
        self,
        run_id: str,
        workspace_id: str | None,
        visible_to: "UserContext | None",
    ) -> tuple[str, list[dict[str, Any]]]:
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)
            row, _shared = await self._visible_row_db(
                session, run_id, workspace_id, visible_to
            )
            events = await self._fetch_events(session, run_id, 0)
            return row["tenant_id"], events

    async def _events_after_db(
        self, run_id: str, tenant_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        async with self._session(tenant_id) as session:
            return await self._fetch_events(session, run_id, after_sequence)

    async def _fetch_events(
        self, session: "AsyncSession", run_id: str, after_sequence: int
    ) -> list[dict[str, Any]]:
        rows = (
            (
                await session.execute(
                    select(run_events)
                    .where(
                        run_events.c.run_id == run_id,
                        run_events.c.sequence > after_sequence,
                    )
                    .order_by(run_events.c.sequence)
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "type": row["type"],
                "run_id": run_id,
                "sequence": row["sequence"],
                "created_at": row["created_at"],
                "data": row["data"],
            }
            for row in rows
        ]

    async def _emit_db(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        fence_attempt: int | None = None,
    ) -> None:
        async with self._session(DEFAULT_TENANT) as session:
            row, _root, access = await self._lock_execution_path_db(session, run_id)
            if access is None:
                raise AuthorizationRevoked(
                    "run event discarded after execution authority changed"
                )
            if fence_attempt is not None:
                # Atomic fence: the sequence allocation itself carries
                # the ownership condition, so a takeover between a
                # check and the write cannot interleave zombie events.
                fenced_seq = (
                    await session.execute(
                        update(runs)
                        .where(
                            runs.c.run_id == run_id,
                            runs.c.claimed_by == self._worker_id,
                            runs.c.attempt == fence_attempt,
                        )
                        .values(event_seq=runs.c.event_seq + 0)
                        .returning(runs.c.event_seq)
                    )
                ).scalar_one_or_none()
                if fenced_seq is None:
                    log.warning(
                        "Event %s for run %s discarded; the run now belongs to another "
                        "worker attempt.",
                        event_type,
                        run_id,
                    )
                    return
            if row["status"] in _TERMINAL_VALUES:
                # The log's ends-terminal invariant is what stops SSE
                # streams; post-terminal signals are dropped LOUDLY
                # (memory parity) — finished-run clients reconcile via
                # the REST reads.
                log.warning(
                    "Event %s for run %s discarded: the run is already terminal (%s), "
                    "and the event log ends with the terminal event.",
                    event_type,
                    run_id,
                    row["status"],
                )
                return
            new_snapshot, events = expand_run_event(
                event_type, payload, status=row["status"]
            )
            if new_snapshot is not None:
                await session.execute(
                    update(runs)
                    .where(runs.c.run_id == run_id)
                    .values(snapshot=new_snapshot)
                )
            await self._append_events_db(session, run_id, row["tenant_id"], events)
            await self._project_child_progress_db(
                session,
                child_run_id=run_id,
                kind=str(row["kind"] or "standard"),
                parent_run_id=row["parent_run_id"],
                request_payload=dict(row["request_payload"] or {}),
                run_status=str(row["status"]),
                event_type=event_type,
                payload=payload,
                snapshot=(
                    new_snapshot
                    if new_snapshot is not None
                    else dict(row["snapshot"] or {})
                ),
                attempt=int(row["attempt"] or 0) or None,
            )

    async def _append_events_db(
        self,
        session: "AsyncSession",
        run_id: str,
        tenant_id: str,
        events: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for event_type, data in events:
            sequence = (
                await session.execute(
                    update(runs)
                    .where(runs.c.run_id == run_id)
                    .values(event_seq=runs.c.event_seq + 1)
                    .returning(runs.c.event_seq)
                )
            ).scalar_one()
            await session.execute(
                insert(run_events).values(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    sequence=sequence,
                    type=event_type,
                    created_at=time.time(),
                    data=data,
                )
            )

    async def _project_child_progress_db(
        self,
        session: "AsyncSession",
        *,
        child_run_id: str,
        kind: str,
        parent_run_id: str | None,
        request_payload: dict[str, Any],
        run_status: str,
        event_type: str,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        attempt: int | None = None,
    ) -> None:
        """Append one bounded child projection to its parent event log."""
        body = request_payload.get("body") or {}
        parent_task_id = (
            str(body.get("parent_task_id") or "") if isinstance(body, dict) else ""
        )
        logical_attempt = (
            int(body.get("parent_task_attempt", 0) or 0)
            if isinstance(body, dict)
            else 0
        )
        if (
            kind != "agent_child"
            or not parent_run_id
            or not parent_task_id
            # `payload` is the child EVENT's payload; `body` is the child
            # run's request body (parent ids, work profile) — the gate reads
            # the event and whether the child is a kernel role run.
            or not should_project_child_event(
                event_type,
                payload,
                work_profile=(
                    str(body.get("work_profile") or "") or None
                    if isinstance(body, dict)
                    else None
                ),
            )
        ):
            return
        parent = (
            await session.execute(
                select(runs.c.status, runs.c.tenant_id)
                .where(
                    runs.c.run_id == parent_run_id,
                    runs.c.status.notin_(_TERMINAL_VALUES),
                )
                .with_for_update()
            )
        ).first()
        if parent is None:
            return
        projected = build_child_progress_payload(
            child_run_id=child_run_id,
            parent_task_id=parent_task_id,
            run_status=run_status,
            event_type=event_type,
            payload=payload,
            snapshot=snapshot,
            attempt=logical_attempt or attempt,
        )
        await self._append_events_db(
            session,
            parent_run_id,
            parent[1],
            expand_run_event(
                CHILD_PROGRESS_EVENT,
                projected,
                status=parent[0],
            )[1],
        )

    async def _record_terminal_run_db(
        self,
        session: "AsyncSession",
        *,
        run_id: str,
        tenant_id: str,
        kind: str,
        parent_run_id: str | None,
        request_payload: dict[str, Any],
        status: str,
        event_type: str,
        payload: dict[str, Any],
        snapshot: dict[str, Any],
        attempt: int | None,
        audit_detail: dict[str, Any] | None = None,
        audit_correlation: dict[str, str] | None = None,
    ) -> str | None:
        """Persist one terminal signal, child projection, and parent wake.

        The terminal audit row (Dienststart-Index) is written HERE, in
        the same transaction, through the ONE pre-existing
        ``run.{status}`` effects write — outcome derives from the
        status, correlation always carries the run id, and the primary
        execution path passes tokens/duration/trace via the two audit
        parameters. One row per terminal, never two.
        """
        await self._append_events_db(
            session,
            run_id,
            tenant_id,
            expand_run_event(event_type, payload, status=status)[1],
        )
        await self._project_child_progress_db(
            session,
            child_run_id=run_id,
            kind=kind,
            parent_run_id=parent_run_id,
            request_payload=request_payload,
            run_status=status,
            event_type=event_type,
            payload=payload,
            snapshot=snapshot,
            attempt=attempt,
        )
        authority = (
            await session.execute(
                select(
                    runs.c.created_by_user_id,
                    runs.c.execution_actor_user_id,
                ).where(runs.c.run_id == run_id)
            )
        ).first()
        if authority is not None:
            await append_resource_effects(
                session,
                tenant_id=tenant_id,
                actor_user_id=authority.execution_actor_user_id,
                owner_user_id=authority.created_by_user_id,
                action=f"run.{status}",
                resource_type="run",
                resource_id=run_id,
                scope="runs",
                outcome=(
                    "success" if status == "completed" else "failure"
                ),
                detail=audit_detail,
                correlation={
                    "run_id": run_id,
                    **(audit_correlation or {}),
                },
            )
        if kind == "agent_child" and parent_run_id:
            return await self._maybe_wake_parent_db(
                session, parent_run_id=parent_run_id
            )
        return None

    async def _terminal_db(
        self,
        run_id: str,
        status: RunStatus,
        *,
        fence_attempt: int | None,
        event_builder,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        warn_on_noop: bool = True,
        exclude_waiting: bool = False,
    ) -> bool:
        woken_parent: str | None = None
        async with self._session(DEFAULT_TENANT) as session:
            try:
                _locked, _root, access = await self._lock_execution_path_db(
                    session, run_id
                )
            except RunNotFound:
                if warn_on_noop:
                    log.warning(
                        "Terminal write for missing run %s was discarded.",
                        run_id,
                    )
                return False
            if access is None and status is not RunStatus.CANCELLED:
                revoked_error = {
                    "message": "Execution authority was revoked",
                    "type": AuthorizationRevoked.code,
                }
                status = RunStatus.FAILED
                result = None
                error = revoked_error
                snapshot = None
                event_builder = lambda row_snapshot: (
                    "inqtrix.run.failed",
                    {
                        "status": "failed",
                        "error": revoked_error,
                        "snapshot": row_snapshot,
                    },
                )
                log.warning(
                    "Run %s terminalized as authorization_revoked; "
                    "the attempted execution result was discarded.",
                    run_id,
                )
            now = time.time()
            values: dict[str, Any] = {
                "status": status.value,
                "finished_at": now,
                **_terminal_timing_values(now),
            }
            if result is not None:
                values["result"] = result
            if error is not None:
                values["error"] = error
            if snapshot is not None:
                values["snapshot"] = snapshot
            query = (
                update(runs)
                .where(
                    runs.c.run_id == run_id,
                    runs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(**values)
                .returning(
                    runs.c.snapshot,
                    runs.c.tenant_id,
                    runs.c.kind,
                    runs.c.parent_run_id,
                    runs.c.request_payload,
                    runs.c.attempt,
                    runs.c.mode,
                    runs.c.created_by_user_id,
                    runs.c.workspace_id,
                    runs.c.created_at,
                )
            )
            if exclude_waiting:
                # Auto-complete safety net only: a parked (waiting) run
                # is not "still running work that forgot to complete" —
                # completing it would destroy the interrupt.
                query = query.where(runs.c.status.notin_(_WAITING_VALUES))
            if fence_attempt is not None:
                query = query.where(
                    runs.c.claimed_by == self._worker_id,
                    runs.c.attempt == fence_attempt,
                )
            row = (await session.execute(query)).first()
            if row is None:
                # Already terminal, missing, or fenced out (a reclaimed
                # zombie) — absorbing states stay absorbing. The
                # discarded write is operator-visible, EXCEPT for the
                # auto-complete safety net whose no-op is the expected
                # happy path (the work already completed itself).
                if warn_on_noop:
                    log.warning(
                        "Terminal write for run %s discarded because it is already "
                        "terminal or was claimed by another worker.",
                        run_id,
                    )
                return False
            event_type, payload = event_builder(dict(row[0] or {}))
            # Dienststart-Index metadata for the terminal audit row
            # (written inside _record_terminal_run_db, same tx): mode,
            # duration, token sums, error type, trace id — never
            # content. Fenced-out/no-op writes never reach this line,
            # so zombie attempts cannot forge index rows.
            audit_detail: dict[str, Any] | None = None
            audit_correlation: dict[str, str] | None = None
            if self._audit_service_starts:
                audit_detail = {"mode": str(row[6] or "") or "standard"}
                if row[9]:
                    audit_detail["duration_s"] = round(
                        max(0.0, now - float(row[9])), 2
                    )
                usage = (result or {}).get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens") or 0)
                completion_tokens = int(
                    usage.get("completion_tokens") or 0
                )
                if prompt_tokens or completion_tokens:
                    audit_detail["prompt_tokens"] = prompt_tokens
                    audit_detail["completion_tokens"] = completion_tokens
                if error and error.get("type"):
                    audit_detail["error_type"] = str(error["type"])
                trace_rows = await session.execute(
                    select(run_events.c.data)
                    .where(
                        run_events.c.run_id == run_id,
                        run_events.c.type == "inqtrix.run.trace",
                    )
                    .order_by(run_events.c.sequence.desc())
                    .limit(1)
                )
                trace_row = trace_rows.first()
                if trace_row is not None:
                    trace_hex = str(
                        (trace_row[0] or {}).get("trace_id") or ""
                    )
                    if trace_hex:
                        audit_correlation = {"trace_id": trace_hex}
            woken_parent = await self._record_terminal_run_db(
                session,
                run_id=run_id,
                tenant_id=row[1],
                kind=str(row[2] or "standard"),
                parent_run_id=row[3],
                request_payload=dict(row[4] or {}),
                status=status.value,
                event_type=event_type,
                payload=payload,
                snapshot=dict(row[0] or {}),
                attempt=int(row[5] or 0) or None,
                audit_detail=audit_detail,
                audit_correlation=audit_correlation,
            )
            if status is RunStatus.FAILED:
                # Parent terminal failure cascades to live descendants
                # (orphans burning quota behind a dead parent) — FENCED:
                # this branch runs only after the terminal CAS above
                # landed, so a reclaimed zombie attempt can never cancel
                # anyone. Same transaction, same subtree lock order as
                # cancel_tree.
                child_rows = (await self._lock_run_subtree_db(session, run_id))[1:]
                cascaded: list[str] = []
                for child in child_rows:
                    if child["status"] in _TERMINAL_VALUES:
                        continue
                    cascaded.append(child["run_id"])
                    await self._cancel_row_db(
                        session,
                        child["run_id"],
                        child["tenant_id"],
                        child["status"],
                        cascade_reason="parent_failed",
                    )
                if cascaded:
                    log.warning(
                        "Parent run %s failed; %d active child runs cancelled "
                        "(parent_failed).",
                        run_id,
                        len(cascaded),
                    )
                    self._failed_cascades.put((run_id, tuple(cascaded)))
        if woken_parent:
            # Post-commit handoff: the sync mutators dispatch (queue
            # enqueue / local re-append) — never this loop coroutine.
            self._parents_to_wake.put(woken_parent)
        return True

    async def _maybe_wake_parent_db(
        self, session: "AsyncSession", *, parent_run_id: str
    ) -> str | None:
        """Flip a children-parked parent to QUEUED once no child is left.

        Runs inside the child's terminal transaction (or the park-time
        self-heal): the sibling probe and the parent CAS commit together
        with the child's transition. The CAS keys on
        ``waiting_for_children`` alone (resume is attempt-agnostic,
        exactly like ``_resume_db``); a parent still RUNNING — the child
        finished before the parent finished parking — makes this a no-op,
        closed by the park-time self-heal in ``_mark_waiting_db``.

        The caller already holds the canonical root-to-child lineage; this
        method re-locks the parent row (``FOR NO KEY UPDATE``) reentrantly.
        This is load-bearing under multi-worker queue mode: two of the
        parent's children terminating in CONCURRENT transactions on
        DIFFERENT workers run under READ COMMITTED, where each cannot see
        the other's uncommitted terminal write. Without the lock, both
        sibling probes would count the other child as still outstanding
        (=1) and BOTH decline to wake — a lost wake-up that strands the
        parent until the waiting TTL. The row lock serialises the two
        wake deciders on the parent: the later one blocks until the
        earlier commits, then re-reads the true committed count (0) and
        wakes. NO KEY UPDATE suffices — no PK/FK column is touched — and
        is lighter than FOR UPDATE. Child terminalization, parking, child
        admission, and cancellation all acquire lineage in the same
        root-to-leaf order; this parent lock is therefore never a child-to-
        parent inversion.

        Returns:
            The parent id when the flip landed (caller hands it to the
            post-commit dispatch), ``None`` otherwise.
        """
        locked = (
            await session.execute(
                select(runs.c.run_id, runs.c.segment_count)
                .where(runs.c.run_id == parent_run_id)
                .with_for_update(key_share=True)
            )
        ).first()
        if locked is None:
            return None
        outstanding = await session.scalar(
            select(func.count())
            .select_from(runs)
            .where(
                runs.c.parent_run_id == parent_run_id,
                runs.c.status.notin_(_TERMINAL_VALUES),
            )
        )
        if outstanding:
            return None
        now = time.time()
        segment_ordinal = int(locked[1] or 0) + 1
        segment_id = run_segment_id(parent_run_id, segment_ordinal)
        row = (
            await session.execute(
                update(runs)
                .where(
                    runs.c.run_id == parent_run_id,
                    runs.c.status == RunStatus.WAITING_FOR_CHILDREN.value,
                )
                .values(
                    status=RunStatus.QUEUED.value,
                    waiting_since=None,
                    waiting_seconds=(
                        runs.c.waiting_seconds + _elapsed_sql(runs.c.waiting_since, now)
                    ),
                    queued_since=now,
                    segment_count=segment_ordinal,
                    current_segment_id=segment_id,
                    current_segment_reason="children",
                )
                .returning(runs.c.tenant_id, runs.c.created_at)
            )
        ).first()
        if row is None:
            return None
        position = await self._queue_position_db(session, row[1])
        _, events = expand_run_event(
            "inqtrix.run.queued",
            {
                "status": "queued",
                "queue_position": position,
                "resumed": True,
                "segment_id": segment_id,
                "segment_ordinal": segment_ordinal,
            },
            status=RunStatus.QUEUED.value,
        )
        await self._append_events_db(session, parent_run_id, row[0], events)
        return parent_run_id

    async def _terminalize_authorization_revoked_db(
        self,
        session: "AsyncSession",
        *,
        run_id: str,
    ) -> str | None:
        """Fail one already lineage-locked run without requiring its share."""
        error = {
            "message": "Execution authority was revoked",
            "type": AuthorizationRevoked.code,
        }
        now = time.time()
        row = (
            await session.execute(
                update(runs)
                .where(
                    runs.c.run_id == run_id,
                    runs.c.status.notin_(_TERMINAL_VALUES),
                )
                .values(
                    status=RunStatus.FAILED.value,
                    result=None,
                    error=error,
                    finished_at=now,
                    **_terminal_timing_values(now),
                )
                .returning(
                    runs.c.snapshot,
                    runs.c.tenant_id,
                    runs.c.kind,
                    runs.c.parent_run_id,
                    runs.c.request_payload,
                    runs.c.attempt,
                )
            )
        ).first()
        if row is None:
            return None
        return await self._record_terminal_run_db(
            session,
            run_id=run_id,
            tenant_id=row[1],
            kind=str(row[2] or "standard"),
            parent_run_id=row[3],
            request_payload=dict(row[4] or {}),
            status=RunStatus.FAILED.value,
            event_type="inqtrix.run.failed",
            payload={
                "status": "failed",
                "error": error,
                "snapshot": dict(row[0] or {}),
            },
            snapshot=dict(row[0] or {}),
            attempt=int(row[5] or 0) or None,
        )

    async def _claim_db(
        self, run_id: str, tenant_id: str, *, allow_takeover: bool
    ) -> ClaimedRun | None:
        woken_parent: str | None = None
        claimed: ClaimedRun | None = None
        async with self._session(tenant_id) as session:
            # Cutover fence, FIRST statement of the claim transaction: a
            # worker whose image predates a completed migration must not
            # take a durable claim -- nor run this transaction's other
            # writes -- against a schema its code no longer matches. Rides
            # in the same transaction, so a mismatch rolls everything back
            # and the queue entry stays unacked for an upgraded worker.
            await assert_schema_head(session)
            try:
                _locked, _root, access = await self._lock_execution_path_db(
                    session, run_id
                )
            except RunNotFound:
                return None
            if access is None:
                woken_parent = await self._terminalize_authorization_revoked_db(
                    session, run_id=run_id
                )
                row = None
            elif bool(_locked["cancel_requested"]) and str(
                _locked["status"]
            ) in (
                (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
                if allow_takeover
                else (RunStatus.QUEUED.value,)
            ):
                # A cancel arrived while no live worker was watching this
                # row (its owner crashed before the poller observed it, or
                # the run re-queued with the request pending). Resolve the
                # cancel here instead of opening a doomed attempt that
                # would re-execute cancelled work. The RUNNING half is
                # takeover-only: without takeover authority this claim
                # must not touch a row a live owner still resolves.
                now = time.time()
                cancel_statuses = (
                    (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
                    if allow_takeover
                    else (RunStatus.QUEUED.value,)
                )
                cancelled = (
                    await session.execute(
                        update(runs)
                        .where(
                            runs.c.run_id == run_id,
                            runs.c.status.in_(cancel_statuses),
                        )
                        .values(
                            status=RunStatus.CANCELLED.value,
                            finished_at=now,
                            **_terminal_timing_values(now),
                        )
                        .returning(
                            runs.c.kind,
                            runs.c.parent_run_id,
                            runs.c.request_payload,
                            runs.c.snapshot,
                            runs.c.attempt,
                        )
                    )
                ).first()
                if cancelled is not None:
                    # The true reason lives on the cancel_requested event
                    # (a cascade may have set the flag, e.g.
                    # parent_failed/session_deleting) — read it back so
                    # the terminal event never relabels a cascade as a
                    # client action.
                    requested_reason = (
                        await session.execute(
                            select(run_events.c.data)
                            .where(
                                run_events.c.run_id == run_id,
                                run_events.c.type
                                == "inqtrix.run.cancel_requested",
                            )
                            .order_by(run_events.c.sequence.desc())
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    reason = (
                        str((requested_reason or {}).get("reason") or "")
                        or "client_requested_cancel"
                    )
                    woken_parent = await self._record_terminal_run_db(
                        session,
                        run_id=run_id,
                        tenant_id=tenant_id,
                        kind=str(cancelled[0] or "standard"),
                        parent_run_id=cancelled[1],
                        request_payload=dict(cancelled[2] or {}),
                        status=RunStatus.CANCELLED.value,
                        event_type="inqtrix.run.cancelled",
                        payload={
                            "status": "cancelled",
                            "reason": reason,
                        },
                        snapshot=dict(cancelled[3] or {}),
                        attempt=int(cancelled[4] or 0) or None,
                    )
                row = None
            else:
                previous_status = str(_locked["status"])
                now = time.time()
                initial_start = _locked["started_at"] is None
                resumed_dispatch = (
                    previous_status == RunStatus.QUEUED.value and not initial_start
                )
                segment_ordinal = int(_locked["segment_count"] or 0)
                segment_id = _locked["current_segment_id"]
                segment_reason = _locked["current_segment_reason"]
                if initial_start:
                    segment_ordinal += 1
                    segment_id = run_segment_id(run_id, segment_ordinal)
                    segment_reason = "initial"
                elif resumed_dispatch and (
                    not segment_id or segment_reason == "legacy"
                ):
                    segment_ordinal += 1
                    segment_id = run_segment_id(run_id, segment_ordinal)
                    segment_reason = "resume"
                allowed = [RunStatus.QUEUED.value]
                if allow_takeover:
                    allowed.append(RunStatus.RUNNING.value)
                values: dict[str, Any] = {
                    "status": RunStatus.RUNNING.value,
                    "claimed_by": self._worker_id,
                    "attempt": runs.c.attempt + 1,
                    "started_at": _locked["started_at"] or now,
                    "active_started_at": (
                        _locked["active_started_at"]
                        if previous_status == RunStatus.RUNNING.value
                        and _locked["active_started_at"] is not None
                        else now
                    ),
                    "queued_since": None,
                    "segment_count": segment_ordinal,
                    "current_segment_id": segment_id,
                    "current_segment_reason": segment_reason,
                }
                if previous_status == RunStatus.QUEUED.value and not initial_start:
                    values["queued_seconds"] = runs.c.queued_seconds + _elapsed_sql(
                        runs.c.queued_since, now
                    )
                row = (
                    (
                        await session.execute(
                            update(runs)
                            .where(
                                runs.c.run_id == run_id,
                                runs.c.status.in_(allowed),
                            )
                            .values(**values)
                            .returning(
                                runs.c.attempt,
                                runs.c.request_payload,
                                runs.c.snapshot,
                                runs.c.created_by_user_id,
                                runs.c.created_by_tenant_id,
                                runs.c.workspace_id,
                                runs.c.kind,
                                runs.c.parent_run_id,
                                runs.c.execution_actor_user_id,
                                runs.c.execution_scopes,
                                runs.c.current_segment_id,
                                runs.c.segment_count,
                                runs.c.current_segment_reason,
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                claimed = None
            else:
                lifecycle_event = (
                    "inqtrix.run.started"
                    if initial_start
                    else "inqtrix.run.resumed" if resumed_dispatch else None
                )
                if lifecycle_event is not None:
                    lifecycle_payload = {
                        "status": "running",
                        "snapshot": dict(row["snapshot"] or {}),
                        "segment_id": row["current_segment_id"],
                        "segment_ordinal": int(row["segment_count"] or 0),
                        "reason": row["current_segment_reason"],
                    }
                    await self._append_events_db(
                        session,
                        run_id,
                        tenant_id,
                        expand_run_event(
                            lifecycle_event,
                            lifecycle_payload,
                            status=RunStatus.RUNNING.value,
                        )[1],
                    )
                    await self._project_child_progress_db(
                        session,
                        child_run_id=run_id,
                        kind=str(row["kind"] or "standard"),
                        parent_run_id=row["parent_run_id"],
                        request_payload=dict(row["request_payload"] or {}),
                        run_status=RunStatus.RUNNING.value,
                        event_type=lifecycle_event,
                        payload=lifecycle_payload,
                        snapshot=dict(row["snapshot"] or {}),
                        attempt=int(row["attempt"]),
                    )
                claimed = ClaimedRun(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    attempt=int(row["attempt"]),
                    request_payload=dict(row["request_payload"] or {}),
                    kind=str(row["kind"] or "standard"),
                    created_by_user_id=row["created_by_user_id"],
                    created_by_tenant_id=row["created_by_tenant_id"],
                    workspace_id=row["workspace_id"],
                    execution_actor_user_id=row["execution_actor_user_id"],
                    execution_scopes=tuple(row["execution_scopes"] or ()),
                )
        if woken_parent:
            self._parents_to_wake.put(woken_parent)
        return claimed

    async def _cancel_requested_db(self, run_ids: dict[str, str]) -> set[str]:
        if not run_ids:
            return set()
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                (
                    await session.execute(
                        select(runs.c.run_id).where(
                            runs.c.run_id.in_(list(run_ids)),
                            runs.c.cancel_requested.is_(True),
                        )
                    )
                )
                .scalars()
                .all()
            )
            return set(rows)

    async def _dispatch_status_db(self, run_id: str, tenant_id: str) -> str | None:
        async with self._session(tenant_id) as session:
            return (
                await session.execute(
                    select(runs.c.status).where(runs.c.run_id == run_id)
                )
            ).scalar_one_or_none()

    async def _stale_queued_db(
        self, older_than_seconds: float
    ) -> list[tuple[str, str]]:
        async with self._session(DEFAULT_TENANT) as session:
            rows = (
                await session.execute(
                    select(runs.c.run_id, runs.c.tenant_id).where(
                        runs.c.status == RunStatus.QUEUED.value,
                        # Age since the row last ENTERED the queue, not
                        # since submission: a resumed segment is fresh
                        # again, only rows predating the column fall back.
                        func.coalesce(
                            runs.c.queued_since, runs.c.created_at
                        )
                        < time.time() - older_than_seconds,
                    )
                )
            ).all()
            return [(row[0], row[1]) for row in rows]

    async def _cleanup_pass_db(self) -> None:
        """Run the run store's lazy cleanup once in its own transaction.

        Covers the restart sweep (first pass only) plus this store's
        waiting-TTL sweep, terminal retention, and the stuck-row failsafe.
        """
        async with self._session(DEFAULT_TENANT) as session:
            await self._cleanup_db(session)

    async def _cleanup_db(self, session: "AsyncSession") -> None:
        woken_parents: list[str] = []
        if self._sweep_orphans:
            self._sweep_orphans = False
            woken_parents.extend(await self._recover_orphans_db(session))
        # Waiting TTL: a parked run is auto-cancelled with a visible
        # reason naming WHAT was awaited — a human decision
        # (``approval_timeout``) or child runs that never all ended
        # (``children_timeout``) — never silently forever (memory-store
        # parity). Runs BEFORE the stuck-row failsafe so a timed-out
        # wait goes terminal cleanly instead of being deleted as stuck.
        # Probe candidates without locks, then acquire each complete lineage
        # root-to-leaf before the CAS. This is the same order used by cancel
        # and worker terminalization; cleanup never introduces a reverse edge.
        timed_out: list[tuple[Any, str]] = []
        for statuses, reason in (
            (
                (
                    RunStatus.WAITING_FOR_APPROVAL.value,
                    RunStatus.WAITING_FOR_INPUT.value,
                ),
                "approval_timeout",
            ),
            ((RunStatus.WAITING_FOR_CHILDREN.value,), "children_timeout"),
        ):
            candidate_ids = (
                (
                    await session.execute(
                        select(runs.c.run_id)
                        .where(
                            runs.c.status.in_(statuses),
                            runs.c.waiting_since.isnot(None),
                            runs.c.waiting_since
                            < time.time() - self._waiting_ttl_seconds,
                        )
                        .order_by(
                            func.coalesce(runs.c.root_run_id, runs.c.run_id),
                            runs.c.run_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for candidate_id in candidate_ids:
                try:
                    await self._lock_system_run_path_db(session, candidate_id)
                except RunNotFound:
                    continue
                now = time.time()
                swept = (
                    await session.execute(
                        update(runs)
                        .where(
                            runs.c.run_id == candidate_id,
                            runs.c.status.in_(statuses),
                            runs.c.waiting_since.isnot(None),
                            runs.c.waiting_since
                            < time.time() - self._waiting_ttl_seconds,
                        )
                        .values(
                            status=RunStatus.CANCELLED.value,
                            cancel_requested=True,
                            finished_at=now,
                            **_terminal_timing_values(now),
                        )
                        .returning(
                            runs.c.run_id,
                            runs.c.tenant_id,
                            runs.c.kind,
                            runs.c.parent_run_id,
                            runs.c.request_payload,
                            runs.c.snapshot,
                            runs.c.attempt,
                        )
                    )
                ).first()
                if swept is not None:
                    timed_out.append((swept, reason))
        for waited, reason in timed_out:
            waited_id = waited[0]
            log.warning(
                "Run %s waited longer than %d seconds and was automatically cancelled "
                "(%s).",
                waited_id,
                int(self._waiting_ttl_seconds),
                reason,
            )
            payload = {
                "status": "cancelled",
                "reason": reason,
                "snapshot": dict(waited[5] or {}),
            }
            woken_parent = await self._record_terminal_run_db(
                session,
                run_id=waited_id,
                tenant_id=waited[1],
                kind=str(waited[2] or "standard"),
                parent_run_id=waited[3],
                request_payload=dict(waited[4] or {}),
                status=RunStatus.CANCELLED.value,
                event_type="inqtrix.run.cancelled",
                payload=payload,
                snapshot=dict(waited[5] or {}),
                attempt=int(waited[6] or 0) or None,
            )
            if woken_parent:
                woken_parents.append(woken_parent)
        self._register_cleanup_handoffs(
            session,
            swept_run_ids=[row[0] for row, _reason in timed_out],
            woken_parent_ids=woken_parents,
        )
        # Queued-TTL failsafe: a run nobody consumed within the generous
        # window fails with a typed error instead of waiting for the
        # hard age cap. This sweep runs inline on API paths, so it fires
        # precisely in the deployment shape that has ZERO workers; the
        # window is sized far above any legitimate backlog wait.
        queued_expired = list(
            (
                await session.execute(
                    select(runs.c.run_id).where(
                        runs.c.status == RunStatus.QUEUED.value,
                        # Fresh submits leave queued_since NULL (only a
                        # resume or child-wake sets it), so the age falls
                        # back to created_at — otherwise the sweep would
                        # be blind to exactly its target case. Historical
                        # NULL rows older than the window are swept too,
                        # deliberately: they ARE the stuck shape.
                        func.coalesce(
                            runs.c.queued_since, runs.c.created_at
                        )
                        < time.time() - self._queued_ttl_seconds,
                    )
                )
            )
            .scalars()
            .all()
        )
        if queued_expired:
            log.warning(
                "%d runs waited longer than %d seconds for an execution worker; "
                "attempting to mark them as failed: %s",
                len(queued_expired),
                int(self._queued_ttl_seconds),
                ", ".join(queued_expired[:5]),
            )
            queued_woken = await self._recover_orphans_db(
                session,
                candidate_ids=queued_expired,
                error={
                    "message": (
                        f'The run waited longer than {int(self._queued_ttl_seconds)} seconds for an execution worker and was terminated.'
                    ),
                    "type": "queued_timeout",
                },
                # Re-assert the TTL predicate under the row lock: a run a
                # worker claimed (or re-queued) since the select above
                # must survive this pass.
                candidate_guard=and_(
                    runs.c.status == RunStatus.QUEUED.value,
                    func.coalesce(runs.c.queued_since, runs.c.created_at)
                    < time.time() - self._queued_ttl_seconds,
                ),
            )
            self._register_cleanup_handoffs(
                session,
                swept_run_ids=[],
                woken_parent_ids=queued_woken,
            )
        await self._delete_retained_runs_db(
            session,
            criteria=(
                runs.c.status.in_(_TERMINAL_VALUES),
                runs.c.finished_at.isnot(None),
                runs.c.finished_at < time.time() - self._completed_ttl_seconds,
            ),
            action="run.retention_deleted",
        )
        # An incognito knowledge ask leaves after its own short window; the
        # ordinary retention above would keep it for months.
        await self._delete_retained_runs_db(
            session,
            criteria=(
                runs.c.incognito.is_(True),
                runs.c.status.in_(_TERMINAL_VALUES),
                runs.c.finished_at.isnot(None),
                runs.c.finished_at < time.time() - self._incognito_ttl_seconds,
            ),
            action="run.retention_deleted",
        )
        # Stuck-row failsafe: rows still non-terminal after the hard age
        # cap are force-FAILED, in every queue mode — after this long no
        # worker legitimately owns them, and attempt fencing absorbs any
        # zombie write. The terminal write emits the terminal event an
        # attached stream has been waiting for and starts the ordinary
        # completed-TTL retention clock that deletes the payload later.
        # Waiting rows are NOT stuck: the waiting-TTL sweep above is
        # their visible lifecycle end.
        stuck_ids = list(
            (
                await session.execute(
                    select(runs.c.run_id).where(
                        runs.c.status.notin_(_TERMINAL_VALUES),
                        runs.c.status.notin_(_WAITING_VALUES),
                        runs.c.created_at
                        < time.time() - _STUCK_ROW_MAX_AGE_SECONDS,
                    )
                )
            )
            .scalars()
            .all()
        )
        if stuck_ids:
            # Announcement of the pass, not its outcome: the per-row
            # warnings below report what actually landed (the status CAS
            # may skip rows that turned terminal in the meantime).
            log.warning(
                "%d run rows older than %d days have no terminal state; attempting to "
                "mark them as failed: %s",
                len(stuck_ids),
                int(_STUCK_ROW_MAX_AGE_SECONDS // 86_400),
                ", ".join(stuck_ids[:5]),
            )
            stuck_woken = await self._recover_orphans_db(
                session,
                candidate_ids=stuck_ids,
                error=_EXECUTION_LOST_ERROR,
            )
            self._register_cleanup_handoffs(
                session,
                swept_run_ids=[],
                woken_parent_ids=stuck_woken,
            )

    async def _delete_retained_runs_db(
        self,
        session: "AsyncSession",
        *,
        criteria: tuple[Any, ...],
        action: str,
    ) -> list[str]:
        """Delete retention candidates with shares and effects atomically."""
        if action == "run.retention_deleted":
            # A saved private conversation owns its originals until explicit
            # aggregate deletion. Registry rows in deletion/retry states remain
            # owned by that saga, never by the generic run TTL sweeper.
            retained_session = and_(
                runs.c.kind.in_(("agent", "agent_child")),
                exists(select(agent_sessions.c.id).where(
                    agent_sessions.c.id == runs.c.session_id,
                    agent_sessions.c.tenant_id == runs.c.tenant_id,
                    agent_sessions.c.created_by_user_id.is_not_distinct_from(runs.c.created_by_user_id),
                    agent_sessions.c.workspace_id.is_not_distinct_from(runs.c.workspace_id),
                )),
            )
            criteria = (*criteria, ~retained_session)
        candidate_ids = (
            (
                await session.execute(
                    select(runs.c.run_id)
                    .where(*criteria)
                    .order_by(
                        func.coalesce(runs.c.root_run_id, runs.c.run_id),
                        runs.c.root_run_id.isnot(None),
                        runs.c.run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        deleted: list[str] = []
        for candidate_id in candidate_ids:
            try:
                await self._lock_system_run_path_db(session, candidate_id)
            except RunNotFound:
                continue
            row = (
                await session.execute(
                    select(
                        runs.c.run_id,
                        runs.c.tenant_id,
                        runs.c.created_by_user_id,
                    ).where(runs.c.run_id == candidate_id, *criteria)
                )
            ).first()
            if row is None:
                continue
            subtree = await self._lock_run_subtree_db(session, row.run_id)
            if action == "run.retention_deleted" and any(
                child["status"] not in _TERMINAL_VALUES for child in subtree
            ):
                continue
            if action == "run.retention_deleted" and self._checkpoint_cleanup is not None:
                from inqtrix.exceptions import RunCleanupFailed

                try:
                    for child in subtree:
                        await asyncio.to_thread(self._checkpoint_cleanup, child["run_id"], child["mode"])
                except RunCleanupFailed:
                    # The cleanup callback reports its failure. Keep the
                    # terminal anchors so a later sweep can retry safely.
                    continue
            for child in subtree:
                recipients = await revoke_resource_shares(
                    session,
                    tenant_id=child["tenant_id"],
                    resource_type="run",
                    resource_id=child["run_id"],
                    revoked_by_user_id=None,
                )
                await append_resource_effects(
                    session,
                    tenant_id=child["tenant_id"],
                    actor_user_id=None,
                    owner_user_id=child["created_by_user_id"],
                    action=action,
                    resource_type="run",
                    resource_id=child["run_id"],
                    scope="runs",
                    additional_targets=recipients,
                )
            await session.execute(delete(runs).where(runs.c.run_id == row.run_id))
            deleted.extend(child["run_id"] for child in subtree)
        return deleted

    async def _recover_orphans_db(
        self,
        session: "AsyncSession",
        *,
        candidate_ids: list[str] | None = None,
        error: dict[str, str] | None = None,
        candidate_guard: Any | None = None,
    ) -> list[str]:
        """Fail active rows that no process will ever execute again.

        Two callers share this per-candidate lock → CAS → terminal-event
        path: the once-per-process restart sweep (``candidate_ids=None``
        selects every queued/running row — in-process closures did not
        survive the restart) and the pre-filtered id lists of the
        lost-execution fence and the stuck-row failsafe. The status CAS
        is the true guard: a candidate that finished or got claimed in
        the meantime is skipped silently.

        The blanket restart selection assumes a SINGLE API process in
        no-queue durable mode (the documented deployment shape): a
        second process sharing the database would have its in-flight
        runs swept here. Multi replica deployments use the queue
        backend.
        """
        if error is None:
            error = {
                "message": "A server restart interrupted the run.",
                "type": "server_restarted",
            }
        if candidate_ids is None:
            status_filter = runs.c.status.in_(
                (
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                )
            )
        else:
            # Explicit candidates were pre-filtered by their caller; the
            # broader non-terminal/non-waiting predicate additionally
            # covers stray states nothing here writes (retention parity
            # with the historical stuck delete). ``candidate_guard`` lets
            # a caller whose selection can be OUTRACED (the queued-TTL: a
            # worker may claim between select and this pass) re-assert
            # its own predicate so a freshly claimed row survives.
            status_filter = and_(
                runs.c.status.notin_(_TERMINAL_VALUES),
                runs.c.status.notin_(_WAITING_VALUES),
                runs.c.run_id.in_(candidate_ids),
                *([candidate_guard] if candidate_guard is not None else []),
            )
        # One canonical, lineage-grouped ordering for BOTH branches: the
        # per-candidate path locks root before descendants, and every
        # concurrent maintenance pass must acquire cross-lineage locks in
        # this same order or two passes deadlock.
        candidate_ids = (
            (
                await session.execute(
                    select(runs.c.run_id)
                    .where(status_filter)
                    .order_by(
                        func.coalesce(runs.c.root_run_id, runs.c.run_id),
                        runs.c.root_run_id.isnot(None),
                        runs.c.run_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = []
        for candidate_id in candidate_ids:
            try:
                await self._lock_system_run_path_db(session, candidate_id)
            except RunNotFound:
                continue
            now = time.time()
            row = (
                await session.execute(
                    update(runs)
                    .where(
                        runs.c.run_id == candidate_id,
                        runs.c.status.notin_(_TERMINAL_VALUES),
                        runs.c.status.notin_(_WAITING_VALUES),
                        *(
                            [candidate_guard]
                            if candidate_guard is not None
                            else []
                        ),
                    )
                    .values(
                        status=RunStatus.FAILED.value,
                        finished_at=now,
                        error=error,
                        **_terminal_timing_values(now),
                    )
                    .returning(
                        runs.c.run_id,
                        runs.c.tenant_id,
                        runs.c.kind,
                        runs.c.parent_run_id,
                        runs.c.request_payload,
                        runs.c.snapshot,
                        runs.c.attempt,
                    )
                )
            ).first()
            if row is not None:
                rows.append(row)
        woken_parents: list[str] = []
        for row in rows:
            run_id = row[0]
            log.warning(
                "Orphaned run %s marked as failed (%s).",
                run_id,
                error["type"],
            )
            payload = {
                "status": "failed",
                "error": error,
                "snapshot": dict(row[5] or {}),
            }
            woken_parent = await self._record_terminal_run_db(
                session,
                run_id=run_id,
                tenant_id=row[1],
                kind=str(row[2] or "standard"),
                parent_run_id=row[3],
                request_payload=dict(row[4] or {}),
                status=RunStatus.FAILED.value,
                event_type="inqtrix.run.failed",
                payload=payload,
                snapshot=dict(row[5] or {}),
                attempt=int(row[6] or 0) or None,
            )
            if woken_parent:
                woken_parents.append(woken_parent)
        return woken_parents


def _row_view(row: Any) -> _RowView:
    """The record view of a full ``runs`` row."""
    payload = row["request_payload"] or {}
    return _record_view(
        row,
        # Persisted inside the replay payload (no column): the child's
        # idempotency key for kernel tool re-execution.
        origin_key=(payload.get("body") or {}).get("origin_key"),
        work_profile=(payload.get("body") or {}).get("work_profile"),
        # TOP LEVEL, not under "body": the submitted question is its own
        # payload key (``origin_key`` is the one that lives in ``body``).
        question_full=payload.get("question"),
    )


def _summary_view(row: Any) -> _RowView:
    """The record view of a :func:`_summary_select` row."""
    body = row["_payload_body"] or {}
    return _record_view(
        row,
        origin_key=body.get("origin_key"),
        work_profile=body.get("work_profile"),
        question_full=row["_question_full"],
    )


def _summary_select():
    """The columns a run summary reads, for every list of summaries.

    A list renders summaries only, so it never loads a run's result (a deep
    report stores about a quarter of a megabyte) or its whole request
    payload: the small ``body`` object and the question are read by path.
    Every path read decompresses a stored payload again, so the summary
    takes two, not one per key.
    """
    payload = runs.c.request_payload
    return select(
        runs.c.run_id,
        runs.c.status,
        runs.c.question,
        runs.c.stack_name,
        runs.c.workspace_id,
        runs.c.mode,
        runs.c.agent_overrides,
        runs.c.created_at,
        runs.c.started_at,
        runs.c.finished_at,
        runs.c.segment_count,
        runs.c.event_seq,
        runs.c.current_segment_id,
        runs.c.queued_since,
        runs.c.active_started_at,
        runs.c.active_seconds,
        runs.c.waiting_seconds,
        runs.c.queued_seconds,
        runs.c.waiting_since,
        runs.c.snapshot,
        runs.c.error,
        runs.c.kind,
        runs.c.parent_run_id,
        runs.c.root_run_id,
        runs.c.session_id,
        runs.c.cancel_requested,
        runs.c.created_by_user_id,
        runs.c.created_by_tenant_id,
        payload["body"].label("_payload_body"),
        payload["question"].as_string().label("_question_full"),
    )


def _record_view(
    row: Any,
    *,
    origin_key: Any,
    work_profile: Any,
    question_full: Any,
) -> _RowView:
    return _RowView(
        run_id=row["run_id"],
        status=row["status"],
        question=row["question"],
        stack_name=row["stack_name"],
        workspace_id=row["workspace_id"],
        mode=row["mode"],
        agent_overrides=dict(row["agent_overrides"] or {}),
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        segment_count=int(row["segment_count"] or 0),
        event_seq=int(row["event_seq"] or 0),
        current_segment_id=row["current_segment_id"],
        queued_since=row["queued_since"],
        active_started_at=row["active_started_at"],
        active_seconds=float(row["active_seconds"] or 0.0),
        waiting_seconds=float(row["waiting_seconds"] or 0.0),
        queued_seconds=float(row["queued_seconds"] or 0.0),
        waiting_since=row["waiting_since"],
        snapshot=dict(row["snapshot"] or {}),
        error=dict(row["error"]) if row["error"] else None,
        kind=row["kind"] or "standard",
        parent_run_id=row["parent_run_id"],
        root_run_id=row["root_run_id"],
        session_id=row["session_id"],
        origin_key=origin_key,
        work_profile=work_profile,
        question_full=question_full,
        cancel_requested=bool(row["cancel_requested"]),
    )


def _workspace_matches_row(row: Any, workspace_id: str | None) -> bool:
    return workspace_id is None or row["workspace_id"] == workspace_id


def _visible_row(row: Any, visible_to: "UserContext | None") -> bool:
    """SQL-row twin of the in-memory visibility predicate."""
    if visible_to is None:
        return row["created_by_user_id"] is None
    return (
        row["created_by_user_id"] is not None
        and row["created_by_user_id"] == visible_to.principal.user_id
        and row["created_by_tenant_id"] == visible_to.principal.tenant_id
    )
