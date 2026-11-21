"""Contracts of the agent control store (stored plans, approvals,
clarifications, artifacts).

The control tables are the SOURCE OF TRUTH for everything an agent run
negotiates with its user: human-in-the-loop approvals and
clarifications, and the artifact documents the canvas renders. Plan rows are
the stored plans of older mission runs; no run writes one.
Run events are signals only — clients reconcile via the REST reads over this
store.

Two implementations behind one port:
:class:`~inqtrix.agents.control_memory.MemoryAgentControlStore` (offline/test)
and
:class:`~inqtrix.storage.agent_control_postgres.PostgresAgentControlStore`.
Control rows remain principal-agnostic, but user mutations are composed with
the run store's live authorization callback. The router's first visibility
check is only an early indistinct-404 gate; the callback locks the canonical
run/share boundary again in the same transaction as the control write.

Retention: all control rows are children of their run row (``ON DELETE
CASCADE`` in Postgres, mirrored in memory) — the durable run retention window
is the single retention authority. A session-scoped artifact outlives the
turn that created it because every agent write re-anchors it onto the
writing run via :meth:`AgentControlStore.upsert_artifact`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from inqtrix.agents.context_archive import (
    ARCHIVE_READ_WINDOW_CHARS, ArchiveCatalogueEntry, ArchiveWindow,
)

PLAN_STATUSES = ("draft", "proposed", "approved", "rejected", "superseded")
"""The status values a stored plan VERSION holds. ``superseded`` marks a
version a newer one replaced (a user's edit or a replan); at most one version
per run is neither superseded nor rejected."""

APPROVAL_KINDS = ("discovery", "plan", "patch", "replan", "tool")
"""What a run asked permission for: the values an approval row may hold.

``tool`` is the kernel's per-call policy gate: the payload carries
``actions: [{tool, args, summary}]`` with the web query VERBATIM in
``args`` — the args ARE the approval content the user reviews. Edit
decisions are allowed for exactly one action (the HITL
``edited_action`` contract). The other kinds are stored rows of runs of the
retired mission workflow (see :data:`DECIDABLE_APPROVAL_KINDS`)."""

DECIDABLE_APPROVAL_KINDS = frozenset({"tool"})
"""The approval kinds a run still creates and a user still decides. A stored
approval of another kind of :data:`APPROVAL_KINDS` is listed and read as it
was stored, never created anew or decided."""

APPROVAL_STATUSES = ("pending", "approved", "rejected", "edited")
"""``edited`` is the approve-with-changes outcome: the decision carried
revised actions (a stored mission approval: a revised plan)."""

APPROVAL_DECISIONS = ("approve", "reject", "edit")

APPROVAL_STATUS_BY_DECISION = {
    "approve": "approved",
    "reject": "rejected",
    "edit": "edited",
}
"""THE one decision-verb -> approval-status mapping; the service (event
payloads) and both stores (row writes) consume it, so the emitted status
can never desynchronize from the stored one."""

CLARIFICATION_STATUSES = ("pending", "answered")
"""A terminal run deliberately leaves its unanswered rounds ``pending``:
the row is truthful (nothing was answered) and inert — answering runs
through the resume CAS, which a terminal run refuses. The run status,
not the clarification row, gates the answer affordance."""

ARTIFACT_KINDS = (
    "memo",
    "evidence_bundle",
    "critic_report",
    "editor_patch",
    "answer",
    "deliverable",
    "context_archive",
)
"""``answer`` is the run-local chat-form deliverable: rendered
inline in the agent timeline instead of a canvas. Added
to the DB CHECK by migration 0039 together with the structured
clarification columns in one migration.

``deliverable`` is a kernel canvas document (``write_canvas``):
session-scoped and (session_id, artifact_id)-addressed like the memo,
but a session may hold SEVERAL — the artifact registry in the session
context keeps them distinguishable. Its ``payload.deliverable_kind``
carries the format hint (memo|email|talking_points|generic). Added to
the DB CHECK by migration 0040.

``context_archive`` is the kernel's run-local compaction archive: evicted
transcript history and offloaded bulk tool results, appended in order.
Session-less, one per run (``art_<run12>_ctx``), readable by the model via
``read_canvas`` — the durable half of ledger-grounded compaction, so
compacting can never destroy retrievable context or break a citation.
Added to the DB CHECK by migration 0050."""

TERMINAL_APPROVAL_SETTLE_NOTE = (
    "Der Lauf endete, bevor die Freigabe entschieden wurde."
)
"""System note both stores stamp on approvals a terminal run left
pending — held once here so the twins can never drift apart."""

ARTIFACT_STATUSES = ("writing", "ready")
"""``writing`` locks the artifact against user edits: the canvas is
read-only while the agent streams into it."""

SESSION_SINGLETON_ARTIFACT_KINDS = frozenset({"memo"})
"""Session-scoped artifact kinds addressed by ``(session_id, kind)``.

Kernel ``deliverable`` documents are deliberately absent: one session may
hold several of them, so they are always addressed by ``artifact_id``.
"""

RUN_SINGLETON_ARTIFACT_KINDS = frozenset(
    {
        "evidence_bundle",
        "critic_report",
        "editor_patch",
        "answer",
        "context_archive",
    }
)
"""Session-less diagnostic kinds addressed by ``(run_id, kind)``.

``deliverable`` is deliberately absent for the same reason as in the
session-scoped contract: one run may create several canvases.
"""

TASK_TOOL_KINDS = (
    "web_research",
    "web_instant",
    "rag_query",
    "file_analysis",
    "synthesis",
)

TASK_STATUSES = (
    "pending",
    "running",
    "cancel_requested",
    "cancelled",
    "completed",
    "failed",
    "insufficient_evidence",
    "skipped",
)
TASK_RESULT_SUMMARY_MAX_WORDS = 300


class PlanNotFound(KeyError):
    """Raised when a run has no plan (yet) or the version is unknown."""


class ApprovalNotFound(KeyError):
    """Raised when an approval id is unknown for the run (HTTP 404)."""


class ApprovalAlreadyDecided(Exception):
    """Raised when a decision hits an approval that is no longer pending.

    Carries the stored record so the caller can distinguish a replay of the
    SAME decision (idempotent 200) from a conflicting one (409).
    """

    def __init__(self, approval: "ApprovalRecord") -> None:
        super().__init__(f"approval {approval.approval_id} already decided")
        self.approval = approval


class ClarificationNotFound(KeyError):
    """Raised when a clarification id is unknown for the run (HTTP 404)."""


class ClarificationAlreadyAnswered(Exception):
    """Raised when an answer hits a clarification that is not pending.

    Carries the stored record for the replay-idempotency check (same
    answer -> 200, different answer -> 409).
    """

    def __init__(self, clarification: "ClarificationRecord") -> None:
        super().__init__(
            f"clarification {clarification.clarification_id} already answered"
        )
        self.clarification = clarification


class ArtifactNotFound(KeyError):
    """Raised when an artifact id (or requested revision) is unknown."""


class ArtifactLocked(Exception):
    """User edit rejected because the agent is writing (409)."""


class ContextArchiveImmutable(Exception):
    """Captured archive content and metadata cannot be edited (409)."""


class ArtifactRevisionConflict(Exception):
    """Optimistic-concurrency miss on a user edit (409).

    Attributes:
        current_revision: The revision the row actually holds — returned to
            the client so it can rebase instead of blind-retrying.
    """

    def __init__(self, current_revision: int) -> None:
        super().__init__(f"artifact is at revision {current_revision}")
        self.current_revision = current_revision


class ArtifactPublicationFenced(Exception):
    """A durable answer write belongs to a superseded run attempt.

    Artifact revision CAS protects the document row; this separate fence
    binds the write to the durable run claim.  It prevents a reclaimed worker
    from advancing the answer artifact after a newer attempt owns the run.
    """

    def __init__(
        self,
        *,
        expected_attempt: int,
        current_attempt: int | None,
        status: str,
    ) -> None:
        self.expected_attempt = expected_attempt
        self.current_attempt = current_attempt
        self.status = status
        super().__init__(
            "answer publication attempt was fenced "
            f"(expected={expected_attempt}, current={current_attempt}, "
            f"status={status})"
        )


@dataclass(frozen=True)
class PlanTaskRecord:
    """One task row of a stored plan version of a mission run (read only).

    No run writes plan rows; the attributes say what a stored row holds.

    Attributes:
        task_id: Stable id (``task-...``), unique within the plan version;
            ``depends_on`` references these ids.
        plan_id: Owning plan-version row.
        run_id: Denormalized run id (query convenience; always equals the
            plan's run).
        ordinal: Display/served order (0-based, stable).
        title: User-facing task label (German, like all agent surface text).
        tool_kind: One of :data:`TASK_TOOL_KINDS`.
        objective: What the task had to produce.
        queries: Concrete search/ask strings the task started from.
        gap_ids: The discovery gaps the task covered (empty for synthesis).
        depends_on: Task ids that had to complete before it.
        budget: ``{}``, or the per-task caps a historic row stored; no code
            reads them.
        params: Tool tuning (``profile``/``recency``/
            ``collection_ids``).
        expected_output: Short description of the deliverable shape.
        status: One of :data:`TASK_STATUSES`, the task's last recorded
            execution state. ``cancel_requested`` is non-terminal: a
            synchronous provider call was still unwinding when it was
            recorded.
        child_run_id: The spawned child run of a ``web_research`` task.
        result_summary: Compact result (<= 300 words).
        result_payload: The fields the scalar columns do not hold
            (evidence, claims, usage, complete answer Markdown and failure
            detail); the kernel builds its reading records of an older
            mission from them. Not exposed over HTTP.
    """

    task_id: str
    plan_id: str
    run_id: str
    ordinal: int
    title: str
    tool_kind: str
    objective: str = ""
    queries: tuple[str, ...] = ()
    gap_ids: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    expected_output: str = ""
    status: str = "pending"
    child_run_id: str | None = None
    result_summary: str = ""
    result_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PlanRecord:
    """One stored plan VERSION of a mission run (read only).

    Attributes:
        plan_id: Row id (``plan-...``).
        run_id: The run this plan belongs to.
        version: 1-based, unique per run; a later version (a user's edit or
            a replan) superseded the previous one.
        status: One of :data:`PLAN_STATUSES`.
        created_by: ``agent`` (planner output) or ``user`` (edit decision).
        summary_markdown: The plan's one-paragraph intent.
        assumptions: Non-blocking open points the plan proceeded on.
        success_criteria: Measurable criteria (German) the critic checked
            the memo against.
        reason: Why this version exists — empty for the initial plan,
            ``critic_research``/``replan`` for an agent revision, or
            ``user_edit``. The unconstrained string accepts every persisted
            reason value.
        created_at: Unix seconds.
    """

    plan_id: str
    run_id: str
    version: int
    status: str
    created_by: str
    summary_markdown: str = ""
    assumptions: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    reason: str = ""
    created_at: float = 0.0


@dataclass(frozen=True)
class ApprovalRecord:
    """One human-in-the-loop approval request of a run.

    Attributes:
        approval_id: Row id (``apr_...``).
        run_id: The asking run.
        kind: One of :data:`APPROVAL_KINDS`.
        status: One of :data:`APPROVAL_STATUSES`.
        subject_type/subject_id: What is being approved (empty for a tool
            gate; ``plan`` + plan_id on a stored plan/replan approval).
        payload: Kind-specific request context shown to the user (for
            ``tool``: the actions; a stored mission approval keeps what its
            gate showed, for ``plan``/``replan`` ``plan_id`` and
            ``plan_version``, for ``replan`` additionally ``reason`` and
            ``new_tasks``).
        decision: The verb that resolved it (``approve``/``reject``/
            ``edit``), empty while pending.
        decision_payload: The edited actions for ``edit`` decisions;
            decision-scoped keys may ride it (``approval_scope: "run"`` on
            tool gates — a plain approve-once stays ``{}`` so its replay
            identity is unchanged; a stored plan decision also holds the
            composed ``report_guidance`` and the ``report_requirement``
            parts it was composed from). Part of replay identity: a retry
            with a different payload conflicts. Every reader sees
            :func:`public_decision_payload` instead.
        note: Optional free-text the decider attached.
        decided_by_user_id: Verified subject that decided.
        interrupt_key: Checkpoint correlation key for resuming the
            interrupted run (opaque to this layer).
        created_at/decided_at: Unix seconds.
    """

    approval_id: str
    run_id: str
    kind: str
    status: str = "pending"
    subject_type: str = ""
    subject_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    decision: str = ""
    decision_payload: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    decided_by_user_id: uuid.UUID | None = None
    interrupt_key: str = ""
    created_at: float = 0.0
    decided_at: float | None = None


def check_creatable_approval(approval: ApprovalRecord) -> None:
    """Refuse an approval of a kind no run asks for any more.

    Both stores call this before they write a new approval row.

    Raises:
        ValueError: *approval* is not of a :data:`DECIDABLE_APPROVAL_KINDS`
            kind.
    """
    if approval.kind not in DECIDABLE_APPROVAL_KINDS:
        raise ValueError(
            f"An approval of kind {approval.kind} is not created any more; a run "
            "asks only for tool approvals."
        )


def public_decision_payload(approval: ApprovalRecord) -> dict[str, Any]:
    """The decision payload as every reader sees it.

    A stored plan-gate decision of a mission run holds the COMPOSED
    requirement under ``report_guidance``, the text the mission rendered into
    its writing prompts. That text carries the full bodies of the attached
    library rules, and an approval outlives the rules it was decided
    under: handing it out would keep a deleted, edited or no longer
    shared rule readable to everyone who can read the run or its
    conversation. Readers get what the user chose instead — the parts
    and the typed text under ``report_requirement``. The composed text is
    never handed out, also not by a decision that stored no parts.
    """
    payload = dict(approval.decision_payload)
    payload.pop("report_guidance", None)
    return payload


@dataclass(frozen=True)
class ClarificationRecord:
    """One clarification GATE ROUND the run asked its user.

    A round carries 1-3 structured questions; the legacy single-question
    columns stay valid so a whole-round free-text answer through the
    composer keeps working unchanged.

    Attributes:
        clarification_id: Row id (``clr_...``).
        run_id: The asking run.
        question: Joined prompt text of the round (German) — the legacy
            single-text reading and the free-text answer target.
        options: LEGACY single-question options, each ``{"id": ..,
            "label": ..}`` (mirrors ``questions[0]["options"]`` when the
            round has exactly one question, else empty). Kept so older
            readers and the plain ``option_id`` answer path survive.
        questions: Structured round payload, each ``{"id", "prompt",
            "options": [{"id", "label", "description"}], "multi_select"}``
            (sanitized, deterministic ids — see
            :func:`inqtrix.agents.clarification.sanitize_questions`).
            Empty for legacy rounds.
        answers: Structured answers by question id, each
            ``{"option_ids": [..], "text": ".."}``; empty when the round
            was answered with whole-round free text or a legacy option.
        default_assumption: The agent's proposed default assumption,
            shown to the user up front. A round has no expiry and no
            dismiss path: it stays pending until the user answers or the
            run ends.
        status: One of :data:`CLARIFICATION_STATUSES`.
        answer: Whole-round free-text answer (empty when structured
            ``answers`` or a legacy option was given).
        option_id: Picked legacy option id (empty otherwise).
        answered_by_user_id: Verified subject that answered.
        created_at/answered_at: Unix seconds.
    """

    clarification_id: str
    run_id: str
    question: str
    options: tuple[dict[str, Any], ...] = ()
    questions: tuple[dict[str, Any], ...] = ()
    answers: dict[str, Any] = field(default_factory=dict)
    default_assumption: str = ""
    status: str = "pending"
    answer: str = ""
    option_id: str = ""
    answered_by_user_id: uuid.UUID | None = None
    created_at: float = 0.0
    answered_at: float | None = None


@dataclass(frozen=True)
class ArtifactRecord:
    """One artifact document of a run (the canvas content's source of truth).

    Attributes:
        artifact_id: Row id (``art_...``).
        run_id: The run that currently anchors the artifact. Every agent
            write re-anchors a session-scoped artifact onto the writing run,
            so its retention follows the LATEST run that wrote it.
        session_id: Agent-desk session for cross-run artifacts (canvas
            documents; the memo of a stored mission run); ``None`` for
            run-local artifacts (answer, evidence bundle, context archive).
        kind: One of :data:`ARTIFACT_KINDS`.
        title: Display title.
        status: ``writing`` (agent streams, user edits rejected with 409)
            or ``ready``.
        revision: 1-based optimistic-concurrency counter; every content
            write increments it and appends a revision row.
        updated_by: ``agent`` or ``user`` — who wrote the current revision.
        content_markdown: The document body (markdown with ``[K#]``/``[W#]``
            citation labels).
        payload: Kind-specific structured data (e.g. a canvas's
            ``deliverable_kind``, a stored critic report's JSON).
        refs: Evidence references backing the content (citation label ->
            source descriptor).
        created_at/updated_at: Unix seconds.
        lines_added/lines_removed: Line delta of THIS write against the
            previous revision, computed once at the write and
            carried only on the record a write RETURNS — reads always
            yield ``None`` (both stores, deliberately symmetric).
            ``None`` also means "not counted" (size guard, or a write
            path that does not count).
    """

    artifact_id: str
    run_id: str
    kind: str
    session_id: str | None = None
    title: str = ""
    status: str = "ready"
    revision: int = 1
    updated_by: str = "agent"
    content_markdown: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    refs: tuple[dict[str, Any], ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0
    lines_added: int | None = None
    lines_removed: int | None = None


def artifact_event_payload(record: ArtifactRecord) -> dict[str, Any]:
    """Uniform ``artifact.created/updated`` payload of an agent write.

    ONE builder for every agent-side emitter (kernel write_canvas,
    deep-revision batch, answer publisher) so the
    durable payloads never drift apart again: they all carry title and
    updated_by, plus the write's line delta when the store counted it —
    absent fields mean "honestly not counted", never zero. The user PUT
    keeps its own deliberately kind-less payload (it must not chip).
    """
    payload: dict[str, Any] = {
        "artifact_id": record.artifact_id,
        "kind": record.kind,
        "revision": record.revision,
        "title": record.title,
        "status": record.status,
        "updated_by": record.updated_by,
        "from_revision": record.revision - 1,
    }
    if record.lines_added is not None and record.lines_removed is not None:
        payload["lines_added"] = record.lines_added
        payload["lines_removed"] = record.lines_removed
    return payload


@dataclass(frozen=True)
class ArtifactRevisionRecord:
    """One append-only content revision of an artifact (diff history).

    Attributes:
        artifact_id: Owning artifact.
        revision: The revision number this row snapshots.
        created_by: ``agent`` or ``user``.
        content_markdown: Full body at that revision (revision diffs are
            computed client-side from two full bodies).
        created_at: Unix seconds.
        status: The artifact status the write carried: ``writing`` for a
            write the agent had not finished (the empty answer artifact a
            publication starts with; in a stored mission run, each memo
            flush), ``ready`` for a finished write or a user edit.
            ``writing`` snapshots stay in the history for diffs and recovery
            but are not versions a reader picks from.
    """

    artifact_id: str
    revision: int
    created_by: str
    content_markdown: str = ""
    created_at: float = 0.0
    status: str = "ready"


@dataclass(frozen=True)
class ArtifactBatchRevision:
    """One content-only artifact update inside an atomic revision batch.

    Attributes:
        artifact_id: Existing session artifact to revise.
        expected_revision: Exact current revision required by CAS.
        content_markdown: Complete replacement body. Metadata and evidence
            references are preserved from the stored artifact.
    """

    artifact_id: str
    expected_revision: int
    content_markdown: str


@runtime_checkable
class AgentControlStore(Protocol):
    """Persistence port for agent run control data.

    Writes that resolve an interrupt (approval decision, clarification
    answer) do NOT resume the run here — the service composes them with the
    run store so the durable backend can put the decision write and the
    ``waiting -> queued`` flip into one transaction.
    """

    # -- plans --------------------------------------------------------- #

    async def get_plan(
        self, run_id: str, *, version: int | None = None
    ) -> tuple[PlanRecord, list[PlanTaskRecord]]:
        """One stored plan version of a mission run (latest when ``version``
        is None) with its tasks ordered by ordinal; :class:`PlanNotFound`
        when absent. Plans are read only: the kernel reads the readings of
        an older mission's plan, and no run writes one."""
        ...

    # -- approvals ------------------------------------------------------ #

    async def create_approval(self, approval: ApprovalRecord) -> ApprovalRecord:
        """Write one new approval; ``ValueError`` for a kind outside
        :data:`DECIDABLE_APPROVAL_KINDS` (:func:`check_creatable_approval`)."""
        ...

    async def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        """All approvals of the run, newest-first."""
        ...

    async def get_approval(self, run_id: str, approval_id: str) -> ApprovalRecord:
        """One approval; :class:`ApprovalNotFound` when absent (the run id
        must match — approvals are not addressable across runs)."""
        ...

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
    ) -> tuple[ApprovalRecord, dict[str, Any]]:
        """Record the decision AND resume the waiting run — atomically per
        backend.

        *resume* is an AWAITABLE callable ``await resume(control_write)
        -> run summary``
        supplied by the service (it wraps ``run_store.resume_run``): the
        memory store hands ``resume`` a synchronous writer executed under the
        shared authority/run lock; the Postgres store hands it a coroutine
        writer executed in the run transaction. Both perform the decision CAS
        INSIDE the run store's ``waiting -> queued`` boundary, so no observer
        can see one without the other.

        Raises:
            ApprovalNotFound: Unknown approval for the run.
            ApprovalAlreadyDecided: The approval is not pending (carries
                the stored record for the replay-idempotency check).
            Whatever *resume* raises when the run cannot resume (the
                decision is rolled back in that case).
        """
        ...

    # -- clarifications -------------------------------------------------- #

    async def create_clarification(
        self, clarification: ClarificationRecord
    ) -> ClarificationRecord: ...

    async def list_clarifications(self, run_id: str) -> list[ClarificationRecord]:
        """All clarifications of the run, newest-first."""
        ...

    async def get_clarification(
        self, run_id: str, clarification_id: str
    ) -> ClarificationRecord: ...

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
        """Record the answer AND resume the waiting run.

        Same composition contract as
        :meth:`decide_approval_and_resume` — exactly one of *answer* /
        *option_id* / *answers* is non-empty (the service validates
        that, including the per-question shape of *answers*).

        Raises:
            ClarificationNotFound: Unknown clarification for the run.
            ClarificationAlreadyAnswered: Not pending (carries the stored
                record for the replay check).
            Whatever *resume* raises (the answer is rolled back then).
        """
        ...

    # -- artifacts ------------------------------------------------------- #

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
        """Create or advance an artifact through the agent write path.

        Upsert key: explicit ``artifact_id`` when given, else
        ``(session_id, kind)`` for session-scoped kinds, else
        ``(run_id, kind)``. Every content change bumps ``revision`` and
        appends a revision row; the run anchor moves to *run_id*.

        Args:
            expected_revision: Optimistic-concurrency guard for the
                agent's write path. ``None`` (default) advances
                unconditionally — correct for write-once per-run kinds
                (evidence bundles, critic reports) the user never edits.
                An int makes the write conditional against an EXISTING row:
                if its revision differs (someone wrote since the caller
                read it), :class:`ArtifactRevisionConflict` is raised
                (carrying the current revision) instead of clobbering.
                The sentinel ``0`` means "I expect to CREATE" — a
                concurrently-inserted row is a conflict, not a silent
                advance (closes the fresh-session insert race). When no row
                exists the value is irrelevant: the insert proceeds.
            expected_run_attempt: Optional durable worker claim fence.  When
                set, the artifact mutation and the assertion that the run is
                still ``running`` under this exact attempt occur in one
                transaction.  In-memory execution has no reclaiming workers
                and therefore leaves it ``None``.
        """
        ...

    async def put_context_archive(
        self, *, run_id: str, title: str, text: str,
        transcript: dict[str, Any] | None = None,
    ) -> list[ArtifactRecord]:
        """Atomically store exact text parts, or replay their identical rows.

        Captured parts are immutable and do not duplicate their bodies in the
        mutable-artifact revision history. Their one revision is the original
        row. Every part commits under the same live run-write authority.
        """
        ...

    async def read_context_archive(
        self, run_id: str, artifact_id: str, *, offset: int | None = None,
        content_hash: str = "", max_chars: int = ARCHIVE_READ_WINDOW_CHARS,
        include_private: bool = False,
    ) -> ArchiveWindow:
        """Read exact code-point coordinates within the run's archive only.

        A direct part id starts at that part when offset is omitted. An
        explicit continuation requires the fingerprint from a prior read.
        Metadata establishes a complete group; only overlapping bodies need
        loading. Missing, changed or corrupt data is an error, never a cut.
        """
        ...

    async def list_context_archive_sources(
        self, run_id: str, *, include_private: bool = False,
    ) -> list[ArchiveCatalogueEntry]:
        """One consistent metadata snapshot of logical run-archive sources.

        Bodies stay out. Grouped sources occur once; legacy rendered sections
        retain their separate identities and explicitly lower fidelity.
        """
        ...

    async def list_source_artifacts(self, run_ids: list[str]) -> list[ArtifactRecord]:
        """Metadata-only source inventory for already owner-authorized run ids."""
        ...

    async def list_source_inputs(self, run_ids: list[str]) -> list[dict[str, Any]]:
        """Decision identities/timestamps only, never the user-authored bodies."""
        ...

    async def get_session_artifact_by_id(
        self, session_id: str, artifact_id: str
    ) -> ArtifactRecord:
        """Return one full session artifact addressed by id.

        This is the cross-run read used by the kernel's ``read_canvas``
        tool. Both keys are required so an id from another session cannot be
        used as a confused-deputy target.

        Raises:
            ArtifactNotFound: The id is unknown or belongs to another
                session.
        """
        ...

    async def revise_session_artifacts_atomically(
        self,
        *,
        run_id: str,
        session_id: str | None,
        revisions: list[ArtifactBatchRevision],
    ) -> list[ArtifactRecord]:
        """Apply a content-only multi-artifact CAS as one transaction.

        Every id must belong to ``session_id`` and every expected revision
        must match before any row changes. Payloads and evidence refs are
        immutable through this path.

        Raises:
            ArtifactNotFound: Any target is unknown or outside the session.
            ArtifactRevisionConflict: Any target revision has moved.
        """
        ...

    async def list_session_artifacts(
        self, session_id: str
    ) -> list[ArtifactRecord]:
        """ALL session-scoped artifacts of a session, oldest first,
        META-ONLY (``content_markdown`` empty).

        The session-context builder lists every deliverable of
        the session so a follow-up turn can name and target the right
        one; bodies stay out (the registry is a prompt-sized index, the
        body read stays :meth:`get_session_artifact_by_id`).
        """
        ...

    async def user_update_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        content_markdown: str,
        expected_revision: int,
        authorize: Any,
    ) -> ArtifactRecord:
        """Optimistic user edit under the run's live edit authority.

        ``authorize(control_write)`` executes the artifact CAS inside the
        same authorization boundary that locks the canonical run root and an
        accepted direct share. Revocation and this write therefore have one
        observable order.

        Raises :class:`ArtifactNotFound`, :class:`ArtifactLocked` (status
        ``writing``), or :class:`ArtifactRevisionConflict` (revision CAS
        miss, carries the current revision).
        """
        ...

    async def rename_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        title: str,
        authorize: Any,
    ) -> ArtifactRecord:
        """Metadata-only user rename: sets ``title`` +
        ``updated_at`` and NOTHING else — no revision bump, no revision
        row, so a rename never pollutes the content history or fakes a
        change badge. Same authorization boundary as
        :meth:`user_update_artifact`.

        Raises :class:`ArtifactNotFound` or :class:`ArtifactLocked`
        (status ``writing``). Title conflicts are last-write-wins by
        design (the name is display, the id is identity).
        """
        ...

    async def get_artifact(
        self, run_id: str, artifact_id: str, *, revision: int | None = None,
        include_private: bool = False,
    ) -> tuple[ArtifactRecord, list[ArtifactRevisionRecord]]:
        """One artifact plus its revision METADATA list (no bodies).

        With ``revision`` the returned record carries that revision's body
        (the row metadata stays current); :class:`ArtifactNotFound` for
        unknown ids or revisions.
        """
        ...

    async def list_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        limit: int = 50,
        after: tuple[float, str] | None = None,
        include_private: bool = False,
    ) -> tuple[list[ArtifactRecord], str | None]:
        """Metadata page (``content_markdown=""``), newest-first keyset
        pagination; returns ``(rows, next_cursor)``."""
        ...

    async def settle_terminal_control_rows(
        self, run_id: str
    ) -> tuple[int, int]:
        """Idempotent CAS recovery for a run that cannot re-enter its graph.

        Releases every ``writing`` artifact to ``ready`` (content and
        revision unchanged — the last streamed state becomes
        user-editable) and settles every ``pending`` approval as
        ``rejected`` with an empty decision verb and a system note.
        Returns ``(artifacts_released, approvals_settled)``. Unanswered
        clarifications stay ``pending`` deliberately (see
        :data:`CLARIFICATION_STATUSES`).
        """
        ...
