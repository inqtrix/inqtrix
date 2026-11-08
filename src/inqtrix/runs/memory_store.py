"""In-memory run registry and queue for native Inqtrix UI clients.

The OpenAI-compatible chat endpoint remains request/response oriented.
This module backs the native ``/v1/runs`` surface: it accepts research
jobs, caps active provider work, keeps a bounded FIFO queue, and exposes
short-lived event buffers for browser UIs.
"""

from __future__ import annotations

from copy import deepcopy
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from queue import Queue
from functools import partial
from typing import Any, Callable

from inqtrix import aio
from inqtrix.auth.log_redaction import log_authorization_denial
from inqtrix.auth.memory_authority import (
    MemoryAuthorityCoordinator,
    MemoryResourceSnapshot,
)
from inqtrix.auth.permissions import SharePermission
from inqtrix.auth.principal import Principal, UserContext
from inqtrix.auth.shares import ShareFacts
from inqtrix.constants import RUN_INCOGNITO_RETENTION_SECONDS
from inqtrix.exceptions import RunAccessDenied, RunCleanupFailed, RunNotFound
from inqtrix.execution_authority import AuthorizationRevoked
from inqtrix.observability.context import bind_log_context
from inqtrix.observability.incognito import incognito_request
from inqtrix.observability.otel import run_execute_span
from inqtrix.observability.propagation import inject_traceparent
from inqtrix.execution_failures import terminate_native_run
from inqtrix.pagination import keyset_page
from inqtrix.runs.ports import RunStoreMetrics
from inqtrix.runs.records import (
    IMPORTED_RUN_CONTRACT,
    RunActive,
    RunHandle,
    RunParentInactive,
    RunPerUserLimit,
    RunQueueFull,
    RunRecord,
    RunSessionActive,
    RunStatus,
    RunSubscription,
    RunWork,
    TERMINAL_RUN_STATUSES,
    WAITING_RUN_STATUSES,
)
from inqtrix.runs.shared import (
    ADMIN_CANCEL_REASON,
    BINDING_ASKED_STATUS,
    binding_overview_payload,
    inline_on_loop,
    on_transition_lane,
    clipped_question,
    access_annotation,
    build_child_progress_payload,
    build_run_summary,
    expand_run_event,
    run_elapsed_seconds,
    run_segment_id,
    should_project_child_event,
    status_value,
)
from inqtrix.runtime_logging import new_run_id
from inqtrix.settings import ServerSettings
from inqtrix.urls import sanitize_error
from inqtrix.harness.contracts.events import (
    CHILD_PROGRESS_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_CANCEL_REQUESTED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_QUEUED_EVENT,
    RUN_RESUMED_EVENT,
    RUN_STARTED_EVENT,
    RUN_TRACE_EVENT,
    RUN_WAITING_EVENT,
)

log = logging.getLogger(__name__)


class RunStore:
    """Thread-safe in-memory queue and registry for native run endpoints.

    Args:
        max_concurrent: Maximum number of actively executing research jobs.
            Additional accepted jobs stay in the FIFO queue.
        max_queue_size: Maximum number of waiting jobs. Active jobs do not
            count against this number.
        completed_ttl_seconds: How long terminal records remain queryable
            after completion. Queued/running runs are never TTL-evicted.
        event_buffer_size: Number of recent events retained per run for
            late SSE subscribers.
        waiting_ttl_seconds: How long a run may sit in a waiting status
            before it is auto-cancelled with the visible
            ``approval_timeout`` reason. Default seven days — a parked
            approval must never linger silently forever.
        incognito_ttl_seconds: How long a terminal incognito knowledge
            ask remains queryable; shorter windows win over
            ``completed_ttl_seconds``.
    """

    def __init__(
        self,
        *,
        max_concurrent: int,
        max_queue_size: int,
        completed_ttl_seconds: int,
        event_buffer_size: int,
        waiting_ttl_seconds: float = 7 * 24 * 3600.0,
        max_concurrent_per_user: int | None = None,
        audit_service_starts: bool = True,
        incognito_ttl_seconds: int = RUN_INCOGNITO_RETENTION_SECONDS,
    ) -> None:
        self._audit_service_starts = bool(audit_service_starts)
        self._max_concurrent = _require_minimum(
            "max_concurrent", max_concurrent, minimum=1
        )
        self._max_queue_size = _require_minimum(
            "max_queue_size", max_queue_size, minimum=0
        )
        self._completed_ttl_seconds = _require_minimum(
            "completed_ttl_seconds", completed_ttl_seconds, minimum=0
        )
        self._incognito_ttl_seconds = _require_minimum(
            "incognito_ttl_seconds", incognito_ttl_seconds, minimum=0
        )
        self._event_buffer_size = _require_minimum(
            "event_buffer_size", event_buffer_size, minimum=1
        )
        if float(waiting_ttl_seconds) <= 0:
            raise ValueError(
                f"waiting_ttl_seconds must be > 0, got {waiting_ttl_seconds}"
            )
        self._waiting_ttl_seconds = float(waiting_ttl_seconds)
        if max_concurrent_per_user is not None:
            _require_minimum(
                "max_concurrent_per_user", max_concurrent_per_user, minimum=1
            )
        self._max_concurrent_per_user = max_concurrent_per_user
        self._records: dict[str, RunRecord] = {}
        self._pending: deque[str] = deque()
        self._running_count = 0
        self._lock = threading.RLock()
        self._share_lookup: Callable[..., SharePermission | None] | None = None
        self._share_workspace_check: Callable[..., bool] | None = None
        self._resource_access_guard: Callable[..., Any] | None = None
        self._restrict_share_workspaces = False
        self._authority: MemoryAuthorityCoordinator | None = None
        self._session_retention: Callable[..., bool] | None = None
        self._checkpoint_cleanup: Callable[[str, str], None] | None = None

    def bind_checkpoint_cleanup(self, cleanup: Callable[[str, str], None]) -> None:
        """Bind execution-cache cleanup before deleting its authorized run anchor."""
        self._checkpoint_cleanup = cleanup

    def bind_session_retention(self, predicate: Callable[..., bool]) -> None:
        """Use the saved-session registry as the conversation retention authority."""
        self._session_retention = predicate

    async def aclose(self) -> None:
        """Ask every running body to stop: a lane's thread keeps the process alive until it ends."""
        with self._lock:
            for record in self._records.values():
                if record.status == RunStatus.RUNNING:
                    record.cancel_event.set()

    def sweep(self) -> None:
        """Run the cleanup pass every access runs, without waiting for one."""
        with self._lock:
            self._cleanup_locked()

    @property
    def atomic_resource_effects(self) -> bool:
        """Whether deletion handles share revocation and effects atomically."""
        return self._authority is not None

    def bind_authority_coordinator(
        self, coordinator: MemoryAuthorityCoordinator
    ) -> None:
        """Join run records and direct-share state under one authority lock."""
        self._authority = coordinator
        self._lock = coordinator.lock
        self._resource_access_guard = coordinator.resource_access_guard
        coordinator.register_resource("run", self._resource_snapshot)

    def _resource_snapshot(
        self, tenant_id: str, resource_id: str
    ) -> MemoryResourceSnapshot:
        record = self._records.get(resource_id)
        return MemoryResourceSnapshot(
            exists=record is not None
            and (record.created_by_tenant_id or "default") == tenant_id,
            owner_user_id=(record.created_by_user_id if record is not None else None),
        )

    def _append_run_effect_locked(
        self,
        record: RunRecord,
        *,
        action: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> None:
        if self._authority is None:
            return
        actor = (
            actor_user_id
            if actor_user_id is not None
            else (
                record.execution_actor_user_id
                if record.execution_actor_user_id is not None
                else record.created_by_user_id
            )
        )
        self._authority.append_registered_resource_effects(
            tenant_id=record.created_by_tenant_id or "default",
            actor_user_id=actor,
            action=action,
            resource_type="run",
            resource_id=record.run_id,
            scope="runs",
        )

    def bind_authorization(
        self,
        *,
        share_lookup: Callable[..., SharePermission | None],
        share_workspace_check: Callable[..., bool],
        resource_access_guard: Callable[..., Any],
        restrict_to_workspace_members: bool,
    ) -> None:
        """Bind live direct-share reads after the composition root is ready."""
        self._share_lookup = share_lookup
        self._share_workspace_check = share_workspace_check
        self._resource_access_guard = resource_access_guard
        self._restrict_share_workspaces = restrict_to_workspace_members

    @classmethod
    def from_settings(
        cls,
        settings: ServerSettings,
        *,
        audit_service_starts: bool = True,
    ) -> "RunStore":
        """Build a run store from HTTP server settings."""
        return cls(
            max_concurrent=settings.run_max_concurrent or settings.max_concurrent,
            max_queue_size=settings.run_queue_max_size,
            completed_ttl_seconds=settings.run_completed_ttl_seconds,
            event_buffer_size=settings.run_event_buffer_size,
            max_concurrent_per_user=settings.run_max_concurrent_per_user,
            audit_service_starts=audit_service_starts,
            incognito_ttl_seconds=settings.run_incognito_retention_seconds,
        )

    def submit(
        self,
        *,
        question: str,
        stack_name: str,
        work: RunWork,
        mode: str,
        workflow_contract: int,
        agent_overrides: dict[str, Any] | None = None,
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
        """Create a queued run and dispatch it if capacity is available.

        Args:
            mode: The run's mode, half of its binding.
            workflow_contract: The contract the mode's algorithm carries on
                acceptance, the other half.
            request_payload: Re-execution payload persisted by durable
                backends so worker processes can rebuild the run from the row
                alone. The memory backend retains the same detached payload
                for control-plane dependency validation while execution keeps
                the work closure in-process.
            kind: Run role in an agent tree (``standard`` default keeps
                every historical caller byte-identical).
            parent_run_id: Spawning agent run for ``agent_child`` rows.
            root_run_id: Tree root for child rows.
            session_id: Saved Agent or Knowledge session grouping.

        Returns:
            Public run summary suitable for HTTP responses.

        Raises:
            RunQueueFull: When the waiting queue is already full.
        """
        stored_request_payload = deepcopy(request_payload or {})
        # Carry the submitter's trace context in the run row (W3C
        # traceparent) so a later worker segment parents its execution
        # span here — one trace across the process boundary.
        inject_traceparent(stored_request_payload)
        request_body = (
            stored_request_payload.get("body")
            if isinstance(stored_request_payload, dict)
            else {}
        )
        execution_actor_user_id = created_by_user_id
        parent_task_id = (
            str(request_body.get("parent_task_id") or "")
            if isinstance(request_body, dict)
            else ""
        )
        parent_task_attempt = (
            int(request_body.get("parent_task_attempt", 0) or 0)
            if isinstance(request_body, dict)
            else 0
        )
        with self._lock, ExitStack() as authority_stack:
            self._cleanup_locked()
            if kind == "agent_child":
                if not parent_run_id:
                    raise RunParentInactive("agent child has no parent run")
                parent = self._records.get(parent_run_id)
                if parent is None:
                    raise RunParentInactive("agent child parent is missing")
                canonical_root_id = parent.root_run_id or parent.run_id
                root = self._records.get(canonical_root_id)
                if root is None:
                    raise RunParentInactive("agent child root is missing")
                if execution_actor_user_id != root.execution_actor_user_id:
                    raise AuthorizationRevoked(
                        "agent child admission lost root execution authority"
                    )
                authority_stack.enter_context(
                    self._execution_authority_context_locked(root)
                )
                if origin_key:
                    existing = next(
                        (
                            record
                            for record in self._records.values()
                            if record.kind == "agent_child"
                            and record.parent_run_id == parent_run_id
                            and record.origin_key == origin_key
                        ),
                        None,
                    )
                    if existing is not None:
                        return self._summary_locked(existing)
                blocked = (
                    parent.status in TERMINAL_RUN_STATUSES
                    or parent.cancel_event.is_set()
                    or root.status in TERMINAL_RUN_STATUSES
                    or root.cancel_event.is_set()
                )
                if blocked:
                    raise RunParentInactive(
                        "agent child parent or root is no longer active"
                    )
                # The parent row, not a caller-provided value, owns lineage.
                root_run_id = canonical_root_id
                created_by_user_id = root.created_by_user_id
                created_by_tenant_id = root.created_by_tenant_id
                execution_actor_user_id = root.execution_actor_user_id
                execution_scopes = root.execution_scopes
                workspace_id = root.workspace_id
            elif self._authority is not None:
                authority_stack.enter_context(
                    self._authority.creation_guard(
                        tenant_id=created_by_tenant_id or "default",
                        actor_user_id=created_by_user_id,
                    )
                )
            if kind == "agent" and parent_run_id is None and session_id:
                active_statuses = {
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    *WAITING_RUN_STATUSES,
                }
                if any(
                    record.kind == "agent"
                    and record.parent_run_id is None
                    and record.session_id == session_id
                    and record.status in active_statuses
                    for record in self._records.values()
                ):
                    raise RunSessionActive(session_id)
            if (
                len(self._pending) >= self._max_queue_size
                and self._running_count >= self._max_concurrent
            ):
                raise RunQueueFull("native run queue is full")
            if (
                self._max_concurrent_per_user is not None
                and execution_actor_user_id is not None
            ):
                # Fairness bound UNDER the global cap: a recount over the
                # (TTL-bounded) records, leak-free by construction and
                # EXACT here (the whole submit holds self._lock, so two
                # submits by one canonical actor serialize — unlike the
                # Postgres path,
                # which is an approximate bound under READ COMMITTED).
                #
                # What is COUNTED: QUEUED+RUNNING standard runs and agent
                # CHILDREN — the runs that actually occupy an execution
                # slot. EXCLUDED: WAITING runs (parked, slot-free) AND
                # agent PARENTS (kind='agent'), which park almost
                # immediately and would otherwise contend against their
                # OWN children for the user's budget (self-starvation).
                # So one agent tree costs the user its children, not the
                # orchestrator on top.
                #
                # Scope is the effective actor UUID only: run storage is
                # single-tenant today; the row tenant remains the RLS boundary.
                in_flight = sum(
                    1
                    for record in self._records.values()
                    if record.execution_actor_user_id == execution_actor_user_id
                    and record.kind != "agent"
                    and record.status in (RunStatus.QUEUED, RunStatus.RUNNING)
                )
                if in_flight >= self._max_concurrent_per_user:
                    raise RunPerUserLimit("per-user in-flight run cap reached")

            run_id = self._new_unique_run_id_locked()
            record = RunRecord(
                run_id=run_id,
                question=clipped_question(question),
                stack_name=stack_name,
                workspace_id=workspace_id,
                created_at=time.time(),
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
                execution_actor_user_id=execution_actor_user_id,
                execution_scopes=frozenset(execution_scopes),
                request_payload=stored_request_payload,
                work=work,
                agent_overrides=dict(agent_overrides or {}),
                mode=mode,
                workflow_contract=workflow_contract,
                kind=kind,
                parent_run_id=parent_run_id,
                root_run_id=root_run_id,
                session_id=session_id,
                incognito=incognito_request(stored_request_payload),
                origin_key=origin_key,
                parent_task_id=parent_task_id or None,
                parent_task_attempt=(parent_task_attempt or None),
            )
            record.events = deque(maxlen=self._event_buffer_size)
            self._records[run_id] = record
            self._pending.append(run_id)
            self._emit_locked(
                record,
                RUN_QUEUED_EVENT,
                {
                    "status": "queued",
                    "queue_position": self._queue_position_locked(run_id),
                },
            )
            self._append_run_effect_locked(record, action="run.created")
            self._dispatch_locked()
            return self._summary_locked(record)

    def import_completed_run(
        self,
        *,
        source_run_id: str,
        question: str,
        stack_name: str,
        result: dict[str, Any],
        mode: str,
        status: str = "completed",
        agent_overrides: dict[str, Any] | None = None,
        snapshot: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        created_at: float | None = None,
        workspace_id: str | None = None,
        created_by_user_id: uuid.UUID | None = None,
        created_by_tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an ALREADY-TERMINAL run carried in from a project file.

        Unlike :meth:`submit` (which queues a fresh run for execution), this
        stores a completed report snapshot directly so a loaded project's
        reports survive a reload + follow the user, scoped to the caller.
        ``source_run_id`` is only an owner-scoped idempotency key. Every new
        imported row receives a fresh server-generated ``run_id`` so retention
        can never free a client id that later resurrects a stale run share.

        ``created_at`` keeps the report's ORIGINAL date for display + ordering,
        but ``finished_at`` is set to the import time so the durable-retention
        clock starts now: a report older than the retention window must NOT be
        pruned on the next cleanup just because its original run finished long
        ago (that would re-lose it). ``finished_at`` is therefore not a
        parameter.

        Raises:
            ValueError: When *status* is not a terminal status.
        """
        terminal = _coerce_status(status)
        if terminal not in TERMINAL_RUN_STATUSES:
            raise ValueError(
                f"import_completed_run requires a terminal status, got {status!r}"
            )
        source_run_id = source_run_id.strip()
        if not source_run_id:
            raise ValueError("source_run_id must not be empty")
        if len(source_run_id) > 255:
            raise ValueError("source_run_id must not exceed 255 characters")
        now = time.time()
        creation_authority = (
            self._authority.creation_guard(
                tenant_id=created_by_tenant_id or "default",
                actor_user_id=created_by_user_id,
            )
            if self._authority is not None
            else nullcontext()
        )
        with self._lock, creation_authority:
            self._cleanup_locked()
            existing = next(
                (
                    record
                    for record in self._records.values()
                    if record.source_run_id == source_run_id
                    and record.created_by_user_id == created_by_user_id
                ),
                None,
            )
            if existing is not None:
                return self._summary_locked(existing)
            run_id = self._new_unique_run_id_locked()
            record = RunRecord(
                run_id=run_id,
                question=clipped_question(question),
                stack_name=stack_name,
                workspace_id=workspace_id,
                created_at=created_at if created_at is not None else now,
                created_by_user_id=created_by_user_id,
                created_by_tenant_id=created_by_tenant_id,
                source_run_id=source_run_id,
                work=None,
                agent_overrides=dict(agent_overrides or {}),
                mode=mode,
                workflow_contract=IMPORTED_RUN_CONTRACT,
            )
            record.events = deque(maxlen=self._event_buffer_size)
            record.status = terminal
            # Retention clock = import time (not the original finish), so an old
            # report is kept the full window from when it was imported.
            record.started_at = now
            record.finished_at = now
            record.finished_monotonic = time.monotonic()
            record.snapshot = dict(snapshot or {})
            record.result = dict(result) if terminal == RunStatus.COMPLETED else None
            record.error = dict(error) if error else None
            self._records[run_id] = record
            self._append_run_effect_locked(record, action="run.imported")
            return self._summary_locked(record)

    def owner_user_id(self, run_id: str) -> uuid.UUID | None:
        """The run's creator regardless of visibility (share layer)."""
        with self._lock:
            record = self._records.get(run_id)
            return record.created_by_user_id if record is not None else None

    def share_facts(self, run_ids: Sequence[str]) -> dict[str, ShareFacts]:
        """Creator and question of each listed run regardless of visibility
        (share layer), in one read; unknown runs are absent."""
        with self._lock:
            return {
                run_id: ShareFacts(
                    owner_user_id=record.created_by_user_id,
                    title=record.question,
                )
                for run_id in run_ids
                if (record := self._records.get(run_id)) is not None
            }

    def events_snapshot(
        self, run_id: str, *, after: int = 0
    ) -> list[dict[str, Any]]:
        """Replay-buffer events for the admin run drawer (visibility-free).

        Memory tier honesty: the ring holds the LAST N events only —
        older ones were evicted (the durable tier keeps everything).
        """
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return []
            return [
                # data gets its own copy: the nested payload is shared
                # with the live replay ring, and a snapshot must never
                # hand out a mutable view of it.
                {**event, "data": dict(event.get("data") or {})}
                for event in record.events
                if int(event.get("sequence") or 0) > int(after)
            ]

    def trace_id(self, run_id: str) -> str | None:
        """Latest execution segment's trace id (admin surface).

        Not visibility-gated — authorization happens at the instance-
        admin boundary before this lookup (``owner_user_id`` precedent).
        Reads the dedicated record field, NOT the events deque: that
        replay ring is bounded and long runs evict the trace event.
        """
        with self._lock:
            record = self._records.get(run_id)
            return record.trace_id if record is not None else None

    def execution_request_body(self, run_id: str) -> dict[str, Any]:
        """Return a detached copy of the run's persisted request body."""
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFound(run_id)
            body = record.request_payload.get("body")
            if body is None:
                return {}
            if not isinstance(body, dict):
                raise RuntimeError("Persisted run request body is invalid.")
            return deepcopy(body)

    def source_request_body(self, run_id: str, *, visible_to, workspace_id=None) -> dict[str, Any]:
        """Read source provenance under the same owner/workspace check as its body."""
        with self._lock:
            record, shared = self._record_locked(run_id, workspace_id=workspace_id, visible_to=visible_to)
            if shared is not None:
                raise RunNotFound(run_id)
            body = record.request_payload.get("body") or {}
            if not isinstance(body, dict):
                raise RuntimeError("Persisted run request body is invalid.")
            return deepcopy(body)

    def source_run_catalogue(self, session_id: str, *, visible_to, workspace_id=None) -> list[dict[str, Any]]:
        """Only owned metadata, ordered independently of artifact storage."""
        with self._lock:
            self._cleanup_locked()
            return [
                {"run_id": row.run_id, "question": clipped_question(row.question), "kind": row.kind,
                 "mode": row.mode, "status": row.status.value, "created_at": row.created_at}
                for row in sorted(self._records.values(), key=lambda item: (item.created_at, item.run_id))
                if row.session_id == session_id and _visible_to_matches(row, visible_to)
                and _workspace_matches(row, workspace_id)
                and not (row.request_payload.get("body") or {}).get("work_profile")
            ]

    def source_origins_visible(self, run_ids: list[str], *, visible_to, owner_only: bool = False) -> bool:
        """Check a dependency set under one store lock, without copying bodies."""
        with self._lock:
            for run_id in set(run_ids):
                try:
                    _, shared = self._record_locked(run_id, visible_to=visible_to)
                except RunNotFound:
                    return False
                if owner_only and shared is not None:
                    return False
            return True

    def total_elapsed_seconds(self, run_id: str) -> float:
        """Return worker-visible wall time without public authorization.

        The row has already crossed run admission and the handle owns its
        execution. This read exposes only timing, not user data, and must work
        for owner-scoped as well as ownerless runs.
        """

        with self._lock:
            record = self._raw_record_locked(run_id)
            return float(run_elapsed_seconds(record) or 0.0)

    def title(self, run_id: str) -> str | None:
        """The run's question as a share-surface title, regardless of
        visibility — a pending-share recipient must see it to decide. ``None``
        when the run no longer exists (e.g. pruned), so the inbox skips it."""
        with self._lock:
            record = self._records.get(run_id)
            return record.question if record is not None else None

    def get(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return a public summary for *run_id*."""
        with self._lock:
            self._cleanup_locked()
            record, shared = self._record_locked(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            return self._summary_locked(record, shared=shared)

    def list(
        self,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Return public summaries for all in-memory runs.

        Shared-in runs join the listing REGARDLESS of the workspace
        namespace filter (they carry the grantor's workspace id) and
        carry the additive ``access`` annotation.
        """
        with self._lock:
            self._cleanup_locked()
            summaries = []
            for record in sorted(
                self._records.values(),
                # (created_at, run_id) matches list_page's keyset order so the
                # unbounded read and the paginated endpoint agree on ties.
                key=lambda item: (item.created_at, item.run_id),
                reverse=True,
            ):
                if _workspace_matches(record, workspace_id) and _visible_to_matches(
                    record, visible_to
                ):
                    summaries.append(self._summary_locked(record))
                    continue
                shared = self._shared_permission_locked(record, visible_to)
                if shared is not None:
                    summaries.append(self._summary_locked(record, shared=shared))
            return summaries

    def list_session_runs(
        self,
        session_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Visible summaries of one agent session, oldest first."""
        with self._lock:
            self._cleanup_locked()
            return [
                self._summary_locked(record)
                for record in sorted(
                    (
                        record
                        for record in self._records.values()
                        if record.session_id == session_id
                        and _visible_to_matches(record, visible_to)
                    ),
                    key=lambda item: (item.created_at, item.run_id),
                )
            ]

    def session_owners(
        self, session_id: str
    ) -> set[tuple[str | None, uuid.UUID | None]]:
        """Return every recorded owner identity for ``session_id``."""
        with self._lock:
            self._cleanup_locked()
            return {
                (record.created_by_tenant_id, record.created_by_user_id)
                for record in self._records.values()
                if record.session_id == session_id
            }

    def list_page(
        self,
        *,
        limit: int,
        after: tuple[float, str] | None = None,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One keyset page of visible runs + the next cursor (wire parity).

        Mirrors the durable store's ``list_page`` wire shape: filter-before-
        slice so pages stay full, newest-first over ``(created_at, run_id)``.
        Note this in-process tier sorts the whole visible set per call
        (O(n log n)); the durable store is the truly index-bounded path, and
        multi-replica scale requires that backend anyway (the in-memory store
        is per-pod). The page bound here caps the RESPONSE size, not the scan.
        """
        with self._lock:
            self._cleanup_locked()
            visible: list[tuple[RunRecord, "SharePermission | None"]] = []
            for record in self._records.values():
                if _workspace_matches(record, workspace_id) and _visible_to_matches(
                    record, visible_to
                ):
                    visible.append((record, None))
                    continue
                shared = self._shared_permission_locked(record, visible_to)
                if shared is not None:
                    visible.append((record, shared))
            visible.sort(
                key=lambda item: (item[0].created_at, item[0].run_id),
                reverse=True,
            )
            page, next_cursor = keyset_page(
                visible,
                limit=limit,
                after=after,
                created_at_of=lambda item: item[0].created_at,
                id_of=lambda item: item[0].run_id,
            )
            summaries = [
                self._summary_locked(record, shared=shared) for record, shared in page
            ]
            return summaries, next_cursor

    def metrics_snapshot(self) -> "RunStoreMetrics":
        """In-process queue load under the store lock (see the port)."""
        with self._lock:
            return RunStoreMetrics(
                queued=len(self._pending),
                active=self._running_count,
                capacity=self._max_concurrent,
            )

    def binding_overview(
        self,
        *,
        mode: str | None,
        below: int | None,
        above: int | None,
        limit: int,
    ) -> dict[str, Any]:
        """Open runs by binding, for the operator list (see the port)."""
        with self._lock:
            open_records = [
                record
                for record in self._records.values()
                if record.status not in TERMINAL_RUN_STATUSES
                and (mode is None or record.mode == mode)
            ]
        def shown(record: RunRecord) -> str:
            return BINDING_ASKED_STATUS if record.cancel_requested else record.status.value

        counts: dict[tuple[str, str, int], int] = {}
        for record in open_records:
            key = (record.mode, shown(record), record.workflow_contract)
            counts[key] = counts.get(key, 0) + 1
        outside = sorted(
            (
                record
                for record in open_records
                if (below is not None and record.workflow_contract < below)
                or (above is not None and record.workflow_contract > above)
            ),
            key=lambda record: (record.created_at, record.run_id),
        )
        return binding_overview_payload(
            counts,
            [
                (record.run_id, record.mode, shown(record), record.workflow_contract)
                for record in outside[:limit]
            ],
            total=len(outside),
        )

    def cancel_outside_binding(
        self,
        *,
        mode: str,
        workflow_contracts: Mapping[str, int],
        actor_user_id: uuid.UUID | None,
    ) -> dict[str, int]:
        """Cancel the open runs of *mode* this server cannot run (see the port)."""
        server_contract = workflow_contracts.get(mode)
        outcome = {"cancelled": 0, "cancel_requested": 0}
        with self._lock:
            self._cleanup_locked()
            outside = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.status not in TERMINAL_RUN_STATUSES
                    and record.mode == mode
                    and (
                        server_contract is None
                        or record.workflow_contract > server_contract
                    )
                ),
                key=lambda record: (record.created_at, record.run_id),
            )
            # Oldest first: a parent is created before its children, so its
            # cancel takes them along. A run already asked to stop (by this
            # cancel's parent or an earlier one) is neither asked nor audited
            # again.
            for record in outside:
                if record.status in TERMINAL_RUN_STATUSES or record.cancel_requested:
                    continue
                self._cancel_record_locked(record, reason_override=ADMIN_CANCEL_REASON)
                self._cancel_descendants_locked(record, cascade_reason="parent_cancelled")
                self._append_run_effect_locked(
                    record, action="run.cancelled_by_admin", actor_user_id=actor_user_id
                )
                outcome[
                    "cancelled"
                    if record.status in TERMINAL_RUN_STATUSES
                    else "cancel_requested"
                ] += 1
        return outcome

    def result(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Return the stored result payload for a completed run."""
        with self._lock:
            self._cleanup_locked()
            record, _shared = self._record_locked(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            if record.result is None:
                raise RunNotFound(run_id)
            return {
                "run_id": run_id,
                "status": record.status.value,
                **record.result,
            }

    def cancel(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Request cancellation for a queued or running run.

        Shared-in recipients need at least an ``edit`` grant — a
        view-only invitee watching a run must not be able to kill it;
        the denial is the indistinct 404.
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
        """Cancel one run tree and return ids touched under the store lock."""
        with self._lock:
            self._cleanup_locked()
            record, shared = self._record_locked(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            if shared is not None and not shared.at_least(SharePermission.EDIT):
                raise RunNotFound(run_id)
            try:
                authority = (
                    self._caller_control_authority_context_locked(record, visible_to)
                    if visible_to is not None
                    else self._execution_authority_context_locked(record)
                )
                with authority:
                    result = self._cancel_tree_locked(record)
                    self._append_run_effect_locked(
                        record,
                        action="run.cancel_requested",
                        actor_user_id=(
                            visible_to.principal.user_id
                            if visible_to is not None
                            else record.execution_actor_user_id
                        ),
                    )
                    return result
            except AuthorizationRevoked as exc:
                raise RunNotFound(run_id) from exc

    def authorized_control_write(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        control_write: Any,
    ) -> Any:
        """Apply one control-table write under the caller's live run grant.

        The run lock and the bound identity guard remain held while the
        callback updates the memory control store. Its ``cancel_child``
        helper is deliberately limited to a direct child of ``run_id`` and
        uses the already-authorized root; child runs inherit the root share
        and never require a second, nonexistent share row.
        """
        with self._lock:
            self._cleanup_locked()
            record = self._records.get(run_id)
            if record is None or not _workspace_matches(record, workspace_id):
                raise RunNotFound(run_id)
            try:
                authority = self._caller_control_authority_context_locked(
                    record, visible_to
                )
                with authority:

                    def _cancel_child(child_run_id: str) -> str:
                        child = self._records.get(child_run_id)
                        if child is None or child.parent_run_id != run_id:
                            raise RunNotFound(child_run_id)
                        child_root = self._execution_root_locked(child)
                        parent_root = self._execution_root_locked(record)
                        if child_root.run_id != parent_root.run_id:
                            raise RunNotFound(child_run_id)
                        summary, _affected = self._cancel_tree_locked(child)
                        return str(summary["status"])

                    return control_write(None, _cancel_child)
            except AuthorizationRevoked as exc:
                raise RunNotFound(run_id) from exc

    def _cancel_tree_locked(
        self, record: RunRecord
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Cancel ``record`` and descendants while the store lock is held."""
        summary = self._cancel_record_locked(record)
        affected = [record.run_id, *self._cancel_descendants_locked(record)]
        return summary, tuple(affected)

    def _cancel_descendants_locked(
        self,
        record: RunRecord,
        *,
        cascade_reason: str | None = None,
        ended: str | None = None,
    ) -> list[str]:
        """Cancel every live descendant of ``record`` (lock held).

        The ONE parent-link walk behind every explicit tree cancel and every
        end of a run other than its completion: a run that failed, stopped or
        timed out must never leave its children running invisibly (orphans
        burning quota behind a dead run). The store lock also serializes
        child admission against this traversal, so nested Kernel -> Mission
        -> Research trees terminate as one unit.

        ``cascade_reason`` overrides the per-state cancel reason on every
        descendant event (``parent_failed``, ``parent_cancelled``, so an
        orphan's cancel says WHY); the explicit tree cancel leaves it
        ``None`` to keep its own reasons. A running descendant whose cancel
        was already requested is left alone: it ends with the reason of its
        own request, which no cascade overwrites. ``ended`` completes the
        operator line logged when the walk cancelled anything.

        Returns:
            Every descendant id, terminal ones included.
        """
        frontier = [record.run_id]
        seen = {record.run_id}
        affected: list[str] = []
        cancelled = 0
        while frontier:
            parent_id = frontier.pop()
            children = [
                child
                for child in self._records.values()
                if child.parent_run_id == parent_id and child.run_id not in seen
            ]
            for child in children:
                seen.add(child.run_id)
                affected.append(child.run_id)
                frontier.append(child.run_id)
                if child.status in TERMINAL_RUN_STATUSES or child.cancel_requested:
                    continue
                cancelled += 1
                self._cancel_record_locked(child, reason_override=cascade_reason)
        if cancelled and ended is not None:
            log.warning(
                "Run %s %s; %d active child runs cancelled (%s).",
                record.run_id,
                ended,
                cancelled,
                cascade_reason,
            )
        return affected

    def _cancel_record_locked(
        self, record: RunRecord, *, reason_override: str | None = None
    ) -> dict[str, Any]:
        """Cancel one record in place (queued/waiting/running semantics).

        ``reason_override`` replaces the per-state cancel reason (used by
        the parent-failure cascade to stamp ``parent_failed``); ``None``
        keeps the state-specific default.
        """
        if record.status == RunStatus.QUEUED:
            self._remove_pending_locked(record.run_id)
            record.cancel_event.set()
            self._mark_terminal_locked(record, RunStatus.CANCELLED)
            self._emit_locked(
                record,
                RUN_CANCELLED_EVENT,
                {
                    "status": "cancelled",
                    "reason": reason_override or "cancelled_before_start",
                },
            )
            record.work = None
            return self._summary_locked(record)
        if record.status in WAITING_RUN_STATUSES:
            # Nothing is executing — the wait resolves immediately.
            record.cancel_event.set()
            self._mark_terminal_locked(record, RunStatus.CANCELLED)
            record.waiting_since = None
            self._emit_locked(
                record,
                RUN_CANCELLED_EVENT,
                {
                    "status": "cancelled",
                    "reason": reason_override or "cancelled_while_waiting",
                },
            )
            record.work = None
            return self._summary_locked(record)
        if record.status == RunStatus.RUNNING:
            record.cancel_event.set()
            record.cancel_reason = reason_override or "client_requested_cancel"
            self._emit_locked(
                record,
                RUN_CANCEL_REQUESTED_EVENT,
                {"status": "running", "reason": record.cancel_reason},
            )
            return self._summary_locked(record)
        return self._summary_locked(record)

    def delete(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        requester_user_id: uuid.UUID | None = None,
    ) -> None:
        """Permanently remove one terminal run (owner-only).

        Stronger than :meth:`cancel`: a shared-in recipient — even with an
        ``edit`` grant — must never delete the owner's run, so the gate is
        creator identity, not share visibility. A non-owner or a
        cross-namespace caller gets the indistinct ``RunNotFound`` (denial
        equals absence). Only terminal runs are deletable; an active run
        raises ``RunActive`` so the executing worker cannot write into a
        record that vanished mid-run.
        """
        with self._lock:
            self._cleanup_locked()
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFound(run_id)
            if (
                record.created_by_user_id is not None
                and record.created_by_user_id != requester_user_id
            ) or not _workspace_matches(record, workspace_id):
                # Owner-only for runs that HAVE a recorded creator; a legacy
                # pre-scoping run (created_by_user_id is None) has no owner signal
                # but its workspace, so the namespace match alone gates it —
                # otherwise such a run would be undeletable by anyone.
                log_authorization_denial(
                    log,
                    action="delete",
                    principal_kind=None,
                    actor_user_id=requester_user_id,
                    tenant_id=record.created_by_tenant_id,
                    resource_type="run",
                    resource_id=run_id,
                )
                raise RunNotFound(run_id)
            if record.status not in TERMINAL_RUN_STATUSES:
                raise RunActive(run_id)
            try:
                authority = (
                    self._authority.resource_access_guard(
                        tenant_id=record.created_by_tenant_id or "default",
                        owner_user_id=record.created_by_user_id,
                        actor_user_id=requester_user_id,
                        resource_type="run",
                        resource_id=run_id,
                        minimum=SharePermission.EDIT,
                        owner_only=True,
                    )
                    if self._authority is not None
                    else nullcontext()
                )
                with authority:
                    if self._checkpoint_cleanup is not None:
                        self._checkpoint_cleanup(run_id, record.mode)
                    if self._authority is not None:
                        self._authority.revoke_deleted_resource(
                            tenant_id=record.created_by_tenant_id or "default",
                            actor_user_id=requester_user_id,
                            owner_user_id=record.created_by_user_id,
                            action="run.deleted",
                            resource_type="run",
                            resource_id=run_id,
                            scope="runs",
                        )
                    del self._records[run_id]
            except AuthorizationRevoked as exc:
                raise RunNotFound(run_id) from exc

    def subscribe(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        stream: bool = True,
    ) -> RunSubscription:
        """Subscribe to a run's event stream, replaying buffered events.

        ``stream=False`` is the one-shot replay read (JSON polling
        fallback): same visibility check and replay, but the queue is
        never registered — ``close()`` then finds nothing to detach.
        """
        with self._lock:
            self._cleanup_locked()
            record, _shared = self._record_locked(
                run_id,
                workspace_id=workspace_id,
                visible_to=visible_to,
            )
            queue: Queue = Queue()
            if stream:
                record.subscribers.append(queue)
            return RunSubscription(
                run_id=run_id,
                queue=queue,
                replay=list(record.events),
                store=self,
            )

    def unsubscribe(self, run_id: str, queue: Queue) -> None:
        """Remove a queue from the subscriber list if it is still present."""
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                return
            try:
                record.subscribers.remove(queue)
            except ValueError:
                return

    def emit(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        fence_attempt: int | None = None,
    ) -> None:
        """Emit one event to the run buffer and live subscribers.

        Terminal runs accept no further events: the log's ends-terminal
        invariant is what tells SSE streams (live and replayed) to stop.
        A post-terminal signal (e.g. a user editing an artifact after the
        run finished) is dropped LOUDLY — clients of finished runs
        reconcile via the REST reads, not the closed event stream.

        Args:
            fence_attempt: Accepted for :class:`RunStorePort` parity;
                ignored (see :meth:`mark_waiting` — the in-process store
                has no claim/reclaim to fence).
        """
        with self._lock:
            record = self._raw_record_locked(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                log.warning(
                    "Event %s for run %s discarded: the run is already terminal (%s), "
                    "and its event log ends with the terminal event.",
                    event_type,
                    run_id,
                    record.status.value,
                )
                return
            with self._execution_authority_context_locked(record):
                self._emit_locked(record, event_type, payload or {})

    def complete(
        self,
        run_id: str,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        fence_attempt: int | None = None,
    ) -> None:
        """Store the final result and mark the run completed.

        Args:
            fence_attempt: Accepted for :class:`RunStorePort` parity;
                ignored (see :meth:`mark_waiting`).
        """
        with self._lock:
            record = self._raw_record_locked(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                return
            with self._execution_authority_context_locked(record):
                if snapshot:
                    record.snapshot = dict(snapshot)
                record.result = dict(result)
                self._mark_terminal_locked(record, RunStatus.COMPLETED)
                metrics = (
                    result.get("metrics")
                    if isinstance(result.get("metrics"), dict)
                    else {}
                )
                self._emit_locked(
                    record,
                    RUN_COMPLETED_EVENT,
                    {
                        "status": "completed",
                        "metrics": metrics,
                        "result_url": f"/v1/runs/{run_id}/result",
                        "snapshot": record.snapshot,
                    },
                )

    def fail(
        self,
        run_id: str,
        message: str,
        *,
        error_type: str = "server_error",
        fence_attempt: int | None = None,
    ) -> None:
        """Mark a run failed with a sanitized error payload.

        Args:
            fence_attempt: Accepted for :class:`RunStorePort` parity;
                ignored (see :meth:`mark_waiting`).
        """
        with self._lock:
            record = self._raw_record_locked(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                return
            if error_type == AuthorizationRevoked.code:
                self._fail_record_locked(record, message, error_type)
                return
            try:
                with self._execution_authority_context_locked(record):
                    self._fail_record_locked(record, message, error_type)
            except AuthorizationRevoked:
                self._fail_record_locked(
                    record,
                    "Execution authority was revoked",
                    AuthorizationRevoked.code,
                )

    def mark_cancelled(
        self, run_id: str, *, reason: str, fence_attempt: int | None = None
    ) -> None:
        """Mark a running run cancelled after its worker exits.

        A run asked to stop ends with the reason of the latest request; its
        engine only saw the cancel event and reports a generic *reason*. Its
        live children go with it (``parent_cancelled``).

        Args:
            fence_attempt: Accepted for :class:`RunStorePort` parity;
                ignored (see :meth:`mark_waiting`).
        """
        with self._lock:
            record = self._raw_record_locked(run_id)
            if record.status in TERMINAL_RUN_STATUSES:
                return
            self._mark_terminal_locked(record, RunStatus.CANCELLED)
            self._emit_locked(
                record,
                RUN_CANCELLED_EVENT,
                {
                    "status": "cancelled",
                    "reason": record.cancel_reason or reason,
                    "snapshot": record.snapshot,
                },
            )
            self._cancel_descendants_locked(
                record, cascade_reason="parent_cancelled", ended="was cancelled"
            )

    def _fail_record_locked(
        self,
        record: RunRecord,
        message: str,
        error_type: str,
    ) -> None:
        """Apply one trusted failed terminal transition under the run lock."""
        record.result = None
        record.error = {
            "message": sanitize_error(message),
            "type": error_type,
        }
        self._mark_terminal_locked(record, RunStatus.FAILED)
        self._emit_locked(
            record,
            RUN_FAILED_EVENT,
            {
                "status": "failed",
                "error": record.error,
                "snapshot": record.snapshot,
            },
        )
        # Parent terminal failure cascades: live children of a failed
        # parent are orphans (their results have no consumer) — cancel
        # them through the same walk explicit tree-cancel uses, stamped
        # with ``parent_failed`` so each orphan's cancel says why.
        self._cancel_descendants_locked(
            record, cascade_reason="parent_failed", ended="failed"
        )

    def mark_waiting(
        self,
        run_id: str,
        *,
        status: RunStatus | str,
        fence_attempt: int | None = None,
    ) -> None:
        """Park a RUNNING run in a waiting status (agent interrupt).

        The executing closure returns after calling this; the run keeps
        its work closure so :meth:`resume_run` can re-dispatch it. Only
        waiting statuses are accepted, and only from RUNNING — anything
        else is a caller bug, raised loudly. Parking on children
        immediately self-heals the lost-wakeup race: when every child is
        ALREADY terminal at park time (the last one finished while this
        run was still unwinding towards the park), the run flips straight
        back to QUEUED instead of waiting for a wake-up that fired.

        Args:
            fence_attempt: Accepted for :class:`RunStorePort` parity with
                the durable backend (the worker calls both stores through
                the port and always passes it). Deliberately unused here:
                the in-process store executes closures on a single owner
                with no claim/reclaim, so there is no zombie attempt to
                fence out.
        """
        waiting = _coerce_status(status)
        if waiting not in WAITING_RUN_STATUSES:
            raise ValueError(f"not a waiting status: {status!r}")
        with self._lock:
            record = self._raw_record_locked(run_id)
            if record.status is not RunStatus.RUNNING:
                raise RunActive(
                    f"run {run_id} cannot wait from status {record.status.value}"
                )
            if record.cancel_event.is_set():
                # A cancel is already pending: parking would leave a
                # cancelled assignment sitting in a waiting status until
                # its TTL. Resolve the cancel now instead, with the
                # request's reason; the closure returns normally either way.
                self._mark_terminal_locked(record, RunStatus.CANCELLED)
                self._emit_locked(
                    record,
                    RUN_CANCELLED_EVENT,
                    {
                        "status": "cancelled",
                        "reason": record.cancel_reason or "cancelled_while_waiting",
                        "snapshot": record.snapshot,
                    },
                )
                return
            with self._execution_authority_context_locked(record):
                now = time.time()
                self._close_active_interval_locked(record, now)
                record.status = waiting
                record.waiting_since = now
                record.park_in_flight = True
                self._emit_locked(
                    record,
                    RUN_WAITING_EVENT,
                    {"status": waiting.value, "snapshot": record.snapshot},
                )
                if waiting is RunStatus.WAITING_FOR_CHILDREN:
                    # Lost-wakeup self-heal: the last child may have gone
                    # terminal BEFORE this park landed — its wake probe then
                    # found the parent still RUNNING and no-oped. Re-probe
                    # now; park_in_flight is set, so a hit defers the
                    # re-dispatch to the unwinding worker.
                    self._wake_parent_if_children_done_locked(run_id)

    def resume_run(
        self,
        run_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
        execution_scopes: frozenset[str] = frozenset(),
        control_write: Any = None,
    ) -> dict[str, Any]:
        """Move a waiting run back to QUEUED and dispatch it.

        The decision endpoints (approval/clarification) call this after
        recording the decision; execution resumes as a fresh dispatch of
        the retained work closure (the closure re-enters at its
        checkpoint).

        Raises:
            RunNotFound: Unknown id.
            RunActive: The run is not in a waiting status.
        """
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFound(run_id)
            caller: UserContext | None = None
            if actor_user_id is not None:
                actor = Principal(
                    user_id=actor_user_id,
                    kind="oidc_session",
                    tenant_id=record.created_by_tenant_id or "default",
                    role="member",
                    scopes=execution_scopes,
                )
                caller = UserContext(actor)
            try:
                authority = (
                    self._caller_control_authority_context_locked(record, caller)
                    if caller is not None
                    else self._execution_authority_context_locked(record)
                )
                with authority:
                    if record.status not in WAITING_RUN_STATUSES:
                        raise RunActive(
                            f"run {run_id} is not waiting "
                            f"(status {status_value(record.status)})"
                        )
                    if record.work is None:
                        # In-memory closures never survive a process restart;
                        # fail before the composed decision writer can mutate.
                        raise RunActive(f"run {run_id} has no retained work to resume")
                    if control_write is not None:
                        # Memory lockstep for Postgres' resume transaction:
                        # the callback updates the control store while every
                        # reader, share mutation and user disable is excluded
                        # by the same coordinator lock.
                        control_write(None)
                    if actor_user_id is not None:
                        record.execution_actor_user_id = actor_user_id
                        record.execution_scopes = frozenset(execution_scopes)
                    previous_status = record.status
                    now = time.time()
                    self._close_waiting_interval_locked(record, now)
                    self._begin_segment_locked(
                        record,
                        reason=self._resume_reason(previous_status),
                    )
                    record.status = RunStatus.QUEUED
                    record.queued_since = now
                    if record.park_in_flight:
                        # The parking worker has not unwound yet: dispatching
                        # now would run the same closure on two threads. The
                        # unwind performs the deferred re-dispatch.
                        record.resume_requested = True
                    else:
                        self._pending.append(run_id)
                    self._emit_locked(
                        record,
                        RUN_QUEUED_EVENT,
                        {
                            "status": "queued",
                            "queue_position": self._queue_position_locked(run_id),
                            "resumed": True,
                            "segment_id": record.current_segment_id,
                            "segment_ordinal": record.segment_count,
                        },
                    )
                    self._dispatch_locked()
                    return self._summary_locked(record)
            except AuthorizationRevoked as exc:
                raise RunNotFound(run_id) from exc

    @contextmanager
    def execution_control_guard(self, run_id: str):
        """Hold the effective actor's live run authority through one write."""
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise AuthorizationRevoked("run is missing")
            with self._execution_authority_context_locked(record):
                yield

    def execution_principal(
        self,
        run_id: str,
        *,
        fallback: Principal | None = None,
    ) -> Principal | None:
        """Reconstruct the actor governing the current segment."""
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise RunNotFound(run_id)
            root = self._execution_root_locked(record)
            if root.execution_actor_user_id is None:
                return fallback
            return Principal(
                user_id=root.execution_actor_user_id,
                kind="oidc_session",
                tenant_id=root.created_by_tenant_id or "default",
                role="member",
                scopes=root.execution_scopes,
            )

    def _execution_root_locked(self, record: RunRecord) -> RunRecord:
        """Return and validate the canonical root for one locked record."""
        root_id = record.root_run_id or record.run_id
        root = self._records.get(root_id)
        if root is None:
            raise AuthorizationRevoked("run root is missing")
        if (
            record.execution_actor_user_id != root.execution_actor_user_id
            or record.created_by_user_id != root.created_by_user_id
            or record.created_by_tenant_id != root.created_by_tenant_id
        ):
            raise AuthorizationRevoked(
                "run lineage has inconsistent execution authority"
            )
        return root

    def _execution_authority_context_locked(self, record: RunRecord):
        """Hold live root authorization across one in-memory mutation."""
        root = self._execution_root_locked(record)
        actor_user_id = root.execution_actor_user_id
        owner_user_id = root.created_by_user_id
        if self._resource_access_guard is None:
            if actor_user_id == owner_user_id:
                return nullcontext()
            raise AuthorizationRevoked(
                "in-memory run store has no transactional share guard"
            )
        return self._resource_access_guard(
            tenant_id=root.created_by_tenant_id or "default",
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            resource_type="run",
            resource_id=root.run_id,
            minimum=SharePermission.EDIT,
        )

    def _caller_control_authority_context_locked(
        self,
        record: RunRecord,
        visible_to: "UserContext | None",
    ) -> Any:
        """Hold a caller's live edit grant across one control mutation."""
        root = self._execution_root_locked(record)
        principal = visible_to.principal if visible_to is not None else None
        actor_user_id = principal.user_id if principal is not None else None
        owner_user_id = root.created_by_user_id
        tenant_id = root.created_by_tenant_id or "default"
        if principal is not None and principal.tenant_id != tenant_id:
            raise AuthorizationRevoked("control caller belongs to another tenant")
        if self._resource_access_guard is None:
            if actor_user_id == owner_user_id:
                return nullcontext()
            raise AuthorizationRevoked(
                "in-memory run store has no transactional share guard"
            )
        return self._resource_access_guard(
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            actor_user_id=actor_user_id,
            resource_type="run",
            resource_id=root.run_id,
            minimum=SharePermission.EDIT,
        )

    def check_execution_authority(self, run_id: str) -> None:
        """Assert live edit authority for the persisted effective actor."""
        with self._lock:
            record = self._records.get(run_id)
            if record is None:
                raise AuthorizationRevoked("run is missing")
            with self._execution_authority_context_locked(record):
                return

    def children(self, run_id: str) -> list[dict[str, Any]]:
        """Summaries of this run's direct children, newest first.

        Authorization happens on the PARENT (the route resolves it via
        :meth:`get` before calling this); children inherit that access
        as well, so no per-child visibility filter here.
        """
        with self._lock:
            self._cleanup_locked()
            records = sorted(
                (
                    record
                    for record in self._records.values()
                    if record.parent_run_id == run_id
                ),
                key=lambda record: record.created_at,
                reverse=True,
            )
            return [self._summary_locked(record) for record in records]

    def _run_worker(
        self,
        run_id: str,
        work: RunWork,
        cancel_event: threading.Event,
        request_payload: dict[str, Any] | None = None,
        tenant_id: str | None = None,
        actor_user_id: "uuid.UUID | None" = None,
    ) -> None:
        handle = RunHandle(self, run_id, cancel_event)
        crashed = False
        # The in-process pendant to the queue worker's binding: every log
        # line of this run thread carries the run_id (JSON mode), and the
        # run root span parents itself in the submitter's trace context
        # (the dispatcher hands the payload over — this thread must not
        # touch the store lock before ``work`` starts). The lane gave this
        # body a context of its own, so neither binding outlives it.
        # Setup INSIDE the try: a telemetry failure must never skip the
        # finally below, which is the ONLY place _running_count is
        # decremented and _dispatch_locked runs — a leaked slot would
        # silently stall the in-memory dispatcher after max_concurrent.
        telemetry_stack = ExitStack()
        try:
            # Same correlation field set as the other two execution
            # boundaries (worker loop, durable no-queue): run + tenant +
            # subject pseudonym. The values RIDE ALONG from the
            # dispatcher for the same reason request_payload does — this
            # thread must not touch the store lock before ``work``
            # starts (callers observe side effects racing submit-return).
            span_tenant = str(tenant_id or "default")
            telemetry_stack.enter_context(
                run_execute_span(
                    run_id=run_id,
                    tenant_id=span_tenant,
                    attempt=0,
                    payload=request_payload,
                )
            )
            context_fields: dict[str, object] = {
                "run_id": run_id,
                "tenant": span_tenant,
            }
            if actor_user_id is not None:
                from inqtrix.auth.log_redaction import stable_pseudonym

                context_fields["user"] = stable_pseudonym(
                    "usr", actor_user_id
                )
            bind_log_context(**context_fields)
            work(handle)
            with self._lock:
                record = self._records.get(run_id)
                if (
                    record is not None
                    and not handle.parked
                    and record.status not in TERMINAL_RUN_STATUSES
                    and record.status not in WAITING_RUN_STATUSES
                ):
                    # ``handle.parked`` guards the resumed-in-window
                    # case: a resume may have flipped the run back to
                    # QUEUED before this line — completing it here
                    # would destroy the interrupt.
                    try:
                        with self._execution_authority_context_locked(record):
                            self._mark_terminal_locked(record, RunStatus.COMPLETED)
                            self._emit_locked(
                                record,
                                RUN_COMPLETED_EVENT,
                                {
                                    "status": "completed",
                                    "snapshot": record.snapshot,
                                },
                            )
                    except AuthorizationRevoked:
                        self._fail_record_locked(
                            record,
                            "Execution authority was revoked",
                            AuthorizationRevoked.code,
                        )
        except Exception as exc:  # noqa: BLE001 - run workers must terminate cleanly
            crashed = True
            log.exception("Native run %s failed", run_id)
            terminate_native_run(handle, exc)
        finally:
            telemetry_stack.close()
            # The park handoff completes HERE: a resume that arrived
            # before this unwind parked its request in
            # ``resume_requested`` and is dispatched now.
            with self._lock:
                record = self._records.get(run_id)
                if record is not None:
                    parked_alive = (
                        handle.parked
                        and not crashed
                        and (
                            record.status in WAITING_RUN_STATUSES
                            or (
                                record.status is RunStatus.QUEUED
                                and record.resume_requested
                            )
                        )
                    )
                    if parked_alive:
                        # A parked run keeps its closure — resume_run
                        # re-dispatches the same segment-aware work.
                        record.park_in_flight = False
                        if record.resume_requested:
                            record.resume_requested = False
                            self._pending.append(run_id)
                    else:
                        record.work = None
                        record.park_in_flight = False
                        record.resume_requested = False
                self._running_count = max(0, self._running_count - 1)
                self._dispatch_locked()

    def _dispatch_locked(self) -> None:
        while self._running_count < self._max_concurrent and self._pending:
            run_id = self._pending.popleft()
            record = self._records.get(run_id)
            if (
                record is None
                or record.status != RunStatus.QUEUED
                or record.work is None
            ):
                continue
            initial_start = record.started_at is None
            now = time.time()
            if initial_start:
                record.queued_since = None
            else:
                self._close_queued_interval_locked(record, now)
            if initial_start:
                record.started_at = now
                self._begin_segment_locked(record, reason="initial")
            elif record.current_segment_id is None:
                # Defensive compatibility for an older in-memory record that
                # was created before execution segments were introduced.
                self._begin_segment_locked(record, reason="resume")
            record.status = RunStatus.RUNNING
            record.active_started_at = now
            self._running_count += 1
            self._emit_locked(
                record,
                (RUN_STARTED_EVENT if initial_start else RUN_RESUMED_EVENT),
                {
                    "status": "running",
                    "snapshot": record.snapshot,
                    "segment_id": record.current_segment_id,
                    "segment_ordinal": record.segment_count,
                    "reason": record.current_segment_reason,
                },
            )
            # request_payload rides along because the dispatcher already
            # holds the lock here — the body must not re-acquire it before
            # ``work`` starts (callers observe side effects of ``work`` racing
            # submit-return).
            aio.start_legacy_segment(
                partial(
                    self._run_worker,
                    run_id,
                    record.work,
                    record.cancel_event,
                    dict(record.request_payload),
                    record.created_by_tenant_id,
                    record.created_by_user_id,
                )
            )

    def _retention_seconds(self, record: RunRecord) -> int:
        """How long a terminal *record* stays; an incognito ask leaves early."""
        if record.incognito:
            return min(self._completed_ttl_seconds, self._incognito_ttl_seconds)
        return self._completed_ttl_seconds

    def _cleanup_locked(self) -> None:
        now = time.monotonic()
        # A wait must never linger silently forever: past the TTL the run
        # is auto-cancelled with a visible reason (event + warning), then
        # ages out via the normal terminal TTL. The reason names WHAT was
        # awaited: a human decision (``approval_timeout``) or child runs
        # that never all terminated (``children_timeout``).
        wall_now = time.time()
        timed_out: list[RunRecord] = []
        for record in list(self._records.values()):
            if (
                record.status in WAITING_RUN_STATUSES
                and record.waiting_since is not None
                and (wall_now - record.waiting_since) > self._waiting_ttl_seconds
            ):
                reason = (
                    "children_timeout"
                    if record.status is RunStatus.WAITING_FOR_CHILDREN
                    else "approval_timeout"
                )
                log.warning(
                    "Run %s waited longer than %d s and was cancelled automatically "
                    "(%s).",
                    record.run_id,
                    int(self._waiting_ttl_seconds),
                    reason,
                )
                record.cancel_event.set()
                self._mark_terminal_locked(record, RunStatus.CANCELLED)
                record.waiting_since = None
                record.work = None
                self._emit_locked(
                    record,
                    RUN_CANCELLED_EVENT,
                    {"status": "cancelled", "reason": reason},
                )
                timed_out.append(record)
        # After every timed-out wait ended with its own reason: a timed-out
        # child keeps approval_timeout, the live rest goes with its parent.
        for record in timed_out:
            self._cancel_descendants_locked(
                record, cascade_reason="parent_cancelled", ended="timed out waiting"
            )
        expired = [
            run_id
            for run_id, record in self._records.items()
            if record.status in TERMINAL_RUN_STATUSES
            and record.finished_monotonic is not None
            and (now - record.finished_monotonic) > self._retention_seconds(record)
            and not record.subscribers
            and not (
                record.kind in {"agent", "agent_child"} and record.session_id
                and self._session_retention is not None
                and self._session_retention(
                    session_id=record.session_id,
                    tenant_id=record.created_by_tenant_id or "default",
                    owner_user_id=record.created_by_user_id,
                    workspace_id=record.workspace_id,
                )
            )
        ]
        for run_id in expired:
            record = self._records[run_id]
            if self._checkpoint_cleanup is not None:
                try:
                    self._checkpoint_cleanup(run_id, record.mode)
                except RunCleanupFailed:
                    continue
            if self._authority is not None:
                self._authority.revoke_deleted_resource(
                    tenant_id=record.created_by_tenant_id or "default",
                    actor_user_id=record.created_by_user_id,
                    owner_user_id=record.created_by_user_id,
                    action="run.retention_deleted",
                    resource_type="run",
                    resource_id=run_id,
                    scope="runs",
                )
            del self._records[run_id]

    def _record_locked(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[RunRecord, "SharePermission | None"]:
        """The record plus the share grant that admitted it (if any).

        Shared-in runs BYPASS the workspace namespace filter — they
        carry the GRANTOR's workspace id, which would otherwise hide
        every shared run from recipients filtering their own
        namespace.
        """
        record = self._records.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        if _visible_to_matches(record, visible_to):
            if not _workspace_matches(record, workspace_id):
                raise RunNotFound(run_id)
            return record, None
        shared = self._shared_permission_locked(record, visible_to)
        if shared is not None:
            return record, shared
        # The client sees the indistinct 404; the denial itself must
        # stay operator-visible. This store only logs
        # it: it is sync/threaded and the audit sink is async; the
        # durable run store also persists denials to its audit sink.
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
        raise RunAccessDenied(run_id)

    def _raw_record_locked(self, run_id: str) -> RunRecord:
        """Return a record for store-internal lifecycle writes."""
        record = self._records.get(run_id)
        if record is None:
            raise RunNotFound(run_id)
        return record

    def _shared_permission_locked(
        self,
        record: RunRecord,
        visible_to: "UserContext | None",
    ) -> SharePermission | None:
        """Read the accepted direct share at the decision point."""
        if (
            visible_to is None
            or self._share_lookup is None
            or visible_to.principal.user_id is None
            or record.created_by_user_id is None
        ):
            return None
        principal = visible_to.principal
        shared = self._share_lookup(
            tenant_id=principal.tenant_id,
            resource_type="run",
            resource_id=record.run_id,
            recipient_user_id=principal.user_id,
        )
        if shared is None:
            return None
        if self._restrict_share_workspaces:
            if self._share_workspace_check is None or not self._share_workspace_check(
                tenant_id=principal.tenant_id,
                user_id_a=record.created_by_user_id,
                user_id_b=principal.user_id,
            ):
                return None
        return shared

    def _new_unique_run_id_locked(self) -> str:
        for _ in range(8):
            run_id = new_run_id()
            if run_id not in self._records:
                return run_id
            log.warning("Native run id collision detected; retrying allocation.")
        raise RuntimeError("could not allocate a unique native run id")

    def _mark_terminal_locked(self, record: RunRecord, status: RunStatus | str) -> None:
        now = time.time()
        self._close_current_interval_locked(record, now)
        record.status = _coerce_status(status)
        record.finished_at = now
        record.finished_monotonic = time.monotonic()
        self._append_terminal_audit_locked(record)
        # Every terminal transition of an agent child funnels through
        # here (complete/fail/cancel/TTL alike), so this is THE choke
        # point for waking a parent parked on its children.
        if record.kind == "agent_child" and record.parent_run_id:
            self._wake_parent_if_children_done_locked(record.parent_run_id)

    def _append_terminal_audit_locked(self, record: RunRecord) -> None:
        """Dienststart-Index terminal row (memory twin of the Postgres
        in-transaction write): metadata + correlation only. Callers set
        result/error BEFORE the terminal transition, so both are
        readable here."""
        if not self._audit_service_starts or self._authority is None:
            return
        action = {
            RunStatus.COMPLETED: "run.completed",
            RunStatus.FAILED: "run.failed",
            RunStatus.CANCELLED: "run.cancelled",
        }.get(record.status)
        if action is None:
            return
        usage = (record.result or {}).get("usage") or {}
        detail: dict[str, str] = {"mode": record.mode or "standard"}
        if record.created_at and record.finished_at:
            detail["duration_s"] = str(
                round(max(0.0, record.finished_at - record.created_at), 2)
            )
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        if prompt_tokens or completion_tokens:
            detail["prompt_tokens"] = str(prompt_tokens)
            detail["completion_tokens"] = str(completion_tokens)
        if record.error and record.error.get("type"):
            detail["error_type"] = str(record.error["type"])
        correlation = {"run_id": record.run_id}
        if record.trace_id:
            correlation["trace_id"] = record.trace_id
        workspace_uuid = None
        if record.workspace_id:
            try:
                workspace_uuid = uuid.UUID(str(record.workspace_id))
            except ValueError:
                workspace_uuid = None
        append_row = getattr(self._authority, "append_audit_row", None)
        if append_row is None:
            return
        try:
            append_row(
                tenant_id=record.created_by_tenant_id or "default",
                actor_user_id=(
                    record.execution_actor_user_id
                    or record.created_by_user_id
                ),
                action=action,
                resource_type="run",
                resource_id=record.run_id,
                detail=detail,
                outcome=(
                    "success"
                    if record.status is RunStatus.COMPLETED
                    else "failure"
                ),
                correlation=correlation,
                workspace_id=workspace_uuid,
            )
        except Exception:  # noqa: BLE001 — index row must not kill terminals
            log.warning(
                "Could not write the service-start index row for run %s.",
                record.run_id,
                exc_info=True,
            )

    def _wake_parent_if_children_done_locked(self, parent_run_id: str) -> None:
        """Resume a children-parked parent once its last child ended.

        No-ops unless the parent currently sits in
        ``waiting_for_children`` AND every sibling is terminal — the
        CAS-like status check makes at-least-once invocation safe (a
        second call finds the parent already QUEUED). The park handshake
        is honored exactly like :meth:`resume_run`: a parent whose
        parking worker has not unwound yet defers its re-dispatch to
        that unwind via ``resume_requested``.
        """
        parent = self._records.get(parent_run_id)
        if parent is None or parent.status is not RunStatus.WAITING_FOR_CHILDREN:
            return
        if any(
            sibling.parent_run_id == parent_run_id
            and sibling.status not in TERMINAL_RUN_STATUSES
            for sibling in self._records.values()
        ):
            return
        parent.status = RunStatus.QUEUED
        now = time.time()
        self._close_waiting_interval_locked(parent, now)
        self._begin_segment_locked(parent, reason="children")
        parent.queued_since = now
        if parent.park_in_flight:
            parent.resume_requested = True
        else:
            self._pending.append(parent_run_id)
        self._emit_locked(
            parent,
            RUN_QUEUED_EVENT,
            {
                "status": "queued",
                "queue_position": self._queue_position_locked(parent_run_id),
                "resumed": True,
                "segment_id": parent.current_segment_id,
                "segment_ordinal": parent.segment_count,
            },
        )
        self._dispatch_locked()

    @staticmethod
    def _resume_reason(status: RunStatus) -> str:
        if status is RunStatus.WAITING_FOR_APPROVAL:
            return "approval"
        if status is RunStatus.WAITING_FOR_INPUT:
            return "input"
        if status is RunStatus.WAITING_FOR_CHILDREN:
            return "children"
        return "resume"

    @staticmethod
    def _begin_segment_locked(record: RunRecord, *, reason: str) -> None:
        record.segment_count += 1
        record.current_segment_id = run_segment_id(record.run_id, record.segment_count)
        record.current_segment_reason = reason

    @staticmethod
    def _close_active_interval_locked(record: RunRecord, now: float) -> None:
        if record.active_started_at is not None:
            record.active_seconds += max(0.0, now - record.active_started_at)
            record.active_started_at = None

    @staticmethod
    def _close_waiting_interval_locked(record: RunRecord, now: float) -> None:
        if record.waiting_since is not None:
            record.waiting_seconds += max(0.0, now - record.waiting_since)
            record.waiting_since = None

    @staticmethod
    def _close_queued_interval_locked(record: RunRecord, now: float) -> None:
        if record.queued_since is not None:
            record.queued_seconds += max(0.0, now - record.queued_since)
            record.queued_since = None

    def _close_current_interval_locked(self, record: RunRecord, now: float) -> None:
        if record.status is RunStatus.RUNNING:
            self._close_active_interval_locked(record, now)
        elif record.status in WAITING_RUN_STATUSES:
            self._close_waiting_interval_locked(record, now)
        elif record.status is RunStatus.QUEUED:
            self._close_queued_interval_locked(record, now)

    def _queue_position_locked(self, run_id: str) -> int | None:
        try:
            return list(self._pending).index(run_id) + 1
        except ValueError:
            return None

    def _remove_pending_locked(self, run_id: str) -> None:
        try:
            self._pending.remove(run_id)
        except ValueError:
            return

    def _summary_locked(
        self,
        record: RunRecord,
        *,
        shared: "SharePermission | None" = None,
    ) -> dict[str, Any]:
        return build_run_summary(
            record,
            queue_position=self._queue_position_locked(record.run_id),
            access=access_annotation(shared, owner_user_id=record.created_by_user_id),
        )

    def _emit_locked(
        self,
        record: RunRecord,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        new_snapshot, events = expand_run_event(
            event_type, payload, status=record.status.value
        )
        if new_snapshot is not None:
            record.snapshot = new_snapshot
        for expanded_type, clean_payload in events:
            self._append_event_locked(record, expanded_type, clean_payload)
        self._project_child_progress_locked(
            record,
            event_type=event_type,
            payload=payload,
        )

    def _project_child_progress_locked(
        self,
        child: RunRecord,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Append a bounded child status projection to its parent stream."""
        if (
            child.kind != "agent_child"
            or not child.parent_run_id
            or not child.parent_task_id
            or not should_project_child_event(
                event_type, payload, work_profile=child.work_profile
            )
        ):
            return
        parent = self._records.get(child.parent_run_id)
        if parent is None or parent.status in TERMINAL_RUN_STATUSES:
            return
        projected = build_child_progress_payload(
            child_run_id=child.run_id,
            parent_task_id=child.parent_task_id,
            run_status=child.status.value,
            event_type=event_type,
            payload=payload,
            snapshot=child.snapshot,
            attempt=child.parent_task_attempt,
        )
        _, events = expand_run_event(
            CHILD_PROGRESS_EVENT,
            projected,
            status=parent.status.value,
        )
        for projected_type, clean_payload in events:
            self._append_event_locked(parent, projected_type, clean_payload)

    def _append_event_locked(
        self,
        record: RunRecord,
        event_type: str,
        clean_payload: dict[str, Any],
    ) -> None:
        if event_type == RUN_TRACE_EVENT:
            # Durable capture outside the bounded replay ring (see
            # RunRecord.trace_id); recency wins across retries.
            value = str(clean_payload.get("trace_id") or "")
            if value:
                record.trace_id = value
        record.event_seq += 1
        event = {
            "type": event_type,
            "run_id": record.run_id,
            "sequence": record.event_seq,
            "created_at": time.time(),
            "data": clean_payload,
        }
        record.events.append(event)
        for subscriber in list(record.subscribers):
            subscriber.put(event)

    # A control write runs its caller's callback under the identity guard, so it stays
    # off the loop; the rest is memory work of its own.
    asubmit = inline_on_loop(submit)
    aimport_completed_run = inline_on_loop(import_completed_run)
    aowner_user_id = inline_on_loop(owner_user_id)
    ashare_facts = inline_on_loop(share_facts)
    aevents_snapshot = inline_on_loop(events_snapshot)
    atrace_id = inline_on_loop(trace_id)
    aexecution_request_body = inline_on_loop(execution_request_body)
    asource_request_body = inline_on_loop(source_request_body)
    asource_run_catalogue = inline_on_loop(source_run_catalogue)
    asource_origins_visible = inline_on_loop(source_origins_visible)
    atotal_elapsed_seconds = inline_on_loop(total_elapsed_seconds)
    atitle = inline_on_loop(title)
    aget = inline_on_loop(get)
    alist = inline_on_loop(list)
    alist_session_runs = inline_on_loop(list_session_runs)
    asession_owners = inline_on_loop(session_owners)
    alist_page = inline_on_loop(list_page)
    ametrics_snapshot = inline_on_loop(metrics_snapshot)
    abinding_overview = inline_on_loop(binding_overview)
    acancel_outside_binding = inline_on_loop(cancel_outside_binding)
    aresult = inline_on_loop(result)
    acancel = inline_on_loop(cancel)
    acancel_tree = inline_on_loop(cancel_tree)
    aauthorized_control_write = on_transition_lane(authorized_control_write)
    adelete = inline_on_loop(delete)
    asubscribe = inline_on_loop(subscribe)
    aemit = inline_on_loop(emit)
    acomplete = inline_on_loop(complete)
    afail = inline_on_loop(fail)
    amark_cancelled = inline_on_loop(mark_cancelled)
    amark_waiting = inline_on_loop(mark_waiting)
    aresume_run = inline_on_loop(resume_run)
    aexecution_principal = inline_on_loop(execution_principal)
    acheck_execution_authority = inline_on_loop(check_execution_authority)
    achildren = inline_on_loop(children)
    asweep = inline_on_loop(sweep)


def _require_minimum(name: str, value: int, *, minimum: int) -> int:
    """Coerce an integer setting and reject invalid values loudly."""
    coerced = int(value)
    if coerced < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {coerced}")
    return coerced


def _workspace_matches(record: RunRecord, workspace_id: str | None) -> bool:
    """Return whether *record* belongs to the optional workspace namespace."""
    return workspace_id is None or record.workspace_id == workspace_id


def _visible_to_matches(record: RunRecord, visible_to: "UserContext | None") -> bool:
    """Authorization visibility predicate for one run record.

    ``None`` is an anonymous/static principal and may see only ownerless rows.
    A scoped principal sees runs owned by its canonical UUID in the same
    tenant. Ownerless records stay invisible to scoped principals rather than
    leaking across users. Shared access is resolved by the authorization
    layer, not by broadening this owner predicate.
    """
    if visible_to is None:
        return record.created_by_user_id is None
    return (
        record.created_by_user_id is not None
        and record.created_by_user_id == visible_to.principal.user_id
        and record.created_by_tenant_id == visible_to.principal.tenant_id
    )


def _coerce_status(status: RunStatus | str) -> RunStatus:
    """Convert status strings to RunStatus and fail on unknown values."""
    if isinstance(status, RunStatus):
        return status
    return RunStatus(status)


