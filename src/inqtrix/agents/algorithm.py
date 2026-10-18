"""The workspace-agent algorithm (``mode=workspace_agent``).

A CHECKPOINTED LangGraph phase machine — deterministic control flow, the
LLM only fills phase-shaped structured calls. Human-in-the-loop
pauses are LangGraph ``interrupt()`` calls surfaced outward as the run
waiting statuses: the algorithm parks the run and returns; the
decision endpoints resume it, and this class fast-forwards from the
checkpoint with the recorded decision (the control store is the
truth, the checkpoint only the resumability cache).

Node writes that precede an ``interrupt()`` re-execute on resume, so
every such write is idempotent, keyed on ``(run_id, kind, round)``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, TypedDict

from inqtrix.constants import REASONING_TIMEOUT
from inqtrix.agents import (
    critic_phase,
    evidence,
    memory_reflection,
    report_quality,
    synthesis,
)
from inqtrix.agents.checkpoint_guard import ensure_checkpoint_restart_safe
from inqtrix.agents.memory_ports import AgentMemoryUnavailable
from inqtrix.agents.markdown import (
    normalize_agent_markdown,
    normalize_writer_markdown,
)
from inqtrix.agents.patterns._structured import observe_structured_retries
from inqtrix.agents.telemetry import model_retry_activity, provider_retry_activity
from inqtrix.evidence import (
    evidence_cut_words,
    attach_web_search_lineage,
    build_instant_web_search_ledger,
    merge_web_search_ledgers,
)
from inqtrix.agents.clarification import (
    MAX_QUESTIONS_PER_ROUND,
    RunSettingsFacts,
    answered_subjects,
    build_clarification,
    guarded_questions,
    discovery_questions,
    filter_repeated_questions,
    filter_settled_questions,
    intake_questions,
    not_asked_assumptions,
    not_asked_narration,
    number_round,
    round_qa_lines,
    run_clarifications,
    sanitize_questions,
    settled_narration,
)
from inqtrix.agents.skills_runtime import (
    allowed_task_kinds,
    build_skills_block,
    build_tool_directives_line,
    check_skill_points,
    skill_model_pins,
    skill_point_key,
    strictest_requires_plan,
    unanswered_required_points,
)
from inqtrix.agents.control_ports import (
    ApprovalRecord,
    ArtifactNotFound,
    ArtifactRecord,
    ArtifactRevisionConflict,
    ClarificationNotFound,
    PlanRecord,
    TASK_TERMINAL_STATUSES,
    PlanTaskRecord,
    artifact_event_payload,
    decided_requirement_parts,
    session_memo_artifact_id,
    settle_cancelled_plan_tasks,
)
from inqtrix.i18n import t, t_count
from inqtrix.prompt_language import heading_for, resolve_answer_language
from inqtrix.agents.discovery import (
    build_probe_plan,
    execute_probes,
    run_discovery_analysis,
)
from inqtrix.agents.evidence_context import synthesis_evidence
from inqtrix.agents.intake import (
    WEB_NOT_NEEDED_NOTE_KEY,
    assignment_needs_web,
    route_readiness,
    run_intake,
)
from inqtrix.agents.limit_contract import LIMIT_REACHED_EVENT
from inqtrix.agents.phase_models import (
    AssignmentProfile,
    ClarificationOptionModel,
    ContradictionReport,
    DiscoveryResult,
    MemoryReflection,
    ProposedClarificationQuestion,
    ReportOutline,
    ReportSection,
    SufficiencyJudgement,
)
from inqtrix.agents.narration import (
    discovery_narration,
    emit_narration,
    first_sentences,
    plan_narration,
    section_narration,
    synthesis_narration,
    task_narration,
)
from inqtrix.agents.plan_collections import CollectionCatalogEntry
from inqtrix.agents.plan_models import (
    WEB_RESEARCH_PROFILE_ORDER,
    ExecutionPlanModel,
)
from inqtrix.agents.planner import (
    PlanningFailed,
    plan_to_records,
    replan_headroom,
    run_planner,
)
from inqtrix.agents.run_outcome import cancel_final, send_outcome
from inqtrix.agents.research_prospect import (
    RETURNS_ON_REREADING,
    Candidate,
    DocumentSelection,
    Prospect,
    ProspectFacts,
    ReadingView,
    document_windows_used,
    held_versions,
    readings_of,
    research_can_reach,
    select_documents,
    version_reach,
)
from inqtrix.agents.replan import (
    autonomy_auto_approves,
    evaluate_replan,
    evidence_identity,
    has_unresolved_outcome,
)
from inqtrix.agents.session_context import build_session_context
from inqtrix.agents.document_analysis import (
    WINDOW_STEP_CHARS,
    analysis_facts,
    analysis_notes,
    document_version,
    has_reading,
    reading_blockers,
    reading_predicates,
)
from inqtrix.agents.prompts import (
    REPLAN_BRANCH_WORDS,
    REPLAN_CRITERIA_CHANGED,
    REPLAN_CRITERIA_UNCLASSIFIED,
    REPLAN_CRITERION_OPEN,
    REPLAN_CRITERION_TEXT_DEFECT,
    REPLAN_CRITIC_GAPS_HEAD,
    REPLAN_ENTRIES_CUT,
    REPLAN_GAP_RULE,
    REPLAN_MORE_CONTRADICTIONS,
    REPLAN_POSITIONS_CUT,
    REPLAN_PROSPECT_LINE,
    REPLAN_SUFFICIENCY_GAPS_HEAD,
    REPLAN_WINDOWS_SPENT,
    REREADING_HINT,
    REREADING_LIMIT_WORDS,
    DocumentAnalysisLimits,
)
from inqtrix.untrusted_content import neutralize_fence_delimiters, untrusted_fence
from inqtrix.agents.scheduler import (
    TaskOutcome,
    execute_wave,
    project_child_run_outcome,
    run_in_pool,
    should_retry,
    task_result_payload,
    task_result_summary,
    topological_waves,
)
from inqtrix.agents.kernel.work_profiles import (
    REVIEW_NODE,
    TRANSPORT_PROMPT,
    TRANSPORT_ROLE,
)
from inqtrix.agents.source_policy import (
    KNOWLEDGE_TASK_KINDS,
    WEB_TASK_KINDS,
    allowed_task_kinds_for_policy,
    coerce_source_policy,
    effective_source_policy,
    knowledge_scope_of,
    require_task_allowed,
)
from inqtrix.agents.report_requirement import (
    requirement_attached_payload,
    requirement_parts,
)
from inqtrix.agents.tier_policy import (
    effective_clarification_rounds,
    resolve_section_register,
    resolve_tier_policy,
    review_required,
)
from inqtrix.agents.web_execution_policy import (
    WebResearchPolicy,
    derive_web_research_policy,
)
from inqtrix.exceptions import (
    AgentCancelled,
    AgentPolicyDenied,
    AgentTokenBudgetExceeded,
)
from inqtrix.execution_authority import (
    AuthorizationRevoked,
    pinned_knowledge_collection_ids,
)
from inqtrix.knowledge.retrieval_warnings import (
    SearchNotice,
    collect_search_notices,
    search_notice_lines,
    stored_search_notices,
)
from inqtrix.execution_failures import (
    RETRYABLE_AGENT_TASK_ORCHESTRATION_CODES,
    classify_execution_failure,
)
from inqtrix.model_routing import (
    describe_resolution,
    describe_unresolved_resolution,
)
from inqtrix.providers.base import observe_provider_retries
from inqtrix.text import LOG_CUT_MARKER, shorten_visibly
from inqtrix.urls import sanitize_error

if TYPE_CHECKING:
    from inqtrix.agents.checkpointing import CheckpointerHandle
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.capabilities import CapabilityRegistry
    from inqtrix.core.context import RunContext, RuntimeContext
    from inqtrix.core.results import AgentResult, HandoverClarification, RunRequest
    from inqtrix.services.agent_context import AgentContextResolver
    from inqtrix.services.run_service import RunService
    from inqtrix.settings import AgentPlatformSettings

log = logging.getLogger("inqtrix")

_AGENT_NODES = (
    "agent_intake",
    "agent_discovery_analyst",
    "agent_skill_point_check",
    "agent_plan",
    "agent_contradiction",
    "agent_sufficiency",
    "agent_synthesis",
    "agent_answer",
    "agent_answer_light",
    REVIEW_NODE,
    "agent_memory_reflection",
    "agent_file_analysis",
    "agent_patch",
)

_OVERRIDE_SCOPED_NODES = frozenset(
    {"agent_plan", "agent_synthesis", "agent_answer", "agent_answer_light", REVIEW_NODE}
)
"""Planning, writing and substantive review follow the selected model and effort.

Request overrides and skill pins apply to these roles. Classification and
coverage checks keep their configured tiers. The kernel resolves its loop
and final review separately, so it does not need this scope set.
"""

PHASE_CHANGED_EVENT = "inqtrix.agent.phase.changed"
PATCH_PROPOSED_EVENT = "inqtrix.agent.patch.proposed"
PLAN_PROPOSED_EVENT = "inqtrix.agent.plan.proposed"
PLAN_REVISED_EVENT = "inqtrix.agent.plan.revised"
APPROVAL_REQUESTED_EVENT = "inqtrix.agent.approval.requested"
CLARIFICATION_REQUESTED_EVENT = "inqtrix.agent.clarification.requested"
TASK_STARTED_EVENT = "inqtrix.agent.task.started"
TASK_FINISHED_EVENT = "inqtrix.agent.task.finished"
TASK_FAILED_EVENT = "inqtrix.agent.task.failed"
ARTIFACT_CREATED_EVENT = "inqtrix.agent.artifact.created"
ARTIFACT_UPDATED_EVENT = "inqtrix.agent.artifact.updated"
ARTIFACT_EDIT_CONFLICT_EVENT = "inqtrix.agent.artifact.edit_conflict"
# The durable record of WHICH canvas comments rode a submission —
# run summaries deliberately never carry the canvas context, so the
# transcript surfaces it from this replayable event.
CANVAS_CONTEXT_ATTACHED_EVENT = "inqtrix.agent.canvas_context.attached"
# The durable record of the result requirement a run starts under: which
# library rules at which frozen revision, how long the typed half is and
# whether a review pass checks the result. Emitted once, when the first
# segment seeds the run (kernel and mission alike).
REQUIREMENT_ATTACHED_EVENT = "inqtrix.agent.requirement.attached"
ACTIVITY_EVENT = "inqtrix.agent.activity"
DELEGATED_CRITIC_RESEARCH_ROUNDS = 1
"""Critic research rounds a DELEGATED mission (kernel child) may open.

The parent's deep verification is that mission's second check, so a
second in-mission research round would verify three times over (planned
task, critic round, parent pass). The critic's remaining findings close
the memo as named open points instead — see ``_critic_research_cap``."""

_WAITING_STATUS_BY_KIND = {
    "clarification": "waiting_for_input",
    "approval": "waiting_for_approval",
    "children": "waiting_for_children",
}
"""Interrupt kind -> run waiting status. ``children`` parks the parent
WITHOUT holding an execution slot while its child research runs execute;
the store wakes it when the last child terminates (no human involved)."""

class AgentPhaseState(TypedDict, total=False):
    """Checkpointed graph state — JSON-serializable values only."""

    question: str
    language: str
    language_source: str
    history: str
    autonomy: str
    delegated: bool
    """This run is a delegated child (run row carries a parent): its
    round-0 plan gate was covered by the delegation approval — see the
    plan node's narrowing. Read once at round 0, checkpoint-durable."""
    session_id: str
    skill_point_answers: dict[str, dict[str, str]]
    """Per attached skill: point-name -> answer (from context or the
    clarify round); feeds the {{name}} substitution."""
    skill_questions: list[dict[str, Any]]
    """Pending REQUIRED skill points for the clarify gate; emptied
    after the round (never re-asked)."""
    artifact_registry: list[dict[str, Any]]
    """Meta of ALL session deliverables (the multi-deliverable index),
    filled at intake from the session-context builder."""
    last_response_form: str
    """Output form of the latest completed session turn (the routing
    memory); '' when unknown."""
    deliverable: str
    """The turn's output form: ``chat`` writes the
    run-local inline answer artifact, ``canvas`` the session memo.
    Decided ONCE at intake (request override > intake profile > canvas;
    a patch assignment forces canvas)."""
    phase: str
    profile: dict[str, Any] | None
    route: str
    discovery_tool_use_counts: dict[str, int]
    """Successful source probes run before the persisted mission plan."""
    memory_briefing: str
    prior_evidence_count: int
    """Canonical count of evidence already attached to session artifacts."""
    memory_status: str
    memory_candidates: list[dict[str, Any]]
    discovery: dict[str, Any] | None
    clarification_rounds: int
    clarification_asked: list[str]
    """The question prompts of every clarification round this run held,
    appended when a round's answer arrives. Checkpointed, so the repeat
    filter of a later round reads the same list in every segment."""
    clarification_assumptions: list[dict[str, str]]
    """Questions the run answered with an assumption instead of asking,
    each ``{prompt, assumption, reason}`` (``reason``: ``limit``, ``tier``
    or ``overflow``). Named in the transcript, fenced for the analyst and
    the planner, and carried verbatim into the first plan's assumptions;
    never part of ``history``, which holds only the user's answers."""
    plan_version: int
    plan_id: str
    planned_rounds: int
    needs_plan_interrupt: bool
    replan_noop: bool
    """The latest model delta deliberately added/skipped no work; after its
    auditable plan version is stored, route directly to synthesis."""
    replan_gate: dict[str, Any]
    """What the replan approval shows: ``reason`` and the not yet executed
    non-synthesis ``new_tasks`` of the latest version (a checkpointed
    channel — an undeclared key would not survive the node boundary)."""
    web_consent: bool
    """Whether a plan version of the user's own holds a web task
    (``web_research`` or ``web_instant``). Sticky for the run and
    reconstructed from the persisted plan lineage when a checkpoint lacks
    the field."""
    web_consent_checked_version: int
    """Newest persisted plan version inspected while reconstructing consent."""
    answered_subjects: list[str]
    """Subjects of the clarification questions the user answered
    (``sources``, ``knowledge_scope`` ...). An answered ``sources`` question
    is the user's own choice about the web, above the intake's reading."""
    success_criteria: list[str]
    report_guidance: str
    """Decision-scoped user guidance for the report (structure, focus,
    audience) attached at the plan gate; rendered into the outline,
    section and chat-answer prompts."""
    report_requirement_parts: dict[str, Any]
    """The parts of the requirement in force, in the form a gate decision
    stores them: ``{"free_text", "rules": [{template_id, label,
    title, revision}]}``. Seeded from the submission, replaced by every gate
    decision that sets a requirement. A plan or replan gate opens with them
    (its payload's ``report_requirement``), so a gate the user touches
    starts from what the run works under instead of an empty picker that
    would replace it. Absent when the submitted text does not split into
    its parts (a run queued before the parts were kept)."""
    clarified_context: str
    """Compact answered-clarification text for the deterministic
    discovery prober. MUST be declared here: LangGraph drops
    undeclared keys between nodes."""
    plan_rejected: bool
    """A human gate (plan, replan or discovery approval) was rejected.
    NOT a failure: the run completes cleanly with a deterministic
    receipt as its answer (or keeps an already-synthesized draft), and
    the result payload derives ``plan_decision="rejected"`` from it."""
    outcomes: dict[str, dict[str, Any]]
    pending_children: dict[str, dict[str, Any]]
    """Submitted-but-unfolded child research runs, keyed by task id:
    ``{"child_run_id": str, "attempt": int}``. Non-empty exactly while
    the run is (heading towards) ``waiting_for_children`` — the
    children_wait node folds them into ``outcomes`` on resume."""
    legacy_budget_notice_task_ids: list[str]
    """Legacy-budget tasks already narrated in this checkpointed run."""
    references: list[dict[str, Any]]
    web_search_ledger: dict[str, Any]
    """Provider search answers and citation lineage retained for the Canvas."""
    claims: list[dict[str, Any]]
    contradictions: list[dict[str, Any]]
    tool_use_counts: dict[str, int]
    """Cumulative successful source-tool calls for the execution overview."""
    web_searches: int
    """Web searches the mission ran itself (:func:`_mission_web_searches`)."""
    sufficiency: dict[str, Any] | None
    replan_rounds: int
    memo_markdown: str
    memo_title: str
    artifact_id: str
    prior_memo: str
    memo_base_revision: int
    memo_user_prefix: str
    critic: dict[str, Any] | None
    last_review: dict[str, Any]
    """The latest review of this run: ``{status, round}`` with ``status``
    ``completed``, ``unavailable`` (then also ``reason``) or ``waived`` (the
    run's Stufe skips the review; written by the finalize node). Tells a
    review that could not run apart from one that never ran, which
    ``critic=None`` cannot; the outcome record reads it."""
    research_prospect: dict[str, Any]
    """The latest research prospect decision: ``{round, route, reachable,
    branches, codes}`` (route ``critic`` or ``gate``). Stored where the
    replan is decided, before the planner runs."""
    tasks_before_round: list[str]
    """The task ids that had run when this round's plan version was saved
    (``[]`` for round 0). A file analysis of this round does not read again
    what their readings read; an analysis of the same round reads what it
    finds."""
    review_pending: bool
    synthesis_pending: bool
    """A synthesis round handed sections to section roles and parks on
    them. The synthesis task is still ``running`` when the run comes back,
    exactly as after a crash in the middle of writing; only a committed node
    return can have set this flag, so it tells the return from the park apart
    from a crash BEFORE the round's first park - which stays fail-closed. A
    crash after a wake starts from the checkpoint the park committed and
    resumes from its progress. The synthesis node lowers the flag on every
    way out except a new park."""
    synthesis_progress: dict[str, Any]
    """What the current synthesis round has written, carried across its
    park: ``round``, the ``outline`` as written, per section index (a
    string key) its ``transport`` and, once written, its ``title``,
    ``body``, ``preselected``, ``cited`` and what a role ``read``; and
    ``folded``, how many sections are already in the memo. Cleared when the
    round completes - the memo artifact is the record from then on."""
    target_document_id: str
    patch_id: str
    patch_decision: str
    usage: dict[str, int]
    cancelled: bool
    failure: str


class WorkspaceAgentAlgorithm:
    """Workspace agent over the platform seams.

    Args:
        control_store: Plans, approvals, clarifications, and artifacts.
        run_service: Child research runs + the run store handle.
        resolver: Builds child ``ResolvedAgentContext``s (report
            profiles per task according to the task contract).
        capability_registry: Read-only tool surface; ``None``
            degrades discovery/instant tools loudly per probe.
        checkpointer: Postgres checkpointer or the volatile development
            escape.
        platform: Server-side agent limits (never prompted).
    """

    id = "workspace_agent"
    display_name = "Workspace-Agent"

    def __init__(
        self,
        *,
        control_store: "AgentControlStore",
        run_service: "RunService",
        resolver: "AgentContextResolver",
        capability_registry: "CapabilityRegistry | None",
        checkpointer: "CheckpointerHandle",
        platform: "AgentPlatformSettings",
        permission_service: Any = None,
        knowledge_service: Any = None,
        editor_patch_service: Any = None,
        editor_persistence_service: Any = None,
        agent_memory_service: Any = None,
        skill_service: Any = None,
    ) -> None:
        self._control = control_store
        self._run_service = run_service
        self._resolver = resolver
        self._capabilities = capability_registry
        self._checkpointer = checkpointer
        self._platform = platform
        self._permissions = permission_service
        self._knowledge = knowledge_service
        self._editor_patches = editor_patch_service
        self._editor_docs = editor_persistence_service
        self._memory = agent_memory_service
        self._skills = skill_service
        self._graph: Any = None
        self._graph_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        """Registry manifest entry (no research-graph streaming)."""
        return {
            "requires": ["llm"],
            "streams_events": True,
            "supports_chat_completions": False,
            "terminal_node": "agent_answer",
            "produces": ["markdown", "artifacts", "plan"],
            "interrupts": ["approval", "clarification"],
        }

    # -- execution --------------------------------------------------------- #

    def run(
        self,
        request: "RunRequest",
        *,
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> "AgentResult":
        """Execute or RESUME one agent run (segment-aware)."""
        from inqtrix.core.results import AgentResult

        if context.run_id is None or context.park is None:
            raise RuntimeError(
                ("workspace_agent runs only through /v1/runs (parking support and run_id"
                " required).")
            )
        run_id = context.run_id
        graph = self._compiled_graph()
        config = {"configurable": {"thread_id": run_id}}
        deps = _RunDeps(self, request, runtime, context)

        existing = graph.get_state(config)
        if (
            existing is not None
            and existing.values
            and "language" in existing.values
        ):
            deps.language = _language(existing.values)
        # Usage at segment START: execute_run_request books raw.usage
        # after EVERY segment, so each return must carry only this
        # segment's DELTA — re-booking the checkpointed cumulative total
        # would double-count every earlier segment against the quota.
        start_usage = dict(
            (existing.values.get("usage") or {})
            if existing is not None and existing.values
            else {}
        )
        if existing is None or not existing.values:
            ensure_checkpoint_restart_safe(
                run_id,
                control=self._control,
                run_store=self._run_service.run_store,
                run_async=_run_async,
            )
            # Whether this run IS a delegation: the run row is the only
            # truthful source (RunRequest carries no tree linkage), read
            # once at round 0 and checkpointed with the state, so resumes
            # never re-read. Pre-upgrade checkpoints lack the key and
            # keep their plan gate (fail-safe toward gating). The read
            # MUST present the owner's visibility: under the store's
            # authorization predicate a ``visible_to=None`` read only
            # sees anonymous rows, so an owned (child) run would answer
            # RunNotFound (same contract as ``deps.visible_to`` reads).
            own_row = self._run_service.run_store.get(
                run_id, visible_to=deps.visible_to
            )
            requirement_parts = _submitted_requirement_parts(request, run_id)
            if request.report_requirement:
                deps.emit(
                    REQUIREMENT_ATTACHED_EVENT,
                    requirement_attached_payload(
                        request.report_requirement,
                        request.report_rules,
                        review_owed=(
                            deps.tier_policy is None
                            or review_required(deps.tier_policy.verify)
                        ),
                        run_id=run_id,
                    ),
                )
            graph_input: Any = {
                "question": request.question,
                "history": request.history,
                "autonomy": request.autonomy
                or self._platform.default_autonomy,
                "delegated": bool(own_row.get("parent_run_id")),
                "session_id": request.session_id,
                "target_document_id": request.document_id,
                "phase": "intake",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                # Seeded from the submit-time requirement. Three
                # paths never reach a plan gate — autonomous, the speed
                # tier and delegated children — so without this seed
                # they had NO way to state one. A later plan-gate
                # decision replaces it; an approval that says nothing
                # about the result leaves it standing.
                "report_guidance": request.report_requirement or "",
                **(
                    {"report_requirement_parts": requirement_parts}
                    if requirement_parts is not None
                    else {}
                ),
                "clarification_rounds": 0,
                "replan_rounds": 0,
                "outcomes": {},
                "references": [],
                "web_search_ledger": {
                    "schema_version": 1,
                    "kind": "web_search_ledger",
                    "searches": {},
                },
                "claims": [],
                "contradictions": [],
                "tool_use_counts": {"web": 0, "knowledge": 0},
                "web_searches": 0,
                "discovery_tool_use_counts": {"web": 0, "knowledge": 0},
                "cancelled": False,
            }
        elif existing.tasks and any(
            task.interrupts for task in existing.tasks
        ):
            from langgraph.types import Command

            resume_value = self._decision_for_interrupt(run_id, existing)
            graph_input = Command(resume=resume_value)
        else:
            # Crash between segments without a pending interrupt: the
            # checkpoint fast-forwards past completed nodes on its own.
            graph_input = None

        _DEPS.set(deps)
        try:
            try:
                with observe_provider_retries(
                    deps.llm,
                    lambda notice: deps.emit_model_retry(
                        "agent_model", notice
                    ),
                ), observe_structured_retries(deps.emit_model_retry):
                    result_state = graph.invoke(graph_input, config)
            except AgentCancelled as exc:
                _send_outcome(
                    deps,
                    lambda: graph.get_state(config).values,
                    run_id=run_id,
                    final=cancel_final(exc),
                )
                usage_total = dict(getattr(exc, "usage", {}) or start_usage)
                _run_async(
                    settle_cancelled_plan_tasks(
                        self._control, run_id, language=_deps_language(deps)
                    )
                )
                cancel_reason = (
                    "token_budget_exceeded"
                    if isinstance(exc, AgentTokenBudgetExceeded)
                    else "client_requested_cancel"
                )
                self._checkpointer.delete_thread(run_id)
                return AgentResult(
                    answer="",
                    result_type="workspace_agent_cancelled",
                    raw={
                        "answer": "",
                        "usage": _usage_delta(usage_total, start_usage),
                        "result_state": {
                            "cancelled": True,
                            "cancel_reason": cancel_reason,
                            "usage_total": usage_total,
                            "execution": _workspace_execution_payload(
                                deps, {"usage": usage_total}
                            ),
                        },
                    },
                )
            except Exception:
                # The graph failed on its own: the record says so from the
                # last committed state, and the original error goes on.
                _send_outcome(
                    deps,
                    lambda: graph.get_state(config).values,
                    run_id=run_id,
                    final="failed",
                )
                raise
        finally:
            _DEPS.set(None)

        interrupts = result_state.get("__interrupt__") or ()
        if interrupts:
            payload = interrupts[0].value if interrupts else {}
            waiting_status = _WAITING_STATUS_BY_KIND.get(
                payload.get("kind", ""), "waiting_for_approval"
            )
            context.park(waiting_status)
            return AgentResult(
                answer="",
                result_type="workspace_agent_parked",
                raw={
                    "answer": "",
                    "usage": _usage_delta(
                        result_state.get("usage", {}), start_usage
                    ),
                    "result_state": {
                        "cancelled": False,
                        "parked": True,
                        "phase": result_state.get("phase", ""),
                        "usage_total": dict(result_state.get("usage", {})),
                        "execution": _workspace_execution_payload(
                            deps, result_state
                        ),
                    },
                },
            )

        cancelled = bool(result_state.get("cancelled"))
        answer = result_state.get("memo_markdown", "") or ""
        failure = result_state.get("failure", "")
        # Patch-phase failures are HARD even though a memo exists: the
        # user explicitly targeted a document — completing quietly would
        # hide that the core of the assignment did not happen.
        hard_patch_failure = failure in (
            "patch_document_not_found",
            "editor_patches_unavailable",
            "patch_proposal_failed",
            "patch_document_too_large",
        )
        raises = bool(
            failure and not cancelled and (not answer or hard_patch_failure)
        )
        # Sent before the checkpoint goes: the state is in hand, and a
        # checkpointer error below must not lose a finished run's record.
        _send_outcome(
            deps,
            lambda: result_state,
            run_id=run_id,
            final=(
                "failed" if raises else "cancelled" if cancelled else "completed"
            ),
        )
        self._checkpointer.delete_thread(run_id)
        usage_total = dict(result_state.get("usage", {}))
        usage = _usage_delta(usage_total, start_usage)
        if raises:
            raise RuntimeError(failure)
        return AgentResult(
            answer=answer,
            result_type="workspace_agent_result",
            raw={
                "answer": answer,
                "usage": usage,
                "result_state": {
                    "cancelled": cancelled,
                    "answer": answer,
                    "usage_total": usage_total,
                    "phase": result_state.get("phase", "done"),
                    "plan_decision": (
                        "rejected"
                        if result_state.get("plan_rejected")
                        else ""
                    ),
                    "plan_version": result_state.get("plan_version", 0),
                    "artifact_id": result_state.get("artifact_id", ""),
                    "critic_report": result_state.get("critic"),
                    "references": result_state.get("references", []),
                    "web_search_ledger": result_state.get(
                        "web_search_ledger", {}
                    ),
                    "report_references": result_state.get(
                        "references", []
                    ),
                    "answer_claim_bindings": result_state.get(
                        "answer_claim_bindings", []
                    ),
                    "contradictions": result_state.get(
                        "contradictions", []
                    ),
                    "replan_rounds": result_state.get("replan_rounds", 0),
                    "patch_id": result_state.get("patch_id", ""),
                    "patch_decision": result_state.get(
                        "patch_decision", ""
                    ),
                    "memory_status": result_state.get("memory_status", ""),
                    "memory_candidates": result_state.get(
                        "memory_candidates", []
                    ),
                    "execution": _workspace_execution_payload(
                        deps, result_state
                    ),
                },
            },
        )

    def _decision_for_interrupt(self, run_id: str, snapshot: Any) -> dict[str, Any]:
        """Resolve the resume payload from the authoritative control store."""
        payload: dict[str, Any] = {}
        for task in snapshot.tasks:
            for intr in task.interrupts:
                payload = dict(intr.value or {})
                break
        kind = payload.get("kind", "approval")
        if kind == "children":
            # No control-store row backs a children wait: the child RUN
            # rows are the truth — the children_wait node
            # reads their terminal outcomes from the run store itself.
            return {"kind": "children"}
        if kind == "clarification":
            record = _run_async(
                self._control.get_clarification(
                    run_id, payload.get("id", "")
                )
            )
            return {
                "kind": "clarification",
                "status": record.status,
                "answer": record.answer,
                "option_id": record.option_id,
                "answers": dict(record.answers),
            }
        record = _run_async(
            self._control.get_approval(run_id, payload.get("id", ""))
        )
        resume: dict[str, Any] = {
            "kind": "approval",
            "status": record.status,
            "decision": record.decision,
            "note": record.note,
        }
        decided_payload = dict(record.decision_payload)
        # Presence carries the decision: a key that was never sent must
        # not arrive as "" and overwrite an earlier guidance, and a key
        # that WAS sent empty must arrive so it can clear one.
        if "report_guidance" in decided_payload:
            resume["report_guidance"] = str(
                decided_payload["report_guidance"] or ""
            )
            # The parts the composed text was made of travel with it,
            # always stored together by the decision: the next gate opens
            # with them.
            resume["report_requirement"] = decided_requirement_parts(
                decided_payload, approval_id=record.approval_id
            )
        return resume

    # -- graph ------------------------------------------------------------- #

    def _compiled_graph(self) -> Any:
        with self._graph_lock:
            if self._graph is None:
                self._graph = _build_graph(self._checkpointer.saver())
            return self._graph


# The per-run dependency bundle travels via a context variable: LangGraph
# node functions receive only the state, and the compiled graph is shared
# across runs (thread_id separates them) — a threading-local would break
# if langgraph ever re-schedules nodes, contextvars follow the execution.
import contextvars

_DEPS: contextvars.ContextVar["_RunDeps | None"] = contextvars.ContextVar(
    "inqtrix_agent_deps", default=None
)


def _deps() -> "_RunDeps":
    deps = _DEPS.get()
    if deps is None:  # pragma: no cover - graph never runs undepped
        raise RuntimeError("agent graph invoked without run dependencies")
    return deps


class _RunDeps:
    """Everything one segment execution needs (never checkpointed)."""

    def __init__(
        self,
        algorithm: WorkspaceAgentAlgorithm,
        request: "RunRequest",
        runtime: "RuntimeContext",
        context: "RunContext",
    ) -> None:
        self.algorithm = algorithm
        self.request = request
        self.runtime = runtime
        self.context = context
        self.control = algorithm._control
        self.run_service = algorithm._run_service
        self.resolver = algorithm._resolver
        self.capabilities = algorithm._capabilities
        self.source_policy = effective_source_policy(request.source_policy)
        self.platform = algorithm._platform
        self.llm = context.providers.llm
        self.knowledge = algorithm._knowledge
        self.editor_patches = algorithm._editor_patches
        self.editor_docs = algorithm._editor_docs
        self.memory = algorithm._memory
        # Per-user visibility, resolved ONCE per segment: the agent's
        # tool calls must see exactly what its owner sees. By contract,
        # visible_to=None is the
        # see-everything view of the anonymous/static modes only).
        self.visible_to = None
        if (
            algorithm._permissions is not None
            and context.principal is not None
        ):
            self.visible_to = _run_async(
                algorithm._permissions.resolve_user_context(
                    context.principal
                )
            )
        self.knowledge_collection_ids = pinned_knowledge_collection_ids(
            request.knowledge_filters,
            scoped_principal=bool(
                context.principal is not None
                and context.principal.user_id is not None
            ),
        )
        # Long-term agent memory is opt-in per user (privacy default OFF),
        # resolved ONCE per segment like visible_to. The read runs on the
        # account-preferences NullPool store (loop-agnostic), safe from this
        # sync worker thread; opt_in_enabled degrades to False visibly on any
        # read error, so a failure never silently enables memory.
        self.agent_memory_opt_in = False
        if algorithm._memory is not None and context.principal is not None:
            self.agent_memory_opt_in = _run_async(
                algorithm._memory.opt_in_enabled(context.principal)
            )
        self._collection_scope: list[str] | None = None
        # The run's document-analysis window budget, opened on the first
        # read of a segment, a claim or a peek (see _windows_left_locked).
        self._document_windows_lock = threading.Lock()
        self._document_windows_left: int | None = None
        settings = context.agent_settings
        self.timeout = float(
            getattr(settings, "reasoning_timeout", REASONING_TIMEOUT)
        )
        self.depth = settings.depth
        # The selected Stufe ('' = legacy depth semantics). All budget/
        # gate consequences resolve through tier_policy.TIER_POLICIES.
        self.tier: str = getattr(settings, "agent_tier", "") or ""
        self.tier_policy = (
            resolve_tier_policy(self.tier) if self.tier else None
        )
        # Router-admitted skills, loaded ONCE per segment from durable
        # rows (rows are truth) — BEFORE the node resolution so skill
        # model pins can inform it. A skill deleted between
        # admission and this segment fails the run LOUDLY — the user
        # attached it explicitly, running without it would be a silent
        # behavior change.
        self.skills: list[Any] = []
        if request.skill_ids:
            if algorithm._skills is None:
                raise RuntimeError(
                    "Skills are attached but not configured on this server."
                )
            for skill_id in request.skill_ids:
                try:
                    record, _access = _run_async(
                        algorithm._skills.get_visible(
                            skill_id,
                            tenant_id=(
                                context.principal.tenant_id
                                if context.principal is not None
                                else "default"
                            ),
                            visible_to=self.visible_to,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 — loud, never partial
                    raise RuntimeError(
                        f'Attached skill {skill_id} is no longer available; run aborted.'
                    ) from exc
                expected_revision = request.skill_revisions.get(skill_id)
                if (
                    expected_revision is None
                    or record.revision != expected_revision
                ):
                    raise RuntimeError(
                        f'Attached skill {skill_id} changed since approval; run aborted.'
                    )
                self.skills.append(record)
        self.resolutions = self._resolve_nodes(settings)

    def _resolve_nodes(self, settings: Any) -> dict[str, dict[str, str]]:
        provider_models = getattr(self.llm, "models", None)
        requested_tier = (getattr(settings, "model_tier", "") or "").strip()
        requested_model = (getattr(settings, "model", "") or "").strip()
        requested_effort = (getattr(settings, "effort", "") or "").strip()
        pin_tier, pin_effort = skill_model_pins(self.skills)
        if (pin_tier and not requested_tier) or (
            pin_effort and not requested_effort
        ):
            # Precedence: explicit user override > skill pin > tier
            # map. The pin only fills what the request left empty.
            log.info(
                "Skill pin active: model_tier=%r effort=%r",
                pin_tier or "(none)",
                pin_effort or "(none)",
            )
        requested_tier = requested_tier or pin_tier
        requested_effort = requested_effort or pin_effort
        self.model_overrides = {
            key: value for key, value in (
                ("model_tier", requested_tier),
                ("model", requested_model),
                ("effort", requested_effort),
            ) if value
        }
        if requested_tier or requested_model or requested_effort:
            log.info(
                "Model override active (tier=%r model=%r effort=%r); applies to %s, "
                "while pipeline nodes retain the tier map.",
                requested_tier or "(none)",
                requested_model or "(none)",
                requested_effort or "(none)",
                ", ".join(sorted(_OVERRIDE_SCOPED_NODES)),
            )
        resolutions: dict[str, dict[str, str]] = {}
        for node in _AGENT_NODES:
            scoped = node in _OVERRIDE_SCOPED_NODES
            node_tier = requested_tier if scoped else ""
            node_model = requested_model if scoped else ""
            node_effort = requested_effort if scoped else ""
            if provider_models is None and not node_model:
                desc = describe_unresolved_resolution(node, node_tier)
            else:
                desc = describe_resolution(
                    node,
                    provider_models,
                    node_tier,
                    requested_model=node_model,
                    requested_effort=node_effort,
                )
            resolutions[node] = desc
            self.emit("inqtrix.node.model_resolution", desc)
        return resolutions

    def resolved(self, node: str) -> tuple[str | None, str | None]:
        desc = self.resolutions[node]
        return (desc.get("model") or None, desc.get("effort") or None)

    def child_scope(self, state: "AgentPhaseState") -> Any:
        """What a role child of this mission inherits (never more than the mission)."""
        from inqtrix.agents.kernel.tools import ChildScope

        filters = self.request.knowledge_filters
        return ChildScope(
            run_id=self.context.run_id or "",
            run_service=self.run_service,
            resolver=self.resolver,
            principal=self.context.principal,
            workspace_id=self.context.workspace_id,
            session_id=str(state.get("session_id") or ""),
            stack_name=getattr(self.context, "stack_name", "") or "",
            source_policy=self.source_policy,
            model_overrides=dict(self.model_overrides),
            knowledge_collection_ids=self.knowledge_collection_ids,
            knowledge_scope_explicit=bool(filters.get("explicit")),
            knowledge_scope=filters.get("scope") or None,
            tier=self.tier,
            depth=self.depth,
            # A mission's role children read the requirement in their
            # handover; nothing is forwarded on the submit.
            report_requirement="",
            report_rules=(),
        )

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.context.event_sink is not None:
            self.context.event_sink(event_type, payload)

    def emit_model_retry(
        self,
        node: str,
        notice: dict[str, Any],
    ) -> None:
        """Emit one provider retry through the existing Agent activity stream."""
        self.emit(
            ACTIVITY_EVENT,
            model_retry_activity(
                notice, node=node, language=_deps_language(self) or ""
            ),
        )

    def cancelled(self) -> bool:
        token = self.context.cancel_token
        return bool(token is not None and token.is_set())

    def _windows_left_locked(self, state: "AgentPhaseState") -> int:
        """The run's windows left (the caller holds the lock).

        The budget spans waves, replans and parks: the first read of a
        segment starts from what the run's recorded outcomes already used.
        """
        if self._document_windows_left is None:
            self._document_windows_left = _run_windows_left(self, state.get("outcomes"))
        return self._document_windows_left

    def document_windows_left(self, state: "AgentPhaseState") -> int:
        """The run's document-analysis windows left, without claiming any."""
        with self._document_windows_lock:
            return self._windows_left_locked(state)

    def claim_document_windows(self, state: "AgentPhaseState", wanted: int) -> int:
        """Grant up to *wanted* windows from the run's document-analysis budget.

        Thread-safe, because the tasks of one wave run in parallel.
        """
        with self._document_windows_lock:
            left = self._windows_left_locked(state)
            granted = max(0, min(int(wanted), left))
            self._document_windows_left = left - granted
            return granted

    def collection_scope(self) -> list[str]:
        """The admitted collection ids this run may retrieve from.

        A concrete persisted list, including an empty list, is immutable for
        the run. Only deliberately unscoped anonymous/static execution keeps
        the historical live-all view. Current visibility is still checked so
        a revoke fails closed instead of silently shrinking the corpus.
        """
        if self._collection_scope is not None:
            return self._collection_scope
        if self.source_policy.knowledge != "available":
            self._collection_scope = []
            return self._collection_scope
        if self.knowledge_collection_ids is not None:
            requested = sorted(self.knowledge_collection_ids)
            self._collection_scope = (
                self.assert_collections(requested) if requested else []
            )
            return self._collection_scope
        if self.knowledge is None:
            self._collection_scope = []
            return self._collection_scope
        collections = _run_async(
            self.knowledge.list_collections(visible_to=self.visible_to)
        )
        self._collection_scope = [
            str(getattr(item, "id", None) or item.get("id"))
            if not hasattr(item, "id")
            else str(item.id)
            for item in collections
        ]
        return self._collection_scope

    def assert_collections(self, collection_ids: list[str]) -> list[str]:
        """Require both run admission and current actor visibility."""
        from inqtrix.knowledge.stores.ports import CollectionNotFound

        requested = set(collection_ids)
        if (
            self.knowledge_collection_ids is not None
            and not requested.issubset(self.knowledge_collection_ids)
        ):
            raise CollectionNotFound(
                "Collection is outside the admitted run scope."
            )
        if self.knowledge is None:
            raise RuntimeError(
                "Collection access cannot be checked without the knowledge service."
            )
        _run_async(
            self.knowledge.assert_collections_visible(
                collection_ids, visible_to=self.visible_to
            )
        )
        return list(collection_ids)


def _usage_delta(
    total: dict[str, Any], start: dict[str, Any]
) -> dict[str, int]:
    """This segment's token spend (cumulative total minus segment start)."""
    return {
        "prompt_tokens": max(
            0,
            int(total.get("prompt_tokens", 0) or 0)
            - int(start.get("prompt_tokens", 0) or 0),
        ),
        "completion_tokens": max(
            0,
            int(total.get("completion_tokens", 0) or 0)
            - int(start.get("completion_tokens", 0) or 0),
        ),
    }


def _send_outcome(
    deps: "_RunDeps",
    read_state: Callable[[], Mapping[str, Any]],
    *,
    run_id: str,
    final: str,
) -> None:
    """Send the run's outcome record (:func:`inqtrix.agents.run_outcome.send_outcome`).

    ``final`` is the algorithm's decision at its exit; every line passes the
    mission's reader guard over the state the record is built from. A
    record that cannot be sent is missing, visibly in the log, and never
    replaces the error the run is already leaving with.
    """
    send_outcome(
        deps.emit,
        read_state,
        run_id=run_id,
        final=final,
        guard_for=lambda values: lambda text: _guard_reader_text(values, text),
    )


def _workspace_execution_payload(
    deps: "_RunDeps", state: AgentPhaseState
) -> dict[str, object]:
    """Canonical Agent Desk execution block for the mission algorithm."""
    from inqtrix.agents.source_policy import (
        execution_payload,
        knowledge_boundary_payload,
    )

    response_form = str(
        state.get("deliverable") or deps.request.response_form or "auto"
    )
    resolution_node = (
        "agent_answer" if response_form == "chat" else "agent_synthesis"
    )
    model, effort = deps.resolved(resolution_node)
    autonomy = str(
        state.get("autonomy")
        or deps.request.autonomy
        or deps.platform.default_autonomy
    )
    limits: dict[str, object] = {
        "discovery_tool_calls": {
            "used": sum(
                int(value or 0)
                for value in dict(
                    state.get("discovery_tool_use_counts") or {}
                ).values()
            ),
            "limit": deps.platform.discovery_max_tool_calls,
            "ceiling": deps.platform.discovery_max_tool_calls,
            "recoverable": False,
            "extendable": False,
            "reason": "deterministic_discovery_budget",
        },
        "plan_tasks": {
            "limit": deps.platform.max_plan_tasks,
            "ceiling": deps.platform.max_plan_tasks,
            "recoverable": False,
            "extendable": False,
            "reason": "validated_plan_shape",
        },
        "replan_rounds": {
            "used": int(state.get("replan_rounds", 0) or 0),
            "limit": deps.platform.max_replan_rounds,
            "ceiling": deps.platform.max_replan_rounds,
            "recoverable": False,
            "extendable": False,
            "reason": "deterministic_mission_budget",
        },
        "clarification_rounds": {
            "used": int(state.get("clarification_rounds", 0) or 0),
            "limit": _clarification_cap(deps),
            "ceiling": _clarification_cap(deps),
            "recoverable": False,
            "extendable": False,
            "reason": "deterministic_mission_budget",
        },
    }
    token_budget = int(deps.context.token_budget or 0)
    if token_budget > 0:
        usage = dict(state.get("usage") or {})
        limits["tokens"] = {
            "used": sum(int(value or 0) for value in usage.values()),
            "limit": token_budget,
            "ceiling": token_budget,
            "recoverable": False,
            "extendable": False,
            "reason": "operator_ceiling_exactly_once_required",
        }
    return execution_payload(
        effective_mode="workspace_agent",
        response_form=response_form,
        depth=deps.depth,
        model=model,
        reasoning_effort=effort,
        source_policy=deps.source_policy,
        consent_reason=(
            "autonomous_policy"
            if autonomy == "autonomous"
            else "permission_policy"
        ),
        tool_use_counts=dict(state.get("tool_use_counts") or {}),
        web_searches=int(state.get("web_searches") or 0),
        limits=limits,
        **knowledge_boundary_payload(deps.request.knowledge_filters),
    )


def _run_async(coro: Any) -> Any:
    """Drive one control-store coroutine from the worker thread."""
    return asyncio.run(coro)


def _emit_narration(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    narration_id: str,
    kind: str,
    text: str,
    phase: str,
    evidence: Sequence[dict[str, Any]] = (),
) -> None:
    """One transcript prose line, silently skipping empties.

    ``narration_id`` is DETERMINISTIC per emission site (plan version,
    task id, section index ...), never a running counter — checkpointed
    nodes may re-execute after an interrupt, and a stable id keeps the
    replayed line recognizable instead of multiplying.

    ``state`` is here for ONE reason: this is the transcript's single
    choke point, and therefore the only place where the writer guard can
    be paid for once instead of at every emission site. Eight of the
    lines below carry model-written fragments — an analyst's gap, a
    plan summary, a task or section title, a clarification prompt — and
    a guard placed per call site is one the ninth line will not have.
    :func:`_guard_reader_text` is what it buys.
    """
    emit_narration(
        deps.emit,
        narration_id=narration_id,
        kind=kind,
        text=text,
        phase=phase,
        guard=lambda line: _guard_reader_text(state, line, evidence=evidence),
    )


def _guard_reader_text(
    state: AgentPhaseState,
    text: str,
    *,
    evidence: Sequence[dict[str, Any]] = (),
) -> str:
    """The writer guard for a transcript line, with a visible repair.

    The same projection every mission writer passes
    (:func:`_writer_guard`): what identifiers could have been copied, and
    which of them the USER wrote themselves. Only the authorship question
    is asked. The edit question — was this identifier already standing —
    belongs to a text that is rewritten; a narration line is written once
    and never edited, so there is no earlier version for it to stand in.

    What a leak costs differs from a memo section, and so does the
    answer. A section is produced by a call this server can repeat, so it
    goes to the bounded repair round. A narration line is assembled from
    artifacts a finished phase already produced — nobody is left to
    re-ask, and one transcript line does not earn a model call. The
    identifier is replaced with a marker in the run's answer language,
    which is the repair that keeps the reader's step visible instead of
    dropping the line.
    """
    # The run's ledger is merged ONCE, in ``_node_evidence``. Every line a
    # phase before it emits therefore finds ``references`` empty, and a
    # guard over an empty set finds nothing however hard it looks. A caller
    # that already holds the evidence its own phase produced hands it in;
    # the subtask line is the one that matters, because its text comes from
    # a model that saw raw document ids in its search results.
    #
    # The earlier lines - clarification, discovery, plan - are written by
    # models whose material is the digest, and that digest names
    # sources by title and collection, not by id. They are guarded here too,
    # against whatever the state does hold; where it holds nothing the guard
    # is honest about finding nothing rather than pretending to check.
    guard = _writer_guard(
        state, [*state.get("references", []), *evidence]
    )
    return report_quality.guarded_reader_text(
        text,
        guard["internal_reference_ids"],
        authored_by=guard["authored_by"],
        marker=t(state, "agent_reader_identifier_removed"),
    )


def _guarded_plan(
    state: AgentPhaseState,
    plan: PlanRecord,
    tasks: list[PlanTaskRecord],
) -> tuple[PlanRecord, list[PlanTaskRecord]]:
    """The plan's reader text, checked where the plan becomes DURABLE.

    Three fields of a plan version are read by a person: the intent
    paragraph above the task list, and every task's title and objective.
    All three are written by the planner, which is handed the discovery
    digest and can copy an identifier out of it the same way a section
    writer can.

    Checked at the store port rather than at the places that render them,
    and that is the point of this function existing at all. A plan row is
    read from four places that have nothing to do with each other - the
    approval card, the task list, the child view, and every later listing
    of a finished run - and an enumeration of rendering components is an
    overfit to today's front end: it is wrong the first time somebody
    adds a fifth. The store port is where the text stops being a local
    and starts being something every later reader inherits.

    Until this existed, the same task title was masked in the transcript
    (``task_narration`` goes through :func:`_emit_narration`) and shown
    verbatim on the plan card - one string, two answers, in front of the
    same reader.

    Its SCOPE, because a guard that cannot fire looks exactly like one
    that found nothing. The identifier list comes from the run's ledger,
    which is merged in ``_node_evidence``, after ``_node_plan``: on plan
    ROUND 0 the list is empty and this masks nothing at all.

    What the planner can copy on round 0 is therefore not held here. The
    discovery digest names sources by title and collection and adds no id
    FIELD of its own (pinned by ``tests/agents/test_discovery_digest.py::
    test_digest_names_title_and_collection_inside_the_fence_without_ids``)
    — but it adds none, it does not STRIP one: a document whose own title
    contains an id-shaped string hands that string to the analyst and the
    planner. And the digest is not all the planner reads: its prompt lists
    the released knowledge collections by name AND id, because a task's
    ``params.collection_ids`` must carry ids. No list this function checks
    contains those collection ids, on any round.

    So this binding earns its keep from the REPLAN rounds, where the
    planner is handed a populated ledger and writes the next version's
    titles from it. A round-0 plan card that carries no marker is
    therefore not evidence that this works; only a plan version with a
    ``reason`` says which round was seen.
    """
    return (
        replace(
            plan,
            summary_markdown=_guard_reader_text(state, plan.summary_markdown),
        ),
        [_guarded_task(state, task) for task in tasks],
    )


def _guarded_task(state: AgentPhaseState, task: PlanTaskRecord) -> PlanTaskRecord:
    """One task's reader text as :func:`_guarded_plan` stores it."""
    return replace(
        task,
        title=_guard_reader_text(state, task.title),
        objective=_guard_reader_text(state, task.objective),
    )


def _add_usage(state: AgentPhaseState, delta: dict[str, int]) -> None:
    usage = dict(state.get("usage", {}))
    usage["prompt_tokens"] = usage.get("prompt_tokens", 0) + int(
        delta.get("prompt_tokens", 0) or 0
    )
    usage["completion_tokens"] = usage.get("completion_tokens", 0) + int(
        delta.get("completion_tokens", 0) or 0
    )
    state["usage"] = usage
    deps = _DEPS.get()
    if deps is None:
        # Pure helper tests call phase-finalization functions without a
        # running graph. Production graph execution always installs deps.
        return
    budget = int(deps.context.token_budget or 0)
    used = usage["prompt_tokens"] + usage["completion_tokens"]
    if budget and used >= budget:
        log.warning(
            "Workspace agent stopped by server-side token budget: %d/%d tokens.",
            used,
            budget,
        )
        deps.emit(
            LIMIT_REACHED_EVENT,
            {
                "kind": "tokens",
                "used": used,
                "limit": budget,
                "ceiling": budget,
                "extendable": False,
                "recoverable": False,
                "reason": "operator_ceiling_exactly_once_required",
                "state": "failed_at_node_boundary",
            },
        )
        _emit_narration(
            deps,
            state,
            narration_id="n-limit-tokens",
            kind="limit",
            text=t(state, "agent_limit_tokens_narration"),
            phase=str(state.get("phase", "execution") or "execution"),
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "scope": "run",
                "phase": str(state.get("phase", "")),
                "operation": "run.token_budget",
                "detail": t(state, "agent_limit_tokens_detail"),
                "status": "failed",
                "error": {
                    "code": "token_budget_exceeded",
                    "message": "Server-side token budget reached.",
                },
            },
        )
        raise AgentTokenBudgetExceeded(
            "Run stopped by token budget (max_tokens_per_run).",
            usage=usage,
        )


def _set_phase(state: AgentPhaseState, phase: str) -> None:
    """THE one phase transition: state + event + snapshot."""
    previous = state.get("phase", "")
    state["phase"] = phase
    _deps().emit(
        PHASE_CHANGED_EVENT,
        {
            "phase": phase,
            "previous_phase": previous,
            "snapshot": {
                "current_node": phase,
                "phase": phase,
                "execution": _workspace_execution_payload(_deps(), state),
            },
        },
    )


def _load_memory_briefing(
    deps: "_RunDeps", state: AgentPhaseState
) -> None:
    """Load non-evidentiary long-term memory context for this run."""
    service = deps.memory
    principal = deps.context.principal
    if service is None or principal is None:
        state["memory_status"] = "disabled"
        return
    if not deps.agent_memory_opt_in:
        # Long-term memory is opt-in per user (privacy default OFF): with no
        # opt-in the agent recalls nothing. The choice stays visible via the
        # settings toggle and the "disabled" status, never silently.
        state["memory_status"] = "disabled"
        return
    status = service.status(principal)
    if not status.get("available") or not status.get("principal_eligible"):
        state["memory_status"] = "disabled"
        return
    try:
        briefing, memory_status = _run_async(
            service.recall_briefing(
                principal=principal,
                query=state.get("question", ""),
                limit=5,
            )
        )
    except AgentMemoryUnavailable:
        briefing, memory_status = "", "unavailable"
    state["memory_briefing"] = briefing
    state["memory_status"] = memory_status
    label = t(
        state,
        "agent_memory_label_"
        + (memory_status if memory_status in {"used", "unavailable"} else "empty"),
    )
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": (
                "memory_unavailable"
                if memory_status == "unavailable"
                else "memory"
            ),
            "label": label,
            "status": memory_status,
            "detail": (
                t(state, "agent_memory_detail_used")
                if memory_status == "used"
                else t(state, "agent_memory_detail_unavailable")
                if memory_status == "unavailable"
                else ""
            ),
        },
    )


# -- nodes ------------------------------------------------------------------ #


def _language(state: AgentPhaseState) -> str | None:
    """The run's resolved answer language, ``None`` while unresolved."""
    return str(state.get("language") or "") or None


def _deps_language(deps: "_RunDeps") -> str | None:
    """The answer language for helpers that only hold the segment deps.

    Set by :func:`_set_mission_language` in the intake segment and restored
    from the phase checkpoint on resume, including an unresolved value.
    Only a legacy segment without a saved language uses the text resolver.
    """
    if hasattr(deps, "language"):
        language = deps.language
        return str(language) if language else None
    request = getattr(deps, "request", None)
    question = str(getattr(request, "question", "") or "")
    return resolve_answer_language(question).code


def _set_mission_language(
    deps: "_RunDeps", state: AgentPhaseState, profile: Any
) -> None:
    """Resolve the answer language ONCE per run (classifier, text, unresolved).

    The intake's ``language`` field is the classifier; the text heuristic
    is the fallback; nothing defaults silently — an unresolved language
    leaves the writing prompts with the neutral directive and is logged.
    """
    classified = (
        profile.language if isinstance(profile, AssignmentProfile) else None
    )
    resolved = resolve_answer_language(state["question"], classified=classified)
    state["language"] = resolved.code or ""
    state["language_source"] = resolved.source
    deps.language = resolved.code
    if resolved.code:
        log.info(
            "answer language resolved: %s (source=%s)",
            resolved.code,
            resolved.source,
        )
    else:
        log.warning(
            "answer language unresolved: the writing prompts carry the "
            "neutral directive"
        )


def _clarification_cap(deps: "_RunDeps") -> int:
    """The run's clarification-round cap — the number the clarify node
    enforces is the number the snapshot publishes."""
    return effective_clarification_rounds(
        deps.platform.max_clarification_rounds, deps.tier_policy
    )


def _pinned_form(deps: "_RunDeps", document_id: str) -> str:
    """The output form the run's settings fix before the intake runs.

    ``""`` when nothing fixes it; the intake profile's inference then
    decides. The one edit point for the deliverable decision AND the
    clarification filter, so a form question is never asked while the
    form is fixed. Precedence: a patch target forces canvas, then an
    explicit request override, a skill's pinned deliverable, the tier's
    default (schnell -> chat, tief -> canvas).

    A skill may pin four values, but only two of them name a surface:
    `email` and `talking_points` are FORMS that live on the canvas. The
    fold is therefore correct — and it is not a silent one:
    `build_skills_block` states the pinned form in the prompt, so the
    author's choice survives the routing decision instead of evaporating
    in it.
    """
    if document_id:
        return "canvas"
    requested_form = (deps.request.response_form or "").strip()
    if requested_form in ("chat", "canvas"):
        return requested_form
    skill_deliverable = next(
        (
            "chat" if skill.deliverable == "chat" else "canvas"
            for skill in deps.skills
            if skill.deliverable
        ),
        "",
    )
    if skill_deliverable:
        return skill_deliverable
    if (
        deps.tier_policy is not None
        and deps.tier_policy.response_form != "auto"
    ):
        return deps.tier_policy.response_form
    return ""


def _run_settings_facts(
    deps: "_RunDeps", state: AgentPhaseState, *, with_names: bool
) -> RunSettingsFacts:
    """The run's fixed settings, recomputed from immutable inputs.

    Never checkpointed: the request, the admitted boundary and the tier
    are the same in every segment, so a resumed clarify node decides
    exactly as its first pass did. ``with_names`` reads the collection
    names for display; a boundary the actor can no longer see raises
    ``CollectionNotFound``, any other read failure is logged and leaves
    the names unknown (``None``) — never "no collections".
    """
    knowledge = (
        "off"
        if deps.source_policy.knowledge != "available"
        else knowledge_scope_of(deps.request.knowledge_filters)
    )
    boundary = deps.knowledge_collection_ids
    return RunSettingsFacts(
        knowledge=knowledge,  # type: ignore[arg-type]
        collection_count=None if boundary is None else len(boundary),
        collections=(
            _collection_labels(deps)
            if with_names and knowledge != "off"
            else None
        ),
        web=deps.source_policy.web == "available",
        pinned_form=_pinned_form(  # type: ignore[arg-type]
            deps, str(state.get("target_document_id") or "")
        ),
        tier=deps.tier,
        cap=_clarification_cap(deps),
        rounds_used=int(state.get("clarification_rounds", 0) or 0),
        autonomy=str(
            state.get("autonomy")
            or deps.request.autonomy
            or deps.platform.default_autonomy
        ),
    )


def _collection_labels(
    deps: "_RunDeps",
) -> tuple[tuple[str, int], ...] | None:
    """``(name, document_count)`` of the admitted collections, for display."""
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    try:
        catalog = _collection_catalog(deps)
    except CollectionNotFound:
        raise
    except Exception as exc:  # noqa: BLE001 — names are display, never a gate
        log.warning(
            "Knowledge collection names unavailable for the run settings "
            "(error_type=%s).",
            type(exc).__name__,
        )
        return None
    if catalog is None:
        log.warning(
            "Knowledge collection names unavailable for the run settings "
            "(no knowledge service)."
        )
        return None
    if len(catalog) != len(deps.collection_scope()):
        # A collection inside the boundary vanished from the listing (e.g.
        # its deletion began): the names are incomplete, so the prompt
        # states the boundary's count instead of claiming fewer.
        log.warning(
            "Knowledge collection names incomplete for the run settings "
            "(listed=%d, admitted=%d).",
            len(catalog),
            len(deps.collection_scope()),
        )
        return None
    return tuple((entry.name, entry.document_count) for entry in catalog)


def _collection_scope_or_fail(deps: "_RunDeps", state: AgentPhaseState) -> bool:
    """Resolve the run's admitted collections, or fail the run by type.

    A collection the actor can no longer see ends the run with the
    capability code ``knowledge.collection_not_found``; any other error
    goes through the shared execution-failure classifier. Runs before any
    spend in intake, and again in discovery and planning (a resumed
    segment re-checks visibility before it spends).
    """
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    try:
        deps.collection_scope()
    except CollectionNotFound as exc:
        log.warning(
            "Run knowledge boundary is not visible (error_type=%s).",
            type(exc).__name__,
        )
        state["failure"] = "knowledge.collection_not_found"
        return False
    except Exception as exc:  # noqa: BLE001 — every boundary error fails loudly
        log.warning(
            "Run knowledge boundary could not be resolved (error_type=%s).",
            type(exc).__name__,
        )
        state["failure"] = classify_execution_failure(exc)
        return False
    return True


def _node_intake(state: AgentPhaseState) -> AgentPhaseState:
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    _set_phase(state, "intake")
    # A patch assignment validates its TARGET here, before any spend: a
    # typo'd or invisible document must fail in seconds, not after the
    # whole research pipeline ran (and the failure path books nothing).
    document_id = state.get("target_document_id", "")
    if document_id:
        if deps.editor_patches is None or deps.editor_docs is None:
            state["failure"] = "editor_patches_unavailable"
            return state
        from inqtrix.project.editor_ports import DocumentNotFound

        try:
            _run_async(
                deps.editor_docs.get_document(
                    document_id, visible_to=deps.visible_to
                )
            )
        except DocumentNotFound:
            state["failure"] = "patch_document_not_found"
            return state
    # Session-memo lineage: a follow-up turn continues the ONE memo
    # canvas of its session. Read its LATEST revision now — including any
    # edit the user made after the previous turn — so synthesis extends it
    # and the write CAS-guards against that exact revision (never clobbers
    # the user's text).
    session_id = state.get("session_id", "")
    if session_id:
        prior = _run_async(
            deps.control.get_session_artifact(session_id, "memo")
        )
        if prior is not None:
            state["artifact_id"] = prior.artifact_id
            state["memo_base_revision"] = prior.revision
            state["prior_memo"] = prior.content_markdown
            state["memo_title"] = prior.title
    # Session metadata is always reconstructed. Explicit request history may
    # replace the composed history block, but it must not erase the rest of
    # the continuity contract: artifact registry, last response form and
    # prior evidence count.
    if session_id:
        pack = build_session_context(
            session_id,
            run_store=deps.run_service.run_store,
            control=deps.control,
            run_async=_run_async,
            visible_to=deps.visible_to,
            current_run_id=deps.context.run_id or "",
        )
        if pack.history_block and not str(state.get("history", "")).strip():
            state["history"] = pack.history_block
        if pack.artifact_registry:
            state["artifact_registry"] = [
                dict(item) for item in pack.artifact_registry
            ]
        if pack.last_response_form:
            state["last_response_form"] = pack.last_response_form
        state["prior_evidence_count"] = pack.prior_evidence_count
    if not _collection_scope_or_fail(deps, state):
        return state
    _load_memory_briefing(deps, state)
    model, effort = deps.resolved("agent_intake")
    outcome = run_intake(
        deps.llm,
        question=state["question"],
        history=state.get("history", ""),
        settings=_run_settings_facts(deps, state, with_names=True),
        language=resolve_answer_language(state["question"]).code,
        model=model,
        reasoning_effort=effort,
        timeout=deps.timeout,
        skills_block=_skills_prompt_block(deps, state),
        artifact_registry=list(state.get("artifact_registry") or []),
        last_response_form=str(state.get("last_response_form") or ""),
        prior_evidence_count=int(state.get("prior_evidence_count") or 0),
    )
    _add_usage(state, outcome.usage)
    profile = outcome.value
    state["profile"] = (
        profile.model_dump() if isinstance(profile, AssignmentProfile) else None
    )
    _set_mission_language(deps, state, profile)
    state["success_criteria"] = (
        list(profile.success_criteria)
        if isinstance(profile, AssignmentProfile)
        else []
    )
    state["route"] = route_readiness(
        profile if isinstance(profile, AssignmentProfile) else None
    )
    # Deliverable decision, ONCE per run: the form the settings pin
    # (patch target, request, skill, tier — see _pinned_form), then the
    # intake profile's inference; a failed intake stays on the
    # established canvas path (safe default = today's behavior).
    pinned_form = _pinned_form(deps, document_id)
    if pinned_form:
        state["deliverable"] = pinned_form
    elif (
        isinstance(profile, AssignmentProfile)
        and profile.response_form == "chat"
    ):
        state["deliverable"] = "chat"
    else:
        state["deliverable"] = "canvas"
    # Skill point check, once per run: what
    # question+history already answer never gets asked again; missing
    # REQUIRED points queue structured questions for the clarify gate.
    if deps.skills and "skill_point_answers" not in state:
        point_answers: dict[str, dict[str, str]] = {}
        skill_questions: list[dict[str, Any]] = []
        check_model, check_effort = deps.resolved("agent_skill_point_check")
        for skill in deps.skills:
            answers, usage = check_skill_points(
                deps.llm,
                skill=skill,
                question=state["question"],
                history=state.get("history", ""),
                model=check_model,
                reasoning_effort=check_effort,
                timeout=deps.timeout,
            )
            _add_usage(state, usage)
            point_answers[skill.id] = answers
            for point in unanswered_required_points(skill, answers):
                skill_questions.append(
                    {
                        "skill_id": skill.id,
                        "key": skill_point_key(point),
                        "name": str(point.get("name", "") or ""),
                        "prompt": str(point.get("question", "")),
                        "default_assumption": str(
                            point.get("default_assumption", "") or ""
                        ),
                        "options": [
                            {
                                "label": option.get("label", ""),
                                "description": option.get(
                                    "description", ""
                                ),
                            }
                            for option in point.get("options", [])
                        ],
                    }
                )
        state["skill_point_answers"] = point_answers
        state["skill_questions"] = skill_questions
        if skill_questions and state.get("route") != "ask_user_first":
            # Missing required inputs block exactly like an ambiguous
            # assignment: ask BEFORE any probe runs.
            state["route"] = "ask_user_first"
    return state


def _node_clarify(state: AgentPhaseState) -> AgentPhaseState:
    """Ask the user what the run's settings and earlier rounds leave open.

    ONE interrupt per round. The node re-executes from its start on
    resume, so every step before the interrupt computes the same result
    from checkpointed state and immutable inputs:

    1. the source's questions — the intake profile before discovery, the
       analyst's ``questions_for_user`` after it — uncapped;
    2. drop the ones the run's settings answer (each quoted, with its
       setting);
    3. drop the ones an earlier round asked or the run already assumed;
    4. append the pending skill points;
    5. nothing left: return without a phase change or an event;
    6. at the round cap: name every question with its assumption,
       record the assumptions and continue;
    7. otherwise ask the first :data:`MAX_QUESTIONS_PER_ROUND` and name
       the overflow like step 6.
    """
    from langgraph.types import interrupt

    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    rounds = int(state.get("clarification_rounds", 0) or 0)
    discovery = _discovery_of(state)
    source = "intake" if discovery is None else "discovery"
    if discovery is not None:
        questions = discovery_questions(discovery)
    elif state.get("route") == "ask_user_first":
        questions = intake_questions(_profile_of(state))
    else:
        questions = []
    run_id = deps.context.run_id or ""
    clarification_id = f"clr_{run_id[-12:]}_{rounds}"
    facts = _run_settings_facts(deps, state, with_names=False)
    questions, settled = filter_settled_questions(questions, facts)
    if settled:
        log.warning(
            "Clarification questions answered by the run settings were not "
            "asked (run=%s, source=%s, count=%d).",
            run_id,
            source,
            len(settled),
        )
        _emit_narration(
            deps,
            state,
            narration_id=f"n-clarify-settled-{source}-{rounds}",
            kind="discovery",
            text=settled_narration(
                settled, _named_settings(deps, state, facts), state
            ),
            phase="clarification",
        )
    if questions:
        # Deterministic backstop behind the analyst's never-re-ask rule:
        # a question that rephrases an already-asked round or an assumed
        # question is dropped LOUDLY. Both lists are checkpointed state
        # written only after a round completes, so an interrupt
        # re-execution filters against exactly what the first pass saw.
        handled_prompts = [
            *(state.get("clarification_asked") or []),
            *(
                str(entry.get("prompt", ""))
                for entry in state.get("clarification_assumptions") or []
            ),
        ]
        questions, dropped = filter_repeated_questions(
            questions, handled_prompts
        )
        if dropped:
            log.warning(
                "Clarification deduplication skipped previously handled entries "
                "(run=%s, count=%d).",
                run_id,
                len(dropped),
            )
            _emit_narration(
                deps,
                state,
                narration_id=f"n-clarify-skip-{source}-{rounds}",
                kind="discovery",
                text=t_count(state, "agent_clarify_skipped", len(dropped)),
                phase="clarification",
            )
    # Skill point questions join the same round — appended LAST, so the
    # answer mapping below finds them in the tail of the stored round.
    # Each carries its point's default assumption for the case the round
    # cap or the overflow keeps it from being asked.
    skill_questions = list(state.get("skill_questions") or [])
    if skill_questions:
        questions = [
            *questions,
            *sanitize_questions(
                [
                    ProposedClarificationQuestion(
                        prompt=str(item["prompt"]),
                        options=[
                            ClarificationOptionModel(
                                label=str(option.get("label", "")),
                                description=str(option.get("description", "")),
                            )
                            for option in item.get("options", [])
                        ],
                        multi_select=bool(item.get("multi_select")),
                        subject="assignment",
                        default_assumption=str(
                            item.get("default_assumption", "") or ""
                        ),
                    )
                    for item in skill_questions
                ],
                limit=None,
            ),
        ]
    if not questions:
        return state
    cap = facts.cap
    if rounds >= cap:
        reason = (
            "tier"
            if deps.tier_policy is not None
            and deps.tier_policy.clarification_rounds == 0
            else "limit"
        )
        entries = not_asked_assumptions(questions, reason)
        state["clarification_assumptions"] = [
            *(state.get("clarification_assumptions") or []),
            *entries,
        ]
        log.warning(
            "Clarification round cap reached; %d questions continue as "
            "assumptions (run=%s, source=%s, reason=%s, cap=%d).",
            len(entries),
            run_id,
            source,
            reason,
            cap,
        )
        deps.emit(
            LIMIT_REACHED_EVENT,
            {
                "kind": "clarification_rounds",
                "used": rounds,
                "limit": cap,
                "ceiling": cap,
                "extendable": False,
                "recoverable": False,
                "reason": "deterministic_mission_budget",
                "state": "continued_with_visible_assumption",
                "suppressed": len(entries),
            },
        )
        _emit_narration(
            deps,
            state,
            narration_id=f"n-clarify-not-asked-{source}-{rounds}",
            kind="limit",
            text=not_asked_narration(entries, reason, state),
            phase="clarification",
        )
        # The points fall back to their visible default assumptions.
        state["skill_questions"] = []
        return state
    overflow = questions[MAX_QUESTIONS_PER_ROUND:]
    questions = number_round(questions[:MAX_QUESTIONS_PER_ROUND])
    if overflow:
        entries = not_asked_assumptions(overflow, "overflow")
        state["clarification_assumptions"] = [
            *(state.get("clarification_assumptions") or []),
            *entries,
        ]
        log.warning(
            "Clarification round holds %d questions; %d continue as assumptions "
            "(run=%s, source=%s).",
            MAX_QUESTIONS_PER_ROUND,
            len(entries),
            run_id,
            source,
        )
        _emit_narration(
            deps,
            state,
            narration_id=f"n-clarify-not-asked-{source}-{rounds}",
            kind="limit",
            text=not_asked_narration(entries, "overflow", state),
            phase="clarification",
        )
    _set_phase(state, "clarification")
    try:
        record = _run_async(
            deps.control.get_clarification(run_id, clarification_id)
        )
    except ClarificationNotFound:
        record = _run_async(
            deps.control.create_clarification(
                build_clarification(
                    run_id,
                    questions=guarded_questions(
                        questions,
                        lambda text: _guard_reader_text(state, text),
                    ),
                    clarification_id=clarification_id,
                )
            )
        )
        deps.emit(
            CLARIFICATION_REQUESTED_EVENT,
            {
                "clarification_id": record.clarification_id,
                "question": record.question,
                "options": [dict(option) for option in record.options],
                "question_count": len(record.questions),
            },
        )
    decision = interrupt({"kind": "clarification", "id": clarification_id})
    state["clarification_rounds"] = rounds + 1
    # The STORED round is what the user saw; later rounds never re-ask it.
    state["clarification_asked"] = [
        *(state.get("clarification_asked") or []),
        *(
            prompt
            for question in record.questions
            if (prompt := str(question.get("prompt", "")).strip())
        ),
    ]
    subjects = answered_subjects(
        questions=questions,
        answers=decision.get("answers") or {},
        answer=str(decision.get("answer", "")),
        option_id=str(decision.get("option_id", "")),
    )
    if subjects:
        state["answered_subjects"] = sorted(
            {*(state.get("answered_subjects") or []), *subjects}
        )
    history_block = _clarification_history_block(record, decision)
    if history_block:
        state["history"] = state.get("history", "") + history_block
        # Compact answer text for the DETERMINISTIC discovery prober. It
        # reaches only the probes built from the question itself — the
        # fallback search of an intake without content questions and the
        # web preview; content questions run as the intake phrased them.
        # Per Q/A block (blocks are blank-line separated) with newlines
        # collapsed — a multi-line free-text answer keeps ALL its lines.
        answers_text = " ".join(
            " ".join(block.split("Answer: ", 1)[1].split())
            for block in history_block.split("\n\n")
            if "Answer: " in block
        ).strip()
        if answers_text:
            state["clarified_context"] = (
                f"{state.get('clarified_context', '')} {answers_text}"
            ).strip()
    if skill_questions:
        # Map answers back onto the skill points from the STORED round:
        # the persisted row is the layout the answer ids refer to, and the
        # round's overflow may have cut skill points from its end. A
        # whole-round free-text answer cannot be attributed per
        # point — it rides the history instead and the points fall back to
        # their visible default assumptions, like the ones the round did
        # not ask.
        answers_map = decision.get("answers") or {}
        point_answers = dict(state.get("skill_point_answers") or {})
        for item, stored in _asked_skill_questions(
            list(record.questions), skill_questions
        ):
            entry = answers_map.get(str(stored.get("id", ""))) or {}
            labels = {
                option["id"]: option["label"]
                for option in stored.get("options", [])
            }
            parts = [
                labels[oid]
                for oid in entry.get("option_ids", [])
                if oid in labels
            ]
            text = str(entry.get("text", "") or "").strip()
            if text:
                parts.append(text)
            if not parts:
                continue
            per_skill = dict(point_answers.get(item["skill_id"]) or {})
            # The canonical point key (skill_point_key) — the reader in
            # skill_input_lines resolves the SAME key, so answers to
            # free (nameless) points reach the prompt too.
            per_skill[item.get("key") or item["name"]] = ", ".join(parts)
            point_answers[item["skill_id"]] = per_skill
        state["skill_point_answers"] = point_answers
        # The round is answered — never re-ask the same points.
        state["skill_questions"] = []
    return state


def _named_settings(
    deps: "_RunDeps", state: AgentPhaseState, facts: RunSettingsFacts
) -> RunSettingsFacts:
    """*facts* with the collection names, for the transcript line only.

    The settled decision never reads names; a boundary that is no longer
    visible leaves the line with the count, and the next node's boundary
    check (discovery or plan, :func:`_collection_scope_or_fail`) fails
    the run with its code.
    """
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    try:
        return _run_settings_facts(deps, state, with_names=True)
    except CollectionNotFound as exc:
        log.warning(
            "Collection names for the clarification transcript unavailable "
            "(error_type=%s).",
            type(exc).__name__,
        )
        return facts


def _asked_skill_questions(
    stored_questions: list[dict[str, Any]],
    skill_questions: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(skill point, stored question)`` pairs of the points a round asked.

    Skill points are appended last and a round's overflow cuts from the
    end, so the asked points are a prefix of the askable ones and sit in
    the tail of the stored round, in order. Matched by prompt against the
    stored row, never by the recomputed list.
    """
    askable = [
        item for item in skill_questions if str(item.get("prompt", "")).strip()
    ]
    for count in range(min(len(askable), len(stored_questions)), 0, -1):
        tail = stored_questions[len(stored_questions) - count:]
        if [str(question.get("prompt", "")) for question in tail] == [
            str(item["prompt"]).strip() for item in askable[:count]
        ]:
            return list(zip(askable[:count], tail))
    return []


def _clarification_history_block(
    record: Any, decision: dict[str, Any]
) -> str:
    """The deterministic Q/A transcript block of one answered round.

    Composition lives in :func:`round_qa_lines` (shared with the
    session-context builder). An unanswered round contributes nothing.
    """
    lines = round_qa_lines(
        questions=list(record.questions),
        question=record.question,
        options=list(record.options),
        answers=decision.get("answers") or {},
        answer=str(decision.get("answer", "")),
        option_id=str(decision.get("option_id", "")),
    )
    if not lines:
        return ""
    return "\n\n" + "\n\n".join(
        f"Clarification: {prompt}\nAnswer: {answer}" for prompt, answer in lines
    )


def _node_discovery(state: AgentPhaseState) -> AgentPhaseState:
    from langgraph.types import interrupt

    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    _set_phase(state, "discovery")
    profile = _profile_of(state)
    if not _collection_scope_or_fail(deps, state):
        return state
    collection_ids = deps.collection_scope()
    plan = build_probe_plan(
        profile,
        question=state["question"],
        clarified_context=state.get("clarified_context", ""),
        collection_ids=collection_ids,
        max_calls=deps.platform.discovery_max_tool_calls,
        # Standard mode (balanced) keeps ALL web contact behind the plan
        # gate (the tasks carry their verbatim queries — that is the
        # consent surface); only Auto (autonomous) may probe the web
        # during discovery in autonomous mode. Internal probes
        # stay available in every mode.
        # The probe sends the question verbatim: only a question the user
        # wrote. A delegated mission's question is a model's text written
        # after reading project documents; its web contact goes through
        # planned tasks, which carry the external-search rule.
        web_preview_allowed=(
            deps.platform.allow_web_discovery_preview
            and deps.capabilities is not None
            and state.get("autonomy") == "autonomous"
            and deps.source_policy.web == "available"
            and not state.get("delegated")
        ),
        knowledge_allowed=(
            deps.source_policy.knowledge == "available"
        ),
        user_chose_sources=_user_demanded_web(deps, edit_consent=False),
    )
    if plan.omitted_count > 0:
        deps.emit(
            LIMIT_REACHED_EVENT,
            {
                "kind": "discovery_tool_calls",
                "used": len(plan.probes),
                "limit": plan.limit,
                "ceiling": plan.limit,
                "extendable": False,
                "recoverable": False,
                "omitted": plan.omitted_count,
                "reason": "deterministic_discovery_budget",
                "state": "continued_with_visible_omission",
            },
        )
        _emit_narration(
            deps,
            state,
            narration_id="n-discovery-limit",
            kind="limit",
            text=t_count(
                state,
                "agent_discovery_limit_narration",
                plan.omitted_count,
                limit=plan.limit,
            ),
            phase="discovery",
        )

    if state.get("autonomy") == "strict":
        run_id = deps.context.run_id or ""
        approval_id = f"apr_{run_id[-12:]}_discovery_0"
        _ensure_approval(
            deps,
            run_id,
            approval_id,
            kind="discovery",
            payload={"probes": plan.as_payload()},
        )
        decision = interrupt({"kind": "approval", "id": approval_id})
        if decision.get("decision") == "reject":
            # Same contract as the plan gate: a rejected gate is a clean
            # user decision, not a run failure.
            _finish_with_gate_rejection(
                deps,
                state,
                decision=decision,
                gate_subject_key="discovery",
                narration_id="n-discovery-rejected",
                phase="discovery",
            )
            return state

    if deps.capabilities is None:
        log.warning(
            "Discovery ran without a capability registry "
            "(planned_probes=%d); nothing was searched.",
            len(plan.probes),
        )
        digest = "(no source tools available)"
        source_counts = {"web": 0, "knowledge": 0}
    else:
        capability_context = _capability_context(
            deps,
            on_provider_retry=lambda notice: deps.emit(
                ACTIVITY_EVENT,
                provider_retry_activity(
                    notice,
                    purpose=t(
                        {"language": _deps_language(deps) or ""},
                        "agent_discovery_web_preview_purpose",
                    ),
                    scope="discovery",
                    phase="discovery",
                    language=_deps_language(deps) or "",
                ),
            ),
        )
        operation_totals: dict[str, int] = {}
        for probe in plan.probes:
            operation = str(probe.get("kind", ""))
            operation_totals[operation] = (
                operation_totals.get(operation, 0) + 1
            )
        operation_current: dict[str, int] = {}

        def _invoke_probe(
            capability_id: str, payload: dict[str, Any]
        ) -> Any:
            current = operation_current.get(capability_id, 0) + 1
            operation_current[capability_id] = current
            event = {
                "kind": "searching",
                "scope": "discovery",
                "phase": "discovery",
                "operation": capability_id,
                "detail": str(payload.get("query", "")),
                "status": "started",
                "current": current,
                "total": operation_totals.get(capability_id, 1),
                **(
                    {"query": str(payload.get("query", ""))}
                    if payload.get("query")
                    else {}
                ),
            }
            deps.emit(ACTIVITY_EVENT, event)
            try:
                output = _run_async(
                    deps.capabilities.invoke(
                        capability_id, payload, capability_context
                    )
                )
            except Exception as exc:
                deps.emit(
                    ACTIVITY_EVENT,
                    {
                        **event,
                        "status": "failed",
                        "error": {
                            "code": _task_failure_code(exc),
                            "message": sanitize_error(exc),
                        },
                    },
                )
                raise
            deps.emit(
                ACTIVITY_EVENT,
                {
                    **event,
                    "status": "completed",
                    "metrics": {"result_count": _result_count(output)},
                },
            )
            return output

        digest, source_counts = execute_probes(
            plan,
            registry=deps.capabilities,
            capability_context=capability_context,
            invoke=_invoke_probe,
        )
    state["discovery_tool_use_counts"] = dict(source_counts)
    state["tool_use_counts"] = dict(state["discovery_tool_use_counts"])
    # Every web discovery probe is one instant search.
    state["web_searches"] = _mission_web_searches([], {}, base_counts=state["discovery_tool_use_counts"])
    model, effort = deps.resolved("agent_discovery_analyst")
    outcome = run_discovery_analysis(
        deps.llm,
        question=state["question"],
        probe_digest=digest,
        language=_language(state),
        plan=plan,
        settings=_run_settings_facts(deps, state, with_names=True),
        assumptions=list(state.get("clarification_assumptions") or []),
        model=model,
        reasoning_effort=effort,
        timeout=deps.timeout,
        history=state.get("history", ""),
    )
    _add_usage(state, outcome.usage)
    discovery = outcome.value
    state["discovery"] = (
        discovery.model_dump()
        if isinstance(discovery, DiscoveryResult)
        else None
    )
    _emit_narration(
        deps,
        state,
        narration_id="n-discovery",
        kind="discovery",
        text=discovery_narration(
            discovery if isinstance(discovery, DiscoveryResult) else None,
            language=state["language"],
        ),
        phase="discovery",
    )
    return state


def _skills_prompt_block(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    scope: str = "document",
) -> str:
    """The combined skill + tool-directive prompt section of one run.

    ``scope="section"`` is for the per-section writer: it writes a PART
    of the deliverable, so a pinned format contributes its register but
    not its envelope. With the whole-deliverable wording a four-section
    memo became four complete emails — four subject lines, four
    salutations, four closings — assembled under the memo's headings.
    """
    parts: list[str] = []
    directives = build_tool_directives_line(
        deps.request.tool_directives or ()
    )
    if directives:
        parts.append(directives)
    block = build_skills_block(
        deps.skills,
        dict(state.get("skill_point_answers") or {}),
        scope=scope,
    )
    if block:
        parts.append(block)
    return "\n\n".join(parts)


def _node_plan(state: AgentPhaseState) -> AgentPhaseState:
    """Plan + validate + persist (NO interrupt here: the node's state
    writes must COMMIT — an interrupt in the same node would discard
    them, losing the planner's token usage; the approval lives in
    :func:`_node_plan_approval`)."""
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    if state.get("route") == "critic_research" and not critic_phase.uncovered_criteria(
        state.get("critic"), kind="information",
    ):
        log.warning("Critic follow-up research ended without a classified factual gap.")
        state["route"] = "critic_research_exhausted"
        _end_critic_research(
            deps,
            state,
            "no_gap",
            label=t(state, "agent_critic_no_gap_label"),
            detail=t(state, "agent_critic_no_gap_detail"),
        )
        return state
    if not _collection_scope_or_fail(deps, state):
        return state
    _set_phase(state, "planning")
    run_id = deps.context.run_id or ""
    profile = _profile_of(state)
    discovery = _discovery_of(state)
    replan_round = state.get("replan_rounds", 0)
    autonomy = state.get("autonomy", "balanced")

    # Round marker (NOT the plan version: user edits append versions of
    # their own, which must never make a later replan a silent no-op).
    if state.get("planned_rounds", 0) <= replan_round:
        previous_plan: PlanRecord | None = None
        previous_tasks: list[PlanTaskRecord] = []
        # The tasks that ran before this round: the plan-time check reads
        # their readings, and this round's plan version records them, so
        # plan time and execution read one set.
        before = sorted(state.get("outcomes") or {}) if replan_round > 0 else []
        # New file analyses of this replan the plan-time check dropped.
        rejected: list[str] = []
        drop: Callable[[PlanTaskRecord], bool] | None = None
        if replan_round > 0:
            previous_plan, previous_tasks = _run_async(
                deps.control.get_plan(run_id)
            )
            view = _reading_view(state, previous_tasks, task_ids=before)
            # The prospect's own number: what the recorded outcomes left.
            windows_left = _run_windows_left(deps, state.get("outcomes"))

            def drop_if_rejected(task: PlanTaskRecord) -> bool:
                if _rejects_new_analysis(
                    deps,
                    state,
                    task,
                    view=view,
                    windows_left=windows_left,
                    round_=replan_round,
                ):
                    rejected.append(task.task_id)
                    return True
                return False

            drop = drop_if_rejected

        edit_consent = _plan_lineage_web_consent(
            deps,
            state,
            current_plan=previous_plan,
            current_tasks=previous_tasks,
        )
        sources_answered = _sources_answered(state)
        web_needed = assignment_needs_web(
            profile,
            user_chose_sources=(
                _user_demanded_web(deps, edit_consent=edit_consent)
                or sources_answered
            ),
        )
        model, effort = deps.resolved("agent_plan")
        try:
            plan, usage = run_planner(
                deps.llm,
                question=state["question"],
                discovery=discovery,
                criteria=_plan_criteria(state),
                covered_gap_ids=_covered_gap_ids(
                    previous_tasks, _outcomes_from_state(state)
                ),
                drop_new_task=drop,
                max_tasks=deps.platform.max_plan_tasks,
                web_allowed=_web_allowed(deps),
                web_needed=web_needed,
                sources_answered=sources_answered,
                knowledge_allowed=(
                    deps.source_policy.knowledge == "available"
                ),
                model=model,
                reasoning_effort=effort,
                timeout=deps.timeout,
                replan_context=_build_replan_context(state),
                memory_briefing=state.get("memory_briefing", ""),
                collection_catalog=_collection_catalog(deps),
                language=_language(state),
                skills_block=_skills_prompt_block(deps, state),
                allowed_task_kinds=allowed_task_kinds_for_policy(
                    allowed_task_kinds(deps.skills),
                    policy=deps.source_policy,
                ),
                depth=deps.depth,
                explicit_web_research=(
                    "web_research" in deps.request.tool_directives
                    or edit_consent
                ),
                previous_plan=previous_plan,
                previous_tasks=previous_tasks,
                history=state.get("history", ""),
                document_analysis_limits=DocumentAnalysisLimits(
                    documents=int(deps.platform.document_analysis_max_documents),
                    windows=int(deps.platform.document_analysis_max_windows),
                    window_step=WINDOW_STEP_CHARS,
                ),
                tier=deps.tier or None,
                run_assumptions=list(
                    state.get("clarification_assumptions") or []
                ),
            )
        except PlanningFailed as exc:
            # A failed planner attempt is still spend.
            _add_usage(state, exc.usage)
            detail = "; ".join(exc.errors)
            if replan_round <= 0:
                state["failure"] = f"plan_invalid: {detail}"
                return state
            # Round >= 1: the evidence already gathered outranks a replan
            # that could not be validated. Proceed VISIBLY with the last
            # valid plan version instead of losing the run — or, on the
            # critic route, keep the memo the critic was judging.
            log.warning(
                "Replan round %d discarded (%s); the run continues with plan version "
                "%s.",
                replan_round,
                detail,
                previous_plan.version if previous_plan else "?",
            )
            # The label says what happened and how the run goes on. The
            # planner's validation errors are no reader text: they travel as
            # machine data and stand in the log above.
            deps.emit(
                ACTIVITY_EVENT,
                {
                    "errors": list(exc.errors),
                    "kind": "replan_invalid",
                    "label": t(state, "agent_replan_invalid_label"),
                },
            )
            if state.get("route") == "critic_research":
                state["route"] = "critic_research_exhausted"
                _end_critic_research(
                    deps,
                    state,
                    "replan_invalid",
                    label=t(state, "agent_replan_invalid_label"),
                    detail=t(state, "agent_critic_replan_invalid_detail"),
                )
            else:
                state["route"] = "synthesize"
            return state
        _add_usage(state, usage)
        if replan_round == 0:
            # The questions the run did not ask stand in the first plan
            # version as its assumptions, verbatim — the plan approval is
            # where the user sees and can correct them. Replans keep them
            # (merge_replan_delta starts from the previous assumptions).
            plan = _with_run_assumptions(
                plan, state.get("clarification_assumptions") or [], state
            )
        # The web was open and the plan leaves it out: the plan says so in
        # the server's words, in every round, and stops saying it once the
        # user chose the web (a replan copies the previous assumptions).
        plan = _with_server_assumption(
            plan,
            t(state, WEB_NOT_NEEDED_NOTE_KEY),
            present=_web_allowed(deps) and not web_needed,
        )
        if replan_round == 0 and not plan.success_criteria and state.get("success_criteria"):
            # The review judges the run's criteria: the first plan version
            # states them, so its approval shows what the review will use.
            log.warning(
                "Plan: the planner named no success criteria; the %d criteria of "
                "the assignment stand in plan version 1.",
                len(state["success_criteria"]),
            )
            plan = plan.model_copy(
                update={"success_criteria": list(state["success_criteria"])}
            )
        # Every round: a replan's merge carries the previous version's list.
        state["success_criteria"] = list(plan.success_criteria)
        reason = (
            ""
            if replan_round == 0
            else "critic_research"
            if state.get("route") == "critic_research"
            else "replan"
        )
        # Checked HERE, where the planner's output becomes records, so
        # that every later reader of them inherits the same text: the
        # approval card, the replan gate's new-task list, the plan
        # narration, the task list and every listing of the finished run.
        record, tasks = _guarded_plan(
            state,
            *plan_to_records(
                plan,
                run_id=run_id,
                created_by="agent",
                reason=reason,
                previous_tasks=previous_tasks,
            ),
        )
        # requires_plan matrix (strictest wins): `always`
        # forces the gate in EVERY mode and round (the one way a skill
        # author reins in Auto), `never` frees only ROUND 0 — the
        # web-replan re-gate is a security consent no skill may lift.
        skill_plan_policy = strictest_requires_plan(deps.skills)
        needs_interrupt = autonomy != "autonomous" and replan_round == 0
        if (
            replan_round == 0
            and deps.tier_policy is not None
            and deps.tier_policy.plan_gate == "skip_unless_strict"
        ):
            # Speed tier: no gate unless the PERMISSION dimension
            # (strict) demands one — permission always beats speed.
            needs_interrupt = autonomy == "strict"
        if replan_round == 0 and state.get("delegated"):
            # A delegated child's round-0 plan was already consented to:
            # whoever approved dispatching this run has seen its objective
            # before dispatching it. Permission still beats
            # delegation — strict keeps the gate — and replans re-gate
            # per delta below, because their content is new information
            # the delegation approval never covered.
            needs_interrupt = autonomy == "strict"
        if replan_round == 0:
            if skill_plan_policy == "always":
                needs_interrupt = True
            elif skill_plan_policy == "never":
                needs_interrupt = False
        if replan_round > 0:
            # The replan policy judges the DELTA: only tasks that
            # were not executed before count as new (deltas are
            # additive, results never discarded).
            previously_executed = set(state.get("outcomes", {}))
            new_tasks = [
                task
                for task in tasks
                if task.task_id not in previously_executed
                and task.tool_kind != "synthesis"
            ]
            previous_source_ids = {
                task.task_id
                for task in previous_tasks
                if task.tool_kind != "synthesis"
            }
            current_source_ids = {
                task.task_id
                for task in tasks
                if task.tool_kind != "synthesis"
            }
            state["replan_noop"] = (
                not new_tasks
                and current_source_ids == previous_source_ids
            )
            # What the replan gate will SHOW: the reason and the not yet
            # executed source tasks of this version — the card used to
            # know only plan_id/version and said "start" for a plan that
            # was being extended.
            state["replan_gate"] = {
                "reason": reason,
                "new_tasks": [
                    {
                        "task_id": task.task_id,
                        "ordinal": task.ordinal,
                        "title": task.title,
                        "tool_kind": task.tool_kind,
                    }
                    for task in new_tasks
                ],
            }
            needs_interrupt = not autonomy_auto_approves(
                autonomy=autonomy, new_tasks=new_tasks
            )
            if state["replan_noop"]:
                # An explicit no-op delta means "proceed with the known gap".
                # It neither creates new external contact nor benefits from a
                # human gate; route straight to synthesis after persisting the
                # auditable plan version.
                needs_interrupt = False
                if state.get("route") == "critic_research":
                    # The critic asked for more evidence and the replan
                    # produced no new task: the memo it judged stands with
                    # the named gap — re-synthesizing unchanged evidence
                    # would only rewrite the same memo.
                    state["route"] = "critic_research_exhausted"
                    _end_critic_research(
                        deps,
                        state,
                        "replan_empty",
                        label=t(state, "agent_critic_cap_label"),
                        detail=t(
                            state,
                            "agent_critic_replan_all_dropped_detail"
                            if rejected
                            else "agent_critic_cap_replan_empty_detail",
                        ),
                    )
            if skill_plan_policy == "always":
                needs_interrupt = not state["replan_noop"]
        else:
            state["replan_noop"] = False
        record = replace(
            record, status="proposed" if needs_interrupt else "approved"
        )
        saved = _run_async(
            deps.control.save_plan(run_id=run_id, plan=record, tasks=tasks)
        )
        state["plan_id"] = saved.plan_id
        state["plan_version"] = saved.version
        state["planned_rounds"] = replan_round + 1
        state["tasks_before_round"] = before
        state["needs_plan_interrupt"] = needs_interrupt
        deps.emit(
            PLAN_PROPOSED_EVENT if replan_round == 0 else PLAN_REVISED_EVENT,
            {
                "plan_id": saved.plan_id,
                "version": saved.version,
                "task_count": len(tasks),
                **(
                    {"reason": reason, "auto_approved": not needs_interrupt}
                    if replan_round
                    else {}
                ),
            },
        )
        _emit_narration(
            deps,
            state,
            narration_id=f"n-plan-{saved.version}",
            kind="plan",
            text=plan_narration(
                plan.summary_markdown,
                len(tasks),
                language=state["language"],
            ),
            phase="planning",
        )
    return state


def _with_run_assumptions(
    plan: ExecutionPlanModel,
    assumptions: list[dict[str, str]],
    state: AgentPhaseState,
) -> ExecutionPlanModel:
    """*plan* with every question the run did not ask in its assumptions.

    Each entry appears once: its assumption verbatim, or — when none was
    proposed — the question itself, named as open, so the plan approval
    lists every question the run continued past.
    """
    merged = list(plan.assumptions)
    for entry in assumptions:
        assumption = str(entry.get("assumption", "")).strip() or t(
            state,
            "agent_plan_assumption_unproposed",
            prompt=str(entry.get("prompt", "")).strip(),
        )
        if assumption not in merged:
            merged.append(assumption)
    if merged == list(plan.assumptions):
        return plan
    return plan.model_copy(update={"assumptions": merged})


def _with_server_assumption(
    plan: ExecutionPlanModel, assumption: str, *, present: bool
) -> ExecutionPlanModel:
    """*plan* stating one server assumption exactly once, or not at all."""
    kept = [item for item in plan.assumptions if item != assumption]
    wanted = [*kept, assumption] if present else kept
    if wanted == list(plan.assumptions):
        return plan
    return plan.model_copy(update={"assumptions": wanted})


def _finish_with_gate_rejection(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    decision: dict[str, Any],
    gate_subject_key: str,
    narration_id: str,
    phase: str,
) -> None:
    """Clean no-research finish for a rejected human gate.

    A gate rejection (plan, replan or discovery approval) is an ordered
    user decision, not a run failure: the run completes with a
    deterministic receipt in the run's answer language (no LLM call) as its
    chat answer, while
    the approval row keeps the durable ``rejected`` status. A rejected
    REPLAN that already synthesized a draft (critic verdict "research")
    keeps that draft as the deliverable instead — the pre-existing
    completed-with-draft behavior.

    Args:
        decision: Resume payload of the approval interrupt; ``note``
            carries the decider's free text and is echoed as a Markdown
            blockquote (newlines re-quoted so a multi-line note cannot
            escape the quote).
        gate_subject_key: Translation key suffix for the receipt's subject
            ("plan", "discovery") — resolved nominative so the same word
            works in title, receipt and narration.
        narration_id: Deterministic id in the established ``n-*`` scheme.
        phase: Station vocabulary value ("planning", "discovery").
    """
    state["plan_rejected"] = True
    subject = t(state, f"agent_gate_subject_{gate_subject_key}")
    if state.get("memo_markdown"):
        _emit_narration(
            deps,
            state,
            narration_id=narration_id,
            kind="plan",
            text=t(state, "agent_gate_rejected_draft_kept", subject=subject),
            phase=phase,
        )
        return
    note = str(decision.get("note", "") or "").strip()
    receipt = t(state, "agent_gate_rejected_receipt", subject=subject)
    if note:
        receipt += "\n\n" + t(state, "agent_gate_rejected_note_prefix") + (
            note.replace("\n", "\n> ")
        )
    receipt += t(state, "agent_gate_rejected_resubmit")
    state["deliverable"] = "chat"
    state["memo_markdown"] = receipt
    state["memo_title"] = t(
        state, "agent_gate_rejected_title", subject=subject
    )
    _emit_narration(
        deps,
        state,
        narration_id=narration_id,
        kind="plan",
        text=t(state, "agent_gate_rejected_no_research", subject=subject),
        phase=phase,
    )


def _node_plan_approval(state: AgentPhaseState) -> AgentPhaseState:
    """The plan approval interrupt (idempotent under re-execution)."""
    from langgraph.types import interrupt

    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    if not state.get("needs_plan_interrupt"):
        return state
    run_id = deps.context.run_id or ""
    replan_round = state.get("replan_rounds", 0)
    plan_id = str(state.get("plan_id", ""))
    if not plan_id:
        latest_plan, _tasks = _run_async(deps.control.get_plan(run_id))
        plan_id = latest_plan.plan_id
        state["plan_id"] = plan_id
    approval_id = f"apr_{run_id[-12:]}_plan_{replan_round}"
    replan_gate = state.get("replan_gate") or {}
    in_force = state.get("report_requirement_parts") or {}
    _ensure_approval(
        deps,
        run_id,
        approval_id,
        kind="plan" if replan_round == 0 else "replan",
        payload={
            "plan_id": plan_id,
            "plan_version": state.get("plan_version", 1),
            **(
                {
                    "reason": str(replan_gate.get("reason") or ""),
                    "new_tasks": list(replan_gate.get("new_tasks") or []),
                }
                if replan_round
                else {}
            ),
            # The requirement in force, by its parts and never its rule
            # bodies: the gate opens with it, and a decision that keeps a
            # rule keeps the revision the run is frozen under.
            **(
                {"report_requirement": dict(in_force)}
                if in_force.get("free_text") or in_force.get("rules")
                else {}
            ),
        },
    )
    decision = interrupt({"kind": "approval", "id": approval_id})
    if decision.get("decision") == "reject":
        if replan_round > 0 and state.get("route") == "critic_research":
            # The review asked for research and the user declined it: the
            # criteria it left open stay open for that reason.
            _end_critic_research(
                deps,
                state,
                "replan_rejected",
                label=t(state, "agent_critic_replan_rejected_label"),
                detail=t(state, "agent_critic_replan_rejected_detail"),
            )
        _finish_with_gate_rejection(
            deps,
            state,
            decision=decision,
            gate_subject_key="plan",
            narration_id=f"n-plan-rejected-{replan_round}",
            phase="planning",
        )
        return state
    if "report_guidance" in decision:
        # An absent key leaves an earlier guidance standing (a replan
        # approval without new requirements keeps the old ones); a
        # present key wins, including the empty string that clears them.
        state["report_guidance"] = str(
            decision.get("report_guidance") or ""
        ).strip()
        parts = decision["report_requirement"]
        state["report_requirement_parts"] = {
            "free_text": str(parts.get("free_text") or ""),
            "rules": [dict(rule) for rule in parts.get("rules") or []],
        }
    # approve OR edit: the control store holds the authoritative latest
    # version (an edit appended one) — reload.
    latest, latest_tasks = _run_async(deps.control.get_plan(run_id))
    _plan_lineage_web_consent(
        deps,
        state,
        current_plan=latest,
        current_tasks=latest_tasks,
    )
    state["plan_version"] = latest.version
    # The approved version's list, also when an edit emptied it: the user's
    # criteria are the ones the review judges.
    state["success_criteria"] = list(latest.success_criteria)
    return state


def _submitted_requirement_parts(
    request: "RunRequest", run_id: str
) -> dict[str, Any] | None:
    """The parts of the submitted requirement, as a gate decision stores them.

    ``None`` — with a WARNING — when the submitted text does not split
    into its stored parts: the run's gates then open without a prefill,
    exactly as before the parts were kept, and resolve every rule the
    user attaches again.
    """
    parts = requirement_parts(
        request.report_requirement or "", request.report_rules
    )
    if parts is None:
        log.warning(
            "Run %s: the submitted result requirement does not split into "
            "its %d rule part(s); its plan gates open without it.",
            run_id,
            len(request.report_rules),
        )
    return parts


def _outcomes_from_state(state: AgentPhaseState) -> dict[str, TaskOutcome]:
    """Rehydrate the checkpointed outcomes map, tolerating schema drift.

    One place for the read half of the durable-checkpoint round-trip (was
    an identical ``{k: TaskOutcome(**v) ...}`` comprehension in four nodes);
    routes through :meth:`TaskOutcome.from_state` so a schema change does not
    break resume of an in-flight run.
    """
    return {
        key: TaskOutcome.from_state(value)
        for key, value in state.get("outcomes", {}).items()
    }


def _outcomes_to_state(
    outcomes: dict[str, TaskOutcome],
) -> dict[str, dict[str, Any]]:
    """The checkpoint-serializable outcomes map (write half of the round-trip)."""
    return {key: outcome.to_state() for key, outcome in outcomes.items()}


def _source_tool_counts(
    tasks: list[PlanTaskRecord],
    outcomes: dict[str, TaskOutcome],
    *,
    base_counts: dict[str, int] | None = None,
) -> dict[str, int]:
    """Count completed source operations represented by task outcomes."""
    base = base_counts or {}
    counts = {
        "web": max(0, int(base.get("web", 0) or 0)),
        "knowledge": max(0, int(base.get("knowledge", 0) or 0)),
    }
    for task in tasks:
        outcome = outcomes.get(task.task_id)
        if outcome is None or outcome.status not in {
            "completed",
            "insufficient_evidence",
        }:
            continue
        width = max(1, len(_task_queries(task)))
        if task.tool_kind == "web_research":
            # One delegated web tool invocation; its internal query count is
            # intentionally owned by the child run's own execution block.
            counts["web"] += 1
        elif task.tool_kind == "web_instant":
            counts["web"] += width
        elif task.tool_kind in KNOWLEDGE_TASK_KINDS:
            counts["knowledge"] += width
    return counts


def _mission_web_searches(
    tasks: list[PlanTaskRecord],
    outcomes: dict[str, TaskOutcome],
    *,
    base_counts: dict[str, int] | None = None,
) -> int:
    """The web searches a mission ran itself, for the execution block.

    Its discovery probes and one per instant web task that completed (the
    plan admits exactly one question per such task). A research task is a
    delegation; its child reports the searches its provider ran.
    """
    searches = max(0, int((base_counts or {}).get("web", 0) or 0))
    for task in tasks:
        outcome = outcomes.get(task.task_id)
        if task.tool_kind == "web_instant" and outcome is not None and outcome.status in {
            "completed",
            "insufficient_evidence",
        }:
            searches += 1
    return searches


def _child_origin_key(task: PlanTaskRecord, attempt: int) -> str:
    """Stable child-submission identity across checkpoint re-execution."""
    return (
        f"mission-plan:{task.plan_id}:task:{task.task_id}:attempt:{attempt}"
    )


def _existing_child_for_attempt(
    deps: "_RunDeps", task: PlanTaskRecord, attempt: int
) -> dict[str, Any] | None:
    """Find the durable child for one logical task attempt, if submitted."""
    origin_key = _child_origin_key(task, attempt)
    return next(
        (
            row
            for row in deps.run_service.run_store.children(
                deps.context.run_id or ""
            )
            if row.get("origin_key") == origin_key
        ),
        None,
    )


def _attempt_for_child(
    task: PlanTaskRecord,
    child_run_id: str,
    pending_info: dict[str, Any] | None,
    children: list[dict[str, Any]],
) -> int:
    """Recover a child's logical attempt from checkpoint or origin key."""
    if pending_info and str(pending_info.get("child_run_id", "")) == child_run_id:
        return max(1, int(pending_info.get("attempt", 1) or 1))
    origins = {
        _child_origin_key(task, attempt): attempt for attempt in (1, 2)
    }
    for child in children:
        if str(child.get("run_id", "")) == child_run_id:
            return origins.get(str(child.get("origin_key", "")), 1)
    return 1


def _outcome_from_persisted_task(
    task: PlanTaskRecord,
    *,
    checkpointed: TaskOutcome | None = None,
    child_outcome: TaskOutcome | None = None,
) -> TaskOutcome:
    """Rebuild a complete outcome from row columns plus recovery payload."""
    checkpoint_matches = (
        checkpointed is not None and checkpointed.status == task.status
    )
    base = (
        checkpointed
        if checkpoint_matches and checkpointed is not None
        else TaskOutcome(status=task.status)
    )
    if (
        child_outcome is not None
        and task.status == child_outcome.status
        and task.status in {"completed", "insufficient_evidence"}
    ):
        base = child_outcome
    payload = task.result_payload or {}
    persisted_evidence = [
        dict(item)
        for item in payload.get("evidence", [])
        if isinstance(item, dict)
    ]
    persisted_claims = [
        dict(item)
        for item in payload.get("claims", [])
        if isinstance(item, dict)
    ]
    persisted_ledger = (
        dict(payload.get("web_search_ledger"))
        if isinstance(payload.get("web_search_ledger"), dict)
        else {}
    )
    persisted_usage_raw = payload.get("usage")
    persisted_usage = (
        {
            "prompt_tokens": int(
                persisted_usage_raw.get("prompt_tokens", 0) or 0
            ),
            "completion_tokens": int(
                persisted_usage_raw.get("completion_tokens", 0) or 0
            ),
        }
        if isinstance(persisted_usage_raw, dict)
        else {"prompt_tokens": 0, "completion_tokens": 0}
    )
    failure_reason = base.failure_reason
    failure_code = base.failure_code
    if task.status in {"failed", "cancelled"}:
        failure_reason = (
            failure_reason
            or str(payload.get("failure_reason") or "")
            or (task.result_summary if task.status == "failed" else "")
        )
        failure_code = (
            failure_code
            or str(payload.get("failure_code") or "")
            or (
                "persisted_task_failed"
                if task.status == "failed"
                else "task_cancelled"
            )
        )
    return TaskOutcome(
        status=task.status,
        summary=task.result_summary,
        answer_markdown=(
            base.answer_markdown
            or str(payload.get("answer_markdown") or "")
        ),
        evidence=list(base.evidence) or persisted_evidence,
        web_search_ledger=(
            dict(base.web_search_ledger) or persisted_ledger
        ),
        claims=list(base.claims) or persisted_claims,
        child_run_id=task.child_run_id or base.child_run_id,
        failure_reason=failure_reason,
        failure_code=failure_code,
        usage=(
            dict(base.usage)
            if checkpoint_matches or child_outcome is not None
            else persisted_usage
        ),
        transient=False,
        evidence_contract_status=(
            base.evidence_contract_status
            if base.evidence_contract_status != "unknown"
            else str(payload.get("evidence_contract_status") or "unknown")
        ),
        confidence=base.confidence or int(payload.get("confidence", 0) or 0),
        single_source_ratio=(
            base.single_source_ratio
            or float(payload.get("single_source_ratio", 0.0) or 0.0)
        ),
        omitted_evidence_count=(
            base.omitted_evidence_count
            or int(payload.get("omitted_evidence_count", 0) or 0)
        ),
        document_analysis=(
            dict(base.document_analysis)
            or (
                dict(payload["document_analysis"])
                if isinstance(payload.get("document_analysis"), dict)
                else {}
            )
        ),
        omitted_evidence_query_count=(
            base.omitted_evidence_query_count
            or int(payload.get("omitted_evidence_query_count", 0) or 0)
        ),
        search_notices=(
            list(base.search_notices) or list(payload.get("search_notices") or [])
        ),
    )


EVIDENCE_REVIEW_STATUSES = frozenset({"needs_review", "source_context_only"})
"""Child evidence-contract states the mission must carry forward as an
open point — the child capped its own confidence for them."""


_EVIDENCE_CONTRACT_KEYS: dict[str, str] = {
    "needs_review": "task_evidence_needs_review",
    "source_context_only": "task_evidence_source_context_only",
}
"""Reader wording (i18n key per contract status) of a capped evidence
contract. The memo's readers saw the raw status token before (``Task
t1: Evidenzlage needs_review``) — an internal check protocol in the
deliverable, which two independent reviews of real runs named as the one
line that cost trust."""

def _task_evidence_flagged(outcome: TaskOutcome) -> bool:
    """Whether a task's evidence must travel as an open point: a capped
    contract, a report that left sources unevaluated (partial coverage), a
    document analysis that did not establish everything it was asked to
    read (see :func:`~inqtrix.agents.document_analysis.analysis_notes`), or
    a knowledge search that reported a limit."""
    return (
        outcome.evidence_contract_status in EVIDENCE_REVIEW_STATUSES
        or outcome.omitted_evidence_count > 0
        or bool(
            outcome.document_analysis
            and analysis_notes(outcome.document_analysis, "en")
        )
        or bool(outcome.search_notices)
    )


def _analysis_covers_its_gaps(outcome: TaskOutcome) -> bool:
    """Whether a document analysis settles its discovery gaps (a routing
    decision, not a claim about the documents).

    It does when every limit of its coverage is a property of the stored
    documents — a page without a text layer, an unrecorded extraction — that
    every re-reading returns again. Any other gap in coverage (a part not
    read, a document not selected) keeps the gap open: passages from a
    partial reading prove what they say, never that nothing else is there.
    """
    record = outcome.document_analysis
    if not record:
        return True
    if not record.get("documents"):
        return False
    return all(
        blocker["source_bound"]
        for blocker in reading_blockers(record)
        if blocker["blocks"] == "coverage"
    )


def _coverage_words(outcome: TaskOutcome, language: str | None) -> str:
    """Reader wording of a partial evidence coverage ('' when complete)."""
    if outcome.omitted_evidence_count <= 0:
        return ""
    return t(
        {"language": language or ""},
        "task_evidence_partial_coverage",
        cut=evidence_cut_words(
            outcome.omitted_evidence_count,
            outcome.omitted_evidence_query_count,
            language=language or "en",
        ),
    )


def _task_label(task_id: str, title: str | None, words: dict[str, str]) -> str:
    """A subtask named by its plan title (planner or user text, fence
    delimiters defused); by its id only when the title is missing."""
    name = " ".join(str(title or "").split())
    if name:
        return t(words, "task_evidence_label_quoted", title=neutralize_fence_delimiters(name))
    log.warning("Evidence note of task %s has no plan title; naming it by its id.", task_id)
    return t(words, "task_evidence_label_id", task_id=task_id)


def _task_evidence_note(
    task_id: str,
    outcome: TaskOutcome,
    language: str | None,
    *,
    title: str | None,
    view: str = "limits",
) -> str:
    """One line per child, the same READER wording on every surface
    (memo prompts, review diagnostics, activity detail).

    No task ids, no status codes, no mechanics vocabulary: the reader
    learns which sub-task rests on thin evidence, how thin, whether the
    child read all of its sources, and how confident it was. A document
    analysis shows its server facts only, never its prose: *view*
    ``limits`` names what it did not establish, ``facts`` also what it read.
    """
    words = {"language": language or ""}
    label = _task_label(task_id, title, words)
    if outcome.document_analysis:
        render = analysis_facts if view == "facts" else analysis_notes
        return f"{label}: " + " ".join(render(outcome.document_analysis, language or ""))
    if outcome.search_notices:
        # An in-process search has no evidence contract or confidence of
        # its own: its limits are the whole note.
        return f"{label}: " + " ".join(
            search_notice_lines(stored_search_notices(outcome.search_notices), language or "")
        )
    parts: list[str] = []
    if outcome.evidence_contract_status in EVIDENCE_REVIEW_STATUSES or (
        outcome.omitted_evidence_count <= 0
    ):
        key = _EVIDENCE_CONTRACT_KEYS.get(outcome.evidence_contract_status)
        status = t(words, key) if key else outcome.evidence_contract_status
        ratio = float(outcome.single_source_ratio or 0.0)
        if ratio >= 0.995:
            share = t(words, "task_evidence_single_source_all")
        elif ratio > 0:
            share = t(
                words,
                "task_evidence_single_source_share",
                share=int(round(ratio * 100)),
            )
        else:
            share = ""
        parts.append(f"{status}{share}")
    coverage = _coverage_words(outcome, language)
    if coverage:
        parts.append(coverage)
    confidence = t(words, "task_evidence_confidence", confidence=outcome.confidence)
    return f"{label}: {', '.join(parts)}, {confidence}"


def _analysis_metrics(record: dict[str, Any]) -> dict[str, Any]:
    """Task-end counters of a document analysis (empty for other tasks)."""
    if not record:
        return {}
    documents = list(record.get("documents") or [])
    return {
        "document_windows": sum(int(doc.get("windows_planned") or 0) for doc in documents),
        "document_windows_analysed": sum(
            int(doc.get("windows_analysed") or 0) for doc in documents
        ),
        "document_quotes_cited": len(record.get("findings") or []),
        "document_quotes_rejected": sum(
            sum((doc.get("findings_rejected") or {}).values()) for doc in documents
        ),
        "document_pages_without_text": sum(
            len(doc.get("pages_without_text") or []) for doc in documents
        ),
        "document_coverage": "complete" if reading_predicates(record)[0] else "partial",
    }


def _task_evidence_notes(
    outcomes: dict[str, TaskOutcome],
    language: str | None,
    *,
    titles: dict[str, str],
    readings: bool,
) -> str:
    """The children's notes for a writing prompt or the review (may be empty).

    Flagged children always; a reading (:func:`~inqtrix.agents.
    document_analysis.has_reading`) only when *readings* — the writing
    prompts get what it read and its limits, the review gets the same facts
    as its own reading record instead.
    """
    lines: list[str] = []
    for task_id, outcome in sorted(outcomes.items()):
        record = outcome.document_analysis
        if has_reading(record):
            if readings:
                lines.append(
                    _task_evidence_note(task_id, outcome, language, title=titles.get(task_id), view="facts")
                )
            continue
        if _task_evidence_flagged(outcome):
            lines.append(_task_evidence_note(task_id, outcome, language, title=titles.get(task_id)))
    return "\n".join(lines)


def _cancelled_task_outcome(
    spent: TaskOutcome | None = None,
    *,
    language: str,
) -> TaskOutcome:
    """Canonical cancel projection that retains only consumed resources.

    A provider result that arrives after the authoritative task row entered
    ``cancel_requested`` must not become synthesis input.  Usage remains
    accountable, while evidence and prose from the superseded attempt stay
    outside the task-result projection (child runs keep their own audit
    lineage).  Treating late evidence as an ordinary cancelled-task result
    would let a user cancellation influence the final answer on resume.
    """
    return TaskOutcome(
        status="cancelled",
        summary=t(
            {"language": language or ""},
            "agent_task_summary_cancelled_by_user",
        ),
        child_run_id=spent.child_run_id if spent is not None else None,
        failure_code="task_cancelled",
        usage=dict(spent.usage) if spent is not None else {},
        # Document-analysis windows are a consumed resource like tokens: the
        # run's window budget keeps them after a park.
        document_analysis=(
            {"windows_granted": int(spent.document_analysis.get("windows_granted", 0) or 0)}
            if spent is not None and spent.document_analysis
            else {}
        ),
    )


def _current_plan_task(
    deps: "_RunDeps", task: PlanTaskRecord
) -> PlanTaskRecord:
    """Reload one task from the authoritative latest plan version."""
    _plan, tasks = _run_async(deps.control.get_plan(task.run_id))
    return next(
        row
        for row in tasks
        if row.plan_id == task.plan_id and row.task_id == task.task_id
    )


def _reconcile_persisted_execution(
    deps: "_RunDeps",
    state: AgentPhaseState,
    tasks: list[PlanTaskRecord],
    outcomes: dict[str, TaskOutcome],
    pending: dict[str, dict[str, Any]],
    *,
    research_policy: WebResearchPolicy,
) -> tuple[dict[str, TaskOutcome], dict[str, dict[str, Any]]]:
    """Reconcile checkpoint caches with task rows without repeating work.

    A terminal row is never reopened. A running child reattaches to its
    durable run (including the submit-before-checkpoint crash window). A
    running local task has an unknowable external outcome and therefore fails
    closed instead of executing twice.
    """
    children = deps.run_service.run_store.children(deps.context.run_id or "")
    for original in tasks:
        if original.tool_kind == "synthesis":
            continue
        task = original
        checkpointed = outcomes.get(task.task_id)
        info = pending.get(task.task_id)
        if task.status in TASK_TERMINAL_STATUSES:
            pending.pop(task.task_id, None)
            child_outcome: TaskOutcome | None = None
            if task.child_run_id:
                attempt = _attempt_for_child(
                    task, task.child_run_id, info, children
                )
                child_outcome = _child_task_outcome(
                    deps, task.child_run_id, attempt
                )
            recovered = _outcome_from_persisted_task(
                task,
                checkpointed=checkpointed,
                child_outcome=child_outcome,
            )
            outcomes[task.task_id] = recovered
            if checkpointed is None and any(recovered.usage.values()):
                # The row won the terminal-write -> checkpoint crash race.
                # Its usage has not reached state yet. A checkpointed outcome
                # already contributed to cumulative usage and must never be
                # charged again on resume.
                _add_usage(state, recovered.usage)
            continue
        if task.tool_kind != "web_research":
            pending.pop(task.task_id, None)
            if task.status == "cancel_requested":
                outcome = _cancelled_task_outcome(
                language=str(state.get("language") or "")
            )
                outcomes[task.task_id] = outcome
                _emit_task_ended(deps, state, task, task.task_id, outcome)
                continue
            if task.status == "running":
                outcome = TaskOutcome(
                    status="failed",
                    summary=t(state, "agent_task_summary_not_restarted"),
                    failure_reason="local_execution_interrupted",
                    failure_code="local_execution_interrupted",
                )
                outcomes[task.task_id] = outcome
                _emit_task_ended(deps, state, task, task.task_id, outcome)
            continue
        if task.status == "cancel_requested" and not (
            task.child_run_id or info
        ):
            outcome = _cancelled_task_outcome(
                language=str(state.get("language") or "")
            )
            outcomes[task.task_id] = outcome
            _emit_task_ended(deps, state, task, task.task_id, outcome)
            continue
        if task.status not in {"running", "cancel_requested"} and info is None:
            continue

        child_id = str(task.child_run_id or (info or {}).get("child_run_id") or "")
        attempt = max(1, int((info or {}).get("attempt", 1) or 1))
        if child_id:
            attempt = _attempt_for_child(task, child_id, info, children)
        else:
            # Prefer a later retry if it exists; otherwise submit-or-find the
            # logical attempt checkpointed before the crash (normally one).
            existing = _existing_child_for_attempt(deps, task, 2)
            if existing is not None:
                attempt = 2
            else:
                existing = _existing_child_for_attempt(deps, task, attempt)
            if existing is not None:
                child_id = str(existing["run_id"])
            else:
                try:
                    child_id = _submit_child_run(
                        deps,
                        state,
                        task,
                        attempt,
                        research_policy=research_policy,
                    )
                except Exception as exc:  # noqa: BLE001
                    outcome = TaskOutcome(
                        status="failed",
                        failure_reason=sanitize_error(exc),
                        failure_code=_task_failure_code(exc),
                    )
                    outcomes[task.task_id] = outcome
                    pending.pop(task.task_id, None)
                    _emit_task_ended(deps, state, task, task.task_id, outcome)
                    continue
        if task.status == "pending" or task.child_run_id != child_id:
            task = _transition_task(
                deps,
                task,
                status="running",
                child_run_id=child_id,
            )
        pending[task.task_id] = {
            "child_run_id": child_id,
            "attempt": attempt,
        }
    return outcomes, pending


def _fold_pending_children(
    deps: "_RunDeps",
    state: AgentPhaseState,
    tasks: list[PlanTaskRecord],
    outcomes: dict[str, TaskOutcome],
    pending: dict[str, dict[str, Any]],
    *,
    research_policy: WebResearchPolicy,
) -> tuple[dict[str, TaskOutcome], dict[str, dict[str, Any]]]:
    """Fold terminal children and retain only genuinely active attempts."""
    tasks_by_id = {task.task_id: task for task in tasks}
    still_pending: dict[str, dict[str, Any]] = {}
    for task_id, info in pending.items():
        child_id = str(info.get("child_run_id", ""))
        attempt = int(info.get("attempt", 1) or 1)
        task = tasks_by_id.get(task_id)
        outcome = _child_task_outcome(deps, child_id, attempt)
        if outcome is None:
            still_pending[task_id] = info
            continue
        if task is not None and task.status == "cancel_requested":
            outcome = _cancelled_task_outcome(
                outcome, language=str(state.get("language") or "")
            )
        if task is not None and should_retry(
            outcome, attempt=attempt, cancelled=deps.cancelled()
        ):
            log.warning(
                "Task %s failed transiently (failure_code=%s); retrying once with "
                "unchanged operator limits.",
                task_id,
                outcome.failure_code or "unknown",
            )
            try:
                retry_id = _submit_child_run(
                    deps,
                    state,
                    task,
                    2,
                    research_policy=research_policy,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Retry for agent task %s could not be submitted (error_type=%s).",
                    task_id,
                    type(exc).__name__,
                )
                outcomes[task_id] = outcome
                _emit_task_ended(deps, state, task, task_id, outcome)
                continue
            still_pending[task_id] = {
                "child_run_id": retry_id,
                "attempt": 2,
            }
            _transition_task(
                deps,
                task,
                status="running",
                child_run_id=retry_id,
            )
            continue
        outcomes[task_id] = outcome
        _emit_task_ended(deps, state, task, task_id, outcome)
    return outcomes, still_pending


def _node_execute(state: AgentPhaseState) -> AgentPhaseState:
    """Run the plan's waves — in-process tools inline, children parked.

    ``web_research`` tasks are SUBMITTED as child runs and awaited via
    the children_wait interrupt (the parent parks slot-free instead of
    block-polling siblings out of the shared execution pool); all other
    tools run in-process through :func:`execute_wave` exactly as
    before. The node re-enters after every fold with the remaining
    todo, so waves progress across park/resume segments.
    """
    deps = _deps()
    if deps.cancelled():
        _run_async(
            settle_cancelled_plan_tasks(
                deps.control,
                deps.context.run_id or "",
                language=str(state.get("language") or ""),
            )
        )
        return {"cancelled": True}
    _set_phase(state, "execution")
    run_id = deps.context.run_id or ""
    plan_record, tasks = _run_async(deps.control.get_plan(run_id))
    _emit_legacy_budget_notices(deps, state, tasks)
    research_policy = _research_child_policy(
        deps,
        edit_consent=_plan_lineage_web_consent(
            deps,
            state,
            current_plan=plan_record,
            current_tasks=tasks,
        ),
    )
    outcomes = _outcomes_from_state(state)
    pending = dict(state.get("pending_children") or {})
    outcomes, pending = _reconcile_persisted_execution(
        deps,
        state,
        tasks,
        outcomes,
        pending,
        research_policy=research_policy,
    )
    outcomes, pending = _fold_pending_children(
        deps,
        state,
        tasks,
        outcomes,
        pending,
        research_policy=research_policy,
    )
    if pending:
        state["outcomes"] = _outcomes_to_state(outcomes)
        state["tool_use_counts"] = _source_tool_counts(
            tasks,
            outcomes,
            base_counts=state.get("discovery_tool_use_counts"),
        )
        state["web_searches"] = _mission_web_searches(
            tasks,
            outcomes,
            base_counts=state.get("discovery_tool_use_counts"),
        )
        state["pending_children"] = pending
        return state
    todo = [
        task
        for task in tasks
        if task.tool_kind != "synthesis"
        and task.task_id not in outcomes
        and task.task_id not in pending
    ]
    max_parallel = max(1, deps.platform.max_parallel_children)
    for wave in topological_waves(todo):
        if state.get("cancelled") or deps.cancelled():
            state["cancelled"] = True
            break
        local_tasks = [
            task for task in wave if task.tool_kind != "web_research"
        ]
        child_tasks = [
            task for task in wave if task.tool_kind == "web_research"
        ]
        if child_tasks:
            # Submit children first so they overlap the independent local work
            # below. When both kinds exist and capacity permits, reserve one
            # slot for local work; this is one shared wave limit, not a second
            # scheduler.
            child_limit = (
                max_parallel - 1
                if local_tasks and max_parallel > 1
                else max_parallel
            )
            for task in child_tasks:
                if len(pending) >= child_limit:
                    break
                try:
                    task = _transition_task(deps, task, status="running")
                except ValueError:
                    current = _current_plan_task(deps, task)
                    if current.status not in TASK_TERMINAL_STATUSES:
                        raise
                    outcome = _outcome_from_persisted_task(current)
                    outcomes[task.task_id] = outcome
                    _emit_task_ended(
                        deps, state, current, current.task_id, outcome
                    )
                    continue
                deps.emit(
                    TASK_STARTED_EVENT,
                    {
                        "task_id": task.task_id,
                        "ordinal": task.ordinal,
                        "tool_kind": task.tool_kind,
                        "title": task.title,
                        "attempt": 1,
                    },
                )
                try:
                    child_id = _submit_child_run(
                        deps,
                        state,
                        task,
                        1,
                        research_policy=research_policy,
                    )
                except Exception as exc:  # noqa: BLE001 — admission stays visible
                    log.warning(
                        "Agent task %s (web_research) could not be submitted "
                        "(error_type=%s).",
                        task.task_id,
                        type(exc).__name__,
                    )
                    outcome = TaskOutcome(
                        status="failed",
                        failure_reason=sanitize_error(exc),
                        failure_code=_task_failure_code(exc),
                        transient=False,
                    )
                    outcomes[task.task_id] = outcome
                    _emit_task_ended(deps, state, task, task.task_id, outcome)
                    continue
                pending[task.task_id] = {
                    "child_run_id": child_id,
                    "attempt": 1,
                }
                try:
                    _transition_task(
                        deps,
                        task,
                        status="running",
                        child_run_id=child_id,
                    )
                except ValueError:
                    current = _current_plan_task(deps, task)
                    if current.status != "cancel_requested":
                        raise
                    _transition_task(
                        deps,
                        current,
                        status="cancel_requested",
                        child_run_id=child_id,
                    )
                    deps.run_service.run_store.cancel(
                        child_id,
                        workspace_id=deps.context.workspace_id,
                    )
        local_capacity = max_parallel - len(pending)
        if local_tasks and local_capacity > 0:
            # Control-store clients may own loop-affine async engines. Persist
            # task state serially on the graph thread; worker-pool functions
            # execute provider work only and never touch the store. Mark only
            # the next executable batch running: queued work beyond the shared
            # capacity remains honestly pending if a cancel arrives.
            for offset in range(0, len(local_tasks), local_capacity):
                if state.get("cancelled") or deps.cancelled():
                    state["cancelled"] = True
                    break
                candidate_batch = local_tasks[
                    offset : offset + local_capacity
                ]
                batch: list[PlanTaskRecord] = []
                for task in candidate_batch:
                    try:
                        batch.append(
                            _transition_task(deps, task, status="running")
                        )
                    except ValueError:
                        current = _current_plan_task(deps, task)
                        if current.status not in TASK_TERMINAL_STATUSES:
                            raise
                        outcome = _outcome_from_persisted_task(current)
                        outcomes[task.task_id] = outcome
                        _emit_task_ended(
                            deps, state, current, current.task_id, outcome
                        )
                if not batch:
                    continue

                def persist_local_outcome(
                    task: PlanTaskRecord, outcome: TaskOutcome
                ) -> None:
                    _emit_task_ended(
                        deps, state, task, task.task_id, outcome
                    )
                    current = _current_plan_task(deps, task)
                    outcomes[task.task_id] = _outcome_from_persisted_task(
                        current,
                        checkpointed=(
                            outcome
                            if current.status == outcome.status
                            else None
                        ),
                    )

                def admit_local_retry(task: PlanTaskRecord) -> bool:
                    current = _current_plan_task(deps, task)
                    admitted = (
                        current.status == "running"
                        and not deps.cancelled()
                    )
                    if not admitted:
                        log.info(
                            "Agent task %s: retry discarded because of status %s.",
                            task.task_id,
                            current.status,
                        )
                    return admitted

                batch_outcomes = execute_wave(
                    batch,
                    executor=lambda task, attempt: _execute_task(
                        deps, state, task, attempt
                    ),
                    max_parallel=local_capacity,
                    cancelled=deps.cancelled,
                    language=str(state.get("language") or ""),
                    on_outcome=persist_local_outcome,
                    retry_allowed=admit_local_retry,
                )
                batch_usage = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
                for outcome in batch_outcomes.values():
                    batch_usage["prompt_tokens"] += int(
                        outcome.usage.get("prompt_tokens", 0) or 0
                    )
                    batch_usage["completion_tokens"] += int(
                        outcome.usage.get("completion_tokens", 0) or 0
                    )
                # Persist every parallel outcome before enforcing the
                # cumulative run cap; a typed stop must never strand a
                # sibling that already finished in ``running``.
                _add_usage(state, batch_usage)
                if any(
                    outcomes[task_id].failure_code
                    == "token_budget_exceeded"
                    for task_id in batch_outcomes
                ):
                    raise AgentTokenBudgetExceeded(
                        "Run stopped by token budget.",
                        usage=state.get("usage", {}),
                    )
                if any(
                    outcomes[task_id].failure_code
                    == "client_requested_cancel"
                    for task_id in batch_outcomes
                ):
                    state["cancelled"] = True
                    break
        if state.get("cancelled") or deps.cancelled():
            state["cancelled"] = True
            break
        if pending:
            # The parent parks after its overlapping local work finishes;
            # remaining tasks re-enter from authoritative rows after wake.
            break
    if deps.cancelled():
        # A cancel that arrived DURING the final wave leaves the loop
        # without the between-waves check firing; the flag must land so
        # the run ends cancelled, not "all_tasks_failed".
        state["cancelled"] = True
    state["outcomes"] = _outcomes_to_state(outcomes)
    state["tool_use_counts"] = _source_tool_counts(
        tasks,
        outcomes,
        base_counts=state.get("discovery_tool_use_counts"),
    )
    state["web_searches"] = _mission_web_searches(
        tasks,
        outcomes,
        base_counts=state.get("discovery_tool_use_counts"),
    )
    state["pending_children"] = pending
    if (
        outcomes
        and not pending
        and not state.get("cancelled")
        and all(
            outcome.status == "failed" for outcome in outcomes.values()
        )
    ):
        # Failure policy: synthesis always runs EXCEPT when every
        # task hard-failed — an evidence-free memo would be a lie.
        # (insufficient_evidence is NOT a hard failure: it feeds the
        # replan gate. Pending children defer the verdict: their
        # outcomes are still open.)
        synthesis_task = next(
            (task for task in tasks if task.tool_kind == "synthesis"),
            None,
        )
        if synthesis_task is not None and synthesis_task.status == "pending":
            _emit_task_ended(
                deps,
                state,
                synthesis_task,
                synthesis_task.task_id,
                TaskOutcome(
                    status="skipped",
                    summary=t(
                        state, "agent_task_summary_upstream_failed"
                    ),
                ),
            )
        state["failure"] = "all_tasks_failed"
    return state


def _emit_task_ended(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: "PlanTaskRecord | None",
    task_id: str,
    outcome: TaskOutcome,
) -> None:
    """Emit the shared TASK_FINISHED/TASK_FAILED projection."""
    persisted_summary = outcome.summary or outcome.failure_reason
    # Guarded BEFORE the clip, never after: the repair replaces one
    # identifier with a several-word marker, and a mask applied to an
    # already-bounded summary could push it past the store's word bound
    # and turn a leak into a failed terminal transition.
    row_summary = task_result_summary(
        _guard_reader_text(state, persisted_summary, evidence=outcome.evidence)
    )
    if task is not None:
        try:
            task = _transition_task(
                deps,
                task,
                status=outcome.status,
                child_run_id=outcome.child_run_id,
                result_summary=row_summary,
                result_payload=task_result_payload(
                    outcome, persisted_summary=row_summary
                ),
            )
        except ValueError:
            current = _current_plan_task(deps, task)
            if current.status not in {"cancel_requested", "cancelled"}:
                raise
            outcome = _cancelled_task_outcome(
                outcome, language=_deps_language(deps) or ""
            )
            persisted_summary = outcome.summary
            row_summary = task_result_summary(
                _guard_reader_text(
                    state, persisted_summary, evidence=outcome.evidence
                )
            )
            task = (
                current
                if current.status == "cancelled"
                else _transition_task(
                    deps,
                    current,
                    status="cancelled",
                    child_run_id=current.child_run_id,
                    result_summary=row_summary,
                    result_payload=task_result_payload(
                        outcome, persisted_summary=row_summary
                    ),
                )
            )
    metrics = {
        "reference_count": len(outcome.evidence),
        "claim_count": len(outcome.claims),
        **(
            {
                "evidence_contract_status": outcome.evidence_contract_status,
                "confidence": outcome.confidence,
            }
            if outcome.evidence_contract_status != "unknown"
            else {}
        ),
        **(
            {
                "coverage": outcome.coverage,
                "omitted_evidence_count": outcome.omitted_evidence_count,
                "omitted_evidence_query_count": (
                    outcome.omitted_evidence_query_count
                ),
            }
            if outcome.omitted_evidence_count > 0
            else {}
        ),
        **_analysis_metrics(outcome.document_analysis),
    }
    if _task_evidence_flagged(outcome):
        title = task.title if task is not None else None
        note = _task_evidence_note(task_id, outcome, _deps_language(deps), title=title)
        log.warning(
            "Child evidence limited: %s",
            _task_evidence_note(task_id, outcome, "en", title=title),
        )
        # A limit of the evidence is a note about the task, not a fallback
        # path it took: the task keeps its own status (a completed task
        # stays completed) and the note travels as an open point.
        deps.emit(
            ACTIVITY_EVENT,
            {
                "activity_id": f"evidence-review:{task_id}",
                "scope": "task",
                "phase": "execution",
                "operation": (
                    "task.evidence_needs_review"
                    if outcome.evidence_contract_status in EVIDENCE_REVIEW_STATUSES
                    else "task.evidence_partial"
                ),
                "detail": note,
                "status": "completed",
                "task_id": task_id,
                "evidence_limit": True,
                "metrics": {
                    "confidence": outcome.confidence,
                    "single_source_ratio": outcome.single_source_ratio,
                    "omitted_evidence_count": outcome.omitted_evidence_count,
                },
            },
        )
    deps.emit(
        TASK_FINISHED_EVENT
        if outcome.status != "failed"
        else TASK_FAILED_EVENT,
        {
            "task_id": task_id,
            "ordinal": task.ordinal if task else 0,
            "tool_kind": task.tool_kind if task else "",
            "title": task.title if task else "",
            "status": outcome.status,
            "child_run_id": outcome.child_run_id,
            "result_summary": row_summary,
            "metrics": metrics,
            **(
                {
                    "error": outcome.failure_reason,
                    "failure": {
                        "code": outcome.failure_code or "task_failed",
                        "message": outcome.failure_reason,
                    }
                }
                if outcome.failure_reason
                else {}
            ),
        },
    )
    if outcome.status == "completed":
        _emit_narration(
            deps,
            state,
            narration_id=f"n-task-{task_id}",
            kind="task",
            text=task_narration(
                task.title if task else task_id, outcome.summary
            ),
            phase="execution",
            # This line runs BEFORE the run merges its ledger, and its text
            # comes from a model whose search results name documents by id.
            # The evidence that model produced is right here; the state's
            # own references are still empty.
            evidence=list(outcome.evidence),
        )


def _node_children_wait(state: AgentPhaseState) -> AgentPhaseState:
    """Park on the submitted children; fold their outcomes on resume.

    The run rows are checked before parking so a child that finished in the
    submit/checkpoint window is folded immediately. Only genuinely active
    children reach ``interrupt``; their terminal write re-queues the parent.
    A transient failure is resubmitted once with unchanged operator limits.
    """
    from langgraph.types import interrupt

    deps = _deps()
    if not state.get("pending_children"):
        if deps.cancelled():
            _run_async(
                settle_cancelled_plan_tasks(
                    deps.control,
                    deps.context.run_id or "",
                    language=str(state.get("language") or ""),
                )
            )
            return {"cancelled": True}
        return state
    run_id = deps.context.run_id or ""
    plan_record, tasks = _run_async(deps.control.get_plan(run_id))
    _emit_legacy_budget_notices(deps, state, tasks)
    research_policy = _research_child_policy(
        deps,
        edit_consent=_plan_lineage_web_consent(
            deps,
            state,
            current_plan=plan_record,
            current_tasks=tasks,
        ),
    )
    outcomes = _outcomes_from_state(state)
    outcomes, still_pending = _fold_pending_children(
        deps,
        state,
        tasks,
        outcomes,
        dict(state.get("pending_children") or {}),
        research_policy=research_policy,
    )
    state["outcomes"] = _outcomes_to_state(outcomes)
    state["tool_use_counts"] = _source_tool_counts(
        tasks,
        outcomes,
        base_counts=state.get("discovery_tool_use_counts"),
    )
    state["pending_children"] = still_pending
    if deps.cancelled():
        # A child may have committed its terminal run immediately before the
        # parent cancel arrived. Fold first so the authoritative completed
        # task cannot be downgraded to failed by cancellation settlement.
        _run_async(
            settle_cancelled_plan_tasks(
                deps.control, run_id, language=str(state.get("language") or "")
            )
        )
        state["cancelled"] = True
        return state
    if still_pending:
        interrupt({"kind": "children"})
    return state


def _node_evidence(state: AgentPhaseState) -> AgentPhaseState:
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    _set_phase(state, "evidence")
    outcomes = _outcomes_from_state(state)
    references, claims = evidence.merge_evidence(outcomes)
    web_search_ledger = merge_web_search_ledgers(
        [
            outcome.web_search_ledger
            for outcome in outcomes.values()
            if outcome.web_search_ledger
        ]
    )
    references = attach_web_search_lineage(
        [dict(reference) for reference in references],
        web_search_ledger,
    )
    state["references"] = references
    state["claims"] = claims
    state["web_search_ledger"] = web_search_ledger
    if references or web_search_ledger.get("searches"):
        # The run's ledger as an artifact: what a review role re-reads, and
        # the provenance of the deliverable. Knowledge-only runs have one too.
        _run_async(
            deps.control.upsert_artifact(
                run_id=deps.context.run_id or "",
                kind="evidence_bundle",
                session_id=None,
                title="Agent evidence",
                status="ready",
                content_markdown="",
                payload={
                    "schema_version": 1,
                    "web_search_ledger": web_search_ledger,
                },
                refs=references,
                updated_by="agent",
                artifact_id=f"art_{(deps.context.run_id or '')[-12:]}_evidence",
            )
        )

    consolidator = getattr(
        deps.context.strategies, "claim_consolidation", None
    )
    if claims and consolidator is not None:
        pairs = evidence.overlapping_claim_pairs(
            claims, signature=consolidator.claim_signature
        )
        model, effort = deps.resolved("agent_contradiction")
        outcome = evidence.run_contradiction_analysis(
            deps.llm,
            pairs=pairs,
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
        )
        if outcome is not None:
            _add_usage(state, outcome.usage)
            report = outcome.value
            state["contradictions"] = (
                [c.model_dump() for c in report.contradictions]
                if isinstance(report, ContradictionReport)
                else []
            )
    if not has_unresolved_outcome(outcomes):
        state["sufficiency"] = {"skipped_reason": "no_unresolved_outcome"}
        log.info("Sufficiency check skipped: no failed or insufficient subtask.")
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "sufficiency_skipped",
                "scope": "run",
                "phase": "evidence",
                "label": t(state, "agent_sufficiency_skipped_label"),
                "detail": t(state, "agent_sufficiency_skipped_detail"),
                "reason": "no_unresolved_outcome",
            },
        )
        return state
    model, effort = deps.resolved("agent_sufficiency")
    outcome = evidence.run_sufficiency_judgement(
        deps.llm,
        success_criteria=state.get("success_criteria", []),
        evidence_digest=evidence.evidence_digest(references),
        model=model,
        reasoning_effort=effort,
        timeout=deps.timeout,
    )
    _add_usage(state, outcome.usage)
    judgement = outcome.value
    if isinstance(judgement, SufficiencyJudgement):
        state["sufficiency"] = judgement.model_dump()
    else:
        # A failed verdict must never silently mean "covered": degrade
        # to the conservative middle (partial) and say so.
        log.warning(
            "Sufficiency verdict could not be interpreted; conservatively treating it "
            "as 'partial'."
        )
        state["sufficiency"] = {"coverage": "partial", "missing": []}
    return state


def _covered_gap_ids(
    tasks: Sequence[PlanTaskRecord], outcomes: dict[str, TaskOutcome]
) -> frozenset[str]:
    """The discovery gaps a completed task covered (see
    :func:`_analysis_covers_its_gaps`)."""
    return frozenset(
        gap_id
        for task in tasks
        if task.task_id in outcomes
        and outcomes[task.task_id].status == "completed"
        and _analysis_covers_its_gaps(outcomes[task.task_id])
        for gap_id in task.gap_ids
    )


def _node_replan_gate(state: AgentPhaseState) -> AgentPhaseState:
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    outcomes = _outcomes_from_state(state)
    discovery = _discovery_of(state)
    blocking_uncovered = False
    run_id = deps.context.run_id or ""
    current: tuple[PlanRecord, list[PlanTaskRecord]] | None = None
    if discovery is not None:
        current = _run_async(deps.control.get_plan(run_id))
        _plan, tasks = current
        covered_gaps = _covered_gap_ids(tasks, outcomes)
        blocking_uncovered = any(
            gap.blocking and gap.gap_id not in covered_gaps
            for gap in discovery.gaps
        )
    sufficiency = state.get("sufficiency") or {}
    needs_replan = evaluate_replan(
        outcomes=outcomes,
        blocking_gap_uncovered=blocking_uncovered,
        sufficiency_coverage=str(sufficiency.get("coverage", "covered")),
        replan_rounds_used=state.get("replan_rounds", 0),
        max_replan_rounds=deps.platform.max_replan_rounds,
    )
    if needs_replan:
        # Make the gate's decision visible at the decision site (its sibling
        # decision nodes all log/emit) with the ACCURATE reason derived from
        # the same inputs, so the logged reason states what actually
        # happened: an uncovered blocking gap is never reported as
        # "task_failed".
        reason = (
            "blocking_gap_uncovered"
            if blocking_uncovered
            else "sufficiency_uncovered"
            if str(sufficiency.get("coverage", "covered")) == "uncovered"
            else "task_failure_partial"
        )
        # Decided before the round is counted: a replan that cannot reach
        # anything unread consumes no round, so no later text claims
        # research that never ran.
        plan, tasks = current or _run_async(deps.control.get_plan(run_id))
        prospect = _research_prospect(
            deps, state, plan=plan, tasks=tasks, route="gate"
        )
        if not prospect.reachable:
            log.info(
                "Replan gate: no prospect (%s); synthesizing the evidence in hand.",
                ", ".join(
                    [
                        *prospect.codes,
                        *(
                            ["replan_headroom=0"]
                            if prospect.inputs.get("replan_headroom") == 0
                            else []
                        ),
                    ]
                ),
            )
            state["route"] = "synthesize"
            return state
        log.info(
            "Replan gate: round %d triggered (%s).",
            state.get("replan_rounds", 0) + 1,
            reason,
        )
        state["replan_rounds"] = state.get("replan_rounds", 0) + 1
        state["route"] = "replan"
    else:
        state["route"] = "synthesize"
    return state


def _restore_completed_synthesis(
    deps: "_RunDeps", state: AgentPhaseState
) -> bool:
    """Restore a synthesis output committed before its node checkpoint."""
    run_id = deps.context.run_id or ""
    artifact_id = state.get("artifact_id") or (
        f"art_{run_id[-12:]}_answer"
        if state.get("deliverable") == "chat"
        else session_memo_artifact_id(state.get("session_id"), run_id)
    )
    try:
        artifact, _revisions = _run_async(
            deps.control.get_artifact(
                run_id=run_id,
                artifact_id=artifact_id,
            )
        )
    except ArtifactNotFound:
        return False
    if not artifact.content_markdown:
        return False
    state["artifact_id"] = artifact.artifact_id
    state["memo_markdown"] = artifact.content_markdown
    state["memo_title"] = artifact.title
    state["memo_base_revision"] = artifact.revision
    return True


def _node_synthesize(state: AgentPhaseState) -> AgentPhaseState:
    """Execute synthesis exactly once and persist its plan-task lifecycle."""
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    # Read the park first - it is what tells a return from the park apart
    # from a crash - and then start unparked. Only a synthesis that hands
    # sections to roles sets the flag again; every other way out of this
    # node must leave it down, or the router would send a finished
    # synthesis back to the wait node and round the loop forever.
    parked = _synthesis_parked(state)
    state["synthesis_pending"] = False
    run_id = deps.context.run_id or ""
    _plan, tasks = _run_async(deps.control.get_plan(run_id))
    task = next((row for row in tasks if row.tool_kind == "synthesis"), None)
    if task is None:
        state["failure"] = "synthesis_task_missing"
        return state
    if task.status == "completed":
        if not _restore_completed_synthesis(deps, state):
            state["failure"] = "synthesis_output_missing"
        # The round is over; its progress must not drive a later entry.
        state["synthesis_progress"] = {}
        return state
    if task.status in {"failed", "skipped", "insufficient_evidence"}:
        state["failure"] = task.result_summary or "synthesis_failed"
        return state
    if task.status == "running" and not parked:
        # Running without a committed park: a crash in the middle of
        # writing. Nothing tells which sections reached the memo, so the
        # synthesis is not restarted behind the reader's back.
        outcome = TaskOutcome(
            status="failed",
            summary=t(state, "agent_synthesis_summary_not_restarted"),
            failure_reason="synthesis_execution_interrupted",
            failure_code="synthesis_execution_interrupted",
        )
        _emit_task_ended(deps, state, task, task.task_id, outcome)
        state["failure"] = "synthesis_execution_interrupted"
        return state
    if task.status != "running":
        task = _transition_task(deps, task, status="running")
        deps.emit(
            TASK_STARTED_EVENT,
            {
                "task_id": task.task_id,
                "ordinal": task.ordinal,
                "tool_kind": task.tool_kind,
                "title": task.title,
                "attempt": 1,
            },
        )
    try:
        result = _perform_synthesis(state, tasks)
    except synthesis.CitationValidationFailed as exc:
        _add_usage(state, exc.usage)
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "citation_validation_failed",
                "operation": "synthesis.citation_validation",
                "status": "failed",
                "error": {
                    "code": "citation_validation_failed",
                    "message": (
                        ("The synthesis still contained unknown evidence labels after "
                        "one repair attempt.")
                    ),
                },
            },
        )
        _emit_task_ended(
            deps,
            state,
            task,
            task.task_id,
            TaskOutcome(
                status="failed",
                summary=t(state, "agent_synthesis_summary_invalid_labels"),
                failure_reason="citation_validation_failed",
                failure_code="citation_validation_failed",
            ),
        )
        raise
    except AgentCancelled:
        _emit_task_ended(
            deps,
            state,
            task,
            task.task_id,
            TaskOutcome(
                status="failed",
                summary=t(state, "agent_synthesis_summary_cancelled"),
                failure_reason="client_requested_cancel",
                failure_code="client_requested_cancel",
            ),
        )
        raise
    except Exception:
        _emit_task_ended(
            deps,
            state,
            task,
            task.task_id,
            TaskOutcome(
                status="failed",
                summary=t(state, "agent_synthesis_summary_failed"),
                failure_reason="synthesis_failed",
                failure_code="synthesis_failed",
            ),
        )
        raise
    if result.get("cancelled"):
        _emit_task_ended(
            deps,
            state,
            task,
            task.task_id,
            TaskOutcome(
                status="failed",
                summary=t(state, "agent_synthesis_summary_cancelled"),
                failure_reason="client_requested_cancel",
                failure_code="client_requested_cancel",
            ),
        )
        return result
    if result.get("synthesis_pending"):
        # Sections are in role hands: the task stays running, the node
        # returns so its progress commits, and the wait node parks.
        return result
    outcome = TaskOutcome(
        status="completed",
        summary=task_result_summary(
            result.get("memo_markdown", "")
            or t(state, "agent_synthesis_summary_done")
        ),
        answer_markdown=str(result.get("memo_markdown", "") or ""),
    )
    _emit_task_ended(deps, state, task, task.task_id, outcome)
    return result


def _ranked_digest_references(
    deps: "_RunDeps", references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The PROMPT digest scope: rank-capped above the evidence budget.

    The citation ledger itself is never truncated — every label a
    section cites still resolves; only what the synthesis/revision
    prompt SEES is bounded.
    """
    tiering = getattr(deps.context.strategies, "source_tiering", None)
    return report_quality.rank_evidence(
        references,
        budget=deps.platform.synthesis_evidence_budget,
        tier_for_url=tiering.tier_for_url if tiering is not None else None,
    )


def _section_register_references(
    deps: "_RunDeps", references: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """What a section may REACH FOR, beside what its outline entry chose.

    Ranked and capped like the full-text digest, but on its own budget: a
    register line costs a fraction of a full-text entry, so the two
    questions - what may the deliverable quote at length, and what may a
    section cite at all - carry different numbers. The citation ledger
    itself stays untruncated either way.
    """
    override = deps.platform.synthesis_section_register_budget
    budget = override if override is not None else resolve_section_register(deps.tier)
    if budget == 0:
        # Off means off. rank_evidence reads 0 as "no cap" - handing it a
        # 0 here would give a section the WHOLE ledger, the opposite of
        # what the operator switched.
        return []
    tiering = getattr(deps.context.strategies, "source_tiering", None)
    return report_quality.rank_evidence(
        references,
        budget=budget,
        tier_for_url=tiering.tier_for_url if tiering is not None else None,
        limit_name=report_quality.SECTION_REGISTER_LIMIT,
    )


def _user_written_text(state: AgentPhaseState) -> str:
    """Every text of this mission the USER wrote, as one block.

    The writer guard asks one question of it: does this identifier stand in
    the user's own words? An id the user put in the assignment, in a
    clarification answer or in the canvas passage this run continues was
    not introduced by the model, and a guard that cannot tell the two apart
    sends a correct answer into the repair round for quoting the user back.

    The model's own texts stay out, and ``history`` is why this goes
    through a projection rather than a join: a verbatim transcript carries
    the previous ANSWER and the model's own clarification questions, so
    handing it in whole would let one leaked id exempt every later copy of
    itself. ``clarified_context`` already holds answer halves only
    (:func:`_node_clarify`), so it travels plain.
    """
    return report_quality.user_authored_text(
        *(
            text for key in (
                "question", "report_guidance", "clarified_context",
                "memo_user_prefix",
            ) if (text := str(state.get(key) or ""))
        ),
        history=str(state.get("history") or ""),
    )


def _writer_guard(
    state: AgentPhaseState, references: list[dict[str, Any]]
) -> dict[str, Any]:
    """The writer guard's arguments for one mission writing call.

    ONE place decides what a mission writer may not introduce and what
    counts as already standing, so the chat answer, a section written in
    one call and a section written by a role are checked alike. The
    references differ per site - each writer is held to what IT could see.
    """
    return {
        "internal_reference_ids": report_quality.writer_identifiers(references),
        "authored_by": _user_written_text(state),
    }


def _repair_outline_headings(
    deps: "_RunDeps",
    state: AgentPhaseState,
    outline: ReportOutline,
    references: list[dict[str, Any]],
    *,
    model: str | None,
    effort: str | None,
) -> ReportOutline:
    """The memo's headings pass the same guard its sentences pass.

    ``# {title}`` and every ``## {section title}`` of the memo are
    OUTLINE text (``synthesis.assemble_memo``), never section text — so
    the guard that checks a written section has never seen them, although
    the outline call is handed the same evidence cards the section writer
    is handed and can copy the same identifiers out of them.

    Repaired HERE, where the outline is adopted, because this is the last
    moment at which a writer can still be asked, and because one title
    reaches four reader surfaces from this one place: the memo body, the
    artifact's name in the canvas tab and every later listing, the
    synthesis narration, and the per-section narration. Repairing at the
    artifact write would leave the other three.

    Returns the outline to work from. The repaired one is what gets
    checkpointed into the synthesis progress, so a resumed segment
    continues with the clean headings instead of repairing them again.
    """
    guard = _writer_guard(state, references)
    known_labels = [str(ref["label"]) for ref in references]

    def _clean(heading: str) -> str:
        cleaned, usage = synthesis.repair_heading(
            deps.llm,
            heading,
            known_labels=known_labels,
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
            **guard,
        )
        if usage:
            _add_usage(state, usage)
        return cleaned

    title = _clean(outline.title)
    sections = [
        section.model_copy(update={"title": cleaned})
        if (cleaned := _clean(section.title)) != section.title
        else section
        for section in outline.sections
    ]
    if title == outline.title and sections == list(outline.sections):
        return outline
    log.warning(
        "Memo headings carried internal identifiers and were repaired "
        "(title_changed=%s, sections_changed=%d).",
        title != outline.title,
        sum(
            1
            for before, after in zip(outline.sections, sections)
            if before.title != after.title
        ),
    )
    return outline.model_copy(update={"title": title, "sections": sections})


def _perform_synthesis(state: AgentPhaseState, tasks: list[Any]) -> AgentPhaseState:
    """Build the chat or canvas deliverable after lifecycle admission."""
    deps = _deps()
    _set_phase(state, "synthesis")
    # What the children read and where their evidence is limited belongs in
    # every writing prompt, the outline included, and remains visible
    # within the requested output structure.
    evidence_notes = _task_evidence_notes(
        _outcomes_from_state(state),
        _language(state),
        titles={task.task_id: task.title for task in tasks},
        readings=True,
    )
    references = state.get("references", [])
    digest_references = _ranked_digest_references(deps, references)
    digest = synthesis_evidence(digest_references, state.get("web_search_ledger", {}))
    contradictions_digest = "\n".join(
        f"- {c.get('internal_position', '')} VS "
        f"{c.get('external_position', '')} ({c.get('severity', '')})"
        for c in state.get("contradictions", [])
    )
    if contradictions_digest:
        contradictions_digest = untrusted_fence(
            contradictions_digest, "contradictions"
        )
    if state.get("deliverable") == "chat":
        return _synthesize_chat_answer(
            deps, state, digest, contradictions_digest, evidence_notes,
            known_labels=[str(ref["label"]) for ref in digest_references],
            **_writer_guard(state, digest_references),
        )
    # The run's clarifications as the store holds them, read once: the
    # outline, every section in either transport and the estimate that picks
    # a section's transport all refine the assignment with the same record.
    clarifications = run_clarifications(
        _run_async(deps.control.list_clarifications(deps.context.run_id or ""))
    )
    # What a section may reach for beyond its own preselection, as one
    # line per reference. Built ONCE and byte-identical for every section,
    # so a reader comparing two sections sees the same register - and only
    # here, below the chat answer, which writes no section and has no
    # register to cut.
    register_references = _section_register_references(deps, references)
    section_register = evidence.evidence_digest(register_references)
    model, effort = deps.resolved("agent_synthesis")
    skills_block = _skills_prompt_block(deps, state)
    # The section writer gets the same skills with a PART-scoped format
    # line: the outline decides the whole deliverable, a section only
    # fills one of its parts.
    section_skills_block = _skills_prompt_block(deps, state, scope="section")
    round_ = _synthesis_round(state)
    progress = dict(state.get("synthesis_progress") or {})
    if progress.get("round") != round_:
        progress = {}
    if progress.get("outline"):
        # Back from this round's park: the outline was written and paid for
        # before it. Writing it again would re-plan sections that are
        # already written, or already in a role's hands.
        outline = ReportOutline.model_validate(progress["outline"])
    else:
        # Resynthesis continues this run's memo; an earlier session memo is
        # used only when the current run has not written one yet.
        prior_memo = state.get("memo_markdown") or state.get("prior_memo", "")
        outline_outcome = synthesis.run_outline(
            deps.llm,
            question=state["question"],
            success_criteria=state.get("success_criteria", []),
            evidence_digest=digest,
            known_labels=[str(ref["label"]) for ref in digest_references],
            prior_memo=prior_memo,
            skills_block=skills_block,
            user_guidance=state.get("report_guidance", ""),
            evidence_notes=evidence_notes,
            clarifications=clarifications,
            language=_language(state),
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
        )
        _add_usage(state, outline_outcome.usage)
        outline = outline_outcome.value
        if not isinstance(outline, ReportOutline) or not outline.sections:
            from inqtrix.exceptions import AgentStructuredOutputError

            raise AgentStructuredOutputError(
                model or "", "ReportOutline",
                "The outline contains no valid sections or evidence references.",
            )
        outline = _repair_outline_headings(
            deps,
            state,
            outline,
            digest_references,
            model=model,
            effort=effort,
        )
        _emit_narration(
            deps,
            state,
            narration_id="n-synthesis",
            kind="synthesis",
            text=synthesis_narration(
                outline.title,
                len(outline.sections),
                language=state["language"],
            ),
            phase="synthesis",
        )
        progress = {
            "round": round_,
            "outline": outline.model_dump(mode="json"),
            "sections": {},
            "folded": 0,
        }
    state["synthesis_progress"] = progress
    written: dict[str, dict[str, Any]] = progress["sections"]
    # Once each section can see the whole register, this is the only thing
    # that still tells it which reference belongs to a sibling - without
    # it, two sections would independently reach for the same label.
    outline_overview = "\n".join(
        f"{number}. {entry.title}"
        # One label per bracket, the form the writers are told to cite in:
        # an overview written as [K1, K2] taught every section the group.
        + (
            " " + " ".join(f"[{label}]" for label in entry.evidence_labels)
            if entry.evidence_labels
            else ""
        )
        for number, entry in enumerate(outline.sections, start=1)
    )
    role_available = deps.resolver is not None and deps.resolver.executes("agent_kernel")

    def _material(section: ReportSection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """What a section leads with, and everything it may cite."""
        # The outline's choice decides what this section READS IN FULL,
        # never what exists. An empty choice means "nothing preselected",
        # and reading it as "no evidence" is what made sections write
        # that no sources were available while the run held a ledger.
        selected = [ref for ref in digest_references if ref["label"] in section.evidence_labels]
        # Citable = read in full OR listed in the register. Both lists are
        # derived from the SAME references, because a label the section may
        # cite whose reference id it never saw would leak that id into the
        # prose the reader gets.
        citable = list(selected)
        seen = {str(ref["label"]) for ref in selected}
        citable.extend(
            ref for ref in register_references
            if str(ref["label"]) not in seen
        )
        return selected, citable

    def _write_one(_position: int, item: tuple[int, ReportSection]) -> _SectionOutcome:
        """ONE section's prose in one call, owning all of its own state.

        Sections are independent by construction — each is prompted from
        its outline entry and its own evidence digest and reads nothing a
        sibling writes — so they run concurrently; the memo is folded in
        outline order on the calling thread.
        """
        index, section = item
        if deps.cancelled():
            return _SectionOutcome(
                index=index, title=section.title, skipped=True
            )
        selected, citable = _material(section)
        body, usage = synthesis.write_section(
            deps.llm,
            evidence_notes=evidence_notes,
            question=state["question"],
            section_title=section.title,
            section_focus=section.focus,
            evidence_digest=synthesis_evidence(
                selected, state.get("web_search_ledger", {})
            ),
            evidence_register=section_register,
            outline_overview=outline_overview,
            clarifications=clarifications,
            language=_language(state),
            contradictions_digest=contradictions_digest,
            skills_block=section_skills_block,
            user_guidance=state.get("report_guidance", ""),
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
            known_labels=[str(ref["label"]) for ref in citable],
            **_writer_guard(state, citable),
        )
        return _SectionOutcome(
            index=index,
            title=section.title,
            body=body,
            usage=usage,
            preselected=tuple(str(ref["label"]) for ref in selected),
            cited=tuple(
                str(ref["label"])
                for ref in report_quality.cited_references(body, citable)
            ),
        )

    def _fold_ready() -> None:
        """Fold every written section that is next in outline order.

        The memo grows as a PREFIX of the outline: a section still in a
        role's hands stops the fold, and the written sections behind it
        wait in the progress until it returns. The memo never stands in
        the order sections happened to finish.
        """
        while progress["folded"] < len(outline.sections):
            index = progress["folded"] + 1
            done = written.get(str(index))
            if done is None or "body" not in done:
                return
            preselected = tuple(done["preselected"])
            cited = tuple(done["cited"])
            beyond = tuple(label for label in cited if label not in preselected)
            # "The outline preselected nothing although the run held
            # evidence" - asked of the run's LEDGER, never of the register:
            # a register an operator switched off must not hide the gap it
            # leaves wider open. And of the list, never its rendering - an
            # empty render is the sentinel string, which is true.
            reached_past_nothing = not preselected and bool(references)
            if reached_past_nothing:
                log.warning(
                    "Section %d (%s) had no preselected evidence although the run "
                    "held %d references; its register listed %d and it cited %d.",
                    index,
                    done["title"],
                    len(references),
                    len(register_references),
                    len(cited),
                )
            activity = {
                "kind": (
                    "section_without_preselection"
                    if reached_past_nothing
                    else "section_evidence"
                ),
                "scope": "run",
                "phase": "synthesis",
                "status": "degraded" if reached_past_nothing else "completed",
                "label": done["title"],
                "preselected": list(preselected),
                "cited": list(cited),
                "cited_beyond_preselection": list(beyond),
                "transport": done["transport"],
            }
            if done["transport"] == TRANSPORT_ROLE:
                activity["read_preselected"] = list(done["read_preselected"])
            deps.emit(ACTIVITY_EVENT, activity)
            progress["folded"] = index
            memo = synthesis.assemble_memo(
                outline.title,
                [
                    (written[str(number)]["title"], written[str(number)]["body"])
                    for number in range(1, index + 1)
                ],
            )
            state["memo_markdown"] = memo
            # The name belongs to the DOCUMENT, not to the turn. A session
            # memo is named once, when it is created, and changes name only
            # through an explicit rename. Re-titling on every canvas turn
            # silently threw away a name the user had given it — and made
            # the file in the transcript change identity under them.
            if not state.get("memo_base_revision"):
                state["memo_title"] = outline.title
            _flush_memo(deps, state, memo, status="writing")
            _emit_narration(
                deps,
                state,
                narration_id=f"n-section-{index - 1}",
                kind="synthesis",
                text=section_narration(done["title"], language=state["language"]),
                phase="synthesis",
            )

    def _record(outcome: _SectionOutcome) -> None:
        """Book one written section where it was paid for, then fold."""
        if outcome.skipped:
            state["cancelled"] = True
            return
        if state.get("cancelled"):
            return
        # Booked in the segment that wrote it, even when it is folded only
        # after a role ahead of it returns in a later segment.
        _add_usage(state, outcome.usage)
        written[str(outcome.index)] = {
            "transport": outcome.transport,
            "title": outcome.title,
            "body": outcome.body,
            "preselected": list(outcome.preselected),
            "cited": list(outcome.cited),
            "read_preselected": list(outcome.read_preselected),
        }
        _fold_ready()

    in_one_call: list[tuple[int, ReportSection]] = []
    pending = False
    for index, section in enumerate(outline.sections, start=1):
        done = written.get(str(index))
        if done is not None and "body" in done:
            continue
        selected, citable = _material(section)
        transport = (done or {}).get("transport") or synthesis.choose_section_transport(
            synthesis.section_prompt(
                question=state["question"],
                section_title=section.title,
                section_focus=section.focus,
                evidence_digest=synthesis_evidence(
                    selected, state.get("web_search_ledger", {})
                ),
                evidence_register=section_register,
                contradictions_digest=contradictions_digest,
                skills_block=section_skills_block,
                user_guidance=state.get("report_guidance", ""),
                evidence_notes=evidence_notes,
                outline_overview=outline_overview,
                clarifications=clarifications,
                language=_language(state),
            ),
            role_available=role_available,
        )
        written[str(index)] = {"transport": transport}
        if transport != TRANSPORT_ROLE:
            in_one_call.append((index, section))
            continue
        handed = _section_by_role(
            deps,
            state,
            round_=round_,
            index=index,
            section=section,
            handover=_section_handover(
                deps,
                state,
                outline=outline,
                index=index,
                section=section,
                selected=selected,
                evidence_notes=evidence_notes,
                contradictions_digest=contradictions_digest,
                section_register=section_register,
                outline_overview=outline_overview,
                skills_block=section_skills_block,
                clarifications=clarifications,
            ),
            selected=selected,
            citable=citable,
            ledger=references,
            model=model,
            effort=effort,
        )
        if handed.outcome is not None:
            _record(handed.outcome)
        elif handed.pending:
            pending = True
        else:
            # Never a quiet gap and never a whole synthesis lost to one role:
            # the section is written in one call, and the reader is told.
            log.warning(
                "Section role for section %d (%s) could not deliver (reason=%s); "
                "writing the section in one call.",
                index,
                section.title,
                handed.failure,
            )
            deps.emit(
                ACTIVITY_EVENT,
                {
                    "kind": "section_role_unavailable",
                    "scope": "run",
                    "phase": "synthesis",
                    "status": "degraded",
                    "label": section.title,
                    "reason": handed.failure,
                    "detail": handed.detail[:200],
                },
            )
            written[str(index)] = {"transport": TRANSPORT_PROMPT}
            in_one_call.append((index, section))
    if state.get("cancelled"):
        return state
    run_in_pool(
        in_one_call,
        _write_one,
        max(1, int(deps.platform.synthesis_max_parallel_sections or 1)),
        skipped=lambda _position, item: _SectionOutcome(
            index=item[0], title=item[1].title, skipped=True
        ),
        on_result=_record,
        thread_name_prefix="inqtrix-agent-section",
    )
    if state.get("cancelled"):
        return state
    if pending:
        # Every role of this round is submitted; the node returns so its
        # progress commits, and the wait node parks ONCE for all of them.
        state["synthesis_pending"] = True
        return state
    # The round is complete: the memo artifact is the record from here on.
    state["synthesis_progress"] = {}
    return state

def answer_node_for(tasks: list[Any]) -> str:
    """The model auto-downgrade rule, deterministic and in ONE place.

    An approved web task keeps synthesis on the high tier even when that task
    returned no references. Plans without a web task use the mid tier. This
    ties model selection to the authorized work rather than to output luck.
    """
    has_web = any(
        str(
            task.get("tool_kind", "")
            if isinstance(task, dict)
            else getattr(task, "tool_kind", "")
        )
        in {"web_research", "web_instant"}
        for task in tasks
    )
    return "agent_answer" if has_web else "agent_answer_light"


def _synthesize_chat_answer(
    deps: "_RunDeps",
    state: AgentPhaseState,
    digest: str,
    contradictions_digest: str,
    evidence_notes: str,
    *,
    known_labels: list[str],
    internal_reference_ids: list[str],
    authored_by: str,
) -> AgentPhaseState:
    """The chat-form deliverable: ONE answer call, no outline loop.

    Model routing follows the approved plan: a web task uses the high
    tier even when retrieval yielded no references; all-internal work uses
    the mid tier.
    """
    _plan, tasks = _run_async(
        deps.control.get_plan(deps.context.run_id or "")
    )
    node = answer_node_for(tasks)
    model, effort = deps.resolved(node)
    body, usage = synthesis.write_chat_answer(
        deps.llm,
        question=state["question"],
        evidence_digest=digest,
        contradictions_digest=contradictions_digest,
        evidence_notes=evidence_notes,
        history=state.get("history", ""),
        prior_memo=state.get("prior_memo", ""),
        skills_block=_skills_prompt_block(deps, state),
        user_guidance=state.get("report_guidance", ""),
        language=_language(state),
        model=model,
        reasoning_effort=effort,
        timeout=deps.timeout,
        known_labels=known_labels,
        internal_reference_ids=internal_reference_ids,
        authored_by=authored_by,
    )
    _add_usage(state, usage)
    state["memo_markdown"] = body
    state["memo_title"] = t(state, "memo_title_answer")
    _flush_deliverable(deps, state, body, status="writing")
    _emit_narration(
        deps,
        state,
        narration_id="n-answer",
        kind="synthesis",
        text=t(state, "agent_answer_written"),
        phase="synthesis",
    )
    return state


def _critic_research_cap(state: AgentPhaseState, deps: "_RunDeps") -> int:
    """Effective critic-research round cap for this mission.

    The platform cap (``max_replan_rounds``) is shared with the gate
    replan; a delegated child narrows it to
    ``DELEGATED_CRITIC_RESEARCH_ROUNDS`` because its parent verifies the
    memo once more.
    """
    cap = int(deps.platform.max_replan_rounds)
    if state.get("delegated"):
        cap = min(cap, DELEGATED_CRITIC_RESEARCH_ROUNDS)
    return cap


def _mission_active_seconds(deps: "_RunDeps") -> float | None:
    """Active compute seconds of this run so far, read from its run row.

    Gate waits are excluded on purpose: the time budget bounds the work
    the user pays for, not how long a human took to answer. ``None``
    (row without a timing block) means the budget cannot be judged; the
    caller then relies on the round cap alone and says so in the log.
    """
    run_id = deps.context.run_id or ""
    if not run_id:
        return None
    row = deps.run_service.run_store.get(run_id, visible_to=deps.visible_to)
    timing = row.get("timing") or {}
    value = timing.get("active_seconds")
    return float(value) if value is not None else None


def _memo_ends_with_open_points(memo: str, language: str | None) -> bool:
    """True when the memo's LAST second-level heading is the open-points one.

    The outline doctrine makes the open-points section (``heading_for(
    "open_points", language)``) the memo's closing section, so the
    critic's remaining points join that section instead of opening a
    second one with the same name.
    """
    headings = re.findall(r"^## +(.+?)\s*$", memo, flags=re.MULTILINE)
    wanted = heading_for("open_points", language).strip().lower()
    return bool(headings) and headings[-1].strip().lower() == wanted


def _critic_open_points(
    critic: Mapping[str, Any] | None, memo: str = "", language: str | None = None
) -> str:
    """Closing block from the success criteria the critic still saw uncovered.

    Appended deterministically (no model call) when the research route is
    exhausted, so the memo the parent and the user read names what stayed
    open instead of ending on a memo the critic already judged incomplete.
    Only the plan's own success criteria are listed, in reader language
    ("Noch offen: ..."); the critic's finding details stay in its report —
    they are checker vocabulary (paragraph counts, unverified quotes), not
    memo content. The inline label is omitted when the memo already closes
    with an 'Offene Punkte' section. No structural heading is added.
    """
    if not critic:
        return ""
    items = critic_phase.uncovered_criteria(critic, kind="information")
    if not items:
        return ""
    words = {"language": language or ""}
    heading = (
        []
        if _memo_ends_with_open_points(memo, language)
        else [f"**{heading_for('open_points', language)}:**", ""]
    )
    return "\n".join(
        [
            "",
            "",
            *heading,
            # Only the evidence brake has an intro of its own; every other
            # limit keeps the general one.
            t(
                words,
                "memo_open_points_intro_no_new_evidence"
                if "no_new_evidence" in (critic.get("research_limits") or ())
                else "memo_open_points_intro",
            ),
            "",
            *[
                "- " + t(words, "memo_open_points_item", item=item)
                for item in items
            ],
        ]
    )


def _plan_criteria(state: AgentPhaseState) -> tuple[str, ...]:
    """The run's success criteria as the planner and the review number them."""
    return tuple(
        str(item).strip()
        for item in state.get("success_criteria", [])
        if str(item).strip()
    )


def _review_subject(
    deps: "_RunDeps", state: AgentPhaseState, tasks: list[Any], plan: Any
) -> tuple[Any, list[dict[str, Any]]]:
    """The mission's deliverable as the subject of the shared review pass.

    The mission measures, the review judges: coverage, quote checks,
    contradictions and the stored execution travel as run diagnostics. The
    memo's label space is the mission's own reference list; an edit cites the
    labels of its passage and of its finding's basis
    (:mod:`inqtrix.agents.kernel.review_edits`). The ledger a finding may
    rest on is that same list.
    """
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.evidence_read import (
        document_analysis_diagnostic,
        project_review_sources,
    )
    from inqtrix.agents.kernel.review_pass import ReviewSubject
    from inqtrix.agents.prompts import build_mission_review_assignment
    from inqtrix.core.results import HandoverOutput

    run_id = deps.context.run_id or ""
    memo = state.get("memo_markdown", "")
    references = [dict(ref) for ref in state.get("references", [])]
    search_ledger = state.get("web_search_ledger", {})
    quote_checks = synthesis.verify_quotes(memo, references)
    outcomes = _outcomes_from_state(state)
    cited = synthesis.cited_references(memo, references)
    # What each document analysis read, as its own record: the review can
    # confirm a complete reading from it, and a named limit stays open.
    readings = [
        record
        for task in tasks
        if task.task_id in outcomes
        for record in [
            document_analysis_diagnostic(
                outcomes[task.task_id].document_analysis,
                run_id=run_id,
                task_id=task.task_id,
                title=task.title,
                objective=task.objective or task.title,
                evidence=list(outcomes[task.task_id].evidence),
                language=_language(state) or "",
            )
        ]
        if record is not None
    ]
    cited_sources = {
        critic_phase.SOURCE_BASIS_PREFIX + KernelDeps.ledger_id(ref) for ref in cited
    }
    diagnostics = critic_phase.review_diagnostics(
        run_id=run_id,
        language=_language(state) or "",
        coverage=synthesis.citation_coverage(memo),
        quote_checks=quote_checks,
        contradictions=state.get("contradictions", []),
        memo_markdown=memo,
        uncited_verified_sources=sorted(
            {source for record in readings for source in record["values"]["verified_sources"]}
            - cited_sources
        ),
        task_evidence_notes=_task_evidence_notes(
            outcomes,
            _language(state),
            titles={task.task_id: task.title for task in tasks},
            readings=False,
        ),
        execution={
            "plan_version": plan.version,
            "plan_status": plan.status,
            "tasks": [
                {"task_id": task.task_id, "title": task.title,
                 "tool_kind": task.tool_kind, "status": task.status,
                 "objective": task.objective}
                for task in tasks
            ],
            "source_operations_including_discovery": dict(state.get("tool_use_counts", {})),
            "discovery_operations": dict(state.get("discovery_tool_use_counts", {})),
        },
    ) + readings

    def evidence_for(selected: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **project_review_sources(selected, search_ledger),
            "run_diagnostics": diagnostics,
        }

    artifact_id = str(state.get("artifact_id") or "")
    as_chat = state.get("deliverable") == "chat" or not artifact_id
    output_id = "chat" if as_chat else artifact_id
    # By address only when a reader can open exactly the text under review:
    # a canvas is read through its session, and a preserved user edit makes
    # the stored document longer than this run's memo.
    by_address = bool(state.get("session_id")) and not state.get("memo_user_prefix")
    return (
        ReviewSubject(
            assignment=build_mission_review_assignment(
                state["question"],
                skills_block=_skills_prompt_block(deps, state),
                user_guidance=state.get("report_guidance", ""),
                memory_briefing=state.get("memory_briefing", ""),
            ),
            outputs={output_id: memo},
            canvases=()
            if as_chat
            else (
                HandoverOutput(
                    kind="canvas",
                    artifact_id=artifact_id,
                    revision=int(state.get("memo_base_revision", 0) or 0),
                    title=str(state.get("memo_title") or ""),
                    text="" if by_address else memo,
                ),
            ),
            allowed_references={output_id: references},
            ledger={KernelDeps.ledger_id(ref): ref for ref in references},
            cited=cited,
            evidence=evidence_for(cited),
            evidence_for=evidence_for,
            diagnostic_ids={output_id: {item["basis_id"] for item in diagnostics}},
            criteria=_plan_criteria(state),
            # The run's clarifications as the store holds them, not as the
            # history phrased them.
            clarifications=run_clarifications(
                _run_async(deps.control.list_clarifications(run_id))
            ),
            user_text=_user_written_text(state),
            evidence_run_id=run_id,
            language=_language(state) or "",
        ),
        quote_checks,
    )


def _review_host(deps: "_RunDeps", state: AgentPhaseState, tasks: list[Any]) -> Any:
    """What the shared review pass needs from this mission segment."""
    from inqtrix.agents.kernel.review_pass import ModelCall, ReviewHost

    # The revision is written by the node that wrote the deliverable.
    writer = (
        answer_node_for(tasks)
        if state.get("deliverable") == "chat"
        else "agent_synthesis"
    )
    return ReviewHost(
        scope=deps.child_scope(state),
        emit=deps.emit,
        visible_to=deps.visible_to,
        llm=deps.llm,
        timeout=deps.timeout,
        platform=deps.platform,
        revision_call=ModelCall(writer, *deps.resolved(writer)),
        review_call=ModelCall(REVIEW_NODE, *deps.resolved(REVIEW_NODE)),
        role_available=deps.resolver is not None
        and deps.resolver.executes("agent_kernel"),
    )


@dataclass(frozen=True)
class _RoleSection:
    """Where one section handed to a role stands.

    Exactly one of three: ``outcome`` is the written section, ``pending``
    means the role still runs and the synthesis parks on it, ``failure``
    names why the role could not deliver - the section is then written in
    one call, visibly.
    """

    outcome: "_SectionOutcome | None" = None
    pending: bool = False
    failure: str = ""
    detail: str = ""


def _synthesis_round(state: AgentPhaseState) -> int:
    """The synthesis round of this run: one per research round.

    A critic that sends the mission back to research opens a new round, and
    a new round writes its memo afresh - it never continues the progress,
    or the section roles, of the round before it.
    """
    return int(state.get("replan_rounds") or 0) + 1


def _synthesis_parked(state: AgentPhaseState) -> bool:
    """Whether this run comes back from its own synthesis park.

    Only a committed return of the synthesis node sets the flag, and only
    for the round it belongs to. A crash before the round's first park
    commits nothing and cannot look like this; a crash after a wake starts
    from the park's own checkpoint and resumes from the progress it holds.
    """
    progress = state.get("synthesis_progress") or {}
    return bool(state.get("synthesis_pending")) and progress.get("round") == _synthesis_round(state)


def _section_handover(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    outline: ReportOutline,
    index: int,
    section: ReportSection,
    selected: list[dict[str, Any]],
    evidence_notes: str,
    contradictions_digest: str,
    section_register: str,
    outline_overview: str,
    skills_block: str,
    clarifications: "Sequence[HandoverClarification]",
) -> Any:
    """The typed work order of one section role.

    Its brief is built by the SAME builder as the single call's prompt; the
    only difference is that the evidence the section leads with arrives as
    addresses to read, because that evidence is what made it too large.
    The run's clarifications travel in the brief, in the writers' form: the
    handover's own record field is the review's, a requirement to check
    against, and stays empty here.
    """
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.prompts import build_section_brief
    from inqtrix.core.results import WorkHandover

    # The id the role's ledger files each reference under - a mission
    # reference often carries none of its own, and an address the reader
    # cannot resolve is no address.
    addressed = [{**ref, "reference_id": KernelDeps.ledger_id(ref)} for ref in selected]
    return WorkHandover(
        goal=(
            f"Write the memo section '{section.title}' (section {index} of "
            f"{len(outline.sections)} of the memo '{outline.title}')."
        ),
        brief=build_section_brief(
            state["question"],
            section.title,
            section.focus,
            evidence.evidence_digest(addressed, with_reference_ids=True),
            contradictions_digest,
            evidence_register=section_register,
            outline_overview=outline_overview,
            skills_block=skills_block,
            user_guidance=state.get("report_guidance", ""),
            evidence_notes=evidence_notes,
            clarifications=clarifications,
            evidence_by_address=True,
        ),
        evidence_run_id=deps.context.run_id or "",
        # What the brief addresses is what this role has to read; the rest
        # of the mission's ledger is reachable but not assigned, and a
        # budget priced on it would pay for work nobody ordered.
        evidence_references=len(addressed),
        expected_result="SectionText: the Markdown prose of this one section, without its heading.",
        language=_language(state),
    )


def _section_origin_key(round_: int, index: int) -> str:
    """Stable identity of one section role across re-entries of its round."""
    return f"section:{round_}:{index}"


def _section_by_role(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    round_: int,
    index: int,
    section: ReportSection,
    handover: Any,
    selected: list[dict[str, Any]],
    citable: list[dict[str, Any]],
    ledger: list[dict[str, Any]],
    model: str | None,
    effort: str | None,
) -> _RoleSection:
    """Hand one section to the section role, or read what it wrote.

    Re-entrant like the review: the child is found again by its origin key,
    so the node may run around its park as often as it wakes. The role's
    prose goes through the SAME closing stage as a section written in one
    call, with the same citable labels - the transport decides how the
    material reaches the writer, never what the section may cite.
    """
    from inqtrix.agents.kernel.deps import KernelDeps
    from inqtrix.agents.kernel.tools import (
        TOOL_FINISHED_EVENT,
        TOOL_STARTED_EVENT,
        inherited_stufe,
        submit_or_find_child,
    )
    from inqtrix.agents.kernel.work_profiles import (
        SECTION_WRITER,
        resolve_work_profile,
        settle_role_child,
    )

    profile = resolve_work_profile(SECTION_WRITER)
    origin_key = _section_origin_key(round_, index)
    scope = deps.child_scope(state)
    store = getattr(deps.run_service, "run_store", None)
    row = {
        "tool": profile.operation,
        "tool_call_id": origin_key,
        "invocation_id": origin_key,
    }
    known = store is not None and any(
        child.get("origin_key") == origin_key
        for child in store.children(scope.run_id)
    )
    if not known:
        # Once per section: the re-entries around the park find the child.
        deps.emit(TOOL_STARTED_EVENT, {**row, "args_preview": section.title})

    def failed(reason: str, detail: str = "") -> _RoleSection:
        deps.emit(
            TOOL_FINISHED_EVENT,
            {
                **row,
                "status": "error",
                "result_preview": shorten_visibly(detail, 200, marker=LOG_CUT_MARKER),
            },
        )
        return _RoleSection(failure=reason, detail=detail)

    try:
        child = submit_or_find_child(
            scope,
            origin_key=origin_key,
            mode="agent_kernel",
            question=handover.goal,
            # Priced like the run that ordered it, exactly as the review
            # role is: a section commissioned by a "tief" mission would
            # otherwise run at the server's default depth, and the budget
            # its brief now states would have nothing to move.
            agent_overrides=inherited_stufe(scope.tier, scope.depth),
            # A role gates nothing and asks nobody: it re-reads what this
            # mission already reached under the user's own approvals.
            autonomy="autonomous",
            work_profile=profile.name,
            handover=handover,
        )
        stored = (
            store.result(str(child["run_id"]), visible_to=deps.visible_to)
            if store is not None
            and isinstance(child, dict)
            and child["status"] == "completed"
            else None
        )
    except AgentCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - the section is still writable in one call
        return failed("unavailable", type(exc).__name__)
    if isinstance(child, str):
        return failed("unavailable", child)
    settled = settle_role_child(child, stored, profile)
    if settled is None:
        return _RoleSection(pending=True)
    if settled.reason:
        return failed(settled.reason, settled.detail)
    try:
        body, usage = synthesis.finish_section(
            deps.llm,
            settled.value.markdown,
            usage={},
            known_labels=[str(ref["label"]) for ref in citable],
            # Every id the role could have seen and copied. A single call
            # is held to its own material - the digest it was shown and
            # the register beside it. A role reaches further: its work
            # order names the preselection by ledger id, and read_evidence
            # renders the WHOLE ledger with its provenance, so its check
            # takes the ledger.
            **_writer_guard(state, ledger),
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
        )
    except BaseException as exc:
        # The row closes whatever ends the section; the failure itself is
        # the same one a single call would raise, and travels on.
        deps.emit(
            TOOL_FINISHED_EVENT,
            {**row, "status": "error", "result_preview": type(exc).__name__},
        )
        raise
    read = {source_id for kind, source_id in settled.read_before_result if kind == "retained_evidence"}
    deps.emit(
        TOOL_FINISHED_EVENT,
        {
            **row,
            "status": "success",
            "result_preview": t_count(
                {"language": _language(state)}, "agent_section_role_result", len(read)
            ),
        },
    )
    return _RoleSection(
        outcome=_SectionOutcome(
            index=index,
            title=section.title,
            body=body,
            usage=usage,
            preselected=tuple(str(ref["label"]) for ref in selected),
            cited=tuple(
                str(ref["label"]) for ref in report_quality.cited_references(body, citable)
            ),
            transport=TRANSPORT_ROLE,
            read_preselected=tuple(
                str(ref["label"]) for ref in selected
                if KernelDeps.ledger_id(ref) in read
            ),
        )
    )


def _review_output_bundle(subject: Any) -> str:
    """The deliverable as the revision sees it: text, and the labels it may cite.

    Same shape as the kernel's bundle. The references are listed compactly:
    their material reaches the revision through the bases the findings name.
    """
    payload: dict[str, Any] = {"artifacts": []}
    for output_id, text in subject.outputs.items():
        if output_id == "chat":
            payload["chat"] = text
            continue
        canvas = next(item for item in subject.canvases if item.artifact_id == output_id)
        payload["artifacts"].append({
            "artifact_id": output_id,
            "revision": canvas.revision,
            "content_markdown": text,
            "refs": [
                {key: ref[key] for key in ("label", "title", "url", "document_id") if ref.get(key)}
                for ref in subject.cited
            ],
        })
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ResearchBudget:
    """The mission's budget for follow-up research, read once per decision.

    ``used`` research rounds of ``cap`` (``delegated_cap``: the cap is the
    delegated narrowing), ``active`` compute seconds (``None``: the run row
    has no timing) of ``budget_seconds`` (0: no time budget).
    """

    used: int
    cap: int
    delegated_cap: bool
    active: float | None
    budget_seconds: int

    @property
    def limits(self) -> tuple[str, ...]:
        """Every budget limit that applies; the round cap and the clock can both."""
        return (
            *(("replan_rounds",) if self.used >= self.cap else ()),
            *(
                ("mission_time_budget",)
                if self.budget_seconds > 0
                and self.active is not None
                and self.active >= self.budget_seconds
                else ()
            ),
        )


def _research_budget(state: AgentPhaseState, deps: "_RunDeps") -> ResearchBudget:
    """What the review may still spend on research (reads only, emits nothing)."""
    cap = _critic_research_cap(state, deps)
    budget = int(deps.platform.mission_time_budget_seconds or 0)
    active = _mission_active_seconds(deps) if budget > 0 else None
    if budget > 0 and active is None:
        log.warning(
            "Mission time budget cannot be checked because the run row has no "
            "timing; only the round limit bounds follow-up research."
        )
    return ResearchBudget(
        used=int(state.get("replan_rounds", 0) or 0),
        cap=cap,
        delegated_cap=bool(state.get("delegated"))
        and cap == DELEGATED_CRITIC_RESEARCH_ROUNDS,
        active=active,
        budget_seconds=budget,
    )


def _emit_research_exhausted(
    state: AgentPhaseState,
    deps: "_RunDeps",
    *,
    limits: Sequence[str],
    budget: ResearchBudget,
    prospect: Prospect | None,
) -> None:
    """Name every limit that ends the research route, in one decision.

    One ``limit.reached`` per budget limit, then one
    ``critic_research_exhausted`` activity: ``reason`` is the first limit,
    ``reasons`` all of them, ``codes`` what closed the prospect. The label is
    the first limit's, the detail joins every limit's own sentence.
    """
    ceiling = {
        "extendable": False,
        "recoverable": False,
        "reason": "deterministic_mission_budget",
        "state": "continued_with_visible_gap",
    }
    labels: list[str] = []
    details: list[str] = []
    for limit in limits:
        if limit == "no_new_evidence":
            labels.append(t(state, "agent_critic_no_new_evidence_label"))
            details.append(t(state, "agent_critic_no_new_evidence_detail"))
        elif limit == "replan_rounds":
            deps.emit(
                LIMIT_REACHED_EVENT,
                {"kind": "replan_rounds", "used": budget.used, "limit": budget.cap,
                 "ceiling": budget.cap, **ceiling},
            )
            labels.append(t(state, "agent_critic_cap_label"))
            details.append(t(
                state,
                "agent_critic_cap_delegated_detail"
                if budget.delegated_cap
                else "agent_critic_cap_replan_detail",
            ))
        elif limit == "mission_time_budget":
            deps.emit(
                LIMIT_REACHED_EVENT,
                {"kind": "mission_time_budget", "used": int(budget.active or 0),
                 "limit": budget.budget_seconds, "ceiling": budget.budget_seconds,
                 **ceiling},
            )
            labels.append(t(state, "agent_mission_time_budget_label"))
            minutes = round(budget.budget_seconds / 60, 1)
            # A fraction of a minute counts as plural ("1,5 Minuten"); the
            # count only chooses the form. German writes a decimal comma.
            shown = f"{minutes:g}"
            if _language(state) == "de":
                shown = shown.replace(".", ",")
            details.append(t_count(
                state,
                "agent_mission_time_budget_detail",
                int(minutes) if minutes.is_integer() else 0,
                minutes=shown,
            ))
        elif limit == "no_prospect":
            labels.append(t(state, "agent_research_prospect_closed_label"))
            details.append(
                _prospect_detail(state, prospect, route="critic") if prospect else ""
            )
        elif limit == "plan_task_limit":
            labels.append(t(state, "agent_critic_plan_task_limit_label"))
            details.append(
                _prospect_detail(state, prospect, route="critic") if prospect else ""
            )
        else:
            raise ValueError(f"unknown research limit: {limit!r}")
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": "critic_research_exhausted",
            "reason": limits[0],
            "reasons": list(limits),
            "codes": list(prospect.codes) if prospect else [],
            "label": labels[0],
            "detail": " ".join(detail for detail in details if detail),
        },
    )


def _end_critic_research(
    deps: "_RunDeps",
    state: AgentPhaseState,
    reason: str,
    *,
    label: str,
    detail: str,
) -> None:
    """A research round the review opened ends before any task ran.

    The review's record names why (``research_limits``), and the decision
    has the shape every ``critic_research_exhausted`` has: the codes are
    those of the prospect that opened the round.
    """
    critic = state.get("critic")
    limits = [reason]
    if isinstance(critic, dict):
        limits = [*(critic.get("research_limits") or []), reason]
        state["critic"] = {**critic, "research_limits": limits}
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": "critic_research_exhausted",
            "reason": reason,
            "reasons": limits,
            "codes": list((state.get("research_prospect") or {}).get("codes") or []),
            "label": label,
            "detail": detail,
        },
    )


def _node_critic(state: AgentPhaseState) -> AgentPhaseState:
    """Review the deliverable through the shared review pass and decide.

    The pass reviews, assesses and proposes anchored edits; this node owns
    the mission's decision and its words: back to research while an
    information criterion is open and the budget allows, otherwise apply the
    edits finding by finding through the deliverable's own write path — each
    finding ends applied or named with its reason —, otherwise pass or name
    what stays open. A revised text is not reviewed a second time; the pass
    checks each applied model edit against the text it replaced and the
    texts of the labels it cites, verifies one only when a second,
    identical check holds it too, and takes none back: an edit it does not
    hold stays applied, unverified.
    """
    from inqtrix.agents.kernel import review_pass
    from inqtrix.observability.trace_scores import record_deep_review_scores

    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    if state.get("phase") != "critic":
        # A re-entry after the wait finds the phase already announced.
        _set_phase(state, "critic")
    state["review_pending"] = False
    run_id = deps.context.run_id or ""
    plan, tasks = _run_async(deps.control.get_plan(run_id))
    subject, quote_checks = _review_subject(deps, state, tasks, plan)
    host = _review_host(deps, state, tasks)
    (output_id,) = subject.outputs
    try:
        review = review_pass.request_review(
            host,
            subject,
            # One review per synthesis: research in between opens a new round.
            origin_key=(
                f"{review_pass.REVIEW_ROLE_ORIGIN}:"
                f"{int(state.get('replan_rounds', 0) or 0) + 1}"
            ),
        )
    except review_pass.AwaitingReview:
        state["review_pending"] = True
        return state
    _add_usage(state, review.usage)
    if review.verdict is None:
        return _critic_unavailable(
            deps, state, review.reason,
            review_pass.reader_detail(review, _language(state) or ""),
        )
    record_deep_review_scores(review.verdict)
    language = _language(state) or ""
    measured: list[Any] = []
    # The quotes the server measured as unverified: a finding anchored by
    # its paragraph holds that paragraph's verified quotes as well.
    unverified: frozenset[str] = frozenset()
    if deps.tier_policy is not None and deps.tier_policy.verify == "escalating":
        # Deep tier: an unverified quote in a web-cited paragraph is a
        # measured defect, whatever the verdict says about the text.
        measured, unanchored = critic_phase.unverified_web_quote_findings(
            subject.outputs[output_id],
            quote_checks,
            target="chat" if output_id == "chat" else "artifact",
            artifact_id="" if output_id == "chat" else output_id,
            basis_id=critic_phase.quote_check_basis(run_id),
            finding_text=t(state, "agent_critic_quote_finding"),
        )
        unverified = frozenset(
            report_quality.unverified_web_quotes(subject.outputs[output_id], quote_checks)
        )
        if measured or unanchored:
            log.warning(
                "Deep tier: %d unverified web-cited quotations join the review "
                "findings, %d stay open without a unique anchor.",
                len(measured),
                len(unanchored),
            )
            if measured:
                # Only when measured findings join the review.
                deps.emit(
                    ACTIVITY_EVENT,
                    {
                        "kind": "critic_web_quote_escalation",
                        "label": t(state, "agent_critic_quote_escalation_label"),
                        "detail": t(state, "agent_critic_quote_escalation_detail"),
                    },
                )
            # The measured findings stay the server's: they join the pass,
            # not the verdict, so the stored verdict is the model's own.
            review = replace(
                review,
                verdict=review.verdict.model_copy(
                    update={
                        "not_checked": [
                            *review.verdict.not_checked,
                            *(
                                t(state, "agent_critic_quote_unanchored", quote=quote)
                                for quote in unanchored
                            ),
                        ],
                    }
                ),
            )
    assessment = review_pass.assess_review(review, subject, measured=measured)
    if assessment.status == "inconsistent":
        return _critic_unavailable(deps, state, "inconsistent", "")
    open_points = review_pass.open_points(assessment, language)
    identity = evidence_identity(state.get("references", []))
    previous = state.get("critic") or {}
    # The round this review judged, fixed before a research decision counts
    # the next one.
    review_round = int(state.get("replan_rounds", 0) or 0) + 1
    limits: list[str] = []
    prospect: Prospect | None = None

    def decided(
        verdict: str,
        *,
        revision: str = "none",
        results: Sequence[Any] = (),
        draft_round: int | None = None,
        quotes: Collection[str] | None = None,
    ) -> AgentPhaseState:
        # Every finding the text did not take over stays open with its reason.
        # The statements are counted on the memo as finalize publishes it;
        # on the research route the draft is rewritten next, and none is.
        facts = review_pass.report_facts(
            assessment, results,
            revision=revision, language=language, draft_round=draft_round,
            unverified=quotes,
            outputs=(
                None if draft_round is not None
                else {output_id: normalize_writer_markdown(str(state.get("memo_markdown") or ""))}
            ),
        )
        state["critic"] = {
            "verdict": verdict,
            "round": review_round,
            "revision": facts["revision"],
            "accounting": facts["accounting"],
            "evidence_identity": identity,
            "research_limits": list(limits),
            "prospect_codes": (
                [code for code in prospect.codes if code != "extraction_limit"]
                if prospect is not None and not prospect.reachable
                else []
            ),
            # The list the review numbered: a later replan context names
            # its criteria by these numbers only while the plan holds it.
            "criteria": list(subject.criteria),
            "criteria_covered": list(assessment.criteria_met),
            "criteria_uncovered": facts["criteria_uncovered"],
            "open_lines": facts["open_lines"],
            # Per confirmed finding, model and server alike; the verdict
            # below is the model's own.
            "findings": facts["findings"],
            "reach": facts["reach"],
            **{key: facts[key] for key in ("claims", "marks") if key in facts},
            "transport": review.transport,
            "review": review.verdict.model_dump(),
        }
        state["last_review"] = {"status": "completed", "round": review_round}
        return state

    # Only a criterion of the plan can send the mission back to research:
    # the planner plans from exactly these, and a review must demand nothing
    # the assignment does not require.
    missing = [
        first_sentences(item["text"], 120)
        for item in assessment.criteria_unmet
        if item["kind"] == "information"
    ]
    if missing:
        # Every limit is evaluated, none short-circuits the next: the
        # decision names all that apply.
        if (
            previous.get("verdict") == "research"
            and previous.get("evidence_identity") == identity
        ):
            # Research that added no evidence cannot close what stayed
            # open: the next round would ask the same sources the same
            # questions.
            limits.append("no_new_evidence")
            log.info("Critic research ends: the last research round added no evidence.")
        budget = _research_budget(state, deps)
        limits.extend(budget.limits)
        prospect = _research_prospect(
            deps, state, plan=plan, tasks=tasks, route="critic"
        )
        if not prospect.reachable:
            limits.append(
                "plan_task_limit"
                if prospect.inputs.get("replan_headroom") == 0
                else "no_prospect"
            )
        if not limits:
            state["replan_rounds"] = review_round
            state["route"] = "critic_research"
            deps.emit(
                ACTIVITY_EVENT,
                {
                    "kind": "critic_research",
                    "label": t(state, "agent_critic_research_label"),
                    "detail": t(
                        state,
                        "agent_critic_research_detail_items",
                        items="; ".join(missing),
                    ),
                },
            )
            # Nothing is applied before research: each located finding
            # stays open with the route as its reason.
            return decided(
                "research",
                results=[
                    *assessment.results,
                    *review_pass.located_results(assessment, "research_route"),
                ],
                draft_round=review_round,
            )
        _emit_research_exhausted(
            state, deps, limits=limits, budget=budget, prospect=prospect
        )
        state["route"] = "critic_research_exhausted"
    if assessment.status in {"revise", "inapplicable"}:
        first = next(
            (
                first_sentences(
                    review_pass.readable(
                        item.finding, assessment.names, _language(state) or ""
                    ),
                    120,
                )
                for item in assessment.findings
            ),
            "",
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "critic_revise",
                "label": t(state, "agent_critic_revise_label"),
                "detail": first or t(state, "agent_critic_revise_detail"),
            },
        )
        results = list(assessment.results)
        if assessment.status == "revise":
            deps.emit(
                ACTIVITY_EVENT,
                review_pass.findings_activity(
                    assessment, language,
                    titles={output_id: str(state.get("memo_title") or "")},
                ),
            )
            revision = review_pass.revise_outputs(
                host,
                subject,
                assessment,
                output_bundle=_review_output_bundle(subject),
            )
            _add_usage(state, revision.usage)
            results += revision.results
            if revision.status == "revised":
                state["memo_markdown"] = revision.outputs[output_id]
                # Applied, each model edit re-checked by the pass and none
                # taken back; the text is by design not reviewed a second time.
                _flush_deliverable(
                    deps, state, revision.outputs[output_id], status="writing"
                )
        # This is the last round: a server finding that lost its overlap is
        # measured again on the text the reader gets.
        delivered = str(state.get("memo_markdown") or "")
        results = _settle_server_losers(
            results, delivered, list(state.get("references", [])), unverified
        )
        # A server line quotes what the server measured and the reader still
        # finds: a paragraph anchor also holds verified quotes, and the
        # revision may have removed one of the measured ones.
        quotes = unverified & {
            inner for inner, _passage in report_quality.quoted_passages(delivered)
        }
        # What stands unchanged, visibly and in reader words.
        unapplied = review_pass.unapplied_activity(
            results, assessment.names, language, unverified=quotes
        )
        if unapplied is not None:
            deps.emit(ACTIVITY_EVENT, unapplied)
        return decided(
            "revise",
            revision=review_pass.revision_state(results),
            results=results,
            quotes=quotes,
        )
    # A finding that marked its passage asked for no revision; the passage
    # stays as written, listed among the findings, and the review ends open.
    marked = review_pass.marked_results(assessment.results)
    if marked:
        deps.emit(
            ACTIVITY_EVENT,
            review_pass.findings_activity(
                assessment, language,
                titles={output_id: str(state.get("memo_title") or "")},
            ),
        )
    if open_points or marked:
        return decided("open", results=list(assessment.results))
    return decided("pass")


def _settle_server_losers(
    results: list[Any],
    text: str,
    references: list[dict[str, Any]],
    unverified: Collection[str],
) -> list[Any]:
    """Measure every server finding that lost its overlap again, on *text*.

    *text* is what the reader gets after the last round's revision. The
    winner's edit may have removed the loser's quote, so the loser is named
    only while its quote still stands, web-cited and unverified. Gone, or no
    longer web-cited, the overlapping edit carried the fix: it ends applied
    with :data:`~inqtrix.agents.kernel.review_edits.OVERLAP_EDIT`. A loser's
    quote is the one the server measured (*unverified*), not every quote of
    the paragraph that anchors it.
    """
    from inqtrix.agents.kernel.review_edits import OVERLAP_EDIT

    losers = {
        result.finding_id for result in results
        if result.origin == "server"
        and result.state == "named"
        and result.reason == "anchor_overlap"
    }
    if not losers:
        return results
    checks = synthesis.verify_quotes(text, references)
    flagged = set(report_quality.unverified_web_quotes(text, checks))
    standing = {inner for inner, _passage in report_quality.quoted_passages(text)}
    settled: list[Any] = []
    for result in results:
        if result.finding_id not in losers:
            settled.append(result)
            continue
        inner = [
            quote for quote, _passage in report_quality.quoted_passages(result.quote)
            if quote in unverified
        ]
        if not inner:
            log.warning(
                "Server finding %s holds no quote the server measured; it stays "
                "named.", result.finding_id,
            )
            settled.append(result)
            continue
        if any(quote in flagged for quote in inner):
            settled.append(result)
            continue
        if any(quote in standing for quote in inner):
            log.info(
                "Server finding %s: its quote stands without a web citation "
                "after the overlapping edit.", result.finding_id,
            )
        else:
            log.info(
                "Server finding %s: its quote no longer stands after the "
                "overlapping edit.", result.finding_id,
            )
        settled.append(replace(result, state="applied", reason=OVERLAP_EDIT))
    return settled


def _critic_unavailable(
    deps: "_RunDeps", state: AgentPhaseState, reason: str, detail: str
) -> AgentPhaseState:
    """No usable review: the deliverable stands, and the run says so."""
    log.warning("Mission review unavailable (reason=%s).", reason)
    state["critic"] = None
    state["last_review"] = {
        "status": "unavailable",
        "round": int(state.get("replan_rounds", 0) or 0) + 1,
        "reason": reason,
    }
    state["route"] = "critic_unavailable"
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": "critic_unavailable",
            "scope": "run",
            "status": "failed",
            "reason": reason,
            "label": t(state, "agent_critic_unavailable_label"),
            "detail": " ".join(
                part for part in (t(state, "agent_critic_unavailable_detail"), detail)
                if part
            ),
        },
    )
    return state


def _node_review_wait(state: AgentPhaseState) -> AgentPhaseState:
    """Park on the review role child; its terminal write wakes this run.

    Its own node, like the children wait: everything the critic did before
    the wait is committed, so a resume neither repeats it nor re-announces
    the phase. A wake that finds the role still running simply comes back
    here through the critic.
    """
    from langgraph.types import interrupt

    if _deps().cancelled():
        return {"cancelled": True}
    interrupt({"kind": "children"})
    return state


def _node_synthesis_wait(state: AgentPhaseState) -> AgentPhaseState:
    """Park on the section roles of this synthesis round.

    Its own node, like the review wait: everything the synthesis did
    before the wait is committed - the outline, the sections written in one
    call, the roles it submitted - so a resume neither repeats nor re-books
    any of it. A children park, because that is the status the store wakes
    when the last child ends; a wake that finds a role still running comes
    back here through the synthesis.
    """
    from langgraph.types import interrupt

    if _deps().cancelled():
        return {"cancelled": True}
    interrupt({"kind": "children"})
    return state


def _node_patch(state: AgentPhaseState) -> AgentPhaseState:
    """Propose an editor patch against the assignment's target document.

    Runs only when the request carried a ``document_id``. NO interrupt
    here — the LLM call and the control-store writes must COMMIT before
    the approval node interrupts (the plan/plan_approval split). The
    agent NEVER applies the patch: apply happens solely
    through ``POST /v1/editor/patches/{id}:apply``.
    """
    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    _set_phase(state, "patch")
    run_id = deps.context.run_id or ""
    document_id = state.get("target_document_id", "")
    if deps.editor_patches is None or deps.editor_docs is None:
        # Loud, never silent: the assignment targeted a document but the
        # deployment has no editor persistence/patch surface.
        state["failure"] = "editor_patches_unavailable"
        return state
    from inqtrix.project.editor_ports import DocumentNotFound
    from inqtrix.services.editor_persistence_service import (
        CollaborationProjectionUnavailable,
    )

    try:
        document = _run_async(
            deps.editor_docs.get_document_for_ai(
                document_id, visible_to=deps.visible_to
            )
        )
    except DocumentNotFound:
        state["failure"] = "patch_document_not_found"
        return state
    except CollaborationProjectionUnavailable:
        state["failure"] = "collaboration_projection_unavailable"
        return state

    from inqtrix.agents.patch_phase import (
        PatchProposalFailed,
        propose_patch_edits,
    )

    model, effort = deps.resolved("agent_patch")
    settings = deps.context.agent_settings
    try:
        result, tokens = propose_patch_edits(
            deps.llm,
            question=state.get("question", ""),
            memo_markdown=state.get("memo_markdown", ""),
            document_markdown=document.content_markdown,
            language=str(state.get("language") or ""),
            model=model,
            reasoning_effort=effort,
            # The editor budget, not the reasoning one — the SAME window
            # /v1/editor/instruct enforces (decoupled by design).
            timeout=float(
                getattr(settings, "editor_assistant_timeout", deps.timeout)
            ),
        )
    except PatchProposalFailed as exc:
        # HARD failure (never "no changes needed"): the requested
        # document edit did not happen — visible, not smoothed over.
        state["failure"] = str(exc)
        return state
    # Combined prompt+completion count (the instruct pipeline reports one
    # sum); booked under prompt_tokens — one bucket, never dropped.
    _add_usage(
        state, {"prompt_tokens": tokens, "completion_tokens": 0}
    )
    if not result.edits:
        # A genuine zero-edit judgement is a legitimate outcome.
        state["patch_decision"] = "no_changes"
        return state
    principal = deps.context.principal
    patch = _run_async(
        deps.editor_patches.propose(
            document_id=document_id,
            run_id=run_id,
            source="agent",
            edits=[edit.to_payload() for edit in result.edits],
            summary=result.assistant_message,
            warnings=list(result.warnings),
            created_by_user_id=getattr(principal, "user_id", None),
            visible_to=deps.visible_to,
            # Attributes the proposal in the audit trail as an agent write;
            # actor_user_id remains the segment's effective actor.
            principal=principal,
        )
    )
    state["patch_id"] = patch.patch_id
    # The artifact row references the patch id — the edits live
    # exactly once, in the editor_patches row.
    artifact_id = f"art_{run_id[-12:]}_patch"
    _run_async(
        deps.control.upsert_artifact(
            run_id=run_id,
            kind="editor_patch",
            session_id=None,
            title=document.title,
            status="ready",
            content_markdown="",
            payload={"patch_id": patch.patch_id},
            refs=[],
            updated_by="agent",
            artifact_id=artifact_id,
        )
    )
    deps.emit(
        PATCH_PROPOSED_EVENT,
        {
            "patch_id": patch.patch_id,
            "document_id": document_id,
            "artifact_id": artifact_id,
            "edit_count": len(result.edits),
        },
    )
    return state


def _node_patch_approval(state: AgentPhaseState) -> AgentPhaseState:
    """The patch approval interrupt is ALWAYS gated.

    Deliberately no autonomy branch: a write effect interrupts in every
    mode, including ``autonomous``. Rejection ends the run NORMALLY (the
    memo stays the deliverable); only the patch decision is recorded.
    """
    from langgraph.types import interrupt

    deps = _deps()
    if deps.cancelled():
        return {"cancelled": True}
    run_id = deps.context.run_id or ""
    approval_id = f"apr_{run_id[-12:]}_patch_0"
    _ensure_approval(
        deps,
        run_id,
        approval_id,
        kind="patch",
        payload={
            "patch_id": state.get("patch_id", ""),
            "document_id": state.get("target_document_id", ""),
        },
    )
    decision = interrupt({"kind": "approval", "id": approval_id})
    rejected = decision.get("decision") == "reject"
    state["patch_decision"] = "rejected" if rejected else "approved"
    if rejected and deps.editor_patches is not None:
        # The generic decisions endpoint decides only the approval row;
        # the patch row must follow, or the explicitly rejected edits
        # would stay pending and appliable forever. Idempotent — the FE
        # review flow rejects the row first, this replay returns stored.
        from inqtrix.project.editor_patch_ports import PatchAlreadyDecided

        try:
            _run_async(
                deps.editor_patches.reject(
                    state.get("patch_id", ""),
                    note="",
                    visible_to=deps.visible_to,
                )
            )
        except PatchAlreadyDecided:
            pass
    return state


def _node_finalize(state: AgentPhaseState) -> AgentPhaseState:
    deps = _deps()
    if (
        not state.get("last_review")
        and not state.get("cancelled")
        and not state.get("failure")
        and deps.tier_policy is not None
        and not review_required(deps.tier_policy.verify)
    ):
        # The Stufe skipped the review (``_after_synthesize``): the outcome
        # record says so instead of reading "not run".
        state["last_review"] = {"status": "waived", "round": 0}
    if state.get("cancelled"):
        _run_async(
            settle_cancelled_plan_tasks(
                deps.control,
                deps.context.run_id or "",
                language=str(state.get("language") or ""),
            )
        )
    memo = state.get("memo_markdown", "")
    if memo and state.get("deliverable") != "chat":
        # The last review still saw an information criterion open that no
        # further round covers — the round cap or the time budget ended the
        # research: the memo closes with those points named (never silently
        # on a memo the review judged incomplete). A CHAT answer keeps its
        # text: it is short, and its review report is a visible artifact.
        memo = memo.rstrip() + _critic_open_points(
            state.get("critic"), memo, _language(state)
        )
        state["memo_markdown"] = memo
    if memo:
        _flush_deliverable(deps, state, memo, status="ready")
        # The evidence artifact remains the complete audit ledger.  Public
        # answer references are only those still cited after deterministic
        # claim repair, so an unknown/attributed-only source cannot re-enter
        # the answer through the final result projection.
        state["references"] = synthesis.cited_references(
            state.get("memo_markdown", ""),
            state.get("references", []),
        )
    critic = state.get("critic")
    if critic is not None:
        run_id = deps.context.run_id or ""
        _run_async(
            deps.control.upsert_artifact(
                run_id=run_id,
                kind="critic_report",
                session_id=None,
                title=t(state, "critic_report_title"),
                status="ready",
                content_markdown="",
                payload=critic,
                refs=[],
                updated_by="agent",
                artifact_id=f"art_{run_id[-12:]}_critic",
            )
        )
    # A gate-rejected run produced no research worth remembering — an
    # LLM reflection over the deterministic receipt would be pure waste
    # (and the draft case adds nothing a completed run would not).
    if not state.get("plan_rejected"):
        _stage_memory_candidates(deps, state)
    _set_phase(state, "done" if not state.get("cancelled") else "cancelled")
    return state


_MEMO_CONFLICT_SEPARATOR = (
    "\n\n---\n\n> Hinweis: Das Memo wurde zwischenzeitlich manuell "
    "bearbeitet. Die folgende Aktualisierung stammt aus diesem Agent-Lauf.\n\n"
)
_MEMO_FLUSH_MAX_ATTEMPTS = 5


def _flush_deliverable(
    deps: "_RunDeps", state: AgentPhaseState, body: str, *, status: str
) -> None:
    """Route the deliverable write to its ONE artifact channel.

    Canvas deliverables stay on the session-memo path with their provenance
    reconcile.  Chat deliverables are deliberately not persisted
    inside graph nodes: the native RunService publisher creates the empty
    ``writing`` answer artifact only when the algorithm has returned its final
    Markdown, then finalizes it immediately before ``answer.ready``.
    """
    if state.get("deliverable") == "chat":
        _ground_mission_output(deps, state, body)
        return
    _flush_memo(deps, state, body, status=status)


def _ground_mission_output(
    deps: "_RunDeps", state: AgentPhaseState, body: str
) -> str:
    """Commit the already synthesized answer without a second content gate."""

    _ = deps
    answer = normalize_writer_markdown(body)
    state["memo_markdown"] = answer
    return answer


def _flush_memo(
    deps: "_RunDeps", state: AgentPhaseState, memo: str, *, status: str
) -> None:
    """Write the memo artifact, reconciling by PROVENANCE, never clobbering.

    The session memo is one row shared across a session's turns, and the
    graph checkpoints only at node boundaries — so on a crash-retry the
    checkpointed ``memo_base_revision`` reverts while the DB row is already
    ahead from this run's own partial section flushes. A blind revision-CAS
    cannot tell that self-authored advance from a genuine user edit. The
    bounded reconcile loop below distinguishes them via ``updated_by``:

    * ``updated_by == 'agent'`` with this ``run_id`` on a conflict = our OWN
      uncommitted partial (retry fast-forward) — adopt its revision and
      re-write, NO user flag;
    * an agent write from another run is a foreign concurrent edit and is
      preserved exactly like a user edit;
    * ``updated_by == 'user'`` = a real edit — keep that text as
      a prefix, append this run's memo below it, surface the conflict ONCE;
    * ``expected_revision`` is passed verbatim (``0`` on a fresh session)
      so a concurrently-inserted row is caught as a conflict too.
    """
    memo = _ground_mission_output(deps, state, memo)
    run_id = deps.context.run_id or ""
    session_id = state.get("session_id") or None
    resolved_id = state.get("artifact_id") or session_memo_artifact_id(
        state.get("session_id"), run_id
    )

    def _write(content: str, expected: int) -> ArtifactRecord:
        cited_refs = synthesis.cited_references(
            content, state.get("references", [])
        )
        return _run_async(
            deps.control.upsert_artifact(
                run_id=run_id,
                kind="memo",
                session_id=session_id,
                title=state.get("memo_title") or t(state, "memo_title_default"),
                status=status,
                content_markdown=content,
                payload={},
                refs=cited_refs,
                updated_by="agent",
                artifact_id=resolved_id,
                expected_revision=expected,
            )
        )

    base = state.get("memo_base_revision", 0)
    prefix = state.get("memo_user_prefix", "")
    record: ArtifactRecord | None = None
    for _ in range(_MEMO_FLUSH_MAX_ATTEMPTS):
        body = memo if not prefix else prefix + _MEMO_CONFLICT_SEPARATOR + memo
        try:
            record = _write(body, base)
            break
        except ArtifactRevisionConflict:
            # Re-read the conflicting row by the SAME key the write used:
            # the session-memo lineage key when this run belongs to a
            # session, else the run-scoped artifact_id (a session-less run
            # writes ``resolved_id`` with ``session_id=None``, which
            # get_session_artifact — filtering on session_id equality —
            # can never match, so a by-id read is the only witness).
            current: ArtifactRecord | None = None
            if session_id:
                current = _run_async(
                    deps.control.get_session_artifact(session_id, "memo")
                )
            if current is None:
                try:
                    current, _ = _run_async(
                        deps.control.get_artifact(run_id, resolved_id)
                    )
                except ArtifactNotFound:
                    current = None
            if current is None:
                base = 0  # vanished between attempts — recreate
                continue
            if current.updated_by == "agent" and current.run_id == run_id:
                # Our own partial from a crashed attempt — adopt, not a
                # user edit; the re-run's cumulative memo overwrites it.
                base = current.revision
                # If that partial already embedded a preserved user edit
                # (before the first separator), recover it so the retry
                # keeps appending BELOW the user's text — checkpoint state
                # reverted on the crash, the DB row is the only witness.
                if (
                    not prefix
                    and _MEMO_CONFLICT_SEPARATOR in current.content_markdown
                ):
                    prefix = current.content_markdown.split(
                        _MEMO_CONFLICT_SEPARATOR, 1
                    )[0]
                    state["memo_user_prefix"] = prefix
                continue
            # A genuine user edit: preserve it once, then keep appending
            # below it on every subsequent flush of this run.
            if not prefix:
                prefix = current.content_markdown
                state["memo_user_prefix"] = prefix
                deps.emit(
                    ARTIFACT_EDIT_CONFLICT_EVENT,
                    {"artifact_id": resolved_id, "kind": "memo"},
                )
            base = current.revision
            continue
    if record is None:
        # Pathological churn (a user editing on every retry) — fail loudly,
        # never a silent corrupt write.
        raise RuntimeError("memo_flush_conflict_unresolved")

    first = not state.get("artifact_id")
    state["artifact_id"] = record.artifact_id
    state["memo_base_revision"] = record.revision
    deps.emit(
        (
            ARTIFACT_CREATED_EVENT
            if first and record.revision == 1
            else ARTIFACT_UPDATED_EVENT
        ),
        # The shared builder carries the title (without it the memo chip
        # falls back to its placeholder) plus the line delta.
        artifact_event_payload(record),
    )


def _stage_memory_candidates(
    deps: "_RunDeps", state: AgentPhaseState
) -> None:
    """Generate candidate-only long-term memories after a finished run."""
    service = deps.memory
    principal = deps.context.principal
    if service is None or principal is None or state.get("cancelled"):
        return
    if not deps.agent_memory_opt_in:
        # No opt-in -> stage nothing (same per-user privacy gate as the read
        # path in _load_memory_briefing).
        return
    status = service.status(principal)
    if (
        status.get("provider") == "none"
        or status.get("mode") == "off"
        or not status.get("principal_eligible")
    ):
        return
    memo = state.get("memo_markdown", "")
    if not memo.strip():
        return
    model, effort = deps.resolved("agent_memory_reflection")
    try:
        outcome = memory_reflection.run_memory_reflection(
            deps.llm,
            question=state.get("question", ""),
            memo_markdown=memo,
            critic_digest=_critic_digest(state),
            task_digest=_task_digest(state),
            language=_language(state),
            model=model,
            reasoning_effort=effort,
            timeout=deps.timeout,
        )
    except Exception as exc:  # noqa: BLE001 - candidate generation is optional
        log.warning(
            "Agent memory reflection failed (error_type=%s).",
            type(exc).__name__,
        )
        state["memory_status"] = "unavailable"
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "memory_unavailable",
                "label": t(state, "agent_memory_label_unavailable"),
                "detail": t(state, "agent_memory_detail_candidates_failed"),
            },
        )
        return
    _add_usage(state, outcome.usage)
    reflection = outcome.value
    if not isinstance(reflection, MemoryReflection):
        return
    staged = _run_async(
        service.stage_candidates(
            principal=principal,
            candidates=[
                candidate.model_dump() for candidate in reflection.candidates
            ],
            source_run_id=deps.context.run_id or "",
        )
    )
    state["memory_candidates"] = [
        {
            "id": candidate.candidate_id,
            "scope": candidate.scope,
            "category": candidate.category,
            "content": candidate.content,
            "reason": candidate.reason,
            "confidence": candidate.confidence,
            "status": candidate.status,
        }
        for candidate in staged
    ]
    if staged:
        state["memory_status"] = "candidate_created"
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "memory_candidate",
                "label": t(state, "agent_memory_candidate_label"),
                "count": len(staged),
                "detail": t(state, "agent_memory_candidate_detail"),
            },
        )


def _critic_digest(state: AgentPhaseState) -> str:
    """The review's outcome for the memory reflection, complete and in English.

    Every model finding with its prose, then every finding the text did not
    take over by its codes only (kind, state, reason).
    """
    critic = state.get("critic") or {}
    if not critic:
        return "(no critic report)"
    findings = [
        f"- {finding.get('kind', '')}: {finding.get('finding', '')}"
        for finding in (critic.get("review") or {}).get("findings", [])
    ]
    named = [
        f"- {entry.get('finding_id', '')} ({entry.get('kind', '')}): "
        f"{entry.get('state', '')}, {entry.get('reason', '')}"
        for entry in critic.get("findings") or []
        if isinstance(entry, Mapping) and entry.get("state") == "named"
    ]
    return "\n".join(
        [
            f"Verdict: {critic.get('verdict', '')}",
            "Uncovered: "
            + ", ".join(critic_phase.uncovered_criteria(critic)),
            *findings,
            *named,
        ]
    )


def _task_digest(state: AgentPhaseState) -> str:
    outcomes = state.get("outcomes", {})
    lines: list[str] = []
    for task_id, outcome in sorted(outcomes.items()):
        lines.append(
            f"- {task_id}: {outcome.get('status', '')}; "
            f"{str(outcome.get('summary', ''))[:300]}"
        )
    return "\n".join(lines) or "(no task results)"


# -- task execution ----------------------------------------------------------- #


def _task_failure_code(exc: Exception) -> str:
    """Map one task exception onto a stable failure contract."""
    return classify_execution_failure(exc, fallback="task_failed")


def _retryable_task_error(exc: Exception) -> bool:
    """Whether *exc* failed before a provider-owned operation ran."""
    return (
        _task_failure_code(exc)
        in RETRYABLE_AGENT_TASK_ORCHESTRATION_CODES
    )


def _emit_legacy_budget_notices(
    deps: "_RunDeps",
    state: AgentPhaseState,
    tasks: list[PlanTaskRecord],
) -> None:
    """Expose each ignored historic task budget once per checkpointed run."""
    emitted = set(state.get("legacy_budget_notice_task_ids", []))
    for task in tasks:
        if not task.budget or task.task_id in emitted:
            continue
        log.warning(
            "Agent task %s contains an obsolete task budget; its values are ignored, "
            "and operator limits remain authoritative.",
            task.task_id,
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "activity_id": f"legacy-budget:{task.task_id}",
                "scope": "task",
                "phase": "execution",
                "operation": "task.legacy_budget_ignored",
                "detail": "Legacy task budget ignored",
                "status": "completed",
                "task_id": task.task_id,
                "fallback": True,
            },
        )
        emitted.add(task.task_id)
    state["legacy_budget_notice_task_ids"] = sorted(emitted)


def _transition_task(
    deps: "_RunDeps",
    task: PlanTaskRecord,
    *,
    status: str,
    child_run_id: str | None = None,
    result_summary: str | None = None,
    result_payload: dict[str, Any] | None = None,
) -> PlanTaskRecord:
    """Persist one authoritative task-state transition before UI events."""
    return _run_async(
        deps.control.transition_plan_task(
            run_id=task.run_id,
            plan_id=task.plan_id,
            task_id=task.task_id,
            status=status,
            child_run_id=child_run_id,
            result_summary=result_summary,
            result_payload=result_payload,
        )
    )


def _execute_task(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: PlanTaskRecord,
    attempt: int,
) -> TaskOutcome:
    from inqtrix.observability import semconv
    from inqtrix.observability.context import bind_purpose
    from inqtrix.observability.otel import operation_span

    deps.emit(
        TASK_STARTED_EVENT,
        {
            "task_id": task.task_id,
            "ordinal": task.ordinal,
            "tool_kind": task.tool_kind,
            "title": task.title,
            "attempt": attempt,
        },
    )
    # One span per mission task: every provider call the task makes
    # (search, extraction, RAG answer, file analysis) nests under it and
    # carries the task id, so the trace waterfall shows which task burned
    # which tokens — the same structure the research graph has per node.
    with operation_span(
        semconv.SPAN_AGENT_TASK,
        {
            semconv.INQTRIX_TASK_ID: task.task_id,
            semconv.INQTRIX_TASK_KIND: task.tool_kind,
            semconv.INQTRIX_ATTEMPT: attempt,
        },
    ), bind_purpose(
        task.tool_kind, task_id=task.task_id, task_kind=task.tool_kind
    ):
        return _execute_task_inner(deps, state, task, attempt)


def _execute_task_inner(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: PlanTaskRecord,
    attempt: int,
) -> TaskOutcome:
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    try:
        require_task_allowed(
            task.tool_kind, policy=coerce_source_policy(deps)
        )
        # web_research never reaches this executor: _node_execute
        # partitions child tasks out and submits them as parked child
        # runs (children_wait folds their outcomes).
        if task.tool_kind == "web_instant":
            return _run_web_instant(deps, task)
        if task.tool_kind == "rag_query":
            return _run_rag_query(deps, state, task, attempt)
        if task.tool_kind == "file_analysis":
            return _run_file_analysis(deps, state, task)
        raise ValueError(f"unknown tool_kind {task.tool_kind!r}")
    except AgentCancelled:
        raise
    except CollectionNotFound as exc:
        # A plan reference the store cannot resolve is a CONTRACT error,
        # not a flaky call: retrying re-fails identically and burns more
        # operator quota. Non-transient, with a message instead of the
        # raw KeyError repr ("'EU-AI-Act'").
        # NOTE: this text becomes the task row's persisted ``failure_reason``
        # and is rendered long after the run; like the other localized task
        # failure reasons it is written in the run's language when the task
        # fails.
        # The reader's sentence names no collection: the exception carries
        # an internal id or an English message, which belong in the log.
        reason = t(state, "agent_collection_not_visible")
        log.warning(
            "Agent task %s (%s) failed (failure_code=invalid_input, "
            "detail=%s).",
            task.task_id,
            task.tool_kind,
            exc.args[0] if exc.args else type(exc).__name__,
        )
        return TaskOutcome(
            status="failed",
            failure_reason=reason,
            failure_code="invalid_input",
            transient=False,
        )
    except Exception as exc:  # noqa: BLE001 — task failures stay visible
        failure_code = _task_failure_code(exc)
        log.warning(
            "Agent task %s (%s) failed (failure_code=%s, error_type=%s).",
            task.task_id,
            task.tool_kind,
            failure_code,
            type(exc).__name__,
        )
        return TaskOutcome(
            status="failed",
            failure_reason=sanitize_error(exc),
            failure_code=failure_code,
            transient=attempt == 1 and _retryable_task_error(exc),
        )


def _submit_child_run(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: PlanTaskRecord,
    attempt: int,
    *,
    research_policy: WebResearchPolicy,
) -> str:
    """Submit one child research run; returns its run id.

    The child is a REAL run in the shared pool (kind ``agent_child``,
    parented to this run) — the parent does NOT wait here: it parks via
    the children_wait interrupt and the store wakes it when the last
    child terminates. A retry uses the same operator-owned limits. The
    child's own research-graph deadline and the parent's waiting TTL are
    the only time bounds.
    """
    require_task_allowed(
        task.tool_kind, policy=coerce_source_policy(deps)
    )
    if not research_policy.allowed:
        raise AgentPolicyDenied(
            ("web_research is allowed in a normal Agent Desk run only after an explicit "
            "research instruction or plan edit.")
        )
    existing = _existing_child_for_attempt(deps, task, attempt)
    if existing is not None:
        return str(existing["run_id"])
    profile = research_policy.profile
    if profile is None:
        raise RuntimeError("permitted web research has no profile")
    requested_profile = str(task.params.get("profile") or "")
    ceiling = research_policy.max_profile
    if (
        ceiling is not None
        and requested_profile in WEB_RESEARCH_PROFILE_ORDER
        and WEB_RESEARCH_PROFILE_ORDER[requested_profile]
        <= WEB_RESEARCH_PROFILE_ORDER[ceiling]
    ):
        # Tier runs: the validator already admitted any profile up to the
        # tier ceiling — execution must honor that choice (published ==
        # enforced), not silently normalize it back to the tier default.
        profile = requested_profile
    if requested_profile and requested_profile != profile:
        log.warning(
            "Agent task %s contains the obsolete research profile %r; the server uses "
            "%r.",
            task.task_id,
            requested_profile,
            profile,
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "scope": "task",
                "phase": "execution",
                "operation": "task.research_profile_normalized",
                "detail": f"Research-Profil auf {profile} normalisiert",
                "status": "completed",
                "task_id": task.task_id,
                "fallback": True,
            },
        )
    overrides: dict[str, Any] = {"report_profile": profile}
    if deps.tier:
        # The child inherits the mission's Stufe — the same rule the
        # kernel applies to its children (one Stufe per run tree).
        overrides["agent_tier"] = deps.tier
    resolve_payload: dict[str, Any] = {
        "mode": "research",
        "agent_overrides": overrides,
    }
    parent_stack = getattr(deps.context, "stack_name", "") or ""
    if parent_stack:
        # Parity with the kernel: a mission admitted on stack X
        # fans its research children out on the SAME provider stack, not
        # the default stack's models/search.
        resolve_payload["stack"] = parent_stack
    resolved_child = deps.resolver.resolve(resolve_payload)
    question = _task_execution_question(task)
    run_id = deps.context.run_id or ""
    summary = deps.run_service.submit(
        question=question or task.title,
        history="",
        messages=[],
        resolved=resolved_child,
        workspace_id=deps.context.workspace_id,
        principal=deps.context.principal,
        kind="agent_child",
        parent_run_id=run_id,
        root_run_id=run_id,
        session_id=state.get("session_id") or None,
        parent_task_id=task.task_id,
        parent_task_attempt=attempt,
        origin_key=_child_origin_key(task, attempt),
        source_policy={
            "web": deps.source_policy.web,
            "knowledge": deps.source_policy.knowledge,
        },
        web_recency=task.params.get("recency") or None,
    )
    return str(summary["run_id"])


def _child_task_outcome(
    deps: "_RunDeps", child_id: str, attempt: int
) -> TaskOutcome | None:
    """Compatibility wrapper around the shared pure child projector."""
    return project_child_run_outcome(
        deps.run_service.run_store,
        child_id,
        attempt,
        visible_to=getattr(deps, "visible_to", None),
    )


def _run_web_instant(deps: "_RunDeps", task: PlanTaskRecord) -> TaskOutcome:
    if deps.capabilities is None:
        return TaskOutcome(
            status="failed",
            failure_reason="web.search.instant is unavailable",
            failure_code="capability_unavailable",
        )
    query = task.queries[0] if task.queries else task.objective or task.title
    payload: dict[str, Any] = {"query": query, "max_sources": 8}
    if task.params.get("recency"):
        payload["recency"] = task.params["recency"]
    activity = {
        "kind": "searching",
        "scope": "task",
        "phase": "execution",
        "operation": "web.search.instant",
        "detail": query,
        "status": "started",
        "current": 1,
        "total": 1,
        "task_id": task.task_id,
        "query": query,
        "purpose": task.objective or task.title,
    }
    deps.emit(ACTIVITY_EVENT, activity)
    try:
        output = _run_async(
            deps.capabilities.invoke(
                "web.search.instant",
                payload,
                _capability_context(
                    deps,
                    on_provider_retry=lambda notice: deps.emit(
                        ACTIVITY_EVENT,
                        provider_retry_activity(
                            notice,
                            task_id=task.task_id,
                            purpose=task.objective or task.title,
                            language=_deps_language(deps) or "",
                        ),
                    ),
                ),
            )
        )
    except Exception as exc:
        deps.emit(
            ACTIVITY_EVENT,
            {
                **activity,
                "status": "failed",
                "error": {
                    "code": _task_failure_code(exc),
                    "message": sanitize_error(exc),
                },
            },
        )
        raise
    data = output.model_dump() if hasattr(output, "model_dump") else output
    provider_answer = str(data.get("answer", "")).strip()
    answer = normalize_agent_markdown(provider_answer)
    sources = list(data.get("sources", []))
    web_search_ledger = build_instant_web_search_ledger(
        run_id=str(deps.context.run_id or ""),
        query_id=str(data.get("query_id") or ""),
        query=str(data.get("query") or query),
        provider=str(data.get("provider") or ""),
        answer=provider_answer,
        sources=[
            dict(source) for source in sources if isinstance(source, dict)
        ],
        parameters=(
            dict(data.get("parameters"))
            if isinstance(data.get("parameters"), dict)
            else {}
        ),
        started_at=str(data.get("started_at") or ""),
        finished_at=str(data.get("finished_at") or ""),
        duration_ms=int(data.get("duration_ms", 0) or 0),
        prompt_tokens=int(data.get("prompt_tokens", 0) or 0),
        completion_tokens=int(data.get("completion_tokens", 0) or 0),
    )
    usage = {
        "prompt_tokens": int(data.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(data.get("completion_tokens", 0) or 0),
    }
    deps.emit(
        ACTIVITY_EVENT,
        {
            **activity,
            "status": "completed",
            "metrics": {
                "result_count": len(sources),
                **usage,
            },
        },
    )
    instant_evidence = evidence.enrich_instant_evidence(
        provider_answer,
        [
            {
                "url": source.get("url"),
                "title": source.get("title"),
                "provider_snippet": source.get("snippet"),
            }
            for source in sources
            if isinstance(source, dict)
        ],
    )
    instant_evidence = attach_web_search_lineage(
        instant_evidence, web_search_ledger
    )
    return TaskOutcome(
        status="completed" if answer else "insufficient_evidence",
        summary=task_result_summary(
            answer
            or "; ".join(
                str(s.get("snippet", "")) for s in sources[:3]
            )
        ),
        answer_markdown=answer,
        evidence=instant_evidence,
        web_search_ledger=web_search_ledger,
        usage=usage,
    )


class _QueryOutcome(NamedTuple):
    """What ONE sub-query produced, carrying nothing shared.

    Each query owns its result so the queries can run concurrently and
    still be folded in QUERY order — the sequential loop's observable
    behaviour, at a fraction of its wall time.
    """

    index: int
    query: str
    answer: str = ""
    references: tuple[dict[str, Any], ...] | list[dict[str, Any]] = ()
    #: Reader lines of a sub-question without a usable answer.
    warnings: tuple[str, ...] | list[str] = ()
    #: The search's raw warning mappings, a degradation's ``reason`` as its
    #: ``code``; folded into search notices across the task's queries.
    search_warnings: tuple[Any, ...] | list[Any] = ()
    usage: dict[str, Any] = {}  # noqa: RUF012 — read-only per outcome
    #: A rejected sub-answer: a named gap, not a task failure.
    failure: Any = None
    #: An exception from the capability itself; re-raised by the caller.
    raised: BaseException | None = None
    #: Never started, because a sibling had already raised.
    skipped: bool = False


class _SectionOutcome(NamedTuple):
    """What ONE memo section produced, carrying nothing shared.

    Sections are written concurrently and folded in OUTLINE order, so
    the memo grows in reading order exactly as the sequential loop
    produced it.
    """

    index: int
    title: str
    body: str = ""
    usage: dict[str, Any] = {}  # noqa: RUF012 — read-only per outcome
    #: Never written: the run was cancelled or a sibling had raised.
    skipped: bool = False
    #: Labels the outline preselected for this section, empty when it
    #: preselected none. Carried so the fold can tell "the outline chose
    #: nothing here" from "nothing was available".
    preselected: tuple[str, ...] = ()
    #: Labels the finished prose actually cites, including ones the
    #: section reached for beyond its preselection.
    cited: tuple[str, ...] = ()
    #: How the section was written: one call, or a role that read for it.
    transport: str = TRANSPORT_PROMPT
    #: Labels of the preselection a role read before it wrote - the
    #: measured share of its material it actually loaded. Empty for one call.
    read_preselected: tuple[str, ...] = ()


def _execute_queries(
    queries: list[str],
    run_one: "Callable[[int, str], _QueryOutcome]",
    max_parallel: int,
) -> list[_QueryOutcome]:
    """Run a task's sub-queries and return them in QUERY order.

    The queries of one task are independent by construction: each builds
    its prompt from ``(task, query)`` alone (``_task_query_prompt``) and
    reads nothing the others produce. Dependencies live one level up, on
    ``PlanTaskRecord.depends_on``, and are sequenced by the wave
    scheduler — this function never touches that.
    """
    return run_in_pool(
        queries,
        run_one,
        max_parallel,
        skipped=lambda index, query: _QueryOutcome(
            index=index, query=query, skipped=True
        ),
        thread_name_prefix="inqtrix-agent-query",
    )


def _run_rag_query(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: PlanTaskRecord,
    attempt: int,
) -> TaskOutcome:
    from inqtrix.core.context import RunContext
    from inqtrix.core.results import RunRequest

    try:
        knowledge = deps.runtime.registry.get("knowledge")
    except Exception as exc:  # noqa: BLE001 — mode not registered
        return TaskOutcome(
            status="failed",
            failure_reason=(
                f'Knowledge mode is unavailable: {sanitize_error(exc)}'
            ),
            failure_code="capability_unavailable",
        )
    filters: dict[str, Any] = {}
    if task.params.get("profile"):
        filters["profile"] = task.params["profile"]
    filters["collection_ids"] = _task_collection_scope(deps, task)
    queries = _task_queries(task)
    answers: list[str] = []
    references_by_key: dict[str, dict[str, Any]] = {}
    failure_lines: list[str] = []
    per_search_warnings: list[list[Any]] = []
    # Sub-queries whose answer was rejected. They do not stop the task —
    # they become a named gap in its result — but a task where EVERY
    # query failed has nothing to stand on and still fails.
    failed_queries: list[tuple[str, Any]] = []
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    max_parallel = max(
        1, int(deps.platform.max_parallel_queries_per_task or 1)
    )

    def _run_one_query(index: int, query: str) -> _QueryOutcome:
        """Everything ONE sub-query does, owning all of its own state.

        The event sink is bound to THIS query's index. A shared
        ``active_query_index`` would be exactly the kind of sharing that
        survives a sequential loop and silently misreports under
        concurrency: a grounding verdict would be filed under whichever
        query happened to be current.
        """

        def _forward_knowledge_event(
            event: str, payload: dict[str, Any]
        ) -> None:
            """Preserve nested Knowledge verdicts in mission/parent audit."""

            if event not in {
                "inqtrix.knowledge.answer.retry",
                "inqtrix.knowledge.grounding.checked",
                "inqtrix.knowledge.retrieval.degraded",
                "inqtrix.knowledge.retrieval.warning",
            }:
                return
            # Enrichment first, payload last: an event that carries its own
            # `attempt` (answer.retry's regeneration attempt, documented as
            # always 2) must not be clobbered by the mission task attempt.
            deps.emit(
                event,
                {
                    "task_id": task.task_id,
                    "attempt": attempt,
                    "query_index": index,
                    **dict(payload),
                },
            )

        activity = {
            "kind": "searching",
            "scope": "task",
            "phase": "execution",
            "operation": "knowledge.search",
            "detail": query,
            "status": "started",
            "current": index,
            "total": len(queries),
            "task_id": task.task_id,
            "query": query,
            "purpose": task.objective or task.title,
        }
        deps.emit(ACTIVITY_EVENT, activity)
        try:
            result = knowledge.run(
                RunRequest(
                    mode="knowledge",
                    question=_task_query_prompt(task, query),
                    # Own copy per query: the shared dict was safe while
                    # the loop was sequential, and a copy costs nothing.
                    knowledge_filters=dict(filters),
                ),
                runtime=deps.runtime,
                context=RunContext(
                    providers=deps.context.providers,
                    strategies=deps.context.strategies,
                    agent_settings=deps.context.agent_settings,
                    principal=deps.context.principal,
                    workspace_id=deps.context.workspace_id,
                    run_id=None,
                    cancel_token=deps.context.cancel_token,
                    event_sink=_forward_knowledge_event,
                    token_budget=int(deps.context.token_budget or 0),
                ),
            )
        except Exception as exc:
            deps.emit(
                ACTIVITY_EVENT,
                {
                    **activity,
                    "status": "failed",
                    "error": {
                        "code": _task_failure_code(exc),
                        "message": sanitize_error(exc),
                    },
                },
            )
            return _QueryOutcome(index=index, query=query, raised=exc)
        raw_state = result.raw.get("result_state", {}) or {}
        retrieval_state = raw_state.get("knowledge_retrieval", {}) or {}
        retrieval_degradations = list(
            retrieval_state.get("degradations", []) or []
        )
        retrieval_warnings = list(retrieval_state.get("warnings", []) or [])
        # A degradation names its code ``reason``; the notice contract reads
        # one ``code`` for every entry. Anything unreadable passes on as it
        # is and becomes a visible unreadable notice, never a silent drop.
        search_warnings = [
            {**degradation, "code": degradation.get("reason")}
            if isinstance(degradation, dict)
            else degradation
            for degradation in retrieval_degradations
        ] + retrieval_warnings
        usage = result.raw.get("usage", {}) or {}
        terminal_failure = result.terminal_failure
        if terminal_failure is not None:
            # A rejected sub-answer never enters the synthesis — but the
            # sibling queries that DID verify are real evidence. Throwing
            # them away would end whole runs over one malformed block:
            # five checked searches discarded because the sixth could not
            # be parsed. The GAP travels with the result instead — named
            # in the answer under the search-note heading and visible as a
            # failed step — so the synthesis writes with it in view and
            # the reader can see what is missing. Only a task where
            # NOTHING verified still fails.
            deps.emit(
                ACTIVITY_EVENT,
                {
                    **activity,
                    "status": "failed",
                    "error": {
                        "code": terminal_failure.type,
                        "message": terminal_failure.message,
                    },
                },
            )
            return _QueryOutcome(
                index=index,
                query=query,
                failure=terminal_failure,
                warnings=[
                    t(
                        state,
                        "agent_subquestion_no_result",
                        query=query,
                        reason=terminal_failure.message,
                    )
                ],
                search_warnings=search_warnings,
                usage=usage,
            )
        references = [
            dict(ref)
            for ref in raw_state.get("report_references", [])
            if isinstance(ref, dict)
        ]
        deps.emit(
            ACTIVITY_EVENT,
            {
                **activity,
                "status": "completed",
                "metrics": {
                    "result_count": len(
                        raw_state.get("report_references", []) or []
                    )
                },
                "warnings": [*retrieval_degradations, *retrieval_warnings],
            },
        )
        return _QueryOutcome(
            index=index,
            query=query,
            answer=result.answer or "",
            references=references,
            search_warnings=search_warnings,
            usage=usage,
        )

    outcomes = _execute_queries(queries, _run_one_query, max_parallel)

    # Folded in QUERY order, never in completion order. Three things here
    # are order-dependent and stay exactly as the sequential loop had
    # them: the answer text's order, which duplicate reference wins
    # (`setdefault` — the earlier query), and which failure code carries.
    for outcome in outcomes:
        if outcome.raised is not None:
            raise outcome.raised
        if outcome.skipped:
            # A sibling raised before this one started; the raise below
            # carries the failure, so it contributes nothing.
            continue
        usage_total["prompt_tokens"] += int(
            outcome.usage.get("prompt_tokens", 0) or 0
        )
        usage_total["completion_tokens"] += int(
            outcome.usage.get("completion_tokens", 0) or 0
        )
        per_search_warnings.append(list(outcome.search_warnings))
        for message in outcome.warnings:
            if message not in failure_lines:
                failure_lines.append(message)
        if outcome.failure is not None:
            failed_queries.append((outcome.query, outcome.failure))
            continue
        if outcome.answer:
            answers.append(outcome.answer)
        for ref in outcome.references:
            normalized = dict(ref)
            if not normalized.get("excerpt") and normalized.get("source_text"):
                normalized["excerpt"] = normalized["source_text"]
            key = str(
                normalized.get("url")
                or (
                    normalized.get("document_id"),
                    normalized.get("chunk_index"),
                )
            )
            references_by_key.setdefault(key, normalized)
    references = list(references_by_key.values())
    if failed_queries and not answers:
        # Nothing verified: there is no partial result to carry, so the
        # task fails with the FIRST stable code, exactly as before.
        _first_query, first_failure = failed_queries[0]
        return TaskOutcome(
            status="failed",
            summary=task_result_summary(first_failure.message),
            failure_reason=first_failure.message,
            failure_code=first_failure.type,
            usage=usage_total,
            transient=False,
        )
    answer = "\n\n".join(answers)
    # Notices are folded by code over all searches, so counts add up; the
    # answer names every notice, the open point carries only the limits.
    notices = collect_search_notices(per_search_warnings)
    note_lines = [
        *search_notice_lines(notices, str(state.get("language") or "")),
        *failure_lines,
    ]
    if note_lines:
        answer = (
            answer
            + "\n\n"
            + t(state, "search_notice_heading")
            + "\n"
            + "\n".join(f"- {line}" for line in note_lines)
        ).strip()
    return TaskOutcome(
        status=(
            "completed"
            if answer.strip() and references
            else "insufficient_evidence"
        ),
        summary=task_result_summary(answer),
        answer_markdown=normalize_agent_markdown(answer),
        evidence=references,
        usage=usage_total,
        search_notices=[
            notice.as_dict() for notice in notices if notice.severity == "limit"
        ],
    )


@dataclass(frozen=True)
class DocumentSearch:
    """What the searches of one file analysis found."""

    scope: list[str]
    """The collection ids the searches ran over."""
    ranked: list[tuple[str, str]]
    """``(document_id, title)``, best first: rank, then hit count, then id."""
    notices: list[SearchNotice]
    """What the searches reported, folded by code."""


def _search_documents(
    deps: "_RunDeps",
    task: PlanTaskRecord,
    *,
    row: Mapping[str, Any],
    row_id: str = "",
) -> DocumentSearch:
    """Run a file analysis's searches: the documents they find, best first.

    The same queries, retrieval prompts and collection scope at plan time
    and at execution (:func:`_task_queries`, :func:`_task_query_prompt`,
    :func:`_task_collection_scope`). *row* supplies each search row's scope
    fields; a non-empty *row_id* names the rows ``<row_id>:<n>``. A failed
    search shows as a failed row and raises.
    """
    per_search_warnings: list[list[Any]] = []
    queries = _task_queries(task)
    scope = _task_collection_scope(deps, task)
    found_documents: dict[str, dict[str, Any]] = {}
    for index, query in enumerate(queries, start=1):
        if deps.cancelled():
            raise AgentCancelled("Run cancelled before the next document search.")
        activity = {
            "kind": "searching",
            **row,
            "operation": "knowledge.search",
            "detail": query,
            "status": "started",
            "current": index,
            "total": len(queries),
            "query": query,
            "purpose": task.objective or task.title,
            **({"activity_id": f"{row_id}:{index}"} if row_id else {}),
        }
        deps.emit(ACTIVITY_EVENT, activity)
        try:
            output = _run_async(
                deps.capabilities.invoke(
                    "knowledge.search",
                    {
                        "query": _task_query_prompt(task, query),
                        "top_k": 8,
                        "collection_ids": scope,
                    },
                    _capability_context(deps),
                )
            )
        except Exception as exc:
            deps.emit(
                ACTIVITY_EVENT,
                {
                    **activity,
                    "status": "failed",
                    "error": {
                        "code": _task_failure_code(exc),
                        "message": sanitize_error(exc),
                    },
                },
            )
            raise
        data = output.model_dump() if hasattr(output, "model_dump") else output
        query_hits = list(data.get("hits") or [])
        query_warnings = list(data.get("warnings") or [])
        per_search_warnings.append(query_warnings)
        for hit in query_hits:
            document_id = str(hit.get("document_id") or "")
            if not document_id:
                continue
            found = found_documents.setdefault(
                document_id,
                {"title": str(hit.get("document_title") or ""), "rank": 10**6, "hits": 0},
            )
            found["rank"] = min(found["rank"], int(hit.get("rank") or 10**6))
            found["hits"] += 1
        deps.emit(
            ACTIVITY_EVENT,
            {
                **activity,
                "status": "completed",
                "metrics": {"result_count": len(query_hits)},
                "warnings": query_warnings,
            },
        )
    ranked = sorted(
        found_documents.items(),
        key=lambda item: (item[1]["rank"], -item[1]["hits"], item[0]),
    )
    return DocumentSearch(
        scope=scope,
        ranked=[(document_id, found["title"]) for document_id, found in ranked],
        # Folded by code over all searches; the reading record keeps them
        # text-free, and every reader renders them in its own language.
        notices=collect_search_notices(per_search_warnings),
    )


def _select_documents(
    deps: "_RunDeps",
    ranked: Sequence[tuple[str, str]],
    *,
    view: ReadingView | None,
    windows_left: int,
) -> DocumentSelection:
    """The documents a file analysis reads, of the *ranked* ones it found.

    A document a reading of *view* holds at a readable version is judged by
    its current version, read as window 0 reads it; one the readings hold
    only at ``''`` (each failed before its first window) is judged by that
    failure, without a call. A window-0 failure carries the store's own
    code, never a transient one, so such a document is not tried again and
    its ``already_read`` entry names the failure: it was never read. A
    current version that cannot be read keeps the document as an unread
    candidate (``unchecked``); every other document is unread. The rules are
    :func:`~inqtrix.agents.research_prospect.version_reach` and
    :func:`~inqtrix.agents.research_prospect.select_documents`, the
    prospect's own rows and window rule.
    """
    context = _capability_context(deps)
    max_windows = int(deps.platform.document_analysis_max_windows)

    def invoke(capability_id: str, payload: dict[str, Any]) -> Any:
        return _run_async(deps.capabilities.invoke(capability_id, payload, context))

    candidates: list[Candidate] = []
    for document_id, title in ranked:
        versions = held_versions(view, document_id) if view is not None else frozenset()
        if view is None or not versions:
            candidates.append(Candidate(document_id, title, "unread", "", 0))
            continue
        version = document_version(invoke, document_id) if versions - {""} else ""
        if version is None:
            candidates.append(Candidate(document_id, title, "unchecked", "", 0))
            continue
        reach = version_reach(
            view, document_id, version, max_windows=max_windows, windows_left=windows_left
        )
        candidates.append(
            Candidate(document_id, title, reach.state, version, reach.needed, reach.failure)
        )
    return select_documents(
        candidates,
        limit=max(1, int(deps.platform.document_analysis_max_documents)),
        max_windows=max_windows,
        windows_left=windows_left,
    )


def _replan_context_incomplete(
    deps: "_RunDeps | None", state: AgentPhaseState, reason: str, *, phase: str
) -> None:
    """One visible row for a part of the replan's context the server left out.

    ``reason``: ``tasks_before_round_missing`` (no document is skipped; the
    round's document analysis finds it, so its row is of the execution
    phase), ``criteria_changed`` (the review's open criteria are left out),
    ``criterion_unclassified`` (open criteria without a valid kind are left
    out) or ``prospect_missing`` (the prospect line is left out). The
    WARNING is the caller's.
    """
    if deps is None:
        # Pure helper tests build the replan context without a running graph.
        return
    round_ = int(state.get("replan_rounds", 0) or 0)
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": "replan_context_incomplete",
            "scope": "run",
            "phase": phase,
            "activity_id": f"replan-context:{round_}:{reason}",
            "reason": reason,
            "round": round_,
            "label": t(state, "agent_replan_context_incomplete_label"),
            "detail": t(state, f"agent_replan_context_incomplete_{reason}_detail"),
        },
    )


def _execution_reading_view(
    deps: "_RunDeps", state: AgentPhaseState
) -> ReadingView | None:
    """The readings of earlier rounds this round's file analysis does not
    read again (``None``: there are none, and nothing is skipped).

    Only the tasks that had run when this round's plan version was saved
    count (``tasks_before_round``): an analysis of the same round reads what
    it finds, whether or not a sibling finished before it.
    """
    before = state.get("tasks_before_round")
    if before is None:
        if int(state.get("replan_rounds", 0) or 0) >= 1:
            log.warning(
                "Document selection: the run records no tasks before this round; "
                "no document is skipped."
            )
            _replan_context_incomplete(
                deps, state, "tasks_before_round_missing", phase="execution"
            )
        return None
    if not readings_of(state.get("outcomes"), before):
        return None
    _plan, tasks = _run_async(deps.control.get_plan(deps.context.run_id or ""))
    return _reading_view(state, tasks, task_ids=before)


def _rejects_new_analysis(
    deps: "_RunDeps",
    state: AgentPhaseState,
    task: PlanTaskRecord,
    *,
    view: ReadingView,
    windows_left: int,
    round_: int,
) -> bool:
    """Whether a replan's new file analysis would read nothing new: then it is dropped.

    Searched and selected exactly as execution will do it (the task as the
    plan would save it, its readings of every earlier round, the run's
    windows left): a task whose every found document an earlier round read
    as far as a re-read reaches, whose searches find nothing, or that could
    read no window is dropped, visibly. A check that cannot run keeps the
    task, visibly: missing capabilities, a collection the run can no longer
    read, a capability error or a failed search. Under strict autonomy the
    check searches nothing (a search, even an internal one, waits for the
    user's approval there, as the discovery probes do): the task stays,
    visibly, and execution applies the rule after the replan approval. A
    cancel, an exhausted token budget and a revoked authority end the
    planning. The searches are run-scoped rows of the planning phase and
    count as no source operation of the run.
    """
    from inqtrix.capabilities.contracts import CapabilityError
    from inqtrix.capabilities.registry import UnknownCapability
    from inqtrix.knowledge.stores.ports import CollectionNotFound

    prefix = f"plan-check:{round_}:{task.task_id}"
    # The record the plan would save: execution searches with its guarded
    # title and objective, and the rows below show them.
    task = _guarded_task(state, task)
    row = {"scope": "run", "phase": "planning", "activity_id": prefix}

    def unchecked(error_type: str) -> bool:
        log.warning(
            "Replan: the document selection of new file analysis %s failed; the "
            "task stays unchecked (error_type=%s).",
            task.task_id,
            error_type,
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "replan_analysis_unchecked",
                **row,
                "error_type": error_type,
                "label": t(state, "agent_replan_analysis_unchecked_label"),
                "detail": t(state, "agent_replan_analysis_unchecked_detail", title=task.title),
            },
        )
        return False

    def rejected(detail: str, *, found: int | None, already_read: int) -> bool:
        log.info(
            "Replan: new file analysis %s dropped; it would read nothing new "
            "(found=%s, already_read=%d, windows_left=%d).",
            task.task_id,
            found,
            already_read,
            windows_left,
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "replan_analysis_rejected",
                **row,
                "found": found,
                "already_read": already_read,
                "windows_left": windows_left,
                "label": t(state, "agent_replan_analysis_rejected_label"),
                "detail": detail,
            },
        )
        return True

    if windows_left <= 0:
        # The run's windows are spent: the analysis could read nothing, so
        # nothing is searched for it.
        return rejected(
            t(state, "agent_replan_analysis_rejected_detail", title=task.title),
            found=None,
            already_read=0,
        )
    if state.get("autonomy", "balanced") == "strict":
        log.info(
            "Replan: new file analysis %s is not checked before planning; strict "
            "autonomy searches nothing before the replan approval.",
            task.task_id,
        )
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "replan_analysis_unchecked",
                **row,
                "error_type": "consent_gate",
                "label": t(state, "agent_replan_analysis_unchecked_label"),
                "detail": t(state, "agent_replan_analysis_consent_detail", title=task.title),
            },
        )
        return False
    if deps.capabilities is None:
        return unchecked("capability_unavailable")
    try:
        search = _search_documents(
            deps, task, row={"scope": "run", "phase": "planning"}, row_id=prefix
        )
        selection = _select_documents(
            deps, search.ranked, view=view, windows_left=windows_left
        )
    except AuthorizationRevoked:
        raise
    except (CapabilityError, CollectionNotFound, UnknownCapability, RuntimeError) as exc:
        return unchecked(type(exc).__name__)
    if selection.unchecked > 0:
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "document_version_unchecked",
                "scope": "run",
                "phase": "planning",
                "activity_id": f"{prefix}:versions",
                "count": selection.unchecked,
                "label": t(state, "agent_document_version_unchecked_label"),
                "detail": t_count(
                    state, "agent_document_version_unchecked_detail", selection.unchecked
                ),
            },
        )
    if selection.selected:
        return False
    if not search.ranked:
        # What the searches reported travels with the drop, as it would
        # with an execution that found nothing.
        limits = search_notice_lines(search.notices, _language(state) or "", severity="limit")
        detail = " ".join(
            [
                t(state, "agent_replan_analysis_found_nothing_detail", title=task.title),
                *([t(state, "search_notice_heading"), *limits] if limits else []),
            ]
        )
        return rejected(detail, found=0, already_read=0)
    return rejected(
        t(state, "agent_replan_analysis_rejected_detail", title=task.title),
        found=len(search.ranked),
        already_read=len(selection.already_read),
    )


def _run_file_analysis(
    deps: "_RunDeps", state: AgentPhaseState, task: PlanTaskRecord
) -> TaskOutcome:
    """Read the documents the task's queries find, completely.

    The task's searches only SELECT documents (search order: best rank,
    then hit count); the window sweep of ``agents.document_analysis`` reads
    each selected document to its last character and returns verified
    chunk evidence. A document version an earlier round of the run read as
    far as a re-read reaches is not read again (:func:`_select_documents`);
    a task whose documents were all read so completes without reading and
    names them. Search hits never become evidence themselves, and the
    task summary is server-rendered: the analysis prose reaches the reader,
    never a later prompt.
    """
    from inqtrix.agents.document_analysis import (
        AnalysisPorts,
        analyse_documents,
        analysis_answer,
        analysis_summary,
    )

    if deps.capabilities is None:
        return TaskOutcome(
            status="failed",
            failure_reason="File capabilities are unavailable",
            failure_code="capability_unavailable",
        )
    search = _search_documents(
        deps,
        task,
        row={"scope": "task", "phase": "execution", "task_id": task.task_id},
    )
    language = _deps_language(deps) or ""
    words = {"language": language}
    notices = search.notices
    note_lines = search_notice_lines(notices, language)
    warning_note = (
        "\n\n"
        + t(words, "search_notice_heading")
        + " "
        + " ".join(note_lines)
        if note_lines
        else ""
    )
    if not search.ranked:
        return TaskOutcome(
            status="insufficient_evidence",
            summary=task_result_summary(
                t(words, "agent_task_summary_no_document_content") + warning_note
            ),
            # Nothing was read, so the limits of the searches that found
            # nothing are all this task can report as an open point.
            search_notices=[
                notice.as_dict() for notice in notices if notice.severity == "limit"
            ],
        )
    view = _execution_reading_view(deps, state)
    selection = _select_documents(
        deps,
        search.ranked,
        view=view,
        windows_left=deps.document_windows_left(state) if view else 0,
    )
    if selection.unchecked > 0:
        deps.emit(
            ACTIVITY_EVENT,
            {
                "kind": "document_version_unchecked",
                "scope": "task",
                "phase": "execution",
                "task_id": task.task_id,
                "activity_id": f"{task.task_id}:versions",
                "count": selection.unchecked,
                "label": t(words, "agent_document_version_unchecked_label"),
                "detail": t_count(
                    words, "agent_document_version_unchecked_detail", selection.unchecked
                ),
            },
        )
    limit = max(1, int(deps.platform.document_analysis_max_documents))
    queries = _task_queries(task)
    context = _capability_context(deps)
    model, effort = deps.resolved("agent_file_analysis")

    def invoke(capability_id: str, payload: dict[str, Any]) -> Any:
        return _run_async(deps.capabilities.invoke(capability_id, payload, context))

    def emit(payload: dict[str, Any]) -> None:
        # Row identity per task: two tasks may read the same document.
        deps.emit(
            ACTIVITY_EVENT,
            {
                "scope": "task",
                "phase": "execution",
                "task_id": task.task_id,
                "purpose": task.objective or task.title,
                **payload,
                "activity_id": f"{task.task_id}:{payload.get('activity_id', '')}",
            },
        )

    ports = AnalysisPorts(
        invoke=invoke,
        llm=deps.llm,
        model=model,
        reasoning_effort=effort,
        timeout=deps.timeout,
        language=language,
        cancelled=deps.cancelled,
        # A task cancel stops the reading at the next window instead of
        # paying for the rest of a document whose result is discarded.
        stopped=lambda: _current_plan_task(deps, task).status == "cancel_requested",
        emit=emit,
        claim_windows=lambda wanted: deps.claim_document_windows(state, wanted),
        max_windows=int(deps.platform.document_analysis_max_windows),
        max_parallel=int(deps.platform.max_parallel_queries_per_task),
    )
    outcome = analyse_documents(
        ports,
        collection_documents=_scope_document_count(deps, search.scope),
        # The questions travel once, as the numbered criteria.
        objective="\n".join(
            line
            for line in (
                f"Task: {task.title}",
                f"Objective: {task.objective or task.title}",
                f"Expected output: {task.expected_output}" if task.expected_output else "",
            )
            if line
        ),
        criteria=queries,
        documents=list(selection.selected),
        not_selected=list(selection.not_selected),
        already_read=list(selection.already_read),
        document_limit=limit,
        retrieval_warnings=[notice.as_dict() for notice in notices],
    )
    record = outcome.report.model_dump()
    if not selection.selected:
        # Every document found was read by an earlier round as far as a
        # re-read reaches: nothing was read, and nothing failed. Completed,
        # not insufficient_evidence (which would open the gate and a
        # sufficiency call) and not skipped (an upstream failure); the review
        # reads the record as its reading record, the writers as its facts.
        return TaskOutcome(
            status="completed",
            summary=task_result_summary(analysis_summary(record, language)),
            answer_markdown=normalize_agent_markdown(analysis_answer(record, language)),
            evidence=[],
            usage=outcome.usage,
            document_analysis=record,
        )
    if outcome.halted is not None and not any(
        document.windows_analysed for document in outcome.report.documents
    ):
        # The provider failed before any window was analysed: the task
        # fails with the provider's reason, and its report — with the window
        # booking — stays, so the run's accounting and the reader see it.
        # The reason stays short: what was read travels in the record, and
        # every reader renders it from there.
        return TaskOutcome(
            status="failed",
            failure_reason=sanitize_error(outcome.halted),
            failure_code=_task_failure_code(outcome.halted),
            answer_markdown=normalize_agent_markdown(analysis_answer(record, language)),
            usage=outcome.usage,
            document_analysis=record,
            transient=False,
        )
    if not any(not document.failure for document in outcome.report.documents):
        return TaskOutcome(
            status="failed",
            failure_reason=t(words, "document_analysis_unreadable"),
            failure_code="document_unreadable",
            answer_markdown=normalize_agent_markdown(analysis_answer(record, language)),
            usage=outcome.usage,
            document_analysis=record,
            transient=False,
        )
    return TaskOutcome(
        status="completed" if outcome.evidence else "insufficient_evidence",
        summary=task_result_summary(analysis_summary(record, language)),
        answer_markdown=normalize_agent_markdown(analysis_answer(record, language)),
        evidence=list(outcome.evidence),
        usage=outcome.usage,
        document_analysis=record,
    )


def _scope_document_count(deps: "_RunDeps", scope: list[str]) -> int | None:
    """Documents in the collections a task searched (``None`` when unknown).

    Tells a completeness claim its population: the documents a task's
    searches found are not the whole scope.
    """
    try:
        catalog = _collection_catalog(deps)
    except Exception as exc:  # noqa: BLE001 — the count is a reader fact, not a gate
        log.warning(
            "Document count of the searched collections unavailable (error_type=%s).",
            type(exc).__name__,
        )
        return None
    if catalog is None:
        log.warning("Document count of the searched collections unavailable (no catalog).")
        return None
    wanted = set(scope)
    return sum(entry.document_count for entry in catalog if entry.collection_id in wanted)


# -- helpers ------------------------------------------------------------------ #


def _task_queries(task: PlanTaskRecord) -> list[str]:
    """Queries a task should actually execute, never just the first one."""
    queries = [query.strip() for query in task.queries if query.strip()]
    return queries or [task.objective or task.title]


def _task_execution_question(task: PlanTaskRecord) -> str:
    """Full task contract passed to child agents and analytical tools."""
    lines = [
        f"Task: {task.title}",
        f"Objective: {task.objective or task.title}",
    ]
    queries = _task_queries(task)
    if queries:
        lines.append("Queries:")
        lines.extend(f"- {query}" for query in queries)
    if task.expected_output:
        lines.append(f"Expected output: {task.expected_output}")
    return "\n".join(lines)


def _task_query_prompt(task: PlanTaskRecord, query: str) -> str:
    """One retrieval query framed by the full task contract."""
    prompt = _task_execution_question(task)
    return f"{prompt}\n\nCurrent query: {query}"


def _task_collection_scope(
    deps: "_RunDeps", task: PlanTaskRecord
) -> list[str]:
    """Visible collection ids for internal retrieval tasks."""
    explicit = [
        str(item).strip()
        for item in (task.params.get("collection_ids") or [])
        if str(item).strip()
    ]
    if explicit:
        return deps.assert_collections(explicit)
    return deps.collection_scope()


def _collection_catalog(
    deps: "_RunDeps",
) -> list[CollectionCatalogEntry] | None:
    """Currently visible metadata inside the immutable run boundary.

    ``None`` (no knowledge service wired) tells the planner path to skip
    catalog handling entirely; an EMPTY list is a real answer ("this
    run admitted no collections") and makes every explicit reference a
    validation error. Newly visible collections never enter a running plan.
    """
    if deps.source_policy.knowledge != "available":
        return []
    if deps.knowledge is None:
        return None
    admitted = set(deps.collection_scope())
    if not admitted:
        return []
    collections = _run_async(
        deps.knowledge.list_collections(visible_to=deps.visible_to)
    )
    return [
        CollectionCatalogEntry(
            collection_id=str(item.id),
            name=str(item.name),
            embedding_model=str(getattr(item, "embedding_model", "") or ""),
            document_count=int(getattr(item, "document_count", 0) or 0),
        )
        for item in collections
        if str(item.id) in admitted
    ]


def _ensure_approval(
    deps: "_RunDeps",
    run_id: str,
    approval_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
) -> ApprovalRecord:
    """Idempotent approval creation (interrupt nodes re-execute)."""
    from inqtrix.agents.control_ports import ApprovalNotFound

    try:
        return _run_async(deps.control.get_approval(run_id, approval_id))
    except ApprovalNotFound:
        record = _run_async(
            deps.control.create_approval(
                ApprovalRecord(
                    approval_id=approval_id,
                    run_id=run_id,
                    kind=kind,
                    subject_type=(
                        "plan"
                        if kind in ("plan", "replan")
                        else "editor_patch" if kind == "patch" else ""
                    ),
                    subject_id=(
                        str(payload.get("patch_id", ""))
                        if kind == "patch"
                        else str(payload.get("plan_id", ""))
                        if kind in ("plan", "replan")
                        else ""
                    ),
                    payload=payload,
                )
            )
        )
        deps.emit(
            APPROVAL_REQUESTED_EVENT,
            {
                "approval_id": record.approval_id,
                "kind": kind,
                "subject_type": record.subject_type,
                "subject_id": record.subject_id,
            },
        )
        return record


def _capability_context(
    deps: "_RunDeps",
    *,
    on_provider_retry: Callable[[dict[str, object]], None] | None = None,
) -> Any:
    from inqtrix.capabilities import CapabilityContext

    return CapabilityContext(
        principal=deps.context.principal,
        visible_to=deps.visible_to,
        workspace_id=deps.context.workspace_id,
        run_id=deps.context.run_id,
        knowledge_collection_ids=deps.knowledge_collection_ids,
        search_provider=deps.context.providers.search,
        authority_check=getattr(deps.context, "authority_check", None),
        on_provider_retry=on_provider_retry,
    )


def _result_count(output: Any) -> int:
    """Count the primary result rows of a capability output."""
    data = output.model_dump() if hasattr(output, "model_dump") else output
    if not isinstance(data, dict):
        return 0
    for key in ("sources", "hits", "results", "collections"):
        rows = data.get(key)
        if isinstance(rows, list):
            return len(rows)
    return 0


def _profile_of(state: AgentPhaseState) -> AssignmentProfile | None:
    raw = state.get("profile")
    return AssignmentProfile.model_validate(raw) if raw else None


def _discovery_of(state: AgentPhaseState) -> DiscoveryResult | None:
    raw = state.get("discovery")
    return DiscoveryResult.model_validate(raw) if raw else None


def _web_allowed(deps: "_RunDeps") -> bool:
    return (
        deps.capabilities is not None
        and getattr(deps.context.providers, "search", None) is not None
        and deps.source_policy.web == "available"
    )


def _plan_lineage_web_facts(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    current_plan: PlanRecord | None = None,
    current_tasks: list[PlanTaskRecord] | None = None,
) -> tuple[bool, int | None]:
    """The user's own web choice as the plan history holds it (reads only).

    Returns ``(consent, checked_version)``: ``checked_version`` is the
    newest plan version this read inspected, ``None`` when the checkpoint
    alone answered and there is nothing new to record.
    """
    run_id = deps.context.run_id or ""

    def _user_selected_web(
        plan: PlanRecord, tasks: list[PlanTaskRecord]
    ) -> bool:
        return plan.created_by == "user" and any(
            task.tool_kind in WEB_TASK_KINDS for task in tasks
        )

    if current_plan is not None and _user_selected_web(
        current_plan, list(current_tasks or [])
    ):
        return True, current_plan.version
    if state.get("web_consent", False):
        return True, None

    versions = _run_async(deps.control.list_plan_versions(run_id))
    newest_version = max((plan.version for plan in versions), default=0)
    if int(state.get("web_consent_checked_version", 0) or 0) >= newest_version:
        return False, None
    for plan in versions:
        if plan.created_by != "user":
            continue
        _stored, tasks = _run_async(
            deps.control.get_plan(run_id, version=plan.version)
        )
        if _user_selected_web(plan, tasks):
            return True, newest_version
    return False, newest_version


def _plan_lineage_web_consent(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    current_plan: PlanRecord | None = None,
    current_tasks: list[PlanTaskRecord] | None = None,
) -> bool:
    """Recover the user's own web choice from plan history, and keep it.

    A plan's ``created_by`` describes authorship, not an execution policy. The
    checkpoint keeps the already-established decision on the hot path, while
    persisted user-authored plan versions remain the recovery source after a
    deployment or an older checkpoint. Consent is granted when a user version
    holds a web task of either kind: a user who adds one instant search at
    the gate asked for the web as much as one who adds a research run.
    """
    consent, checked_version = _plan_lineage_web_facts(
        deps, state, current_plan=current_plan, current_tasks=current_tasks
    )
    if checked_version is not None:
        state["web_consent"] = consent
        state["web_consent_checked_version"] = checked_version
    return consent


def _user_demanded_web(deps: "_RunDeps", *, edit_consent: bool) -> bool:
    """Whether the user demanded the web in a form code can read.

    A ``web_research`` directive, a web task of the user's own in the plan,
    or activated skills that allow only web tools. Each outranks the
    intake's reading, for the discovery probe and the planner alike.
    """
    skill_kinds = allowed_task_kinds(deps.skills)
    return (
        "web_research" in deps.request.tool_directives
        or edit_consent
        or (
            skill_kinds is not None
            and bool(skill_kinds & WEB_TASK_KINDS)
            and not skill_kinds & KNOWLEDGE_TASK_KINDS
        )
    )


def _sources_answered(state: AgentPhaseState) -> bool:
    """Whether the user answered a clarification question about the sources.

    The answer is free text or a model-worded option, so code cannot read
    whether it includes the web: it opens the choice for the planner, which
    reads the answer and is told that it decides, never for the discovery
    probe, which would send text whose direction nobody checked.
    """
    return "sources" in (state.get("answered_subjects") or [])


def _web_task_in_lineage(deps: "_RunDeps") -> bool:
    """Whether any plan version of this run holds a web task (reads only).

    Every version counts, the agent's and the user's: a web task a later
    version skipped was still planned. Read from the stored versions, never
    from the tool counts, which include the discovery's probes. A
    control-store error propagates: :func:`_prospect_facts` closes the arm
    and names the failed read.
    """
    run_id = deps.context.run_id or ""
    for plan in _run_async(deps.control.list_plan_versions(run_id)):
        _stored, tasks = _run_async(
            deps.control.get_plan(run_id, version=plan.version)
        )
        if any(task.tool_kind in WEB_TASK_KINDS for task in tasks):
            return True
    return False


class _CollectionFacts(NamedTuple):
    """What :func:`_unread_collections` found for one prospect decision."""

    unread: int
    unavailable: bool
    """A collection not yet searched can no longer be read."""
    failed: bool
    """The scope or the catalog could not be read, so ``unread`` is not a
    count but the value that closes the branch."""


def _unread_collections(
    deps: "_RunDeps",
    tasks: Sequence[PlanTaskRecord],
    outcomes: Mapping[str, Any],
) -> _CollectionFacts:
    """Admitted collections no knowledge task of this run has searched.

    A knowledge task counts once it ran (``completed`` or
    ``insufficient_evidence``); its scope is its explicit
    ``collection_ids``, otherwise the whole admitted scope, which leaves no
    collection unread. An unsearched collection counts while it holds
    documents and the run may still read it. One the catalog no longer
    lists, one the run may no longer read and a failed scope or catalog read
    make the prospect say that a collection is unavailable. A failed scope
    or catalog read, and a catalog that is not wired, also mark the count as
    failed: it is then no count, only the value that closes the branch.
    """

    def unavailable(collection_id: str, error_type: str) -> None:
        log.warning(
            "Collection %s counts as unavailable for the prospect (error_type=%s).",
            collection_id,
            error_type,
        )

    try:
        admitted = list(deps.collection_scope())
    except Exception as exc:  # noqa: BLE001 — named in the prospect, never a failed run
        unavailable("(admitted scope)", type(exc).__name__)
        return _CollectionFacts(0, True, True)
    searched: set[str] = set()
    for task in tasks:
        outcome = outcomes.get(task.task_id)
        if (
            task.tool_kind not in KNOWLEDGE_TASK_KINDS
            or not isinstance(outcome, Mapping)
            or outcome.get("status") not in {"completed", "insufficient_evidence"}
        ):
            continue
        explicit = [
            str(item).strip()
            for item in (task.params.get("collection_ids") or [])
            if str(item).strip()
        ]
        if not explicit:
            return _CollectionFacts(0, False, False)
        searched.update(explicit)
    candidates = [item for item in admitted if item not in searched]
    if not candidates:
        return _CollectionFacts(0, False, False)
    try:
        catalog = _collection_catalog(deps)
    except Exception as exc:  # noqa: BLE001 — named in the prospect, never a failed run
        for collection_id in candidates:
            unavailable(collection_id, type(exc).__name__)
        return _CollectionFacts(0, True, True)
    if catalog is None:
        log.warning(
            "Research prospect: no knowledge catalog is wired; no collection "
            "counts as unread."
        )
        return _CollectionFacts(0, False, True)
    documents = {entry.collection_id: entry.document_count for entry in catalog}
    unread = 0
    gone = False
    for collection_id in candidates:
        if collection_id not in documents:
            unavailable(collection_id, "missing_from_catalog")
            gone = True
            continue
        if documents[collection_id] <= 0:
            continue
        try:
            deps.assert_collections([collection_id])
        except Exception as exc:  # noqa: BLE001 — named in the prospect, never a failed run
            unavailable(collection_id, type(exc).__name__)
            gone = True
            continue
        unread += 1
    return _CollectionFacts(unread, gone, False)


def _run_windows_left(deps: Any, outcomes: Mapping[str, Any] | None) -> int:
    """The run's document-analysis windows its recorded outcomes left.

    The one formula behind the claim, the peek, the prospect and the
    plan-time check.
    """
    return max(
        0,
        int(deps.platform.document_analysis_max_windows_per_run)
        - document_windows_used(outcomes),
    )


def _reading_view(
    state: AgentPhaseState,
    tasks: Sequence[PlanTaskRecord],
    *,
    task_ids: Sequence[str] | None = None,
) -> ReadingView:
    """The run's readings (of *task_ids* when given) with their plan positions.

    The plan position orders readings of two versions of one document: a
    replan appends its tasks after every task that already ran.
    """
    readings = readings_of(state.get("outcomes"), task_ids)
    positions = {task.task_id: int(task.ordinal) for task in tasks}
    unplaced = sorted(set(readings) - set(positions))
    if unplaced:
        log.warning(
            "Reading view: readings of tasks %s have no position in the current "
            "plan; they void no reading of another document version.",
            ", ".join(unplaced),
        )
    return ReadingView(
        readings=readings,
        order={
            task_id: position
            for task_id, position in positions.items()
            if task_id in readings
        },
    )


def _prospect_facts(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    plan: PlanRecord | None,
    tasks: Sequence[PlanTaskRecord],
) -> ProspectFacts:
    """Everything one prospect decision reads, gathered without a state write.

    Each input fails on its own: a failed read closes the branch it feeds,
    logs a WARNING and is named in ``failed_inputs`` (``web_task_planned``,
    ``user_demanded_web``, ``unread_collections``), so the activity tells it
    apart from a true negative. A collection the run can no longer read
    shows as ``collection_unavailable``.
    """
    outcomes = dict(state.get("outcomes") or {})
    failed: list[str] = []
    collections = _unread_collections(deps, tasks, outcomes)
    if collections.failed:
        failed.append("unread_collections")
    try:
        edit_consent, _checked = _plan_lineage_web_facts(
            deps, state, current_plan=plan, current_tasks=list(tasks)
        )
    except Exception as exc:  # noqa: BLE001 — one closed arm, never a failed run
        log.warning(
            "Research prospect: the user's web choice could not be read from the "
            "plan versions; it counts as not given (error_type=%s).",
            type(exc).__name__,
        )
        edit_consent = False
        failed.append("user_demanded_web")
    sources_answered = _sources_answered(state)
    web_task_planned = False
    if sources_answered:
        try:
            web_task_planned = _web_task_in_lineage(deps)
        except Exception as exc:  # noqa: BLE001 — one closed arm, never a failed run
            log.warning(
                "Research prospect: the plan versions could not be read; no web "
                "task counts as planned (error_type=%s).",
                type(exc).__name__,
            )
            failed.append("web_task_planned")
    view = _reading_view(state, tasks)
    kinds = allowed_task_kinds_for_policy(
        allowed_task_kinds(deps.skills), policy=deps.source_policy
    )
    web_kinds = set(WEB_TASK_KINDS) if kinds is None else WEB_TASK_KINDS & kinds
    research_policy = derive_web_research_policy(
        depth=deps.depth,
        admitted_directive=(
            "web_research" in deps.request.tool_directives or edit_consent
        ),
        tier=deps.tier or None,
    )
    # validate_plan counts every web_instant of the merged plan, carried
    # tasks included: the current version's count is what a replan starts at.
    instant_tasks = sum(1 for task in tasks if task.tool_kind == "web_instant")
    return ProspectFacts(
        readings=view.readings,
        reading_order=view.order,
        max_windows=int(deps.platform.document_analysis_max_windows),
        windows_left=_run_windows_left(deps, outcomes),
        unread_collections=collections.unread,
        collection_unavailable=collections.unavailable,
        web_allowed=_web_allowed(deps),
        web_task_kinds_admitted=bool(web_kinds),
        web_capacity=(
            ("web_research" in web_kinds and research_policy.allowed)
            or (
                "web_instant" in web_kinds
                and (
                    research_policy.max_instant_tasks is None
                    or instant_tasks < research_policy.max_instant_tasks
                )
            )
        ),
        profile=_profile_of(state),
        user_demanded_web=_user_demanded_web(deps, edit_consent=edit_consent),
        sources_answered=sources_answered,
        web_task_planned=web_task_planned,
        replan_headroom=replan_headroom(tasks, max_tasks=int(deps.platform.max_plan_tasks)),
        failed_inputs=tuple(failed),
    )


def _prospect_detail(
    state: AgentPhaseState, prospect: Prospect, *, route: str
) -> str:
    """The reader's sentence for a prospect decision.

    Open: which unread material research can still reach. Closed: a plan
    without a free task slot first, then every code that closed the branches
    themselves. ``extraction_limit`` closes nothing, so it never stands in
    the sentence that says nothing unread is reachable: it has a sentence of
    its own, on an open answer too. The web note only where the assignment's
    reading left the web out, and on the gate route what happens to the
    round's evidence.
    """
    parts: list[str] = []
    if prospect.reachable:
        parts.append(t(
            state,
            "agent_research_prospect_open_detail",
            branches="; ".join(
                t(state, f"research_branch_{branch}") for branch in prospect.branches
            ),
        ))
    else:
        if prospect.inputs.get("replan_headroom") == 0:
            parts.append(t(state, "agent_critic_plan_task_limit_detail"))
        closing = [code for code in prospect.codes if code != "extraction_limit"]
        if closing:
            parts.append(t(
                state,
                "agent_research_prospect_closed_detail",
                codes="; ".join(t(state, f"research_code_{code}") for code in closing),
            ))
    if "extraction_limit" in prospect.codes:
        parts.append(t(state, "agent_research_prospect_extraction_detail"))
    if "web_not_needed" in prospect.codes:
        parts.append(t(state, WEB_NOT_NEEDED_NOTE_KEY))
    if route == "gate" and not prospect.reachable:
        parts.append(t(state, "agent_research_prospect_gate_detail"))
    return " ".join(parts)


def _research_prospect(
    deps: "_RunDeps",
    state: AgentPhaseState,
    *,
    plan: PlanRecord | None,
    tasks: Sequence[PlanTaskRecord],
    route: str,
) -> Prospect:
    """Decide whether research can reach unread material, and say so.

    Evaluated where a replan is decided (``critic`` or ``gate``), before the
    round is counted, so ``round`` is the round the research would open.
    Stored for the planner and shown as its own activity with its inputs; a
    decision of this run only, never projected to a parent.
    """
    prospect = research_can_reach(_prospect_facts(deps, state, plan=plan, tasks=tasks))
    round_ = int(state.get("replan_rounds", 0) or 0) + 1
    state["research_prospect"] = {
        "round": round_,
        "route": route,
        "reachable": prospect.reachable,
        "branches": list(prospect.branches),
        "codes": list(prospect.codes),
    }
    deps.emit(
        ACTIVITY_EVENT,
        {
            "kind": "research_prospect",
            "scope": "run",
            "activity_id": f"prospect:{route}:{round_}",
            "route": route,
            "round": round_,
            "reachable": prospect.reachable,
            "branches": list(prospect.branches),
            "codes": list(prospect.codes),
            "inputs": dict(prospect.inputs),
            "label": t(
                state,
                "agent_research_prospect_open_label"
                if prospect.reachable
                else "agent_research_prospect_closed_label",
            ),
            "detail": _prospect_detail(state, prospect, route=route),
        },
    )
    return prospect


def _research_child_policy(
    deps: "_RunDeps", *, edit_consent: bool
) -> WebResearchPolicy:
    """Server authority for a multi-step mission research child."""
    return derive_web_research_policy(
        depth=deps.depth,
        admitted_directive=(
            "web_research" in deps.request.tool_directives or edit_consent
        ),
        tier=deps.tier,
    )


REPLAN_ENTRY_CHARS = 400
"""Characters of one task's answer or failure reason the replan context shows."""

REPLAN_POSITION_CHARS = 180
"""Characters of one contradiction position the replan context shows."""

REPLAN_CONTRADICTIONS_SHOWN = 5
"""Contradictions the replan context shows; the rest are counted."""


def _excerpt(text: Any, limit: int) -> tuple[str, bool]:
    """*text* shortened visibly within *limit* characters
    (:data:`~inqtrix.text.LOG_CUT_MARKER` at the cut), and whether it was
    cut."""
    normalized = " ".join(str(text or "").split())
    return shorten_visibly(normalized, limit, marker=LOG_CUT_MARKER), len(normalized) > limit


def _build_replan_context(state: AgentPhaseState) -> str:
    """Compact additive-replan context for the planner.

    The server's own lines stay instructions: statuses, failure codes, the
    open criteria by their number in the plan's list, the prospect that
    opened the round, "do not plan again". What sources, models or the
    review wrote travels fenced and bounded, every cut named: the tasks'
    answers and failure reasons, the readings, the sufficiency and review
    gaps, the contradiction positions.
    """
    if state.get("replan_rounds", 0) <= 0:
        return ""
    deps = _DEPS.get()
    lines: list[str] = []
    outcomes = state.get("outcomes", {})
    readings: list[str] = []
    # A task's answer is what its sources said, and a failure reason can
    # carry a child's error text: data for the planner, fenced like the
    # readings, while the server's own lines around it stay instructions.
    summaries: list[str] = []
    cut: list[str] = []
    if outcomes:
        lines.append("Task outcomes so far:")
        for task_id, outcome in sorted(outcomes.items()):
            detail = f" - {task_id}: {outcome.get('status', '')}"
            if outcome.get("failure_code"):
                detail += f"; failure_code={outcome.get('failure_code')}"
            lines.append(detail)
            record = outcome.get("document_analysis") or {}
            if has_reading(record):
                # What the analysis read, as server facts; below, fenced.
                readings.append(f"{task_id}: " + " ".join(analysis_facts(record, "en")))
                continue
            entries = []
            if outcome.get("summary"):
                entries.append((f"{task_id}: ", outcome.get("summary")))
            if outcome.get("failure_reason"):
                entries.append((f"{task_id} failed: ", outcome.get("failure_reason")))
            for head, text in entries:
                excerpt, was_cut = _excerpt(text, REPLAN_ENTRY_CHARS)
                summaries.append(head + excerpt)
                if was_cut and task_id not in cut:
                    cut.append(task_id)
    if summaries:
        lines.append(
            "What the finished tasks returned (the start of each answer; for a "
            "failed task, its failure reason):"
        )
        lines.append(untrusted_fence("\n".join(summaries), "task-summaries"))
    if cut:
        lines.append(
            REPLAN_ENTRIES_CUT.format(limit=REPLAN_ENTRY_CHARS, task_ids=", ".join(cut))
        )
    if readings:
        lines.append(
            REREADING_HINT.format(
                limits="; ".join(
                    REREADING_LIMIT_WORDS[reason] for reason in sorted(RETURNS_ON_REREADING)
                )
            )
        )
        lines.append(untrusted_fence("\n".join(readings), "task-readings"))
    gap_rule = [REPLAN_GAP_RULE]

    def gap_fence(head: str, body: str, source: str) -> None:
        # The rule once, before the first gap fence.
        lines.extend(gap_rule)
        gap_rule.clear()
        lines.append(head)
        lines.append(untrusted_fence(body, source))

    sufficiency = state.get("sufficiency") or {}
    missing = [" ".join(str(item).split()) for item in sufficiency.get("missing") or []]
    if any(missing):
        gap_fence(
            REPLAN_SUFFICIENCY_GAPS_HEAD,
            "\n".join(f"- {item}" for item in missing if item),
            "sufficiency-gaps",
        )
    contradictions = list(state.get("contradictions", []))
    if contradictions:
        shown = contradictions[:REPLAN_CONTRADICTIONS_SHOWN]
        entries = []
        positions_cut = 0
        for item in shown:
            internal, internal_cut = _excerpt(item.get("internal_position", ""), REPLAN_POSITION_CHARS)
            external, external_cut = _excerpt(item.get("external_position", ""), REPLAN_POSITION_CHARS)
            positions_cut += internal_cut + external_cut
            entries.append(f" - {internal} VS {external}")
        lines.append("Known contradictions (the positions as the sources state them):")
        lines.append(untrusted_fence("\n".join(entries), "contradictions"))
        if len(contradictions) > len(shown):
            lines.append(
                REPLAN_MORE_CONTRADICTIONS.format(count=len(contradictions) - len(shown))
            )
        if positions_cut:
            lines.append(
                REPLAN_POSITIONS_CUT.format(limit=REPLAN_POSITION_CHARS, count=positions_cut)
            )
    critic = state.get("critic") or {}
    uncovered = list(critic.get("criteria_uncovered") or [])
    # An open criterion without a valid kind (older history) has no route,
    # research or text: it is counted and shown, never guessed.
    classified = [
        entry
        for entry in uncovered
        if isinstance(entry, Mapping)
        and entry.get("kind") in {"information", "output_requirement"}
    ]
    if uncovered and list(critic.get("criteria") or []) != list(_plan_criteria(state)):
        # The review numbered another list than the plan holds now (a user
        # edit at the gate): its numbers would name other criteria.
        log.warning(
            "Replan context: the last review numbered other success criteria than "
            "the plan holds; its open criteria are left out."
        )
        _replan_context_incomplete(deps, state, "criteria_changed", phase="planning")
        lines.append(REPLAN_CRITERIA_CHANGED)
    elif uncovered:
        gaps: list[str] = []
        for entry in sorted(classified, key=lambda item: int(item.get("index") or 0)):
            index = int(entry.get("index") or 0)
            if entry.get("kind") == "information":
                lines.append(REPLAN_CRITERION_OPEN.format(index=index))
                gap = " ".join(str(entry.get("gap") or "").split())
                if gap:
                    # Collapsed to one line: an embedded "\n2: ..." cannot
                    # forge another criterion's entry.
                    gaps.append(f"{index}: {gap}")
            else:
                # An output requirement: its gap text is never rendered,
                # research cannot close it.
                lines.append(REPLAN_CRITERION_TEXT_DEFECT.format(index=index))
        unclassified = len(uncovered) - len(classified)
        if unclassified:
            log.warning(
                "Replan context: %d open criteria have no valid classification; "
                "they are left out.",
                unclassified,
            )
            _replan_context_incomplete(deps, state, "criterion_unclassified", phase="planning")
            lines.append(REPLAN_CRITERIA_UNCLASSIFIED.format(count=unclassified))
        if gaps:
            gap_fence(REPLAN_CRITIC_GAPS_HEAD, "\n".join(gaps), "critic-gaps")
    prospect = state.get("research_prospect") or {}
    if prospect.get("reachable") and prospect.get("round") == state["replan_rounds"]:
        lines.append(
            REPLAN_PROSPECT_LINE.format(
                branches="; ".join(
                    REPLAN_BRANCH_WORDS[branch] for branch in prospect.get("branches") or []
                )
            )
        )
    else:
        # The gate and the review store the prospect of the round they open
        # before it is counted: a missing or stale one is a defect.
        log.warning(
            "Replan context: no research prospect for round %d; the prospect line "
            "is left out.",
            state["replan_rounds"],
        )
        _replan_context_incomplete(deps, state, "prospect_missing", phase="planning")
    if deps is not None and _run_windows_left(deps, outcomes) <= 0:
        # The plan-time check drops every new file_analysis now, whatever
        # branch the prospect opened: the planner learns it before planning.
        lines.append(REPLAN_WINDOWS_SPENT)
    if state.get("route") == "critic_research":
        lines.append(
            "Critic verdict: new research required; plan additively."
        )
    done = sorted(outcomes)
    if done:
        lines.append(
            "Do not plan already completed task ids again: "
            + ", ".join(done)
        )
    return "\n".join(lines)


# -- graph wiring --------------------------------------------------------------- #


def _build_graph(saver: Any) -> Any:
    from langgraph.graph import END, StateGraph

    graph = StateGraph(AgentPhaseState)
    graph.add_node("intake", _node_intake)
    graph.add_node("clarify", _node_clarify)
    graph.add_node("discovery", _node_discovery)
    graph.add_node("clarify_gaps", _node_clarify)
    graph.add_node("plan", _node_plan)
    graph.add_node("plan_approval", _node_plan_approval)
    graph.add_node("execute", _node_execute)
    graph.add_node("children_wait", _node_children_wait)
    graph.add_node("evidence", _node_evidence)
    graph.add_node("replan_gate", _node_replan_gate)
    graph.add_node("synthesize", _node_synthesize)
    graph.add_node("synthesis_wait", _node_synthesis_wait)
    graph.add_node("critic", _node_critic)
    graph.add_node("review_wait", _node_review_wait)
    graph.add_node("patch", _node_patch)
    graph.add_node("patch_approval", _node_patch_approval)
    graph.add_node("finalize", _node_finalize)

    graph.set_entry_point("intake")

    def _without_discovery() -> bool:
        deps = _DEPS.get()
        policy = deps.tier_policy if deps is not None else None
        return policy is not None and not policy.discovery

    def _after_intake(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        route = state.get("route", "discover_first")
        if _without_discovery():
            # Speed tier (schnell): no probes. Intake questions still pass
            # the clarify node, whose zero-round cap turns each one into
            # a named assumption instead of an interrupt.
            return "clarify" if route == "ask_user_first" else "plan"
        if route == "ask_user_first":
            return "clarify"
        if route == "plan_now":
            return "plan"
        return "discovery"

    graph.add_conditional_edges("intake", _after_intake)

    def _after_clarify(state: AgentPhaseState) -> str:
        if state.get("cancelled"):
            return "finalize"
        if _without_discovery():
            return "plan"
        return "discovery" if state.get("discovery") is None else "plan"

    graph.add_conditional_edges("clarify", _after_clarify)

    def _after_discovery(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("plan_rejected"):
            return "finalize"
        # A second round exists only for something to ASK: a blocking
        # gap is research the plan must close, not a question.
        discovery = state.get("discovery") or {}
        if discovery.get("questions_for_user") or state.get(
            "skill_questions"
        ):
            return "clarify_gaps"
        return "plan"

    graph.add_conditional_edges("discovery", _after_discovery)

    def _after_clarify_gaps(state: AgentPhaseState) -> str:
        return "finalize" if state.get("cancelled") else "plan"

    graph.add_conditional_edges("clarify_gaps", _after_clarify_gaps)

    def _after_plan(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        # A discarded replan (round >= 1) continues with the previous plan
        # version: the gate route synthesizes from the evidence in hand,
        # the critic route keeps its memo (same vocabulary as
        # ``_after_critic``). An EMPTY critic replan takes the same exit:
        # its plan version is saved and approved by the plan node itself.
        route = state.get("route")
        if route == "synthesize":
            return "synthesize"
        if route == "critic_research_exhausted":
            return "patch" if state.get("target_document_id") else "finalize"
        return "plan_approval"

    graph.add_conditional_edges("plan", _after_plan)

    def _after_plan_approval(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("plan_rejected"):
            return "finalize"
        if state.get("replan_noop"):
            return "synthesize"
        return "execute"

    graph.add_conditional_edges("plan_approval", _after_plan_approval)

    def _after_execute(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("pending_children"):
            return "children_wait"
        return "evidence"

    graph.add_conditional_edges("execute", _after_execute)

    def _after_children_wait(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("pending_children"):
            # A transient child was resubmitted (or an early wake left
            # children outstanding): park again for the next round.
            return "children_wait"
        # Fold done: execute re-enters with the remaining todo (next
        # wave) — an empty todo falls straight through to evidence.
        return "execute"

    graph.add_conditional_edges("children_wait", _after_children_wait)

    def _after_evidence(state: AgentPhaseState) -> str:
        return "finalize" if state.get("cancelled") else "replan_gate"

    graph.add_conditional_edges("evidence", _after_evidence)

    def _after_replan_gate(state: AgentPhaseState) -> str:
        if state.get("cancelled"):
            return "finalize"
        return "plan" if state.get("route") == "replan" else "synthesize"

    graph.add_conditional_edges("replan_gate", _after_replan_gate)

    def _after_synthesize(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("synthesis_pending"):
            return "synthesis_wait"
        deps = _DEPS.get()
        if (
            deps is not None
            and deps.tier_policy is not None
            and not review_required(deps.tier_policy.verify)
        ):
            # Speed tier: citation-label validation already ran inside
            # synthesis; the critic pass is deliberately skipped. A
            # document-targeted assignment still MUST propose its patch —
            # skipping the critic never skips the deliverable.
            return "patch" if state.get("target_document_id") else "finalize"
        return "critic"

    graph.add_conditional_edges("synthesize", _after_synthesize)

    def _after_synthesis_wait(state: AgentPhaseState) -> str:
        return "finalize" if state.get("cancelled") else "synthesize"

    graph.add_conditional_edges("synthesis_wait", _after_synthesis_wait)

    def _after_critic(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        if state.get("review_pending"):
            return "review_wait"
        if state.get("route") == "critic_research":
            return "plan"
        # The patch phase runs only for assignments that TARGET an editor
        # document; the target travels on the run request, mirrored into
        # state by intake so the routing stays checkpoint-replayable.
        return "patch" if state.get("target_document_id") else "finalize"

    graph.add_conditional_edges("critic", _after_critic)

    def _after_review_wait(state: AgentPhaseState) -> str:
        return "finalize" if state.get("cancelled") else "critic"

    graph.add_conditional_edges("review_wait", _after_review_wait)

    def _after_patch(state: AgentPhaseState) -> str:
        if state.get("cancelled") or state.get("failure"):
            return "finalize"
        # No edits proposed -> nothing to approve (honest no-op note).
        return "patch_approval" if state.get("patch_id") else "finalize"

    graph.add_conditional_edges("patch", _after_patch)

    graph.add_edge("patch_approval", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=saver)
