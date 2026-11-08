"""The run-store port: the surface routers and services program against.

Both backends — the in-memory :class:`~inqtrix.runs.memory_store.RunStore`
(default) and :class:`~inqtrix.runs.postgres_store.PostgresRunStore`
(opt-in durability) — satisfy this Protocol structurally; the runs
router, :class:`~inqtrix.services.run_service.RunService`, and the
:class:`~inqtrix.server.container.AppContainer` cannot tell them
apart. The byte-level wire behaviour behind these methods is pinned by
``tests/contract/``.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from inqtrix.auth.principal import Principal, UserContext
    from inqtrix.auth.shares import ShareFacts
    from inqtrix.runs.records import RunWork


@dataclass(frozen=True)
class RunStoreMetrics:
    """A scrape-time snapshot of run-queue load (the ``/metrics`` source).

    Backend-neutral so the collector never branches on the store type:
    ``queued`` and ``active`` are the QUEUED and RUNNING counts (parked/
    waiting runs are excluded — they hold no slot, matching the per-user
    cap's accounting); ``capacity`` is the in-process concurrency ceiling
    for the memory backend, or ``None`` for the durable backend, where the
    worker fleet — not this API process — owns the execution slots.
    """

    queued: int
    active: int
    capacity: int | None


class RunSubscriptionPort(Protocol):
    """What the SSE route consumes from a subscription."""

    run_id: str
    replay: list[dict[str, Any]]
    queue: Any

    def close(self) -> None:
        """Detach the subscriber; idempotent."""
        ...


class RunStorePort(Protocol):
    """Public surface shared by every run-store backend.

    Each operation has two names: ``get`` for a synchronous job body on a thread,
    ``aget`` for a coroutine on the process loop.
    """

    def submit(
        self,
        *,
        question: str,
        stack_name: str,
        work: "RunWork",
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
        """Accept one run; returns the public summary (HTTP 202 body).

        ``mode`` and ``workflow_contract`` are the run's binding: the caller
        names both, the contract being the one the mode's algorithm carries
        on acceptance. The tree kwargs (``kind``/``parent_run_id``/
        ``root_run_id``) and the saved-session relation (``session_id``)
        default to the standard-run shape; summaries omit them entirely at
        defaults, so historical callers are untouched.
        """
        ...

    def get(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Public summary; raises ``RunNotFound`` (denial == absence)."""
        ...

    def list(
        self,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Visible summaries, newest first (unbounded, internal reads)."""
        ...

    def list_session_runs(
        self,
        session_id: str,
        *,
        visible_to: "UserContext | None" = None,
    ) -> list[dict[str, Any]]:
        """Visible summaries of ONE agent session, OLDEST first.

        The session-context builder reads a session's prior turns at
        run start; oldest-first matches transcript order. No
        share-grant union — an agent session is a per-owner surface, so
        only the owner's own visibility applies.
        """
        ...

    def session_owners(
        self, session_id: str
    ) -> set[tuple[str | None, uuid.UUID | None]]:
        """Recorded ``(tenant_id, user_id)`` owners for a session id.

        This raw ownership probe is intentionally not visibility-filtered.
        It is used only to prevent a deleted session registry row from being
        reclaimed by a different principal while historical runs remain.
        """
        ...

    def list_page(
        self,
        *,
        limit: int,
        after: tuple[float, str] | None = None,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One keyset page of visible summaries + next cursor.

        Newest-first over ``(created_at, run_id)``; ``after`` is the
        decoded cursor of the last row of the previous page. The HTTP
        list endpoint uses this so a long run history is not materialised
        whole on every poll.
        """
        ...

    def metrics_snapshot(self) -> RunStoreMetrics:
        """Cheap read of current queue load for the ``/metrics`` collector.

        Read-only and scrape-time only (no hot-path cost): the memory
        backend reads its in-process counters under the store lock; the
        durable backend runs a single grouped COUNT. See
        :class:`RunStoreMetrics` for the field semantics.
        """
        ...

    def binding_overview(
        self,
        *,
        mode: str | None,
        below: int | None,
        above: int | None,
        limit: int,
    ) -> dict[str, Any]:
        """Open runs by binding, for an operator raising or rolling back a contract.

        ``counts`` holds ``{mode, status, workflow_contract, runs}`` per open
        group, sorted. ``runs`` names the open runs bound below *below* or
        above *above*, oldest first and at most *limit* of them;
        ``runs_total`` counts them all and ``truncated`` says the list was
        cut. *mode* narrows both to one mode. Terminal runs never resume, so
        neither part holds them.
        """
        ...

    def cancel_outside_binding(
        self,
        *,
        mode: str,
        workflow_contracts: Mapping[str, int],
        actor_user_id: uuid.UUID | None,
    ) -> dict[str, int]:
        """Cancel the open runs of *mode* that *workflow_contracts* cannot run.

        Those are the runs bound above the contract *workflow_contracts* names
        for *mode*, or every open run of *mode* when it names none. A run bound
        below is left alone: with a stored checkpoint it ends at its next claim,
        without one the claim binds it to the current contract. A running run
        already asked to stop is skipped: neither asked, audited nor counted
        again. Each run is judged again under its lineage lock and cancelled
        with its children:
        the run itself with :data:`~inqtrix.runs.shared.ADMIN_CANCEL_REASON`,
        an active child with ``parent_cancelled``. Each cancel is audited as
        ``run.cancelled_by_admin`` with *actor_user_id* and reaches the
        owner's open views.

        Returns:
            ``cancelled``: runs that ended at once; ``cancel_requested``:
            running runs newly asked to stop, which end at their next step
            or at the next claim of a worker.
        """
        ...

    def result(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Stored result payload; raises ``RunNotFound`` when absent."""
        ...

    def cancel(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> dict[str, Any]:
        """Cancel queued immediately / request cancel of running."""
        ...

    def cancel_tree(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Cancel one run tree and return its exact affected run ids.

        The ids are an internal post-transition handoff for reconciling
        agent control rows. The public HTTP response remains the first tuple
        item, byte-compatible with :meth:`cancel`.
        """
        ...

    def authorized_control_write(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        control_write: Any,
    ) -> Any:
        """Run one agent-control mutation under live ``edit`` authority.

        ``control_write(transaction, cancel_child)`` is invoked only after
        the canonical run root, active caller, and accepted direct share have
        been locked. Durable backends pass their database transaction;
        in-memory backends pass ``None`` while holding the corresponding
        run/identity locks. ``cancel_child`` cancels one direct child subtree
        inside that same boundary and returns the child's resulting status.

        This is the control-table counterpart of ``resume_run``'s
        ``control_write`` seam: a revoke either commits before the callback
        and the write is denied, or waits until the complete control mutation
        (including an optional child cancellation) has committed.
        """
        ...

    def bind_checkpoint_cleanup(self, cleanup: Callable[[str, str], None]) -> None:
        """Remove external execution caches before authorized run/retention deletion."""
        ...

    def sweep(self) -> None:
        """Run the cleanup pass every access runs, without waiting for one.

        Retention (the short incognito window included), the waiting TTL and
        the stuck-run failsafe; the API process calls it on a clock so they
        hold while nobody reads the store.
        """
        ...

    def delete(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        requester_user_id: uuid.UUID | None = None,
    ) -> None:
        """Permanently remove one terminal run; owner-only.

        The gate is creator identity, not share visibility (deletion is
        stronger than cancel). ``RunNotFound`` for unknown, non-owner, or
        cross-namespace ids (indistinct denial); ``RunActive`` for a
        non-terminal run. Not idempotent — a repeat delete raises
        ``RunNotFound``.
        """
        ...

    def subscribe(
        self,
        run_id: str,
        *,
        workspace_id: str | None = None,
        visible_to: "UserContext | None" = None,
        stream: bool = True,
    ) -> RunSubscriptionPort:
        """Event subscription with stored replay plus live tail.

        ``stream=False`` requests a one-shot replay read (the JSON
        polling fallback): identical visibility semantics, but no live
        tail — implementations must not register the subscriber or
        count it as a stream viewer.
        """
        ...

    def owner_user_id(self, run_id: str) -> uuid.UUID | None:
        """The run's ``created_by_user_id`` regardless of visibility.

        The share layer's owner resolver: authorization happens in the
        ShareService against this fact, so the lookup itself must not
        be visibility-gated. ``None`` for unknown runs AND for legacy
        pre-scoping rows (no recorded creator) alike — both are
        unshareable.
        """
        ...

    def share_facts(self, run_ids: Sequence[str]) -> dict[str, ShareFacts]:
        """Creator and question of each listed run regardless of
        visibility, in one read: the share layer's batch resolver behind
        the owner listing. Unknown runs are absent."""
        ...

    def trace_id(self, run_id: str) -> str | None:
        """Hex trace id of the run's LAST execution segment, or None.

        Reads the durable ``inqtrix.run.trace`` event (retries emit it
        again — recency wins). Deliberately NOT visibility-gated: the
        only caller is the instance-admin trace surface, whose
        authorization happens in ``require_instance_admin`` before the
        store is touched (``owner_user_id`` precedent). ``None`` for
        unknown runs and for runs executed with tracing off alike.
        """
        ...

    def execution_request_body(self, run_id: str) -> dict[str, Any]:
        """Return the persisted execution body for internal validation.

        The body is detached from storage and is never part of the public run
        summary. Control and worker paths use it to restore the one immutable
        dependency boundary admitted with the run.
        """
        ...

    def source_request_body(
        self, run_id: str, *, visible_to: Any, workspace_id: str | None = None,
    ) -> dict[str, Any]:
        """Detached source provenance for the owner, never a result-share grant."""
        ...

    def source_run_catalogue(
        self, session_id: str, *, visible_to: Any, workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Owner-only conversation metadata without results or request bodies.

        Internal role runs are no part of a conversation: they hold nothing
        but material another run already read, so they are left out.
        """
        ...

    def source_origins_visible(
        self, run_ids: list[str], *, visible_to: Any, owner_only: bool = False,
    ) -> bool:
        """Check source existence and current grants without loading bodies."""
        ...

    def execution_principal(
        self,
        run_id: str,
        *,
        fallback: "Principal | None" = None,
    ) -> "Principal | None":
        """Reconstruct the effective actor persisted for the run segment."""
        ...

    def total_elapsed_seconds(self, run_id: str) -> float:
        """Return durable wall time for an admitted worker-owned run.

        This is an internal lifecycle read and deliberately does not use the
        public owner/share visibility projection. Missing rows still raise
        ``RunNotFound``.
        """
        ...

    def emit(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        fence_attempt: int | None = None,
    ) -> None:
        """Append one event (worker-handle write path).

        Args:
            fence_attempt: Durable claim-attempt fence (see
                :meth:`mark_waiting`); in-process backends ignore it.
        """
        ...

    def complete(
        self,
        run_id: str,
        result: dict[str, Any],
        *,
        snapshot: dict[str, Any] | None = None,
        fence_attempt: int | None = None,
    ) -> None:
        """Store the result and mark completed (absorbing).

        Args:
            fence_attempt: Durable claim-attempt fence (see
                :meth:`mark_waiting`); in-process backends ignore it.
        """
        ...

    def fail(
        self,
        run_id: str,
        message: str,
        *,
        error_type: str = "server_error",
        fence_attempt: int | None = None,
    ) -> None:
        """Mark failed with a sanitized error (absorbing).

        Args:
            fence_attempt: Durable claim-attempt fence (see
                :meth:`mark_waiting`); in-process backends ignore it.
        """
        ...

    def mark_cancelled(
        self, run_id: str, *, reason: str, fence_attempt: int | None = None
    ) -> None:
        """Mark cancelled after the executor observed the cancel.

        A run that was asked to stop ends with the reason of its latest
        request; *reason* names the end only of a run that stopped on its
        own. Its live children end with it (``parent_cancelled``).

        Args:
            fence_attempt: Durable claim-attempt fence (see
                :meth:`mark_waiting`); in-process backends ignore it.
        """
        ...

    def mark_waiting(
        self, run_id: str, *, status: Any, fence_attempt: int | None = None
    ) -> None:
        """Park a RUNNING run in a waiting status (agent interrupt).

        Non-terminal: the run keeps its work/payload for a later
        :meth:`resume_run`; sweeps exclude waiting runs, only the
        waiting TTL may auto-cancel them (visible ``approval_timeout``).

        Args:
            fence_attempt: The claim attempt that owns this park (the
                durable run row's ``attempt`` counter, matched together
                with its claiming worker). The durable/queue backend
                records it so a reclaimed zombie cannot park a run the
                live attempt owns;
                the worker always supplies it. In-process backends have no
                zombie reclaim and ignore it, so it is optional — but it is
                part of the contract because the worker calls every store
                through this port.
        """
        ...

    def resume_run(
        self,
        run_id: str,
        *,
        actor_user_id: uuid.UUID | None = None,
        execution_scopes: frozenset[str] = frozenset(),
        control_write: Any = None,
    ) -> dict[str, Any]:
        """Move a waiting run back to ``queued`` and re-dispatch it.

        Raises ``RunNotFound`` for unknown ids and ``RunActive`` when
        the run is not waiting (or nothing is retained to resume).
        Returns the public summary.
        """
        ...

    def children(self, run_id: str) -> list[dict[str, Any]]:
        """Direct-children summaries, newest first.

        NOT visibility-gated — the caller must have resolved the
        PARENT via :meth:`get` first; children inherit that access.
        """
        ...

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
        """Persist an already-terminal run imported from a project file.

        Stores a completed report snapshot directly (no execution), scoped to
        the caller, so a loaded project's reports survive a reload and follow
        the user. ``source_run_id`` is an owner-scoped idempotency key; a new
        row always gets a server-generated ``run_id``. ``created_at`` keeps the
        report's original date; the durable-retention clock starts at import
        time, hence no ``finished_at`` parameter. The run is bound to
        ``mode`` and to ``IMPORTED_RUN_CONTRACT``. Returns the public summary.
        """
        ...

    # The same operations for a coroutine on the process loop: same arguments, same
    # results. The sync names above are for job bodies on threads.

    async def asubmit(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def aget(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def alist(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...

    async def alist_session_runs(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...

    async def asession_owners(self, *args: Any, **kwargs: Any) -> set[tuple[str | None, uuid.UUID | None]]: ...

    async def alist_page(self, *args: Any, **kwargs: Any) -> tuple[list[dict[str, Any]], str | None]: ...

    async def ametrics_snapshot(self, *args: Any, **kwargs: Any) -> RunStoreMetrics: ...

    async def abinding_overview(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def acancel_outside_binding(self, *args: Any, **kwargs: Any) -> dict[str, int]: ...

    async def aresult(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def acancel(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def acancel_tree(self, *args: Any, **kwargs: Any) -> tuple[dict[str, Any], tuple[str, ...]]: ...

    async def aauthorized_control_write(self, *args: Any, **kwargs: Any) -> Any: ...

    async def asweep(self, *args: Any, **kwargs: Any) -> None: ...

    async def adelete(self, *args: Any, **kwargs: Any) -> None: ...

    async def asubscribe(self, *args: Any, **kwargs: Any) -> RunSubscriptionPort: ...

    async def aowner_user_id(self, *args: Any, **kwargs: Any) -> uuid.UUID | None: ...

    async def ashare_facts(self, *args: Any, **kwargs: Any) -> dict[str, ShareFacts]: ...

    async def atrace_id(self, *args: Any, **kwargs: Any) -> str | None: ...

    async def aexecution_request_body(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def asource_request_body(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def asource_run_catalogue(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...

    async def asource_origins_visible(self, *args: Any, **kwargs: Any) -> bool: ...

    async def aexecution_principal(self, *args: Any, **kwargs: Any) -> "Principal | None": ...

    async def atotal_elapsed_seconds(self, *args: Any, **kwargs: Any) -> float: ...

    async def aemit(self, *args: Any, **kwargs: Any) -> None: ...

    async def acomplete(self, *args: Any, **kwargs: Any) -> None: ...

    async def afail(self, *args: Any, **kwargs: Any) -> None: ...

    async def amark_cancelled(self, *args: Any, **kwargs: Any) -> None: ...

    async def amark_waiting(self, *args: Any, **kwargs: Any) -> None: ...

    async def aresume_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def achildren(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]: ...

    async def aimport_completed_run(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...

    async def aclose(self) -> None:
        """Called once when the process stops serving."""
        ...
