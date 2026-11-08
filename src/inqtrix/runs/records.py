"""Records, statuses and handles of a run, shared by every run store.

The Postgres store, the services and the worker name these types without the
in-memory store behind :mod:`inqtrix.runs.memory_store`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable

from inqtrix.auth.principal import Principal
from inqtrix.runs.shared import (
    TERMINAL_RUN_STATUS_VALUES,
    answer_artifact_id,
    answer_publication_id,
)
from inqtrix.text import iter_bounded_chunks
from inqtrix.harness.contracts.events import (
    ANSWER_INTERRUPTED_EVENT,
    ANSWER_READY_EVENT,
    ANSWER_STARTED_EVENT,
)

if TYPE_CHECKING:
    from inqtrix.runs.memory_store import RunStore

log = logging.getLogger(__name__)

RunWork = Callable[["RunHandle"], None]

#: Largest ``inqtrix.output_text.delta`` of a published answer, in UTF-8
#: bytes. A finished answer of 65 KB becomes about 33 durable events instead
#: of one per word; the reader still gets contiguous offsets.
ANSWER_DELTA_MAX_BYTES = 2048


class RunStatus(StrEnum):
    """Lifecycle status for a native in-memory run.

    The ``waiting_*`` statuses are the parked, NON-terminal states of
    an agent run suspended mid-graph: the run holds no execution slot,
    is excluded from orphan/stuck sweeps (it is legitimately idle), and
    resumes through ``resume_run``. ``waiting_for_approval`` and
    ``waiting_for_input`` wait on a human decision;
    ``waiting_for_children`` waits on the terminal write of the last
    child research run the agent submitted (the store wakes the parent
    itself — no human involved). An unanswered wait is auto-cancelled
    after ``waiting_ttl_seconds`` (visible, reason ``approval_timeout``
    or ``children_timeout``) — never silently forever.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_CHILDREN = "waiting_for_children"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Enum twin of the shared string set (shared.py may not import this layer);
# deriving it here keeps the two representations from drifting.
TERMINAL_RUN_STATUSES = frozenset(
    RunStatus(value) for value in TERMINAL_RUN_STATUS_VALUES
)

WAITING_RUN_STATUSES = frozenset(
    {
        RunStatus.WAITING_FOR_APPROVAL,
        RunStatus.WAITING_FOR_INPUT,
        RunStatus.WAITING_FOR_CHILDREN,
    }
)


class RunQueueFull(RuntimeError):
    """Raised when the native run queue has no free slot."""


class RunPerUserLimit(RunQueueFull):
    """Raised when the submitting user hit their in-flight run cap.

    Subclass of :class:`RunQueueFull` so every existing 429 path keeps
    working; the router distinguishes it to tell the caller THEIR cap
    was hit (retry after own runs finish), not the shared queue.
    Counted statuses are QUEUED+RUNNING — a parked (waiting) run holds
    no execution slot and must not eat its owner's fairness budget.
    """


class RunActive(RuntimeError):
    """Raised when a delete targets a run that is still queued or running.

    Deletion is terminal-only: removing a record an executing worker still
    holds would let its final write resurrect a half-gone run. The caller
    cancels first, then deletes once the run reaches a terminal state.
    """


class RunSessionActive(RuntimeError):
    """Raised when a session already has an active root agent run."""


class RunParentInactive(RuntimeError):
    """Raised when a child submit races a cancelled or terminal ancestor."""


IMPORTED_RUN_CONTRACT = 1
"""The workflow contract of an imported report. It never runs, so no claim
reads its binding; it takes the first contract."""


@dataclass
class RunRecord:
    """Mutable server-side state for one native run."""

    run_id: str
    question: str
    stack_name: str
    workspace_id: str | None
    created_at: float
    work: RunWork | None = field(repr=False)
    mode: str
    workflow_contract: int
    """The run's binding: its mode and the workflow contract it was accepted
    under. Mirrors the durable ``runs`` columns; neither has a default."""
    created_by_user_id: uuid.UUID | None = None
    """Canonical user UUID that owns the run.

    The value is server-resolved from the principal, unlike ``workspace_id``,
    which is the client-supplied UI namespace. ``None`` identifies an
    ownerless run in an unscoped deployment.
    """
    created_by_tenant_id: str | None = None
    """Tenant of the submitting principal."""
    execution_actor_user_id: uuid.UUID | None = None
    """User whose current authority governs the next execution segment."""
    execution_scopes: frozenset[str] = frozenset()
    """Scope ceiling captured from the request that started the segment."""
    request_payload: dict[str, Any] = field(default_factory=dict, repr=False)
    """Persisted execution input used to restore immutable dependencies.

    This mirrors the durable run row without exposing the payload through the
    public run summary. Keeping the same source in both backends prevents
    control-plane validation from deriving a second dependency scope.
    """
    source_run_id: str | None = None
    """Client-local identity of an imported report.

    It is an idempotency key inside the importing owner's scope, never the
    public server run id. Native runs leave it ``None``.
    """
    agent_overrides: dict[str, Any] = field(default_factory=dict)
    kind: str = "standard"
    """Run role in an agent tree: ``standard`` (every historical run),
    ``agent`` (a workspace-agent root) or ``agent_child`` (a research
    run spawned by an agent task). Summaries omit the default."""
    parent_run_id: str | None = None
    """The spawning agent run for ``agent_child`` rows; ``None`` else."""
    root_run_id: str | None = None
    """The tree root (== parent for one-level trees); ``None`` else."""
    session_id: str | None = None
    """Saved Agent or Knowledge session this run belongs to; ``None`` else."""
    incognito: bool = False
    """An incognito knowledge ask (marked in its request payload): removed
    ``incognito_ttl_seconds`` after it ended, ahead of the ordinary
    retention. Mirrors the durable ``runs.incognito`` column."""
    origin_key: str | None = None
    """Idempotency key of the SUBMITTING tool call for agent children:
    a kernel tool re-executing after park/resume finds its
    already-submitted child by this key instead of spawning a second
    one. Summaries expose it only when set."""
    parent_task_id: str | None = None
    """Internal plan-task correlation copied from the durable replay body.
    It is never part of the public run summary; the store uses it only to
    project child progress onto the parent run's task card."""
    parent_task_attempt: int | None = None
    """Logical parent-task attempt; independent of run claim fencing."""
    status: RunStatus = RunStatus.QUEUED
    started_at: float | None = None
    finished_at: float | None = None
    finished_monotonic: float | None = None
    segment_count: int = 0
    current_segment_id: str | None = None
    current_segment_reason: str | None = None
    queued_since: float | None = None
    active_started_at: float | None = None
    active_seconds: float = 0.0
    waiting_seconds: float = 0.0
    queued_seconds: float = 0.0
    waiting_since: float | None = None
    """Unix time the run entered a waiting status (TTL anchor);
    cleared on resume."""
    park_in_flight: bool = False
    """Set between ``mark_waiting`` and the parking worker's unwind;
    a resume in that window defers its dispatch (``resume_requested``)
    to the unwind instead of racing the still-live worker."""
    resume_requested: bool = False
    """A resume arrived while ``park_in_flight``; the parking worker's
    unwind performs the deferred re-dispatch."""
    snapshot: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    cancel_reason: str | None = None
    """Reason of the latest cancel request on a running run; the cancelled
    terminal names it. Kept apart from ``events``, whose ring drops old
    entries of a long run."""
    event_seq: int = 0
    events: deque[dict[str, Any]] = field(default_factory=deque, repr=False)
    # Latest execution segment's OTel trace id. Captured OUTSIDE the
    # bounded SSE replay ring: long runs evict old events from `events`,
    # and the admin trace surface must survive that.
    trace_id: str | None = None
    subscribers: list[Queue] = field(default_factory=list, repr=False)

    @property
    def work_profile(self) -> str | None:
        """Internal work profile of a role child, read from the replay body.

        Summaries expose it only when set, so a client can tell a role run
        from a delegated child without opening it."""
        return (self.request_payload.get("body") or {}).get("work_profile") or None

    @property
    def cancel_requested(self) -> bool:
        """Whether a cancel is pending on a still-executing run.

        Only RUNNING can carry a pending cancel: queued and waiting runs
        cancel synchronously to a terminal state, and terminal runs must
        not re-expose the flag. Mirrors the Postgres store's persisted
        ``cancel_requested`` column so both backends summarize alike.
        """
        return self.status is RunStatus.RUNNING and self.cancel_event.is_set()


@dataclass(frozen=True)
class RunSubscription:
    """Live event subscription with buffered replay."""

    run_id: str
    queue: Queue
    replay: list[dict[str, Any]]
    store: "RunStore"

    def close(self) -> None:
        """Detach the subscriber queue from the run store."""
        self.store.unsubscribe(self.run_id, self.queue)


class RunHandle:
    """Worker-side handle for updating one run without exposing the store."""

    def __init__(
        self, store: "RunStore", run_id: str, cancel_event: threading.Event
    ) -> None:
        self._store = store
        self.run_id = run_id
        self.cancel_event = cancel_event
        self.parked = False
        """This execution parked its run via :meth:`wait` — the worker
        loop reads it to skip the auto-complete safety net and to
        finish the park handoff in its unwind."""

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        """Emit one structured event for this run.

        Deliberately unguarded. Revocation is enforced at the documented
        safepoints — around provider/search calls, at tool, knowledge,
        skill, child-run and segment boundaries, and around the final
        publication (see ``docs/architecture/agent-platform.md``). Event
        emission is not one of them: it produces no external effect that
        a check could still recall, and bytes already streamed to a
        viewer cannot be taken back. The read path re-authorizes every
        SSE frame independently, which is what actually stops delivery.
        """
        self._store.emit(self.run_id, event_type, payload or {})

    def effective_principal(self, fallback: Principal | None) -> Principal | None:
        """Return the actor persisted for this execution segment."""
        return self._store.execution_principal(self.run_id, fallback=fallback)

    @property
    def publication_fence_attempt(self) -> int | None:
        """Durable claim attempt owning artifact publication, if any.

        In-process execution cannot be reclaimed by a second worker and has
        no attempt fence.  The queue-worker handle overrides this value.
        """
        return None

    def total_elapsed_seconds(self) -> float:
        """Return durable wall time from first start through this segment.

        The store performs an internal lifecycle read and uses the same timing
        projection as the public summary. A worker must not call the public
        visibility-gated ``get`` path for its own scoped run.
        """

        return self._store.total_elapsed_seconds(self.run_id)

    def emit_answer(
        self,
        answer: str,
        *,
        reference_labels: "list[str] | None" = None,
        before_ready: Callable[[], None] | None = None,
    ) -> None:
        """Publish final Markdown through one explicit answer lifecycle.

        The answer is finished before it is published, so it goes out in
        a few byte-bounded deltas (:data:`ANSWER_DELTA_MAX_BYTES`), not one
        per word: every delta is a durable event with its own sequence
        allocation and execution-path check, and a word-by-word publication
        wrote thousands of them while the reader watched a complete answer
        appear over seconds. The deltas are ONE logical publication: the
        caller brackets this call with the final-publication safepoint, so a
        per-chunk check would re-assert the same fact for every delta after
        the answer is already produced.

        ``before_ready`` is the central answer publisher's commit hook.  It
        runs after the last delta and before ``answer.ready``; an exception
        emits ``answer.interrupted`` with the final byte offset and propagates
        so the run cannot claim a successful publication.
        """
        artifact_id = answer_artifact_id(self.run_id)
        publication_id = answer_publication_id(self.run_id)
        self.emit(
            ANSWER_STARTED_EVENT,
            {
                "artifact_id": artifact_id,
                "publication_id": publication_id,
                "status": "writing",
                # The labels the finished answer cites. They are known
                # here — the model turn and the citation validation are
                # both done before publication starts — and a surface
                # that has them can render citations from the FIRST
                # delta. Without them the streamed text carries plain
                # `[W1]` and the whole body is rewritten the moment the
                # answer settles, which reads as the message being
                # re-inserted.
                **(
                    {"reference_labels": list(reference_labels)}
                    if reference_labels
                    else {}
                ),
            },
        )
        offset = 0
        stage = "streaming"
        try:
            for chunk in iter_bounded_chunks(answer or "", ANSWER_DELTA_MAX_BYTES):
                self.emit(
                    "inqtrix.output_text.delta",
                    {
                        "artifact_id": artifact_id,
                        "publication_id": publication_id,
                        "offset": offset,
                        "delta": chunk,
                    },
                )
                offset += len(chunk.encode("utf-8"))
            if before_ready is not None:
                stage = "finalizing"
                before_ready()
            stage = "publishing_ready"
            self.emit(
                ANSWER_READY_EVENT,
                {
                    "artifact_id": artifact_id,
                    "publication_id": publication_id,
                    "bytes": offset,
                    "status": "ready",
                },
            )
        except BaseException:
            self.emit(
                ANSWER_INTERRUPTED_EVENT,
                {
                    "artifact_id": artifact_id,
                    "publication_id": publication_id,
                    "offset": offset,
                    "status": "interrupted",
                    "stage": stage,
                },
            )
            raise

    def complete(
        self, result: dict[str, Any], snapshot: dict[str, Any] | None = None
    ) -> None:
        """Mark the run completed and store its short-lived result payload."""
        self._store.complete(self.run_id, result, snapshot=snapshot)

    def fail(self, message: str, *, error_type: str = "server_error") -> None:
        """Mark the run failed."""
        self._store.fail(self.run_id, message, error_type=error_type)

    def cancel(self, reason: str = "cancelled") -> None:
        """Mark the run cancelled after the worker observed the cancel event."""
        self._store.mark_cancelled(self.run_id, reason=reason)

    def wait(self, status: "RunStatus | str") -> None:
        """Park the run in a waiting status (agent interrupt observed).

        The executing closure returns right after calling this; the
        store keeps the closure for :meth:`RunStore.resume_run`. When a
        cancel request is already pending, the run is cancelled instead
        of parked (the closure still returns normally) — a cancelled
        assignment must not sit in a waiting status until its TTL.
        """
        self._store.mark_waiting(self.run_id, status=status)
        self.parked = True
