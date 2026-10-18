"""Algorithm-neutral request and result models.

``RunRequest`` is what an application service hands to an algorithm;
``AgentResult`` is what comes back. ``AgentResult`` is deliberately
lean: the answer, its result type, and the algorithm's raw result dict,
which carries evidence, usage and result state. Extensions are additive.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


WebRecency = Literal["day", "week", "month", "year"]

# Visible request bounds for the canvas context. Violations are
# REJECTED with the offending field named — never silently truncated
# (no-silent-caps doctrine): the full text always either arrives intact
# or the caller learns exactly why it did not.
CANVAS_CONTEXT_MAX_COMMENTS = 20
CANVAS_QUOTE_MAX_CHARS = 2_000
CANVAS_QUOTE_CONTEXT_MAX_CHARS = 500
CANVAS_COMMENT_MAX_CHARS = 4_000


class CanvasComment(BaseModel):
    """One user comment anchored to a text selection in a canvas document.

    The anchor triple (``quote`` plus optional before/after context)
    follows the editor's anchoring convention; the server passes it to
    the model verbatim and never re-resolves it.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Canvas artifact the comment anchors to.",
    )
    """Canvas artifact the comment anchors to."""
    revision: int = Field(
        ...,
        ge=1,
        description="Artifact revision the user commented on.",
    )
    """Artifact revision the user commented on."""
    quote: str = Field(
        ...,
        min_length=1,
        max_length=CANVAS_QUOTE_MAX_CHARS,
        description="The selected text, verbatim.",
    )
    """The selected text, verbatim."""
    quote_before: str = Field(
        "",
        max_length=CANVAS_QUOTE_CONTEXT_MAX_CHARS,
        description="Text immediately before the selection (anchor context).",
    )
    """Text immediately before the selection (anchor context)."""
    quote_after: str = Field(
        "",
        max_length=CANVAS_QUOTE_CONTEXT_MAX_CHARS,
        description="Text immediately after the selection (anchor context).",
    )
    """Text immediately after the selection (anchor context)."""
    comment: str = Field(
        ...,
        min_length=1,
        max_length=CANVAS_COMMENT_MAX_CHARS,
        description="The user's note about the selection, verbatim.",
    )
    """The user's note about the selection, verbatim."""


class CanvasContext(BaseModel):
    """Structured canvas attachment of an Agent Desk submission.

    Travels as a dedicated request field — NEVER serialized into
    ``question`` (the question column is clipped at persistence, reaches
    share-inbox titles before acceptance, and renders raw in the chat
    bubble). Snapshot semantics: the context reflects the moment of
    submission and is frozen with the first run segment's checkpointed
    user message — later canvas edits never rewrite it.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="The canvas document the user had open.",
    )
    """The canvas document the user had open."""
    revision: int = Field(
        ...,
        ge=1,
        description="Revision of that document at submission time.",
    )
    """Revision of that document at submission time."""
    comments: tuple[CanvasComment, ...] = Field(
        default=(),
        max_length=CANVAS_CONTEXT_MAX_COMMENTS,
        description="Queued selection comments, in queue order.",
    )
    """Queued selection comments, in queue order."""


class SourcePolicy(BaseModel):
    """Per-run availability of the Agent Desk's external sources."""

    model_config = ConfigDict(extra="forbid")

    web: Literal["available", "disabled"] = Field(
        "available",
        description=(
            "Whether web tools are available to this run. The default "
            "preserves the historical tool surface."
        ),
    )
    """Whether web tools are available to this run; defaults to available for backward compatibility."""
    knowledge: Literal["available", "disabled"] = Field(
        "available",
        description=(
            "Whether project-knowledge tools are available to this run. "
            "The default preserves the historical tool surface."
        ),
    )
    """Whether project-knowledge tools are available to this run; defaults to available for backward compatibility."""


class ReleasedSource(BaseModel):
    """One source an internal role may re-read: what its parent run already reached."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, description="Coverage kind of the reader that owns the source.")
    """Coverage kind of the reader that owns the source (``knowledge_document``, ``editor_document``, ``conversation_source``, ``canvas``)."""
    source_id: str = Field(..., min_length=1, description="The reader's own id of the source.")
    """The reader's own id of the source."""
    version: str = Field("", description="Version the parent run read or produced.")
    """Version the parent run read or produced; informational, the reader reports the current one."""
    title: str = Field("", description="Display title, when the parent knows one.")
    """Display title, when the parent knows one."""


class HandoverOutput(BaseModel):
    """One output of the parent run that the role works on."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["chat", "canvas"] = Field(..., description="Chat answer or canvas document.")
    """Chat answer or canvas document."""
    artifact_id: str = Field("", description="Canvas artifact id; empty for chat.")
    """Canvas artifact id; empty for chat."""
    revision: int = Field(0, ge=0, description="Canvas revision at handover time; 0 for chat.")
    """Canvas revision at handover time; 0 for chat."""
    title: str = Field("", description="Canvas title; empty for chat.")
    """Canvas title; empty for chat."""
    text: str = Field("", description="The output verbatim; empty for a canvas that is read by address.")
    """The output verbatim. A chat answer always travels verbatim. A canvas travels as an address and is read through its windowed reader; it carries its text only when the receiver has no reader."""


class HandoverClarification(BaseModel):
    """One clarifying question the run under work asked, and what came of it.

    Read by server code from the run's stored clarification rounds, never
    from a transcript. The question and the option wording are the
    assistant's words; only the choice of an option and the free text come
    from the user. A question the user did not answer is an assumption,
    never an answer.
    """

    model_config = ConfigDict(extra="forbid")

    round: int = Field(..., ge=1, description="Clarification round, counted from 1 in asking order.")
    """Clarification round, counted from 1 in the order the run asked; the questions of one round share it."""
    question: str = Field(..., description="The question as the run asked it.")
    """The question as the run asked it, in the assistant's words."""
    chosen_options: tuple[str, ...] = Field(default=(), description="Wording of the options the user chose.")
    """Wording of the options the user chose: the choice is the user's, the words are the assistant's."""
    free_text: str = Field("", description="What the user wrote in their own words.")
    """What the user wrote in their own words; empty when the user only chose."""
    status: Literal["answered", "unanswered"] = Field(..., description="Whether the user answered the question.")
    """``answered`` when the user chose an option or wrote an answer, otherwise ``unanswered``."""
    default_assumption: str = Field("", description="The assumption the run continued with when the question stayed unanswered.")
    """The assumption the run continued with when the question stayed unanswered; empty for an answered question and for a round that recorded none."""


class WorkHandover(BaseModel):
    """Typed, self-contained assignment of one internal role run.

    Set by server code only and rendered by ONE function into the receiver's
    first model input. For a role run it is stored with the child's request,
    and large originals travel as addresses (:class:`ReleasedSource`,
    ``evidence_run_id``), never as copies.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(..., min_length=1, description="What the role has to deliver.")
    """What the role has to deliver."""
    assignment: str = Field("", description="The effective assignment of the parent run, as its model saw it.")
    """The effective assignment of the parent run, as its model saw it (user message, server policy, resolved skill inputs)."""
    clarifications: tuple[HandoverClarification, ...] = Field(
        default=(), description="The clarifying questions of the run under work and their answers, oldest first."
    )
    """The clarifying questions of the run under work and what came of each, oldest first: the server's record of a step the assignment may order (ask first), rendered in a fence of its own after the assignment. Empty when the run asked nothing."""
    brief: str = Field("", description="The runtime-built work order of a producing role.")
    """The work order of a role that PRODUCES a part of its parent's output (a memo section) rather than judging it: built by the runtime from the same builder the parent's single call uses, so the two transports of one piece of work carry one text. Empty for a judging role, whose assignment is a requirement to check against instead."""
    outputs: tuple[HandoverOutput, ...] = Field(default=(), description="Outputs of the parent run under work.")
    """Outputs of the parent run under work."""
    evidence_run_id: str = Field("", description="Run whose evidence ledger the role re-reads.")
    """Run whose evidence ledger the role re-reads through ``read_evidence``; empty when the role needs none."""
    evidence_references: int = Field(
        0, ge=0, description="How many references lie behind the evidence address."
    )
    """How many references behind ``evidence_run_id`` the role has to work through, beyond :attr:`released_sources`.

    A handover carries addresses, not copies, so its own size says nothing
    about the work it assigns: the review of a run with one output and
    twenty-two sources looks exactly like the review of a run with one. This
    is the one number that lets the size travel WITH the address — a count,
    not a list, because a list would be the copy the address exists to
    avoid. Counted once: a reference whose document is already released
    travels as that released source, so the sum of the three fields counts
    no subject twice."""
    released_sources: tuple[ReleasedSource, ...] = Field(default=(), description="Sources the role may re-read.")
    """Sources the role may re-read: exactly what the parent run read or produced."""
    diagnostics: tuple[dict[str, Any], ...] = Field(default=(), description="Compact run-internal measurements.")
    """Compact run-internal measurements the role may cite by ``basis_id``."""
    stored_evidence: bool = Field(True, description="Whether the run under work holds stored evidence.")
    """Whether the run under work holds anything a statement can be checked against (a source, a released document, a run diagnostic, an unavailable origin). Without any, its outputs rest on general knowledge, and the rendered handover says so once (knowledge mode). Defaults to the reading every handover had before the field existed."""
    criteria: tuple[str, ...] = Field(default=(), description="Explicit success criteria of the run under work.")
    """Explicit success criteria of the run under work. The result accounts for each exactly once, by its number; empty when that run has none."""
    expected_result: str = Field("", description="What the typed result has to contain.")
    """What the typed result has to contain."""
    open_gaps: tuple[str, ...] = Field(default=(), description="Gaps the parent already knows about.")
    """Gaps the parent already knows about (unavailable origins, unread remainders)."""
    language: str = Field("", description="Answer language code of the parent run.")
    """Answer language code of the parent run; the role writes its findings in it."""


class RunRequest(BaseModel):
    """One execution request, independent of the HTTP wire format.

    The HTTP layer (chat completions, native runs) and the library
    layer (``ResearchAgent``) both normalize into this shape before
    dispatching through the :class:`~inqtrix.core.algorithms.AlgorithmRegistry`.
    """

    model_config = ConfigDict(extra="forbid")

    mode: str = Field(
        "research",
        description=(
            "Algorithm id to execute (a registered "
            "``AgentAlgorithm.id``). Validation happens at registry "
            "lookup, not here, so the request model stays decoupled "
            "from the registered set."
        ),
    )
    """Algorithm id to execute (a registered ``AgentAlgorithm.id``). Validation happens at registry lookup, not here, so the request model stays decoupled from the registered set."""
    question: str = Field(
        ...,
        description=(
            "The normalized current user question/task. For chat "
            "protocols this is the text of the latest user message."
        ),
    )
    """The normalized current user question/task. For chat protocols this is the text of the latest user message."""
    history: str = Field(
        "",
        description=(
            "Pre-formatted conversation history block handed to the "
            "agent for context. Formatting (role labels, truncation) "
            "is a protocol concern resolved by the calling service, "
            "not by the algorithm."
        ),
    )
    """Pre-formatted conversation history block handed to the agent for context. Formatting (role labels, truncation) is a protocol concern resolved by the calling service, not by the algorithm."""
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The raw chat messages array as received, for algorithms "
            "or diagnostics that need more than the flattened "
            "``history`` block. Optional; native run submissions may "
            "leave it empty."
        ),
    )
    """The raw chat messages array as received, for algorithms or diagnostics that need more than the flattened ``history`` block. Optional; native run submissions may leave it empty."""
    agent_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Echo of the validated per-request overrides (already "
            "APPLIED to the resolved settings carried by the run "
            "context). Kept on the request for run summaries and "
            "audit, not re-applied by algorithms."
        ),
    )
    """Echo of the validated per-request overrides (already APPLIED to the resolved settings carried by the run context). Kept on the request for run summaries and audit, not re-applied by algorithms."""
    knowledge_filters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Knowledge-retrieval scope for ``mode=knowledge`` requests "
            "(``collection_ids`` list; optional ``profile``, ``top_k``, and "
            "``final_k`` overrides). ``top_k`` is the per-query retrieval width "
            "(1..50); ``final_k`` pins the surfaced-evidence count "
            "(1..EVIDENCE_K_MAX), overriding the profile factor. Inert for "
            "algorithms that do not retrieve from internal documents."
        ),
    )
    """Knowledge-retrieval scope for ``mode=knowledge`` requests (``collection_ids`` list; optional ``profile``, ``top_k``, ``final_k`` overrides). ``top_k`` is the per-query retrieval width (1..50); ``final_k`` pins the surfaced-evidence count (1..EVIDENCE_K_MAX), overriding the profile factor. Inert for algorithms that do not retrieve from internal documents."""
    ui_language: Literal["", "de", "en"] = Field(
        default="",
        description=(
            "Interface language of a ``mode=knowledge`` ask (``de``/``en``): "
            "the answer language when the question's own language is "
            "unclear. Empty when the client sends none."
        ),
    )
    """Interface language of a Knowledge Desk ask, the answer language only where the question's own language is unclear (``resolve_answer_language``): a question with a clear language is answered in it whatever the interface shows. The HTTP route accepts it for knowledge asks only; empty everywhere else."""
    autonomy: str = Field(
        default="",
        description=(
            "Workspace-agent permission mode (strict/balanced/autonomous); "
            "empty for every other mode."
        ),
    )
    """Workspace-agent permission mode (``strict``/``balanced``/``autonomous``); empty for every other mode. The route validates the vocabulary, the agent algorithm consumes it — inert everywhere else."""
    session_id: str = Field(
        default="",
        description=(
            "Agent-desk session the run belongs to; empty when sessionless."
        ),
    )
    """Agent-desk session the run belongs to; empty when the run is sessionless. Mirrors the run row's ``session_id`` so the agent algorithm can anchor the session memo without a store read."""
    document_id: str = Field(
        default="",
        description=(
            "Target editor document for a workspace-agent patch assignment "
            "and empty when the run has no edit target."
        ),
    )
    """Target editor document for a workspace-agent patch assignment. When set, the agent proposes an ``editor_patch`` against this document after synthesis and parks for the patch approval, which is gated in every autonomy mode; the agent never applies the patch itself. Empty (the default) skips the patch phase entirely — inert for every other mode."""
    response_form: str = Field(
        default="",
        description=(
            "Workspace-agent output-form override: ``chat`` "
            "forces the inline chat answer, ``canvas`` the session memo; "
            "empty lets the intake profile decide. Inert for every "
            "other mode."
        ),
    )
    """Workspace-agent output-form override. ``chat`` forces the run-local inline answer artifact, ``canvas`` the session memo canvas; empty (the default, wire value ``auto``) delegates the decision to the intake profile's ``response_form``. A patch assignment (``document_id`` set) always uses canvas — the patch pipeline consumes the memo as source material. Inert for every other mode."""
    skill_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "Explicitly attached skill ids from the composer chips. "
            "Admission (visibility, count cap) happens at the runs "
            "router; the agent runtime loads and enforces them."
        ),
    )
    """Explicitly attached skill ids. The runs router admits them (visible-to-caller check — an invisible skill is a loud 404 — and the ``skills_max_attached`` cap); the agent runtime injects their instructions, runs the clarification point check, and applies ``requires_plan``/``allowed_tools``. Inert for every non-agent mode."""
    skill_revisions: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Server-admitted integer revision for every attached skill; "
            "used to prevent policy drift between admission and resume."
        ),
    )
    """Server-pinned skill revisions keyed by skill id; clients do not supply this field directly."""
    tool_directives: tuple[str, ...] = Field(
        default=(),
        description=(
            "Hard planner/kernel tool hints from the composer's "
            "``/``-menu functions group, for example web_research."
        ),
    )
    """Hard tool hints from the composer's ``/``-functions (``web_research``, ``rag_query``) telling the planner/kernel the user explicitly asked for this tool family. Whitelisted at the runs router (unknown directive = 400). Inert for every non-agent mode."""
    source_policy: SourcePolicy = Field(
        default_factory=SourcePolicy,
        description=(
            "Per-run availability of web and project-knowledge tools. "
            "Missing means both sources remain available."
        ),
    )
    """Web and project-knowledge availability inherited by delegated agent work; both are available when omitted."""
    web_recency: WebRecency | None = Field(
        default=None,
        description=(
            "Provider-neutral web recency filter inherited by a delegated "
            "research run. Null lets the research graph infer recency from "
            "the question."
        ),
    )
    """Provider-neutral recency filter for delegated web research; null delegates to graph classification."""
    canvas_context: CanvasContext | None = Field(
        default=None,
        description=(
            "Canvas attachment of an agent-kernel submission: the open "
            "document (id + revision) and queued selection comments. "
            "None for every other mode and for runs without canvas "
            "context."
        ),
    )
    """Canvas attachment of an agent-kernel submission. Injected into the kernel user message as a fenced data section; frozen with the first segment's checkpoint; never inherited by child runs and never part of run summaries. ``None`` everywhere else."""
    report_requirement: str = Field(
        "",
        description=(
            "Composed result requirement set BEFORE the run: how the "
            "result has to look (structure, focus, audience). Already "
            "resolved and composed server-side from free text plus "
            "attached library rules. Empty when the user set none."
        ),
    )
    """Result requirement set at submit time, composed server-side (free text + attached library rules, each with its origin marker); the HTTP route refuses a client value for it. The only way to state one for runs that never reach a plan gate — ``autonomous``, the speed tier, and the kernel, which has no plan gate at all. A later plan-gate decision REPLACES it; an approval that says nothing leaves it standing. Inherited, together with ``report_rules``, by exactly one kind of child: the single mission a kernel run starts with ``run_deep_mission``, whose memo is that run's deliverable. Research children, the mission strands of ``delegate_batch`` and role runs never inherit it."""
    report_rules: tuple[dict[str, Any], ...] = Field(
        default=(),
        description=(
            "The parts the submit-time requirement was composed from: "
            "{template_id, label, title, revision} per distinct attached library "
            "rule, resolved server-side from the caller's own catalog. "
            "Server-set only; empty when no rule was attached."
        ),
    )
    """Library rules the submit-time ``report_requirement`` was composed from, one ``{template_id, label, title, revision}`` per distinct rule in attachment order. Resolved at submission and frozen with the run: a template edited afterwards changes nothing here, and resume never re-resolves. The kernel binds its transcripts to these revisions so another run can read them only while the rules are unchanged and still visible. Server-set only — the HTTP route refuses a client value. Travels to a child only with ``report_requirement`` (the single ``run_deep_mission`` child); empty everywhere else."""
    attached_reports: tuple[dict[str, Any], ...] = Field(
        default=(),
        description=(
            "Research-Desk reports the user attached, already resolved "
            "server-side to {report_id, title, reference_count}. Names "
            "only — the bodies are fetched by the kernel tool."
        ),
    )
    """Research reports attached to an agent-kernel run, resolved against the caller's own visibility at submit time. Only the NAMES travel: a real report has a median of ~54k characters, and its sources become citable only by passing through the run's evidence ledger, which ``read_research_report`` does on demand. Never inherited by child runs; empty everywhere else."""
    work_profile: str = Field(
        "",
        description=(
            "Name of the internal work profile this run executes under. "
            "Set by server code when a run delegates a bounded role to a "
            "child; never accepted from a client."
        ),
    )
    """Internal work profile of a role run: a bounded tool surface, a typed handover in and a typed result out instead of a published answer. Empty for every ordinary run. Server-set only — the HTTP routes reject it."""
    handover: WorkHandover | None = Field(
        default=None,
        description=(
            "Typed assignment of a role run. Present exactly when "
            "work_profile is set."
        ),
    )
    """Typed assignment of a role run; rendered by one function into the run's first model input. ``None`` for every ordinary run."""


class AgentResult(BaseModel):
    """Outcome of one algorithm execution.

    Carries the answer plus the raw provider-shaped result dict the
    HTTP layer's serialization consumes (``ResearchResult.from_raw``,
    usage extraction, cancel detection). ``raw`` stays verbatim, so every
    algorithm behind the registry yields wire payloads built from the
    same dict shape.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(
        ...,
        description="Final answer text (empty for cancelled runs).",
    )
    """Final answer text (empty for cancelled runs)."""
    result_type: str = Field(
        "research_result",
        description=(
            "Discriminator for consumers that render different result "
            "kinds (research report, direct chat, knowledge answer, "
            "agent result)."
        ),
    )
    """Discriminator for consumers that render different result kinds (research report, direct chat, knowledge answer, agent result)."""
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The unmodified graph/provider result dict (``answer``, "
            "``usage``, ``result_state``). Services build wire "
            "payloads from this dict so existing contracts stay "
            "byte-identical; typed projections such as "
            "``ResearchResult.from_raw`` read from it and never "
            "replace it."
        ),
    )
    """The unmodified graph/provider result dict (``answer``, ``usage``, ``result_state``). Services build wire payloads from this dict so existing contracts stay byte-identical; typed projections such as ``ResearchResult.from_raw`` read from it and never replace it."""

    @property
    def cancelled(self) -> bool:
        """Whether the underlying run finished in the cancelled state."""
        result_state = self.raw.get("result_state") or {}
        return bool(result_state.get("cancelled"))

    @property
    def cancel_reason(self) -> str:
        """Machine-readable cancellation cause emitted by the algorithm."""
        result_state = self.raw.get("result_state") or {}
        return str(result_state.get("cancel_reason") or "")

    @property
    def terminal_failure(self) -> "AgentTerminalFailure | None":
        """Typed projection of the shared returned-failure contract.

        Algorithms sometimes need to return usage and a safe diagnostic after
        a provider/model call while still declaring the run unsuccessful.  The
        native run service already consumes ``result_state._terminal_failure``;
        this projection lets chat and Agent adapters enforce the same contract
        without re-parsing untyped dictionaries or inventing a second status.
        Malformed failure markers fail closed as ``server_error``.
        """

        result_state = self.raw.get("result_state") or {}
        raw = result_state.get("_terminal_failure")
        if raw is None:
            return None
        try:
            return AgentTerminalFailure.model_validate(raw)
        except (TypeError, ValueError):
            return AgentTerminalFailure(
                type="server_error",
                message="The algorithm returned an invalid terminal failure marker.",
            )

    @property
    def successful(self) -> bool:
        """Whether the result is neither cancelled nor terminally failed."""

        return not self.cancelled and self.terminal_failure is None


class AgentTerminalFailure(BaseModel):
    """Stable failure returned with an :class:`AgentResult` and its usage."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        ...,
        min_length=1,
        description="Stable machine-readable execution failure code.",
    )
    message: str = Field(
        ...,
        min_length=1,
        description="Safe user-visible explanation of the failed result.",
    )
