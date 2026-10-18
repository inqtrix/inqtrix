"""Per-segment kernel dependencies via ContextVar.

The compiled kernel graph is SHARED across runs and users (the graph
itself is stateless, the thread_id separates runs) — everything one
segment needs travels through :data:`_KERNEL_DEPS` exactly like the
phase machine's ``_DEPS``. Tools and the delegating chat provider read
it at call time; the algorithm sets/resets it around each segment.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from inqtrix.agents.context_sources import SourceCoverageLedger
from inqtrix.agents.tier_policy import blocked_kernel_tools, resolve_tier_policy
from inqtrix.core.results import SourcePolicy

if TYPE_CHECKING:
    from inqtrix.agents.control_ports import AgentControlStore
    from inqtrix.providers.base import LLMProvider
    from inqtrix.settings import AgentPlatformSettings

log = logging.getLogger("inqtrix")



BLOCKED_TOOL_HINTS: dict[str, str] = {
    "run_deep_mission": (
        "a mission exists only in the tier 'tief'; use run_web_research "
        "or delegate_batch with research assignments"
    ),
    "delegate_batch": "use the existing results",
    "run_web_research": "use the existing results",
    "ask_user": "make a visibly named assumption",
}
"""What the model is told to do instead of a tier-blocked tool. The
blocked set itself is derived from the tier table
(:func:`inqtrix.agents.tier_policy.blocked_kernel_tools`) and enforced at
:meth:`KernelDeps.require_tool_allowed`, never prompt-only."""


_TIER_SOURCE_BUDGETS: dict[str, tuple[str, str, str]] = {
    # tool name -> (policy field, tool_use_counts key, label)
    "web_instant": ("web_instant_budget", "web", "web search(es)"),
    "search_project_knowledge": (
        "knowledge_search_budget",
        "knowledge",
        "project-knowledge search(es)",
    ),
}


def _tier_source_budget(
    tier: str, tool_name: str
) -> tuple[int, str, str] | None:
    """The (limit, counter key, label) a tier puts on one source tool, or
    ``None`` when the tier does not cap it. Read from the frozen tier
    table so published (``/v1/capabilities`` ``agent.tiers``) equals
    enforced — no literal budget lives here."""
    if not tier or tool_name not in _TIER_SOURCE_BUDGETS:
        return None
    field_name, counter, label = _TIER_SOURCE_BUDGETS[tool_name]
    limit = getattr(resolve_tier_policy(tier), field_name)
    if limit is None:
        return None
    return int(limit), counter, label


@dataclass
class KernelDeps:
    """Everything one kernel segment execution needs (never checkpointed).

    Attributes:
        run_id: The executing run (= LangGraph thread_id).
        session_id: The conversation the run belongs to; kernel
            deliverables scope to it (the session-wide deliverable
            index), ``""`` for session-less API runs (then run-scoped).
        control: Control store for clarification/approval rows.
        platform: Server-side agent limits (never prompted).
        llm: The provider resolved for this request's provider stack.
        model: Resolved model override for the ``agent_kernel`` node
            (tier resolution result), ``None`` for provider default.
        reasoning_effort: Resolved effort override, ``None`` when unset.
        timeout: Per-LLM-call timeout in seconds.
        event_sink: Structured run-event emitter; ``None`` without an
            event stream.
        capability_registry: The curated read-only capability surface;
            ``None`` degrades every capability tool to a visible
            not-available result, never a crash.
        capability_context: Per-segment identity for capability calls —
            built by the ALGORITHM from the verified principal (never a
            tool argument).
        run_service: Child-run submission seam (``None`` disables the
            child tools visibly).
        resolver: Builds child ``ResolvedAgentContext``s.
        principal: The verified submitting identity, threaded into
            child submissions for attribution/quota.
        depth: The run's thoroughness: ``normal`` or
            ``deep``. Deep forces the DEEP child research profile and
            gates the extra verification pass.
        autonomy: The run's wire autonomy mode; child missions inherit
            it.
        skill_service: Skill lookups for ``load_skill`` and the
            disclosure block (``None`` disables both visibly).
        skills: ACTIVATED skills of this segment (attached at submit +
            loaded via ``load_skill``, incl. reconstructions from
            checkpointed load markers).
        allowed_tool_union: Kernel-tool allowlist union of the
            activated skills; ``None`` = unrestricted. Enforced at the
            tool-body chokepoint (:meth:`require_tool_allowed`).
        source_policy: Effective per-run availability of web and project
            knowledge (web as switched for the session, knowledge iff the
            message attached a collection).
        web_research_allowed: Server-derived permission for a multi-step
            research child. The normal Kernel may choose the shared compact
            Research-Desk workflow adaptively; speed tiers can still disable
            it deterministically.
        web_research_profile: Server-selected profile for a permitted
            research child; ``None`` when the tool is not permitted.
        tool_use_counts: Successful source-tool invocations in this run
            segment, split into web and project knowledge for transparency.
        tool_call_limit: Effective run-wide tool-call allowance after folding
            explicit, durable extension decisions into the configured base.
        tool_call_ceiling: Operator maximum for that allowance; models and
            clients cannot widen it.
        step_limit: Effective cumulative checkpointed graph-step allowance.
        step_ceiling: Operator maximum for an explicit step extension.
        checkpointed_steps: Steps already committed before this segment.
        tool_calls_used: All model-requested tool calls committed in the
            checkpoint, including non-source tools.
        cancel_token: Per-run cancel event; observed before every model
            call (the kernel's node boundary — a tool loop has no other
            deterministic chokepoint).
        token_budget: Optional HARD per-run token cap
            (``RunContext.token_budget``, 0 = off). Checked against the
            RUN-cumulative total: ``usage`` restarts per segment, so
            ``prior_usage`` carries what earlier segments spent.
        prior_usage: Token totals of all EARLIER segments, reconstructed
            from the ``usage_metadata`` stamped on checkpointed AI
            messages (``_checkpointed_usage``) — the checkpoint itself is
            the bookkeeping channel, not the run row. Feeds the budget
            check and the run-cumulative ``usage_total`` (``run_usage``),
            never the per-segment quota delta.
        usage: THIS segment's token accumulator — fresh per ``run()``
            call, so the returned delta can never double-book earlier
            segments against the quota.
        context_trigger_tokens: Per-run compaction threshold in tokens
            (0 = compaction disabled for the segment). Resolved at
            deps-build time from the platform pin or the resolved
            model's card; the shared compiled graph reads it through
            the deps ContextVar, so one graph serves every model.
        context_offload_chars: Bulk tool results above this many
            characters are archived in full without shortening the model
            handover (0 disables eager archival).
    """

    run_id: str
    control: "AgentControlStore"
    platform: "AgentPlatformSettings"
    llm: "LLMProvider"
    model: str | None
    reasoning_effort: str | None
    timeout: float
    model_overrides: dict[str, str] = field(default_factory=dict)
    """Selected model, model tier and effort before the Deep-mode fallback.

Includes effective request settings and skill pins, but no model or effort
inferred from the kernel's own role. Final review and mission children use
this selection; research children keep their independent model routing.
"""
    session_id: str = ""
    clarified_answers: list[str] = field(default_factory=list)
    """Question/answer pairs the user already settled via ``ask_user``.

    The answer reaches the MODEL through the tool result, but the
    coverage judge only ever saw ``question`` — so after the user named
    what was missing, the verdict still reported it as missing and the
    run kept working against the original wording. Rebuilt on
    resume because the tool re-executes; deduped for that reason.
    """
    request_user_text: str = ""
    """The user's own words on the durable request, as one block.

    ``session_history`` and ``memory_briefing`` are filled only where a
    segment builds a user message, so a resumed segment no longer holds
    what the user wrote in an earlier turn or as a result requirement. The
    writer guard needs exactly that text to tell an identifier the USER
    named from one the model introduced, and the request payload is
    rebuilt from storage on every attempt - so the guard reads it here.
    """
    attached_report_ids: tuple[str, ...] = ()
    """Research-Desk reports the USER attached to this run.

    The attachment is the consent: ``read_research_report`` refuses every
    id outside this tuple, so the model cannot read an arbitrary run it
    merely has visibility for. Resolved server-side at submit time and
    frozen for the run.
    """
    attached_report_titles: dict[str, str] = field(default_factory=dict)
    """The titles of :attr:`attached_report_ids`, resolved with them: a tool
    row names a report by the title the user saw, never by its run id."""
    event_sink: Any = None
    capability_registry: Any = None
    capability_context: Any = None
    run_service: Any = None
    resolver: Any = None
    principal: Any = None
    autonomy: str = "balanced"
    depth: str = "normal"
    tier: str = ""
    """Selected Agent-Desk Stufe ('' = legacy depth semantics). The
    schnell rules are enforced deterministically at the tool chokepoint
    (``require_tool_allowed``: one-web-search budget, blocked tools) —
    the run's user message merely RESTATES them for the model."""
    verify_level: str = ""
    """This run's published report-quality level (see
    ``tier_policy.resolve_verify_level``), which decides whether the run
    owes a review pass. Empty is NOT a level: the segment resolves it
    before the loop, and an empty value means the resolution was
    skipped rather than that the run verifies nothing."""
    skill_service: Any = None
    skills: list[Any] = field(default_factory=list)
    skill_answers: dict[str, dict[str, str]] = field(default_factory=dict)
    report_requirement: str = ""
    """The composed result requirement this run is frozen under (free text
    plus attached rules, each with its origin marker). The kernel writes
    under it through its first user message; a single mission child it
    starts with ``run_deep_mission`` inherits it, because that mission's
    memo is the deliverable."""
    report_rules: tuple[dict[str, Any], ...] = ()
    """The library rules this run is frozen under, as the router resolved
    them (``{template_id, label, title, revision}`` per distinct rule). The
    transcript descriptor binds their revisions, so another run reads
    this run's transcripts only while the rules are unchanged and still
    visible to it; a single mission child inherits them with the text."""
    session_history: str = ""
    artifact_registry: tuple[dict[str, Any], ...] = ()
    collection_catalog: tuple[Any, ...] | None = None
    """Knowledge collections this run may reach, listed for the
    model in the user message. ``None`` means the catalog could not be
    read (block omitted — never a silent "no collections"); an empty
    tuple is a real, stated state. Latched by
    ``collection_catalog_loaded`` so a Deep run's second message build
    does not list twice."""
    collection_catalog_loaded: bool = False
    last_response_form: str = ""
    prior_evidence_count: int = 0
    effective_response_form: str = ""
    question: str = ""
    allowed_tool_union: set[str] | None = None
    source_policy: SourcePolicy = field(default_factory=SourcePolicy)
    web_research_allowed: bool = False
    web_research_profile: str | None = None
    tool_use_counts: dict[str, int] = field(
        default_factory=lambda: {"web": 0, "knowledge": 0}
    )
    web_searches: int = 0
    """Instant web searches this run executed itself, success only: the
    provider ran each one. Rebuilt from the checkpointed tool messages at
    segment start (``_checkpointed_web_searches``). A delegated research
    spends web budget but is no search of this run."""
    source_coverage: SourceCoverageLedger = field(default_factory=SourceCoverageLedger)
    """What this run actually read of every source version:
    rebuilt from checkpointed tool artifacts at segment start
    (``_checkpointed_source_coverage``) and fed by the update stream. The
    sufficiency judge reads it; the execution snapshot publishes it."""
    tool_call_limit: int = 0
    tool_call_ceiling: int = 0
    knowledge_scope_explicit: bool = False
    knowledge_scope: str | None = None
    """The persisted scope label of the admitted request (``"all"`` for the
    session switch), handed to delegated children with the boundary."""
    """Whether the run's knowledge scope is the USER's own pick.

    Submission pins every knowledge-capable run to a concrete collection
    set, so the pinned ids alone cannot distinguish a deliberate scope
    from the "everything visible" default. This flag carries that intent
    into the balanced gate predicate: a user-scoped search is already
    approved by the act of scoping. Fail-closed — an unset flag (legacy
    row, missing segment context) keeps the gate."""
    tool_grants: frozenset[str] = frozenset()
    """Run-wide tool grants: tools the user approved with
    ``approval_scope='run'``, folded from decided approval rows at every
    segment start (``effective_tool_grants``). Only the balanced-mode
    gate predicates consult this — strict stays per-call and
    ``ALWAYS_GATED_TOOLS`` are excluded by construction."""
    target_document_id: str = ""
    """Editor document the run explicitly targets, mirrored from
    ``RunRequest.document_id`` — the mission engine's
    ``target_document_id`` precedent. When set, the editor tools refuse
    every other document; empty means the model may work on any document
    it has read."""
    work_profile: Any = None
    """The ``WorkProfile`` of an internal role run; ``None`` for the
    conversational kernel. A role has a bounded tool surface, enforced at
    compile time (the tool is absent from the prompt) AND at the dispatch
    chokepoint below — one visibility, two layers."""
    released_sources: frozenset[tuple[str, str]] = frozenset()
    """``(coverage kind, source id)`` pairs a role run may re-read: what its
    parent run read or produced, taken from the typed handover. A role
    re-reads, it never acquires — so it needs no user approval of its own
    and can reach nothing its parent did not already reach."""
    released_evidence_run_id: str = ""
    """The run whose evidence ledger this role was handed (the handover's
    ``evidence_run_id``), set by :meth:`hydrate_evidence` once its visibility
    was checked. That run is the parent the role works for and is parked on
    it while the role reads, so it has no completed result by construction;
    its ledger is read like this run's own, never re-authorized as a
    finished foreign origin."""
    editor_read_receipts: dict[str, int] = field(default_factory=dict)
    """Durable read receipts: document_id -> revision the model
    actually READ in this run via ``read_editor_document``/
    ``search_editor_document``. ``propose_editor_patch`` refuses targets
    without a receipt and pins the receipt revision as
    ``expected_revision`` (server-side 409 on drift). Reconstructed at
    segment start from checkpointed ToolMessages with a producing-tool
    check — losing a receipt only forces a re-read (fail-closed), unlike
    a lost skill RESTRICTION, which aborts the resume."""
    step_limit: int = 0
    step_ceiling: int = 0
    checkpointed_steps: int = 0
    tool_calls_used: int = 0
    emitted_tool_start_ids: dict[str, str] = field(default_factory=dict)
    emitted_tool_finish_ids: set[str] = field(default_factory=set)
    """Logical tool invocations already represented in the durable event
    stream. Starts retain the tool name by invocation ID because middleware
    replies can omit it. Seeded from the checkpoint before every segment so approval,
    clarification, and child-wait resumes do not replay start/finish events."""
    tool_call_ordinals: dict[str, int] = field(default_factory=dict)
    """Each counted call's position in the run's sequence of calls, by
    invocation ID, taken when its start is emitted. Its finish event carries
    it: the counter in the finish snapshot says how many calls settled, not
    which one this is, and a batch of parallel calls shares one counter.
    Live only; a role never parks, and a resumed segment's earlier calls
    have settled already."""
    evidence_refs: dict[str, dict[str, Any]] = field(default_factory=dict)
    web_search_ledger: dict[str, Any] = field(
        default_factory=lambda: {
            "schema_version": 1,
            "kind": "web_search_ledger",
            "searches": {},
        }
    )
    _evidence_lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False
    )
    cancel_token: threading.Event | None = None
    token_budget: int = 0
    prior_usage: dict[str, int] = field(default_factory=dict)
    usage: dict[str, int] = field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0}
    )
    context_trigger_tokens: int = 0
    context_offload_chars: int = 0
    context_input_budget: int = 0
    context_sources_available: bool = False
    _source_receipt: dict[str, Any] = field(default_factory=dict, repr=False)
    _source_receipt_lock: Any = field(default_factory=threading.RLock, repr=False)
    source_service: Any = None
    context_has_external_inputs: bool = False
    memory: Any = None
    """The shared AgentMemoryService (None = feature not wired)."""
    memory_opt_in: bool = False
    """Per-user opt-in resolved once per segment; a read error degrades
    to False visibly (memory is never silently enabled)."""
    memory_briefing: str = ""
    """Recalled non-evidentiary briefing — context, never citable."""
    memory_recalled: bool = False
    """Once-per-segment recall latch (deep verify rebuilds the user
    message and must not trigger a second briefing round-trip)."""
    stack_name: str = ""
    """Provider stack of THIS run ("" = default) — threaded into child
    submissions so a parent on stack X never fans out on default."""
    memory_status: str = "disabled"
    """disabled | used | empty | unavailable — mirrors the mission
    engine's vocabulary so both engines report memory identically."""
    post_mission_nudge_pending: bool = False
    """Set when a mission child returned; the next model turn receives
    one advisory to reuse its memo and retained evidence before addressing
    a concrete remaining gap within the existing research permissions."""
    todo_in_progress_index: int | None = None
    """Index of the first ``in_progress`` item of the LAST emitted todo
    list; every following ``tool.started`` carries it as ``todo_index``
    so the transcript can fold tool rows under the todo they served."""
    review_facts: dict[str, Any] = field(default_factory=dict)
    """What this segment's review decided, for the outcome record:
    ``last_review`` (its status, round and, when unavailable, reason) and, for
    a completed review, the ``assessment``, the ``results`` settled so far
    and the ``language``. The record builds the review report from them
    when it is sent, so a located finding no revision decided yet is named
    by the exit that stopped it. Written by ``_deep_verify`` at each
    decision, so a cancel or error after the verdict still reports it, and
    as ``last_review`` ``waived`` when the run's Stufe skips the review.
    Never checkpointed: a resumed segment rebuilds them when it decides its
    review again (a role's verdict is read from its existing child, an
    in-prompt review is called again)."""
    outcome_sent: bool = False
    """Whether this segment sent its outcome record."""

    def emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(event_type, payload)

    def book_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.usage["prompt_tokens"] += max(0, int(prompt_tokens or 0))
        self.usage["completion_tokens"] += max(0, int(completion_tokens or 0))

    def run_usage(self) -> dict[str, int]:
        """Run-cumulative spend: earlier segments plus this one.

        THE accumulator behind the token budget and the published
        ``usage_total`` — ``usage`` alone is the segment delta the quota
        books exactly once.
        """
        return {
            key: int(self.prior_usage.get(key, 0) or 0)
            + int(self.usage.get(key, 0) or 0)
            for key in ("prompt_tokens", "completion_tokens")
        }

    def activate_skill(self, record: Any) -> None:
        """Track one activated skill and widen the tool allowlist."""
        if any(existing.id == record.id for existing in self.skills):
            return
        self.skills.append(record)
        from inqtrix.agents.skills_runtime import allowed_tool_names

        self.allowed_tool_union = allowed_tool_names(self.skills)

    def require_tool_allowed(self, tool_name: str) -> None:
        """The skill, tier and source-policy dispatch chokepoint.

        Raises:
            PermissionError: The activated skills restrict tools and
                *tool_name* is outside the union, or the schnell tier
                blocks the tool — surfaced as a loud tool error the
                model must acknowledge.
        """
        authority_check = getattr(
            self.capability_context, "authority_check", None
        )
        if authority_check is not None:
            authority_check()
        if (
            self.work_profile is not None
            and tool_name not in self.work_profile.allowed_tools
        ):
            log.warning(
                "Tool %s blocked: outside the '%s' work profile.",
                tool_name,
                self.work_profile.name,
            )
            from inqtrix.exceptions import AgentPolicyDenied

            raise AgentPolicyDenied(
                f"Tool {tool_name} is not part of the "
                f"'{self.work_profile.name}' role."
            )
        source_budget = _tier_source_budget(self.tier, tool_name)
        if source_budget is not None:
            limit, counter, label = source_budget
            used = self.tool_use_counts.get(counter, 0)
            if used >= limit:
                # The published tier budget (tier table, `/v1/capabilities`)
                # is enforced here; the checkpoint-derived counter survives
                # park/resume, so the bound holds across the whole run
                # (never prompt-only).
                log.warning(
                    "%s blocked by the '%s' tier budget (%d of %d %s used).",
                    tool_name,
                    self.tier,
                    used,
                    limit,
                    label,
                )
                from inqtrix.exceptions import AgentPolicyDenied

                raise AgentPolicyDenied(
                    f"The '{self.tier}' tier allows at most {limit} {label} "
                    "per run — answer from the results you already have."
                )
        if self.tier and tool_name in blocked_kernel_tools(
            resolve_tier_policy(self.tier)
        ):
            # The Stufen ladder is a deterministic budget, never
            # prompt-only: schnell removes interrupts and children,
            # gruendlich removes the mission — a run without a Stufe
            # (legacy depth) keeps every tool.
            log.warning(
                "Tool %s blocked by tier %s.",
                tool_name,
                self.tier,
            )
            from inqtrix.exceptions import AgentPolicyDenied

            raise AgentPolicyDenied(
                f"Tool {tool_name} is not available in the tier "
                f"'{self.tier}' — "
                + BLOCKED_TOOL_HINTS.get(tool_name, "answer in the chat")
                + "."
            )
        union = self.allowed_tool_union
        # ask_user is the interrupt PRIMITIVE, not a work tool: skill
        # allowed_tools never scoped it (routing it through this
        # chokepoint is new with the tier enforcement), and a skill must
        # not silently remove the human gate.
        if (
            union is not None
            and tool_name not in union
            and tool_name != "ask_user"
        ):
            log.warning(
                "Tool %s blocked by skill allowed_tools (allowed: %s).",
                tool_name,
                ", ".join(sorted(union)),
            )
            from inqtrix.exceptions import AgentPolicyDenied

            raise AgentPolicyDenied(
                f"Tool {tool_name} is not allowed by the activated skills "
                f"(allowed: {', '.join(sorted(union))})."
            )
        from inqtrix.agents.source_policy import require_kernel_tool_allowed

        require_kernel_tool_allowed(tool_name, policy=self.source_policy)

    def require_released(self, kind: str, source_id: str) -> None:
        """A role re-reads what its parent run reached — nothing else.

        No-op for the conversational kernel. For a role run the target of
        every reader must be one of the handover's released sources.

        Raises:
            AgentPolicyDenied: The target lies outside the handover.
        """
        if self.work_profile is None or (kind, source_id) in self.released_sources:
            return
        log.warning(
            "Read blocked: %s target outside the handover of role '%s'.",
            kind,
            self.work_profile.name,
        )
        from inqtrix.exceptions import AgentPolicyDenied

        raise AgentPolicyDenied(
            "This source is not part of your handover. A role re-reads what "
            "the assigning run released; name the gap in your result instead."
        )

    @property
    def gathered_sources(self) -> bool:
        """Whether THIS run searched or read sources itself.

        References also enter the ledger by reading a document that
        carries them; those are not this run's basis of work.
        """
        return any(count > 0 for count in self.tool_use_counts.values())

    def record_source_tool_use(self, source: str) -> None:
        """Increment one successful source-tool invocation."""
        if source not in ("web", "knowledge"):
            raise ValueError(f"Unknown source-tool counter: {source!r}")
        self.tool_use_counts[source] = self.tool_use_counts.get(source, 0) + 1

    def user_text(self) -> str:
        """Every text of this run the user wrote, as one guard haystack.

        The writer guard asks one question of it: does this identifier
        stand in the user's own words? Order and duplication do not matter,
        so the blocks are simply joined - but WHOSE words they are decides
        whether the guard holds, so each block is projected to its user
        half first (:func:`report_quality.user_authored_text`).

        ``skill_answers`` is deliberately absent: its values are filled by
        an extraction call (``check_skill_points``) unless the user was
        asked, so it is model output wearing the user's shape.

        KNOWN GAP, deliberate: ``session_history`` is hydrated only where
        a segment composes a user message, and a segment RESUMED from an
        interrupt composes none. Its earlier turns are then missing here,
        so an id the user wrote two turns ago is read as the model's and
        the writer guard asks for a repair it did not need. That is the
        safe direction - a guard without a user side would ask for the
        same repair - but it makes one run answer differently before and
        after a resume. Closing that gap would change what a resumed
        segment loads, which is graph-resume semantics.
        """
        from inqtrix.agents.report_quality import user_authored_text

        return user_authored_text(
            self.question,
            self.request_user_text,
            history=self.session_history,
            clarifications=self.clarified_answers,
        )

    def guard_reader_text(self, text: str) -> str:
        """One text this run hands a reader, with any leaked id taken out.

        This run's binding of the shared guard: what its ledger could
        have been copied from, minus what the user wrote themselves. Only
        the authorship question — the surfaces it serves are written once
        and never edited, so "was it already standing" has no earlier
        version to ask about.

        A canvas write can refuse and let the loop try again. These
        cannot: a narration line and a clarification round describe or
        interrupt something that already happened, and the turn that
        produced them is over. So the repair is deterministic — the
        identifier is replaced by a marker in the run's answer language,
        which keeps the step visible instead of dropping text the reader
        would never learn was missing.
        """
        from inqtrix.agents.report_quality import (
            guarded_reader_text,
            writer_identifiers,
        )
        from inqtrix.i18n import t
        from inqtrix.prompt_language import resolve_answer_language

        with self._evidence_lock:
            registered = list(self.evidence_refs.values())
        return guarded_reader_text(
            text,
            writer_identifiers(registered),
            authored_by=self.user_text(),
            marker=t(
                {"language": resolve_answer_language(self.question).code},
                "agent_reader_identifier_removed",
            ),
        )

    @staticmethod
    def reference_id(ref: dict[str, Any]) -> str:
        """Return the stable opaque id of one canonical evidence record."""
        from inqtrix.agents.evidence import dedup_key

        key = dedup_key(ref)
        if not key:
            return ""
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return f"ref_{digest}"

    @classmethod
    def ledger_id(cls, ref: dict[str, Any]) -> str:
        """The id a ledger files a reference under: its own, else its content hash.

        One derivation for every reader and writer of a ledger, so the id a
        role reads a source under is the id its parent confirms it by.
        """
        return str(ref.get("reference_id") or cls.reference_id(ref))

    def hydrate_evidence(self, run_id: str = "") -> None:
        """Restore the evidence ledger: this run's after park/resume, or a released run's.

        Args:
            run_id: Another run whose ledger a role run re-reads (the
                handover's ``evidence_run_id``). Its visibility is checked
                first, so a revoked grant leaves the role without that
                ledger instead of reading it past the revocation. Empty
                restores this run's own ledger.

        Raises:
            AgentPolicyDenied: The released run is no longer visible.
        """
        from inqtrix.agents.control_ports import ArtifactNotFound

        source_run_id = run_id or self.run_id
        if source_run_id != self.run_id:
            store = getattr(self.run_service, "run_store", None)
            try:
                if store is None:
                    raise LookupError("run store unavailable")
                store.get(source_run_id, visible_to=self.visible_to)
            except Exception as exc:  # noqa: BLE001 — every miss is a denial
                from inqtrix.exceptions import AgentPolicyDenied

                raise AgentPolicyDenied(
                    "The released evidence ledger is no longer readable."
                ) from exc
            self.released_evidence_run_id = source_run_id
        artifact_id = f"art_{source_run_id[-12:]}_evidence"
        try:
            record, _ = run_coro(
                self.control.get_artifact(source_run_id, artifact_id)
            )
        except ArtifactNotFound:
            return
        changed = False
        with self._evidence_lock:
            raw_ledger = record.payload.get("web_search_ledger")
            if isinstance(raw_ledger, dict):
                from inqtrix.evidence import merge_web_search_ledgers

                self.web_search_ledger = merge_web_search_ledgers(
                    [self.web_search_ledger, raw_ledger]
                )
            for raw in record.refs:
                ref = dict(raw)
                ref_id = self.ledger_id(ref)
                if ref_id:
                    ref["reference_id"] = ref_id
                    changed = (
                        self._ensure_reference_label(ref_id, ref) or changed
                    )
                    self.evidence_refs[ref_id] = ref
            if changed and source_run_id == self.run_id:
                # A released ledger is read, never written back.
                self._persist_evidence_artifact(artifact_id=artifact_id)

    def register_references(
        self, refs: list[dict[str, Any]] | tuple[dict[str, Any], ...]
    ) -> list[dict[str, Any]]:
        """Merge trusted tool evidence and persist the canonical ledger."""
        from inqtrix.agents.evidence import merge_missing_evidence_fields
        from inqtrix.urls import safe_public_url_identity

        registered: list[dict[str, Any]] = []
        changed = False
        with self._evidence_lock:
            for raw in refs:
                ref = dict(raw)
                raw_url = str(ref.get("url") or "").strip()
                if raw_url:
                    if raw_url.casefold().startswith(
                        ("http://", "https://")
                    ):
                        try:
                            ref["url"] = safe_public_url_identity(raw_url).url
                        except ValueError:
                            continue
                    elif not raw_url.casefold().startswith("inqtrix://"):
                        continue
                ref_id = self.ledger_id(ref)
                if not ref_id:
                    continue
                ref["reference_id"] = ref_id
                existing = self.evidence_refs.get(ref_id)
                if existing is None:
                    self._ensure_reference_label(ref_id, ref)
                    self.evidence_refs[ref_id] = ref
                    existing = ref
                    changed = True
                else:
                    changed = (
                        merge_missing_evidence_fields(existing, ref) or changed
                    )
                    changed = (
                        self._ensure_reference_label(ref_id, existing)
                        or changed
                    )
                registered.append(dict(existing))
            if changed:
                self._persist_evidence_artifact()
        return registered

    def _persist_evidence_artifact(self, *, artifact_id: str = "") -> None:
        """Persist trusted refs and provider-returned search lineage."""

        run_coro(
            self.control.upsert_artifact(
                run_id=self.run_id,
                kind="evidence_bundle",
                session_id=None,
                title="Kernel evidence",
                status="ready",
                content_markdown="",
                payload={
                    "schema_version": 1,
                    "web_search_ledger": dict(self.web_search_ledger),
                },
                refs=list(self.evidence_refs.values()),
                updated_by="agent",
                artifact_id=artifact_id
                or f"art_{self.run_id[-12:]}_evidence",
            )
        )

    def register_web_search_ledger(
        self,
        ledger: dict[str, Any],
        references: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> list[dict[str, Any]]:
        """Persist provider search output and link its references to it.

        The ledger is a read-only projection for audit and Canvas display. It
        never fetches a source and never determines whether information may be
        used in an answer.
        """

        from inqtrix.evidence import (
            attach_web_search_lineage,
            merge_web_search_ledgers,
        )

        with self._evidence_lock:
            merged = merge_web_search_ledgers(
                [self.web_search_ledger, ledger]
            )
            changed = merged != self.web_search_ledger
            self.web_search_ledger = merged
            if changed:
                self._persist_evidence_artifact()
        if not references:
            return []
        projected = attach_web_search_lineage(
            [dict(reference) for reference in references],
            self.web_search_ledger,
        )
        return self.register_references(projected)

    def register_instant_web_search(self, output: Any) -> list[dict[str, Any]]:
        """Register one ``web.search.instant`` result without re-fetching it."""

        from inqtrix.agents.evidence import (
            enrich_instant_evidence,
            shorten_grounded_support,
        )
        from inqtrix.evidence import build_instant_web_search_ledger

        query_id = str(output.query_id)
        query = str(output.query)
        provider_answer = str(output.answer or "")
        sources = [
            (
                source.model_dump(mode="json")
                if hasattr(source, "model_dump")
                else dict(source)
            )
            for source in list(getattr(output, "sources", []) or [])
        ]
        ledger = build_instant_web_search_ledger(
            run_id=self.run_id,
            query_id=query_id,
            query=query,
            provider=str(output.provider),
            answer=provider_answer,
            sources=sources,
            parameters=dict(output.parameters or {}),
            started_at=str(output.started_at or ""),
            finished_at=str(output.finished_at or ""),
            duration_ms=int(getattr(output, "duration_ms", 0) or 0),
            prompt_tokens=int(output.prompt_tokens or 0),
            completion_tokens=int(output.completion_tokens or 0),
        )
        references = enrich_instant_evidence(
            provider_answer,
            [
                {
                    "url": str(source.get("url") or ""),
                    "title": str(source.get("title") or "") or None,
                    "provider_snippet": (
                        str(source.get("snippet") or "") or None
                    ),
                    "source_rank": int(source.get("rank") or index),
                    "origin": str(source.get("origin") or "provider"),
                }
                for index, source in enumerate(sources, start=1)
                if source.get("url")
            ],
        )
        if not references and provider_answer.strip():
            # Azure may return one coherent grounded answer without exposing
            # individual URL rows. Preserve that search result as a citable
            # ledger pointer instead of making the evidence disappear. The
            # full, unmodified answer remains in ``web_search_ledger``; this
            # bounded field is only the compact synthesis projection.
            support = shorten_grounded_support(provider_answer)
            identity = hashlib.sha1(
                f"web-search:{self.run_id}:{query_id}".encode("utf-8")
            ).hexdigest()[:16]
            references = [
                {
                    "reference_id": f"ref_{identity}",
                    "query_id": query_id,
                    "query_ids": [query_id],
                    "source_run_id": self.run_id,
                    "source_run_ids": [self.run_id],
                    "title": query.strip() or "Web search result",
                    "grounded_support": support,
                    "evidence_kind": "provider_search_answer",
                    "provider": str(output.provider),
                }
            ]
        return self.register_web_search_ledger(ledger, references)

    def _ensure_reference_label(
        self, ref_id: str, ref: dict[str, Any]
    ) -> bool:
        """Assign one stable K/W label inside the locked kernel ledger."""
        import re

        label = str(ref.get("label") or "")
        owners = {
            str(item.get("label") or ""): existing_id
            for existing_id, item in self.evidence_refs.items()
        }
        if re.fullmatch(r"[KW][1-9][0-9]*", label) and owners.get(
            label, ref_id
        ) == ref_id:
            return False
        prefix = "K" if ref.get("document_id") is not None else "W"
        numbers = [
            int(match.group(1))
            for known in owners
            if (match := re.fullmatch(fr"{prefix}([1-9][0-9]*)", known))
        ]
        next_number = max(numbers, default=0) + 1
        ref["label"] = f"{prefix}{next_number}"
        if label:
            # Collision renumber (typically a child-ledger merge): never
            # silent — a copied child citation like [W3] now means the
            # NEW label here (the child tool output surfaces the mapping).
            log.info(
                "Evidence label reassigned after a kernel ledger collision: run_id=%s "
                "reference_id=%s existing_labels=%d",
                self.run_id,
                ref_id,
                len(owners),
            )
        return ref["label"] != label

    def resolve_reference_ids(
        self, reference_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Resolve model-selected ids against trusted evidence only."""
        with self._evidence_lock:
            unknown = [
                item for item in reference_ids if item not in self.evidence_refs
            ]
            if unknown:
                raise ValueError(
                    "Unknown reference_id(s): " + ", ".join(unknown)
                )
            return [dict(self.evidence_refs[item]) for item in reference_ids]

    @property
    def visible_to(self) -> Any:
        """The owner's per-run visibility context (UserContext or None).

        Child runs are created with the parent's principal, so reading
        a child row/result/outcome must present the SAME visibility the
        store authorizes against — under RLS a ``visible_to=None`` read
        only sees anonymous rows (``created_by_user_id IS NULL``), so an
        authenticated parent projecting its child would be denied
        (``RunNotFound``). Resolved once per segment on the capability
        context; mirrors the mission engine's ``deps.visible_to``.
        """
        return getattr(self.capability_context, "visible_to", None)

    @property
    def context_archive_prefix(self) -> str:
        """The run-local archive catalogue identity."""
        from inqtrix.agents.context_archive import archive_prefix

        return archive_prefix(self.run_id)

    def append_context_archive(self, section_title: str, text: str) -> str:
        """Store one logical source; its first reference supports continuation."""
        ids = self.append_context_archive_chunked(section_title, text)
        if not ids:
            raise ValueError("Empty captured text has no archive source.")
        return ids[0]

    def append_private_context_archive(self, title: str, text: str, messages: list[Any], representation: str) -> str:
        from inqtrix.agents.kernel.transcript import transcript_descriptor

        run_coro(self.source_service.assert_scope(self.capability_context))
        encoded = [message.model_dump(mode="json") for message in messages]
        descriptor = transcript_descriptor(self, encoded, text, representation)
        records = run_coro(self.control.put_context_archive(
            run_id=self.run_id, title=title, text=text, transcript=descriptor,
        ))
        if not records:
            raise ValueError("Private archive returned no source.")
        return records[0].artifact_id

    def append_context_archive_chunked(
        self, section_title: str, text: str
    ) -> list[str]:
        """Commit unchanged text parts atomically through the existing store.

        Part ids remain useful for direct reads. Each part also identifies its
        complete source, version and original position; no Markdown repair is
        written back into the captured data. Identical writes are idempotent.
        """
        if text == "":
            return []
        records = run_coro(self.control.put_context_archive(
            run_id=self.run_id, title=section_title, text=text,
        ))
        return [record.artifact_id for record in records]

    def remember_source_receipt(self, receipt: dict[str, Any]) -> None:
        from inqtrix.agents.context_sources import merge_source_receipts

        if receipt:
            with self._source_receipt_lock:
                self._source_receipt = merge_source_receipts([self._source_receipt, receipt])

    def source_receipt(self) -> dict[str, Any]:
        from copy import deepcopy

        with self._source_receipt_lock:
            return deepcopy(self._source_receipt)

    def hydrate_source_receipts(self, state: Any) -> None:
        from inqtrix.agents.context_sources import SOURCE_RECEIPT

        values = dict(getattr(state, "values", {}) or {})
        self.remember_source_receipt(values.get(SOURCE_RECEIPT) or {})
        for message in values.get("messages") or []:
            if getattr(message, "type", "") != "tool" or getattr(message, "name", "") not in {"read_context", "search_context", "read_canvas"}:
                continue
            artifact = getattr(message, "artifact", None)
            if isinstance(artifact, dict):
                self.remember_source_receipt(artifact.get(SOURCE_RECEIPT) or {})

    def check_source_authority(self) -> None:
        """Revalidate transferred originals without changing budget timing."""
        receipt = self.source_receipt()
        if receipt:
            from inqtrix.capabilities import CapabilityError
            from inqtrix.execution_authority import AuthorizationRevoked
            from inqtrix.sync_bridge import run_coro_sync

            if self.source_service is None:
                raise AuthorizationRevoked("Conversation source authorization is unavailable.")
            try:
                run_coro_sync(self.source_service.validate_receipt(self.capability_context, receipt))
            except CapabilityError as exc:
                raise AuthorizationRevoked("Previously read conversation sources are no longer authorized.") from exc

    def check_abort(self) -> None:
        """Stop the loop at the model-call boundary (cancel or budget).

        Mirrors the research graph's node-boundary check
        (``state.raise_if_cancelled``): best-effort — an in-flight
        provider HTTP call is not interrupted, the NEXT model turn is.

        Raises:
            AgentCancelled: The cancel event is set, or the configured
                token budget is reached by the run-cumulative total.
        """
        from inqtrix.exceptions import AgentCancelled, AgentTokenBudgetExceeded

        if self.cancel_token is not None and self.cancel_token.is_set():
            raise AgentCancelled("Run cancelled.")
        self.check_source_authority()
        if self.token_budget:
            used = sum(self.run_usage().values())
            if used >= self.token_budget:
                log.warning(
                    "Kernel run stopped by token budget: %d/%d tokens "
                    "(INQTRIX_QUOTA_MAX_TOKENS_PER_RUN).",
                    used,
                    self.token_budget,
                )
                raise AgentTokenBudgetExceeded(
                    "Run stopped by token budget (max_tokens_per_run)."
                )


_KERNEL_DEPS: contextvars.ContextVar[KernelDeps | None] = (
    contextvars.ContextVar("inqtrix_kernel_deps", default=None)
)


def kernel_deps() -> KernelDeps:
    deps = _KERNEL_DEPS.get()
    if deps is None:  # pragma: no cover - graph never runs undepped
        raise RuntimeError("Kernel dependencies are not set for this segment.")
    return deps


def set_kernel_deps(deps: KernelDeps | None) -> None:
    _KERNEL_DEPS.set(deps)


def run_coro(coro: Any) -> Any:
    """Drive one control-store coroutine from the sync worker thread."""
    return asyncio.run(coro)
